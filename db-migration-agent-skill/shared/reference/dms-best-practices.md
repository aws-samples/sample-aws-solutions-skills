# DMS Best Practices — Quick Reference

## Replication Instance Sizing

| Workload | Instance | RAM | Use Case |
|----------|----------|-----|----------|
| Dev/Test | dms.r6i.large | 16 GB | < 50 tables, testing |
| Small Production | dms.r6i.xlarge | 32 GB | 50-300 tables, < 500 GB |
| Medium Production | dms.r6i.2xlarge | 64 GB | 300-1000 tables, LOBs present |
| Large Production | dms.r6i.4xlarge | 128 GB | 1000+ tables, > 2 TB |

R-family (memory-optimized) is the default above because most migrations are memory-bound
(buffering large transactions, cached changes, log files). **For heterogeneous/cross-engine
migrations, prefer the equivalent C-family (compute-optimized) size instead** — DMS's own
data-type-conversion machinery is CPU-intensive on these tasks (e.g. Oracle→PostgreSQL, or
any task where DMS Schema Conversion is doing real work), and that's the bottleneck AWS
calls out C5 for, not RAM. Same size tier, swap R for C: `dms.c5.large` for dev/test,
`dms.c5.xlarge` for small production heterogeneous, and so on. See
`heterogeneous-migration.md` for the migrations this applies to.

**Rules:**
- Allocate 50-70% of RAM for `MemoryLimitTotal`
- Never use T-family for production (CPU credit exhaustion) — dev/test at this DB's actual
  scale (a handful of tables, low millions of rows) is a legitimate use of a T-family
  instance; this rule is about production, not an absolute ban
- Start larger during full load, scale down for steady-state CDC
- Multi-AZ for production migrations (auto-failover)

## LOB Handling

| Mode | Max Size | Performance | Use When |
|------|----------|-------------|----------|
| Limited LOB | Configurable (e.g., 32 KB) | Fast (inline transfer) | LOB sizes are predictable and bounded |
| Full LOB | Unlimited | Slow (2 lookups per row) | LOBs vary wildly in size |
| Inline LOB | Configurable threshold | Best of both | Mixed LOB sizes (small + occasional large) |

**Query to profile LOB sizes:**
```sql
-- MySQL: Find actual max LOB sizes
SELECT MAX(OCTET_LENGTH(`your_lob_column`)) AS max_bytes
FROM `your_db`.`your_table`;
```

**Recommendation:** Use a non-truncating Full LOB mode unless a verified upper bound,
including future CDC writes, fits Limited LOB mode. Set `LobMaxSize` in KB at or above
that bound (round bytes up to KB); a percentile is not a safe limit. If the maximum is
unknown or exceeds the supported limit, use Full LOB mode and test performance. Never
treat truncation as an acceptable default.

## Task Types

| Type | Downtime | Prerequisites | Migrates Views? |
|------|----------|---------------|-----------------|
| Full Load Only | Yes (duration = data size / throughput) | None | ✅ Yes |
| CDC Only | No (ongoing) | Data must already exist on target | ❌ No |
| Full Load + CDC | Near-zero (seconds at cutover) | Binary logging / logical replication | ❌ No (tables only) |

## Critical Settings

### Full Load Optimization
```json
{
  "MaxFullLoadSubTasks": 16,
  "CommitRate": 50000,
  "TargetTablePrepMode": "DROP_AND_CREATE",
  "CreatePkAfterFullLoad": true
}
```

`CreatePkAfterFullLoad: true` — creates primary keys AFTER data load, significantly speeding up full load (no index maintenance during bulk insert).

### CDC Optimization
```json
{
  "BatchApplyEnabled": true,
  "BatchApplyPreserveTransaction": true,
  "BatchApplyTimeoutMax": 30,
  "MinTransactionSize": 1000
}
```

`BatchApplyEnabled: true` — groups changes into batches instead of applying one-by-one. 2-5x throughput improvement.

## What DMS Does NOT Migrate

| Object Type | Alternative |
|-------------|-------------|
| Stored procedures | mysqldump --routines / pg_dump --schema-only |
| Triggers | mysqldump --triggers / pg_dump |
| Views | Full Load task only, OR mysqldump |
| Functions | mysqldump --routines / pg_dump |
| Events (MySQL) | mysqldump --events |
| Sequences (PostgreSQL) | pg_dump --schema-only |
| Indexes (optionally) | Created after full load for speed |
| User permissions/grants | Manual recreation |
| Custom data types (PG) | pg_dump --schema-only |

## Pre-Migration Assessment

Always run the DMS pre-migration assessment before starting:
```bash
aws dms create-replication-task \
  --replication-task-identifier "assessment-task" \
  --source-endpoint-arn $SOURCE_ARN \
  --target-endpoint-arn $TARGET_ARN \
  --replication-instance-arn $INSTANCE_ARN \
  --migration-type "full-load" \
  --table-mappings file://table-mappings.json

aws dms start-replication-task-assessment-run \
  --replication-task-arn "$TASK_ARN" \
  --service-access-role-arn "$ASSESSMENT_ROLE_ARN" \
  --result-location-bucket "$ASSESSMENT_BUCKET" \
  --assessment-run-name "pre-migration-assessment"
```

Set `TASK_ARN` to the created task's ARN. The assessment role must trust DMS and have
access to the result bucket (and its KMS key, if used). Wait for the assessment run to
finish and review its results before starting replication.

