#!/usr/bin/env python3
"""
NFR Guardrail Console (Enterprise) — Run Mode 3, zero-LLM.

v2 adds the REPO BUNDLE: register any repo by git URL (or local path) with
the branches to track (develop / master / release / ...). The console clones
each tracked branch into a local bundle directory, keeps it fresh with
`git fetch` on every scan, and records history per (repo, branch, commit) —
so NFR debt is comparable across branches and over time.

    pip install fastapi uvicorn pyyaml
    python ui/enterprise/server.py            # → http://127.0.0.1:8787

Design choice (instead of Sourcegraph): plain git + SQLite. No license, no
new infra, approvable anywhere git already is. Bundle layout:
    ui/enterprise/bundle/<repo-name>/<branch>/   ← one working copy per branch

Governance: read-only against repo content; git operations are clone/fetch
only (never push); scan history in local SQLite; no network beyond your git
remote; no LLM anywhere.
"""

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Cookie, Depends
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               Response, JSONResponse, RedirectResponse)
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent.parent
SCANNER = KIT_ROOT / "scanner" / "nfr_scan.py"
DB_PATH = HERE / "console.db"
BUNDLE = HERE / "bundle"

app = FastAPI(title="NFR Guardrail Console")


# ----------------------------- storage --------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS repos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, url TEXT UNIQUE, branches TEXT, added TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT, repo_name TEXT, branch TEXT DEFAULT '-', commit_sha TEXT DEFAULT '',
        ts TEXT, files_scanned INTEGER,
        blocker INTEGER, high INTEGER, medium INTEGER, low INTEGER,
        findings TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER, ts TEXT, source TEXT, content TEXT)""")
    # migrate pre-v2 databases
    cols = [r[1] for r in con.execute("PRAGMA table_info(scans)")]
    if "branch" not in cols:
        con.execute("ALTER TABLE scans ADD COLUMN branch TEXT DEFAULT '-'")
        con.execute("ALTER TABLE scans ADD COLUMN commit_sha TEXT DEFAULT ''")
    return con


# ----------------------------- git helpers ----------------------------
def _git(args, cwd=None, timeout=300):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HTTPException(500, f"git {' '.join(args[:2])} failed: {proc.stderr[-800:]}")
    return proc.stdout.strip()


def repo_slug(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "-", name.removesuffix(".git")) or "repo"


def sync_branch(url: str, name: str, branch: str) -> Path:
    """Clone the branch into the bundle if missing; otherwise fetch + hard
    reset to origin. Returns the working-copy path. Clone/fetch only — never push."""
    dest = BUNDLE / name / branch
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "50", "--branch", branch, "--single-branch",
              url, str(dest)], timeout=600)
    else:
        _git(["fetch", "origin", branch], cwd=dest, timeout=600)
        _git(["reset", "--hard", f"origin/{branch}"], cwd=dest)
    return dest


def head_info(path: Path):
    try:
        sha = _git(["rev-parse", "--short", "HEAD"], cwd=path)
        br = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        return br, sha
    except Exception:
        return "-", ""


# ----------------------------- scan core ------------------------------
def run_profiler(target: Path):
    out_dir = target / ".nfr-scan"
    subprocess.run([sys.executable, str(KIT_ROOT / "scanner" / "service_profiler.py"),
                    str(target), "--out", str(out_dir)],
                   capture_output=True, text=True, timeout=300)
    pf = out_dir / "profile.json"
    return json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else None


def run_scanner(target: Path):
    out_dir = target / ".nfr-scan"
    proc = subprocess.run(
        [sys.executable, str(SCANNER), str(target), "--out", str(out_dir)],
        capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr[-2000:])
    return json.loads((out_dir / "findings.json").read_text(encoding="utf-8"))


def store_scan(repo_path, repo_name, branch, sha, data):
    findings = data["findings"]
    counts = {s: sum(1 for f in findings if f["severity"] == s)
              for s in ("blocker", "high", "medium", "low")}
    con = db()
    cur = con.execute(
        "INSERT INTO scans (repo, repo_name, branch, commit_sha, ts, files_scanned, "
        "blocker, high, medium, low, findings) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(repo_path), repo_name, branch, sha,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         data["files_scanned"], counts["blocker"], counts["high"],
         counts["medium"], counts["low"], json.dumps(findings)))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return {"scan_id": sid, "branch": branch, "commit": sha,
            "files_scanned": data["files_scanned"], "counts": counts,
            "total": len(findings)}


# ----------------------------- API: ad-hoc local scan -----------------
class ScanRequest(BaseModel):
    repo_path: str


@app.post("/api/scan")
def run_scan(req: ScanRequest):
    repo = Path(req.repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise HTTPException(400, f"Not a directory: {repo}")
    branch, sha = head_info(repo)
    run_profiler(repo)
    data = run_scanner(repo)
    out = store_scan(repo, repo.name, branch, sha, data)
    out.update({"repo": str(repo), "repo_name": repo.name})
    return out


# ----------------------------- API: repo bundle -----------------------
class RepoRequest(BaseModel):
    url: str
    branches: list[str] = ["develop", "master", "release"]


@app.post("/api/repos")
def register_repo(req: RepoRequest):
    url = req.url.strip()
    branches = [b.strip() for b in req.branches if b.strip()]
    if not url or not branches:
        raise HTTPException(400, "url and at least one branch required")
    name = repo_slug(url)
    con = db()
    try:
        con.execute("INSERT INTO repos (name, url, branches, added) VALUES (?,?,?,?)",
                    (name, url, json.dumps(branches),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
    except sqlite3.IntegrityError:
        con.execute("UPDATE repos SET branches=? WHERE url=?", (json.dumps(branches), url))
        con.commit()
    con.close()
    return {"name": name, "url": url, "branches": branches}


@app.get("/api/repos")
def list_repos():
    con = db()
    repos = [dict(zip(["id", "name", "url", "branches", "added"], r)) for r in
             con.execute("SELECT id, name, url, branches, added FROM repos ORDER BY name")]
    for r in repos:
        r["branches"] = json.loads(r["branches"])
        r["latest"] = {}
        for b in r["branches"]:
            row = con.execute(
                "SELECT id, ts, commit_sha, blocker, high, medium, low FROM scans "
                "WHERE repo_name=? AND branch=? ORDER BY id DESC LIMIT 1",
                (r["name"], b)).fetchone()
            if row:
                r["latest"][b] = dict(zip(
                    ["scan_id", "ts", "commit", "blocker", "high", "medium", "low"], row))
    con.close()
    return repos


@app.post("/api/repos/{repo_id}/scan")
def scan_repo_branches(repo_id: int):
    """Sync + scan every tracked branch of a registered repo."""
    con = db()
    row = con.execute("SELECT name, url, branches FROM repos WHERE id=?",
                      (repo_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Repo not registered")
    name, url, branches = row[0], row[1], json.loads(row[2])
    results, errors = [], []
    for branch in branches:
        try:
            wc = sync_branch(url, name, branch)
            _, sha = head_info(wc)
            data = run_scanner(wc)
            results.append(store_scan(wc, name, branch, sha, data))
        except HTTPException as e:
            errors.append({"branch": branch, "error": str(e.detail)[:300]})
    return {"repo": name, "scanned": results, "errors": errors}


@app.delete("/api/repos/{repo_id}")
def remove_repo(repo_id: int):
    con = db()
    con.execute("DELETE FROM repos WHERE id=?", (repo_id,))
    con.commit()
    con.close()
    return {"removed": repo_id}


# ----------------------------- API: history & exports -----------------
@app.get("/api/scans")
def list_scans():
    con = db()
    rows = con.execute(
        "SELECT id, repo, repo_name, branch, commit_sha, ts, files_scanned, "
        "blocker, high, medium, low FROM scans ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    keys = ["id", "repo", "repo_name", "branch", "commit", "ts", "files_scanned",
            "blocker", "high", "medium", "low"]
    return [dict(zip(keys, r)) for r in rows]


def _get_scan(scan_id: int):
    con = db()
    row = con.execute(
        "SELECT repo, repo_name, branch, commit_sha, ts, files_scanned, findings "
        "FROM scans WHERE id=?", (scan_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Scan not found")
    return {"repo": row[0], "repo_name": row[1], "branch": row[2], "commit": row[3],
            "ts": row[4], "files_scanned": row[5], "findings": json.loads(row[6])}


@app.get("/api/scan/{scan_id}/findings")
def get_findings(scan_id: int):
    return _get_scan(scan_id)


@app.get("/api/scan/{scan_id}/report.md")
def get_report(scan_id: int):
    s = _get_scan(scan_id)
    report = Path(s["repo"]) / ".nfr-scan" / "nfr_report.md"
    if report.exists():
        return PlainTextResponse(report.read_text(encoding="utf-8"),
                                 media_type="text/markdown")
    raise HTTPException(404, "Report file not found — re-run the scan")


@app.get("/api/scan/{scan_id}/findings.json")
def get_findings_json(scan_id: int):
    return Response(json.dumps(_get_scan(scan_id), indent=2),
                    media_type="application/json")


@app.get("/api/scan/{scan_id}/sarif")
def get_sarif(scan_id: int):
    s = _get_scan(scan_id)
    level = {"blocker": "error", "high": "error", "medium": "warning", "low": "note"}
    rules, seen, results = [], set(), []
    for f in s["findings"]:
        if f["rule_id"] not in seen:
            seen.add(f["rule_id"])
            rules.append({"id": f["rule_id"],
                          "shortDescription": {"text": f["title"]},
                          "help": {"text": f["fix"]},
                          "properties": {"pillar": f["pillar"], "layer": f["layer"]}})
        results.append({"ruleId": f["rule_id"], "level": level[f["severity"]],
                        "message": {"text": f"{f['title']} — {f['why']}"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": f["where"].split(" ")[0]},
                            "region": {"startLine": f["line"] or 1}}}]})
    sarif = {"$schema": "https://json.schemastore.org/sarif-2.1.0.json",
             "version": "2.1.0",
             "runs": [{"tool": {"driver": {"name": "NFR Guardrail",
                                           "informationUri": "internal",
                                           "rules": rules}},
                       "results": results}]}
    return Response(json.dumps(sarif, indent=2), media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="nfr_{s["repo_name"]}_{s["branch"]}.sarif"'})


# ---------------- internal LLM (pluggable, keyless-vendor) ------------
# Configure via ui/enterprise/llm_config.json (gitignored). The day your
# platform team gives you the internal LLM's REST endpoint + auth header,
# fill this in — the AI Insights tab gains a "Generate" button; until then
# the copy/paste bridge remains the path. No Azure/OpenAI keys involved.
LLM_CONFIG_PATH = HERE / "llm_config.json"


def llm_config():
    if LLM_CONFIG_PATH.exists():
        try:
            cfg = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
            if cfg.get("url"):
                return cfg
        except Exception:
            pass
    return None


@app.get("/api/ai-config")
def ai_config():
    cfg = llm_config()
    return {"enabled": bool(cfg), "name": (cfg or {}).get("name", "internal LLM")}


class GenerateRequest(BaseModel):
    prompt: str


@app.post("/api/scan/{scan_id}/ai-generate")
def ai_generate(scan_id: int, req: GenerateRequest):
    """Send the analysis prompt to the configured INTERNAL LLM endpoint and
    store the response as this scan's Deep Analysis. Requires llm_config.json:
    { "name": "InternalGPT", "url": "https://llm.internal/api/chat",
      "auth_header": "Authorization", "auth_value": "Bearer <internal-token>",
      "payload_template": {"prompt": "{PROMPT}"}, "response_path": "answer" }
    payload_template: {PROMPT} is replaced with the prompt text.
    response_path: dot-path to the text in the JSON response."""
    _get_scan(scan_id)
    cfg = llm_config()
    if not cfg:
        raise HTTPException(400, "Internal LLM not configured — create ui/enterprise/llm_config.json (see llm_config.example.json)")
    import urllib.request
    payload = json.dumps(cfg.get("payload_template", {"prompt": "{PROMPT}"})
                         ).replace("{PROMPT}", json.dumps(req.prompt)[1:-1])
    r = urllib.request.Request(cfg["url"], data=payload.encode("utf-8"),
                               headers={"Content-Type": "application/json",
                                        **({cfg["auth_header"]: cfg["auth_value"]}
                                           if cfg.get("auth_header") else {})})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(502, f"Internal LLM call failed: {e}")
    content = body
    for key in cfg.get("response_path", "").split("."):
        if key and isinstance(content, dict):
            content = content.get(key, "")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(502, "Internal LLM returned no text at response_path")
    con = db()
    con.execute("INSERT INTO analyses (scan_id, ts, source, content) VALUES (?,?,?,?)",
                (scan_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 cfg.get("name", "internal LLM"), content[:200_000]))
    con.commit()
    con.close()
    return {"saved": True, "chars": len(content)}


class AnalysisRequest(BaseModel):
    content: str
    source: str = "browser-llm"


@app.post("/api/scan/{scan_id}/analysis")
def save_analysis(scan_id: int, req: AnalysisRequest):
    """Store a Deep Analysis produced by a browser LLM (copy-out/paste-in
    bridge). No keys, no direct LLM connection — the SRE carries the text."""
    _get_scan(scan_id)  # 404 if unknown
    if not req.content.strip():
        raise HTTPException(400, "Empty analysis")
    con = db()
    con.execute("INSERT INTO analyses (scan_id, ts, source, content) VALUES (?,?,?,?)",
                (scan_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 req.source[:60], req.content[:200_000]))
    con.commit()
    con.close()
    return {"saved": True}


@app.get("/api/scan/{scan_id}/analysis")
def get_analysis(scan_id: int):
    con = db()
    row = con.execute("SELECT ts, source, content FROM analyses WHERE scan_id=? "
                      "ORDER BY id DESC LIMIT 1", (scan_id,)).fetchone()
    con.close()
    if not row:
        return {"exists": False}
    return {"exists": True, "ts": row[0], "source": row[1], "content": row[2]}



@app.get("/api/runbooks")
def get_runbooks():
    import yaml
    spec = yaml.safe_load((KIT_ROOT / "skill" / "rules" / "sre_runbooks.yaml")
                          .read_text(encoding="utf-8"))
    return {"runbooks": spec["runbooks"]}

@app.get("/api/kb")
def get_kb():
    import yaml
    spec = yaml.safe_load((KIT_ROOT / "skill" / "rules" / "sre_knowledge_base.yaml")
                          .read_text(encoding="utf-8"))
    return {"entries": spec["entries"]}



import html as html_lib

def _rag_state(counts):
    if counts.get("blocker", 0) > 0:   return "RED",   "NOT READY",    "var(--crit)",  "#B42318"
    if counts.get("high", 0) > 0:      return "AMBER", "CONDITIONAL",  "var(--high)",  "#C2620C"
    return "GREEN", "READY", "var(--ok)", "#12805C"

def _sev_counts(findings):
    c = {"blocker":0,"high":0,"medium":0,"low":0}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"],0)+1
    return c

def _generate_highlevel(scan, profile, findings):
    c = _sev_counts(findings)
    rag, status, rag_css, rag_hex = _rag_state(c)
    svc  = profile.get("service", scan["repo_name"])
    stype= profile.get("service_type",{}).get("value","Unknown")
    stack= ", ".join((profile.get("stack",{}).get("frameworks",[]) or profile.get("stack",{}).get("languages",["Unknown"])))
    ups  = [u["host"] for u in profile.get("upstreams",[])]
    dbs  = [(d["kind"], d.get("target","")) for d in profile.get("datastores",[])]
    msg  = [m["kind"] for m in profile.get("messaging",[])]
    eps  = profile.get("endpoints",[])[:6]
    by_pillar = {}
    for f in findings:
        by_pillar.setdefault(f["pillar"],[]).append(f)
    pillar_rows = ""
    for pillar, items in sorted(by_pillar.items(), key=lambda x:-len(x[1])):
        sc = _sev_counts(items)
        worst = "critical" if sc["blocker"] else ("high" if sc["high"] else ("medium" if sc["medium"] else "low"))
        color = {"critical":"#B42318","high":"#C2620C","medium":"#9A7B14","low":"#64748B"}[worst]
        pillar_rows += f'<tr><td><b>{html_lib.escape(pillar)}</b></td><td style="font-family:var(--mono)">{len(items)}</td><td style="font-family:var(--mono)">{sc["blocker"]}</td><td style="color:{color};font-weight:600">{worst}</td></tr>\n'
    crit_rows = ""
    for f in [x for x in findings if x["severity"] in ("blocker","high")][:10]:
        loc = f["where"].split(" ")[0] + (f":{f['line']}" if f.get("line") else "")
        sev_c = "#B42318" if f["severity"]=="blocker" else "#C2620C"
        crit_rows += f'<tr><td style="font-family:var(--mono);font-size:10px;font-weight:700;color:{sev_c}">{f["severity"].upper()}</td><td style="font-family:var(--mono);font-size:11px">{html_lib.escape(f["rule_id"])}</td><td>{html_lib.escape(f["title"])}</td><td style="font-family:var(--mono);font-size:11px">{html_lib.escape(loc)}</td><td style="font-size:12px">{html_lib.escape(f["why"][:120])}</td></tr>\n'
    ep_chips = "".join(f'<span style="display:inline-block;font-family:var(--mono);font-size:10.5px;background:#EEF3F8;border:1px solid #CFDDEC;border-radius:5px;padding:2px 8px;margin:2px 4px 2px 0"><b style=\"color:#16457E\">{html_lib.escape(e["method"])}</b> {html_lib.escape(e["path"])}</span>' for e in eps)
    up_rows  = "".join(f'<div style=\"font-size:12px;padding:6px 0;border-bottom:1px solid #DCE3EB;color:#37485C\">{html_lib.escape(u)}<span style=\"display:block;font-family:var(--mono);font-size:9.5px;color:#8195A8\">detected</span></div>' for u in ups)
    db_rows  = "".join(f'<div style=\"font-size:12px;padding:6px 0;border-bottom:1px solid #DCE3EB;color:#37485C\">{html_lib.escape(k)} — {html_lib.escape(v)}</div>' for k,v in dbs)
    msg_rows = "".join(f'<div style=\"font-size:12px;padding:6px 0;color:#37485C\">{html_lib.escape(m)}</div>' for m in msg)

    return f"""<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">
