# Data Validation Patterns

## Validation Strategy

Validation happens at three points:
1. **During migration** — DMS built-in validation (continuous)
2. **Pre-cutover** — Manual validation before switching traffic
3. **Post-cutover** — Application-level verification after go-live

---

## 1. DMS Built-in Validation

### Enable Validation in Task Settings

```json
{
  "ValidationSettings": {
    "EnableValidation": true,
    "ThreadCount": 5,
    "ValidationMode": "ROW_LEVEL",
    "FailureMaxCount": 10000,
    "HandleCollationDiff": "true",
    "RecordSuspendedState": "RECORD_ONLY",
    "SkipLobColumns": false,
    "TableFailureMaxCount": 1000,
    "ValidationOnly": false,
    "ValidationPartialLobSize": 0
  }
}
```

### Monitor Validation Status

```bash
# Check table-level validation status
aws dms describe-table-statistics \
  --replication-task-arn $TASK_ARN \
  --query 'TableStatistics[].{
    Table: TableName,
    State: ValidationState,
    Pending: ValidationPendingRecords,
    Failed: ValidationFailedRecords,
    Suspended: ValidationSuspendedRecords
  }' --output table
```

### Validation States

| State | Meaning | Action |
|-------|---------|--------|
| `Not enabled` | Validation not configured | Enable in task settings |
| `Pending records` | Records queued for validation | Wait — normal during active CDC |
| `Mismatched records` | Rows differ between source/target | Investigate — check LOB handling |
| `Suspended records` | Validation couldn't compare (e.g., no PK) | Add PK or accept limitation |
| `No primary key` | Table has no PK for row-level comparison | Add PK or use manual validation |
| `Table error` | Validation encountered an error | Check DMS logs |
| `Validated` | All rows match ✅ | Good to proceed |

### Common Validation Failures

| Failure Type | Cause | Resolution |
|-------------|-------|------------|
| Mismatched records (small count) | CDC latency — records changed after validation | Re-validate after CDC catches up |
| Mismatched records (LOB tables) | LOB mode truncation | Use Full LOB Mode or increase `LobMaxSize` |
| Suspended records | Table lacks primary key | Add PK, or accept and do manual validation |
| All records suspended | Permissions issue on source/target | Check DMS user grants |

---

## 2. Pre-Cutover Manual Validation

### 2.1 Row Count Comparison

**MySQL:**
```sql
-- Generate comparison queries dynamically
SELECT CONCAT(
  'SELECT ''', table_name, ''' AS tbl, COUNT(*) AS cnt FROM `', table_name, '` UNION ALL'
) FROM information_schema.tables
WHERE table_schema = 'your_db' AND table_type = 'BASE TABLE'
ORDER BY table_name;

-- Run the generated query on both source and target, compare results
```

**PostgreSQL:**
```sql
-- Generate comparison queries
SELECT format('SELECT %L AS tbl, COUNT(*) AS cnt FROM %I.%I UNION ALL',
  tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname = 'public'
ORDER BY tablename;
```

**Automation script:**
```bash
#!/bin/bash
# Compare row counts between source and target
# Credentials via MYSQL_PWD env or --defaults-extra-file — never in argv (see source-assessment.md)
export MYSQL_PWD=${MYSQL_PWD:?set via Secrets Manager fetch}
TABLES=$(mysql -h $SOURCE -u $USER -N -e \
  "SELECT table_name FROM information_schema.tables WHERE table_schema='$DB'")

echo "TABLE | SOURCE | TARGET | MATCH"
echo "------|--------|--------|------"
for TABLE in $TABLES; do
  SRC=$(mysql -h $SOURCE -u $USER -N -e "SELECT COUNT(*) FROM $DB.$TABLE")
  TGT=$(mysql -h $TARGET -u $USER -N -e "SELECT COUNT(*) FROM $DB.$TABLE")
  if [ "$SRC" = "$TGT" ]; then
    MATCH="✅"
  else
    MATCH="❌ (diff: $((TGT - SRC)))"
  fi
  echo "$TABLE | $SRC | $TGT | $MATCH"
done
```

### 2.2 Checksum Verification

