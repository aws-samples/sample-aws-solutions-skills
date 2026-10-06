# Troubleshooting Quick Reference

> Symptom → likely cause → fix, across all phases and engines. Scan this FIRST when
> anything fails; each fix links back to the phase that prevents it.

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| DMS: `binlog truncated` | Binlogs expired before CDC read them | Increase `expire_logs_days`, restart full load |
| DMS: Out of memory | Instance undersized or LOBs too large | Scale up instance, use Limited LOB Mode |
| DMS: Increasing CDC latency | Target can't keep up | Enable `BatchApplyEnabled`, scale target |
| Aurora: `Access denied for user` | DEFINER clause references non-existent user | Strip DEFINERs from schema objects |
| Aurora: Stored proc fails | Uses SUPER privilege or unsupported syntax | Rewrite proc without SUPER, fix syntax |
| Slow queries after migration | Missing optimizer statistics | `ANALYZE TABLE` on all tables |
| Connection failures | Security group misconfiguration | Verify SG rules between app → Aurora |
| SSM agent on the source goes unresponsive/disconnects mid-engagement (an access path that worked during discovery stops working later) | Confirm via SSM's own ping status that the connection is actually lost, not just slow — that confirms SSM Agent connectivity only, nothing about *why*. One real possibility worth naming as a hypothesis (not a diagnosis without separate evidence): a small/undersized source host can genuinely run low on memory under the load of migration validation queries (checksums, row counts, native replication) — this is not always just a transient agent hiccup | First, confirm the database itself is still up (TCP to the DB port, a direct query via whatever alternate path exists — bastion, existing VPN/SSH) before treating this as a blocker; if the DB answers, keep working through that path. Restarting the agent or rebooting the host is a **source-side change** — flag it for the customer/operator, don't do it yourself. Report this the moment it happens, not after it recurs — an access path that worked during discovery no longer working is exactly the kind of contradicted expectation hard constraint 13 requires surfacing immediately. |
| A newly registered SSM managed host (migration host, helper EC2) reboots ~10 min after registration; long-running dump/load/probe dies | An SSM association / maintenance window / patch baseline (e.g. a weekly "PatchWeekly" association targeting all managed instances) patched and rebooted it — customer patch automation can do the same to the source or app hosts | `aws ssm describe-instance-associations-status --instance-id <id>`, `aws ssm list-associations`, `aws ssm describe-maintenance-windows`; record them and ask the customer to suspend/schedule around the migration window (preflight-iam-cost.md §1). Make the job resumable (recorded coordinates, per-table steps) and resume the same step — don't restart the migration |
| CloudWatch `AWS/DMS` CDCLatency query returns no datapoints / DMS alarm stuck `INSUFFICIENT_DATA` | Dimension `ReplicationTaskIdentifier` set to the friendly task name; it must be the task's resource id (ARN suffix) | Use `${TASK_ARN##*:}` (CDK `Fn.select(6, Fn.split(':', task.ref))`); `ReplicationInstanceIdentifier` stays the friendly instance id (dms-best-practices.md §Monitoring) |
| `ERROR 1227` on `SET GLOBAL read_only` | Account lacks SUPER / READ_ONLY ADMIN | Use the freeze fallback: stop all write clients, verify writers=0 via processlist (cutover-procedures.md §freeze fallback) |
| App can't reach new DB after secret rotation | DB host hardcoded in systemd/config, not in the secret | Change the systemd `ExecStart`/config (highest-priority source wins); backfill host into the secret (cutover-procedures.md §client discovery) |
| Migration load or app fails with TLS/SSL error | `require_secure_transport=ON` on target | Add TLS params to load tool + connector (target-provisioning.md §TLS-Enforcement Gate) |
| Schema drift on first app connection to new DB | ORM `ddl-auto=update`/auto-migrate | Set `validate`/`none` before cutover (cutover-procedures.md §client discovery) |
| MyISAM error on Aurora | MyISAM not supported | Convert to InnoDB before migration |
| TDE error | Encrypted tablespaces | Decrypt before migration |
| Oracle: `ORA-39083` on import | Missing privilege or target tablespace | Grant on target user; `METADATA_REMAP` tablespace |
| Oracle: `ORA-31693` table load failed | Tablespace quota / space | `ALTER USER ... QUOTA UNLIMITED`; grow storage |
| Oracle: invalid objects after import | Dependencies / compile order | `UTL_RECOMP.RECOMP_PARALLEL` (no `utlrp.sql` — no shell) |
| Oracle: can't import (FULL mode) | RDS blocks FULL mode | Use schema/table mode; exclude SYS-owned Scheduler objects |
| Oracle: TDE dump won't import | `ENCRYPTION_MODE=TRANSPARENT` | Re-export with `ENCRYPTION_MODE=PASSWORD` |
| SQL Server: restore fails, higher version | `.bak` from newer engine | Target RDS engine version must be ≥ source |
| SQL Server: orphaned users post-restore | Login SID mismatch | Recreate login w/ same SID, or `ALTER USER ... WITH LOGIN` |
| SQL Server: FILESTREAM restore rejected | FILESTREAM filegroup in `.bak` | Remove FILESTREAM; redesign as BLOB/S3 |
| SQL Server: missing Agent jobs/logins | Server-level objects not in user `.bak` | Script + recreate separately (execution-runbooks.md §schema objects) |
| Soak Lambda: `AccessDenied` / `AccessDeniedException` (incl. "Access to KMS is not allowed") | Missing grant — often an imported or CMK-encrypted secret/bucket, or a reused read-only secret with no grant | Invoke `{"mode":"preflight"}` and read each row's exact `iam_action` + `resource` (normal runs log the same in `SOAK_CHECK_ERROR` / `detail.aws_errors[]`); fix every row in one change, redeploy once — [cdk-stacks.md](../patterns/cdk-stacks.md) §soak-stack.ts "Soak IAM" table. Never redeploy blindly |
| Soak Lambda: timeout / `network: ... NOT an IAM problem` / `ConnectTimeoutError` | Lambda subnets have no NAT route and no VPC endpoint for that service (or SG/route to the DB is missing) | Not IAM — add NAT or interface endpoints (+ S3 gateway endpoint) per [preflight-iam-cost.md](preflight-iam-cost.md) §0b, then re-run preflight mode |
| Soak stack deploy: bucket "already exists" / secret "scheduled for deletion" on retry | Fixed physical names from an older snippet + rolled-back first deploy | Use the generated-name stack in cdk-stacks.md §soak-stack.ts; "Retry after a failed first deploy" covers cleanup |
| Customer's presigned dashboard link: `AuthorizationQueryParametersError ... region 'us-east-1' is wrong` | URL signed for the caller's default region, not the bucket's | Re-run `generate_presigned_urls.py` (current version detects the bucket region and verifies every URL returns 200 before printing it) |
