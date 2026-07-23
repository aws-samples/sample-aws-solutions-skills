# Source Assessment — Scope, Compatibility Blockers, Sizing, Access Paths

> Read this during **Phase 2 (Assess)**. It covers: engine scope, the blocker/adjustment
> catalog with assessment queries (MySQL/MariaDB/PostgreSQL/Oracle/SQL Server), how to
> physically reach a private-subnet source, credential-handling rules, sizing queries,
> throughput estimation, and the offline-seed (Snow/DataSync) branch.
> Full per-limitation detail: [rds-aurora-limitations.md](rds-aurora-limitations.md).

## Scope & Coverage (Read First)

**Engines covered.** This skill covers migration to **all RDS/Aurora engines**: MySQL, MariaDB, PostgreSQL, Oracle, SQL Server, Db2, Aurora MySQL, Aurora PostgreSQL.

- **Homogeneous** (same engine family, e.g. EC2 MySQL → Aurora MySQL, **Oracle → RDS Oracle, SQL Server → RDS SQL Server**): the native-tool fast paths (see method-selection.md and execution-runbooks.md). DMS is *not* the default. Oracle and SQL Server lift-and-shift have **dedicated native paths** — Oracle Data Pump ([execution-runbooks.md](execution-runbooks.md) §"Oracle Data Pump") and SQL Server native backup/restore via S3 (execution-runbooks.md §"SQL Server Native Backup/Restore"). The matrix in [method-selection.md](method-selection.md) covers them.
- **Heterogeneous** (Oracle/SQL Server → **Aurora/PostgreSQL/MySQL**, i.e. the engine family *changes*): requires schema/code conversion. See **[heterogeneous-migration.md](heterogeneous-migration.md)** — SCT / DMS Schema Conversion / Babelfish, PL/SQL→PL/pgSQL challenges, license implications. **Oracle → RDS Oracle and SQL Server → RDS SQL Server are NOT heterogeneous** — stay in this skill.
- **Korean-market source engines** (Tibero, CUBRID, Altibase): **no native AWS DMS/SCT tooling** — PoC + JDBC paths. See heterogeneous-migration.md §5.