<title>NFR Release Readiness — {html_lib.escape(svc)}</title>
<link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap\" rel=\"stylesheet\">
<style>
:root{{--paper:#F4F6F9;--surface:#fff;--ink:#0E1B2B;--muted:#5B6B7C;--navy:#16457E;--navy-deep:#0C2A52;--line:#DCE3EB;--gold:#C99A2E;--crit:#B42318;--high:#C2620C;--ok:#12805C;--sans:\"IBM Plex Sans\",sans-serif;--mono:\"IBM Plex Mono\",monospace}}
*{{box-sizing:border-box;margin:0}}body{{font-family:var(--sans);background:var(--paper);color:var(--ink)}}
.wrap{{max-width:1060px;margin:0 auto;padding:0 28px 60px}}
.toolbar{{position:sticky;top:0;background:rgba(244,246,249,.9);backdrop-filter:blur(7px);border-bottom:1px solid var(--line);padding:8px 28px;display:flex;justify-content:flex-end;gap:8px;z-index:10}}
.toolbar button{{font-family:var(--sans);font-size:12.5px;font-weight:600;border:1px solid var(--line);background:var(--surface);color:var(--navy);border-radius:6px;padding:7px 15px;cursor:pointer}}
.mast{{background:linear-gradient(122deg,#0C2A52 0%,#16457E 74%,#1B5493 100%);color:#fff;border-radius:0 0 14px 14px;padding:28px 38px 24px;position:relative;overflow:hidden}}
.mast::after{{content:\"\";position:absolute;right:-80px;top:-80px;width:280px;height:280px;border-radius:50%;border:36px solid rgba(201,154,46,.15)}}
.mast .k{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#9DB4D4;font-weight:700}}
.mast h1{{font-size:23px;font-weight:700;margin-top:8px}}
.mast h1 code{{font-family:var(--mono);font-size:18px;background:rgba(255,255,255,.12);padding:2px 10px;border-radius:6px}}
.mast .meta{{font-family:var(--mono);font-size:10.5px;color:#BCCDE4;margin-top:10px;line-height:1.8}}
.bdg{{font-size:11px;font-weight:600;padding:5px 12px;border-radius:99px;display:inline-block;margin-top:12px}}
.rbanner{{display:flex;align-items:center;gap:18px;background:var(--surface);border:1px solid var(--line);border-left:5px solid {rag_hex};border-radius:10px;padding:16px 22px;margin-top:18px}}
.rag{{display:flex;gap:8px}}.lamp{{width:14px;height:14px;border-radius:50%;background:#DCE3EB;opacity:.3}}.lamp.on{{opacity:1}}
h2.sec{{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--navy);font-weight:700;margin:30px 0 2px;padding-bottom:8px;border-bottom:2px solid var(--line)}}
h2.sec .n{{font-family:var(--mono);color:var(--gold);margin-right:8px}}
.grid2{{display:grid;grid-template-columns:1.3fr 1fr;gap:14px;margin:12px 0}}.panel{{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:16px 20px}}
.panel h3{{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:#8195A8;font-weight:700;margin-bottom:8px}}.panel p{{font-size:13px;color:#37485C;margin:6px 0}}
.fact{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line);font-size:12.5px}}.fact:last-child{{border-bottom:0}}.fact b{{font-family:var(--mono);font-weight:600}}
table{{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);border-radius:9px;overflow:hidden;font-size:12.5px;margin:12px 0}}
thead th{{background:#EEF2F6;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#8195A8;text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}}
td{{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}tbody tr:nth-child(even){{background:#FAFBFC}}tbody tr:last-child td{{border-bottom:0}}
.foot{{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);font-size:11px;color:#8195A8;line-height:1.8}}
@media print{{.toolbar{{display:none}}}}
</style></head><body>
<div class=\"toolbar\"><button onclick=\"window.print()\">⎙ Export PDF</button></div>
<div class=\"mast\">
<div class=\"k\">NFR Release Readiness Report · Production Readiness Review</div>
<h1><code>{html_lib.escape(svc)}</code></h1>
<div class=\"meta\">Branch {html_lib.escape(scan.get("branch","-"))} @ {html_lib.escape(scan.get("commit","")[:7])} · Generated {scan["ts"].replace("T"," ")} UTC · {scan["files_scanned"]} files · {len(findings)} gaps</div>
<span class=\"bdg\" style=\"background:var(--gold);color:#2E2205\">Tier-2 · Target 99.9%</span>
</div>
<div class=\"wrap\">
<div class=\"rbanner\">
<div><div class=\"rag\" style=\"margin-bottom:8px\">
<span class=\"lamp {"on" if rag=="RED" else ""}\" style=\"background:#B42318\"></span>
<span class=\"lamp {"on" if rag=="AMBER" else ""}\" style=\"background:#C2620C\"></span>
<span class=\"lamp {"on" if rag=="GREEN" else ""}\" style=\"background:#12805C\"></span>
</div>
<div style=\"font-size:18px;font-weight:700;color:{rag_hex}\">{status}<span style=\"display:block;font-size:10px;font-weight:600;text-transform:uppercase;color:#8195A8;margin-top:2px\">Readiness · {rag}</span></div>
</div>
<div style=\"font-size:13px;color:#5B6B7C;border-left:1px solid var(--line);padding-left:18px\">
<b style=\"color:var(--ink)\">● RED</b> critical open &nbsp;·&nbsp; <b style=\"color:#C2620C\">● AMBER</b> conditional / waivers &nbsp;·&nbsp; <b style=\"color:#12805C\">● GREEN</b> ready<br>
{c["blocker"]} critical · {c["high"]} high · {c["medium"]} medium · {c["low"]} low · <b>Not an automated pass/fail — sign-off group decides.</b>
</div></div>
<h2 class=\"sec\"><span class=\"n\">01</span>Service Comprehension</h2>
<div class=\"grid2\">
<div class=\"panel\"><h3>Service identity</h3>
<div class=\"fact\"><span>Service type</span><b>{html_lib.escape(stype)}</b></div>
<div class=\"fact\"><span>Stack</span><b>{html_lib.escape(stack)}</b></div>
<div class=\"fact\"><span>API endpoints</span><b>{len(eps)}</b></div>
<div class=\"fact\"><span>Upstreams detected</span><b>{len(ups)}</b></div>
<div class=\"fact\"><span>Datastores</span><b>{", ".join(k for k,_ in dbs) or "none detected"}</b></div>
<div class=\"fact\"><span>Messaging</span><b>{", ".join(msg) or "none"}</b></div>
<div style=\"margin-top:10px\">{ep_chips}</div>
</div>
<div class=\"panel\">
<h3>Downstream connections <span style=\"font-weight:400;color:#8195A8;font-size:9px\">(detected from code)</span></h3>
{up_rows}{db_rows}{msg_rows}
</div></div>
<h2 class=\"sec\"><span class=\"n\">02</span>Readiness by NFR Domain</h2>
<table><thead><tr><th>NFR Domain</th><th>Gaps</th><th>Critical</th><th>Highest Sev</th></tr></thead>
<tbody>{pillar_rows}</tbody></table>
<h2 class=\"sec\"><span class=\"n\">03</span>Critical &amp; High Findings</h2>
<table><thead><tr><th>Sev</th><th>Rule</th><th>Finding</th><th>Where (file:line)</th><th>Why it matters</th></tr></thead>
<tbody>{crit_rows}</tbody></table>
<h2 class=\"sec\"><span class=\"n\">04</span>PRR Sign-off</h2>
<table><thead><tr><th>Role</th><th>Name</th><th>Decision</th><th>Date</th><th>Waivers</th></tr></thead>
<tbody>
<tr><td><b>Service Tech Lead</b></td><td></td><td>Pending</td><td></td><td></td></tr>
<tr><td><b>SRE Lead</b></td><td></td><td>Pending</td><td></td><td></td></tr>
<tr><td><b>Application Owner</b><br><span style=\"font-size:11px;color:#8195A8\">Security &amp; Compliance</span></td><td></td><td>Pending</td><td></td><td></td></tr>
<tr><td><b>Tower Lead</b><br><span style=\"font-size:11px;color:#8195A8\">Cover Lead approval</span></td><td></td><td>Pending</td><td></td><td></td></tr>
</tbody></table>
<div class=\"foot\">Deterministic scan: nfr_scan.py v1.1 · 27 rules · 7 pillars · 4 layers · read-only · scan #{scan.get("id","?")}. No automated pass/fail verdict — release decision rests with sign-off group above.</div>
</div></body></html>"""


@app.get("/api/scan/{scan_id}/report-highlevel")
def report_highlevel(scan_id: int):
    s = _get_scan(scan_id)
    pf_path = Path(s["repo"]) / ".nfr-scan" / "profile.json"
    profile = json.loads(pf_path.read_text()) if pf_path.exists() else {}
    html = _generate_highlevel(s, profile, s["findings"])
    return Response(html, media_type="text/html",
                    headers={"Content-Disposition": f'attachment; filename="NFR_Readiness_{s["repo_name"]}.html"'})

@app.get("/api/dictionary")
def get_dictionary():
    import yaml
    spec = yaml.safe_load((KIT_ROOT / "skill" / "rules" / "nfr_dictionary.yaml")
                          .read_text(encoding="utf-8"))
    return {"terms": spec["terms"]}


@app.get("/api/prompts")
def get_prompts():
    import yaml
    spec = yaml.safe_load((KIT_ROOT / "skill" / "prompts" / "copilot_prompts.yaml")
                          .read_text(encoding="utf-8"))
    return {"prompts": spec["prompts"]}


@app.get("/api/scan/{scan_id}/profile")
def get_profile(scan_id: int):
    s = _get_scan(scan_id)
    pf = Path(s["repo"]) / ".nfr-scan" / "profile.json"
    if pf.exists():
        return json.loads(pf.read_text(encoding="utf-8"))
    return {"error": "no profile — re-run scan"}


@app.get("/api/rules")
def get_rules():
    import yaml
    spec = yaml.safe_load((KIT_ROOT / "skill" / "rules" / "nfr_rules.yaml")
                          .read_text(encoding="utf-8"))
    rules = [{"id": r["id"], "title": r["title"], "pillar": r["pillar"],
              "layer": r["layer"], "severity": r["severity"],
              "why": " ".join(r["why"].split()), "fix": r["fix"]}
             for r in spec["rules"]]
    return {"pillars": spec.get("pillars", {}), "rules": rules}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(HERE / "static" / "login.html")

@app.get("/", response_class=HTMLResponse)
def index(nfr_session: str = Cookie(default="")):
    if not _valid_session(nfr_session):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(HERE / "static" / "index.html")



# ===================== Low-level report (SRE Workspace) =====================
def _generate_lowlevel(scan, profile, findings):
    import html as hl
    svc = profile.get("service", scan["repo_name"])
    c   = _sev_counts(findings)
    cards = ""
    for i, f in enumerate(findings):
        loc = f["where"].split(" ")[0] + (f":{f['line']}" if f.get("line") else "")
        scol = {"blocker":"#F0524D","high":"#F5943B","medium":"#E7C24A","low":"#6B85A6"}[f["severity"]]
        sbg  = {"blocker":"rgba(240,82,77,.15)","high":"rgba(245,148,59,.15)","medium":"rgba(231,194,74,.15)","low":"rgba(107,133,166,.18)"}[f["severity"]]
        bord = {"blocker":"#F0524D","high":"#F5943B","medium":"#E7C24A","low":"#6B85A6"}[f["severity"]]
        cards += f"""<div style="background:#131C2B;border:1px solid #233246;border-left:4px solid {bord};border-radius:12px;margin:14px 0;overflow:hidden">
<div style="display:flex;align-items:center;gap:12px;padding:12px 18px;background:#0F1723;border-bottom:1px solid #233246;flex-wrap:wrap">
<span style="font-family:IBM Plex Mono,monospace;font-size:12px;font-weight:700;color:#3B7DD8">NFR-{101+i}</span>
<span style="font-size:14px;font-weight:700;flex:1">{hl.escape(f["title"])}</span>
<span style="font-family:IBM Plex Mono,monospace;font-size:10px;font-weight:700;padding:3px 9px;border-radius:5px;background:{sbg};color:{scol}">{f["severity"].upper()} · {hl.escape(f["rule_id"])}</span>
</div>
<div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#7A8CA0;padding:7px 18px;border-bottom:1px solid #233246">Layer: <b style="color:#3B7DD8">{f["layer"]}</b> &nbsp;·&nbsp; Where: <b style="color:#E6EDF5">{hl.escape(loc)}</b> &nbsp;·&nbsp; {hl.escape(f["pillar"])}</div>
<div style="padding:14px 18px">
<div style="font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#7A8CA0;font-weight:700;margin-bottom:6px">Why it fails at scale</div>
<div style="border-left:4px solid #F0524D;background:rgba(240,82,77,.06);border-radius:0 8px 8px 0;padding:10px 14px;font-size:12.5px;color:#E0CDCB">{hl.escape(f["why"])}</div>
<div style="font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#7A8CA0;font-weight:700;margin:12px 0 6px">How to remediate</div>
<pre style="font-family:IBM Plex Mono,monospace;font-size:11.5px;line-height:1.7;background:#0A1019;color:#C9D6E8;padding:12px 14px;border-radius:8px;border:1px solid #233246;overflow-x:auto;white-space:pre-wrap">{hl.escape(f["fix"])}</pre>
</div></div>"""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>SRE Workspace — {hl.escape(svc)}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0}}body{{font-family:IBM Plex Sans,sans-serif;background:#0B111C;color:#E6EDF5}}.wrap{{max-width:1060px;margin:0 auto;padding:0 24px 60px}}.tb{{position:sticky;top:0;background:rgba(11,17,28,.9);backdrop-filter:blur(8px);border-bottom:1px solid #233246;padding:9px 24px;display:flex;justify-content:flex-end}}.tb button{{font-family:IBM Plex Sans,sans-serif;font-size:12.5px;font-weight:600;border:1px solid #233246;background:#131C2B;color:#E6EDF5;border-radius:6px;padding:7px 15px;cursor:pointer}}.mast{{border-radius:0 0 14px 14px;background:linear-gradient(125deg,#0C1526 0%,#152744 55%,#1B3557 100%);border:1px solid #233246;border-top:0;padding:26px 36px}}.k{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#8B7BE8;font-weight:700}}.mast h1{{font-size:22px;font-weight:700;margin-top:7px}}.mast h1 code{{font-family:IBM Plex Mono,monospace;font-size:17px;background:rgba(255,255,255,.09);padding:2px 10px;border-radius:6px}}.meta{{font-family:IBM Plex Mono,monospace;font-size:11px;color:#9DB0CC;margin-top:9px;line-height:1.8}}h2{{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#8B7BE8;font-weight:700;margin:28px 0 4px;padding-bottom:8px;border-bottom:1px solid #233246}}.foot{{margin-top:34px;padding-top:14px;border-top:1px solid #233246;font-size:11px;color:#556579;line-height:1.8}}@media print{{.tb{{display:none}}}}</style></head><body>
<div class="tb"><button onclick="window.print()">⎙ Export PDF</button></div>
<div class="mast"><div class="k">SRE Architecture & Remediation Workspace · Low-Level Engineering Deep-Dive</div>
<h1><code>{hl.escape(svc)}</code> — Remediation Study</h1>
<div class="meta">Branch {hl.escape(scan.get("branch","-"))} @ {hl.escape(scan.get("commit","")[:7])} · {scan["files_scanned"]} files · {len(findings)} findings · scan #{scan.get("id","?")}</div></div>
<div class="wrap"><h2>{c["blocker"]} critical · {c["high"]} high · {c["medium"]} medium · {c["low"]} low</h2>
{cards}
<div class="foot">Companion to the NFR Release Readiness Report. Same scan, engineering audience. Each card: why it fails at scale + paste-ready fix. Copilot prompts live in the Execution Sheet.</div>
</div></body></html>"""


@app.get("/api/scan/{scan_id}/report-lowlevel")
def report_lowlevel(scan_id: int):
    s   = _get_scan(scan_id)
    pf  = Path(s["repo"]) / ".nfr-scan" / "profile.json"
    prf = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
    htm = _generate_lowlevel(s, prf, s["findings"])
    return Response(htm, media_type="text/html",
                    headers={"Content-Disposition":
                             f'attachment; filename="SRE_Workspace_{s["repo_name"]}.html"'})


# ===================== Execution sheet (Copilot prompts) =====================
def _generate_execsheet(scan, findings):
    import html as hl
    svc = scan["repo_name"]
    units = ""
    crit_high_med = [f for f in findings if f["severity"] in ("blocker","high","medium")]
    for i, f in enumerate(crit_high_med):
        loc       = f["where"].split(" ")[0] + (f":{f['line']}" if f.get("line") else "")
        file_part = f["where"].split(" ")[0]
        scol = {"blocker":"#F0524D","high":"#F5943B","medium":"#E7C24A"}[f["severity"]]
        sbg  = {"blocker":"rgba(240,82,77,.15)","high":"rgba(245,148,59,.15)","medium":"rgba(231,194,74,.15)"}[f["severity"]]
        bord = {"blocker":"#F0524D","high":"#F5943B","medium":"#E7C24A"}[f["severity"]]
        prompt = (f"@workspace In {file_part}.\n"
                  f"DIRECTIVE: Fix the following NFR gap: {f['title']} ({f['rule_id']}).\n"
                  f"CONSTRAINTS:\n"
                  f"- Do NOT alter the method signature, public API contract, or payload schema.\n"
                  f"- Do NOT introduce a new library without adding it to the build file.\n"
                  f"- Do NOT swallow, catch-and-suppress, or silently ignore any exception.\n"
                  f"- Externalise all configuration values; do NOT hardcode timeouts, limits, or keys.\n"
                  f"- Apply the minimum change to fix this gap only; do NOT refactor surrounding code.\n"
                  f"OUTPUT: Return ONLY the unified code diff. Suppress all explanation and prose.")
        units += f"""<div style="background:#111821;border:1px solid #1E2A38;border-left:4px solid {bord};border-radius:12px;margin:14px 0;overflow:hidden">
<div style="display:flex;align-items:center;gap:12px;padding:12px 20px;background:#0D131B;border-bottom:1px solid #1E2A38;flex-wrap:wrap">
<span style="font-family:IBM Plex Mono,monospace;font-size:12px;font-weight:700;color:#3FD07E">EXU-{101+i}</span>
<span style="font-size:14px;font-weight:700;flex:1">{hl.escape(f["title"])}</span>
<span style="font-family:IBM Plex Mono,monospace;font-size:10px;font-weight:700;padding:3px 9px;border-radius:5px;background:{sbg};color:{scol}">{f["severity"].upper()} · {hl.escape(f["rule_id"])}</span>
</div>
<div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#556579;padding:7px 20px;border-bottom:1px solid #1E2A38">Target: <b style="color:#3B7DD8">{hl.escape(file_part)}</b> · line {hl.escape(str(f.get("line") or "—"))}</div>
<div style="display:grid;grid-template-columns:1fr 300px">
<div style="border-right:1px solid #1E2A38">
<div style="font-family:IBM Plex Mono,monospace;font-size:10px;padding:7px 14px;background:#0C1119;border-bottom:1px solid #1E2A38;color:#556579">VS Code Copilot — Agent mode · paste verbatim</div>
<pre style="font-family:IBM Plex Mono,monospace;font-size:11.5px;line-height:1.75;color:#CBD8E6;padding:14px 16px;background:#0A0F16;white-space:pre-wrap;margin:0">{hl.escape(prompt)}</pre>
</div>
<div style="padding:14px 16px;background:#111821">
<div style="font-size:10px;letter-spacing:.07em;text-transform:uppercase;font-weight:700;color:#E0A63B;margin-bottom:8px">⛭ Why these constraints</div>
<ul style="list-style:none;padding:0">
<li style="font-size:11.5px;color:#BDCAD8;padding:6px 0 6px 14px;position:relative;border-bottom:1px solid rgba(255,255,255,.04)"><span style="position:absolute;left:0;color:#E0A63B">▪</span><b style="font-family:IBM Plex Mono,monospace;font-size:10px">No signature change</b> — keeps fix drop-in; prevents caller breakage.</li>
<li style="font-size:11.5px;color:#BDCAD8;padding:6px 0 6px 14px;position:relative;border-bottom:1px solid rgba(255,255,255,.04)"><span style="position:absolute;left:0;color:#E0A63B">▪</span><b style="font-family:IBM Plex Mono,monospace;font-size:10px">Build file required</b> — blocks orphaned imports from new libraries.</li>
<li style="font-size:11.5px;color:#BDCAD8;padding:6px 0 6px 14px;position:relative;border-bottom:1px solid rgba(255,255,255,.04)"><span style="position:absolute;left:0;color:#E0A63B">▪</span><b style="font-family:IBM Plex Mono,monospace;font-size:10px">Externalise config</b> — enables per-env tuning without rebuild.</li>
<li style="font-size:11.5px;color:#BDCAD8;padding:5px 0 5px 14px;position:relative"><span style="position:absolute;left:0;color:#E0A63B">▪</span><b style="font-family:IBM Plex Mono,monospace;font-size:10px">Minimum change</b> — limits diff to this gap; reduces review surface.</li>
</ul>
</div></div></div>"""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Execution Sheet — {hl.escape(svc)}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0}}body{{font-family:IBM Plex Sans,sans-serif;background:#0A0F16;color:#DEE8F0}}.wrap{{max-width:1080px;margin:0 auto;padding:0 26px 60px}}.tb{{position:sticky;top:0;background:rgba(10,15,22,.9);backdrop-filter:blur(8px);border-bottom:1px solid #1E2A38;padding:9px 26px;display:flex;justify-content:flex-end}}.tb button{{font-family:IBM Plex Sans,sans-serif;font-size:12.5px;font-weight:600;border:1px solid #1E2A38;background:#111821;color:#DEE8F0;border-radius:6px;padding:7px 15px;cursor:pointer}}.mast{{border-radius:0 0 14px 14px;background:linear-gradient(125deg,#0B141B 0%,#0F2019 60%,#123024 100%);border:1px solid #1E2A38;border-top:0;padding:26px 36px}}.k{{font-family:IBM Plex Mono,monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#3FD07E;font-weight:700}}.mast h1{{font-size:22px;font-weight:700;margin-top:7px}}.mast h1 code{{font-family:IBM Plex Mono,monospace;font-size:17px;background:rgba(255,255,255,.08);padding:2px 10px;border-radius:6px}}.meta{{font-family:IBM Plex Mono,monospace;font-size:11px;color:#8FB0A0;margin-top:9px;line-height:1.7}}.foot{{margin-top:34px;padding-top:14px;border-top:1px solid #1E2A38;font-size:11px;color:#556579;line-height:1.8}}@media print{{.tb{{display:none}}}}</style></head><body>
<div class="tb"><button onclick="window.print()">⎙ Export PDF</button></div>
<div class="mast"><div class="k">SRE Zero-Waste Prompt Execution Sheet · Deterministic Remediation</div>
<h1><code>{hl.escape(svc)}</code> — Copilot Execution Sheet</h1>
<div class="meta">Branch {hl.escape(scan.get("branch","-"))} @ {hl.escape(scan.get("commit","")[:7])} · {len(crit_high_med)} prompts (critical + high + medium) · scan #{scan.get("id","?")}<br>Paste each prompt into VS Code Copilot (Agent mode) verbatim. Review diff against Constraints panel before accepting.</div></div>
<div class="wrap">{units}
<div class="foot">Execution layer of the NFR guardrail trilogy. Same scan data as Readiness Report and Architecture Workspace. Prompts are compiler directives — edit inputs, never loosen constraints.</div>
</div></body></html>"""


@app.get("/api/scan/{scan_id}/report-execsheet")
def report_execsheet(scan_id: int):
    s   = _get_scan(scan_id)
    htm = _generate_execsheet(s, s["findings"])
    return Response(htm, media_type="text/html",
                    headers={"Content-Disposition":
                             f'attachment; filename="Exec_Sheet_{s["repo_name"]}.html"'})


# ===================== Application Registry (SEAL ID) =====================
# Apps table: seal_id (e.g. 99899), app_name, bitbucket_url, branches, added

def _ensure_apps_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seal_id TEXT UNIQUE, app_name TEXT,
        bitbucket_url TEXT, branches TEXT, added TEXT)""")

class AppRequest(BaseModel):
    seal_id: str
    app_name: str
    bitbucket_url: str
    branches: list[str] = ["develop", "master", "release"]

@app.post("/api/apps")
def register_app(req: AppRequest):
    """Register an application by SEAL ID, name and Bitbucket URL."""
    con = db(); _ensure_apps_table(con)
    branches = [b.strip() for b in req.branches if b.strip()]
    try:
        con.execute("INSERT INTO apps (seal_id, app_name, bitbucket_url, branches, added) "
                    "VALUES (?,?,?,?,?)",
                    (req.seal_id.strip(), req.app_name.strip(), req.bitbucket_url.strip(),
                     json.dumps(branches),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
    except Exception:
        con.execute("UPDATE apps SET app_name=?, bitbucket_url=?, branches=? WHERE seal_id=?",
                    (req.app_name, req.bitbucket_url, json.dumps(branches), req.seal_id))
        con.commit()
    con.close()
    return {"seal_id": req.seal_id, "app_name": req.app_name, "branches": branches}

@app.get("/api/apps")
def list_apps():
    con = db(); _ensure_apps_table(con)
    rows = con.execute(
        "SELECT id, seal_id, app_name, bitbucket_url, branches, added FROM apps ORDER BY app_name"
    ).fetchall()
    con.close()
    apps = []
    for r in rows:
        a = dict(zip(["id","seal_id","app_name","bitbucket_url","branches","added"], r))
        a["branches"] = json.loads(a["branches"])
        # attach latest scan per branch
        con2 = db()
        a["latest"] = {}
        for b in a["branches"]:
            row = con2.execute(
                "SELECT id, ts, commit_sha, blocker, high, medium, low FROM scans "
                "WHERE repo_name=? AND branch=? ORDER BY id DESC LIMIT 1",
                (a["app_name"], b)).fetchone()
            if row:
                a["latest"][b] = dict(zip(["scan_id","ts","commit","blocker","high","medium","low"], row))
        con2.close()
        apps.append(a)
    return apps

@app.delete("/api/apps/{app_id}")
def remove_app(app_id: int):
    con = db(); _ensure_apps_table(con)
    con.execute("DELETE FROM apps WHERE id=?", (app_id,)); con.commit(); con.close()
    return {"removed": app_id}

@app.post("/api/apps/{app_id}/scan")
def scan_app_branches(app_id: int):
    """Sync from Bitbucket and scan every tracked branch for this app."""
    con = db(); _ensure_apps_table(con)
    row = con.execute("SELECT seal_id, app_name, bitbucket_url, branches FROM apps WHERE id=?",
                      (app_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "App not registered")
    seal_id, name, url, branches = row[0], row[1], row[2], json.loads(row[3])
    results, errors = [], []
    for branch in branches:
        try:
            wc = sync_branch(url, name, branch)
            _, sha = head_info(wc)
            data = run_scanner(wc)
            run_profiler(wc)
            r = store_scan(wc, name, branch, sha, data)
            r["seal_id"] = seal_id
            results.append(r)
        except Exception as e:
            errors.append({"branch": branch, "error": str(e)[:300]})
    return {"seal_id": seal_id, "app": name, "scanned": results, "errors": errors}

@app.get("/api/apps/{seal_id}/history")
def app_history(seal_id: str):
    """All scans for an app identified by SEAL ID."""
    con = db(); _ensure_apps_table(con)
    row = con.execute("SELECT app_name FROM apps WHERE seal_id=?", (seal_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"No app with SEAL ID {seal_id}")
    scans = con.execute(
        "SELECT id, branch, commit_sha, ts, files_scanned, blocker, high, medium, low "
        "FROM scans WHERE repo_name=? ORDER BY id DESC",
        (row[0],)).fetchall()
    con.close()
    keys = ["scan_id","branch","commit","ts","files_scanned","blocker","high","medium","low"]
    return {"seal_id": seal_id, "app_name": row[0],
            "scans": [dict(zip(keys, r)) for r in scans]}


# ===================== Authentication =====================
# Corporate config via environment variables (never hardcode):
#   NFR_ADMIN_USER  = DOMAIN\networkid
#   NFR_ADMIN_HASH  = bcrypt hash of your 16-char password
#   NFR_SECRET_KEY  = python3 -c "import secrets;print(secrets.token_hex(32))"
import os, secrets, hashlib, hmac

SECRET_KEY  = os.environ.get("NFR_SECRET_KEY", secrets.token_hex(32))
ADMIN_USER  = os.environ.get("NFR_ADMIN_USER", "")
ADMIN_HASH  = os.environ.get("NFR_ADMIN_HASH", "")
SESSION_TTL = int(os.environ.get("NFR_SESSION_TTL", "28800"))
_sessions = {}

def _hash_token(tok):
    return hmac.new(SECRET_KEY.encode(), tok.encode(), hashlib.sha256).hexdigest()

def _make_session():
    tok = secrets.token_urlsafe(48)
    _sessions[_hash_token(tok)] = datetime.now(timezone.utc).isoformat()
    return tok

def _valid_session(tok):
    if not tok: return False
    h = _hash_token(tok)
    if h not in _sessions: return False
    if (datetime.now(timezone.utc) -
        datetime.fromisoformat(_sessions[h])).total_seconds() > SESSION_TTL:
        del _sessions[h]; return False
    return True

def _check_password(pwd):
    if ADMIN_HASH:
        try:
            from passlib.context import CryptContext
            return CryptContext(schemes=["bcrypt"], deprecated="auto").verify(pwd, ADMIN_HASH)
        except Exception:
            return False
    return len(pwd) == 16          # dev mode

def _check_user(uid):
    if ADMIN_USER:
        return uid.strip().lower() == ADMIN_USER.strip().lower()
    return bool(uid.strip())       # dev mode

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginRequest):
    if len(req.password) != 16:
        raise HTTPException(401, "Password must be exactly 16 characters")
    if not _check_user(req.username) or not _check_password(req.password):
        raise HTTPException(401, "Invalid credentials")
    r = JSONResponse({"ok": True, "user": req.username.strip()})
    r.set_cookie("nfr_session", _make_session(), httponly=True,
                 samesite="strict", max_age=SESSION_TTL, secure=False)
    return r

@app.post("/api/auth/logout")
def logout(nfr_session: str = Cookie(default="")):
    if nfr_session: _sessions.pop(_hash_token(nfr_session), None)
    r = JSONResponse({"ok": True}); r.delete_cookie("nfr_session"); return r

@app.get("/api/auth/status")
def auth_status(nfr_session: str = Cookie(default="")):
    return {"authenticated": _valid_session(nfr_session)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
