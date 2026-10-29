# MySQL ↔ PostgreSQL (heterogeneous) — Migration Reference

> **Load at Phase 2–3 as soon as source and target engine families differ between MySQL
> and PostgreSQL** (MySQL/MariaDB/Aurora MySQL ↔ PostgreSQL/Aurora PostgreSQL), and again at
> Phases 6–8. Two directions in one file: **§MySQL → PostgreSQL** and **§PostgreSQL →
> MySQL**, plus shared sections. `heterogeneous-migration.md` stays the Oracle/SQL
> Server/Korean-engine reference; schema/code conversion itself is still chained to the
> official `dms-schema-conversion` skill (`mcp-and-tooling.md` §Chaining) — this file is the
> decision framing, the read-only assessment, the type/semantic traps, data movement,
> cross-engine validation and the reverse-DMS rollback.
>
> Facts verified against AWS/MySQL/PostgreSQL/pgloader documentation **as of 2026-10**
> (sources at the end; *[live]* = also reproduced on mysql:8.0 / postgres:16 containers).
> Versions and DMS Schema Conversion support move — re-verify with
> `aws rds describe-db-engine-versions` and the DMS "Sources/Targets" pages at GATE 2.

## 0. Rules for this pair (read first)

1. **Application SQL review is mandatory, every time.** Data can be converted perfectly and
   the application still breaks (quoting, `||`, `GROUP BY`, booleans, case-sensitive
   comparisons, isolation level). There is no "unchanged app" heterogeneous path: produce
   `app-remediation-findings.md` (`heterogeneous-migration.md` §7) and track it as a
   critical-path dependency. ORM-only apps reduce, but do not remove, the review (raw SQL,
   native queries, migrations, reports, stored procedures).
2. **Never let DMS create the target tables** (`TargetTablePrepMode: DROP_AND_CREATE`) for
   this pair. Its default type mappings silently change semantics (§4.2/§4.3). Pre-create
   the converted schema and load with `DO_NOTHING` or `TRUNCATE_BEFORE_LOAD`.
3. **Native tools do not cross the family.** `mysqldump`/`pg_dump` files, XtraBackup,
   snapshots, Read Replicas, Blue/Green, binlog replication and PG logical replication are
   all out. Data movement is **AWS DMS** (full load, or full load + CDC) or, for MySQL →
   PostgreSQL only, **pgloader** (offline, no CDC). Method rows: `method-selection.md`
   §Heterogeneous MySQL ↔ PostgreSQL rows H1–H5.
4. **Rollback = a reverse DMS task or an accepted RPO.** There is no native reverse
   replication between the families. A reverse task has hard prerequisites that must be in
   place **before** cutover (§10) — `rds.logical_replication=1` on a PostgreSQL target is a
   static parameter (reboot), PostgreSQL sources cannot start CDC from a timestamp.
5. **Validate with normalized comparison, not checksums.** `CHECKSUM TABLE` and
   `md5(string_agg(...))` can never match across engines; use
   `shared/scripts/cross_engine_compare.py` (§9) plus DMS data validation.
6. **A column DMS treats as a LOB on a table without a PK/UK is a GATE 2 blocker for every
   DMS path, both directions and the reverse task** (§4.7 — confirmed blockers vs checks): DMS drops the column, every row arrives
   NULL, row counts still match. Never let `change-data-type` stand unproven (§4.4).

## 1. Decision inputs (both directions)

### 1.1 Target engine and version [as of 2026-10]

| Target | Current line | Choose when | Watch |
|---|---|---|---|
| **Aurora PostgreSQL** | 18 GA 2026-06-11 (from 18.3); latest minors 18.6 / 17.11 / 16.15 / 15.19 / 14.24 | MySQL → PG default for production | DMS **Schema Conversion** lists Aurora PG **14–17** as targets (not 18 yet); DMS data migration supports PG 17/18 targets on **DMS 3.5.4+**. Aurora PG 18 changes `max_parallel_workers_per_gather` default to 0. |
| **RDS for PostgreSQL** | 14–18 | Lower floor cost, single-instance | DMS SC targets RDS PG 14–18. |
| **Aurora MySQL 3.x** | MySQL **8.0**-compatible; standard support ends **2028-04-30** (Extended Support to 2029-07-31) | PG → MySQL when DMS Schema Conversion must target it directly | DMS SC lists **Aurora MySQL 8.0.32 / RDS MySQL 8.0.23** as targets only. |
| **Aurora MySQL 8.4** | GA **2026-05-21** (8.4.7-compatible; 8.4.8 GA 2026-09-03); standard support to April 2032 | PG → MySQL with the longest runway | Not a DMS SC target (convert against 8.0, apply to 8.4 = **unverified path — rehearse**). DMS needs **3.5.4+**; `require_secure_transport=ON` by default → DMS `SslMode` `none` fails (MySQL error 3159) — use `verify-ca`/`verify-full`; new users default to `caching_sha2_password`. See `version-upgrades.md` §"MySQL 8.0 → 8.4". |
| **RDS for MySQL 8.4** | 8.4 LTS | Same as above, RDS | Same DMS/TLS notes. |

**`lower_case_table_names` is a creation-time decision for a MySQL target.** AWS SCT's
PostgreSQL → MySQL conversion requires `lower_case_table_names=1` on the target. On **Aurora
MySQL 3** the value is set permanently at cluster creation; on **RDS for MySQL 8.0/8.4** it
can't be modified in a parameter group associated with an instance (set it in a custom
parameter group before creating the instance; value 1 is supported from 8.0.28 and on 8.4).
Decide before Phase 5 (`target-provisioning.md` §immutable settings; `cdk-stacks.md`).

### 1.2 Application modifiability (discovery #11)

| App shape | What the review must cover |
|---|---|
| ORM (Hibernate/JPA, Django, Rails, Sequelize, Prisma, SQLAlchemy) | dialect switch + driver; native/raw queries; schema-migration files (Flyway/Liquibase/Alembic) re-authored for the target; boolean/enum/UUID/JSON type mappings; identifier case in `@Table`/`@Column`; generated-ID strategy (`IDENTITY` vs `SEQUENCE`). |
| Raw SQL / stored procedures | every statement: §8 checklist; procedural code rewrite (§4.6). |
| Reports / BI / ETL reading the DB | their SQL is application SQL too — inventory them with the client list (Phase 7.5). |

If the app cannot be modified at all, stop and say so at GATE 2: this pair has no
compatibility layer comparable to Babelfish (which is SQL Server/T-SQL only).

### 1.3 Schema conversion tooling

| Tool | Directions | Limits [as of 2026-10] |
|---|---|---|
| **DMS Schema Conversion** (managed; AWS-recommended over SCT) | MySQL → Aurora PG / RDS PG; PG → Aurora MySQL / RDS MySQL | Sources: **MySQL 5.5–8.0 only (no 8.4)**, PostgreSQL 9.2+. Targets: Aurora PG 14–17, RDS PG 14–18, **Aurora MySQL 8.0.32, RDS MySQL 8.0.23** (no 8.4). No generative-AI conversion for either pair. |
| **AWS SCT** (desktop) | MySQL 5.5+ → Aurora PG / PG; PG 9.1+ → Aurora MySQL / MySQL | PG → MySQL target needs `lower_case_table_names=1`, `log_bin_trust_function_creators=1` when binlog is on, and installs extension-pack schemas `AWS_POSTGRESQL_EXT` / `AWS_POSTGRESQL_EXT_DATA`. MySQL → PG target needs `CREATE ON DATABASE`; search_path `"$user", public_synonyms, public`. |
| **Manual DDL** (from §2/§3 inventory + §4 tables) | both | Always for the long tail; required for an 8.4 source (DMS SC doesn't read 8.4). |
| **pgloader** (MySQL → PG only) | creates tables/indexes/FKs/defaults while loading | §11; not a reviewed DDL artefact — prefer pre-created DDL for production. |

A per-pair catalogue of DMS SC/SCT action items is not published (unverified); expect items
for procedures/triggers/functions, unsupported types and extension-pack emulation, and
budget manual review for each.

### 1.4 Data movement by downtime (details: `method-selection.md` H-rows)

| Downtime | MySQL → PostgreSQL | PostgreSQL → MySQL |
|---|---|---|
| Hours, small DB | **pgloader** one-shot (H1) or DMS full load | DMS full load (H4); MySQL Workbench Migration Wizard is an offline alternative (schema + data) |
| Hours, any size | **DMS full load** into pre-created schema (H2) | **DMS full load** (H4) |
| Minutes / near-zero | **DMS full load + CDC** (H3) | **DMS full load + CDC** (H5) |

No OSS CDC tool for PG → MySQL was found (absence claim, unverified); pgloader has no CDC
and cannot write to MySQL.

## 2. Read-only assessment — MySQL source (MySQL → PostgreSQL)

All read-only; run through the Phase 2 access path with credentials per
`source-assessment.md` §Credential Handling. Every query below was executed against MySQL
8.0 *[live]*. Replace the schema filter with the in-scope schemas.

```sql
-- A1. Columns whose type maps badly (each row = a decision for the target DDL)
SELECT table_schema, table_name, column_name, column_type,
  CASE
    WHEN data_type = 'tinyint' AND column_type LIKE 'tinyint(1)%' THEN 'TINYINT(1)/BOOL: BOOLEAN or SMALLINT? (DMS default SMALLINT)'
    WHEN data_type = 'bigint' AND column_type LIKE '%unsigned%' THEN 'BIGINT UNSIGNED: NUMERIC(20) (DMS default BIGINT overflows > 2^63-1)'
    WHEN column_type LIKE '%unsigned%' THEN 'UNSIGNED: next wider signed type (+ CHECK >= 0)'
    WHEN data_type IN ('enum', 'set') THEN 'ENUM/SET: CREATE TYPE / CHECK / text[] (DMS default VARCHAR)'
    WHEN data_type = 'json' THEN 'JSON: JSONB (DMS default TEXT)'
    WHEN data_type = 'timestamp' THEN 'TIMESTAMP: TIMESTAMPTZ (stored UTC; DMS converts to UTC)'
    WHEN data_type = 'datetime' THEN 'DATETIME: TIMESTAMP (naive) - decide the zone semantics'
    WHEN data_type = 'time' THEN 'TIME: TIME, or INTERVAL if values exceed 24h / are negative (DMS default VARCHAR)'
    WHEN data_type = 'year' THEN 'YEAR: SMALLINT'
    WHEN data_type = 'bit' THEN 'BIT(n): BIT(n) or BOOLEAN for BIT(1)'
    WHEN data_type IN ('float', 'double') THEN 'approximate numeric - compare at fixed digits'
    WHEN data_type IN ('geometry','point','linestring','polygon','multipoint','multilinestring','multipolygon','geometrycollection','geomcollection') THEN 'spatial: PostGIS, manual'
    WHEN data_type IN ('mediumblob','longblob','mediumtext','longtext') THEN 'DMS LOB (BLOB/NCLOB): LOB mode decides truncation; no PK/UK -> A10 blocker'
    WHEN data_type IN ('tinyblob','blob','tinytext','text') THEN 'DMS BYTES/WSTRING (not a LOB): size the target column; no PK/UK -> A10 check'
  END AS concern
FROM information_schema.columns
WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
HAVING concern IS NOT NULL
ORDER BY table_schema, table_name, column_name;

-- A2. Zero / partial-zero dates (legacy data written under a lax sql_mode). Generates one
--     COUNT per date column; run the generated statements. pgloader writes NULL; DMS
--     wrote '0101-01-01' on Aurora PostgreSQL 17 in a live run (§4.2 zero dates).
SELECT CONCAT('SELECT ''', table_schema, '.', table_name, '.', column_name, ''' AS col, COUNT(*) AS zero_or_partial_dates FROM `',
              table_schema, '`.`', table_name, '` WHERE `', column_name, '` IS NOT NULL AND (MONTH(`', column_name,
              '`) = 0 OR DAYOFMONTH(`', column_name, '`) = 0);') AS stmt
FROM information_schema.columns
WHERE data_type IN ('date', 'datetime', 'timestamp')
  AND table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema');

-- A3. Server semantics the target must reproduce or the app must stop relying on
SELECT @@GLOBAL.sql_mode, @@lower_case_table_names, @@character_set_server, @@collation_server,
       @@GLOBAL.time_zone, @@system_time_zone, @@explicit_defaults_for_timestamp, @@transaction_isolation, @@version;

-- A4. Unique keys under a case/accent-INSENSITIVE collation: enforced in MySQL, NOT in a
--     deterministic PostgreSQL collation ('a@x.com' and 'A@x.com' become legal duplicates)
SELECT s.table_schema, s.table_name, s.index_name,
       GROUP_CONCAT(s.column_name ORDER BY s.seq_in_index) AS cols, GROUP_CONCAT(DISTINCT c.collation_name) AS collations
FROM information_schema.statistics s
JOIN information_schema.columns c
  ON c.table_schema = s.table_schema AND c.table_name = s.table_name AND c.column_name = s.column_name
WHERE s.non_unique = 0 AND c.collation_name IS NOT NULL AND c.collation_name NOT LIKE '%\_bin'
  AND s.table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
GROUP BY s.table_schema, s.table_name, s.index_name;

-- A5. Tables without a primary key (DMS CDC/validation/batch apply need one; the reverse
--     task from PostgreSQL needs REPLICA IDENTITY FULL for them — §10.1). Any of them with
--     a LOB-class column is a GATE 2 blocker: A10.
SELECT t.table_schema, t.table_name, t.engine, t.table_rows
FROM information_schema.tables t
LEFT JOIN information_schema.table_constraints k
  ON k.table_schema = t.table_schema AND k.table_name = t.table_name AND k.constraint_type = 'PRIMARY KEY'
WHERE t.table_type = 'BASE TABLE' AND k.constraint_name IS NULL
  AND t.table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema');

-- A6. Generated columns (DMS does not capture their changes), FULLTEXT/SPATIAL and prefix
--     indexes (DMS does not create prefix indexes; PG has no prefix index -> expression index)
SELECT table_schema, table_name, column_name, extra, generation_expression
FROM information_schema.columns
WHERE extra LIKE '%GENERATED%' AND table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema');
SELECT table_schema, table_name, index_name, index_type, column_name, sub_part
FROM information_schema.statistics
WHERE (index_type IN ('FULLTEXT', 'SPATIAL') OR sub_part IS NOT NULL)
  AND table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema');

-- A7. Code and objects DMS never carries: routines, triggers, events, views,
--     AUTO_INCREMENT columns (identity/sequence on PG), cascading FKs, CHECKs
SELECT 'routine' AS kind, routine_schema AS sch, routine_name AS name, routine_type AS detail FROM information_schema.routines
 WHERE routine_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION ALL SELECT 'trigger', trigger_schema, trigger_name, CONCAT(action_timing, ' ', event_manipulation, ' ON ', event_object_table)
 FROM information_schema.triggers WHERE trigger_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION ALL SELECT 'event', event_schema, event_name, status FROM information_schema.events
 WHERE event_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION ALL SELECT 'view', table_schema, table_name, is_updatable FROM information_schema.views
 WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION ALL SELECT 'auto_increment', table_schema, CONCAT(table_name, '.', column_name), column_type FROM information_schema.columns
 WHERE extra LIKE '%auto_increment%' AND table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION ALL SELECT 'fk_cascade', constraint_schema, CONCAT(table_name, '.', constraint_name), CONCAT('ON UPDATE ', update_rule, ' ON DELETE ', delete_rule)
 FROM information_schema.referential_constraints WHERE update_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT') OR delete_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT')
UNION ALL SELECT 'check', constraint_schema, CONCAT(table_name, '.', constraint_name), enforced
 FROM information_schema.table_constraints WHERE constraint_type = 'CHECK' AND constraint_schema NOT IN ('mysql', 'sys');

