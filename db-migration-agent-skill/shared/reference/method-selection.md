# Migration Method Selection

> Read this during **Phase 3 (Select Method)**. The method is a decision the user must
> make or approve — present options with trade-offs, never silently pick one.
> Full per-method procedures: [execution-runbooks.md](execution-runbooks.md) and
> [aws-official-migration-methods.md](aws-official-migration-methods.md).

## Method Selection Procedure

**This is a decision the user must make (or approve).** Present the options with trade-offs.

> The decision inputs below were already collected during Phase 1 discovery
> (`migration-plan.md` §Phase 1) — **do not re-ask what is already answered**; use this
> list only to spot gaps that block a matrix row from resolving.

### Decision Input — Ask the User

1. **Homogeneous or heterogeneous?** Same engine family (MySQL→MySQL/Aurora MySQL, PostgreSQL→PostgreSQL/Aurora PG, MariaDB→MariaDB, **Oracle→RDS Oracle, SQL Server→RDS SQL Server**) = **homogeneous**, stay in this skill (homogeneous path). Engine family *changes* (Oracle/SQL Server/Tibero/CUBRID → Aurora/PostgreSQL/MySQL, MySQL→PostgreSQL) = **heterogeneous**, go to [heterogeneous-migration.md](heterogeneous-migration.md) first (schema/code conversion), then return for cutover.
2. **What is your source?** EC2 MySQL / EC2 MariaDB / EC2 PostgreSQL / EC2/on-prem Oracle / EC2/on-prem SQL Server / RDS MySQL / RDS MariaDB / RDS PostgreSQL / On-premises / Other cloud (Azure DB, Cloud SQL)
3. **What is your target?** Aurora MySQL / Aurora PostgreSQL / RDS MySQL / RDS MariaDB / RDS PostgreSQL / **RDS Oracle** / **RDS SQL Server**
4. **Downtime tolerance?** Zero / Seconds / Minutes / Hours (maintenance window)
5. **Database size?** < 10 GB / 10-100 GB / 100 GB - 1 TB / > 1 TB
6. **Network bandwidth (source → AWS)?** Same-VPC/region / Direct Connect (state Gbps) / VPN (state Mbps) / internet egress (state Mbps) — needed for the throughput check in source-assessment.md §Throughput Estimation.
7. **Do you need stored procedures, triggers, views migrated?** Yes / No / Don't know
8. **Migrating more than one database?** If yes, see "Multi-database scenarios" below.
9. **Do you have a preferred method?** (User may already know what they want)
10. **Number of databases on the source host?** (If >1, each migrates independently. Flag schema name collisions.)
11. **Can the application code be modified?** (Gates: app-side encryption, connection-string change vs Secrets Manager, dual-read capability)
12. **RPO (Recovery Point Objective)?** (Acceptable data loss if rollback needed. Zero RPO requires reverse replication.)
13. **Downstream CDC consumers?** (Debezium/Kafka, BI replicas, ETL jobs reading binlog — these break at cutover and need re-pointing.)
14. **Encryption requirement at creation time?** (KMS key type: AWS-managed vs CMK. Cannot change after cluster creation.)
15. **Cross-region or cross-account?** (Routes to different networking/KMS/DMS setup.)

### Method Decision Matrix

> **Homogeneous only.** This matrix is for same-engine-family migrations, including **Oracle → RDS
> Oracle** (rows 13–15) and **SQL Server → RDS SQL Server** (rows 16–18). If the engine family
> *changes* (Oracle/SQL Server/Tibero/CUBRID → Aurora/PostgreSQL/MySQL, or MySQL ↔ PostgreSQL), stop
> and use [heterogeneous-migration.md](heterogeneous-migration.md) — you need
> schema/code conversion (SCT / DMS Schema Conversion / Babelfish) before any data move.
>
> **Deterministic read order.** Rows are evaluated **top to bottom; take the FIRST row whose
> Source + Target + Size + Downtime + Bandwidth all match.** Conditions are mutually exclusive
> within a source, so exactly one row applies. "Bulk transfer fits window?" refers to the
> Phase 2 throughput estimate (`estimated_hours` vs transfer window).

