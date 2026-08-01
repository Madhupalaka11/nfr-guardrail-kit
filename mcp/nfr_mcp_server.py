#!/usr/bin/env python3
"""
nfr_mcp_server.py — Custom MCP server: the bridge between the LLM (Copilot /
Claude / any MCP client) and your corporate ecosystem.

Why MCP: the LLM has NO direct access to repos, scan results, or internal
systems. MCP inverts that — the LLM calls named, audited tools that YOU
implement and control. Each tool is a narrow, logged doorway; the model never
gets raw filesystem or network access. That is what makes this approvable in
a bank: read-only tools, explicit scopes, full audit trail.

Run (stdio transport — what VS Code / Copilot uses):
    pip install "mcp[cli]" pyyaml
    python nfr_mcp_server.py

Register in VS Code:  .vscode/mcp.json (see mcp.json in this folder).
Then in Copilot Chat (Agent mode):  "Scan repo X for NFR gaps and explain
the blockers"  → Copilot calls scan_repo → get_findings → explain_gap.

Extension points for your environment (add as new @mcp.tool functions):
  - query_splunk(query)        -> wrap approved Splunk REST endpoint
  - get_datadog_slo(service)   -> wrap Datadog SLO API
  - get_service_tier(service)  -> lookup from your CMDB/service catalog
  - get_recent_deploys(service)-> wrap internal CD API
Every tool: read-only, service-account auth, log every call with caller +
arguments to satisfy governance.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

KIT_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = KIT_ROOT / "skill" / "rules" / "nfr_rules.yaml"
SCANNER = KIT_ROOT / "scanner" / "nfr_scan.py"
AUDIT_LOG = KIT_ROOT / "mcp" / "audit.log"

mcp = FastMCP("nfr-guardrail")


def _audit(tool: str, args: dict):
    """Governance requirement: every LLM tool call is logged."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool, "args": args}
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _load_rules():
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"]


@mcp.tool()
def scan_repo(repo_path: str) -> str:
    """Run the deterministic NFR gap scan on a repository. Returns a JSON
    summary (severity counts per pillar). Full findings via get_findings."""
    _audit("scan_repo", {"repo_path": repo_path})
    out_dir = Path(repo_path) / ".nfr-scan"
    proc = subprocess.run(
        [sys.executable, str(SCANNER), repo_path, "--out", str(out_dir)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        return json.dumps({"error": proc.stderr[-2000:]})
    data = json.loads((out_dir / "findings.json").read_text(encoding="utf-8"))
    summary: dict = {}
    for f in data["findings"]:
        p = summary.setdefault(f["pillar"], {"blocker": 0, "high": 0, "medium": 0, "low": 0})
        p[f["severity"]] += 1
    return json.dumps({"repo": repo_path, "files_scanned": data["files_scanned"],
                       "total_gaps": len(data["findings"]), "by_pillar": summary,
                       "findings_file": str(out_dir / "findings.json")}, indent=2)


@mcp.tool()
def get_findings(repo_path: str, pillar: str = "", severity: str = "") -> str:
    """Fetch detailed findings from the last scan of a repo, optionally
    filtered by pillar (availability|reliability|performance|resilience|
    security|observability|operability) and/or severity (blocker|high|medium|low)."""
    _audit("get_findings", {"repo_path": repo_path, "pillar": pillar, "severity": severity})
    fp = Path(repo_path) / ".nfr-scan" / "findings.json"
    if not fp.exists():
        return json.dumps({"error": "No scan found — call scan_repo first."})
    findings = json.loads(fp.read_text(encoding="utf-8"))["findings"]
    if pillar:
        findings = [f for f in findings if f["pillar"] == pillar]
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    return json.dumps(findings[:100], indent=2)


@mcp.tool()
def explain_gap(rule_id: str) -> str:
    """Get the full Why / detection logic / paste-ready fix for a rule id
    (e.g. REL-BE-003). Use this to explain a finding to a developer."""
    _audit("explain_gap", {"rule_id": rule_id})
    for r in _load_rules():
        if r["id"] == rule_id:
            return json.dumps({"id": r["id"], "title": r["title"],
                               "pillar": r["pillar"], "layer": r["layer"],
                               "severity": r["severity"],
                               "why": " ".join(r["why"].split()),
                               "fix": r["fix"]}, indent=2)
    return json.dumps({"error": f"Unknown rule {rule_id}"})


@mcp.tool()
def list_rules(pillar: str = "") -> str:
    """List the NFR rule catalog (id, title, layer, severity), optionally
    filtered by pillar. Use to answer 'what do we check for?'."""
    _audit("list_rules", {"pillar": pillar})
    rules = _load_rules()
    if pillar:
        rules = [r for r in rules if r["pillar"] == pillar]
    return json.dumps([{"id": r["id"], "title": r["title"], "layer": r["layer"],
                        "severity": r["severity"]} for r in rules], indent=2)


@mcp.tool()
def get_prr_signoff_template(service_name: str, tier: str = "Tier-2") -> str:
    """Generate the PRR sign-off table for a service (Markdown), pre-filled
    with roles and the tier's SLA target. Tiers: Tier-1 (99.99%), Tier-2
    (99.9%), Tier-3 (99%)."""
    _audit("get_prr_signoff_template", {"service_name": service_name, "tier": tier})
    slas = {"Tier-1": "99.99% (≤52 min/yr)", "Tier-2": "99.9% (≤8.7 h/yr)", "Tier-3": "99% (≤3.65 d/yr)"}
    return (
        f"## PRR Sign-off — {service_name} ({tier}, target {slas.get(tier, 'n/a')})\n\n"
        "| Role | Name | Status | Date | Waivers |\n|---|---|---|---|---|\n"
        "| Service Tech Lead |  | Pending review |  |  |\n"
        "| SRE Lead |  | Pending review |  |  |\n"
        "| Security Lead |  | Pending review |  |  |\n"
        "| Waiver tracking |  | n/a |  | List waived rule ids + expiry |\n"
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport — VS Code / Copilot compatible
