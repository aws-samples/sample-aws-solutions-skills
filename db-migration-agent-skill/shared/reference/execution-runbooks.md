# Execution Runbooks — Method-Specific Migration Procedures

> Read the section for the **approved method only** during **Phase 6 (Execute)**.
> Covers: mysqldump/pg_dump, Percona XtraBackup + S3, DMS Full Load + CDC, Aurora Read
> Replica, Blue/Green, Oracle Data Pump via S3, SQL Server native backup/restore,
> schema-object migration (DEFINER stripping, logins/Agent jobs, TDE certificates), and
> the pre-production rehearsal. DMS deep configuration: [dms-best-practices.md](dms-best-practices.md).

## Method-Specific Procedures

### If mysqldump / pg_dump (small DBs, downtime OK)

```bash
# MySQL: Full export including all schema objects
mysqldump --single-transaction --routines --triggers --events \
  --set-gtid-purged=OFF --column-statistics=0 \
  -h $SOURCE_HOST -u admin -p your_db > full_dump.sql

# Import to Aurora
mysql -h $AURORA_ENDPOINT -u admin -p your_db < full_dump.sql
```

> **Logical dump = a clean major-version-upgrade opportunity.** Because mysqldump/pg_dump re-import the data from scratch, the target can be a **newer major version** than the source (e.g. MariaDB 10.5 → 10.11, MySQL 5.7 → 8.0, PostgreSQL 13 → 16) in the *same* migration — no separate upgrade project. Validate with `CHECKSUM TABLE` that bytes are identical across the version gap (they were 10.5→10.11 in the reference migration). **But version gaps introduce behavioral changes** — reserved-word additions, deprecated/removed functions, changed defaults (charset, sql_mode, auth plugin). Review **[version-upgrades.md](version-upgrades.md)** before choosing the target version. (Physical methods — XtraBackup, snapshot, Read Replica — cannot skip major versions; only logical dump can.)

```bash
# PostgreSQL: Parallel dump/restore
pg_dump -Fd -j 8 -h $SOURCE_HOST -U postgres -d your_db -f /backup/
pg_restore -Fd -j 8 -h $AURORA_ENDPOINT -U postgres -d your_db /backup/
```

### If Percona XtraBackup + S3 (large MySQL, physical)

```bash
# 1. Full backup
export MYSQL_PWD=$(aws secretsmanager get-secret-value --secret-id $SECRET_ID --query SecretString --output text | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])')
xtrabackup --backup --target-dir=/backup --user=admin

# 2. Prepare backup
xtrabackup --prepare --target-dir=/backup

# 3. Upload to S3
aws s3 sync /backup/ s3://your-bucket/xtrabackup/ --sse aws:kms

# 4. Restore to Aurora (via console or CLI)
aws rds restore-db-cluster-from-s3 \
  --db-cluster-identifier your-aurora-cluster \
  --engine aurora-mysql \
  --engine-version 8.0.mysql_aurora.3.07.1 \
  --s3-bucket-name your-bucket \
  --s3-prefix xtrabackup/ \
  --s3-ingestion-role-arn arn:aws:iam::ACCOUNT:role/aurora-s3-role \
  --source-engine mysql \
  --source-engine-version 8.0.36 \
  --master-username admin \
  --master-user-password $NEW_PASS
```

**Requirements:**
- Source: MySQL 5.7 (XtraBackup 2.4) or MySQL 8.0 (XtraBackup 8.0)
- `innodb_file_per_table` must be enabled
- NO encrypted tablespaces (TDE must be off)
- InnoDB page size must be 16 KB (default)

### If DMS Full Load + CDC (zero-downtime)

See [dms-best-practices.md](dms-best-practices.md) for complete DMS configuration including:
- Replication instance sizing
- Source/target endpoint configuration
- Task settings (Full Load + CDC, parallel load, batch apply, LOB handling)
- Table mappings
- Monitoring

**Key prerequisite for CDC:**
- MySQL/MariaDB: `binlog_format=ROW`, `binlog_row_image=FULL`, `log_bin=ON`
- PostgreSQL: `wal_level=logical`, available replication slots

