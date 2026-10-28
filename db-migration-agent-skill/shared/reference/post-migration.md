# Post-Migration Operations & Decommission

> Read this during **Phase 9 (Post-Migration)** — after cutover is verified. Alarms and
> Performance Insights were enabled at provisioning ([preflight-iam-cost.md](preflight-iam-cost.md) §4);
> this phase is stabilization, tuning back to steady state, and the controlled teardown.

> **Cache targets:** baseline (ops/s, hit ratio, latency), snapshot retention (node-based
> default 0 = no automatic backups), TLS/RBAC drift from the post-`complete-migration`
> change, source migration users and S3 seed cleanup — [elasticache-migration.md](elasticache-migration.md) §11.4.

## Immediately after cutover stabilizes (T+1h → T+24h)

1. **Refresh optimizer statistics** — `ANALYZE TABLE` on all tables (MySQL/MariaDB),
   `ANALYZE` / autovacuum check (PostgreSQL), `DBMS_STATS.GATHER_SCHEMA_STATS` (Oracle),
   `sp_updatestats` (SQL Server). Most "the new DB is slow" reports trace here.
2. **Baseline comparison (required — the rollback window does not close without it)** —
   compare the Phase 2 baseline (`preflight-iam-cost.md` §4: top-20 statements by total time
   with their `EXPLAIN` plans; peak/typical connections, QPS, p95 latency from the app's own
   metrics) with the same metrics on the target after cutover, same time-of-day window.
   Record a table in `migration-plan.md` (metric · Phase 2 value · post-cutover value ·
   delta · verdict) and present it; a missing baseline is stated as such, never filled in.
   Regressions →
   [validation-patterns.md](validation-patterns.md) §Performance Baseline and, for
   major-version gaps, [version-upgrades.md](version-upgrades.md) "After the upgrade".
3. **Confirm alarms are quiet** and reverse replication lag ≈ 0; record the T+24h check
   in the plan.
4. **Confirm target backup retention and availability match the approved production
   configuration** (`execution-runbooks.md` §Target Hygiene During Load/CDC). Aurora
   backups and multi-AZ storage were never disabled; verify its planned reader/failover
   topology separately. Any approved RDS load-only changes must already be restored.

## Return to steady state (after the T+24h watch)

4. **Verify the production parameter group remains active** ([../patterns/cdk-stacks.md](../patterns/cdk-stacks.md)
   generates both). The migration → production swap, required reboots, and connection
   recycling happen **before Phase 7 validation and soak**, not after the first day of
   production. Verify effective durability, integrity checks, TLS, and source timezone.
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
    - **If a physical-copy method was used (XtraBackup+S3, native backup/restore staged
      via S3, DataSync seed)**: delete the intermediate S3 objects
      (`aws s3 rm s3://your-bucket/xtrabackup/ --recursive`, or the equivalent prefix) —
      a full copy of the database sits there from the restore step
      (execution-runbooks.md §XtraBackup + S3) and nothing deletes it automatically once
      `restore-db-cluster-from-s3` has consumed it. It's SSE-KMS-encrypted, not
      egregiously exposed, but it's still a real residual full-data copy outside the
      managed instance — list it explicitly in the decommission confirmation, don't let
      attention move straight to the EC2/DMS items and skip it.
    - Remove migration-only security-group rules and the migration stack from the CDK app.
11. **Reconcile IaC drift before handover.** Anything created or changed outside the CDK
    app (helper EC2, RDS parameters such as `binlog retention hours`, Proxy auth entries,
    alarms, SG rules) is either back-ported into the CDK app (and `cdk diff` shows no change)
    or listed as drift with an owner and a reconcile step (`../patterns/cdk-stacks.md`
    §Post-stabilization changes). Stored procedures like `rds_set_configuration` are not CDK
    resources: record them in the README's post-deploy steps.
12. Mark the plan complete: final costs vs the GATE 2 estimate, lessons-learned notes,
    handover of the CDK project + runbooks to the customer.

## Verify the handoff before delivery

This also applies to the Mode 2 package prepared before customer cutover. Read
[handoff-packaging.md](handoff-packaging.md) and use `shared/scripts/handoff_bundle.py`.

Build reports and dashboard summaries from the same reviewed operation results and
evidence references. For supervised steps, use the attempt's `result.json` and bound
verification evidence; preserve failed attempts and later reconciliation separately.
Reconcile stale runbook headers, timestamps, resource/version bindings and evidence
hashes before packaging. Distinguish measured work, accepted waivers, pending observations
and customer-reported results. Packaging cannot validate those claims for you.

Review an explicit nonsecret allowlist and enumerate known references as included,
external or intentionally omitted. Pause writers, create and verify a new snapshot,
then retain the whole-archive digest separately. Inspect the resulting manifest: a
verified ZIP proves the selected bytes, not that every referenced result was selected.
Do not call the package complete while required artifacts or reference decisions remain
unresolved. Raw logs, dumps and credentials are excluded unless separately reviewed and
explicitly appropriate; credentials must never be part of the delivered report bundle.

Later customer receipt or save confirmation belongs in a separate record referencing
the frozen archive name/hash, with remaining work and its owner. Do not rebuild the ZIP
to put its own later receipt inside it or claim a download succeeded merely because
the archive was generated.