**MySQL — pt-table-checksum (Percona Toolkit):**
```bash
# Install Percona Toolkit
apt-get install percona-toolkit

# Run checksum comparison
pt-table-checksum \
  --host=$SOURCE \
  --user=$USER \
  \
  --databases=$DB \
  --replicate=percona.checksums \
  --no-check-binlog-format \
  --chunk-size=5000

# Check results
pt-table-sync --print --replicate percona.checksums \
  --host=$SOURCE --user=$USER   # password via MYSQL_PWD
```

**MySQL — Native CHECKSUM TABLE (simpler but locks tables):**
```sql
-- Quick checksums (READ lock during execution)
CHECKSUM TABLE orders, products, users, cart_items;
-- Compare output between source and target
```

**PostgreSQL — Hash-based verification:**
```sql
-- MD5 hash of critical columns for a table
SELECT md5(string_agg(
  md5(CAST(id AS text) || CAST(amount AS text) || CAST(status AS text) || CAST(created_at AS text)),
  '' ORDER BY id
)) AS table_hash
FROM orders;
-- Run on both source and target, compare hashes
```

### 2.3 Sample Record Deep Comparison

```sql
-- Compare the last N records (catching recent CDC-applied changes)
-- Source:
SELECT * FROM orders WHERE id IN (
  SELECT id FROM orders ORDER BY id DESC LIMIT 100
) ORDER BY id;

-- Target:
SELECT * FROM orders WHERE id IN (
  SELECT id FROM orders ORDER BY id DESC LIMIT 100
) ORDER BY id;

-- Export to CSV and diff:
-- mysql -h $SOURCE ... --batch --raw > /tmp/source_sample.csv
-- mysql -h $TARGET ... --batch --raw > /tmp/target_sample.csv
-- diff /tmp/source_sample.csv /tmp/target_sample.csv
```

### 2.4 Referential Integrity Check

Run on the TARGET to ensure FK relationships are intact:

```sql
-- MySQL: Find orphaned references
-- Orders referencing non-existent users
SELECT o.id, o.user_id FROM orders o
LEFT JOIN users u ON o.user_id = u.id
WHERE u.id IS NULL;

-- Order items referencing non-existent orders
SELECT oi.id, oi.order_id FROM order_items oi
LEFT JOIN orders o ON oi.order_id = o.id
WHERE o.id IS NULL;

-- Order items referencing non-existent products
SELECT oi.id, oi.product_id FROM order_items oi
LEFT JOIN products p ON oi.product_id = p.id
WHERE p.id IS NULL;
```

```sql
-- PostgreSQL: Check all FK constraints
SELECT conname AS constraint_name,
  conrelid::regclass AS table_name,
  confrelid::regclass AS referenced_table
FROM pg_constraint
WHERE contype = 'f'
  AND NOT convalidated;  -- Shows invalid (violated) constraints

-- Validate all constraints (will error if violations exist)
-- ALTER TABLE orders VALIDATE CONSTRAINT fk_user_id;
```

### 2.5 Schema Object Verification

```sql
-- MySQL: Compare stored procedure counts
SELECT ROUTINE_TYPE, COUNT(*) FROM information_schema.routines
WHERE ROUTINE_SCHEMA = 'your_db' GROUP BY ROUTINE_TYPE;

-- Compare trigger counts
SELECT COUNT(*) FROM information_schema.triggers WHERE TRIGGER_SCHEMA = 'your_db';

-- Compare view counts
SELECT COUNT(*) FROM information_schema.views WHERE TABLE_SCHEMA = 'your_db';

-- Compare index counts
SELECT TABLE_NAME, COUNT(*) AS index_count FROM information_schema.statistics
WHERE TABLE_SCHEMA = 'your_db' GROUP BY TABLE_NAME ORDER BY TABLE_NAME;
```

---

## 3. Post-Cutover Validation

### Application Smoke Tests

Run immediately after cutover:

```bash
# Health check
curl -sf https://your-app.example.com/health | jq '.database'

# API functional tests (read)
curl -sf https://your-app.example.com/api/products | jq '.[] | .id' | head -5

# API functional tests (write)
curl -sf -X POST https://your-app.example.com/api/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1, "items": [{"product_id": 1, "quantity": 1}]}'

# Verify the write persisted
curl -sf https://your-app.example.com/api/orders?latest=1
```

### Performance Baseline Comparison