-- A8. Identifiers: > 63 bytes (PostgreSQL silently truncates), mixed case (PG folds unquoted
--     names to lower case), and the full identifier list to intersect with PG reserved words
SELECT table_schema, table_name, column_name, LENGTH(column_name) AS name_bytes
FROM information_schema.columns
WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
  AND (LENGTH(column_name) > 63 OR LENGTH(table_name) > 63
       OR CAST(column_name AS BINARY) <> CAST(LOWER(column_name) AS BINARY)
       OR CAST(table_name AS BINARY) <> CAST(LOWER(table_name) AS BINARY));
SELECT DISTINCT LOWER(column_name) AS identifier FROM information_schema.columns
WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
UNION SELECT DISTINCT LOWER(table_name) FROM information_schema.tables
WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema');
--     ...then on the PostgreSQL target (or any PG of the target major):
--     SELECT word FROM pg_get_keywords() WHERE catcode IN ('R', 'T');   -- reserved words

-- A9. Engines/charsets per schema, and CDC readiness of the binlog
SELECT table_schema, engine, table_collation, COUNT(*) AS tables, ROUND(SUM(data_length + index_length) / 1024 / 1024) AS mb
FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
GROUP BY table_schema, engine, table_collation;
SHOW GLOBAL VARIABLES WHERE Variable_name IN ('log_bin', 'binlog_format', 'binlog_row_image', 'binlog_expire_logs_seconds',
  'server_id', 'gtid_mode', 'binlog_transaction_compression', 'log_replica_updates', 'log_slave_updates', 'require_secure_transport');

-- A10. LOB-class columns on a table with no PRIMARY KEY and no UNIQUE key — DMS removes LOB
--      columns of such tables (§4.7). dms_lob = 1: a DMS LOB type (JSON = CLOB, MEDIUM/
--      LONGTEXT = NCLOB, MEDIUM/LONGBLOB and spatial = BLOB) -> GATE 2 BLOCKER for every DMS
--      path, both directions. 0: TEXT/BLOB/TINY* (the DMS doc maps them to WSTRING/BYTES) — a
--      conditional check, proven by the §4.7 post-load non-null check.
SELECT c.table_schema, c.table_name,
       GROUP_CONCAT(CONCAT(c.column_name, ' ', c.column_type) ORDER BY c.ordinal_position SEPARATOR ', ') AS lob_columns,
       MAX(c.data_type IN ('json', 'mediumtext', 'longtext', 'mediumblob', 'longblob', 'geometry', 'point', 'linestring',
           'polygon', 'multipoint', 'multilinestring', 'multipolygon', 'geometrycollection', 'geomcollection')) AS dms_lob
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
WHERE c.data_type IN ('json', 'tinytext', 'text', 'mediumtext', 'longtext', 'tinyblob', 'blob', 'mediumblob', 'longblob',
        'geometry', 'point', 'linestring', 'polygon', 'multipoint', 'multilinestring', 'multipolygon', 'geometrycollection', 'geomcollection')
  AND c.table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
  AND NOT EXISTS (SELECT 1 FROM information_schema.table_constraints k
                  WHERE k.table_schema = c.table_schema AND k.table_name = c.table_name
                    AND k.constraint_type IN ('PRIMARY KEY', 'UNIQUE'))
GROUP BY c.table_schema, c.table_name
ORDER BY c.table_schema, c.table_name;
```

Record per finding: object, decision (target type / rewrite / accept), owner. A4 rows need
an explicit choice: `citext`, a unique index on `lower(col)`, or a nondeterministic ICU
collation (PG 12+; `LIKE` on nondeterministic collations only from PG 18; slower, no B-tree
deduplication). A7 `fk_cascade` rows: DMS does not replicate cascade effects from a MySQL
source (InnoDB writes no binlog events for cascaded child changes *[live]*), **and** the PG
target's own FK action does not fire for DMS either — the DMS session runs with
`session_replication_role = replica` (§5), which skips FK triggers *[live]*. So a parent
DELETE/UPDATE on MySQL leaves orphan (or stale-key) children on PG, in the forward task and in
any MySQL → PG reverse task (§10). Choose per FK at GATE 2 — **prefer (a)**: before the
load, replace the MySQL cascade by explicit, binlogged behaviour (app-side child deletes, or a
`BEFORE DELETE` trigger + `RESTRICT` FK — trigger-written rows *are* in the ROW binlog
*[live]*; §10 recipe); or (b) accept it and **reconcile every child table reachable through a
cascade (§10 R2) with a keyed normalized compare against MySQL** after the final drain, before
GATE 3, and again at the freeze — the R1 orphan count is only a quick detector (§10). A10 rows with `dms_lob = 1` are
**GATE 2 blockers** for a DMS method (§4.7 remedy menu) — not a REPLICA IDENTITY question;
`dms_lob = 0` rows are conditional checks (§4.7).

## 3. Read-only assessment — PostgreSQL source (PostgreSQL → MySQL)

Executed against PostgreSQL 16 *[live]*.

```sql
-- P1. Columns whose type maps badly
SELECT * FROM (
  SELECT n.nspname AS sch, c.relname AS tbl, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS type,
    CASE
      WHEN t.typname = 'bool' THEN 'BOOLEAN: TINYINT(1) (DMS default VARCHAR(5) true/false)'
      WHEN t.typname = 'uuid' THEN 'UUID: CHAR(36) (or BINARY(16) + UUID_TO_BIN)'
      WHEN t.typname IN ('json', 'jsonb') THEN 'JSON/JSONB: JSON (DMS default LONGTEXT); rewrite jsonb operators/GIN'
      WHEN t.typcategory = 'A' THEN 'ARRAY: JSON or a child table (DMS default TEXT holding {a,b})'
      WHEN t.typname = 'numeric' AND a.atttypmod = -1 THEN 'unbounded NUMERIC: DMS narrows it to NUMERIC(28,6) AT THE SOURCE - declare NUMERIC(p,s) on the source or accept the loss (4.3)'
      WHEN t.typname = 'numeric' AND ((a.atttypmod - 4) >> 16) > 65 THEN 'NUMERIC precision > 65: exceeds MySQL DECIMAL'
      WHEN t.typname = 'timestamptz' THEN 'TIMESTAMPTZ: DATETIME(6) holding UTC, or TIMESTAMP(6) (range ends 2038)'
      WHEN t.typname = 'timetz' THEN 'TIMETZ: no MySQL equivalent'
      WHEN t.typname = 'interval' THEN 'INTERVAL: no MySQL equivalent (seconds BIGINT / VARCHAR)'
      WHEN t.typname = 'citext' THEN 'CITEXT: VARCHAR with a _ci collation'
      WHEN t.typname = 'text' THEN 'TEXT: TEXT/LONGTEXT, or VARCHAR(n) when indexed/unique (3072-byte key limit)'
      WHEN t.typname = 'bytea' THEN 'BYTEA: LONGBLOB'
      WHEN t.typtype = 'e' THEN 'ENUM type: inline ENUM'
      WHEN t.typtype IN ('c', 'd', 'r', 'm') THEN 'composite/domain/range: redesign'
      WHEN t.typname IN ('inet','cidr','macaddr','macaddr8','money','xml','tsvector','tsquery','hstore','point','line','lseg','box','path','polygon','circle')
        THEN 'no MySQL equivalent: redesign'
    END AS concern
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_type t ON t.oid = a.atttypid
  WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
    AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
) x WHERE concern IS NOT NULL ORDER BY sch, tbl, col;

-- P2. Tables without a primary key, with their REPLICA IDENTITY (d=default, f=full,
--     n=nothing, i=index). Without a PK, DMS IGNORES their UPDATE/DELETE unless
--     REPLICA IDENTITY FULL (test_decoding only; not with pglogical) — an A2 source change.
--     REPLICA IDENTITY does NOT save their LOB columns: P9.
SELECT n.nspname, c.relname, c.relreplident, c.reltuples::bigint AS approx_rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
  AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary);

-- P3. Sequences and identity columns (MySQL has AUTO_INCREMENT only — standalone
--     sequences used through nextval() need an application/table redesign)
SELECT s.schemaname, s.sequencename, s.last_value, d.refobjid::regclass AS owner_table, a.attname AS owner_column,
       a.attidentity AS identity
FROM pg_sequences s
JOIN pg_namespace sn ON sn.nspname = s.schemaname
JOIN pg_class sc ON sc.relname = s.sequencename AND sc.relnamespace = sn.oid
LEFT JOIN pg_depend d ON d.objid = sc.oid AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
LEFT JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid;

-- P4. PostgreSQL-only index/constraint features: partial, expression, GIN/GiST/BRIN/hash,
--     NULLS NOT DISTINCT (PG 15+), exclusion constraints, CHECKs, deferrable constraints
SELECT n.nspname, c.relname AS tbl, i.relname AS idx, am.amname, ix.indisunique,
       ix.indpred IS NOT NULL AS partial, ix.indexprs IS NOT NULL AS expression, pg_get_indexdef(ix.indexrelid) AS def
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class c ON c.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
  AND (ix.indpred IS NOT NULL OR ix.indexprs IS NOT NULL OR am.amname <> 'btree'
       OR pg_get_indexdef(ix.indexrelid) LIKE '%NULLS NOT DISTINCT%');
SELECT conrelid::regclass AS tbl, conname, contype, condeferrable, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE (contype IN ('c', 'x') OR condeferrable)
  AND connamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog', 'information_schema'));

-- P5. Code and objects: functions/procedures (language), row vs statement triggers, views,
--     materialized views (unsupported by DMS), rules, extensions, extra schemas
--     (a MySQL "schema" is a database: each PG schema becomes a MySQL database)
SELECT 'function' AS kind, n.nspname AS sch, p.proname AS name, l.lanname AS detail
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
UNION ALL SELECT 'trigger', n.nspname, c.relname || '.' || t.tgname, CASE WHEN t.tgtype & 1 = 1 THEN 'ROW' ELSE 'STATEMENT' END
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal
UNION ALL SELECT 'view', schemaname, viewname, '' FROM pg_views WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
UNION ALL SELECT 'matview', schemaname, matviewname, '' FROM pg_matviews
UNION ALL SELECT 'rule', schemaname, tablename || '.' || rulename, '' FROM pg_rules WHERE schemaname NOT IN ('pg_catalog')
UNION ALL SELECT 'extension', '', extname, extversion FROM pg_extension WHERE extname <> 'plpgsql'
UNION ALL SELECT 'schema', nspname, '', '' FROM pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'public') AND nspname NOT LIKE 'pg\_%';

-- P6. Identifiers: quoted mixed-case names, names that collide once folded to lower case
--     (lower_case_table_names=1 on the target), and the list to intersect with MySQL
--     reserved words (on the MySQL target: SELECT word FROM information_schema.keywords WHERE reserved = 1)
SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm') AND c.relname <> lower(c.relname)
  AND n.nspname NOT IN ('pg_catalog', 'information_schema');
SELECT n.nspname, lower(c.relname) AS folded, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY 1, 2 HAVING count(*) > 1;
SELECT DISTINCT lower(a.attname) AS identifier FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND n.nspname NOT IN ('pg_catalog', 'information_schema');

-- P7. Values in case-SENSITIVE unique text columns that WILL collide under a MySQL _ci
--     collation (the load fails with a duplicate key). Generates one query per single-column
--     unique text index; accent-only differences also collide under *_ai_ci (not caught here).
SELECT format('SELECT %L AS unique_index, lower(%I) AS folded, count(*) FROM %s GROUP BY 2 HAVING count(*) > 1;',
              i.relname, a.attname, c.oid::regclass) AS stmt
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class c ON c.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ix.indkey[0]
JOIN pg_type t ON t.oid = a.atttypid
WHERE ix.indisunique AND ix.indnatts = 1 AND t.typcategory = 'S'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema');

-- P8. CDC readiness, existing slots (each pins WAL), encoding/locale
SELECT name, setting FROM pg_settings
WHERE name IN ('wal_level', 'max_replication_slots', 'max_wal_senders', 'wal_sender_timeout', 'max_slot_wal_keep_size',
               'shared_preload_libraries', 'rds.logical_replication', 'TimeZone', 'default_transaction_isolation',
               'standard_conforming_strings');
SELECT slot_name, plugin, slot_type, active, restart_lsn, confirmed_flush_lsn FROM pg_replication_slots;
SELECT datname, pg_encoding_to_char(encoding) AS encoding, datcollate, datctype, datlocprovider
FROM pg_database WHERE datname = current_database();

-- P9. LOB-class columns on a table with no PRIMARY KEY and no (non-partial) UNIQUE index —
--     DMS removes LOB columns of such tables (§4.7). dms_lob = true: a DMS LOB type per the
--     PostgreSQL-source mapping (TEXT, CITEXT, JSON/JSONB, arrays, HSTORE, composite = NCLOB;
--     XML, TSVECTOR/TSQUERY, PATH/POLYGON = CLOB; BYTEA = BLOB) -> GATE 2 BLOCKER for every DMS
--     path. false: VARCHAR without length or > 16387 — a conditional check (LOB only if the
--     endpoint sets MapLongVarcharAs to clob/nclob; prove with the §4.7 post-load check).
SELECT n.nspname AS sch, c.relname AS tbl,
       string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) AS lob_columns,
       c.reltuples::bigint AS approx_rows, bool_or(t.typname <> 'varchar') AS dms_lob
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
  AND (t.typname IN ('text', 'citext', 'bytea', 'json', 'jsonb', 'xml', 'hstore', 'tsvector', 'tsquery', 'path', 'polygon')
       OR t.typcategory = 'A' OR t.typtype = 'c'
       OR (t.typname = 'varchar' AND (a.atttypmod = -1 OR a.atttypmod - 4 > 16387)))
  AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid
                  AND (i.indisprimary OR (i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL)))