| # | Source | Target | Size | Downtime tolerance | Bulk transfer fits window? | **Recommended Method** | Why |
|---|--------|--------|------|--------------------|----------------------------|------------------------|-----|
| 1 | RDS MySQL | Aurora MySQL | Any | < 1 min | n/a (same region) | **Aurora Read Replica promotion** | Built-in, seconds of downtime, migrates everything |
| 2 | RDS MySQL **or** RDS MariaDB | RDS MySQL / RDS MariaDB / Aurora MySQL | Any | < 1 min | n/a | **Blue/Green Deployment** | Managed switchover w/ guardrails (MySQL↔Aurora MySQL cross-engine supported) |
| 3 | RDS PostgreSQL | Aurora PostgreSQL | Any | < 1 min | n/a | **Aurora Read Replica promotion** | Built-in |
| 4 | EC2/on-prem MySQL or MariaDB | Aurora MySQL / RDS MySQL / RDS MariaDB | < 10 GB | Yes (< 1 hr) | Yes | **mysqldump** (`--routines --triggers --events`) | Simplest, migrates ALL objects |
| 5 | EC2/on-prem MySQL or MariaDB | Aurora MySQL / RDS MySQL / RDS MariaDB | 10 GB – 1 TB | Yes (hours) | Yes | **Percona XtraBackup + S3** | 3–7× faster than DMS, physical copy incl. all objects |
| 6 | EC2/on-prem MySQL or MariaDB | Aurora MySQL / RDS MySQL / RDS MariaDB | > 1 TB | Minimal (minutes) | Yes | **XtraBackup + S3 seed → binlog/DMS CDC catch-up** | Fast physical bulk, then drain delta; cutover = final drain |
| 7 | EC2/on-prem MySQL or MariaDB | Aurora MySQL / RDS MySQL / RDS MariaDB | Any | No (zero/seconds) | Yes | **DMS Full Load + CDC** | Only near-zero-downtime path from EC2/on-prem (see schema-objects note) |
| 8 | On-prem MySQL/MariaDB/PostgreSQL | Aurora / RDS (same family) | > 1 TB | Any | **No** (bandwidth-bound) | **Snow Family seed + DMS CDC** (or DataSync for 100 GB–1 TB) | Wire can't carry it in time — see Offline-Seed Branch |
| 9 | EC2/on-prem PostgreSQL | Aurora PostgreSQL / RDS PostgreSQL | < 10 GB | Yes | Yes | **pg_dump / pg_restore** (`-Fd -j`) | Complete (all objects), simple, parallel |
| 10 | EC2/on-prem PostgreSQL | Aurora PostgreSQL / RDS PostgreSQL | 10 GB – 1 TB | Yes (hours) | Yes | **pg_dump/restore (parallel)** | No physical-backup S3 path for PG; parallel dump is the bulk tool |
| 11 | EC2/on-prem PostgreSQL | Aurora PostgreSQL / RDS PostgreSQL | Any | No (zero/seconds) | Yes | **Native logical replication** (preferred) or **DMS CDC** | PG-native handles more DDL; DMS simpler to operate |
| 12 | Other cloud (Azure DB for MySQL/PostgreSQL, Cloud SQL) | Aurora / RDS (same family) | Any | No (near-zero) | per estimate | **DMS Full Load + CDC** | No native cross-cloud replica; DMS connects over public/peered endpoint |
| 13 | EC2/on-prem **Oracle** | **RDS Oracle** | < 1 TB | Yes (hours) | Yes | **Oracle Data Pump** (schema/table mode, via S3 integration) | AWS-recommended logical method; migrates schema + data. No FULL mode. |
| 14 | EC2/on-prem **Oracle** (EE) | **RDS Oracle** (EE) | > 1 TB | Minimal (minutes) | Yes | **Transportable tablespaces (XTTS via RMAN)** → optional DMS CDC catch-up | Physical, fastest for very large EE DBs; EE-only, no encrypted tablespaces, source not 11g. |
| 15 | EC2/on-prem **Oracle** | **RDS Oracle** | Any | No (zero/near-zero) | Yes | **Data Pump bulk load → AWS DMS CDC from recorded SCN** (or GoldenGate) | Only near-zero-downtime path; Data Pump seeds, DMS/GoldenGate drains delta. |
| 16 | EC2/on-prem **SQL Server** | **RDS SQL Server** | Any | Yes (hours) | Yes | **Native backup/restore** (.bak via S3, `rds_restore_database`) | AWS-recommended lift-and-shift; migrates the user DB. Logins/jobs separate (execution-runbooks.md §schema objects). For tiny DBs (< ~5 GB) a scripted schema+`bcp` copy is an acceptable substitute when the S3/option-group setup outweighs it — but .bak is the default: it preserves everything in-DB (permissions, defaults, computed cols) with no per-object scripting risk. |
| 17 | EC2/on-prem **SQL Server** | **RDS SQL Server** | > 1 TB | Minimal (minutes) | Yes | **Native FULL + DIFFERENTIAL + LOG** (restore WITH NORECOVERY, final log WITH RECOVERY at cutover) | Shrinks cutover to tail-log restore. Source must be FULL recovery model. |
| 18 | EC2/on-prem **SQL Server** | **RDS SQL Server** | Any | No (zero/near-zero) | Yes | **AWS DMS Full Load + CDC** | Near-zero downtime; DMS moves data only — migrate logins/jobs/perms separately. |

