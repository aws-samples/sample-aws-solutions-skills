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
    WHEN data_type IN ('tinyblob','blob','mediumblob','longblob','tinytext','text','mediumtext','longtext') THEN 'LOB: DMS LOB mode decides truncation'
  END AS concern
FROM information_schema.columns
WHERE table_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
HAVING concern IS NOT NULL
ORDER BY table_schema, table_name, column_name;

-- A2. Zero / partial-zero dates (legacy data written under a lax sql_mode). Generates one
--     COUNT per date column; run the generated statements. DMS and pgloader write NULL.
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
--     task from PostgreSQL needs REPLICA IDENTITY FULL for them — §10.1)
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
```

Record per finding: object, decision (target type / rewrite / accept), owner. A4 rows need
an explicit choice: `citext`, a unique index on `lower(col)`, or a nondeterministic ICU
collation (PG 12+; `LIKE` on nondeterministic collations only from PG 18; slower, no B-tree
deduplication). A7 `fk_cascade` rows: DMS does not replicate cascade effects from a MySQL
source (no binlog events for cascaded changes) — keep the target FK with the same action in
place during CDC, or reconcile children before cutover; the FK-orphan check
(`validation-patterns.md` §2.4) must run after the final drain.

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
```

P7 collisions are a hard blocker for a `_ci` target column: choose `utf8mb4_bin` (or
`utf8mb4_0900_as_cs`) for that column, or resolve the duplicates with the data owner. A
`citext` source column is the opposite case: give it a `_ci` collation on MySQL.

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
| `DATE` | `DATE` | `DATE` | zero dates → NULL (A2) |
| `TIME` | `VARCHAR` (STRING) | `TIME`, or `INTERVAL` for > 24 h / negative | MySQL TIME spans ±838 h |
| `YEAR` | `SMALLINT` (INT2) | `SMALLINT` | |
| `CHAR/VARCHAR/TEXT` | `VARCHAR`/`TEXT` (LOB for `*TEXT`) | `VARCHAR(n)`/`TEXT`; `CITEXT` or `lower()` unique index for ci-unique columns (A4) | PG compares case-sensitively by default |
| `ENUM` | `VARCHAR` (WSTRING) — constraint lost | `CREATE TYPE … AS ENUM` or `TEXT` + `CHECK` | |
| `SET` | `VARCHAR` — constraint lost | `TEXT[]` + `CHECK (col <@ ARRAY[...])` or a child table | no SET type in PG |
| `JSON` | `TEXT` (CLOB) | `JSONB` (or `JSON` to keep text form) | JSONB normalizes key order/whitespace/duplicates |
| `BINARY/VARBINARY/BLOB` | `BYTEA` (BLOB) | `BYTEA` | LOB mode |
| `BIT(1)` / `BIT(n)` | `BOOLEAN` / `BYTEA` | `BOOLEAN` / `BIT(n)` | |
| `GEOMETRY`/`POINT`… | `BYTEA` | PostGIS `geometry` (manual) | DMS spatial = homogeneous only |
| `AUTO_INCREMENT` | attribute dropped | `GENERATED … AS IDENTITY` | reset after load (§4.5) |

### 4.3 Type mapping — PostgreSQL → MySQL

| PostgreSQL | DMS default on MySQL (DMS type) | Recommended MySQL type | Why / fix |
|---|---|---|---|
| `BOOLEAN` | `VARCHAR(5)` holding `'true'`/`'false'` (CHAR(5)) | `TINYINT(1)` | MySQL has no BOOLEAN; DMS says use a transformation rule; `MapBooleanAsBoolean` is a PostgreSQL→Redshift setting and does not help a MySQL target |
| `SMALLINT/INTEGER/BIGINT` | same | same | |
| `NUMERIC(p,s)` (p ≤ 65) | `DECIMAL(p,s)` | same | |
| `NUMERIC` (no p,s) | **`NUMERIC(28,6)` on the DMS source side — truncation** (e.g. 0.611111104488373 → 0.611111) | a wider target `DECIMAL` alone **does not help** — see the note below | fix it on the source, or accept the loss with evidence |
| `MONEY` | `DECIMAL(38,4)` | `DECIMAL(19,4)` | |
| `REAL`/`DOUBLE PRECISION` | `FLOAT`/`DOUBLE` | same | |
| `TEXT` / `CITEXT` | `TEXT`, or `LONGTEXT` with **ucs2** charset (> 64K or full LOB) | `TEXT`/`LONGTEXT` utf8mb4; `VARCHAR(n)` when indexed; `CITEXT` → `_ci` collation | ucs2 cannot hold 4-byte characters (inference) — pre-create utf8mb4 |
| `VARCHAR(n)` | `VARCHAR(n)` | same; unique index ≤ 3072 bytes (DYNAMIC) | `VARCHAR(1000)` utf8mb4 unique fails |
| `UUID` | `VARCHAR` | `CHAR(36)` (or `BINARY(16)`) | |
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