GROUP BY n.nspname, c.relname, c.reltuples
ORDER BY 1, 2;
```

P7 collisions are a hard blocker for a `_ci` target column: choose `utf8mb4_bin` (or
`utf8mb4_0900_as_cs`) for that column, or resolve the duplicates with the data owner. A
`citext` source column is the opposite case: give it a `_ci` collation on MySQL. P9 rows
with `dms_lob = true` are **GATE 2 blockers** for a DMS method (§4.7; `false` rows are checks) — `REPLICA IDENTITY FULL` (A2) fixes
UPDATE/DELETE capture for a key-less table, not its LOB columns. P4 partial unique indexes:
§4.3 recipe.

## 4. Schema conversion

### 4.1 Approach

1. Convert with DMS Schema Conversion (via the `dms-schema-conversion` skill) or SCT where
   §1.3 allows; otherwise write DDL by hand from the §2/§3 inventory.
2. Review every column against the tables below; the converted DDL is a **reviewed
   artefact** in the CDK project (`scripts/`), not something DMS or a tool decides at load.
3. Create tables with **primary keys only**; load; then secondary indexes (before CDC starts —
   DMS guidance), then FKs/CHECKs/triggers (§4.5).

### 4.2 Type mapping — MySQL → PostgreSQL

| MySQL | DMS default on PG (DMS type) | Recommended PG type | Why / fix |
|---|---|---|---|
| `TINYINT(1)` / `BOOL` | `SMALLINT` (INT1) | `BOOLEAN` — or keep `SMALLINT` if the app compares to 0/1 | `WHERE flag = true` breaks on SMALLINT; a TINYINT(1) holding `2` is not boolean — check A1 data first |
| `BIGINT UNSIGNED` | `BIGINT` (UINT8) — **overflow above 2^63−1** | `NUMERIC(20)` (+ `CHECK >= 0`) | rows above the signed range fail to load |
| `INT UNSIGNED` / smaller unsigned | wider signed (UINT4 → `BIGINT`) | same, + `CHECK >= 0` if needed | OK |
| `DECIMAL(p,s)` | `NUMERIC(p,s)` | same | OK |
| `FLOAT` / `DOUBLE` | `REAL` / `DOUBLE PRECISION` | same | values outside ±2.23E-308..1.79E+308 map to STRING |
| `DATETIME[(fsp)]` | `TIMESTAMP` (scale 0–6; 7–9 → VARCHAR(37)), **no TZ conversion** | `TIMESTAMP(fsp)` (naive) — or `TIMESTAMPTZ` only with an agreed zone | decide: naive wall-clock values or instants in a known zone |
| `TIMESTAMP[(fsp)]` | `TIMESTAMP`, **values converted to UTC** | `TIMESTAMPTZ(fsp)` | MySQL TIMESTAMP is a UTC instant; range 1970–2038 |
| `DATE` | `DATE` | `DATE` | zero dates: **not** NULL under DMS — see below (A2) |
| `TIME` | `VARCHAR` (STRING) | `TIME`, or `INTERVAL` for > 24 h / negative | MySQL TIME spans ±838 h |
| `YEAR` | `SMALLINT` (INT2) | `SMALLINT` | |
| `CHAR/VARCHAR/TEXT` | `VARCHAR`/`TEXT` (DMS: `TEXT`/`TINYTEXT` = WSTRING; `MEDIUMTEXT`/`LONGTEXT` = NCLOB, i.e. LOB) | `VARCHAR(n)`/`TEXT`; `CITEXT` or `lower()` unique index for ci-unique columns (A4) | PG compares case-sensitively by default |
| `ENUM` | `VARCHAR` (WSTRING) — constraint lost | `CREATE TYPE … AS ENUM` or `TEXT` + `CHECK` | |
| `SET` | `VARCHAR` — constraint lost | `TEXT[]` + `CHECK (col <@ ARRAY[...])` or a child table | no SET type in PG |
| `JSON` | `TEXT` (CLOB) | `JSONB` (or `JSON` to keep text form) | JSONB normalizes key order/whitespace/duplicates |
| `BINARY/VARBINARY/TINYBLOB/BLOB` | `BYTEA` (DMS BYTES — not a LOB) | `BYTEA` | `MEDIUMBLOB`/`LONGBLOB` are DMS BLOB: LOB mode and the §4.7 key rule apply |
| `BIT(1)` / `BIT(n)` | `BOOLEAN` / `BYTEA` | `BOOLEAN` / `BIT(n)` | |
| `GEOMETRY`/`POINT`… | `BYTEA` | PostGIS `geometry` (manual) | DMS spatial = homogeneous only |
| `AUTO_INCREMENT` | attribute dropped | `GENERATED BY DEFAULT AS IDENTITY` — never `ALWAYS` on a table DMS writes (forward target or reverse source) | DMS apply INSERTs carry explicit ids; `ALWAYS` rejects them unless `OVERRIDING SYSTEM VALUE`; reset after load (§4.5) |

**Zero dates under DMS — observed behaviour contradicts the DMS doc.** The DMS MySQL-source
page says zero `DATETIME`/`TIMESTAMP` values "are recorded as null on the target" when the
target can't hold them. In a live run (DMS 3.6.1 → Aurora PostgreSQL 17.11, pre-created
`timestamptz`/`date` columns, `DO_NOTHING`) DMS wrote **`0101-01-01 00:00:00`** for all
1,950 zero values; DMS validation then reported them as mismatches and only the normalized
compare named the cause. Verify on your DMS/target version in the rehearsal; plan for
`0101-01-01` either way:
1. **Preferred — clean the source under A2 before the full load** (`UPDATE … SET col = NULL
   WHERE col = '0000-00-00 …'` on nullable columns; the customer accepts NULL = "not set" at
   GATE 2; the app must stop writing zero dates — strict `sql_mode` with `NO_ZERO_DATE`).
2. **Otherwise — a per-key target UPDATE after the full load and again after the final drain
   at the freeze**: only the keys whose **source** value is a zero date (list them on the
   source with the A2 predicate: `SELECT <pk> FROM t WHERE MONTH(col) = 0 OR DAYOFMONTH(col) = 0`, or take the `keys` of the
   compare tool's paired findings) — `UPDATE t SET col = NULL WHERE <pk> IN (…) AND col =
   '0101-01-01'`. Never a blanket `WHERE col = '0101-01-01'`: a genuine `0101-01-01` value would
   be erased. Then **re-validate right before cutover**. One post-load UPDATE is not enough: any
   CDC UPDATE to a row whose source column is still zero re-applies the full after-image and
   brings `0101-01-01` back.
3. `cross_engine_compare.py` reports a target `0101-01-01` against a source zero date **on the
   same key** as its own finding, with the keys — never as equal to NULL; unpaired observations
   (key-less tables) are "inconclusive", never a finding (§9). Reverse DMS writes NULL back, not
   a zero date.

### 4.3 Type mapping — PostgreSQL → MySQL

| PostgreSQL | DMS default on MySQL (DMS type) | Recommended MySQL type | Why / fix |
|---|---|---|---|
| `BOOLEAN` | `VARCHAR(5)` holding `'true'`/`'false'` (CHAR(5)) | `TINYINT(1)` + `change-data-type` → **`boolean`** (proven live) — **never `int1`** | MySQL has no BOOLEAN; DMS says use a transformation rule; an `int1` rule converted every `'true'` to **0** in a live run (§4.4); `MapBooleanAsBoolean` is a PostgreSQL→Redshift setting and does not help a MySQL target |
| `SMALLINT/INTEGER/BIGINT` | same | same | |
| `NUMERIC(p,s)` (p ≤ 65) | `DECIMAL(p,s)` | same | |
| `NUMERIC` (no p,s) | **`NUMERIC(28,6)` on the DMS source side — truncation** (e.g. 0.611111104488373 → 0.611111) | a wider target `DECIMAL` alone **does not help** — see the note below | fix it on the source, or accept the loss with evidence |
| `MONEY` | `DECIMAL(38,4)` | `DECIMAL(19,4)` | |
| `REAL`/`DOUBLE PRECISION` | `FLOAT`/`DOUBLE` | same | |
| `TEXT` / `CITEXT` | `TEXT`, or `LONGTEXT` with **ucs2** charset (> 64K or full LOB) | `TEXT`/`LONGTEXT` utf8mb4; `VARCHAR(n)` when indexed; `CITEXT` → `_ci` collation | ucs2 cannot hold 4-byte characters (inference) — pre-create utf8mb4 |
| `VARCHAR(n)` | `VARCHAR(n)` | same; unique index ≤ 3072 bytes (DYNAMIC) | `VARCHAR(1000)` utf8mb4 unique fails |
| `UUID` | `VARCHAR` | `CHAR(36)` (or `BINARY(16)`) | **reverse task (MySQL → PG `uuid`)**: `change-data-type` → `string` length 36 on every uuid column, or the values arrive garbled and the rows are not applied (§10.2, live) |
| `JSON`/`JSONB` | `TEXT`/`LONGTEXT` (NCLOB) | `JSON` | jsonb operators/GIN → generated columns + indexes |
| `ARRAY` | `TEXT` holding the PG literal `{a,b}` | `JSON` array, or a child table | the literal is not JSON *[live]* |
| `HSTORE`/composite | `TEXT` | `JSON` / redesign | |
| `TIMESTAMP` | `DATETIME` | `DATETIME(6)` | |
| `TIMESTAMPTZ` | `DATETIME` — **offset lost** | `DATETIME(6)` holding UTC (app reads as UTC), or `TIMESTAMP(6)` (2038 limit) | |
| `TIME`/`TIMETZ` | `TIME` (TZ lost; fractional seconds of TIME not replicated) | `TIME(6)`; for `TIMETZ` decide the conversion (store UTC wall time, or accept the offset loss) | the compare tool returns NEEDS_REVIEW for TIMETZ ↔ TIME until `timetz_policy` (`utc` / `wallclock`) records that decision |
| `INTERVAL` | `VARCHAR(128)` | `BIGINT` seconds / `VARCHAR` + app change | |
| `ENUM` type | `VARCHAR` | inline `ENUM(...)` | |
| `INET/CIDR/MACADDR` | `VARCHAR` | `VARCHAR(43)` / `VARBINARY(16)` + `INET6_ATON` | |
| `BYTEA` | `BLOB`/`LONGBLOB` | `LONGBLOB` | |
| identity / `SERIAL` | attribute dropped | `AUTO_INCREMENT` | reset after load |
| PostGIS | not supported (homogeneous only) | MySQL spatial (manual) | |

**Unbounded PostgreSQL `NUMERIC` → MySQL — the narrowing happens on the DMS *source*.** The DMS
PostgreSQL-source page: "When transferring data that is a `NUMERIC` data type but without
precision and scale, DMS uses `NUMERIC(28,6)`" — the value is already cut when it reaches the
target, so a wider target `DECIMAL` cannot recover digits. The only documented escape,
`MapUnboundedNumericAsString`, is for **PostgreSQL-compatible targets only** ("Do not use … with
non-PostgreSQL targets"), and even there restricts precision to 28 during CDC. No documented
endpoint setting, task setting or table-mapping rule preserves it for a MySQL target (a
`change-data-type` rule acts on DMS's already-typed value; its effect here is undocumented).
So, per column found by P1:
1. **Fix the source type (preferred):** measure `SELECT max(scale(col)), max(length(trunc(abs(col))::text)) FROM t`,
   then `ALTER TABLE t ALTER COLUMN col TYPE numeric(p,s)` with p ≤ 38 (DMS maps NUMERIC(p,s),
   0 < p < 39, as NUMERIC; p ≥ 39 becomes STRING) — an A2 source change that rewrites the table
   under an `ACCESS EXCLUSIVE` lock (schedule it), and the app must then write values that fit.
2. **Or accept the loss explicitly:** record the measured max scale/digits, the affected rows
   (`WHERE col <> round(col, 6)`), and the customer's acceptance (waiver) at GATE 2.
3. Either way **prove it against real DMS** in the rehearsal — full load **and** CDC (an
   UPDATE writing a > 6-decimal value), compared with `cross_engine_compare.py`. This skill's lab
   tests use scripted, DMS-*shaped* loads (`scripts/test_cross_engine_compare.py`); they validate
   the comparison tool, **not** DMS's conversion.

**Fractional seconds (PG → MySQL) — a GATE 2 decision, not a residual risk.** PostgreSQL `now()` /
`CURRENT_TIMESTAMP` carry microseconds and `timestamp`/`timestamptz` keep them (precision up to
6). MySQL's `NOW()` / `CURRENT_TIMESTAMP` / `SYSDATE()` take an optional `fsp` 0–6 and return **no
fractional part without it**, and a `DATETIME`/`TIMESTAMP` column without `(fsp)` has precision
0 ("If omitted, the default precision is 0"); a value with more digits is **rounded** silently
("No warning or error is given when such rounding occurs"; `TIME_TRUNCATE_FRACTIONAL` truncates
instead). Live (OBS-52): after a PG → MySQL cutover, new rows lost µs because the app's SQL
used `NOW()`; the agent accepted it as "app change only". Detect it before GATE 2: grep the app
SQL / ORM defaults for `now()`, `current_timestamp`, `localtimestamp`, `clock_timestamp()`,
`statement_timestamp()` and server-side defaults, and list every PG column with
`timestamp`/`timestamptz`/`time` and a `DEFAULT now()`/`CURRENT_TIMESTAMP`. Present the options at
GATE 2:
1. **Column precision everywhere** — `DATETIME(6)` (or `TIMESTAMP(6)`) for every PG timestamp
   column, so client-supplied values keep µs (this skill's §4.3 mapping).
2. **Column default** for inserts that **omit** the column — `DEFAULT CURRENT_TIMESTAMP(6)` [`ON
   UPDATE CURRENT_TIMESTAMP(6)` where PG had an `updated_at` trigger]; the precision must be the
   same throughout the definition ("the same value must be used throughout the column
   definition").
3. **App SQL** — `NOW(6)` / `CURRENT_TIMESTAMP(6)` where the app passes the function explicitly
   (an explicit `NOW()` is fsp 0 even into a `DATETIME(6)` column *[live]*).
4. **Last resort** — a `BEFORE INSERT` trigger that sets `NEW.col = NOW(6)` for function-valued
   inserts (it cannot tell a deliberate whole-second value from `NOW()` — document the rule it uses).
Choose explicitly, with the customer, which applies to each column (*[live]* proof:
`scripts/test_hetero_recipes.py` `FractionalSeconds`). Reverse direction (MySQL → PG): PG's
precision (µs) covers MySQL's maximum `fsp` 6 — nothing to do beyond mapping `DATETIME(n)` to
`timestamp(n)`/`timestamp`.

**Partial unique index (P4) → generated column + UNIQUE.** MySQL has no partial index. For
`CREATE UNIQUE INDEX … ON invoices (account_id, invoice_no) WHERE NOT is_void`:

```sql
ALTER TABLE invoices
  ADD COLUMN open_invoice_no VARCHAR(32)
      GENERATED ALWAYS AS (CASE WHEN is_void = 0 THEN invoice_no END) STORED,
  ADD UNIQUE KEY uq_invoices_open_no (account_id, open_invoice_no);