**Korean enterprise scenarios (CHECK EARLY).** Korean enterprises almost always wrap the DB in a domestic **access-control/audit appliance** (Chakra Max, DBSafer, Petra) and a **DB encryption** product (Petra Cipher, D'Amo, CUBE-One). These break on managed RDS in their sniffing/agent/plug-in modes and are the **#1 cause of stalled migrations**. Before choosing a method, read **[third-party-db-security.md](third-party-db-security.md)** and **[regulatory-compliance.md](regulatory-compliance.md)** (PIPA encryption mandates, network separation (mangbunri), ISMS-P, audit-log retention). The rule: **access control → vendor gateway mode in the VPC + Database Activity Streams; encryption → vendor API mode or KMS at-rest + app-side column encryption.**

> **Downtime expectation.** For EC2 → RDS/Aurora migrations, this skill targets a **10–30 second write-pause** at cutover (app restart or connection-pool refresh). True zero-downtime (0 lost requests) isn't achievable on this path without an intermediary proxy already in place — the skill optimizes for the *shortest* pause via CDC catch-up + coordinated cutover. To minimize further: pre-set connection-pool `maxLifetime` to 30s; prefer a coordinated app restart over Secrets Manager rotation (≈10s vs up to 5 min for pool TTL); deploy RDS Proxy on the **target** so future failovers are sub-second even though the initial cutover still pauses briefly. Blue/Green (< 1s with RDS Proxy) needs the source *already* on RDS — it applies to RDS→Aurora upgrades, not initial EC2→RDS moves. See cutover-procedures.md §"Minimize the Write-Pause Window".

---

## Compatibility Assessment (CRITICAL — Do First)

Before choosing a migration method or target, assess whether the source workload is COMPATIBLE with managed services. The following are common **blockers** and **adjustments** that must be resolved first.

### 1.1 Blockers — Must Resolve Before Migration

| Category | Blocker | Impact | Resolution |
|----------|---------|--------|------------|
| **Encryption** | MySQL TDE (Transparent Data Encryption) enabled | Data encrypted at tablespace level won't transfer | Decrypt before migration → re-encrypt with AWS KMS at rest |
| **Encryption** | Keyring plugins (keyring_file, keyring_aws for MySQL) | Plugin not available on RDS/Aurora | Decrypt → use AWS KMS volume encryption instead |
| **Encryption** | Third-party agents (Vormetric, Thales CipherTrust) | Host-level encryption agents can't run on managed instances | Remove agent → use AWS KMS + column-level app encryption |
| **Encryption (KR)** | Korean DB encryption in **plug-in / OS-volume mode** (Petra Cipher plug-in, D'Amo DE/KE, CUBE-One) | Engine plug-ins and OS-volume agents can't run on managed RDS | Switch to vendor **API/app-side mode**, or KMS at-rest + app-side column encryption (SEED/ARIA at app layer if mandated). See `third-party-db-security.md` |
| **Access control (KR)** | Korean DB access/audit in **sniffing or host-agent mode** (Chakra Max, DBSafer agent, Petra sniffing) | No SPAN/port-mirror and no OS access on managed RDS | Move to vendor **gateway/proxy mode** in the VPC + **Database Activity Streams** for audit. See `third-party-db-security.md` |
| **Storage Engine** | MyISAM tables (Aurora target) | Aurora is InnoDB-only | Convert: `ALTER TABLE t ENGINE=InnoDB` before migration |
| **Storage Engine** | FEDERATED engine | Not supported on RDS or Aurora | Redesign as application-level cross-DB queries |
| **Storage Engine** | Custom engines (TokuDB, RocksDB, Spider) | Not available on managed services | Convert to InnoDB |
| **Auth** | PAM / LDAP authentication plugins | Not supported | Switch to native auth, IAM DB auth, or Kerberos (AD) |
| **Auth** | Custom auth plugins | Not loadable | Switch to native password or IAM auth |
| **Features** | C-compiled UDFs (User Defined Functions) | Cannot install custom .so files | Rewrite as stored functions or move logic to app layer |
| **Features** | Galera Cluster / Group Replication topology | Not available on Aurora | Redesign using Aurora Multi-AZ + read replicas |
| **PostgreSQL** | C-language extensions (custom compiled) | Cannot install on Aurora | Check `pg_available_extensions`; rewrite or remove |
| **PostgreSQL** | Direct pg_hba.conf access | No filesystem access | Use RDS security groups + IAM auth + SSL settings |
| **Compressed tables** | InnoDB compressed row_format (Aurora) | Not supported on Aurora | `ALTER TABLE t ROW_FORMAT=DYNAMIC` before migration |

### 1.2 Adjustments — Require Changes But Not Blocking

| Category | Issue | What Happens | Workaround |
|----------|-------|-------------|------------|
| **Privileges** | Code uses SUPER privilege | Will fail on RDS/Aurora | Use `rds_superuser_role` (RDS) or session-level alternatives |
| **Privileges** | DEFINER clauses in stored procs/views | Fails if definer user doesn't exist | Strip DEFINER or recreate user on target |
| **File I/O** | `LOAD DATA INFILE` (server-side) | Blocked on RDS/Aurora | Use `LOAD DATA LOCAL INFILE` (client-side) or S3 import |
| **File I/O** | `SELECT INTO OUTFILE` | Blocked | Use `SELECT ... INTO` with S3 (Aurora) or client-side export |
| **Replication** | Multi-source replication | Only on RDS MySQL 8.0.35+, NOT Aurora | Redesign consolidation approach |
| **Charset** | `lower_case_table_names` set to 2 | Not supported on Linux-based RDS/Aurora | Must be 0 (case-sensitive) or 1 (lowercase) |
| **Timezone** | Non-UTC timezone_data differences | Can corrupt TIMESTAMP columns during migration | Set `time_zone` parameter explicitly on target |
| **Versions** | MySQL 5.6 or earlier | Cannot migrate directly to Aurora MySQL 3.x (8.0) | Upgrade to 5.7 first, then migrate |
| **OS** | Cron jobs / shell scripts on DB host | No OS access on managed | Move to EventBridge Scheduler, Lambda, or ECS tasks |

### 1.3 Assessment Queries

**Check for TDE (MySQL):**
```sql
SELECT * FROM information_schema.INNODB_TABLESPACES WHERE ENCRYPTION = 'Y';
SHOW VARIABLES LIKE 'early-plugin-load';  -- keyring plugin loaded?
```

**Check storage engines (MySQL/MariaDB):**
```sql
SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'your_db' AND ENGINE != 'InnoDB';
```

**Check compressed tables (Aurora blocker):**
```sql
SELECT TABLE_NAME, ROW_FORMAT FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'your_db' AND ROW_FORMAT = 'Compressed';
```

**Check auth plugins:**
```sql
SELECT user, host, plugin FROM mysql.user WHERE plugin NOT IN ('mysql_native_password', 'caching_sha2_password');
```

**Check DEFINER clauses:**
```sql
SELECT ROUTINE_NAME, DEFINER FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'your_db';
SELECT TABLE_NAME, DEFINER FROM information_schema.VIEWS WHERE TABLE_SCHEMA = 'your_db';
```

**Check UDFs:**
```sql
SELECT * FROM mysql.func;  -- Lists all installed UDFs
```

**Check PostgreSQL extensions:**
```sql
SELECT extname, extversion FROM pg_extension;
-- Compare against Aurora's available extensions:
SELECT name FROM pg_available_extensions ORDER BY name;
```

### 1.4 Oracle → RDS for Oracle — Blockers & Compatibility Checks

RDS for Oracle is managed: **no OS/SSH, no RAC, no ASM (EBS-backed), no SYS/SYSDBA, no custom patches.** The master user gets the `DBA` role **minus** `ALTER DATABASE`, `ALTER SYSTEM`, `CREATE ANY DIRECTORY`, `DROP ANY DIRECTORY`, `GRANT ANY PRIVILEGE`, `GRANT ANY ROLE` — admin tasks go through `rdsadmin` packages and parameter groups instead. Assess these before choosing a method:

| Category | Blocker / Adjustment | Impact | Resolution |
|----------|----------------------|--------|------------|
| **Engine version** | Only **19c and 21c** are supported/creatable on managed RDS. 10.2 / 11g / 12c / 18c are gone. | Cannot land on managed RDS | Upgrade to 19c+ first, or use **RDS Custom for Oracle** (BYOL/EE; note end-of-support 2027-03-31) / EC2 for deprecated majors. |
| **Edition / licensing** | License Included = **SE2 only**; Enterprise Edition = **BYOL only**. SE2 caps at 16 vCPU / 128 GiB, no Data Guard / read replicas. | Wrong target edition blocks creation | Decide LI-SE2 vs BYOL-EE up front; EE features (TDE, partitioning options) need BYOL-EE. |
| **Character set** | DB character set + NCHAR set are **fixed at instance creation, cannot change after**. CDB DB charset is always `AL32UTF8` (set non-default only on the PDB). | Mismatch corrupts data, unfixable post-create except recreate | Match target `--character-set-name` / `--nchar-character-set-name` to source (or proper superset) **before** loading. e.g. `KO16MSWIN949` for legacy Korean. |
| **Architecture** | RAC topology / ASM storage | Not available | Redesign as single-instance + Multi-AZ; storage is EBS, no ASM tuning. |
| **Disallowed import modes** | Data Pump **FULL mode** import; importing SYS/SYSTEM/RDSADMIN-owned objects (incl. Scheduler objects) | Can damage the data dictionary; unsupported | Use **schema or table mode** only; `EXCLUDE` system Scheduler objects (see execution-runbooks.md). |
| **Transportable tablespaces import** | Dump files made with `TRANSPORT_TABLESPACES`/`TRANSPORTABLE`/`TRANSPORT_FULL_CHECK` via the *regular* impdp path | Not supported on the standard path | Use the dedicated `rdsadmin.rdsadmin_transport_util` XTTS path (EE-only — see method-selection.md). |
| **TDE wallet** | Customer-managed Oracle Wallet | Cannot import your own wallet into managed RDS | RDS manages the wallet/master key. Export with Data Pump `ENCRYPTION_MODE=PASSWORD` (TRANSPARENT mode unsupported), import into a TDE-enabled target whose wallet AWS generates. See §1.4 checks below. |
| **OS-file dependencies** | BFILE, external tables, `UTL_FILE`, FTP/SFTP, Messaging Gateway, Oracle Text File/URL datastores | No general filesystem; several features unsupported | Re-stage through RDS-managed directories (`DATA_PUMP_DIR` via S3); rework BFILE/external tables; Text must use non-File/URL datastores. |
| **Unsupported features** | Data Guard, Database Vault, Flashback Database, RAS, Unified Auditing Pure Mode, Workspace Manager (WMSYS), hybrid partitioned tables, OEM repository | Won't migrate | Inventory dependencies; redesign or drop. Data Guard target → EC2/RDS Custom only. |
| **File/block limits** | 16 TiB per single file (ext4); `DB_BLOCK_SIZE` fixed at creation; up to ~64 TiB instance storage | Large bigfile datafiles / block-size changes blocked | Plan tablespace layout; set block size at creation. |

**Oracle pre-migration inventory queries** (run on source, resolve before migrating):
```sql
-- Directory objects (master user can't CREATE ANY DIRECTORY on RDS)
SELECT owner, directory_name, directory_path FROM dba_directories;
-- External tables (depend on directory objects / OS files)
SELECT owner, table_name, default_directory_name FROM dba_external_tables;
-- Network/file ACL-dependent packages (UTL_HTTP/SMTP/TCP need re-granted ACLs + VPC egress)
SELECT * FROM dba_network_acls;
-- Database links (recreate; need VPC routing + SG rules)
SELECT owner, db_link, host FROM dba_db_links;
-- Scheduler jobs (recreate app-owned; never import SYS/SYSTEM-owned)
SELECT owner, job_name, schedule_type FROM dba_scheduler_jobs WHERE owner NOT IN ('SYS','SYSTEM','RDSADMIN');
-- Java in the DB
SELECT owner, COUNT(*) FROM dba_objects WHERE object_type LIKE 'JAVA%' GROUP BY owner;
-- TDE in use at source?
SELECT * FROM v$encryption_wallet;
SELECT tablespace_name, encrypted FROM dba_tablespaces WHERE encrypted='YES';
```

### 1.5 SQL Server → RDS for SQL Server — Blockers & Compatibility Checks

RDS for SQL Server is managed: **no OS/RDP, no `sysadmin` server role, no `RESTORE FROM DISK`.** The master user belongs only to `processadmin`, `public`, `setupadmin`; a DB creator gets `db_owner`. The file-based migration path is **native backup/restore via S3** (execution-runbooks.md). Assess:

| Category | Blocker / Adjustment | Impact | Resolution |
|----------|----------------------|--------|------------|
| **Engine version / restore direction** | Native restore accepts a `.bak` from an **equal-or-lower** engine version, **never higher**. Supported majors: 2016/2017/2019/2022. | Higher-version `.bak` won't restore | Pick target RDS engine version ≥ source. EOS versions (2014 and older) are auto-upgraded; can keep older DB `COMPATIBILITY_LEVEL`. |
| **Edition / licensing** | License Included (Enterprise/Standard/Web/Express) or **BYOM** (Ent/Std/Developer) via License Mobility + active Software Assurance. Standard capped at 24 cores / 128 GB by MS limits. | Wrong edition blocks features | Choose edition for the features you need (TDE, Multi-AZ). |
| **TDE edition gating** | TDE needs Enterprise on 2016/2017; **Standard or Enterprise** on 2019/2022. | TDE unavailable on Web/Express | Pick Standard+ / Enterprise. |
| **Security model** | Code needing `sysadmin`, `xp_cmdshell`, `CONTROL SERVER`, `UNSAFE`/`EXTERNAL_ACCESS` assembly, `CREATE ENDPOINT`, server-level triggers, `TRUSTWORTHY` | Roles/permissions not grantable on RDS | Refactor; move logic out of DB; CLR `SAFE` only on ≤2016, **not supported 2017+**. |
| **FILESTREAM** | `.bak` containing a FILESTREAM filegroup | Native restore rejects it | Remove FILESTREAM/FileTable; redesign as BLOB columns or S3. |
| **Unsupported features** | Log Shipping, Replication (as publisher/distributor), Maintenance Plans, Service Broker cross-instance endpoints, MSDTC/distributed transactions, PolyBase, Stretch DB, ML/R Services, backup to Azure Blob | Won't migrate | Emulate log shipping via native full+diff+log restores; recreate jobs; cross-instance messaging won't work. |
| **Server-level objects** | Logins, SQL Agent jobs, linked servers live in `master`/`msdb` — **not** carried by a user-DB `.bak` (can't import `msdb`) | Orphaned users + missing jobs after restore | Script logins (preserve SID + HASHED password), Agent jobs, and linked servers separately; recreate on RDS. See execution-runbooks.md §"SQL Server — Server-Level Objects". |
| **Max databases / Multi-AZ** | Per-instance DB limit depends on instance class + AZ mode; Multi-AZ native restore requires **FULL recovery model**; no native log *backups* from RDS | Restore/convert fails if over limit | Check the per-class limit before sizing; keep source in FULL recovery for minimal-downtime restores. |
| **Collation** | Instance/server default collation set at creation | Server-level collation can't change later | Match instance collation to source; DB/column collations ride in the `.bak`. |

**SQL Server pre-migration inventory queries** (run on source):
```sql
-- Recovery model (must be FULL for Multi-AZ native restore / log restores)
SELECT name, recovery_model_desc FROM sys.databases;
-- FILESTREAM filegroups (blocker for native restore)
SELECT DB_NAME(database_id) db, type_desc, name FROM sys.master_files WHERE type_desc='FILESTREAM';
-- CLR assemblies (unsupported 2017+)
SELECT name, permission_set_desc FROM sys.assemblies WHERE is_user_defined=1;
-- Logins to recreate on RDS (SID + hash preserved later — see execution-runbooks.md)
SELECT name, type_desc FROM sys.server_principals WHERE type IN ('S','U','G') AND name NOT LIKE '##%##';
-- SQL Agent jobs to recreate
SELECT name, enabled FROM msdb.dbo.sysjobs;
-- Linked servers to recreate
SELECT name, product, provider FROM sys.servers WHERE is_linked=1;
-- TDE-encrypted DBs (need certificate migration — see execution-runbooks.md §TDE)
SELECT DB_NAME(database_id) db, encryption_state FROM sys.dm_database_encryption_keys;
```

---

## Third-Party Tool Interference Sweep (ALWAYS run — every engine, every engagement)

Real databases are almost never bare: security, backup, monitoring, HA, and proxy tools
attach to the host or sit in front of the port — and each is a migration dependency that
breaks differently on managed RDS (no OS access, no agents, no SPAN port). Customers
usually forget these exist until asked (discovery Q17), so **detect, don't just ask**:

```bash
# On the DB host (via SSM): one sweep catches most agent categories
ps -eo comm,args | grep -viE "^\[|mysqld|postgres|sqlservr" | sort -u | head -40
systemctl list-units --type=service --state=running --no-pager | grep -viE "mysql|postgres|mssql|ssm|amazon|systemd|dbus|cron|network"
dpkg -l 2>/dev/null | grep -iE "veeam|commvault|netbackup|datadog|newrelic|zabbix|nagios|guardium|imperva|proxysql|pgbouncer|maxscale|sios|pacemaker" \
  || rpm -qa 2>/dev/null | grep -iE "veeam|commvault|netbackup|datadog|newrelic|zabbix|guardium|proxysql|pgbouncer|maxscale"
ss -tnp | awk '$5 ~ /:3306|:5432|:1433|:1521/' | head -20   # who proxies/monitors the port locally?
```

| Category | Examples | What breaks on managed RDS | Disposition |
|----------|----------|---------------------------|-------------|
| **DB security / audit / encryption** | Chakra Max, DBSafer, Petra (KR); Guardium, Imperva, Thales/Vormetric (global) | Sniffing / host-agent / engine-plug-in / OS-volume modes all die (no OS, no SPAN) | Full playbook: [third-party-db-security.md](third-party-db-security.md) — gateway/API modes survive; DAS/KMS replace the rest |
| **Backup agents** | Veeam, Commvault, NetBackup, custom dump scripts | Host agents can't install on RDS; local dump scripts die with the host | Retire in favor of RDS automated backups / AWS Backup; verify the customer's retention policy is met BEFORE decommission — the agent's catalog may be the only long-term archive |
| **Monitoring agents** | Datadog/New Relic/Zabbix DB plugins, Prometheus exporters on-host | On-host collectors gone; some checks (filesystem, replication internals) have no RDS equivalent | Repoint to endpoint-based checks + CloudWatch/Performance Insights integrations; map each dashboard/alert to its replacement in the plan |
| **HA / clusterware** | Galera (already a blocker), SIOS, Pacemaker/heartbeat, keepalived VIPs | Whole topology replaced by Multi-AZ/Aurora; a VIP the app points at is a hidden client-repoint target | Decommission with the cluster; if the app connects to a VIP, that VIP is a Phase 7.5 inventory row |
| **Connection proxies / poolers** | ProxySQL, PgBouncer, MaxScale, HAProxy (on host or in front) | The proxy is BOTH a client (repoint its backend) and topology (apps point at it, not the DB) | Decide: keep the proxy and repoint its backend at cutover (often the cheapest cutover of all), or retire it for RDS Proxy — either way it gets inventory rows on both sides |

Every hit → a row in `migration-plan.md` (finding + disposition + owner). A tool you
found here will reappear in Phase 7.5 (as a client) or Phase 9 (as a decommission item) —
cross-reference the rows.

## Source Database Assessment

### Execution Location — How Will You Reach the Source DB? (Decide First)

The source DB is almost always in a **private subnet**, and your execution environment (laptop, CloudShell, an automation runner, this skill's host) is frequently **in a different VPC or has no `mysql`/`psql` client installed**. Settle the access path *before* running any assessment query — it determines how every assessment, dump, and cutover command in this skill is invoked.

| Option | When | How |
|--------|------|-----|
| **Direct** | Execution host is in the **same VPC/subnet** and has a client installed | Standard `mysql -h <db> …` / `psql` |
| **Bastion (SSH tunnel)** | A bastion host exists in the DB's VPC | `ssh -L 3306:<db-endpoint>:3306 ec2-user@bastion` then connect to `127.0.0.1:3306` |
| **SSM Port Forwarding** | DB host (or a host in-VPC) is an SSM-managed instance; you have a local client | `aws ssm start-session --target <instance-id> --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters '{"host":["<db-endpoint>"],"portNumber":["3306"],"localPortNumber":["13306"]}'` then connect to `127.0.0.1:13306` |
| **SSM Send-Command** | **No local client / cross-VPC** — run the query *on the DB host itself* (or another in-VPC SSM-managed host that has a client) | `aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript --parameters 'commands=["mysql -h 127.0.0.1 -e \"…\""]'` |

> **Default recommendation for an isolated/private-subnet source: SSM.** Port forwarding when you have a local client; **Send-Command when you don't** (run the client that already exists on the DB host). This avoids opening the DB to new networks just to migrate it. The same path is reused for the `mysqldump`/`pg_dump` export (execution-runbooks.md) and the processlist cross-checks (cutover-procedures.md).

### Credential Handling — Never Put Passwords in `argv`

Every command below (assessment, dump, cutover) needs DB credentials. **Passwords in command-line arguments are visible in `ps -ef`, shell history, and — when run via SSM/SSH — in CloudTrail and SSM command history.** Rules:

- **Never** pass `-p<password>` or `--password=<pw>` on the command line.
- Use **`MYSQL_PWD`** env var (`export MYSQL_PWD=...; mysql -h … -u …`) or a **`--defaults-extra-file`** with `[client] password=...` (chmod 600).
- **Preferred:** fetch the secret **on the DB host** using the instance's IAM role, so the plaintext never transits your machine or appears in argv:
  ```bash
  # Run on the DB host (e.g. via SSM Send-Command); password stays on the host, out of argv
  export MYSQL_PWD=$(aws secretsmanager get-secret-value \
    --secret-id ecommerce-demo/db-credentials --query SecretString --output text \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])')
  mysql -h 127.0.0.1 -u admin -e "SELECT VERSION();"
  ```
- PostgreSQL: use `PGPASSWORD` or a `~/.pgpass` (chmod 600) the same way.

### Information to Collect

| Category | What | How |
|----------|------|-----|
| Engine & version | MySQL 5.7/8.0, MariaDB 10.x, PostgreSQL 12-16 | `SELECT VERSION();` |
| Total size | Data + indexes in GB | See queries below |
| Table count | Number of tables, top 10 largest | `information_schema.tables` |
| Schema objects | Stored procs, triggers, views, events, functions | `information_schema.routines`, `pg_proc` — **privileged account required, see warning below** |
| LOB columns | BLOB/TEXT/JSON columns, max sizes | Profile actual data sizes |
| Replication readiness | Binary logging (MySQL) / wal_level (PostgreSQL) | `SHOW VARIABLES LIKE 'log_bin'` / `SHOW wal_level` |
| Current connections | Typical/peak concurrent connections | `SHOW STATUS LIKE 'Threads_connected'` |
| Source location | EC2 (same VPC? same region?), on-premises, other cloud? | User input |
| **Network bandwidth** | Usable Mbps on the path source→AWS (Direct Connect / VPN / internet egress). Use the *usable* figure, not the link's rated speed. | User input / `iperf3` to an EC2 box in target region |
| **Transfer window** | Hours of acceptable downtime (offline copy) *or* hours available for bulk load before CDC catch-up | User input |

> ⚠️ **Schema-object inventory is privilege-filtered — a low-privilege account SILENTLY
> UNDERCOUNTS.** `information_schema.routines` (MySQL/MariaDB) only shows routines the
> current account can see; an app user without `SHOW ROUTINE`/definer visibility returns
> an empty set even when procedures exist, and the plan then wrongly records "no schema
> objects" — the classic way a target ships missing a stored procedure that nothing
> notices until the first application call fails. Rules: (1) run the object inventory with a privileged account (root/definer-
> visible or a migration user granted `SHOW ROUTINE` / `SELECT ON mysql.proc`-equivalent);
> (2) **cross-check counts from two vantage points** — e.g. `SHOW PROCEDURE STATUS WHERE
> Db='your_db'` as root (via SSM on-host) vs the same query as the assessment user — and
> treat any mismatch as "inventory incomplete", never "no objects"; (3) PostgreSQL:
> `pg_proc` visibility is schema/ACL-dependent — inventory as a superuser or table owner.

### Sizing Queries

**MySQL/MariaDB:**
```sql
SELECT table_schema,
  ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 2) AS size_gb,
  COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_schema = 'your_db'
GROUP BY table_schema;
```

**PostgreSQL:**
```sql
SELECT pg_size_pretty(pg_database_size('your_db')) AS db_size;
SELECT count(*) AS table_count FROM pg_tables WHERE schemaname = 'public';
```

**Oracle:**
```sql
-- Total DB size (datafiles + temp)
SELECT ROUND(SUM(bytes)/1024/1024/1024, 2) AS size_gb FROM dba_segments;
-- Per-schema size + object counts
SELECT owner, ROUND(SUM(bytes)/1024/1024/1024,2) AS size_gb
FROM dba_segments WHERE owner NOT IN ('SYS','SYSTEM','RDSADMIN') GROUP BY owner;
SELECT owner, object_type, COUNT(*) FROM dba_objects
WHERE owner NOT IN ('SYS','SYSTEM','RDSADMIN') GROUP BY owner, object_type ORDER BY 1,2;
```

**SQL Server:**
```sql
-- Per-DB size
SELECT DB_NAME(database_id) AS db, SUM(size)*8/1024 AS size_mb FROM sys.master_files GROUP BY database_id;
-- Object counts + per-table rows (run in the target DB context)
SELECT type_desc, COUNT(*) FROM sys.objects GROUP BY type_desc ORDER BY type_desc;
```

### Throughput Estimation (run BEFORE choosing a method)

A method is only viable if the bulk data can physically move within the transfer window.
Estimate the over-the-wire transfer time from DB size and usable bandwidth:

```
estimated_hours = db_size_gb / (bandwidth_mbps * 0.125 * 0.7)
```

- `* 0.125` converts Mbps → MB/s (8 bits per byte).
- `* 0.7` is a 70% real-world efficiency factor (TCP overhead, encryption, contention, restart
  retries). For a clean same-region 10 GbE path you can use 0.8; for busy shared internet egress
  use 0.5.

**Worked examples** (usable bandwidth, not rated link speed):

| DB size | Usable bandwidth | Estimated transfer | Implication |
|---------|------------------|--------------------|-------------|
| 50 GB | 1 Gbps (≈940 Mbps usable) | ~0.2 hr (~12 min) | Online copy trivially fits any window |
| 500 GB | 200 Mbps (typical site VPN) | ~10 hr | Needs an overnight window or CDC catch-up |
| 2 TB | 200 Mbps | ~40 hr | Wire transfer infeasible in a normal window → **Snow Family** |
| 2 TB | 1 Gbps Direct Connect | ~8 hr | Feasible with DX + CDC; without DX, use Snow |
| 10 TB | 500 Mbps | ~127 hr (>5 days) | **Snow Family mandatory** |

**Decision rule:**

1. Compute `estimated_hours`.
2. If `estimated_hours` ≤ transfer window → online method (dump/restore, XtraBackup+S3, DMS) is fine.
3. If `estimated_hours` > transfer window **and** source is on-prem/other-cloud **and** size > 1 TB
   → **flag it** and route to the offline-seed branch below. Do not silently pick a method that
   can't finish in time.
4. If only modestly over window → DMS Full Load + CDC: the bulk full-load can run for days while
   the app stays up, and CDC closes the gap — the *downtime* is just the final CDC drain, not the
   whole transfer.

### Offline-Seed Branch — Snow Family / DataSync (on-prem + low bandwidth + > 1 TB)

When the wire can't carry the data in time, seed Aurora/RDS from a physical/offline copy, then
catch up the delta with CDC:

| Condition | Approach |
|-----------|----------|
| > 1 TB, on-prem, bandwidth-bound, hard cutover deadline | **AWS Snowball Edge**: export dump/XtraBackup to the device → ship to AWS → load into S3 → `restore-db-cluster-from-s3` (MySQL) or import → then **DMS CDC** from on-prem to close the delta accumulated since the export LSN/binlog position. |
| 100 GB – 1 TB, on-prem, slow but no hard deadline | **AWS DataSync** over DX/VPN to land the dump in S3 (managed, resumable, checksummed), then restore + CDC catch-up. |
| Continuous/repeated file sync from on-prem | **DataSync** scheduled tasks. |

**Critical for Snow + CDC**: record the source's binlog file+position (MySQL) or LSN/replication
slot (PostgreSQL) **at the moment the offline export is taken**, so the subsequent DMS CDC task
starts exactly from that point. Mismatched start position = duplicate or missing rows.

---