### 4.4 Making DMS load into the converted schema

- **Pre-create, then `DO_NOTHING` or `TRUNCATE_BEFORE_LOAD`.** `TargetTablePrepMode` values:
  `DO_NOTHING` (data and metadata untouched), `DROP_AND_CREATE` (DMS's own DDL — never for
  this pair), `TRUNCATE_BEFORE_LOAD` (data truncated, metadata kept — use for re-runs).
  `dms-best-practices.md`'s generic `DROP_AND_CREATE` example does **not** apply here.
- **Value conversion DMS must do** (e.g. PG `BOOLEAN` → MySQL `TINYINT(1)`): a
  `change-data-type` transformation rule on that column (DMS supports `boolean`, `int1`…`int8`,
  `uint1`…`uint8`, `numeric` with `precision`/`scale`, `string` with `length`, `datetime`,
  `date`, `time`, `bytes`, `blob`, `clob`, `nclob`, …):

  ```json
  {"rule-type": "transformation", "rule-id": "10", "rule-name": "flag-to-int1",
   "rule-action": "change-data-type", "rule-target": "column",
   "object-locator": {"schema-name": "public", "table-name": "accounts", "column-name": "active"},
   "data-type": {"type": "int1"}}
  ```

  Limits: one transformation action per object; column/table names case-sensitive; columns
  mapped to BLOB/CLOB can only be dropped; no special characters in column names. A
  pre-created target column of the right type plus a value DMS writes in a compatible text
  form (PG accepts `'1'`/`'0'` for BOOLEAN input) may work without a rule — **prove it in
  the rehearsal** with the compare tool, for full load *and* CDC.
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
   - MySQL target: `ALTER TABLE accounts AUTO_INCREMENT = <max+1>;` (verify with `SHOW CREATE
     TABLE`; `information_schema.TABLES.AUTO_INCREMENT` is cached — `validation-patterns.md` §4).

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
columns skipped. Binlog state gate: `method-selection.md` §Binlog State Gate.

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
  doc warns for Oracle → PG; analogous for `_ci` vs deterministic — test).

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
  source change. No PK also disables batch apply, full LOB and validation for that table.
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
| `NULL` in UNIQUE | multiple NULLs allowed | same; `UNIQUE NULLS NOT DISTINCT` (PG 15+) has no MySQL equivalent |
| Functions | `IFNULL`, `GROUP_CONCAT`, `DATE_FORMAT`, `STR_TO_DATE`, `NOW()` | `COALESCE` (`IFNULL` does not exist *[live]*), `string_agg`, `to_char`, `to_timestamp`, `now()` |
| Isolation default | **REPEATABLE READ** (InnoDB) | **READ COMMITTED** — re-test concurrency-sensitive code paths |
| Time zones | `TIMESTAMP` converted session-TZ ↔ UTC; `DATETIME` naive | `timestamptz` stored UTC, rendered in session `TimeZone`; `timestamp` naive |
| JSON | `->`, `->>`, `JSON_EXTRACT`, `JSON_CONTAINS`; keys returned sorted | `->`, `->>`, `@>`, `jsonb_path_*`; `jsonb` normalizes |
| CHECK | enforced from 8.0.16 (parsed and ignored before) | enforced |
| Sequences | `AUTO_INCREMENT` only | sequences are objects (`nextval`, identity) |
| Locking reads | `SELECT … FOR UPDATE`, `LOCK IN SHARE MODE` | `FOR UPDATE`, `FOR SHARE`, `SKIP LOCKED` (MySQL 8.0 also has `SKIP LOCKED`/`NOWAIT`) |
| Driver / URL / TLS | `jdbc:mysql://…?sslMode=VERIFY_IDENTITY`, MySQL Connector/J or MariaDB driver; `caching_sha2_password` needs TLS or RSA key exchange | `jdbc:postgresql://…?sslmode=verify-full&sslrootcert=…`; SCRAM-SHA-256 |
| Pool settings | reconnect/validation query `SELECT 1` works on both; prepared-statement caching flags differ per driver | |

Deliver findings as `app-remediation-findings.md` with file:line, construct, replacement
and severity; never edit the customer's application from this skill.

## 9. Validation across engines

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
connect, since psycopg has no read timeout).

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
| DATE | `YYYY-MM-DD`; MySQL zero/partial-zero dates → NULL (`zero_date: null`, what DMS and pgloader write) or `!zero:` (`distinct`), counted per side |
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
native replica-status lag cannot apply across families — configure the DMS task metrics.

## 10. Cutover and rollback — reverse DMS