```

The generated key must be **NULL for every row outside the predicate** — MySQL UNIQUE allows
any number of NULLs, so many voided invoices may share a number with one open invoice. The
recipe is only as good as the predicate column's values: in a live run the boolean arrived
through an `int1` rule as 0 for every row, every voided invoice claimed the open key, and the
MySQL full-load path (`LOAD DATA LOCAL` = `IGNORE` semantics: duplicates become warnings)
**silently dropped the colliding rows** — the table came up short by exactly the 1,500 voided
invoices, DMS reported 0 error rows, only the count/compare caught it. *Which* rows vanish
depends on load order (the first row per key survives — with void rows loaded first, the real
open invoice is the one lost) *[live]*, so a count proof is not enough. Proof before GATE 3,
per emulated index: source `SELECT count(*) FILTER (WHERE NOT <pred>), count(*) FROM t` equals
the target `SELECT SUM(open_key IS NULL), COUNT(*) FROM t`, the boolean distribution per column
(`SELECT is_void, count(*) … GROUP BY 1` both sides), **and per-key predicate membership**
(the normalized compare over the PK with the predicate column included). Reverse task (MySQL
→ PG): DMS does not capture generated-column changes — add a `remove-column` transformation
for `open_invoice_no`. **Soak:** declare the generated column in the soak config's
`expected_schema_differences` (`{"table": "billing.invoices", "column": "open_invoice_no", "side":
"target", "kind": "extra_column", "reason": "partial-unique-index emulation"}`; Lambda env
`EXPECTED_SCHEMA_DIFFERENCES`) — otherwise every soak interval's schema-drift check is RED (live,
OBS-53); `cross_engine` `ignore_columns` would hide it silently instead of reporting it as
expected, so prefer the declaration (the normalized compare itself only notes an extra target
column as a warning and does not fail on it). Lab proof: `scripts/test_hetero_recipes.py` (rows
outside the predicate, surviving ids under both load orders).

### 4.4 Making DMS load into the converted schema

- **Pre-create, then `DO_NOTHING` or `TRUNCATE_BEFORE_LOAD`.** `TargetTablePrepMode` values:
  `DO_NOTHING` (data and metadata untouched), `DROP_AND_CREATE` (DMS's own DDL — never for
  this pair), `TRUNCATE_BEFORE_LOAD` (data truncated, metadata kept — use for re-runs).
  `dms-best-practices.md`'s generic `DROP_AND_CREATE` example does **not** apply here.
- **Value conversion DMS must do** (e.g. PG `BOOLEAN` → MySQL `TINYINT(1)`): a
  `change-data-type` transformation rule on that column (DMS supports `boolean`, `int1`…`int8`,
  `uint1`…`uint8`, `numeric` with `precision`/`scale`, `string` with `length`, `datetime`,
  `date`, `time`, `bytes`, `blob`, `clob`, `nclob`, …). For a PG boolean (DMS: CHAR(5)
  `'true'`/`'false'`) use **`boolean`** — proven live PG → MySQL (exact distributions after full
  load + CDC), and the same rule carried MySQL `TINYINT(1)` → PG `boolean` (normalized compare MATCH):

  ```json
  {"rule-type": "transformation", "rule-id": "10", "rule-name": "flag-to-boolean",
   "rule-action": "change-data-type", "rule-target": "column",
   "object-locator": {"schema-name": "public", "table-name": "accounts", "column-name": "active"},
   "data-type": {"type": "boolean"}}
  ```

  **Never rely on a `change-data-type` rule without a proof query.** The doc: "AWS DMS can
  apply transformations from one type to another ONLY in supported formats". In live runs an
  `int1` rule turned `'true'` into **0** (941,177 `is_active` rows and every `is_void` — which
  then dropped 1,500 rows, §4.3 partial index), and a `string` rule on a JSON/TEXT column of a
  key-less table was ignored ("the only supported transformation for columns that are mapped to
  BLOB/CLOB data types is to drop the column on the target"; §4.7). Proof = per converted
  column, value distribution source vs target (`GROUP BY col` counts) after the rehearsal full
  load **and** after a CDC UPDATE, before GATE 3. Other limits: one transformation action per
  object; column/table names case-sensitive; no special characters in column names.
- **Identifier case:** PG folds unquoted names to lower case, MySQL table-name case follows
  `lower_case_table_names`; use `convert-lowercase` rules (schema/table/column) so DMS writes
  to the names you pre-created.
- Only one task of a bidirectional pair may be full-load + CDC; the reverse is CDC-only (§10).

### 4.5 Load order, identity/sequence reset

1. Tables with PK only → full load (FK checks off: MySQL target `Initstmt=SET
   FOREIGN_KEY_CHECKS=0` (needs `local_infile=1`); PG target `session_replication_role=replica`
   through `AfterConnectScript`). A MySQL target does not report constraint errors from its
   `LOAD DATA` path — validation catches them.
2. Secondary indexes **before** the CDC phase starts (DMS best practice; CDC on an unindexed
   target scans). Use `"StopTaskCachedChangesNotApplied"`/`"StopTaskCachedChangesApplied"`
   full-load settings to pause at the boundary if you need a window to build them.
3. FKs, CHECKs, then triggers last (`execution-runbooks.md` §Load Order). Never enable target
   triggers while DMS applies — they fire on replicated rows.
4. **Sequences/identity are not carried, and DMS does not move sequences during CDC.** After
   the final drain (cutover step 7), reset:
   - PG target: `SELECT setval(pg_get_serial_sequence('shop.items', 'id'), COALESCE(MAX(id), 1)) FROM shop.items;`
     (identity columns: `ALTER TABLE … ALTER COLUMN id RESTART WITH <max+1>`).
   - MySQL target: `ALTER TABLE accounts AUTO_INCREMENT = <max+1>;`. Verify with `SHOW CREATE
     TABLE` (`AUTO_INCREMENT=<n>` > `MAX(pk)`), or read `information_schema.TABLES.AUTO_INCREMENT`
     only after `SET SESSION information_schema_stats_expiry = 0` — MySQL 8 serves that column from
     a statistics cache ("By default, MySQL retrieves cached values for those columns",
     `information_schema_stats_expiry` default 86400 s), and a live run read a stale value after
     the reseed (OBS-60); `SELECT MAX(id)` plus a test insert proves it **in rehearsal only**
     (`validation-patterns.md` §4).

### 4.6 Procedures, triggers, functions, events, views — rewrite checklist

- [ ] Inventory from A7 / P5 with owners; DMS SC/SCT conversion report attached.
- [ ] MySQL routines (SQL/PSM) ↔ PL/pgSQL: variable declaration, `DECLARE … HANDLER` ↔
      `EXCEPTION` blocks, `SIGNAL` ↔ `RAISE`, cursors, `LAST_INSERT_ID()` ↔ `RETURNING`/`currval`.
- [ ] Triggers: PG triggers call a trigger function; PG has statement-level triggers, MySQL
      only row-level; MySQL allows one trigger per timing/event per table before 5.7.2 (check
      ordering `FOLLOWS/PRECEDES`).
- [ ] MySQL `EVENT`s → `pg_cron` (Aurora/RDS PG extension) or an external scheduler; PG
      `pg_cron` jobs → MySQL events or EventBridge Scheduler.
- [ ] Views: re-create with converted SQL; updatable-view rules differ; materialized views
      (PG) have no MySQL equivalent (table + refresh job).
- [ ] `DEFINER`/`SECURITY DEFINER` and grants recreated per role design (DMS migrates no users).
- [ ] Full-text: MySQL `FULLTEXT`/`MATCH … AGAINST` ↔ PG `tsvector` + GIN — query rewrite,
      ranking differs; validate results, not row counts.
- [ ] Generated columns re-declared (`GENERATED ALWAYS AS (…) STORED` both sides; MySQL VIRTUAL
      has no PG equivalent before PG 18 — verify on the target major).

### 4.7 LOB columns on tables without a PK/UK — GATE 2 blocker (both directions)

DMS: "A table must have a primary key or a unique key for DMS to migrate LOBs" (premigration
assessment `table-with-lob-but-without-primary-key-or-unique-constraint`). It **removes the
column** — task log `Column 'payload' was removed from table definition …: the column data type
is LOB and the table has no primary key or unique index` — and the load still "succeeds". Live,
in limited LOB mode, both directions: MySQL `audit_log.payload` (JSON) all NULL; PostgreSQL
`payment_events.kind/payload` (text/jsonb) empty on all 505k rows in the **production** target,
full load and CDC; the reverse task dropped the same column on rollback. Row counts and DMS
validation (no PK → not validated) cannot see it.

**Confirmed blocker vs conditional check** — decided from the column's *effective* DMS type
under the endpoint settings actually used (DMS source data-type tables):

| Class | MySQL source (A10) | PostgreSQL source (P9) | Action |
|---|---|---|---|
| **Confirmed blocker** | `dms_lob = 1`: JSON (CLOB), MEDIUMTEXT/LONGTEXT (NCLOB), MEDIUMBLOB/LONGBLOB and spatial (BLOB) | `dms_lob = true`: **text and citext too**, json/jsonb, arrays, hstore, composite (NCLOB); xml, tsvector/tsquery, path/polygon (CLOB); bytea (BLOB) — plus `varchar(n > 16387)` / unbounded `varchar` **if** the endpoint sets `MapLongVarcharAs` to `clob`/`nclob` | resolved at GATE 2, before any DMS full load — remedy menu below |
| **Conditional check** | `dms_lob = 0`: TEXT, TINYTEXT (WSTRING), BLOB, TINYBLOB (BYTES) | `dms_lob = false`: unbounded or > 16387 `varchar` under the default `MapLongVarcharAs` (`wstring`) | not a blocker by itself: the rehearsal full load + post-load non-null check (below) must show the column arrived; a drop promotes it to a blocker |

Confirmed blockers — remedy menu:

| Remedy | How | Measured / caveats |
|---|---|---|
| **Surrogate key on the source (A2)** — preferred | MySQL `ALTER TABLE t ADD COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, ALGORITHM=INPLACE, LOCK=SHARED` (the minimum for an auto-increment column: writes to that table wait for the rebuild; `SET SESSION lock_wait_timeout=5`); PG `ALTER TABLE t ADD COLUMN id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` (rewrites the table under `ACCESS EXCLUSIVE`); same key on the target DDL; stop the task → ALTER → reload that table (DMS does not replicate PK definition changes) | 16.4 s for 307k rows on MySQL (longest app write gap 10.6 s), 919.7 ms for 505k rows / 49 MB on PG, both under live writes — measure on the rehearsal clone first. `BY DEFAULT`, not `ALWAYS` (reverse DMS inserts ids). Grep app/trigger code for column-list-less `INSERT … VALUES` first; grant sequence `USAGE` to roles whose triggers insert |
| **Pre-create + backfill at the freeze** | key-less table stays in the task for its scalar columns; after the final drain copy the LOB columns (or the whole table) with a dedicated script inside the write pause | costs window time — measure in rehearsal; only for small or append-only tables |
| **Exclude + separate copy** | exclude the table from DMS; copy history before cutover and the delta at the freeze by a watermark (append-only tables), or leave history on the old system with the customer's written acceptance | no rollback coverage unless the reverse direction is handled the same way |

**Not remedies** (each tried live or ruled out by the doc): `change-data-type` to `string` (the
column is still removed — only "drop" is supported for BLOB/CLOB-mapped columns), `REPLICA
IDENTITY FULL` (UPDATE/DELETE capture only), `MapJsonbAsClob` (JSONB → CLOB instead of NCLOB:
still a LOB), full LOB mode (needs the key too). `MapLongVarcharAs` (default `wstring`) only
decides whether a long `VARCHAR(n > 16387)` becomes a LOB (`clob`/`nclob`) — keep the default
for key-less tables; it does not turn text/jsonb into non-LOBs.

**Mandatory post-full-load quick check — before indexes, validation, rehearsal or anything
else:** for **every LOB-class column, whatever the table's keys are now** — a table repaired
with a surrogate key (the §4.7 remedy) must stay in scope, so the generator below does **not**
filter on current PK/UK; at minimum it must cover every row of the A10/P9 inventory saved at
assessment (confirmed blockers *and* conditional checks: MySQL TEXT/BLOB/TINY*, PG
long/unbounded `varchar`; same type lists) — compare its `col` list against that saved
inventory and add any missing column —
the non-null count source vs target (any target 0 or materially below source = stop; a
conditional-check column that dropped becomes a blocker), plus a grep of the task log for `was
removed from table definition`. Run the generator on the **source**; it emits the matching
source and target statement per column (target identifiers as DMS writes them — lower-case on
PG after `convert-lowercase`; edit the schema if a rule renames it). Run each pair back to back:

```sql
-- MySQL source (A10 types, every table) -> source_stmt on MySQL, target_stmt on PostgreSQL
SELECT CONCAT(c.table_schema, '.', c.table_name, '.', c.column_name) AS col, c.column_type,
       CONCAT('SELECT COUNT(*), COUNT(`', c.column_name, '`) FROM `', c.table_schema, '`.`', c.table_name, '`;') AS source_stmt,
       CONCAT('SELECT count(*), count("', LOWER(c.column_name), '") FROM "', LOWER(c.table_schema), '"."',
              LOWER(c.table_name), '";') AS target_stmt
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
WHERE c.data_type IN ('json', 'tinytext', 'text', 'mediumtext', 'longtext', 'tinyblob', 'blob', 'mediumblob', 'longblob',
        'geometry', 'point', 'linestring', 'polygon', 'multipoint', 'multilinestring', 'multipolygon', 'geometrycollection', 'geomcollection')
  AND c.table_schema IN ('shop')
ORDER BY 1;
-- PostgreSQL source (P9 types, every table) -> source_stmt on PostgreSQL, target_stmt on MySQL
SELECT n.nspname || '.' || c.relname || '.' || a.attname AS col, format_type(a.atttypid, a.atttypmod) AS type,
       format('SELECT count(*), count(%I) FROM %I.%I;', a.attname, n.nspname, c.relname) AS source_stmt,
       format('SELECT COUNT(*), COUNT(`%s`) FROM `%s`.`%s`;', a.attname, n.nspname, c.relname) AS target_stmt
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
  AND n.nspname IN ('shop')
  AND (t.typname IN ('text', 'citext', 'bytea', 'json', 'jsonb', 'xml', 'hstore', 'tsvector', 'tsquery', 'path', 'polygon')
       OR t.typcategory = 'A' OR t.typtype = 'c'
       OR (t.typname = 'varchar' AND (a.atttypmod = -1 OR a.atttypmod - 4 > 16387)))