### If XtraBackup Seed + Binlog/DMS CDC Catch-up (large MySQL, minimal downtime — matrix row 6)

Combine the two procedures above: the physical copy does the bulk, CDC closes the delta.

```bash
# 1. Take the XtraBackup exactly as in the previous section. The backup RECORDS the
#    consistent binlog position itself — read it from the prepared backup dir:
cat /backup/xtrabackup_binlog_info        # e.g.  mysql-bin.000042  1337  [gtid-set]

# 2. Restore to Aurora via restore-db-cluster-from-s3 (previous section, steps 3-4).

# 3. Catch up the delta from the RECORDED position — two equivalent channels:
# 3a. Native binlog replication (Aurora as replica of the source):
mysql -h $AURORA_ENDPOINT -u admin -p -e "
  CALL mysql.rds_set_external_source ('$SOURCE_HOST', 3306, '$REPL_USER', '$REPL_PASS',
       'mysql-bin.000042', 1337, 0);
  CALL mysql.rds_start_replication;"
#     Requires a REPLICATION SLAVE user on the source and binlog retention long enough
#     to cover the bulk-copy + restore time: on the source,
#     SET GLOBAL binlog_expire_logs_seconds ≥ (copy+restore hours × 3600) × 2.
# 3b. Or a DMS CDC-only task with --cdc-start-position "mysql-bin.000042:1337".

# 4. Monitor lag until ≈0 (SHOW REPLICA STATUS → Seconds_Behind_Source, or CDC metrics),
#    then hold it running until the cutover window. At cutover (cutover-procedures.md):
#    freeze source → lag=0 → CALL mysql.rds_stop_replication / stop the DMS task → proceed.
```

**The recorded position is the whole game** — a wrong or expired position means silent
duplicate/missing rows. Record it in `migration-plan.md` the moment the backup completes.
(Oracle equivalent: Data Pump seed with the SCN captured via `FLASHBACK_SCN`, then DMS CDC
`--cdc-start-position` at that SCN — matrix row 15.)

### If PostgreSQL Native Logical Replication (EC2/on-prem PG → Aurora PG, near-zero downtime — matrix row 11)

Preferred over DMS for PG→PG: better datatype fidelity, no replication instance to run.
Constraints to state up front: **sequences are NOT replicated** (re-seed at cutover — the
high-water-mark step is mandatory), **DDL is not replicated** (freeze schema changes for
the migration window), every table needs a PK or `REPLICA IDENTITY FULL`, and large
objects (`lo`) are not carried.

```bash
# 0. Prerequisites — SOURCE postgresql.conf (restart if wal_level changes):
#    wal_level=logical, max_replication_slots ≥ 2, max_wal_senders ≥ 2
#    pg_hba.conf: allow replication connection from the Aurora VPC/SG path.
#    TARGET Aurora PG cluster parameter group: rds.logical_replication=1 (reboot).

# 1. Schema first (logical replication moves rows, not DDL):
pg_dump --schema-only --no-owner --no-privileges -h $SOURCE -U postgres your_db \
  | psql -h $AURORA_ENDPOINT -U postgres -d your_db

# 2. On SOURCE: publication for all tables (or an explicit list):
psql -h $SOURCE -U postgres -d your_db -c "CREATE PUBLICATION mig_pub FOR ALL TABLES;"

# 3. On TARGET: subscription — initial data copy + streaming happen automatically:
psql -h $AURORA_ENDPOINT -U postgres -d your_db -c "
  CREATE SUBSCRIPTION mig_sub
  CONNECTION 'host=$SOURCE port=5432 dbname=your_db user=repl_user password=...'
  PUBLICATION mig_pub;"       # creates its own slot on the source

# 4. Monitor: initial sync state per table, then ongoing lag:
psql -h $AURORA_ENDPOINT -c "SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY 1;"
#    srsubstate: i=init, d=copying, s/r=synced+streaming — wait for all 'r'
psql -h $SOURCE -c "SELECT slot_name, confirmed_flush_lsn,
  pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes
  FROM pg_replication_slots;"

# 5. At cutover (cutover-procedures.md order): freeze source → lag_bytes=0 →
#    DISABLE the subscription (ALTER SUBSCRIPTION mig_sub DISABLE;) → re-seed sequences
#    (setval per pg_get_serial_sequence) → repoint clients.

# 6. AFTER the rollback window — drop subscription AND make sure the source slot is gone
#    (an orphaned slot pins WAL on the source until the disk fills):
psql -h $AURORA_ENDPOINT -c "DROP SUBSCRIPTION mig_sub;"
psql -h $SOURCE -c "SELECT pg_drop_replication_slot(slot_name)
  FROM pg_replication_slots WHERE slot_name LIKE '%mig_sub%' AND NOT active;"
```