```sql
-- Run on Aurora after cutover — compare against source baseline
-- Query 1: Complex join (typical read pattern)
EXPLAIN ANALYZE SELECT o.id, o.total_amount, u.username
FROM orders o JOIN users u ON o.user_id = u.id
WHERE o.created_at > NOW() - INTERVAL 7 DAY
ORDER BY o.created_at DESC LIMIT 100;

-- Query 2: Aggregation (reporting pattern)
EXPLAIN ANALYZE SELECT DATE(created_at) AS dt, COUNT(*) AS orders, SUM(total_amount) AS revenue
FROM orders WHERE created_at > NOW() - INTERVAL 30 DAY
GROUP BY DATE(created_at);
```

### Monitoring Checklist (First 24 Hours)

- [ ] Application error rate < 0.1%
- [ ] P99 query latency within 2x of baseline
- [ ] No connection pool exhaustion
- [ ] No deadlocks or lock waits > 10s
- [ ] Aurora CPU < 70%
- [ ] Aurora FreeableMemory > 2 GB
- [ ] No replication lag on read replicas
- [ ] Scheduled jobs (cron, events) executing successfully
- [ ] Backup completed successfully (first automated backup)

---

## Validation Decision Matrix

| Validation Type | When | Criticality | Automated? |
|----------------|------|-------------|-----------|
| DMS built-in | During migration | High | ✅ Yes |
| Row count comparison | Pre-cutover, post-cutover | High | ✅ Scriptable |
| Checksum verification | Pre-cutover (final) | Medium | ✅ pt-table-checksum |
| Sample record comparison | Pre-cutover | Medium | Semi-auto |
| Referential integrity | Post-full-load, pre-cutover | High | ✅ Scriptable |
| Schema object count | After schema migration | Medium | ✅ Scriptable |
| Application smoke tests | Post-cutover | Critical | ✅ CI/CD |
| Performance comparison | Post-cutover (24h) | Medium | ✅ CloudWatch |

---

## 4. Application-Level & Engine-Specific Validation

### Application-Level Validation

Row counts and checksums prove the *bytes* moved; these checks prove the *application* still
behaves identically against the target. Run them pre-cutover, on BOTH source and target, and diff.

1. **Dual-read / shadow-traffic.** Configure the app to read from **both** source and target and
   compare results before trusting the target. Spring Boot: route via `AbstractRoutingDataSource`.
   Node.js: a read-through proxy that fans the read to both and diffs. Discrepancies surface
   collation, timezone, or replication-lag issues no row-count check would catch.

2. **Collation / sort-order check.** A different default collation silently reorders results and
   breaks `ORDER BY`-dependent pagination and `=`/`LIKE` matching:
   ```sql
   SELECT id, name FROM products ORDER BY name LIMIT 100;
   SHOW VARIABLES LIKE 'collation%';
   ```
   Run on BOTH, diff the results.

3. **AUTO_INCREMENT / sequence high-water-mark.** Confirm the target's next-value counter sits
   above the current max — otherwise the first inserts collide with existing PKs (see cutover-procedures.md §reset high-water marks):
   ```sql
   -- MySQL
   SELECT TABLE_NAME, AUTO_INCREMENT FROM information_schema.tables WHERE TABLE_SCHEMA = 'your_db';
   SELECT MAX(id) FROM orders;  -- Must be < AUTO_INCREMENT

   -- PostgreSQL
   SELECT schemaname, sequencename, last_value FROM pg_sequences;
   ```

4. **Aggregate fidelity.** Cheaper than a full checksum, catches truncated/duplicated rows and
   numeric-type drift:
   ```sql
   SELECT COUNT(*) AS cnt, SUM(total_amount) AS total, MIN(created_at) AS earliest, MAX(id) AS max_id FROM orders;
   ```
   Run on BOTH, compare.

5. **Timezone-shift detection.** A target with a different `time_zone` parameter shifts every
   TIMESTAMP — easy to miss until reports are off by hours:
   ```sql
   SELECT id, created_at FROM orders ORDER BY id DESC LIMIT 10;
   ```
   Any shift between source and target = timezone parameter mismatch (see the Adjustments table in source-assessment.md).

6. **Regression test suite.** Run your application's full test suite against the target database.
   If no test suite exists: for each major table, create 1 record, read it back, update it, and
   delete it — the minimum CRUD round-trip that proves the app's data layer works end-to-end.