**Notes on overlap resolution:**
- Rows 13–15 (Oracle): prefer **Data Pump** (row 13) for the common downtime-OK case; **XTTS** (row 14) only when EE + very large + no encrypted tablespaces + source ≥ 12c; **DMS/GoldenGate CDC** (row 15) when downtime must be near-zero. RMAN whole-DB physical restore is **NOT** supported into managed RDS Oracle (EC2/RDS Custom only).
- Rows 16–18 (SQL Server): prefer **native backup/restore** (row 16); use **full+diff+log** (row 17) to minimize the cutover window; **DMS** (row 18) only for near-zero downtime. Log Shipping, Replication, and `RESTORE FROM DISK` are not available on RDS.
- Rows 1 vs 2 for RDS MySQL → Aurora MySQL: prefer **Read Replica promotion** (row 1) for the
  lowest downtime; choose **Blue/Green** (row 2) when you want staged validation + one-command
  switchover, or when also doing a version upgrade.
- Snapshot migration (RDS-source, minutes of downtime) is a fallback only when Read Replica /
  Blue/Green are unavailable for the engine version — not a first choice, so it's omitted from the
  primary tree.
- The old "RDS Console Auto-Migrate / Any-Any" catch-all has been **removed**: it is just a console
  wrapper around DMS and applied to scenarios it doesn't fit (on-prem, >1 TB, heterogeneous). Use the
  specific row above; if you want the console UX, that maps to row 7/12 (DMS) behind the scenes.

### Binlog State Gate (MySQL/MariaDB — check BEFORE committing to a method)

Every near-zero-downtime path (DMS CDC, binlog replication, reverse replication for lossless rollback) **requires `log_bin=ON` with `binlog_format=ROW`** on the source. Carry the Phase 2 `SHOW VARIABLES LIKE 'log_bin'` result *directly* into method selection — do not assume CDC is available:

| Source `log_bin` | DB size | Recommendation |
|------------------|---------|----------------|
| **ON** (`binlog_format=ROW`) | Any | CDC methods available — matrix rows 6–8 (XtraBackup+CDC, DMS) are on the table. |
| **OFF** | Small (rough guide: **< ~1–2 GB**, fits a brief maintenance window per the Phase 2 throughput estimate) | **Prefer a brief dump cutover** (mysqldump → import → repoint app). Enabling binlog requires a **source restart = downtime anyway**; for a tiny DB the one-shot dump cutover causes *less total disruption* than "restart to enable binlog, then stand up DMS." |
| **OFF** | Large | You must **enable binlog first** (`log_bin=ON`, `binlog_format=ROW`, adequate `binlog retention`) — and **acknowledge the restart cost** with the user — before any CDC method. There is no zero-downtime path while binlog is off. |

> **The contradiction to surface explicitly:** "zero-downtime" and `log_bin=OFF` are mutually exclusive until you take the restart to enable binlog. State this trade-off to the user; for small DBs the honest answer is often a 1–2 minute dump cutover, not a CDC project. This also means **reverse replication may be impossible** — see cutover-procedures.md §"When Reverse Replication is NOT Possible."

### Multi-Database Scenarios