Version note: source must be PG 10+; cross-major replication (13 → 16) is supported and
is the standard near-zero-downtime major-upgrade path — run the
[version-upgrades.md](version-upgrades.md) validation battery when crossing majors.

### If Aurora Read Replica (RDS MySQL/PG → Aurora)

```bash
# From RDS MySQL → Aurora MySQL Read Replica
aws rds create-db-cluster \
  --db-cluster-identifier aurora-replica-cluster \
  --engine aurora-mysql \
  --replication-source-identifier arn:aws:rds:REGION:ACCOUNT:db:source-rds-instance

# Wait for replica to sync, then promote
aws rds promote-read-replica-db-cluster \
  --db-cluster-identifier aurora-replica-cluster
```

**Only works from RDS (not EC2 directly).** For EC2 → Aurora, use DMS or XtraBackup.

### If Blue/Green Deployment (RDS/Aurora in-place upgrade)

```bash
aws rds create-blue-green-deployment \
  --blue-green-deployment-name "migrate-to-aurora" \
  --source "arn:aws:rds:REGION:ACCOUNT:db:source-instance" \
  --target-engine-version "8.0.mysql_aurora.3.07.1"

# Wait for green to be AVAILABLE and synchronized, then:
aws rds switchover-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --switchover-timeout 300
```

### If Oracle Data Pump (Oracle → RDS Oracle, primary method)

RDS Oracle has **no OS shell** — you cannot run `impdp`/`expdp` on the host. You either call **`DBMS_DATAPUMP`** from a SQL client, or run **`impdp` from a remote Oracle Instant Client** against the RDS endpoint. The dump moves to RDS via **S3 integration** (most common) or **`DBMS_FILE_TRANSFER` over a DB link**.

> **Schema/table mode only — never FULL mode**, and never import SYS/SYSTEM/RDSADMIN-owned objects: a full-mode import can damage the data dictionary. RDS does not grant SYS/SYSDBA.

**One-time setup — S3 integration:** attach an IAM policy (`s3:GetObject`, `s3:ListBucket`, `s3:PutObject`; add `s3:AbortMultipartUpload`+`s3:ListMultipartUploadParts` for ≥100 MB files; add `kms:Decrypt`/`Encrypt`/`GenerateDataKey`/`DescribeKey` for SSE-KMS buckets — bucket must be **same Region**, SSE-C not supported), associate the role for `S3_INTEGRATION`, and add the `S3_INTEGRATION` option to the option group:
```bash
aws rds add-role-to-db-instance --db-instance-identifier my-oracle-target \
  --feature-name S3_INTEGRATION --role-arn arn:aws:iam::ACCT:role/rds-s3-integration-role
aws rds add-option-to-option-group --option-group-name myoptiongroup \
  --options OptionName=S3_INTEGRATION,OptionVersion=1.0 --apply-immediately
```

```sql
-- 1. On TARGET (as master): create the schema + grants (schema mode)
CREATE USER schema_1 IDENTIFIED BY "<password>";
GRANT CREATE SESSION, RESOURCE TO schema_1;
ALTER USER schema_1 QUOTA UNLIMITED ON users;
```

