# Authorizations of Record — {engagement} ({source} → {target})

> **This file is the audit anchor.** Every gate sign-off, action-class authorization,
> and waiver lives here with a named person and a date — chat approvals are working
> conversation, not the record. `migration-plan.md` gate rows reference rows in this
> file. Rules and action classes: `shared/reference/engagement-safety.md`.

## 1. Engagement scope

| | Value | Authorized by | Date |
|---|---|---|---|
| Engagement mode | {1 analysis-only / 2 migration-ready — customer executes cutover / 3 full-migration — agent executes cutover} | | |
| Mode 2 handover depth | {(a) full preparation / (b) light preparation} — n/a for Modes 1, 3 | | |
| Mode 3 warnings stated & accepted | {yes — agent will freeze the source and repoint live clients / n/a} | | |
| Engagement parameters | rehearsal {…} · parallel run {N} · validation depth {…} · rollback {…} | | |
| Prior assessment report | {path/date of report that unlocks Mode 3 / "this engagement, Phases 1–3"} | | |
| IAM guardrail in place | {session policy / permissions boundary / simulate-proof} — Deny list active on {source ARNs} | | |

## 2. Action-class authorizations (row required BEFORE first execution)

| # | Action class | Exact scope | Authorized by | Date | Executed (ref) |
|---|--------------|-------------|---------------|------|----------------|
| A1 | Read-only source access | {DB user, hosts} | | | |
| A2 | Source write: {each individually, e.g. "GRANT TRIGGER fix", "create migration user"} | {exact statements or script ref} | | | |
| A3 | Target/production infrastructure deploy | {stack list} | | | |
| A4 | **Cutover execution — Mode 3 only** | window {date/time TZ}, runbook {version/hash} | | | |
| A4b | **Handover acceptance — Mode 2** | package contents listed; cutover ownership transferred | | | |
| A5 | Rollback execution | {pre-authorized on abort criteria / requires call} | | | |
| A6 | Decommission | {exact resource list} | | | |

## 3. Gate sign-offs

| Gate | What was approved | Approver | Date |
|------|-------------------|----------|------|
| GATE 1 | Discovery inputs + mode + engagement parameters locked | | |
| GATE 2 | Method, cost, architecture, rollback strategy | | |
| GATE 3 | Validation evidence accepted | | |
| Soak exit (if a parallel run was run) | {N} consecutive green periods reached on {date} | | |
| GATE 4 — **Mode 3 only** | Cutover go + abort criteria (approver reachable for the window) | | |
| **A4b handover — Mode 2** | Handover package received; customer accepts ownership of the cutover | | |
| Decommission | Rollback window closed; teardown list | | |

## 4. Waivers (recommended parameters skipped — each needs all four columns)

| What was skipped | Risk, stated plainly | Accepted by | Date |
|------------------|----------------------|-------------|------|
| | | | |

## 5. Extended-assurance records (fill only if the engagement chose these parameters)

| | Value |
|---|---|
| Rehearsal convergence (repeat-until-converged) | run 1: {s} → run 2: {s} → run N: {s} (< 20% delta reached: {date}) |
| Approver present at cutover (Mode 3) | {name, confirmed} |
| Reconciliation report sign-offs | {daily rows or ref to soak reports} |