When consolidating or moving more than one database:

- **Migrate sequentially, not in parallel**, unless each DB has its own DMS replication instance
  and target — a shared DMS instance saturates and CDC latency climbs across all tasks.
- **Schema/name collisions**: two source DBs with the same schema name cannot co-locate on one
  target without renaming. Decide up-front: separate RDS/Aurora clusters, or rename schemas via DMS
  table-mapping transformation rules (`rename` actions).
- **Cross-database queries / FKs** between the source DBs block consolidation onto separate clusters
  — those joins must move to the app layer or the DBs must land on the same cluster.
- **Shared users/grants**: migrate the global `mysql.user` / `pg_roles` grants once, deduplicated,
  not per-DB (each method's schema-objects step omits grants — see execution-runbooks.md).
- **Sequence the cutovers**: cut over the least-dependent DB first; keep reverse replication
  (cutover-procedures.md) running per-DB so each can roll back independently.

### Method Summary

| Method | Migrates Schema Objects? | Downtime | Complexity | Best Size Range |
|--------|------------------------|----------|-----------|-----------------|
| mysqldump / pg_dump | ✅ ALL (procs, triggers, views, events) | Minutes-hours | Low | < 50 GB |
| Percona XtraBackup + S3 | ✅ ALL (physical copy) | Minutes-hours | Medium | 10 GB - multi-TB |
| DMS Full Load + CDC | ❌ Data only (no procs/triggers/views) | Near-zero | Medium-High | Any size |
| Aurora Read Replica | ✅ ALL | Seconds | Low | Any (RDS source only) |
| Snapshot migration | ✅ ALL | Minutes | Low | Any (RDS source only) |
| Binlog replication | ❌ Data only | Near-zero | High | Any (MySQL/MariaDB) |
| S3 import (LOAD DATA FROM S3) | ❌ Data only | Depends on load time | Medium | Flat file import |
| PG logical replication | ❌ Data + DDL (no procs/grants) | Near-zero | Medium | Any (PG 10+) |
| Blue/Green Deployment | ✅ ALL | < 1 minute | Low | Any (RDS/Aurora source) |
| Oracle Data Pump (schema/table) | ✅ ALL in-schema objects (procs, triggers, views, sequences); NOT SYS-owned/Scheduler | Minutes-hours | Medium | < 1 TB (Oracle→RDS Oracle) |
| Oracle transportable tablespaces (XTTS) | ❌ Data/tablespaces only (recreate PL/SQL, views, users via metadata) | Minimal | High | > 1 TB EE (Oracle→RDS Oracle) |
| SQL Server native backup/restore | ✅ User DB objects; NOT logins/Agent jobs/linked servers (server-level) | Minutes-hours | Low | Any ≤ 64 TiB (SQL Server→RDS SQL Server) |

### Critical Note on Schema Objects

**DMS, binlog replication, and S3 import do NOT migrate:**
- Stored procedures, functions
- Triggers
- Views
- Events (MySQL)
- Sequences (PostgreSQL)
- User grants/permissions
- Custom data types (PostgreSQL)

**If user needs these migrated → must use dump/restore for schema + chosen method for data**, or use a method that does physical copy (XtraBackup, snapshot, Read Replica).

### Edge-Case Scenarios

**Multi-Database**: see "Multi-Database Scenarios" above.

**Cross-Region**: Requires DMS with cross-region replication. Set up VPC peering or Transit Gateway between source and target regions. Use region-specific KMS keys (encryption keys don't cross regions). Consider Aurora Global Database as the target for ongoing cross-region reads post-migration.

**Cross-Account**: Source and target in different AWS accounts. DMS endpoints use cross-account VPC peering plus IAM roles for access. KMS key policy must grant the target account access for encrypted snapshots. Snapshot sharing requires explicit account grant via modify-db-snapshot-attribute.

**>10 TB or Low Bandwidth (on-prem)**: If estimated transfer time exceeds 7 days, use AWS Snow Family (Snowball Edge) or DataSync for the baseline data. Critical: capture the binlog position or PostgreSQL LSN at the exact point of data export. After the Snow device is loaded into AWS and data is in S3, restore to Aurora and set up CDC replication from the captured position to catch up with changes that occurred during transit.