```sql
-- 2. On SOURCE: export with DBMS_DATAPUMP (schema mode). EXCLUDE Scheduler objects
--    owned by system schemas (importing those into RDS is unsupported).
DECLARE v_hdnl NUMBER;
BEGIN
  v_hdnl := DBMS_DATAPUMP.OPEN(operation=>'EXPORT', job_mode=>'SCHEMA', job_name=>NULL);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample.dmp','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_dump_file);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample_exp.log','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_log_file);
  DBMS_DATAPUMP.METADATA_FILTER(v_hdnl,'SCHEMA_EXPR','IN (''SCHEMA_1'')');
  DBMS_DATAPUMP.START_JOB(v_hdnl);
END;
/
-- (expdp equivalent: expdp user/pwd@src DIRECTORY=DATA_PUMP_DIR DUMPFILE=sample.dmp SCHEMAS=SCHEMA_1 PARALLEL=4)
-- For a TDE source, export with ENCRYPTION_MODE=PASSWORD (TRANSPARENT mode is NOT supported by RDS).
-- Each S3 object must be ≤ 5 TiB — use PARALLEL to split larger dumps into multiple files.
```

```sql
-- 3. Upload dump to S3 (from source, if source is also RDS; else use `aws s3 cp` from the OS)
SELECT rdsadmin.rdsadmin_s3_tasks.upload_to_s3(
  p_bucket_name=>'amzn-s3-demo-bucket', p_directory_name=>'DATA_PUMP_DIR') AS task_id FROM dual;

-- 4. On TARGET (as master): download dump from S3 into DATA_PUMP_DIR (async; returns a task id)
SELECT rdsadmin.rdsadmin_s3_tasks.download_from_s3(
  p_bucket_name=>'amzn-s3-demo-bucket', p_directory_name=>'DATA_PUMP_DIR') AS task_id FROM dual;
-- confirm the file landed
SELECT * FROM TABLE(rdsadmin.rds_file_util.listdir('DATA_PUMP_DIR')) ORDER BY mtime;
```

```sql
-- 5. On TARGET (as master): import via DBMS_DATAPUMP. Add METADATA_REMAP for tablespace/schema
--    remap; set TABLE_EXISTS_ACTION=>'REPLACE' to re-run.
DECLARE v_hdnl NUMBER;
BEGIN
  v_hdnl := DBMS_DATAPUMP.OPEN(operation=>'IMPORT', job_mode=>'SCHEMA', job_name=>NULL);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample.dmp','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_dump_file);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample_imp.log','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_log_file);
  DBMS_DATAPUMP.METADATA_FILTER(v_hdnl,'SCHEMA_EXPR','IN (''SCHEMA_1'')');
  DBMS_DATAPUMP.START_JOB(v_hdnl);
END;
/
```

```bash
# 5-alt. Or run impdp from a REMOTE Instant Client (bastion/EC2) against the RDS endpoint:
impdp admin@//RDS-ENDPOINT:1521/DBNAME \
  directory=DATA_PUMP_DIR dumpfile=sample.dmp logfile=sample_imp.log \
  schemas=SCHEMA_1 table_exists_action=replace
```

```sql
-- 6. Cleanup — dump files are NOT auto-purged and consume the same EBS volume as datafiles
EXEC UTL_FILE.FREMOVE('DATA_PUMP_DIR','sample.dmp');
```

**Alternative transfer (no S3): `DBMS_FILE_TRANSFER` over a DB link** — create a DB link from source to the RDS endpoint, then `DBMS_FILE_TRANSFER.PUT_FILE(...)` to push `sample.dmp` into the target `DATA_PUMP_DIR`; import as in step 5. Requires VPC routing + security-group ingress between source and target.

**Best practice:** transfer the dump → take a **DB snapshot** → test the import. If objects get invalidated, delete and recreate from the snapshot (the staged dump is included).