ORDER BY 1;
```

## 5. DMS execution — MySQL → PostgreSQL

**Source (MySQL) CDC prerequisites** (self-managed): `server_id` ≥ 1, `log-bin` on,
**`binlog_format=ROW`** (restart sessions after changing — it is dynamic per session),
**`binlog_row_image=FULL`**, retention `binlog_expire_logs_seconds` ≥ 86400 (8.4 removed
`expire_logs_days`), `log_replica_updates` when the source is a replica; binlog
transaction compression unsupported; binlogs > 4 GB break tasks; Aurora enhanced binlog
unsupported. Privileges: `REPLICATION CLIENT`, `REPLICATION SLAVE`, `SELECT`; XA in use →
`XA_RECOVER_ADMIN`. DMS 3.5.4+ for a MySQL 8.4 source. Known limits: AUTO_INCREMENT not
migrated, prefix indexes not created, cascade effects not replicated (§2 A7), generated-column
changes not captured, invisible columns not migrated, DROP/RENAME TABLE and
default/nullability/charset DDL not replicated, LOBs disabled → MEDIUM/LONG TEXT/BLOB
columns skipped, **LOB-class columns of key-less tables removed (§4.7)**. Binlog state gate:
`method-selection.md` §Binlog State Gate.

**Endpoints.** Source `engineName: mysql` (or `aurora`), `SslMode` `verify-ca`/`verify-full`
(`require` is unsupported for MySQL endpoints — `dms-best-practices.md` §Endpoint TLS);
Secrets Manager credentials. Target `engineName: aurora-postgresql`/`postgres`,
`SslMode` `verify-full` with the RDS CA, `PostgreSqlSettings.AfterConnectScript:
"SET session_replication_role = replica"` for the full load.

**Task settings (start here, tune in the PoC):**

```json
{
  "TargetMetadata": {"TargetSchema": "", "SupportLobs": true, "FullLobMode": false, "LobChunkSize": 64,
                     "LimitedSizeLobMode": true, "LobMaxSize": 1024, "BatchApplyEnabled": false},
  "FullLoadSettings": {"TargetTablePrepMode": "DO_NOTHING", "CreatePkAfterFullLoad": false,
                       "MaxFullLoadSubTasks": 8, "CommitRate": 10000},
  "ValidationSettings": {"EnableValidation": true, "ThreadCount": 5, "FailureMaxCount": 10000},
  "Logging": {"EnableLogging": true}
}
```

- `LobMaxSize` (KB) at or above the measured maximum of every JSON/TEXT/BLOB column (A1 LOB
  rows, `dms-best-practices.md` §LOB Handling) — or Full LOB mode. Limited LOB mode truncates
  silently unless `FailTasksOnLobTruncation=true`. MySQL `JSON` is a CLOB to DMS.
- `BatchApplyEnabled` (a `TargetMetadata` setting, default `false`) is faster but needs a PK
  or unique key on the source tables, is **not supported for target tables that have both a
  primary key and a unique constraint** (common in a hand-converted schema), works with LOBs
  only in limited LOB mode, and must be `false` if both directions ever run with loopback
  prevention (§10). Leave `TargetSchema` empty for a MySQL target (DMS guidance).
- **DMS data validation** supports MySQL and PostgreSQL as source and target: needs a PK or
  unique index, PK columns not LOB/binary, `VARCHAR` PK < 1024; stops after 10,000
  failures; no views. Hash-based "enhanced" validation (3.5.4) is **not** available for this
  pair. Use `override-validation-function` rules (not on PK columns) when a column is
  deliberately transformed. Collation differences in PK ordering can break validation (the
  doc warns for Oracle → PG; analogous for `_ci` vs deterministic — test). **JSON columns: §9
  "DMS validation and JSON"** — exclude them from DMS validation.

**Common errors:** `Incorrect integer value 'true'` (PG→MySQL boolean without conversion);
`value too long for type character varying` (DMS-created VARCHAR shorter than the multibyte
content); `numeric field overflow` (unsigned BIGINT); duplicate key on a `_ci` target
unique (P7); `ERROR 3159` (TLS `none` against `require_secure_transport=ON`); tasks stuck
in full load on a table without PK and `BatchApply` on.

## 6. DMS execution — PostgreSQL → MySQL

**Source (PostgreSQL) CDC prerequisites** (self-managed): DMS user **superuser** for full
load + CDC (on RDS/Aurora: set up with `rds_superuser`, then the endpoint can use a
least-privilege account with `rds_replication`); `pg_hba.conf` replication entry for the DMS
instance; `wal_level=logical`; `max_replication_slots` ≥ number of tasks (slots survive a
stopped task — drop them); `max_wal_senders` > 1; `wal_sender_timeout` 0 or ≥ 10000 ms
(< 5 min recommended); plugin **`pglogical`** if installed (`shared_preload_libraries`) else
**`test_decoding`** (contrib on self-managed) — override with the endpoint setting `PluginName`,
whose API values are `no-preference` | `test-decoding` | `pglogical` (hyphen; the decoding plugin
itself — and every SQL `pg_create_logical_replication_slot(…, 'test_decoding')` — uses the
underscore). RDS/Aurora PG
source: `rds.logical_replication=1` (static, reboot). DMS 3.5.4+ for PG 17/18. Aurora PG
read replicas cannot be CDC sources.

- **Tables without PK** (P2): UPDATE/DELETE are **ignored** unless `ALTER TABLE …
  REPLICA IDENTITY FULL` (more WAL; works with `test_decoding`, not with pglogical) — an A2
  source change. No PK also disables batch apply, full LOB and validation for that table —
  and DMS **removes its LOB-class columns (text, citext, jsonb, bytea, arrays…) even with
  REPLICA IDENTITY FULL**: GATE 2 blocker, §4.7.
- **Idle-slot WAL:** set `HeartbeatEnable=true` (`HeartbeatFrequency` minutes, default 5;
  `HeartbeatSchema`) so `restart_lsn` keeps moving; alarm on source free storage /
  `ReplicationSlotDiskUsage`. A non-default `max_slot_wal_keep_size` can invalidate a lagging
  slot and fail the task.
- **DDL capture** uses `awsdms_ddl_audit` + the `awsdms_intercept_ddl` event trigger
  (`CaptureDdls`, `DdlArtifactsSchema`) — set `CaptureDdls=false` if DDL is frozen and you
  don't want DMS objects on the source.
- Limits: TRUNCATE captured only PG 11+ with DMS 3.5+; partition ADD/DROP not replicated and
  partitioned tables arrive as plain tables; ARRAY tables need a PK; set/drop DEFAULT/NOT
  NULL and PK changes not replicated; deferred constraints, materialized views and RDS Proxy
  CDC unsupported; long transactions spill to disk beyond 4 MB.

**Endpoints.** Source `engineName: postgres`/`aurora-postgresql`, `PostgreSqlSettings:
{PluginName: "test-decoding", SlotName (only for a native start point), HeartbeatEnable, CaptureDdls}`.
Target `engineName: aurora`/`mysql`, `SslMode verify-full` with the RDS CA (Aurora MySQL 8.4
rejects `none`), `MySqlSettings.AfterConnectScript` / `Initstmt=SET FOREIGN_KEY_CHECKS=0`
for the full load, target `local_infile=1`. Task settings as §5 plus the `change-data-type`
rules for booleans (§4.4).

**MySQL target `sql_mode` — a GATE 2 parameter, set explicitly** (also for a MySQL new primary
that is the reverse source in §10.2). Live (Aurora MySQL 8.4.8): the target's custom
parameter groups left `sql_mode` non-strict, so bad values were silently truncated or written as `''`
instead of failing. Unlike community MySQL, the **RDS/Aurora engine defaults are not strict**
(`describe-engine-default-parameters`, ap-northeast-2, 2026-10-28: `mysql8.0`/`mysql8.4` =
`NO_ENGINE_SUBSTITUTION`; `aurora-mysql8.0`/`8.4` instance-level = `0`; on Aurora `sql_mode` is
an **instance**-level parameter, absent from the cluster parameter group — a cluster-only group
leaves it at the non-strict default; re-check on your version). Set the MySQL
8.0/8.4 community default explicitly in the parameter group
(`cdk-stacks.md`): `ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,
ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION`, and read it back after apply/reboot with
`SELECT @@GLOBAL.sql_mode` (a session inherits the global value at connect — recycle pools).
It is an **application-behaviour decision**: rehearse the converted app SQL and views under
strict mode (`ONLY_FULL_GROUP_BY` rejects loose `GROUP BY`; strict rejects over-long strings,
out-of-range numbers, invalid dates) and record it at GATE 2. What strict mode does and does
not protect *[live]*:
- **CDC apply** (`INSERT`/`UPDATE`) — a bad value **fails** *[live]* (task log /
  `awsdms_apply_exceptions`) instead of being adjusted. Watch the exceptions table after every probe.
- **Full load** uses `LOAD DATA LOCAL`, which is **non-restrictive whatever `sql_mode` says**
  (MySQL: data interpretation "is restrictive if the SQL mode is restrictive and neither the
  IGNORE nor the LOCAL modifier is specified") — truncation and invalid dates become warnings
  *[live]*.
  Only the normalized compare (§9) and the post-load checks catch them.
- **Dates:** PostgreSQL `date`/`timestamp` values after `9999-12-31` or `±infinity` have no
  MySQL value — strict CDC apply rejects them *[live]*; non-strict apply and the full load write
  a **zero date** (`0000-00-00`) *[live]*, which `NO_ZERO_DATE` then makes un-writable by the
  app. Values before `1000-01-01` are outside MySQL's *supported* range but were accepted
  *[live]* ("earlier values might work, but are not guaranteed") — treat them as a check. Count
  them on the source before GATE 2 (`SELECT count(*) FROM t WHERE col < '1000-01-01' OR col >
  '9999-12-31' OR NOT isfinite(col)`) and decide (clean, clamp with acceptance, or exclude).
- MySQL → PostgreSQL forward: the source's own `sql_mode` (A3) is the app's contract on the
  old side and governs reverse-apply writes into it (§10.1) — leave it unchanged; a lax source
  `sql_mode` means reverse-applied values are adjusted silently there, so the rollback rehearsal
  compare must cover every type class.

## 7. Monitoring (both directions)

Same as homogeneous DMS — `dms-best-practices.md` §Monitoring Metrics: `CDCLatencySource`,
`CDCLatencyTarget`, `CDCIncomingChanges`, `FreeableMemory`, `CPUUtilization`, and on a PG
source `ReplicationSlotDiskUsage`. The `ReplicationTaskIdentifier` dimension is the task
ARN's **resource-id suffix** (`${ARN##*:}`), never the friendly name (empty datapoints are
not zero lag). `describe-table-statistics` gives per-table `FullLoadRows`, inserts/updates/
deletes and `ValidationState`. Alarms on the **reverse** task must see datapoints in the
rehearsal (`cutover-procedures.md` §Reverse-CDC prerequisites).

## 8. Application migration checklist (both directions)

Every item was checked on live engines unless marked; MySQL default `sql_mode` assumed.

| Topic | MySQL | PostgreSQL |
|---|---|---|
| Identifier quoting | backticks; `"…"` is a string unless `ANSI_QUOTES` | `"…"` identifiers (case-sensitive), `'…'` strings |
| Identifier case | table names per `lower_case_table_names` (Linux default 0: case-sensitive); column names case-insensitive | unquoted names fold to lower case |
| Identifier length | 64 characters | 63 bytes, longer names **silently truncated** |
| `\|\|` | logical OR (`'a' \|\| 'b'` → `0`) *[live]* | concatenation |
| `CONCAT(x, NULL)` | `NULL` *[live]* | ignores NULL (`'a'`) *[live]* |
| `LIMIT` | `LIMIT n OFFSET m` and `LIMIT m, n` | `LIMIT n OFFSET m` only — `LIMIT m, n` is a syntax error *[live]* |
| Upsert | `INSERT … ON DUPLICATE KEY UPDATE` (any unique key) | `INSERT … ON CONFLICT (cols) DO UPDATE / DO NOTHING` (conflict target required for DO UPDATE) |
| `RETURNING` | none (use `LAST_INSERT_ID()`) | `INSERT/UPDATE/DELETE … RETURNING` |
| `GROUP BY` | `ONLY_FULL_GROUP_BY` (functional dependence allowed); `HAVING` may use select aliases | strict; functional dependence only via PK |
| Booleans | `TRUE` = 1, `TRUE + 1` = 2 *[live]* | real boolean; `TRUE + 1` is an error *[live]* |
| Integer division | `5/2` = `2.5000`; `DIV` for integer division *[live]* | `5/2` = `2` *[live]* |
| Implicit casts | `'1' + 1` = `2` (double) *[live]*; `DATE + 1` = `20260102` (a number!) *[live]* | `'1' + 1` = `2` (unknown literal); `date + 1` = next day; no implicit text↔int in general |
| String comparison | default `utf8mb4_0900_ai_ci`: `'abc' = 'ABC'` true, `LIKE` case-insensitive *[live]*; UCA-9.0 collations NO PAD (older `utf8mb4_general_ci`/`unicode_ci`/`bin` PAD SPACE: trailing spaces ignored) | deterministic: case/accent-sensitive; `ILIKE`, `citext`, `lower()` |
| `citext` vs typed parameters | (`_ci` collation applies to every comparison) | `citext = 'X'` (untyped literal or a `citext` value) is case-insensitive, but against a **text/varchar-typed** value the operator resolves to `text = text` — **case-sensitive** (`= CAST(:e AS text)` / `AS varchar` → 0 rows) *[live]*. pgJDBC sends `setString()` as `varchar` by default (`stringtype`), and many drivers/report tools type parameters as text: cast to `::citext` or compare `lower()` both sides; `LIKE` stayed case-insensitive *[live]* |
| `ORDER BY` an ENUM | sorts by the **definition index** (`ENUM('pending','paid',…)` order, not alphabetical) *[live]* | `VARCHAR`/`TEXT` sorts by collation (alphabetical); a PG `ENUM` type sorts by declaration order — keep an enum type, or `ORDER BY array_position(ARRAY[…], col)` *[live]* |
| `NULL` in UNIQUE | multiple NULLs allowed | same; `UNIQUE NULLS NOT DISTINCT` (PG 15+) has no MySQL equivalent |
| Functions | `IFNULL`, `GROUP_CONCAT`, `DATE_FORMAT`, `STR_TO_DATE`, `NOW()` | `COALESCE` (`IFNULL` does not exist *[live]*), `string_agg`, `to_char`, `to_timestamp`, `now()` |
| Isolation default | **REPEATABLE READ** (InnoDB) | **READ COMMITTED** — re-test concurrency-sensitive code paths |
| `sql_mode` (PG → MySQL target) | set explicitly strict (§6); app SQL and views rehearsed under it — `ONLY_FULL_GROUP_BY`, strict truncation/date errors | PostgreSQL is always strict: an app that relied on PG errors keeps that behaviour on MySQL only with strict `sql_mode` |
| Time zones | `TIMESTAMP` converted session-TZ ↔ UTC; `DATETIME` naive | `timestamptz` stored UTC, rendered in session `TimeZone`; `timestamp` naive |
| Fractional seconds | `NOW()`/`CURRENT_TIMESTAMP`/`SYSDATE()` and `DATETIME`/`TIMESTAMP` default to **fsp 0** — `NOW(6)`, `DATETIME(6)`, `DEFAULT CURRENT_TIMESTAMP(6)` *[live]* | `now()` and timestamps carry µs — a PG → MySQL move needs the §4.3 fractional-seconds decision at GATE 2 |
| JSON | `->`, `->>`, `JSON_EXTRACT`, `JSON_CONTAINS`; keys returned sorted | `->`, `->>`, `@>`, `jsonb_path_*`; `jsonb` normalizes |
| CHECK | enforced from 8.0.16 (parsed and ignored before) | enforced |
| Sequences | `AUTO_INCREMENT` only | sequences are objects (`nextval`, identity) |
| Locking reads | `SELECT … FOR UPDATE`, `LOCK IN SHARE MODE` | `FOR UPDATE`, `FOR SHARE`, `SKIP LOCKED` (MySQL 8.0 also has `SKIP LOCKED`/`NOWAIT`) |
| Driver / URL / TLS | `jdbc:mysql://…?sslMode=VERIFY_IDENTITY`, MySQL Connector/J or MariaDB driver; `caching_sha2_password` needs TLS or RSA key exchange | `jdbc:postgresql://…?sslmode=verify-full&sslrootcert=…`; SCRAM-SHA-256 |
| Pool settings | reconnect/validation query `SELECT 1` works on both; prepared-statement caching flags differ per driver | |

Deliver findings as `app-remediation-findings.md` with file:line, construct, replacement
and severity; never edit the customer's application from this skill.

## 9. Validation across engines

**DMS validation and JSON (both directions).** DMS row validation compares the two engines'
representations of a JSON/jsonb value, and those differ for equal data — number formatting
(PG `jsonb` 6558.930000 vs MySQL `JSON` 6558.93), key order, whitespace. Live (OBS-54): validation
of `payment_events` ended in `Table error` on exactly that, and continuous validation of the table
was lost. Keep DMS validation off the JSON columns and validate them with the normalized compare
(`cross_engine_compare.py` canonicalizes JSON):
- per column: a `validation` rule with `"rule-action": "override-validation-function"` (documented;
  not allowed on PK columns) whose `source-function` and `target-function` both return the same
  constant (e.g. `"'json-excluded'"`), so DMS stops comparing that column — the constant-function
  use is **verify in rehearsal** (confirm the table reaches `Validated` and a real non-JSON change
  is still caught);
- per table, if needed: disable validation on the replication task and run a separate
  **validation-only task** whose selection rules omit the table (documented: validation-only tasks
  with their own table mappings);
- record every excluded column/table as **"not continuously validated by DMS — covered by the
  normalized compare at <times>"** in GATE 3's "does NOT prove" column.

**Why naive checksums differ even for perfect data:** the engines render the same value
differently — `1` vs `t`, `2026-01-01 09:00:00` (DATETIME) vs `2026-01-01 00:00:00+00`
(timestamptz), `{"b": 1, "a": 2}` key order and whitespace, `1.500000` vs `1.5`, float text,
trailing-space PAD semantics, upper- vs lower-case UUIDs, `'a,b'` vs `{a,b}`. A checksum
over each engine's own text form is meaningless across the pair.

**`shared/scripts/cross_engine_compare.py`** — read-only, credentials via `password_env` or
`secret_arn` only, bounded runtime. Config = the `source`/`target` shape of
`soak-config.json`, plus `tables` and optional `cross_engine` options:

```bash
export SRC_RO_PW=... TGT_RO_PW=...      # from Secrets Manager on-host, never argv
python3 shared/scripts/cross_engine_compare.py --config compare-config.json --out compare-report.json
# exit 0 = all MATCH, 1 = MISMATCH, 2 = NEEDS_REVIEW, 3 = config/connection error
```

