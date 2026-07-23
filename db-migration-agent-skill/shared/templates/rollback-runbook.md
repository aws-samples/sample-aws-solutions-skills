# Rollback Runbook — fail back {target} → {source}

> Generated alongside the cutover runbook with real values. Assumes the rollback strategy
> approved at GATE 4: **{reverse-replication (lossless) / write-log replay / acknowledged-RPO}**.
> Valid until {cutover + 7 days} — after source decommission this runbook is void.

**Trigger:** one of the abort criteria in cutover-runbook.md, or approver decision.
**Announce first:** {channel}. Rollback is itself a cutover — same discipline.

## If reverse replication is running (lossless path)
| ✅ | Step | Command | Verify |
|---|------|---------|--------|
| ▢ | 1. Maintenance mode ON | {command} | banner |
| ▢ | 2. Freeze TARGET | `SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;` on {target-endpoint} (PG: `default_transaction_read_only=on`) | 0 writers on target processlist |
| ▢ | 3. Drain reverse CDC to 0 | CDCLatencySource=0 ∧ CDCLatencyTarget=0 on {rev-arn} | =0 |
| ▢ | 4. Stop reverse task | `aws dms stop-replication-task --replication-task-arn {rev-arn}` | `stopped` |
| ▢ | 5. Reset AUTO_INCREMENT/sequences on SOURCE above new max | {generated statements} | next-val > max |
| ▢ | 6. Un-freeze source | {undo of cutover step 3 — read_only=OFF or start clients} | writable |
| ▢ | 7. Revert repoint | {exact revert: secret → source values / DNS swap back / config+unit revert} | — |
| ▢ | 8. Refresh/restart clients | {per-client commands} | up |
| ▢ | 9. Bidirectional verify against SOURCE | health UP ∧ source processlist shows {client IPs} | both |
| ▢ | 10. Maintenance OFF · keep TARGET running read-only (do NOT delete) | | |

## If reverse replication was NOT possible ({chosen alternative})
- **Write-log replay:** freeze target (steps 1–2) → export post-cutover writes from
  {audit table / SQS / Kinesis / request log} since {cutover-ts} → replay against source
  with {prepared replay script} → verify counts on affected tables → continue steps 6–10.
- **Acknowledged RPO:** confirm with approver that writes from {cutover-ts}→now on the
  target are accepted as lost (est. volume: query `{count query}` on target) → steps 6–10.
  Record the actual loss in migration-plan.md §Rollback record.

## Afterwards
- Root-cause in migration-plan.md §Rollback record before any re-attempt.
- Keep the target + DMS resources; re-sync with `--start-replication-task-type resume-processing` for attempt #2.
- Re-run the full validation phase before scheduling the next window.