**Transportable tablespaces (XTTS, very large EE DBs):** use the dedicated `rdsadmin.rdsadmin_transport_util` package (not the regular impdp path). EE-only, source ≥ 12c, Linux only, target release ≥ source, **no encrypted tablespaces**, cannot transport `SYSTEM`/`SYSAUX` or non-data objects (recreate PL/SQL/views/users/sequences via Data Pump metadata), and the instance must have no read replicas. S3 file limit 5 TiB (EFS recommended for larger). See [aws-official-migration-methods.md](aws-official-migration-methods.md).

### If SQL Server Native Backup/Restore (SQL Server → RDS SQL Server, primary method)

RDS SQL Server has **no OS access** and **no `RESTORE FROM DISK`** — you restore a `.bak` staged in **S3** via the `msdb.dbo.rds_*` procedures, enabled by the **`SQLSERVER_BACKUP_RESTORE`** option.

**One-time setup:** create an S3 bucket in the **same Region**, an IAM role (trust `rds.amazonaws.com`, scoped with `aws:SourceArn` for the DB instance + option group), and add the option:
```bash
aws rds add-option-to-option-group --apply-immediately --option-group-name mybackupgroup \
  --options "OptionName=SQLSERVER_BACKUP_RESTORE,OptionSettings=[{Name=IAM_ROLE_ARN,Value=arn:aws:iam::ACCT:role/rds-backup-restore-role}]"
aws rds modify-db-instance --db-instance-identifier mydbinstance \
  --option-group-name mybackupgroup --apply-immediately   # no restart required
```
The IAM permissions policy needs `s3:ListBucket`,`s3:GetBucketLocation` on the bucket and `s3:GetObject`,`s3:PutObject`,`s3:ListMultipartUploadParts`,`s3:AbortMultipartUpload`,`s3:GetObjectAttributes` on `bucket/*`; add `kms:DescribeKey`/`GenerateDataKey`/`Encrypt`/`Decrypt` on a **symmetric** key for encrypted backups.

```sql
-- 1. On SOURCE: take a native backup, then upload .bak to S3 (aws s3 cp from the source host)
BACKUP DATABASE mydatabase TO DISK = 'D:\backups\mydb_full.bak' WITH INIT, FORMAT, COMPRESSION;
```
```bash
aws s3 cp D:\backups\mydb_full.bak s3://my-bucket/sqlbackups/mydb_full.bak
```
```sql
-- 2. On RDS: restore (single file → DB comes online; @with_norecovery defaults to 0 for FULL)
exec msdb.dbo.rds_restore_database
  @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/sqlbackups/mydb_full.bak';

-- 3. Monitor (status refreshes ~every 5%; history kept 36 days)
exec msdb.dbo.rds_task_status @db_name='mydatabase';
-- cancel:  exec msdb.dbo.rds_cancel_task @task_id=<n>;   (cannot cancel FINISH_RESTORE)
```

**Multifile backup (large DBs, ≤10 files, parallel throughput):** the `*` is expanded to `1-of-N`, etc.:
```sql
exec msdb.dbo.rds_backup_database @source_db_name='mydatabase',
  @s3_arn_to_backup_to='arn:aws:s3:::my-bucket/out/backup*.bak',
  @number_of_files=4, @max_transfer_size=4194304, @buffer_count=10;
-- restore by giving the common prefix + '*'
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/out/backup*';
```

**Minimal-downtime sequence (FULL + DIFFERENTIAL + LOG)** — source must be in **FULL recovery model**. Restore the big backups ahead of time `WITH NORECOVERY`, apply the final log at cutover `WITH RECOVERY`:
```sql
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_full.bak', @type='FULL', @with_norecovery=1;
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_diff.bak', @type='DIFFERENTIAL', @with_norecovery=1;
exec msdb.dbo.rds_restore_log @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_log1.trn', @with_norecovery=1;
-- final log at cutover brings the DB online (rds_restore_log defaults to NORECOVERY=1, so set 0)
exec msdb.dbo.rds_restore_log @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_logN.trn', @with_norecovery=0;
-- or, if the last task was left NORECOVERY:
exec msdb.dbo.rds_finish_restore @db_name='mydatabase';
```
`rds_restore_log` supports `@stopat='2026-06-04 03:57:09'` for point-in-time. Drop a stuck restore: `exec msdb.dbo.rds_drop_database @db_name='mydatabase';`