```json
{"source": {"engine": "mysql", "host": "10.0.1.5", "port": 3306, "user": "cmp_ro", "database": "shop",
            "password_env": "SRC_RO_PW", "ssl_ca": "/etc/pki/source-ca.pem"},
 "target": {"engine": "aurora-postgresql", "host": "<cluster>.rds.amazonaws.com", "port": 5432,
            "user": "cmp_ro", "database": "app", "password_env": "TGT_RO_PW"},
 "tables": ["shop.orders", "shop.order_items"],
 "cross_engine": {"naive_timezone": "+09:00", "bool_policy": "strict",
                  "tables": {"shop.orders": {"target": "shop.orders", "columns": {"Legacy_Col": null},
                                             "casefold_columns": ["email"]}}}}
```

How it compares: per table it resolves both catalogs (case-insensitive name match +
explicit `columns` overrides), classifies every column, decides how each **pair** is
compared, then streams both sides in primary-key order (keyset chunks of `chunk_size`;
text keys ordered by bytes on both engines), merges by key and hashes the canonical row.
The report gives row counts, per-table digests, mismatching chunks, and up to
`max_examples` keys **missing on target / extra on target / different** with the differing
column names (values only with `--include-values` — they may be PII). Tables without a
usable key are compared as a multiset (bounded by `multiset_max_rows`). Verdicts are the
soak scripts' four states: **MATCH** (true) / **MISMATCH** (false) / **NEEDS_REVIEW** (null:
budget exhausted, ordering disagreement, unsupported column) / **NOT_APPLICABLE**.
`max_seconds` bounds the whole run: it is checked after every fetch and during
normalization, a comparison that finishes past it is NEEDS_REVIEW (never MATCH), and every
statement also gets a server-side timeout of the same length (MySQL `max_execution_time`,
MariaDB `max_statement_time`, PostgreSQL `statement_timeout` — psycopg connections set it at
connect, since psycopg has no read timeout). Snapshots: PostgreSQL `BEGIN … REPEATABLE READ READ
ONLY`; MySQL-family sides first run `SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ`,
because `WITH CONSISTENT SNAPSHOT` is silently ignored under any other isolation level — a target
parameter group with `transaction_isolation=READ-COMMITTED` produced 78 spurious money
mismatches in a live run before this fix (the soak scripts do the same).

**Throughput, workers, progress.** Measured on this repo's host (8 vCPU Graviton, MySQL 8.0 →
PostgreSQL 16 in local Docker, 1,000,000 rows of bigint PK + varchar + DECIMAL(20,6) +
DATETIME(6)→timestamptz + JSON→jsonb + TINYINT(1)→boolean + text, pg8000): **16.7k rows/s**
single-threaded before the v2 fast paths, **25.7k rows/s** after (33k with the psycopg binary
driver, `pg_driver: "psycopg"`), **51k / 98k / 162k rows/s** with `--workers 2 / 4 / 8`. Live
small helpers are far slower — a t4g.small did ~7k rows/s (27 min for one 12M-row table), so
measure on the real helper in rehearsal and budget Phase 7 as `rows ÷ rows_per_s × 1.2`; size the
compare host (≥ 4–8 vCPU) for the window rather than the compare for the host. `--workers N`
splits each table with one integer key into 4×N key ranges (first/last unbounded, so every row is
covered), each read by a worker process **in its own snapshot pair** — per-range snapshots,
**not a single consistent cut** (report `consistency: "per_range_snapshots"`). Identical results
to one scan while writes are frozen, which the run must declare with `--writes-frozen` (or the
config's `"writes_frozen": true` — a JSON boolean only; `"false"`, `0`, `null` are config errors,
exit 3; recorded as `writes_frozen`). On live data a row written between two ranges' snapshots can show as a
difference, and a row whose key moves from a not-yet-read range into an already-read one is
**missed** — so without `--writes-frozen` an all-ranges MATCH is reported as `NEEDS_REVIEW`
(`unproven_verdict: "MATCH"`, exit 2), never as proof. A range that fails, outlives the budget, starts
after it, or could not be submitted (worker pool broke mid-submission) becomes a `range_errors`
entry while the submitted ranges keep their evidence (MISMATCH still wins);
the coordinator enforces `--max-seconds` itself and terminates stuck workers. Tables without
one integer key run sequentially in one snapshot (noted). Progress goes to stderr every
`--progress-interval` s (rows compared / catalog estimate / rows per s / ETA; a timed heartbeat
even when no worker reports) and, with `--progress-file`, to an atomically replaced JSON
(`table, rows_compared, rows_total_estimate, rows_per_s, eta_s, updated_at, workers, state`) the
agent mirrors into the dashboard step text.

**In-window quick check vs post-switch full compare.** Live: a rehearsal write pause of
**29.7 min**, of which **27 min** was a full normalized compare run inside the freeze — the switch
itself took 163 s (the rollback drill: 29.3 min, switch 144 s). Budget the freeze from the
**measured rehearsal timings** of each step, and keep the full compare out of it unless its
measured time (sized on a quiesced rehearsal run) fits. Say the split at GATE 4:
- *In the freeze (minutes) — quick checks only:* exact `COUNT(*)` per table both sides;
  watermark / last-key per table (`MAX(<pk>)`, `MAX(updated_at)` both sides); per-column non-NULL
  counts (`COUNT(col)` — catches conversions **to NULL** only: DMS's silent LOB drop on key-less
  tables, §4.7, and zero dates turned into NULL); **zero dates written as the non-NULL sentinel
  `0101-01-01` are invisible to counts** — check them **paired by key**: list the zero-date keys on
  the source (§2 A2 predicate) and read exactly those keys on the target (`SELECT <pk>, col FROM t
  WHERE <pk> IN (…)` — expect the agreed NULL), or use the compare tool's paired zero-date
  findings from an earlier full scan re-checked at the freeze; a `tail_keys` scan usually
  excludes these old keys; `SUM()` of money/quantity columns; and a **keyed compare of the
  hot tables' newest keys** — per hot table `"tables": {"<t>": {"tail_keys": N}}` (or
  `"key_range": [lo, hi]`, meaning (lo, hi]) with `--tables <hot> --writes-frozen` (legitimately
  true during the freeze). These are reported `coverage: partial` with the compared key range —
  evidence for those keys only. A full `--workers N --writes-frozen` compare belongs here only
  for tables whose measured time fits the budget.
- *Coverage statement:* the quick check proves row counts, NULL-ness per column and aggregate
  totals; it does **not** prove per-row equality (swapped/truncated values that keep the sums,
  case or time-zone shifts, JSON/text content).
- *Right after the switch:* a full compare while the reverse path is still inside the rollback
  window — but on live data it is **not completed proof by itself**. Both sides move (app writes
  on the new primary, reverse DMS onto the old one): a `--workers N` scan is per-range snapshots
  (NEEDS_REVIEW at best, rows can be missed), and even a single-snapshot `--workers 1` scan sees
  differences on keys written after the switch as lag. Proof = the frozen in-window compare, or
  a `--workers 1` single-snapshot scan **plus per-key reconciliation** of every reported key
  (re-read both sides: it converged, or it is a real difference). Every row untouched since the
  freeze must match exactly; a difference there is a rollback decision, not a ticket.

| Value kind | Canonical form (both engines) |
|---|---|
| integers, DECIMAL/NUMERIC (incl. BIGINT UNSIGNED ↔ NUMERIC(20)) | exact decimal text, no exponent, trailing zeros removed (`1.500000` → `1.5`, `-0` → `0`) — truncation (0.611111104488373 vs 0.611111) is a difference |
| FLOAT/REAL, DOUBLE | `float4_digits` (7) / `float8_digits` (15) significant digits |
| boolean (MySQL `TINYINT(1)`/`BIT(1)`, PG `boolean`, text `'true'/'false'`) | `true`/`false`; a TINYINT(1) holding 2 → `!bool:2` (never equal) |
| boolean ↔ integer column pair | `1`/`0` — and a **type finding** (below) |
| text | exact; `CHAR` right-trimmed; `text_casefold` / `casefold_columns` / `text_rstrip` options for collation-sensitive checks |
| ENUM ↔ text/enum | the label text |
| SET ↔ `text[]`/text | sorted unique members joined by `,` |
| JSON/JSONB/arrays (and JSON stored as text) | key-sorted compact JSON with canonical numbers (MySQL JSON text is parsed; PG drivers already return parsed values) |
| UUID ↔ CHAR(36)/BINARY(16) | lowercase hyphenated |
| binary | lowercase hex; BIT(n) zero-padded bit string |
| DATE | `YYYY-MM-DD`; MySQL zero/partial-zero dates → NULL (`zero_date: null` — what pgloader writes; the DMS doc says NULL too, but DMS 3.6.1 wrote `0101-01-01` on Aurora PG 17, §4.2) or `!zero:` (`distinct`), counted per side. A target `0101-01-01` (date, or midnight datetime/timestamptz) against a source zero date **for the same key** is reported as its own finding class `zero_date_as_0101` (`mismatches.zero_date_as_0101` per column, `zero_date_findings[].keys` + a note) — never as equal to NULL. Only paired per-key evidence counts: a key-less (multiset) table that shows both is `zero_date_inconclusive` (add a `key` and re-run), and a genuine `0101-01-01` that matches on both sides is never flagged. Remediate per key from the listed keys — never a blanket `UPDATE … WHERE col = '0101-01-01'` |
| DATETIME / timestamp (naive) | interpreted in `naive_timezone`, converted to a UTC instant |
| MySQL TIMESTAMP / PG timestamptz | UTC instant (sessions run with time zone UTC) |
| instant rendering | `YYYY-MM-DD HH:MM:SS.ffffff` truncated to `timestamp_precision` (default 6) |
| TIME / INTERVAL | `[-]HH:MM:SS.ffffff` (hours may exceed 24) |
| PG TIME WITH TIME ZONE | both zoned → UTC wall time; vs an unzoned TIME → **NEEDS_REVIEW** + finding unless `timetz_policy` is `utc` (shift to UTC; unzoned side read as UTC) or `wallclock` (drop the offset — the loss DMS performs) |
| NULL | its own marker — never equal to `''` |

**Type findings** are reported per column with a severity: `mismatch` (a mapping defect:
boolean stored as integer or text, JSON/array stored as text, column missing on target,
incompatible kinds) fails the table when `strict_types` is on (default); `risk` (BIGINT
UNSIGNED → BIGINT, unbounded NUMERIC → DECIMAL(p,s), scale or fractional-second narrowing)
and `warning` (ENUM → text, naive ↔ aware timestamps, nullability) are reported, values
decide. `bool_policy: "numeric"` accepts a deliberate TINYINT(1) → SMALLINT mapping; a
`kinds` override (`{"payload": "json"}`) accepts JSON-in-text after review — it changes how the
pair is *compared*, never how the driver's value is read (the physical catalog type still decides
that). Spatial columns
are `unsupported` → NEEDS_REVIEW unless listed in `ignore_columns` (validate them with an
app-level check).

Lab evidence (mysql:8.0 ↔ postgres:16, `scripts/test_cross_engine_compare.py`): correct
conversions MATCH both directions; the DMS-default mappings are flagged (TINYINT(1) →
SMALLINT, JSON → TEXT, TIME → VARCHAR, the BIGINT UNSIGNED overflow row pinpointed as
missing); a +09:00 load session, a case-folded e-mail column and `NUMERIC(20,2)` truncation
are pinpointed per column; a PG `'Bob'`/`'bob'` pair colliding under a `_ci` unique index is
pinpointed as the missing key.

**Also run:** DMS data validation (§5; check `ValidationState` per table and
`awsdms_control.awsdms_validation_failures_v1`), `COUNT(*)` both sides +
`describe-table-statistics`, schema-object counts (§4.6 inventory), FK orphans, and
application-level checks (`validation-patterns.md` §4) — especially ordering-sensitive
queries (collation), timezone-rendered reports, and aggregates. **Soak (Phase 7.7):**
`soak_check.py` / `soak_check_lambda.py` run this comparison for a MySQL ↔ PostgreSQL pair
(watermark-bounded; `cross_engine` options in `soak-config.json` / `CROSS_ENGINE_OPTIONS`);
native replica-status lag cannot apply across families — configure the DMS task metrics. The two
engines are read in separate snapshots, so a live-table count or checksum difference is
**re-checked in fresh snapshots before it can be RED** (`recheck`; only a persistent difference
is RED, a converged one is green with its readings kept); `static_tables` compare exactly, and
an optional `row_count_tolerance` applies to the remaining count difference only
(`execution-runbooks.md` §soak config — live, a one-shot non-atomic count of a live table cost a
soak-window restart). Intentional schema differences (a converted schema's generated or extra
columns, deliberate type changes) go in `expected_schema_differences` and are reported as
`expected (reason)` instead of schema drift; any undeclared difference still fails.

## 10. Cutover and rollback — reverse DMS

DMS supports the bidirectional pattern for MySQL, PostgreSQL, Aurora MySQL and Aurora
PostgreSQL: forward full load + CDC A → B; at cutover record **B's** CDC position; create a
**CDC-only** task B → A from that position. Running both directions at once needs
`LoopbackPreventionSettings` (`EnableLoopbackPrevention`, `SourceSchema`, `TargetSchema`),
`BatchApplyEnabled=false`, DML only, and **no conflict detection**. Every prerequisite
below is a GATE 4 / A4b precondition with evidence (`cutover-procedures.md` §Reverse-CDC
prerequisites).

