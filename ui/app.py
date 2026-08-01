#!/usr/bin/env python3
"""
NFR Guardrail Console — Run Mode 2 (UI for SRE gatekeepers).

pip install streamlit pyyaml
streamlit run ui/app.py

SREs point it at any cloned repo path (they already have repo access across
environments), run the scan, browse gaps by pillar/severity, and download the
report to attach to the PRR ticket. No pass/fail verdict is shown — the UI is
a guardrail console, not an approval gate.
"""

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

KIT_ROOT = Path(__file__).resolve().parent.parent
SCANNER = KIT_ROOT / "scanner" / "nfr_scan.py"

st.set_page_config(page_title="NFR Guardrail Console", page_icon="🛡️", layout="wide")
st.title("🛡️ NFR Guardrail Console")
st.caption("Identify NFR gaps before production. Findings + recommendations only — "
           "release decisions stay with the PRR sign-off group.")

repo_path = st.text_input("Repository path (local clone)", placeholder="/repos/payment-service")
col1, col2 = st.columns([1, 3])
run = col1.button("Run NFR scan", type="primary", disabled=not repo_path)

if run:
    out_dir = Path(repo_path) / ".nfr-scan"
    with st.spinner("Scanning all tiers — frontend, backend, database, infra..."):
        proc = subprocess.run(
            [sys.executable, str(SCANNER), repo_path, "--out", str(out_dir)],
            capture_output=True, text=True,
        )
    if proc.returncode != 0:
        st.error(proc.stderr[-1500:])
        st.stop()
    st.session_state["scan_dir"] = str(out_dir)

scan_dir = st.session_state.get("scan_dir")
if scan_dir and (Path(scan_dir) / "findings.json").exists():
    data = json.loads((Path(scan_dir) / "findings.json").read_text(encoding="utf-8"))
    findings = data["findings"]

    sev_counts = {s: sum(1 for f in findings if f["severity"] == s)
                  for s in ["blocker", "high", "medium", "low"]}
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Files scanned", data["files_scanned"])
    m2.metric("🔴 Blockers", sev_counts["blocker"])
    m3.metric("🟠 High", sev_counts["high"])
    m4.metric("🟡 Medium", sev_counts["medium"])
    m5.metric("⚪ Low", sev_counts["low"])

    fcol1, fcol2 = st.columns(2)
    pillars = sorted({f["pillar"] for f in findings})
    sel_pillars = fcol1.multiselect("Pillar", pillars, default=pillars)
    sel_sev = fcol2.multiselect("Severity", ["blocker", "high", "medium", "low"],
                                default=["blocker", "high", "medium", "low"])

    shown = [f for f in findings if f["pillar"] in sel_pillars and f["severity"] in sel_sev]
    for pillar in sel_pillars:
        items = [f for f in shown if f["pillar"] == pillar]
        if not items:
            continue
        st.subheader(f"{pillar.title()} — {len(items)} gap(s)")
        for f in items:
            icon = {"blocker": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}[f["severity"]]
            loc = f["where"] + (f":{f['line']}" if f.get("line") else "")
            with st.expander(f"{icon} {f['rule_id']} — {f['title']}   ·   `{loc}`"):
                st.markdown(f"**Why it matters:** {f['why']}")
                if f.get("evidence"):
                    st.code(f["evidence"])
                st.markdown("**How to fix:**")
                st.code(f["fix"])

    report = (Path(scan_dir) / "nfr_report.md").read_text(encoding="utf-8")
    st.download_button("⬇️ Download NFR Gap Assessment (Markdown)", report,
                       file_name="NFR_Gap_Assessment.md")