**Constraints:** S3 bucket same Region as instance; cannot restore over an existing DB name; 5 TB per file, native restore up to 64 TiB (Express 10 GB); up to 2 concurrent tasks; **cannot back up to a `.bak` from RDS for log shipping (no native log backups from RDS)**; a `.bak` from a *higher* engine version won't restore; FILESTREAM filegroups rejected; Multi-AZ native restore requires FULL recovery model; not supported with cross-Region read replicas; KMS must be symmetric; procedures can't run inside a transaction. **Logins, SQL Agent jobs, and linked servers are NOT in a user-DB `.bak`** — migrate them separately (§"SQL Server — Server-Level Objects" below).

---

## Schema Object Migration (If Method Doesn't Include Them)

If you used DMS, binlog replication, or S3 import — schema objects must be migrated separately:

```bash
# MySQL: Export schema objects only (no data)
mysqldump --routines --triggers --events --no-data --no-create-info \
  -h $SOURCE -u admin -p your_db > schema_objects.sql

# Remove DEFINER clauses (they break on Aurora)
sed -i 's/DEFINER=[^*]*\*/\*/g' schema_objects.sql

# Import to target
mysql -h $TARGET -u admin -p your_db < schema_objects.sql
```

```bash
# PostgreSQL: Functions, triggers, views, types
pg_dump --schema-only --no-owner --no-privileges \
  -h $SOURCE -U postgres your_db | \
  grep -v 'COMMENT ON EXTENSION' > schema.sql

psql -h $TARGET -U postgres -d your_db -f schema.sql
```

### Oracle — Objects NOT Carried by a Schema-Mode Data Pump

Schema-mode Data Pump brings in-schema objects (procs, triggers, views, sequences). It does **not** bring SYS/SYSTEM-owned Scheduler jobs (intentionally excluded), nor will arbitrary directory objects / ACLs work as-is on RDS:
- **Scheduler jobs**: recreate **app-owned** `DBMS_SCHEDULER` jobs on the target (migrate any legacy `DBMS_JOB` to `DBMS_SCHEDULER`). Never recreate SYS/SYSTEM-owned jobs.
- **Network ACLs** (for `UTL_HTTP`/`UTL_SMTP`/`UTL_TCP`): re-grant with `DBMS_NETWORK_ACL_ADMIN` on the target and confirm VPC egress.
- **Database links**: recreate; they need VPC routing + security-group rules and updated TNS descriptors.
- **Directory objects / external tables / BFILE**: re-stage through RDS-managed directories (the master user lacks `CREATE ANY DIRECTORY`).

### SQL Server — Server-Level Objects (logins, Agent jobs, linked servers)

A user-DB `.bak` carries **database users** but not **server logins** → SQL-auth users are **orphaned** after restore (login SID mismatch). Fastest fix: recreate logins on RDS with the **same SID + HASHED password** so the orphan auto-resolves.

```sql
-- On SOURCE: generate CREATE LOGIN statements preserving hash + SID
SELECT 'CREATE LOGIN ' + QUOTENAME(p.name) +
  CASE WHEN p.type_desc='SQL_LOGIN'
    THEN ' WITH PASSWORD = ' + CONVERT(NVARCHAR(MAX),l.password_hash,1) +
         ' HASHED, SID = ' + CONVERT(NVARCHAR(MAX),p.sid,1) + ';'
    ELSE ' FROM WINDOWS;' END
FROM sys.server_principals p
LEFT JOIN sys.sql_logins l ON p.principal_id=l.principal_id
WHERE p.type_desc IN ('SQL_LOGIN','WINDOWS_LOGIN','WINDOWS_GROUP')
  AND p.name NOT LIKE '##%##' AND p.name <> 'sa'
  AND p.name NOT LIKE 'NT SERVICE%' AND p.name NOT LIKE 'NT AUTHORITY%';
```
```sql
-- On RDS, after restore: if a user is still orphaned, relink (preferred over deprecated sp_change_users_login)
USE [mydatabase];
EXEC sp_change_users_login 'Report';     -- list orphans
ALTER USER [appuser] WITH LOGIN = [appuser];
```
- **SQL Agent jobs** live in `msdb` (not importable) — script them out on the source and recreate (no CmdExec/PowerShell/replication steps, no email/alerts on RDS).
- **Linked servers** are server-level — recreate manually (Oracle OLEDB has a dedicated RDS option).
- **CLR**: `SAFE` assemblies only on ≤2016; not supported 2017+ — refactor.