**Both directions — learned from live rehearsals:**
- **"running" is not proof.** A reverse task failed 28 s after start while the drill had
  already recorded the transient `running` as success. Healthy = a hold of **≥ 60 s** with
  `Status=running` on every poll (no `LastFailureMessage`) **and** a probe write on the new
  primary (INSERT/UPDATE/DELETE on a probe row — on **every** replicated table, covering every
  type class, §10.2 probe matrix) visible by key **and equal by value** (normalized compare —
  OBS-69: presence hid 2 h of corrupted booleans) on the old side **and**, per probed table,
  `AppliedInserts`/`AppliedUpdates`/`AppliedDeletes` (`describe-table-statistics`) increased by
  that table's probes since the pre-probe reading (a delta, not "> 0"), `TableState` not `Table
  error`, no apply errors in the task log, `awsdms_apply_exceptions` empty for the window **and** the backlog
  drained — `CDCIncomingChanges` (changes *waiting* to be applied, not applied work; a healthy
  drained task reads 0) back to ~0 and `CDCLatencyTarget` low. Only then repoint the application.
- **The reverse target must be writable for DMS during the freeze.** Its first statement
  creates `awsdms_control`; `read_only`/`super_read_only` (MySQL) reject it (ERROR 1290), and
  `super_read_only` also blocks `ALTER USER … ACCOUNT LOCK` even for root *[live]*. Fence the
  **application accounts**, not the server: MySQL `ALTER USER '<app>'@'<host>' ACCOUNT LOCK` +
  `KILL` its sessions; PostgreSQL `ALTER ROLE <app> NOLOGIN` + `pg_terminate_backend()` on its
  sessions. Order when the server is already read-only: `SET GLOBAL super_read_only=OFF`
  (`read_only` stays ON and still blocks the app) → lock app accounts → kill their sessions →
  `SET GLOBAL read_only=OFF` → start the reverse task.
- **Triggers on the reverse target fire on DMS-applied rows** (a rollback duplicated every
  audit row, 47 → 94). MySQL has no session switch for that (`FOREIGN_KEY_CHECKS=0` covers FKs
  only): drop the trigger at cutover and recreate it at rollback, or guard it on the writer
  with `USER()` — inside a trigger `CURRENT_USER()` is the **definer** *[live]*. PostgreSQL:
  DMS does not change `session_replication_role` by itself; the endpoint's `AfterConnectScript:
  SET session_replication_role = replica` makes ordinary (`ENABLE`) triggers and FK triggers
  skip for DMS sessions only *[live]* (the endpoint user needs the right to set it:
  `rds_superuser`, or `GRANT SET ON PARAMETER session_replication_role` on PG 15+ — DMS documents
  for `AfterConnectScript` that the task continues whether the statement succeeds or fails, so
  prove it with a trigger-side count). Either
  way it is an explicit **disable-at-cutover /
  re-enable-at-rollback step pair** in `cutover-runbook.md`, rehearsed. Same rule forward: target
  triggers are created only in the cutover sequence (§4.5).
- **`GENERATED ALWAYS AS IDENTITY` on any PostgreSQL table DMS writes** (the forward MySQL →
  PG target, or the old PG source in §10.2) rejects DMS's explicit-id INSERTs. Declare
  `GENERATED BY DEFAULT` on the target; on a source it is an A2 change
  (`ALTER TABLE … ALTER COLUMN id SET GENERATED BY DEFAULT`, catalog-only *[live]*, prove on a clone).
- Key-less tables with LOB-class columns lose those columns in the reverse task too (§4.7).
- **Cascades on a MySQL primary never reach a PostgreSQL rollback side.** InnoDB writes no
  binlog events for cascaded child changes, and the reverse apply runs with
  `session_replication_role = replica`, which also skips the PG side's own FK actions — a parent
  DELETE on the new MySQL primary leaves orphan children on the rollback PG *[live]*. (PG → MySQL
  is not affected: PostgreSQL logs cascaded child changes as ordinary rows, and DMS applies them.)
  Per A7/§3 FK with a cascade action, before cutover either **(a)** convert the MySQL cascade
  into explicit, binlogged behaviour — the application deletes children itself, or

  ```sql
  -- MySQL target, created in the cutover sequence with the other deferred triggers
  ALTER TABLE order_items DROP FOREIGN KEY fk_items_order,
    ADD CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE RESTRICT;
  CREATE TRIGGER orders_bd_children BEFORE DELETE ON orders FOR EACH ROW
    DELETE FROM order_items WHERE order_id = OLD.id;   -- row events in the binlog → reverse task carries them
  ```

  (`ON UPDATE CASCADE` of a key: forbid key updates during the rollback window, or option b) —
  **(a) is the recommended option**; or **(b)** accept it and **reconcile the affected child
  tables against the authoritative MySQL side** after every rollback (and after the forward
  drain for a MySQL → PG forward task), before source writes are released: list every child
  table reachable through a cascade FK, transitively (R2, on MySQL), and run a keyed normalized
  compare of exactly those tables (`cross_engine_compare.py --tables <R2 list>`, under the
  rollback freeze so `--writes-frozen` is true), then repair the listed keys from MySQL. **An
  orphan count cannot prove equality**: a parent deleted and re-created with the same key
  leaves the stale child on PG with a valid parent — R1 reports 0 *[live]*. R1 stays as a quick
  detector (non-zero = certainly broken; zero = nothing proven). Rehearse it: a parent DELETE
  (and a delete + re-insert of the same key) on the new primary, then R2 + the keyed compare on
  the stand-in.

  ```sql
  -- R2. MySQL side: every child table reachable from a parent through CASCADE / SET NULL /
  --     SET DEFAULT FKs, transitively (a RESTRICT hop stops the chain) — the tables to compare.
  WITH RECURSIVE casc (sch, parent, child, depth) AS (
    SELECT constraint_schema, referenced_table_name, table_name, 1
    FROM information_schema.referential_constraints
    WHERE constraint_schema IN ('shop')
      AND (delete_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT') OR update_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT'))
    UNION ALL
    SELECT r.constraint_schema, r.referenced_table_name, r.table_name, c.depth + 1
    FROM information_schema.referential_constraints r
    JOIN casc c ON r.constraint_schema = c.sch AND r.referenced_table_name = c.child
    WHERE (r.delete_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT') OR r.update_rule IN ('CASCADE', 'SET NULL', 'SET DEFAULT'))
      AND c.depth < 16
  )
  SELECT DISTINCT sch, child FROM casc ORDER BY sch, child;
  ```

  ```sql
  -- R1. Quick DETECTOR only — orphan count per FK on the PostgreSQL side. > 0 = certainly
  --     broken (reconcile); 0 proves nothing (a re-created parent key hides stale children).
  SELECT format('SELECT %L AS fk, count(*) AS orphans FROM %s c LEFT JOIN %s p ON %s WHERE %s AND p.ctid IS NULL;',
                con.conrelid::regclass::text || '.' || con.conname, con.conrelid::regclass, con.confrelid::regclass,
                string_agg(format('p.%I = c.%I', pa.attname, ca.attname), ' AND '),
                string_agg(format('c.%I IS NOT NULL', ca.attname), ' AND ')) AS stmt
  FROM pg_constraint con
  CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(child_att, parent_att)
  JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = k.child_att
  JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = k.parent_att
  WHERE con.contype = 'f' AND con.connamespace::regnamespace::text IN ('shop')
  GROUP BY con.oid, con.conrelid, con.conname, con.confrelid;
  ```

### 10.1 MySQL → PostgreSQL: reverse task PostgreSQL → MySQL

**The reverse task of a MySQL → PG engagement is a PostgreSQL-source task** and needs exactly the
PG-source type handling of a forward PG → MySQL migration (§4.3–§4.4, §10.2): booleans (DMS reads
PG `boolean` as `CHAR(5)` `'true'`/`'false'` — `change-data-type` → `boolean`, never `int1`;
`MapBooleanAsBoolean` is a PostgreSQL-**source** endpoint setting documented for **Amazon Redshift
targets**, and the doc says "Since MySQL does not have a BOOLEAN type, use a transformation rule
rather than this setting when migrating BOOLEAN data to MySQL" — so it is not the fix here; verify
the rule's effect in rehearsal), uuid (`string` length 36 where the MySQL side is `CHAR(36)`, OBS-36),
json/jsonb (NCLOB — LOB settings and the key rule, §4.7), numeric (unbounded `NUMERIC` → `NUMERIC(28,6)`
on the DMS source side, §4.3), timestamps/timestamptz (zone and fsp, §4.2/§4.3). Live (OBS-69): the
reverse task silently corrupted **every boolean column for ≈ 2 h of production writes** while the
rollback was declared "armed/verified" — rehearsals had checked only that INSERT/UPDATE/DELETE
landed, never the values. **"Armed" therefore requires a value-level proof** (row below), and the
A5 RPO claim is valid only after it has passed.

**PG boolean → MySQL `TINYINT(1)` in the reverse CDC task — mechanism and recommended rule
(OBS-76).** Live: the reverse task (PG source, `test_decoding`) delivered booleans as the text `t`/`f`
during CDC, while full load delivers `true`/`false` (DMS `CHAR(5)`); the `int1` transform turned both
into `0`, and `MapBooleanAsBoolean` did not help — it is a PostgreSQL endpoint setting ("You must set
this setting on both the source and target endpoints for it to take effect", DMS API
`PostgreSQLSettings`), i.e. PG/Redshift targets, never a MySQL target. Letting MySQL coerce the text is
no fix either (strict apply rejects `'true'`/`'t'` into `TINYINT`; non-strict writes `0`). Recommended
configuration — **verify in rehearsal with a value-level probe in both forms** (a full-loaded row and
a CDC INSERT + UPDATE flipping the value):
1. a `change-data-type` rule to the DMS **`boolean`** type on every PG boolean column (DMS's MySQL
   target maps DMS `BOOLEAN` → MySQL `BOOLEAN`, i.e. `TINYINT(1)`) — never `int1`;
2. no `MapBooleanAsBoolean`; plugin left as the forward design chose (`pglogical` instead of
   `test_decoding` is untested here — do not switch it to fix booleans);
3. a value probe per boolean column after the rehearsal full load **and** after CDC changes
   (`SELECT col, count(*) … GROUP BY 1` both sides, plus the keyed compare of the probe rows).
**Fallback** if the probe still shows `0`/wrong values: a post-hoc boolean realignment from the PG
source of truth by key — a one-time correction plus a realignment step inside every rollback freeze
(what the live run did), recorded as a deviation with its RPO effect. Any rule must handle **both**
text forms (`true`/`false` and `t`/`f`); never assume one.

| ▢ | Prerequisite | Evidence |
|---|---|---|
| ▢ | `rds.logical_replication=1` on the PG target (Aurora: **cluster** parameter group; RDS: DB parameter group) — **static, reboot**; set it at creation (`cdk-stacks.md`) so cutover needs no reboot | `SHOW wal_level` = `logical` |
| ▢ | Reverse source account: set up with `rds_superuser` (DDL artefacts), endpoint uses an account with `rds_replication` + SELECT | `\du`, grants |
| ▢ | Tables without PK on the new PG side: `REPLICA IDENTITY FULL` (test_decoding) or their UPDATE/DELETE never reach MySQL | P2 query on the target |
| ▢ | `HeartbeatEnable=true` on the reverse source endpoint; free-storage / `ReplicationSlotDiskUsage` alarm | endpoint settings, alarm |
| ▢ | **Start point = a logical slot created at the freeze.** PostgreSQL sources do not support a custom CDC start *time*: after the forward drain and before repoint, `SELECT * FROM pg_create_logical_replication_slot('dms_rollback', 'test_decoding');` returns the slot's LSN *[live]*; the reverse source endpoint carries `SlotName=dms_rollback`, the task starts with `--cdc-start-position <confirmed_flush_lsn>` (not older than it) — **mandatory**: with `SlotName` and no LSN the task fails (`When working with Configured Slotname, user must specify LSN`, live) | slot row in `pg_replication_slots`, LSN in the runbook |
| ▢ | Old MySQL source writable for the dedicated reverse writer (`cutover-procedures.md` grants incl. `awsdms_control`), `local_infile=1`, `Initstmt=SET FOREIGN_KEY_CHECKS=0`; `read_only`/`super_read_only` are **global** — OFF before the reverse task starts, the app fenced by `ACCOUNT LOCK` (above) | `SHOW GRANTS`, rehearsal write, `SELECT @@read_only, @@super_read_only` |
| ▢ | MySQL triggers on the old source that the app's writes fire (audit, `updated_at`): dropped or `USER()`-guarded at cutover, restored at rollback (above) | runbook step pair, rehearsal audit count |
| ▢ | Rollback-side tables keep their **original MySQL DDL**; reverse task `TargetTablePrepMode: DO_NOTHING`, plus `change-data-type` rules (PG boolean → **`boolean`**, never `int1` — §4.4) so values land in the original types; boolean distribution checked after the rehearsal | reverse table mappings, `GROUP BY` counts |
| ▢ | Reverse task actually run on the rehearsal pair: INSERT/UPDATE/DELETE on PG each confirmed on the MySQL stand-in, compare tool MATCH | rehearsal evidence |
| ▢ | **Value-level reverse proof (gates "armed" and the A5 RPO):** probe rows on the new PG primary covering every type class in use (boolean, uuid, json, numeric/decimal, timestamps, text/enum/arrays), applied by the reverse task, then compared **by value** across engines — `cross_engine_compare.py --tables <probed tables>` with `key_range`/`tail_keys` on the probe keys, MATCH required — not presence or counts; repeated after any reverse-task restart and at cutover step 8 | compare report on the probe keys; boolean/uuid distributions both sides |
| ▢ | Drop the slot when the rollback window closes (it pins WAL) | decommission checklist |

### 10.2 PostgreSQL → MySQL: reverse task MySQL → PostgreSQL

| ▢ | Prerequisite | Evidence |
|---|---|---|
| ▢ | MySQL target emits a usable binlog: RDS automated backups on (`BackupRetentionPeriod` > 0) / Aurora `binlog_format=ROW` in the **cluster** parameter group; `binlog_row_image=FULL` | `SHOW GLOBAL VARIABLES LIKE 'binlog_%'` |
| ▢ | `CALL mysql.rds_set_configuration('binlog retention hours', 24);` (or more) — NULL purges binlogs ASAP | `CALL mysql.rds_show_configuration;` |
| ▢ | Reverse source account on MySQL: `REPLICATION CLIENT`, `REPLICATION SLAVE`, `SELECT`; TLS `verify-full` (8.4: `none` rejected) | `SHOW GRANTS` |
| ▢ | Start position captured at the freeze: `SHOW BINARY LOG STATUS` (8.4) / `SHOW MASTER STATUS` (8.0) on the new MySQL → `--cdc-start-position "<file>:<pos>"` (a start *time* also works for MySQL sources) | coordinates in the runbook |
| ▢ | Old PG source writable by a dedicated reverse writer (app roles fenced with `NOLOGIN`, not `default_transaction_read_only`); `AfterConnectScript: SET session_replication_role = replica` so source triggers (audit, event rows) and FK triggers don't fire on rollback writes — or disable them at cutover, re-enable at rollback | grants, endpoint setting, rehearsal trigger-side count |
| ▢ | **Reverse cascades:** every FK with `ON DELETE/UPDATE CASCADE` (or `SET NULL`) on the new MySQL primary is either converted to binlogged behaviour (app or `BEFORE DELETE` trigger + `RESTRICT`) before cutover — preferred — or covered by the accepted reconciliation after rollback: keyed normalized compare of every R2 child table against MySQL (R1 orphans only as a detector) — the replica role also skips PG's own FK actions | per-FK decision; rehearsal: parent DELETE and delete + re-insert of the same key on MySQL → keyed compare of the R2 tables on the PG stand-in = MATCH (a) / repaired to MATCH (b) |
| ▢ | Identity columns DMS writes on the old PG source are `GENERATED BY DEFAULT` (A2 if `ALWAYS`) | `SELECT attname, attidentity FROM pg_attribute WHERE attidentity = 'a'` empty |
| ▢ | MySQL generated columns of the converted schema (partial-unique emulation, §4.3) removed from the reverse task (`remove-column`) | reverse table mappings |
| ▢ | Rollback-side PG tables keep their original types (`DO_NOTHING`; `change-data-type` → `boolean` for TINYINT(1)) | reverse table mappings |
| ▢ | **uuid columns:** `change-data-type` → `{"type": "string", "length": 36}` on every MySQL `CHAR(36)` column that is `uuid` on PG. Live (Aurora MySQL 8.4.8 → PG 16 reverse task): without it, `CHAR(36)` utf8mb4 arrived as WSTRING and landed garbled (`d 4 a d …`) — the PG `uuid` input failed, every change to `accounts`/`ledger_entries` went **unapplied**, visible **only in the task log** while the task stayed `running`. The exact DMS conversion path is **verify in rehearsal** | reverse table mappings; rehearsal probe on each uuid table applied |
| ▢ | **Reverse proof covers every table × every type class:** a probe row INSERT + UPDATE (+ DELETE) on **each** replicated table of the new MySQL primary, spanning uuid, boolean, numeric/decimal, json, timestamps (+ text, enum, arrays where used), each verified on PG by key **by value** with the normalized compare (MATCH — presence is not proof; this gates "armed" and the A5 RPO); per table `describe-table-statistics` (`TableState` not `Table error`, `Applied*` deltas), the task log for apply errors, and `awsdms_apply_exceptions` (on the PG side, if present) empty for the probe window — the task status and the overall counters are not evidence. After restarting a reverse task from the recorded cutover position, expect duplicate-key (`23505`) apply errors for rows already applied; reconcile them by key, never by count | probe matrix + per-table evidence in the rehearsal record |
| ▢ | Rehearsed run + compare tool MATCH, as above | rehearsal evidence |

### 10.3 What reverse replication cannot carry — the RPO statement

Reverse DMS carries **DML only** in practice: no sequences (reset identity/AUTO_INCREMENT on
the old source at rollback), most DDL not (and never DDL you didn't rehearse), no
permission/user changes, LOB values limited by the reverse task's LOB settings, and only the
type fidelity the rollback-side DDL + rules preserve (PG `timestamptz`/`uuid`/`jsonb`/unbounded
`numeric` written natively on the new side don't round-trip unless the old side's types can
hold them). State it at GATE 2/4 in these terms: *"Rollback via reverse DMS replays committed
DML from the cutover position; data written after cutover in columns/types listed in
<findings> is converted per <rules>; schema changes made after cutover are not replicated;
sequences are re-seeded manually. RPO = reverse-task lag at the moment of rollback
(rehearsed: <N> s)."* If any reverse prerequisite is missing, rollback is RPO > 0 — use
`cutover-procedures.md` §When Reverse Replication is NOT Possible and record the acceptance.

## 11. pgloader — offline MySQL → PostgreSQL (small DBs, downtime accepted)

pgloader (PostgreSQL License; stable **v3.6.9**, 2022-10-24; a `v4-dev` pre-release exists
from 2026-06) loads schema + data + indexes + FKs + defaults in one pass. **No CDC** (the
source is frozen for the whole load = the outage), **cannot write to MySQL** (no PG → MySQL
equivalent), **views and triggers not migrated** (`MATERIALIZE VIEWS` copies views as tables),
only `POINT` among geometric types. MySQL-side TLS accepts `sslmode` but not the verify modes
(stable docs) — run pgloader inside the VPC / over the existing VPN path.

**Which build — verified against MySQL 8.0 *[live]*:**

| Build | Result against MySQL 8.0 |
|---|---|
| `dimitri/pgloader:latest` (Docker Hub) | amd64-only image; also an old 3.6.x |
| Debian bookworm package `pgloader 3.6.7~devel` | **cannot read MySQL 8**: `QMYND:MYSQL-UNSUPPORTED-AUTHENTICATION` while the server default plugin is `caching_sha2_password` (even for a `mysql_native_password` login), and `76 fell through ECASE expression` when the server defaults to native — its bundled QMyND predates MySQL 8 collation ids 76/255 ([#877](https://github.com/dimitri/pgloader/issues/877), [#1592](https://github.com/dimitri/pgloader/issues/1592)) |
| **v3.6.9 built from source** (`make save`, current QMyND via Quicklisp) | **works**, natively on arm64 and amd64 (Dockerfile in `scripts/test_cross_engine_compare.py`, ~3 min build) |

**Load login: its plugin must match the server's `default_authentication_plugin`.** With the
v3.6.9 source build on a MySQL 8.0 server defaulting to `caching_sha2_password`, a
`caching_sha2_password` login works and a `mysql_native_password` login fails the auth switch
*[live]*; with a server defaulting to native, use a native login. (RDS for MySQL 8.0.34+ uses
`mysql_native_password` as its default and it can't be changed; 8.4 defaults to
`caching_sha2_password` — `version-upgrades.md` §"MySQL 8.0 → 8.4".) Older 3.6.x builds can't
use `caching_sha2_password` at all ([#782](https://github.com/dimitri/pgloader/issues/782)).
Create it as its own A2 action: `CREATE USER pgl_load IDENTIFIED WITH <server default plugin>
BY '…'; GRANT SELECT, SHOW VIEW ON shop.* TO pgl_load;`

**pgloader exits 0 even when a table failed** *[live]* — a single bad value aborts that table's
COPY batch (all 60 rows of the lab table lost) and the run still returns 0. Judge success from
the summary's `errors` column **and** the compare tool, never the exit code.

**Default casts vs what MATCHed** (pgloader v3.6.9 → PostgreSQL 16, MySQL 8.0 source, compared
with `cross_engine_compare.py` *[live]*):

| MySQL | pgloader default | Outcome | CAST rule used |
|---|---|---|---|
| `TINYINT(1)` | `boolean` | MATCH | — |
| `ENUM` | per-column `CREATE TYPE …_status` | MATCH | — |
| `SET` | array of a per-column enum type (`_items_tags`) | MATCH (the compare tool parses the array literal pg8000 returns for unknown array types) | — |
| `JSON` | `json` | MATCH | optional: `type json to jsonb` for jsonb operators/indexes |
| `TIMESTAMP` | `timestamptz` | MATCH **only with the PG session in the MySQL session's zone** — `timezone 'Asia/Seoul'` shifted every value by 9 h | `SET PostgreSQL PARAMETERS timezone TO 'UTC'` (MySQL load session in UTC) |
| `DATETIME` | `timestamptz` (naive value read in the PG session zone) | MATCH with `timezone 'UTC'` + `naive_timezone: UTC`; shifted otherwise | `type datetime to timestamp drop default drop not null using zero-dates-to-null` (keeps naive semantics) |
| `BIGINT UNSIGNED` | `bigint` (the `precision` rule doesn't fire: MySQL 8 reports no display width) | **load failure**: `18446744073709551615 out of range` aborted the table | `type bigint when unsigned to numeric drop typemod` |
| `DATE` with a zero value (no zero-date DEFAULT) | `date` (zero-date conversion applies only to zero-date *defaults*) | **load failure**: `"0000-00-00" out of range` aborted the table | `type date to date drop default using zero-dates-to-null` |
| `TIME` > 24 h | `time` | **load failure**: `"24:00:00.024" out of range` | `type time to interval drop typemod` |
| `DECIMAL`, `DOUBLE`, `VARCHAR`/`TEXT`, `VARBINARY` | `numeric`, `double precision`, `varchar`/`text`, `bytea` | MATCH | — |
| table without PK | plain table | MATCH (multiset) | — |

**Executed command** (lab: containers on one Docker network; production: the same file with
real hosts and `?sslmode=require` / `verify-full`):

```
-- migrate.load — credentials come from MYSQL_PWD / PGPASSWORD in the environment, never the file or argv
LOAD DATABASE
  FROM mysql://pgl@mysqlsrc:3306/shop
  INTO postgresql://postgres@pgtgt:5432/tgt
