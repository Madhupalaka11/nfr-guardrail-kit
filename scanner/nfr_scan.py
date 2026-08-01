#!/usr/bin/env python3
"""
nfr_scan.py — deterministic NFR gap scanner (Phase 1 of the NFR Guardrail).

Walks a repository, evaluates every rule in nfr_rules.yaml, and emits:
  - findings.json   (machine output — consumed by Copilot/LLM via MCP)
  - nfr_report.md   (human output — the SRE gap report, no pass/fail verdict)

Usage:
  python nfr_scan.py /path/to/repo [--rules path/to/nfr_rules.yaml] [--out outdir]

Design notes:
  - "present" rules  -> anti-pattern found  -> one finding PER FILE (+line).
  - "absent" rules   -> required safeguard missing across the whole repo
                        -> ONE repo-level finding.
  - "applies_if"     -> tech-detection gate so Java rules don't fire on a
                        pure Node repo, K8s rules only fire when Deployment
                        manifests exist, etc.
"""

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")

SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", ".venv", "venv",
             "__pycache__", ".idea", ".vscode", "vendor", ".terraform", "coverage"}
MAX_FILE_BYTES = 1_000_000
SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3}

PILLAR_LABELS = {
    "availability": "Availability & HA",
    "reliability": "Reliability",
    "performance": "Performance & Latency",
    "resilience": "Resilience & Defensive Architecture",
    "security": "Security & Governance",
    "observability": "Observability & Telemetry",
    "operability": "Deployment & Operability",
}


def collect_files(repo: Path):
    files = []
    for p in repo.rglob("*"):
        if p.is_dir():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(p)
    return files


def match_globs(path: Path, repo: Path, globs):
    rel = str(path.relative_to(repo)).replace("\\", "/")
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, g) for g in globs)


def read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def gate_passes(rule, repo, files, cache):
    gate = rule.get("detect", {}).get("applies_if")
    if not gate:
        return True
    rx = re.compile(gate["pattern"])
    for f in files:
        if match_globs(f, repo, gate["globs"]):
            if rx.search(cache.setdefault(f, read_text(f))):
                return True
    return False


def line_of(text, span_start):
    return text.count("\n", 0, span_start) + 1


def scan(repo: Path, rules_path: Path):
    spec = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules = spec["rules"]
    files = collect_files(repo)
    cache = {}
    findings = []

    for rule in rules:
        det = rule["detect"]
        targets = [f for f in files if match_globs(f, repo, det["globs"])]
        if not gate_passes(rule, repo, files, cache):
            continue

        base = {
            "rule_id": rule["id"],
            "pillar": rule["pillar"],
            "layer": rule["layer"],
            "title": rule["title"],
            "severity": rule["severity"],
            "why": " ".join(rule["why"].split()),
            "fix": rule["fix"].rstrip(),
        }

        if det["type"] == "present":
            rx = re.compile(det["pattern"])
            for f in targets:
                text = cache.setdefault(f, read_text(f))
                for m in rx.finditer(text):
                    findings.append({**base,
                                     "where": str(f.relative_to(repo)),
                                     "line": line_of(text, m.start()),
                                     "evidence": m.group(0)[:160].strip()})
                    break  # one finding per file per rule keeps reports readable

        elif det["type"] == "absent":
            rx = re.compile(det["required"])
            if targets and not any(rx.search(cache.setdefault(f, read_text(f))) for f in targets):
                sample = ", ".join(str(f.relative_to(repo)) for f in targets[:3])
                findings.append({**base,
                                 "where": f"repo-wide (checked: {sample}...)",
                                 "line": None,
                                 "evidence": f"No match for required safeguard `{det['required']}`"})

    findings.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], x["pillar"], x["rule_id"]))
    return findings, len(files)


def write_report(findings, repo, out_dir: Path, files_scanned: int):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    lines = [
        f"# NFR Gap Assessment — `{repo.name}`",
        f"_Generated {now} · {files_scanned} files scanned · {len(findings)} gaps identified_",
        "",
        "> This report identifies gaps and recommendations. It intentionally issues",
        "> **no pass/fail verdict** — release decisions remain with the PRR sign-off group.",
        "",
        "## Summary",
        "",
        "| Severity | Count | Meaning |",
        "|---|---|---|",
        f"| 🔴 Blocker | {counts.get('blocker', 0)} | Will cause an incident or audit finding — fix before release |",
        f"| 🟠 High | {counts.get('high', 0)} | Likely to fail under real traffic or dependency failure |",
        f"| 🟡 Medium | {counts.get('medium', 0)} | Degrades MTTR/quality; schedule within the release train |",
        f"| ⚪ Low | {counts.get('low', 0)} | Hygiene / maturity improvement |",
        "",
        "## Findings by Pillar",
        "",
    ]

    by_pillar = {}
    for f in findings:
        by_pillar.setdefault(f["pillar"], []).append(f)

    for pillar, items in by_pillar.items():
        lines.append(f"### {PILLAR_LABELS.get(pillar, pillar.title())} ({len(items)})")
        lines.append("")
        lines.append("| Sev | Rule | Layer | Where (file:line) | Why it matters |")
        lines.append("|---|---|---|---|---|")
        for it in items:
            loc = it["where"] + (f":{it['line']}" if it["line"] else "")
            icon = {"blocker": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}[it["severity"]]
            lines.append(f"| {icon} | {it['rule_id']} — {it['title']} | {it['layer']} | `{loc}` | {it['why']} |")
        lines.append("")
        lines.append("<details><summary>How to fix (paste-ready)</summary>")
        lines.append("")
        for it in items:
            lines.append(f"**{it['rule_id']} — {it['title']}**")
            lines.append("```")
            lines.append(it["fix"])
            lines.append("```")
        lines.append("</details>")
        lines.append("")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nfr_report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "findings.json").write_text(
        json.dumps({"repo": str(repo), "generated": now,
                    "files_scanned": files_scanned, "findings": findings}, indent=2),
        encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--rules", default=str(Path(__file__).parent.parent / "skill" / "rules" / "nfr_rules.yaml"))
    ap.add_argument("--out", default=".nfr-scan")
    ap.add_argument("--print-problems", action="store_true",
                    help="Also print findings as 'path:line: severity: message' for VS Code problem matchers")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        sys.exit(f"Not a directory: {repo}")

    findings, n_files = scan(repo, Path(args.rules))
    out_dir = Path(args.out)
    write_report(findings, repo, out_dir, n_files)
    if args.print_problems:
        sev_map = {"blocker": "error", "high": "error", "medium": "warning", "low": "info"}
        for f in findings:
            loc = f["where"].split(" ")[0]
            print(f"{loc}:{f['line'] or 1}: {sev_map[f['severity']]}: [{f['rule_id']}] {f['title']}")
    print(f"Scanned {n_files} files → {len(findings)} gaps")
    print(f"Report:   {out_dir / 'nfr_report.md'}")
    print(f"Machine:  {out_dir / 'findings.json'}")


if __name__ == "__main__":
    main()