### TDE-Encrypted Source — Bringing the Certificate / Re-encrypting

- **Oracle**: you **cannot import your own wallet**. Export with Data Pump `ENCRYPTION_MODE=PASSWORD`, import into a TDE-enabled target whose wallet **AWS generates** (`SELECT * FROM v$encryption_wallet;` to confirm). Create encrypted tablespaces normally: `CREATE TABLESPACE enc_ts ENCRYPTION USING 'AES256' DEFAULT STORAGE(ENCRYPT);`. The `TDE` option is permanent — to remove it you must export to a non-TDE instance.
- **SQL Server**: bring the **source TDE certificate** in first via `rds_restore_tde_certificate` (cert name must start with `UserTDECertificate_`). On the source, `BACKUP CERTIFICATE ... WITH PRIVATE KEY`, where the private-key password is the **plaintext** of a KMS data key (`aws kms generate-data-key --key-spec AES_256`); upload `.cer`/`.pvk` to S3 and tag the `.pvk` object with `x-amz-meta-rds-tde-pwd` = the KMS `CiphertextBlob`:
  ```sql
  EXECUTE msdb.dbo.rds_restore_tde_certificate
    @certificate_name='UserTDECertificate_mycert',
    @certificate_file_s3_arn='arn:aws:s3:::cert-bucket/tde-cert.cer',
    @private_key_file_s3_arn='arn:aws:s3:::cert-bucket/tde-key.pvk',
    @kms_password_key_arn='arn:aws:kms:us-west-2:ACCT:key/<key-id>';
  ```
  Then `rds_restore_database` the TDE `.bak`; RDS **auto-rekeys** the restored DB to an RDS-managed `RDSTDECertificate` before it becomes available. Constraints: both `SQLSERVER_BACKUP_RESTORE` + `TDE` options required; **TDE cert restore not supported on Multi-AZ** (do it Single-AZ, then convert); max 10 user certs; no cross-account KMS keys.

---

## Migration Rehearsal (STRONGLY Recommended Before Production)

> **If the customer declines a clone rehearsal** (common for small DBs — "too much
> cost/time"): do not silently skip. (1) State plainly what risk moves into the production
> window — untested cutover mechanics, unmeasured time budget. (2) Get the waiver recorded
> in `migration-plan.md`. (3) Recover what rehearsal value you can without a clone:
> component-test every freeze-window command against the REAL target (syntax differs
> across engine versions), pre-stage the cutover scripts on-host (see cutover-procedures.md
> §"ONE Pre-Staged Script per Host"), and treat the forward seed+catch-up run as your
> timing measurement. The canonical failure this prevents: an untested
> `SHOW BINARY LOG STATUS` (MySQL 8.4 syntax) against Aurora 8.0, discovered mid-freeze,
> turning a 40-second window into a 5-minute one.

Before executing against production, perform a full dry-run:

1. **Create a source clone**: Snapshot the source EC2, launch a clone in same VPC.
2. **Run the full migration against the clone** (all phases).
3. **Measure actual time**: Record duration of each phase.
4. **Validate cutover procedure**: Practice end-to-end.
5. **Verify rollback**: Test the rollback procedure works.
6. **Destroy the clone**: Delete all rehearsal resources.

This de-risks production by: confirming time estimates, catching permission/network/compatibility issues, giving team confidence, and providing a realistic timeline for stakeholders.