DMS supports the bidirectional pattern for MySQL, PostgreSQL, Aurora MySQL and Aurora
PostgreSQL: forward full load + CDC A → B; at cutover record **B's** CDC position; create a
**CDC-only** task B → A from that position. Running both directions at once needs
`LoopbackPreventionSettings` (`EnableLoopbackPrevention`, `SourceSchema`, `TargetSchema`),
`BatchApplyEnabled=false`, DML only, and **no conflict detection**. Every prerequisite
below is a GATE 4 / A4b precondition with evidence (`cutover-procedures.md` §Reverse-CDC
prerequisites).

### 10.1 MySQL → PostgreSQL: reverse task PostgreSQL → MySQL

| ▢ | Prerequisite | Evidence |
|---|---|---|
| ▢ | `rds.logical_replication=1` on the PG target (Aurora: **cluster** parameter group; RDS: DB parameter group) — **static, reboot**; set it at creation (`cdk-stacks.md`) so cutover needs no reboot | `SHOW wal_level` = `logical` |
| ▢ | Reverse source account: set up with `rds_superuser` (DDL artefacts), endpoint uses an account with `rds_replication` + SELECT | `\du`, grants |
| ▢ | Tables without PK on the new PG side: `REPLICA IDENTITY FULL` (test_decoding) or their UPDATE/DELETE never reach MySQL | P2 query on the target |
| ▢ | `HeartbeatEnable=true` on the reverse source endpoint; free-storage / `ReplicationSlotDiskUsage` alarm | endpoint settings, alarm |
| ▢ | **Start point = a logical slot created at the freeze.** PostgreSQL sources do not support a custom CDC start *time*: after the forward drain and before repoint, `SELECT * FROM pg_create_logical_replication_slot('dms_rollback', 'test_decoding');` returns the slot's LSN *[live]*; the reverse source endpoint carries `SlotName=dms_rollback`, the task starts with `--cdc-start-position <confirmed_flush_lsn>` (not older than it) | slot row in `pg_replication_slots`, LSN in the runbook |
| ▢ | Old MySQL source writable for the dedicated reverse writer (`cutover-procedures.md` grants incl. `awsdms_control`), `local_infile=1`, `Initstmt=SET FOREIGN_KEY_CHECKS=0`, `read_only`/`super_read_only` off for that account only | `SHOW GRANTS`, rehearsal write |
| ▢ | Rollback-side tables keep their **original MySQL DDL**; reverse task `TargetTablePrepMode: DO_NOTHING`, plus `change-data-type` rules (PG boolean → `int1`) so values land in the original types | reverse table mappings |
| ▢ | Reverse task actually run on the rehearsal pair: INSERT/UPDATE/DELETE on PG each confirmed on the MySQL stand-in, compare tool MATCH | rehearsal evidence |
| ▢ | Drop the slot when the rollback window closes (it pins WAL) | decommission checklist |

### 10.2 PostgreSQL → MySQL: reverse task MySQL → PostgreSQL

| ▢ | Prerequisite | Evidence |
|---|---|---|
| ▢ | MySQL target emits a usable binlog: RDS automated backups on (`BackupRetentionPeriod` > 0) / Aurora `binlog_format=ROW` in the **cluster** parameter group; `binlog_row_image=FULL` | `SHOW GLOBAL VARIABLES LIKE 'binlog_%'` |
| ▢ | `CALL mysql.rds_set_configuration('binlog retention hours', 24);` (or more) — NULL purges binlogs ASAP | `CALL mysql.rds_show_configuration;` |
| ▢ | Reverse source account on MySQL: `REPLICATION CLIENT`, `REPLICATION SLAVE`, `SELECT`; TLS `verify-full` (8.4: `none` rejected) | `SHOW GRANTS` |
| ▢ | Start position captured at the freeze: `SHOW BINARY LOG STATUS` (8.4) / `SHOW MASTER STATUS` (8.0) on the new MySQL → `--cdc-start-position "<file>:<pos>"` (a start *time* also works for MySQL sources) | coordinates in the runbook |
| ▢ | Old PG source writable by a dedicated reverse writer; `AfterConnectScript: SET session_replication_role = replica` (FK/trigger bypass) — only if source triggers must not fire on rollback writes | grants, endpoint setting |
| ▢ | Rollback-side PG tables keep their original types (`DO_NOTHING`; `change-data-type` → `boolean` for TINYINT(1)) | reverse table mappings |
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
[CHECK constraints](https://dev.mysql.com/doc/refman/8.0/en/create-table-check-constraints.html).
PostgreSQL — [lexical structure (identifiers)](https://www.postgresql.org/docs/current/sql-syntax-lexical.html) ·
[collations](https://www.postgresql.org/docs/current/collation.html) ·
[citext](https://www.postgresql.org/docs/current/citext.html) ·
[date/time](https://www.postgresql.org/docs/current/datatype-datetime.html) ·
[JSON](https://www.postgresql.org/docs/current/datatype-json.html) ·
[INSERT … ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html) ·
[transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html) ·
[sequence functions](https://www.postgresql.org/docs/current/functions-sequence.html) ·
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
