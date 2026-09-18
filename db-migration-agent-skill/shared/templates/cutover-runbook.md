# Cutover Runbook — {source} → {target} — {date} {window, TZ}

> Generated at Phase 8 planning with **real values — zero placeholders may survive
> generation**. Rehearsed against the clone on {rehearsal-date}. Every step has an owner,
> an expected duration with its own measured/estimated basis, a verification, and an
> abort action. The operator executes top-to-bottom; the agent tracks ✅ per step in
> `migration-plan.md`.

**Executed by:** {customer team (Mode 2 — the agent prepared this runbook and does not
run it) / migration agent (Mode 3, with A4 signed)}

**Timing evidence per step:** mark every execution row `measured` (duration + evidence
reference, environment/version, date) or `estimated` (assumption/basis). A component test
can measure one step while the rest remain estimated; do not label the whole runbook
measured. A syntax-only check is not a duration measurement. Split a mixed step or mark
it estimated and identify its measured component.

**Operational contacts:** operator: {team/channel} · app owner: {team/channel}
**Approval and abort authority:** confirmed context-and-mark block in `authorizations.md`
(no identifying approval details); agreed abort criteria apply at every step.
**Comms:** {channel} — post at start, at each ⏱ checkpoint, at completion/abort.

## T-24h — Prechecks
| ✅ | Step | Command / action | Expect |
|---|------|------------------|--------|
| ▢ | Validation green (GATE 3) | see migration-plan.md Phase 7 | all ▢→✅ |
| ▢ | Production settings effective before validation/soak | {parameter group, reboot/session checks} | durable commits, integrity checks, TLS/timezone, approved backups/availability |
| ▢ | Parallel run passed: {N} consecutive green periods | migration-plan.md Phase 7.7 tracker | counter {N}/{N}; soak-exit block confirmed |
| ▢ | **Mode 3:** cutover authorization confirmed (A4) + abort criteria agreed. **Mode 2:** handover acceptance confirmed (A4b) + abort criteria agreed with the customer's own team | authorizations.md §3 | that block's `**Confirmed:**` date present (no name — this checks the block exists and is marked, not who marked it) |
| ▢ | Client inventory complete | migration-plan.md Phase 7.5 | every client repoint-ready |
| ▢ | Reverse replication task exists, endpoints tested, task NEVER RUN | `aws dms describe-replication-tasks --filters Name=replication-task-arn,Values={rev-arn}` | status `ready` (never `stopped` — a run task holds a stale CDC checkpoint; recreate it if rehearsal ran it) |
| ▢ | DNS TTL lowered (if DNS cutover) | `aws route53 change-resource-record-sets …` TTL=60 | dig shows 60 |
| ▢ | Connection pools pre-tuned | per-client values in migration-plan.md | maxLifetime≈30s |
| ▢ | Alarms + dashboard on operator screen | CloudWatch dashboard {name} | no active alarms |

## Commands by side and version

**Source:** {exact engine/version + endpoint} · **Target:** {exact engine/version + endpoint}.
For a version-crossing migration, fill this table with independently verified commands
for **both sides**, drawing syntax from
[../reference/version-upgrades.md](../reference/version-upgrades.md)'s per-version list.
Use explicit `n/a — reason` where an operation only applies to one side. Link execution
rows and staged scripts to these command IDs; never substitute one generic command for
both versions.

| ID / Operation | Source command ({source version}) | Target command ({target version}) | Component-test result / evidence per side |
|---|---|---|---|
| C1 — Capture binlog/WAL coordinates | {exact source SQL} | {exact target SQL} | {source result + target result; timestamps and evidence refs} |
| C2 — Replication status / stop / start | {source SQL or API; mark unused actions n/a} | {target SQL or API} | {per-side syntax, privileges and output-parser checks} |
| C3 — Final counts/checksums | {source queries} | {target queries} | {per-side results and parser checks} |
| C4 — AUTO_INCREMENT / sequence reset | {n/a unless required by rollback} | {exact target statements file} | {target test result and duration evidence} |
| {additional command IDs used by freeze/rollback scripts} | {source command} | {target command} | {per-side evidence} |

For example, C1 for **MySQL 8.0.46 → 8.4.11** is `SHOW MASTER STATUS;` on the source
and `SHOW BINARY LOG STATUS;` on the target; neither side accepts the other's syntax.
Generate the actual table for the engagement's versions rather than retaining this
example. Read-only component checks may run on the real endpoints; mutating checks
require version-matched clones or their existing explicit action authorization.
See [../reference/execution-runbooks.md](../reference/execution-runbooks.md) §Migration
Rehearsal for the documented failure discovered **mid-freeze** from testing only one
version. Record the script's parser expectations separately for each command.

