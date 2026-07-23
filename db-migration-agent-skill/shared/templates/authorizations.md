# Authorizations of Record — {engagement} ({source} → {target})

> **This file is the audit anchor.** Every gate sign-off, action-class authorization,
> and waiver lives here with a named person and a date — chat approvals are working
> conversation, not the record. `migration-plan.md` gate rows reference rows in this
> file. Rules and action classes: `shared/reference/engagement-safety.md`.

## 1. Engagement scope

| | Value | Authorized by | Date |
|---|---|---|---|
| Engagement mode | {assessment-only / staging-rehearsal / production-migration} | | |
| Criticality tier | {1 Standard / 2 Business-critical / 3 Mission-critical} — *basis:* {one line: what happens if this DB is wrong/down for an hour} | | |
| Prior assessment report | {path/date of report that unlocks production mode / "this engagement, Phases 1–3"} | | |
| IAM guardrail in place | {session policy / permissions boundary / simulate-proof} — Deny list active on {source ARNs} | | |

## 2. Action-class authorizations (row required BEFORE first execution)

| # | Action class | Exact scope | Authorized by | Date | Executed (ref) |
|---|--------------|-------------|---------------|------|----------------|
| A1 | Read-only source access | {DB user, hosts} | | | |
| A2 | Source write: {each individually, e.g. "GRANT TRIGGER fix", "create migration user"} | {exact statements or script ref} | | | |
| A3 | Target/production infrastructure deploy | {stack list} | | | |
| A4 | Cutover execution | window {date/time TZ}, runbook {version/hash} | | | |
| A5 | Rollback execution | {pre-authorized on abort criteria / requires call} | | | |
| A6 | Decommission | {exact resource list} | | | |

## 3. Gate sign-offs

| Gate | What was approved | Approver | Date |
|------|-------------------|----------|------|
| GATE 1 | Discovery inputs + mode + tier locked | | |
| GATE 2 | Method, cost, architecture, rollback strategy | | |
| GATE 3 | Validation evidence accepted | | |
| Soak exit (Tier ≥ 2) | {N} consecutive green days reached on {date} | | |
| GATE 4 | Cutover go + abort criteria | | |
| Decommission | Rollback window closed; teardown list | | |

## 4. Waivers (tier requirements skipped — each needs all four columns)

| What was skipped | Risk, stated plainly | Accepted by | Date |
|------------------|----------------------|-------------|------|
| | | | |

## 5. Tier-3 only

| | Value |
|---|---|
| Rehearsal convergence | run 1: {s} → run 2: {s} → run N: {s} (< 20% delta reached: {date}) |
| Approver present at cutover | {name, confirmed} |
| Reconciliation report sign-offs | {daily rows or ref to soak reports} |
