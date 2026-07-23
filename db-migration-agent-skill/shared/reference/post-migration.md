# Post-Migration Operations & Decommission

> Read this during **Phase 9 (Post-Migration)** — after cutover is verified. Alarms and
> Performance Insights were enabled at provisioning ([preflight-iam-cost.md](preflight-iam-cost.md) §4);
> this phase is stabilization, tuning back to steady state, and the controlled teardown.

## Immediately after cutover stabilizes (T+1h → T+24h)

1. **Refresh optimizer statistics** — `ANALYZE TABLE` on all tables (MySQL/MariaDB),
   `ANALYZE` / autovacuum check (PostgreSQL), `DBMS_STATS.GATHER_SCHEMA_STATS` (Oracle),
   `sp_updatestats` (SQL Server). Most "the new DB is slow" reports trace here.
2. **Baseline comparison** — re-run the Phase 2 top-20 statement set on the target and
   diff against the source baseline in `migration-plan.md` (plans, latency). Regressions →
   [validation-patterns.md](validation-patterns.md) §Performance Baseline and, for
   major-version gaps, [version-upgrades.md](version-upgrades.md) "After the upgrade".
3. **Confirm alarms are quiet** and reverse replication lag ≈ 0; record the T+24h check
   in the plan.

## Return to steady state (after the T+24h watch)

4. **Swap the parameter group** migration → production ([../patterns/cdk-stacks.md](../patterns/cdk-stacks.md)
   generates both) and apply — note which parameters are reboot-scoped and schedule
   accordingly. This reverts import-only settings (`innodb_flush_log_at_trx_commit`,
   relaxed checks) and enforces the production TLS posture.
5. **Scale down** the instance class to steady-state size (the import-sized instance is
   pure cost now).
6. **Restore connection-pool settings** on clients (the 30s `maxLifetime` cutover value →
   normal production value).
7. **Verify backups**: automated backup retention as approved, plus a manual snapshot
   labeled `post-migration-verified`.

## Close the rollback window (default: cutover + 7 days)

8. Confirm with the user the window may close (no open incidents, baseline comparison
   accepted).
9. **Stop reverse replication** (the source stops being a warm standby — from here,
   rollback = restore from snapshot).
10. **Decommission** — with the explicit-confirmation rule (SKILL.md hard constraint 8),
    listing exactly what is deleted:
    - Stop, snapshot (final image), then terminate the source EC2 (or stop if the user
      keeps it as a cold copy).
    - Delete DMS tasks, endpoints, then the replication instance.
    - Remove migration-only security-group rules and the migration stack from the CDK app.
11. Mark the plan complete: final costs vs the GATE 2 estimate, lessons-learned notes,
    handover of the CDK project + runbooks to the customer.