This checks:
- Source DB connectivity and permissions
- Unsupported data types
- Tables without primary keys (affects CDC validation)
- LOB column identification
- Binary logging configuration (MySQL)
- Replication slot availability (PostgreSQL)

## Monitoring Metrics

Task metrics (`CDCLatency*`, `CDCIncomingChanges`, …) are published with two dimensions:
`ReplicationTaskIdentifier` = the task's **resource id** — the last `:` segment of its ARN (`arn:aws:dms:<region>:<acct>:task:CPSTBQCAAFB67LEICTHDNETPSU` → `CPSTBQCAAFB67LEICTHDNETPSU`), **not** the friendly task name; `ReplicationInstanceIdentifier` = the friendly instance identifier. Verified live with `aws cloudwatch list-metrics --namespace AWS/DMS`; the friendly
name silently returns zero datapoints (an alarm on it sits in `INSUFFICIENT_DATA` forever). Get
the id with `aws dms describe-replication-tasks --query 'ReplicationTasks[].ReplicationTaskArn'`
and take the last segment (`${ARN##*:}`); CDK: `Fn.select(6, Fn.split(':', task.ref))`.
Empty datapoints are never "zero lag".

| Metric | Warning Threshold | Action |
|--------|------------------|--------|
| CDCLatencySource | > 30 seconds | Check source load, increase instance |
| CDCLatencyTarget | > 30 seconds | Enable BatchApply, scale target |
| FreeableMemory | < 2 GB | Scale up replication instance |
| SwapUsage | > 0 | Instance is undersized |
| CPUUtilization | > 80% sustained | Scale up or reduce parallelism |

## Endpoint TLS (`SslMode`) — MySQL-family

AWS DMS supports **`none`, `verify-ca` and `verify-full`** for MySQL / MariaDB / Aurora
MySQL endpoints; **`require` is "Not supported"** and the endpoint fails at deploy with
`The require SSL mode is not supported by the 'mysql' engine` (live: a stack rollback after
synth passed). Source: DMS User Guide, "Using SSL with AWS DMS" (per-engine table).

- `verify-ca` / `verify-full` need the CA certificate **imported into DMS** first (PEM;
  `aws dms import-certificate` or CDK `dms.CfnCertificate` — its `Ref` is the ARN to set as
  the endpoint's `CertificateArn`; `cdk-stacks.md` §migration-stack.ts).
- **Self-managed MySQL source:** import the server's CA — the file named by
  `SHOW GLOBAL VARIABLES LIKE 'ssl_ca'` (MySQL's auto-generated set puts `ca.pem` in the
  data directory). Fetch the public `ca.pem` only (never `ca-key.pem`) through the
  approved access path. Auto-generated server certificates don't carry the host name, so use
  `verify-ca`; `verify-full` needs a certificate whose name matches the endpoint's server name.
- **RDS/Aurora target:** import the RDS CA bundle for the region
  (`https://truststore.pki.rds.amazonaws.com/<region>/<region>-bundle.pem`, or the global
  bundle this skill ships as `shared/assets/rds-global-bundle.pem`); `verify-full` works
  against the RDS endpoint name.
- `none` is rejected by a target with `require_secure_transport=ON` (MySQL error 3159 —
  the default on Aurora MySQL 8.4) and needs explicit approval anyway.
- The same rules apply to the **reverse** task's endpoints (new target as DMS source, old
  source as DMS target). The reverse target endpoint's account is a dedicated migration
  account with only apply grants — never the application account
  (`cutover-procedures.md` §Reverse Replication).

## Reverse (rollback) task from RDS/Aurora MySQL — prerequisites

DMS CDC **from** an AWS-managed MySQL needs automated backups on (RDS binlog), `binlog_format=ROW`
and `binlog_row_image=FULL`, and binlog retention set with
`CALL mysql.rds_set_configuration('binlog retention hours', 24)` — RDS purges binlogs as soon
as possible otherwise (DMS User Guide, "MySQL as a source", AWS-managed section). Turn on
task logging (`"Logging": {"EnableLogging": true}`) for every task. The writer on the old
source needs DMS's target grants including `ALL PRIVILEGES ON awsdms_control.*` (control
schema, `ControlTablesSettings.ControlSchema`). Full checklist and GATE 4 rule:
`cutover-procedures.md` §Reverse-CDC prerequisites.

## Engine-Specific Gotchas

### MySQL → Aurora MySQL
- `DEFINER` clauses in views/procedures may fail if the user doesn't exist on Aurora
- `SUPER` privilege not available on Aurora — use `rds_superuser_role` or remove DEFINER
- AUTO_INCREMENT values may differ slightly after CDC (higher on target is OK)
- `sql_mode` differences between versions can cause trigger/procedure failures

### MariaDB → Aurora MySQL
- MariaDB-specific SQL syntax (e.g., `RETURNING` clause) not supported in Aurora MySQL
- Sequence objects (MariaDB 10.3+) not available in Aurora MySQL — use AUTO_INCREMENT
- System-versioned tables (temporal tables) not supported in Aurora MySQL
- GIS spatial functions differ between MariaDB and MySQL 8.0

### PostgreSQL → Aurora PostgreSQL
- Not all extensions are available on Aurora (check `SELECT * FROM pg_available_extensions`)
- `pg_cron` requires Aurora-specific setup
- Large objects (lo) need special handling in DMS
- Custom C-language functions cannot be installed on Aurora
- Replication slots MUST be cleaned up to prevent WAL accumulation