### Post-Version-Upgrade Validation

> **Trigger — run this whole subsection ONLY when the source and target are different MAJOR
> versions** (e.g. MySQL 5.7 → 8.0, MySQL 8.0 → 8.4, MariaDB 10.x → 11.x, PostgreSQL 13 → 15). A
> logical dump can cross majors (execution-runbooks.md logical-dump note), but a version gap is a **behavioral-change surface**:
> the bytes can match (row counts + `CHECKSUM TABLE` pass) while the *engine behaves differently*. The
> checks above prove source↔target equivalence; these prove the workload survives the **version gap**
> itself. Read [version-upgrades.md](version-upgrades.md) for the exact
> per-version changes, then run the relevant checks below. Confirm the gap first — `SELECT VERSION();`
> on both — and record findings in `migration-plan.md`.

**1. Collation / sort-order ALGORITHM change (beyond the param check above).** The general collation
check compares the *variable*; a major upgrade can change the default collation *algorithm* itself —
MySQL 5.7 `utf8mb4_general_ci` vs 8.0 `utf8mb4_0900_ai_ci` sort and *compare* differently (accent/case
weighting) even for identical charset and data. Two risks: silent re-ordering, and **new
unique-key/PK collisions** (values that were distinct under the old collation become "equal").
```sql
-- run on BOTH; the new default collation may reorder rows even with the same charset
SELECT id, name FROM products ORDER BY name, id LIMIT 200;        -- diff the two outputs
-- what did each table/column actually resolve to after import?
SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA='your_db';
SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='your_db' AND COLLATION_NAME IS NOT NULL;
-- prove comparison semantics changed:
SELECT 'a' = 'A' COLLATE utf8mb4_0900_ai_ci AS ai_match;          -- 1 under the 8.0 default
-- new collision risk on a string unique key / PK — must return ZERO rows:
SELECT name, COUNT(*) c FROM products GROUP BY name HAVING c > 1;
```