WITH include drop, create tables, create indexes, reset sequences, foreign keys, downcase identifiers
SET PostgreSQL PARAMETERS timezone TO 'UTC'
CAST type bigint when unsigned to numeric drop typemod,
     type date to date drop default using zero-dates-to-null,
     type datetime to timestamp drop default drop not null using zero-dates-to-null,
     type time to interval drop typemod
INCLUDING ONLY TABLE NAMES MATCHING 'items', 'events'
ALTER SCHEMA 'shop' RENAME TO 'pgl_cast'
;
```

```bash
# MYSQL_PWD / PGPASSWORD fetched on-host from Secrets Manager (source-assessment.md §Credential Handling),
# passed via --env-file (mode 0600), never argv
docker run --rm --network <net> --env-file pgl.env -v "$PWD/migrate.load:/m.load:ro" \
  dbskill-pgloader:3.6.9-src pgloader /m.load
```

Result: `items` 60/60 and `events` 10/10 rows, `cross_engine_compare.py` **MATCH** on both
(one zero date counted and written as NULL). Notes: `SET MySQL PARAMETERS time_zone = '+00:00'`
is rejected (pgloader sends the value unquoted → MySQL 1064) — keep the load login's session
zone at the server default and check `@@time_zone`; `include drop` drops same-named target
tables, so load into a fresh schema (`ALTER SCHEMA … RENAME TO`). Then: re-create views,
triggers, routines (§4.6), add what pgloader doesn't (CHECKs for ENUM/SET if you replace the
types, `citext` / `lower()` uniques for A4 rows), confirm sequences (`reset sequences`
covers serials), and time the rehearsal — that is the outage.

## 12. Facts most likely to be gotten wrong

1. DMS default mappings: TINYINT(1) → **SMALLINT**, BIGINT UNSIGNED → **BIGINT** (overflow),
   JSON → TEXT, MySQL TIMESTAMP converted to UTC; PG BOOLEAN → **VARCHAR(5)** `'true'/'false'`,
   timestamptz loses the offset, unbounded NUMERIC → **NUMERIC(28,6)**.
2. PostgreSQL sources **cannot start CDC from a timestamp** — reverse rollback from a PG target
   needs a logical slot + LSN captured at the freeze, and `rds.logical_replication=1` (static).
3. DMS Schema Conversion: MySQL ≤ 8.0 sources, Aurora MySQL 8.0.32 / RDS MySQL 8.0.23 targets —
   **not 8.4**. Aurora PG 18 is not yet a DMS SC target.
4. SCT PG → MySQL needs `lower_case_table_names=1`, fixed at Aurora MySQL 3 cluster creation.
5. pgloader: no CDC, cannot write MySQL, exits 0 on table failures; the Debian 3.6.7 package
   can't read MySQL 8 — use a v3.6.9 source build with a login matching the server's default
   auth plugin, and the §11 CAST rules (BIGINT UNSIGNED, zero dates, TIME > 24 h abort the load).
6. Aurora MySQL 8.4 rejects DMS `SslMode none`; MySQL 8.4 and PG 17–18 need DMS 3.5.4+.
7. PG tables without PK silently drop UPDATE/DELETE in CDC unless `REPLICA IDENTITY FULL`.
8. DMS does not carry secondary indexes, FKs, defaults, identity, views, routines, triggers,
   users — and does not move sequences during CDC.
9. DMS **removes LOB-class columns of tables without a PK/UK** (MySQL JSON/MEDIUM/LONG
   TEXT/BLOB; PG text/citext/jsonb/bytea/arrays) — silently, counts match (§4.7). GATE 2 blocker.
10. MySQL zero dates arrived as **`0101-01-01`**, not NULL (DMS 3.6.1 → Aurora PG 17, live);
    CDC UPDATEs bring them back after a one-time fix (§4.2).
11. `change-data-type` → `int1` on a PG boolean turns `'true'` into 0; use `boolean` and prove
    every rule with value distributions (§4.4). Rules on BLOB/CLOB-mapped columns are ignored.

## Sources

AWS DMS — [MySQL source](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.MySQL.html) ·
[PostgreSQL source](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html) ·
[MySQL target](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Target.MySQL.html) ·
[PostgreSQL target](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Target.PostgreSQL.html) ·
[Sources](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Introduction.Sources.html) ·
[Targets](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Introduction.Targets.html) ·
[CDC / bidirectional / native start points](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html) ·
[Full-load task settings (TargetTablePrepMode)](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.FullLoad.html) ·
[Transformation rules (change-data-type)](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TableMapping.SelectionTransformation.Transformations.html) ·
[Premigration assessments (LOBs without PK/UK)](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.AssessmentReport.Assessments.All.html) ·
[Data validation](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Validating.html) ·
[Best practices](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_BestPractices.html) ·
[Troubleshooting](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html) ·
[Release notes](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_ReleaseNotes.html) ·
[DMS Schema Conversion](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_SchemaConversion.html) ·
[SCT](https://docs.aws.amazon.com/SchemaConversionTool/latest/userguide/CHAP_Welcome.html) ·
[SCT PostgreSQL source](https://docs.aws.amazon.com/SchemaConversionTool/latest/userguide/CHAP_Source.PostgreSQL.html) ·
[SCT MySQL source](https://docs.aws.amazon.com/SchemaConversionTool/latest/userguide/CHAP_Source.MySQL.html).
RDS/Aurora — [Aurora MySQL parameter groups (lower_case_table_names)](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Reference.ParameterGroups.html) ·
[RDS for MySQL known issues (lower_case_table_names)](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) ·
[Aurora MySQL 8.4 GA](https://aws.amazon.com/about-aws/whats-new/2026/05/amazon-aurora-mysql/8-4/) ·
[Aurora MySQL 8.4.8](https://aws.amazon.com/about-aws/whats-new/2026/09/amazon-aurora-mysql-848-available/) ·
[Aurora MySQL release calendar](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/AuroraMySQL.release-calendars.html) ·
[Aurora PostgreSQL 18](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-aurora-postgresql-major-version-18/) ·
[Aurora PostgreSQL 18.6/17.11/…](https://aws.amazon.com/about-aws/whats-new/2026/09/amazon-aurora-postgresql-18-6-17-11-16-15-15-19-14-24/).
MySQL 8.4 manual — [identifier case](https://dev.mysql.com/doc/refman/8.4/en/identifier-case-sensitivity.html) ·
[identifier length](https://dev.mysql.com/doc/refman/8.4/en/identifier-length.html) ·
[sql_mode](https://dev.mysql.com/doc/refman/8.4/en/sql-mode.html) ·
[Unicode collations](https://dev.mysql.com/doc/refman/8.4/en/charset-unicode-sets.html) ·
[date/time types](https://dev.mysql.com/doc/refman/8.4/en/datetime.html) ·
[JSON](https://dev.mysql.com/doc/refman/8.4/en/json.html) ·
[InnoDB limits](https://dev.mysql.com/doc/refman/8.4/en/innodb-limits.html) ·
[isolation levels](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html) ·
[ON DUPLICATE KEY UPDATE](https://dev.mysql.com/doc/refman/8.4/en/insert-on-duplicate.html) ·
[CHECK constraints](https://dev.mysql.com/doc/refman/8.0/en/create-table-check-constraints.html) ·
[ENUM sorting](https://dev.mysql.com/doc/refman/8.0/en/enum.html) ·
[fractional seconds](https://dev.mysql.com/doc/refman/8.4/en/fractional-seconds.html) ·
[TIMESTAMP/DATETIME initialization (fsp must match)](https://dev.mysql.com/doc/refman/8.4/en/timestamp-initialization.html) ·
[NOW()/SYSDATE() fsp](https://dev.mysql.com/doc/refman/8.4/en/date-and-time-functions.html) ·
[LOAD DATA (LOCAL = IGNORE)](https://dev.mysql.com/doc/refman/8.0/en/load-data.html) ·
[read_only / super_read_only](https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_read_only) ·
[information_schema_stats_expiry (cached TABLES.AUTO_INCREMENT)](https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_information_schema_stats_expiry) ·
[CURRENT_USER() in triggers](https://dev.mysql.com/doc/refman/8.0/en/information-functions.html#function_current-user) ·
[online DDL (auto-increment column)](https://dev.mysql.com/doc/refman/8.0/en/innodb-online-ddl-operations.html).
PostgreSQL — [lexical structure (identifiers)](https://www.postgresql.org/docs/current/sql-syntax-lexical.html) ·
[collations](https://www.postgresql.org/docs/current/collation.html) ·
[citext](https://www.postgresql.org/docs/current/citext.html) ·
[date/time](https://www.postgresql.org/docs/current/datatype-datetime.html) ·
[JSON](https://www.postgresql.org/docs/current/datatype-json.html) ·
[INSERT … ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html) ·
[transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html) ·
[sequence functions](https://www.postgresql.org/docs/current/functions-sequence.html) ·
[CREATE TABLE (GENERATED ALWAYS / BY DEFAULT)](https://www.postgresql.org/docs/current/sql-createtable.html) ·
[session_replication_role](https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-SESSION-REPLICATION-ROLE) ·
[pgJDBC stringtype](https://jdbc.postgresql.org/documentation/use/) ·
[pg_get_keywords / system information functions](https://www.postgresql.org/docs/current/functions-info.html) ·
[PG 15 release (NULLS NOT DISTINCT)](https://www.postgresql.org/docs/release/15.0/) ·
[PG 18 release](https://www.postgresql.org/docs/release/18.0/).
pgloader — [MySQL reference](https://pgloader.readthedocs.io/en/latest/ref/mysql.html) ·
[command / connection strings](https://pgloader.readthedocs.io/en/latest/command.html) ·
[releases](https://github.com/dimitri/pgloader/releases) ·
[issue #782 (caching_sha2_password)](https://github.com/dimitri/pgloader/issues/782) ·
[issue #877](https://github.com/dimitri/pgloader/issues/877) · [issue #1592 (MySQL 8 collation ids)](https://github.com/dimitri/pgloader/issues/1592) ·
[build from source](https://pgloader.readthedocs.io/en/latest/install.html).
MySQL Workbench — [Migration Wizard](https://dev.mysql.com/doc/workbench/en/wb-migration-overview.html).
