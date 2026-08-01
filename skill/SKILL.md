---
name: nfr-guardrail-scanner
description: >
  End-to-end SRE NFR gap scanner and production-readiness guardrail. Use this
  skill whenever the user asks to scan a repository for NFR gaps, run a
  production readiness review (PRR), assess availability / reliability /
  performance / security / observability / resilience / operability before a
  release, or generate an "NFR Gap Assessment" report. Triggers: "NFR scan",
  "PRR", "production readiness", "gap assessment", "release guardrail",
  "pre-prod check", "is this repo production ready".
---

# NFR Guardrail Scanner — SRE Gatekeeper Skill

## What this skill does

Performs a full-stack (frontend + backend + database + infra) NFR gap
assessment of a repository BEFORE release, and produces a structured report:
**Why the gap matters → What it is → Where in the code → How to fix it.**

This is a layer ON TOP of the base Resilience4j Auditor skill: it must
complement, not duplicate, the base skill's resilience findings. If the base
skill already covers a finding, reference it and go deeper (config values,
tuning) instead of repeating it.

**Hard rule: the report NEVER issues a pass/fail verdict.** It identifies
gaps and recommendations; the release decision belongs to the PRR sign-off
group (Service Tech Lead, SRE Lead, Security Lead).

## Two-phase workflow (always follow this order)

### Phase 1 — Run the deterministic scanner FIRST

```bash
python scanner/nfr_scan.py <repo_path> --out .nfr-scan
```

This produces `.nfr-scan/findings.json` (machine findings with rule_id,
pillar, layer, severity, file:line, evidence) and `.nfr-scan/nfr_report.md`.

Never skip this step and never invent findings the scanner could have
produced — the scanner is ground truth for pattern-level gaps. If the scanner
is unavailable, say so explicitly and mark all findings as "LLM-inferred,
unverified".

### Phase 2 — LLM deep analysis (what the scanner cannot see)

Read `findings.json` PLUS the actual source of every flagged file, then add
reasoning-level findings the regex layer cannot catch:

1. **Transaction boundary review** — walk each `@Transactional` method:
   does it mix DB writes with external calls? Is there a compensation path?
2. **Cache invalidation pairing** — for every `@Cacheable` flagged, verify a
   matching `@CachePut`/`@CacheEvict` exists on the write path; report the
   unpaired ones only.
3. **Retry topology** — map retry logic ACROSS service layers; flag
   multiplicative retry (A retries B retries C = retry storm math: 3×3×3=27).
4. **N+1 and query shape** — inspect repository/DAO methods called inside
   loops; check entity relationships for eager fetch on list endpoints.
5. **Graceful degradation** — for each external dependency, ask: if this is
   down, does the core user journey survive? Name the missing fallback.
6. **SLO fitness** — compare declared/likely service tier (Tier-1 99.99% /
   Tier-2 99.9% / Tier-3 99%) against what the code can actually deliver
   (replicas, probes, breakers, multi-AZ hints).
7. **Observability completeness** — logs/metrics/traces triangle: can an
   on-call engineer go from alert → trace → offending query in one hop?
8. **AI-generated-code risks** — missing authz ownership checks (authn ≠
   authz), hallucinated/outdated dependencies, unbounded memory loads.

## Report format (strict)

Produce `NFR_Gap_Assessment_<repo>_<date>.md` with exactly these sections:

1. **Executive summary** — 5 lines max, severity counts, top 3 risks in
   plain English, each paired with the production metric it threatens.
2. **High-level matrix** — one table: Pillar | Gaps | Blockers | Highest-risk
   finding | Business impact (one line, plain English).
3. **Deep-dive per pillar** — table per pillar with columns:
   `Sev | Rule/Finding | Layer | Where (file:line) | Why (plain English + metric) | How to fix (paste-ready)`.
   Scanner findings keep their rule_id; LLM findings get id `LLM-<pillar>-NN`
   and cite the exact file:line evidence.
4. **Fix sequencing** — ordered remediation plan: blockers first, then
   highest MTTR-reduction per effort. Estimate effort as S/M/L.
5. **PRR sign-off table** — pre-filled rows for Service Tech Lead, SRE Lead,
   Security Lead with Status = "Pending review", plus a waiver-tracking row.

Style rules: every technical term is paired with a plain-English translation
and a concrete metric. No pass/fail language anywhere ("gaps identified",
never "failed"). Findings without file:line evidence are labeled "inferred".

## Guardrails

- Read-only: never modify the scanned repository; write only to `.nfr-scan/`
  and the report file.
- Evidence-first: every finding cites file:line or is labeled inferred.
- Scope: all component tiers — UI, API/services, messaging, DB, K8s/infra
  manifests, CI/CD config.
- If the repo tech stack has no matching rules (e.g., Go), state the
  coverage gap explicitly rather than staying silent.
