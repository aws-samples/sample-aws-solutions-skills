# Cutover Runbook — {source} → {target} — {date} {window, TZ}

> Generated at Phase 8 planning with **real values — zero placeholders may survive
> generation**. Rehearsed against the clone on {rehearsal-date}. Every step has an owner,
> an expected duration (from rehearsal), a verification, and an abort action. The
> operator executes top-to-bottom; the agent tracks ✅ per step in `migration-plan.md`.

**Roles:** operator: {name} · app owner: {name} · approver: {name}
**Abort authority:** {name} — abort criteria at bottom apply at every step.
**Comms:** {channel} — post at start, at each ⏱ checkpoint, at completion/abort.

## T-24h — Prechecks
| ✅ | Step | Command / action | Expect |
|---|------|------------------|--------|
| ▢ | Validation green (GATE 3) | see migration-plan.md Phase 7 | all ▢→✅ |
| ▢ | Soak passed (Tier ≥ 2): {N} consecutive green days | migration-plan.md Phase 7.7 tracker | counter {N}/{N}; soak-exit row signed |
| ▢ | Cutover authorization signed (A4) + abort criteria agreed | authorizations.md §2/§3 | named approver + date present |
| ▢ | Client inventory complete | migration-plan.md Phase 7.5 | every client repoint-ready |
| ▢ | Reverse replication task exists, endpoints tested, task NEVER RUN | `aws dms describe-replication-tasks --filters Name=replication-task-arn,Values={rev-arn}` | status `ready` (never `stopped` — a run task holds a stale CDC checkpoint; recreate it if rehearsal ran it) |
| ▢ | DNS TTL lowered (if DNS cutover) | `aws route53 change-resource-record-sets …` TTL=60 | dig shows 60 |
| ▢ | Connection pools pre-tuned | per-client values in migration-plan.md | maxLifetime≈30s |
| ▢ | Alarms + dashboard on operator screen | CloudWatch dashboard {name} | no active alarms |

## T-0 — Execution

> Steps 3–8 (the freeze window) run as **one pre-staged script per host** (staged and
> dry-run-tested at T-24h) — the operator issues one command per host, not one per step.
> Interactive per-step dispatch (e.g. SSM send-command round-trips) adds 30–40 s each and
> blows the budget. All engine-specific syntax below was verified against the actual
> target version during prep.
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

| ✅ | ⏱ est | Step | Command | Verify | Abort action |
|---|-------|------|---------|--------|--------------|
| ▢ | 1m | 1. Confirm CDC caught up | CDCLatencySource=0 ∧ CDCLatencyTarget=0 | metric=0 | wait / abort if climbing |
| ▢ | 1m | 2. Maintenance mode ON | {app-specific} | banner up | — |
| ▢ | 2m | 3. Freeze source ({read_only / stop-clients fallback}) | {exact command} | processlist: 0 writers | unfreeze, exit maint |
| ▢ | 1m | 4. Final CDC drain | sleep 30; re-check latency=0 | =0 | resume, abort |
| ▢ | 1m | 5. Stop forward task | `aws dms stop-replication-task --replication-task-arn {fwd-arn}` | `stopped` | restart task |
| ▢ | 2m | 6. Spot-validate {3-5 critical tables} | {prepared count/checksum commands} | match | ROLLBACK |
| ▢ | 2m | 7. Reset AUTO_INCREMENT / sequences on target | {generated statements file} | next-val > max | ROLLBACK |
| ▢ | 1m | 8. Start REVERSE replication (fresh start from the freeze point — task must be never-run; see cutover-procedures.md step 8 note) | `aws dms start-replication-task --replication-task-arn {rev-arn} --start-replication-task-type start-replication` | `running` | note: rollback now lossy — decide |
| ▢ | 1m | 9. Repoint: {secret update / DNS swap / config+unit change} | {exact command(s) per client} | — | revert repoint |
| ▢ | 3m | 10. Refresh/restart clients ({coordinated / rolling-with-frozen-source}) | {per-client commands} | services up | revert + restart |
| ▢ | 2m | 11. **Bidirectional verify** | app health = UP **and** target processlist shows {expected client IPs, ~pool counts} | both | ROLLBACK |
| ▢ | 1m | 12. Maintenance mode OFF | {command} | traffic flowing | — |

**Total budgeted write-pause: {n}s (rehearsed: {n}s).**

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
