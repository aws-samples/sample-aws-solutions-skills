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