**2. Auth-plugin compatibility (the app's real connector, not the `mysql` CLI).** 8.0 default became
`caching_sha2_password`; **8.4 ships `mysql_native_password` OFF at startup.** Old connectors
(Connector/J 5.1, libmysqlclient < 8.0, legacy PHP/Node drivers) then **fail to authenticate** even
though the import succeeded.
```sql
-- TARGET: which plugin did each account land on after import?
SELECT user, host, plugin FROM mysql.user;
```
```bash
# Authenticate from a host running the APP's actual connector version (the newer mysql CLI hides this)
mysql -h "$TARGET" -u appuser -p -e "SELECT CURRENT_USER(), CONNECTION_ID();"
```
Fix: upgrade the connector, **or** recreate the user on the legacy plugin
(`ALTER USER 'appuser'@'%' IDENTIFIED WITH mysql_native_password BY '…'`; on 8.4 first set
`mysql_native_password=ON` in the target parameter group — see version-upgrades.md MySQL 8.0→8.4).

**3. `sql_mode` strictness — catch queries that worked before but fail now.** A newer major defaults
to a stricter `sql_mode` (8.0 enables `ONLY_FULL_GROUP_BY`, `STRICT_TRANS_TABLES`, `NO_ZERO_DATE`,
`NO_ZERO_IN_DATE`). `SELECT`s and writes that the source tolerated now **error**.
```sql
SELECT @@GLOBAL.sql_mode;   -- run on SOURCE and TARGET, diff the flag set
```
Drive the app's real workload (the regression suite, item 6) under the target `sql_mode`, and probe
the known-risky shapes directly:
```sql
-- ONLY_FULL_GROUP_BY: a non-aggregated, non-grouped column now errors (1055) instead of returning arbitrary rows
SELECT customer_id, order_date, SUM(total) FROM orders GROUP BY customer_id;
-- NO_ZERO_DATE + STRICT: a zero/invalid date now errors instead of warning
INSERT INTO events (created_at) VALUES ('0000-00-00');
```
Also scan existing data that strict mode will reject on the *next* UPDATE:
```sql
SELECT COUNT(*) FROM events WHERE created_at = '0000-00-00' OR created_at IS NULL;
```
Goal is to fix the queries/data; only as a temporary bridge, drop the offending flag from the target
`sql_mode` parameter.

**4. Reserved-word collisions in the existing schema.** A new major reserves identifiers your schema
may already use (8.0: `RANK`, `ROW`, `ROWS`, `GROUPS`, `LEAD`, `LAG`, `OVER`, `CUME_DIST`,
`DENSE_RANK`, `FIRST_VALUE`, `NTILE`, `PERCENT_RANK`, `RECURSIVE`, `SYSTEM`; 8.4: `PARALLEL`,
`QUALIFY`, `TABLESAMPLE`, `MANUAL`; MariaDB 10.7: `ROW_NUMBER`, 11.6: `VECTOR`). The dump imports fine
(mysqldump back-quotes identifiers) — but **unquoted application SQL referencing those names breaks at
runtime.** Scan the migrated schema for collisions:
```sql
SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='your_db'
  AND UPPER(COLUMN_NAME) IN ('RANK','ROW','ROWS','GROUPS','LEAD','LAG','OVER','CUME_DIST',
      'DENSE_RANK','FIRST_VALUE','LAST_VALUE','NTILE','PERCENT_RANK','RECURSIVE','SYSTEM',
      'PARALLEL','QUALIFY','TABLESAMPLE','MANUAL','VECTOR','ROW_NUMBER');
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA='your_db'
  AND UPPER(TABLE_NAME) IN ('RANK','ROW','ROWS','GROUPS','LEAD','LAG','OVER','CUME_DIST',
      'DENSE_RANK','FIRST_VALUE','LAST_VALUE','NTILE','PERCENT_RANK','RECURSIVE','SYSTEM',
      'PARALLEL','QUALIFY','TABLESAMPLE','MANUAL','VECTOR','ROW_NUMBER');
```
Any hit → `grep` the application for unquoted use of that identifier and add back-quotes, or rename
the column. (PostgreSQL: query `information_schema.columns` the same way against the new major's
reserved list; `SELECT quote_ident('row');` shows whether a name now needs quoting.)

**5. Deprecated/removed functions & syntax in stored routines, triggers, views.** A logical dump
*creates* routines, but a function or syntax **removed** in the new major fails at **call time, not
import time** — so a green import hides it. Pull every body, scan for removed constructs, then
actually execute each one:
```sql
SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA='your_db';
SELECT TRIGGER_NAME, ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='your_db';
SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA='your_db';
```
Grep those bodies for things removed across the gap — e.g. 5.7→8.0: `PASSWORD()` (removed),
`GROUP BY … ASC/DESC` (removed), `ENCODE`/`DECODE`/`DES_ENCRYPT`/`DES_DECRYPT` (removed),
`SQL_CALC_FOUND_ROWS`/`FOUND_ROWS()` (deprecated). Then **exercise each routine** (the regression
suite, item 6, must drive every routine/trigger/view path — MySQL has no bulk recompile):
```sql
CALL your_proc(/* representative args */);
SELECT * FROM your_view LIMIT 1;
```
(Oracle's invalid-object scan + `UTL_RECOMP` is in the Oracle Validation block below; SQL Server: see
`sys.dm_os_performance_counters … 'Deprecated Features'` in version-upgrades.md, then re-run modules.)

**6. Default character-set change — verify data RENDERS, not just that counts match.** 5.7→8.0 default
charset went `latin1`→`utf8mb4`. Row counts and even `CHECKSUM TABLE` can pass while text is
**mojibake** if a `latin1` column's bytes were re-interpreted (double-encoded) during dump/reload.
Verify actual rendering of multibyte / Korean / emoji data:
```sql
-- run on BOTH with a utf8mb4 client connection so the CLI itself doesn't mangle the comparison:
--   mysql --default-character-set=utf8mb4 …
SELECT id, name, HEX(name) AS name_hex, LENGTH(name) AS bytes, CHAR_LENGTH(name) AS chars
FROM products WHERE name REGEXP '[^ -~]' LIMIT 50;     -- rows containing non-ASCII
SELECT T.TABLE_NAME, CCSA.CHARACTER_SET_NAME
FROM information_schema.TABLES T
JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY CCSA
  ON CCSA.COLLATION_NAME = T.TABLE_COLLATION
WHERE T.TABLE_SCHEMA='your_db';
```
`CHAR_LENGTH` (characters) **must match on both**; `bytes` differing is expected if encoding differs,
but the rendered glyphs must be identical. Garbled Korean/emoji = a charset bug in the dump step —
re-dump with `--default-character-set` matching the source's *true* storage charset and reload.

**7. Optimizer / query-plan regression.** A new major changes the cost model (MariaDB 11.0 time-based
optimizer; MySQL 8.0 histograms + new CTE/derived-table handling) and plans can **regress**. Baseline
`EXPLAIN` on the **source before cutover** and diff against the target.
```sql
-- SOURCE, pre-cutover: pull the hottest queries to baseline
SELECT DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT
FROM performance_schema.events_statements_summary_by_digest
ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;
EXPLAIN FORMAT=JSON SELECT /* each top query */ … ;     -- capture access type, key, rows, cost
-- TARGET (after ANALYZE TABLE so stats are fresh — Phase 9): same queries, compare
EXPLAIN ANALYZE SELECT … ;                              -- MySQL 8.0.18+ / MariaDB 10.1+: real timing
```
A plan that drops an index, changes join order, or balloons `rows`/cost vs the baseline = regression
→ pin with an index/optimizer hint, or (MariaDB 11.x) tune the new cost variables
(`optimizer_disk_read_cost`, …). **PostgreSQL:** baseline with `EXPLAIN (ANALYZE, BUFFERS)` on both
sides and pull the hot list from `pg_stat_statements`. See
[version-upgrades.md](version-upgrades.md) "After the upgrade" item 4.

### Oracle Validation (Oracle → RDS Oracle)

```sql
-- Monitor the running Data Pump job
SELECT job_name, operation, job_mode, state FROM dba_datapump_jobs WHERE owner_name = USER;
-- Read the import log (no OS access — use the rdsadmin helper)
SELECT * FROM TABLE(rdsadmin.rds_file_util.read_text_file('DATA_PUMP_DIR','sample_imp.log'));
-- Object counts by type (compare source vs target)
SELECT object_type, COUNT(*) FROM dba_objects WHERE owner='SCHEMA_1' GROUP BY object_type ORDER BY 1;
-- Invalid objects — then recompile (utlrp.sql can't run; no shell)
SELECT object_name, object_type FROM dba_objects WHERE status='INVALID' AND owner='SCHEMA_1';
EXEC UTL_RECOMP.RECOMP_PARALLEL(4,'SCHEMA_1');   -- or DBMS_UTILITY.COMPILE_SCHEMA('SCHEMA_1')
-- Row counts (gather stats first, or COUNT(*) for exactness)
EXEC DBMS_STATS.GATHER_SCHEMA_STATS('SCHEMA_1');
SELECT table_name, num_rows FROM dba_tables WHERE owner='SCHEMA_1' ORDER BY table_name;
```
Common import errors: `ORA-39083` (object create failed — grant/tablespace; fix grants or `METADATA_REMAP`), `ORA-31693` (table data load failed — quota/space), `ORA-39166` (object not in dump / wrong schema).

### SQL Server Validation (SQL Server → RDS SQL Server)

```sql
-- Integrity (you have db_owner on RDS); or use the AWSSQLServer-DBCC SSM automation document
DBCC CHECKDB ('mydatabase') WITH NO_INFOMSGS, ALL_ERRORMSGS;
-- Object counts (compare source vs target)
SELECT type_desc, COUNT(*) FROM sys.objects GROUP BY type_desc ORDER BY type_desc;
-- Row counts per table
SELECT s.name, t.name, SUM(p.rows) AS row_count
FROM sys.tables t JOIN sys.schemas s ON t.schema_id=s.schema_id
JOIN sys.partitions p ON t.object_id=p.object_id AND p.index_id IN (0,1)
GROUP BY s.name, t.name ORDER BY 1,2;
-- Orphaned users (fix with ALTER USER ... WITH LOGIN — see execution-runbooks.md §schema objects)
USE [mydatabase]; EXEC sp_change_users_login 'Report';
-- Compare logins source vs target
SELECT name, type_desc, sid FROM sys.server_principals
WHERE type IN ('S','U','G') AND name NOT LIKE '##%##' ORDER BY name;
-- TDE state (3 = encrypted)
SELECT DB_NAME(database_id) db, encryption_state FROM sys.dm_database_encryption_keys;
```