## T-0 — Execution

**Offline/full-load-only branch:** replace CDC-drain steps with the rehearsed final
full export/backup and replacement restore under a continuous source-write fence,
then repeat final validation before repointing. The earlier Phase 6 copy is not current.
Budget the entire copy/validation outage; in Mode 2 the customer executes this branch.

> Steps 3–8 (the freeze window) run as **one pre-staged script per host** (staged and
> dry-run-tested at T-24h) — the operator issues one command per host, not one per step.
> Interactive per-step dispatch (e.g. SSM send-command round-trips) adds 30–40 s each and
> blows the budget. All engine-specific syntax below was verified against the actual
> source and target versions during prep, recorded by side above.
>
> **This is a hard rule, not guidance — per-step dispatch and under-tested scripts are
> the most common way a ≤60–120 s pause budget becomes 2–5 minutes.**
> Executing freeze-window steps as separate remote commands is an abort-level deviation:
> if the single script isn't staged and rehearsed/dry-run-clean, do not open the window.
> Also pre-verify inside the script's dry-run mode: every parse of query output (a `-N`
> flag with `\G` output does not parse), every account/grant the post-repoint app needs
> on the target, the reverse-replication apply path, and **TLS trust against the EXACT
> endpoint the app will connect to** — the RDS Proxy endpoint presents a different
> certificate chain than the cluster/instance endpoints, so a CA bundle validated only
> against the writer endpoint fails at the proxy connection probe (use the combined RDS
> global bundle, or probe the proxy endpoint itself during the dry run).

| ✅ | ⏱ Duration | Timing basis / evidence | Step | Command | Verify | Abort action |
|---|------------|-------------------------|------|---------|--------|--------------|
| ▢ | {duration} | {measured/estimated + evidence/basis} | 1. Confirm CDC caught up | CDCLatencySource=0 ∧ CDCLatencyTarget=0 | metric=0 | wait / abort if climbing |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 2. Maintenance mode ON | {app-specific} | banner up | — |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 3. Freeze source | {fence all app/job reconnects; drain/terminate sessions and transactions; engine read-only controls supplementary} | no writers or prepared write transactions; reconnect fence tested | unfreeze, exit maint |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 4. Final CDC drain | {fresh CloudWatch Source/Target metrics and C1/C2 applied source checkpoint} | post-freeze datapoints =0; missing/stale blocks | resume, abort |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 5. Stop forward task | `aws dms stop-replication-task --replication-task-arn {fwd-arn}` | `stopped` | restart task |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 6. Spot-validate {3-5 critical tables} | {C3 — source queries and target queries separately} | match | ROLLBACK |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 7. Reset AUTO_INCREMENT / sequences on target | {C4 — target statements file} | next-val > max | ROLLBACK |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 8. Start REVERSE replication (fresh start from freeze point, never-run task) | {retain source app/job fence; clear read-only controls for ordinary DMS SQL apply}; `aws dms start-replication-task --replication-task-arn {rev-arn} --start-replication-task-type start-replication` | `running`; apply permissions rehearsed; source app writers still blocked | abort unless alternative rollback/RPO explicitly confirmed |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 9. Repoint: {secret update / DNS swap / config+unit change} | {exact command(s) per client} | — | revert repoint |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 10. Refresh/restart clients ({coordinated / rolling-with-frozen-source}) | {per-client commands} | services up | revert + restart |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 11. **Bidirectional verify** | app health = UP **and** target processlist shows {expected client IPs, ~pool counts} | both | ROLLBACK |
| ▢ | {duration} | {measured/estimated + evidence/basis} | 12. Maintenance mode OFF | {command} | traffic flowing | — |

**Total budgeted write-pause: {n}s. End-to-end rehearsal: {measured duration + evidence /
not measured; do not describe summed component times as a measured full pause}.**

## T+15m / T+1h / T+24h — Watch
- Error rate {baseline}% → now: __ · p95 latency {baseline}ms → now: __
- Reverse CDC lag: __ · Missing clients on processlist: __
- Slow queries → run targeted `ANALYZE TABLE` before suspecting worse.

## Abort / rollback criteria (from cutover-procedures.md — pre-agreed, not negotiable mid-incident)
| Signal | Action |
|--------|--------|
| Error rate > 5% | Immediate rollback (execute rollback-runbook.md) |
| p99 > 3× baseline, not improving in 5 min | Rollback |
| Connection failures > 1% | 10 min to fix SG/creds, else rollback |
| Any data-integrity doubt | Immediate rollback + investigate |
