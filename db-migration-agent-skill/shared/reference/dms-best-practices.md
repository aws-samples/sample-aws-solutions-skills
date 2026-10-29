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

**LOBs need a primary or unique key — in every LOB mode.** DMS: "A table must have a primary
key or a unique key for DMS to migrate LOBs" (premigration assessment
`table-with-lob-but-without-primary-key-or-unique-constraint`). Without one, DMS removes the
column (task log: `Column '<c>' was removed from table definition …: the column data type is
LOB and the table has no primary key or unique index`) — observed live in **limited** LOB mode
too, both MySQL → PG and PG → MySQL, full load and CDC; counts still match. Treat every such
table as a GATE 2 blocker, run the post-full-load non-null check before anything else, and
pick a remedy (surrogate key under A2 / backfill at freeze / exclude + separate copy):
[mysql-postgresql.md](mysql-postgresql.md) §4.7. `change-data-type` on a BLOB/CLOB-mapped
column is not a remedy (only "drop" is supported for such columns).

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
- LOB column identification — and LOBs on tables **without PK/UK** (the column is removed)
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

## Operating DMS tasks (stop / modify / reload / wait) — learned live

Each of these cost a live run retries or ~40 minutes; none is visible from the task status
alone.

- **Modify a deployed task through the API, not a CloudFormation update — then reconcile
  the stack explicitly.** Updating an existing `AWS::DMS::ReplicationTask` (settings or
  mappings) failed live with `UPDATE_FAILED 'ReplicationTask not found'` although the physical
  ids were the correct ARNs, and the rollback hit the same error. Stop the task, `aws dms
  modify-replication-task --replication-task-arn … --replication-task-settings
  file://intended-settings.json` (or `--table-mappings`), and record the drift in the plan. The
  stack's **stored template still holds the old values** — editing the CDK source changes
  neither it nor the task, and an empty `cdk diff` would only prove source == stored template,
  never the live settings. Reconcile in a planned window with one of:
  1. **Replace the task through CloudFormation** (built from documented behaviour; not yet
     executed in a dry run — rehearse it): stop the task, read its
     `ReplicationTasks[0].RecoveryCheckpoint` ("You can provide this value to the
     CdcStartPosition parameter to start a CDC operation that begins at that checkpoint"); put the
     intended settings/mappings in the CDK source **and** give the task a new
     `ReplicationTaskIdentifier` and `ResourceIdentifier` (a `ResourceIdentifier` change is
     "Update requires: Replacement"), with `MigrationType: cdc` and `CdcStartPosition: <checkpoint>`
     for a task that must continue CDC; `cdk deploy` creates the new task and deletes the old one;
     start it (`start-replication-task --start-replication-task-type start-replication`) and
     verify it applies (status + table state + probe, below). Full-load tasks simply reload.
     CloudFormation deletes the old task in the cleanup phase of that same deploy — before you
     start the new one. That is safe for a **MySQL-family source** (the checkpoint is a binlog
     position; binlog retention must cover the gap). **PostgreSQL source — the slot is the
     problem:** "DMS automatically drops replication slots when the task is deleted, if DMS
     created the slot", so a task whose endpoint has no `SlotName` (DMS-created slot) loses the
     WAL its checkpoint points into, and the replacement can only reseed. Replacement on a PG
     source therefore needs a **user-managed slot** (none of this was executed live — rehearse
     it on the rehearsal stack first):
     - If the task already runs on a slot you created (`pg_create_logical_replication_slot`,
       bound by the endpoint's `SlotName`, task started with `CdcStartPosition` — the pattern this
       skill uses for reverse tasks): stop the task; record the slot's `confirmed_flush_lsn` and
       the task's `RecoveryCheckpoint`; deploy the replacement on the **same endpoint/`SlotName`**
       with that `CdcStartPosition`; after the deploy confirm the slot still exists
       (`SELECT slot_name, active, confirmed_flush_lsn FROM pg_replication_slots`); start; verify
       recovery (no lost or duplicated changes on a probe set, per-table statistics, apply
       exceptions) **before** any cleanup.
     - If it runs on a DMS-created slot: do not replace it through a delete-first deploy. In a
       window with the source writes fenced: let the old task drain to zero lag, create a user
       slot, record its LSN, stop the old task, create the replacement (separate logical id, new
       endpoint with `SlotName`, `CdcStartPosition` = that LSN), verify it applies, release the
       writes, and only then remove the old task in a later deploy. Without a fence, changes
       between the old task's last apply and the new slot's creation are lost — reseed instead.
  2. **A real CloudFormation update to the intended values** (in-place: `ReplicationTaskSettings`
     and `TableMappings` are "Update requires: No interruption") — this is the path that failed
     live, so try it only on the rehearsal stack first and fall back to 1.
  Resource import is **not** available: `AWS::DMS::ReplicationTask` is absent from
  CloudFormation's import-supported resource list. After either path, compare the three sources
  independently — intended document, stored template, DMS readback:

  ```bash
  aws cloudformation get-template --stack-name "$STACK" --template-stage Original \
    --query TemplateBody --output json > stored-template.json        # what CloudFormation will re-apply
  aws dms describe-replication-tasks --filters Name=replication-task-arn,Values="$ARN" \
    --query 'ReplicationTasks[0].ReplicationTaskSettings' --output text > live-settings.json
  python3 -B - intended-settings.json live-settings.json <<'PY'
  import json, sys
  want, live = (json.load(open(f)) for f in sys.argv[1:3])
  def diff(w, l, path=""):
      for k, v in w.items():
          if isinstance(v, dict):
              diff(v, l.get(k) or {}, f"{path}{k}.")
          elif l.get(k) != v:
              print(f"DIFF {path}{k}: intended={v!r} live={l.get(k)!r}")
  diff(want, live)
  PY
  ```

  Then check the task's `ReplicationTaskSettings` string inside `stored-template.json` against
  `intended-settings.json` the same way (a CDK token may render it as `Fn::Join` — resolve or
  inspect it). Every `DIFF` line is unreconciled; record each of the three comparisons.
- **Wait for `stopped` — then retry the modify.** `ModifyReplicationTask` right after
  `stop-replication-task` raised `InvalidResourceStateFault` ("cannot be modified while it is
  running") — once even **2 s after** `describe-replication-tasks` already showed `stopped`
  (control-plane lag). Use `aws dms wait replication-task-stopped --filters
  Name=replication-task-arn,Values=$ARN`, then retry the modify on `InvalidResourceStateFault`
  with backoff (e.g. 10 s × 12) before treating it as a failure.
- **`ModifyReplicationTask` merges `ReplicationTaskSettings`** (observed): keys you omit keep
  their current values, so a "temporary" override (e.g. `TargetTablePrepMode`,
  `TableFailureMaxCount`) silently survives the next modify. To change or reset a value, send
  the **full intended settings document** with every key you care about set explicitly, then
  re-read `describe-replication-tasks` → `ReplicationTaskSettings` and diff it against the
  intended document before starting the task. Verify on your DMS version.
- **Reloads and `TargetTablePrepMode`.** A reload under `TRUNCATE_BEFORE_LOAD` (left on from an
  earlier window) failed for every FK-referenced parent — PostgreSQL target: `cannot truncate a
  table referenced in a foreign key constraint` (PG truncates a referenced table only together
  with its referencing tables); MySQL/InnoDB likewise refuses `TRUNCATE` of a table referenced
  by another table's FK (unless `FOREIGN_KEY_CHECKS=0` is in effect for the session — verify).
  For a reload: reset `TargetTablePrepMode` explicitly (full settings, above), reload only the
  tables that need it (`aws dms reload-tables --tables-to-reload …`, leaf tables first), and for
  parents either empty children→parents yourself in FK order under the approved action block,
  or drop/re-create the FKs around the reload (validate them after). Never assume the prep mode
  from the CDK source — read it back.
- **Wait/poll recipe — task status AND table state.** A status-only loop (`running`, 100% full
  load) missed a table that had failed; 40 minutes were lost. Each poll (30 s):

  ```bash
  aws dms describe-replication-tasks --filters Name=replication-task-arn,Values="$ARN" \
    --query 'ReplicationTasks[0].[Status,StopReason,LastFailureMessage,ReplicationTaskStats.FullLoadProgressPercent]'
  aws dms describe-table-statistics --replication-task-arn "$ARN" \
    --query 'TableStatistics[].[SchemaName,TableName,TableState,FullLoadRows,FullLoadErrorRows,AppliedInserts,AppliedUpdates,AppliedDeletes]'
  ```

  Fail fast when: task `Status` is `failed`/`stopped` unexpectedly or `LastFailureMessage` is
  set; any `TableState` is `Table error` (the API's states: Table does not exist, Before load,
  Full load, Table completed, Table cancelled, Table error, Table is being reloaded);
  `FullLoadErrorRows` > 0; or a table in `Full load` whose `FullLoadRows` has not moved for N
  polls (stall — read the task log). Done = every in-scope table `Table completed` (full load)
  plus, for CDC, latency/backlog within bounds — never status alone. Paginate
  `describe-table-statistics` (`--max-items`/`NextToken`) on large table counts.
- **After a connectivity outage between AWS and the source/target, re-check every task — the
  rollback path first.** Live (OBS-72): after a ≈ 20-min network outage to the on-prem side, the
  reverse task stayed in a reconnect loop with `Failed to deobfuscate password` (observed message;
  root cause unverified), CDC latency climbed past 1,789 s, and emergency rollback was impossible
  until it was fixed. Procedure: `describe-replication-tasks` (status, `StopReason`,
  `LastFailureMessage`), the CDC latency metrics, and `aws dms test-connection` for each endpoint
  (then `describe-connections`); if the task does not recover by itself, stop it, wait for
  `stopped` (waiter + retry, above), then `start-replication-task --start-replication-task-type
  resume-processing` from its checkpoint, and verify it applies (status + table state + probe, and
  the value-level probe for a cross-engine reverse task). A rollback path that is unavailable —
  for any length of time — is a **GATE-level surprise: report it to the customer immediately**
  (hard constraint 13), with the RPO it implies until restored.
- **Per-task control schema; benign errors allow-listed explicitly (OBS-74).** A new task that
  reuses another task's DMS control tables in the same target schema logs benign errors such as
  an "index already exists" on `awsdms_validation_failures_v1`, and an "0 error lines" gate failed
  on it. Give each task its own control schema (`ControlTablesSettings.ControlSchema`, e.g.
  `awsdms_<task>`; grant the writer `ALL` on it). Error-line gates keep an **explicit allow-list**
  of known benign patterns (exact message + table), report allow-listed hits separately with their
  count, and never pass silently on anything else.
- **Task replacement re-points monitoring (OBS-75).** After any task replacement (e.g. a never-run
  task for the return trip) the CloudWatch reverse-lag alarm kept watching the stopped old task —
  on a stopped task it sits at `INSUFFICIENT_DATA` or `OK` and looks green. Re-point every alarm,
  dashboard widget and the soak config to the **new task's resource-id dimension**, then verify the
  alarm evaluates the running task: `describe-alarms` shows the new dimension, and
  `get-metric-statistics` for it returns recent datapoints. **`INSUFFICIENT_DATA` on a
  rollback-path alarm means "not monitored"**, never "fine".
  **Then inventory the replication slots on every PostgreSQL endpoint (OBS-81):** a replaced task
  can leave its slot behind (live: an orphan cycle-1 forward slot retained ≈ 117 MB/h of WAL on the
  PG source after the rollback and return):

  ```sql
  SELECT slot_name, plugin, slot_type, active, active_pid,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal,
         wal_status
  FROM pg_replication_slots ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC NULLS LAST;
  ```

  For each slot of a replaced task: **drop it** (`SELECT pg_drop_replication_slot('<slot>')` on an
  inactive slot — a presented block, it is a production change) **or keep it explicitly** — record
  why, add a WAL-growth / free-storage alarm (`ReplicationSlotDiskUsage`, `TransactionLogsDiskUsage`
  or `FreeStorageSpace` on RDS/Aurora; disk free on self-managed), and state the headroom math:
  hours until full ≈ free storage ÷ measured retained-WAL growth per hour. A kept slot is a
  hand-off item (Phase 9).
- **Blocked apply after an outage — check for half-open sessions on the reverse source (OBS-77).**
  Live: after a ≈ 20-min VPN outage the reverse task stalled ≈ 50 min behind a half-open source
  session holding an uncommitted transaction. Look for them:

  ```sql
  -- PostgreSQL (reverse source): sessions of the DMS user idle inside a transaction
  SELECT pid, usename, client_addr, state, xact_start, state_change FROM pg_stat_activity
  WHERE usename = '<dms_user>' AND state IN ('idle in transaction', 'idle in transaction (aborted)');
  -- MySQL: open InnoDB transactions and their sessions
  SELECT trx_id, trx_state, trx_started, trx_mysql_thread_id, trx_rows_locked FROM information_schema.INNODB_TRX
  ORDER BY trx_started;
  ```

  Terminating one (`pg_terminate_backend(<pid>)` / `KILL <thread_id>`) is a production write — its
  own presented block, naming the session and why.
- **Guard / cutover triggers: check the DEFINER where they were created.** A guard trigger
  created by root on the rehearsal clone carried `DEFINER=root@localhost`; production must get
  the intended definer (the production run used `shop_admin@%`). A definer that doesn't exist
  on the target makes every app write that fires the trigger fail. After applying:

  ```sql
  SELECT trigger_schema, trigger_name, event_object_table, definer, action_timing, event_manipulation
  FROM information_schema.triggers WHERE trigger_schema IN ('shop');
  SELECT user, host FROM mysql.user WHERE CONCAT(user, '@', host) IN ('shop_admin@%');   -- definer exists
  ```

  (PostgreSQL: `SELECT tgname, tgrelid::regclass, tgenabled, p.proname, p.prosecdef,
  pg_get_userbyid(p.proowner) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid WHERE NOT
  tgisinternal;` — owner and `SECURITY DEFINER` as intended, `tgenabled` as the runbook says.)

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

### MySQL ↔ PostgreSQL (heterogeneous)
- Pre-create the converted schema and load with `TargetTablePrepMode` `DO_NOTHING` /
  `TRUNCATE_BEFORE_LOAD` — the `DROP_AND_CREATE` default above maps TINYINT(1) → SMALLINT,
  BIGINT UNSIGNED → BIGINT, JSON → TEXT, PG BOOLEAN → VARCHAR(5), unbounded NUMERIC →
  NUMERIC(28,6). Endpoint/task settings, reverse task and validation:
  [mysql-postgresql.md](mysql-postgresql.md) §4–§10.
- Live traps: PG BOOLEAN needs `change-data-type` → `boolean` (an `int1` rule turned every
  `'true'` into 0); MySQL zero dates arrived as `0101-01-01`, not NULL; LOB columns of key-less
  tables are removed (above). Prove every rule with value distributions (§4.2–§4.7 there).

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
