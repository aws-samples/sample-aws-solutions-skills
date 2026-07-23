# Major Version Upgrades During Migration

## Why this lives here

A **logical-dump migration** (`mysqldump` / `pg_dump` / Data Pump → import) re-imports the data from
scratch, so the target can run a **newer major version** than the source *in the same migration* — no
separate upgrade project. This was used in the reference migration: **MariaDB 10.5.29 → 10.11.18**,
verified byte-for-byte with `CHECKSUM TABLE` (e.g. `products` checksum `3581083405` identical across
the version gap).

This is an **opportunity** worth taking — you land on a supported, longer-life version and avoid a
future in-place upgrade. But a version gap is also a **behavioral-change surface**. Review the items
below for the source→target gap before choosing the target version, and add anything relevant to
`migration-plan.md`.

> **Physical methods cannot do this.** XtraBackup, RDS snapshot copy, and Read Replica promotion
> preserve the on-disk format and **cannot skip a major version** — they require same-major (or a
> subsequent in-place upgrade). Only logical dump (and, for the commercial engines, DMS / native
> backup-restore / Data Pump) re-imports cleanly across majors.

> **Scope.** Covers all RDS-supported engines **except Db2**: MySQL, MariaDB, PostgreSQL, Oracle,
> SQL Server. Facts verified against vendor docs + AWS RDS docs current as of **2026-06**. Version
> availability and exact minor thresholds drift — confirm with `aws rds describe-db-engine-versions`
> before committing a target.

---

## General checklist (any engine)

- [ ] **Reserved words** — a new major may reserve identifiers your schema/queries use as
      column/table names. Quote them or rename. (See engine sections.)
- [ ] **Deprecated / removed functions & syntax** — features deprecated in the old version may be
      *removed* in the new one.
- [ ] **Default value changes** — character set, collation, `sql_mode`, auth plugin, optimizer
      defaults, compatibility level can differ between majors and silently change behavior.
- [ ] **Auth plugin / protocol** — the default authentication plugin or minimum TLS/logon version may
      change (affects how the app's connector authenticates).
- [ ] **Optimizer plan changes** — new cost models / cardinality estimators can change query plans
      across a major. Capture `EXPLAIN` baselines before; re-validate latency after.
- [ ] **Parameter/option groups don't cross majors** — RDS will not auto-migrate a *custom* parameter
      or option group to a new major family. Recreate it and **strip removed/deprecated parameters**
      first, or the new instance can fail to apply the group.
- [ ] **Validate after import** — object counts, `CHECKSUM TABLE` / aggregate checks across the gap,
      and a full application regression/CRUD round-trip (see validation-patterns.md §4).
- [ ] **Confirm the target major is offered on RDS/Aurora** for your engine before committing.

---

## MySQL / MariaDB

### MariaDB 10.5 → 10.11 (reference migration)
- Both LTS; 10.11 is a long-term-support release with a longer support horizon — good upgrade target.
- `CHECKSUM TABLE` was **identical** across the gap for InnoDB/Dynamic tables — data fidelity intact.
- Check: new **reserved words** added across 10.6–10.11 (e.g. `OFFSET`, window/CTE-related);
  `utf8` now aliases `utf8mb3` (deprecation warnings) — prefer explicit `utf8mb4`.
- Audit plugin / parameter-group settings (e.g. `MARIADB_AUDIT_PLUGIN`) are configured on the
  **target** option/parameter group, not carried by the dump.

### MySQL 5.7 → 8.0
- **Default charset** changed `latin1` → **`utf8mb4`**, default collation `utf8mb4_0900_ai_ci`.
  This is the most common surprise — verify column/table charsets and re-check sort order/collation
  (validation-patterns.md §4 collation check).
- **Default auth plugin** changed `mysql_native_password` → **`caching_sha2_password`** — older
  connectors may fail to authenticate; either upgrade the connector or create the user with the
  legacy plugin.
- Many **new reserved words** (`RANK`, `ROW`, `GROUPS`, `LEAD`, `LAG`, `CUME_DIST`, `OVER`, …) —
  quote any identifier that collides.
- `sql_mode` defaults stricter (`ONLY_FULL_GROUP_BY` etc.) — queries that relied on loose grouping
  break.
- Removed: query cache, some deprecated `GROUP BY ... ASC/DESC` syntax.

### MySQL 8.0 → 8.4 (latest LTS)
Target of choice for new migrations — 8.0 reaches RDS end of standard support **2026-07-31**, so
land on **8.4 LTS** (RDS GA 2024-11-21; community EOL 2029-04-30, RDS standard support to 2029-07-31).

- **`mysql_native_password` disabled by default** — the plugin still ships but is **OFF at startup**.
  Accounts created with it (and old PHP/JDBC/legacy connectors) **fail to authenticate** until you
  set `mysql_native_password=ON` in the 8.4 parameter group, or recreate users on
  `caching_sha2_password`. *Highest-impact app breakage of this hop.* Plugin is deprecated and slated
  for removal.
- **`default_authentication_plugin` removed** → replaced by `authentication_policy`. A custom 8.0
  parameter group that still references it will block startup — strip it before upgrading.
- **InnoDB defaults re-tuned for modern hardware**, can shift I/O / memory footprint — benchmark:
  `innodb_io_capacity` 200→10000, `innodb_log_buffer_size` 16M→64M, `innodb_adaptive_hash_index`
  ON→OFF, `innodb_change_buffering` all→none, `innodb_flush_method`→`O_DIRECT`,
  `innodb_numa_interleave` OFF→ON, `innodb_use_fdatasync` OFF→ON. Buffer-pool instance auto-sizing
  formula also changed.
- **Replication "master/slave" SQL fully *removed*** (not just deprecated/aliased) — any tooling
  using the old verbs hard-errors:
  - `CHANGE MASTER TO` → `CHANGE REPLICATION SOURCE TO` (and all `MASTER_*` → `SOURCE_*` options)
  - `START/STOP/RESET SLAVE` → `START/STOP/RESET REPLICA`; `SHOW SLAVE STATUS` → `SHOW REPLICA STATUS`
  - `RESET MASTER` → `RESET BINARY LOGS AND GTIDS`; `SHOW MASTER STATUS` → `SHOW BINARY LOG STATUS`
- **App-breaking specifics:** `AUTO_INCREMENT` on `FLOAT`/`DOUBLE` now rejected; `LOW_PRIORITY` with
  `LOCK TABLES ... WRITE` is a syntax error; non-unique / partial keys as FK-referenced keys
  restricted by default (`restrict_fk_on_non_standard_key=ON`); weak TLS ciphers removed (need
  TLS 1.2+ with PFS/SHA2/AEAD); `WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS()` removed.
- **New reserved words: `PARALLEL`, `QUALIFY`, `TABLESAMPLE`, `MANUAL`** (confirmed) — quote any
  colliding identifier. *(Several reported new non-reserved keywords — `AUTO`, `BERNOULLI`, `GTIDS`,
  `LOG`, `PARSE_TREE`, `S3` — are unverified; non-reserved words don't break identifiers regardless.)*
- **Removed system variables** (block startup if left in a parameter group): `expire_logs_days`
  (use `binlog_expire_logs_seconds`), `binlog_transaction_dependency_tracking`,
  `transaction_write_set_extraction`, `log_bin_use_v1_events`, `group_replication_ip_whitelist`
  (→ `..._ip_allowlist`), the legacy `keyring_file_*`/`keyring_encrypted_file_*` variables
  (→ keyring **components**), plus `default_authentication_plugin` (above).
- **Removed tooling**: `mysql_upgrade` (server self-upgrades), `mysqlpump`, `mysql_ssl_rsa_setup`.
  Removed plugins: `authentication_fido` (→ `authentication_webauthn`), `keyring_file`/
  `keyring_encrypted_file`/`keyring_oci`. `utf8mb3`/`utf8` charset now **deprecated** — migrate to
  `utf8mb4` (MySQL 9.x stops accepting the `utf8` name as a startup option).
- **RDS notes:** only **8.0 → 8.4** is supported as a major hop (no skipping); upgrades are never
  automatic. Use a new **`mysql8.4` parameter-group family** (cannot reuse `mysql8.0`) and audit it
  for the removed variables above. **Blue/Green Deployments** support the 8.0 → 8.4 hop and are the
  recommended low-downtime path. On 8.4.4+, **drop spatial indexes before upgrade** and recreate
  after.

### MySQL 5.7 end-of-life note
- **Community support ended 2023-10-31**; **RDS end of standard support 2024-02-29.** Still-running
  5.7 instances are auto-enrolled into **RDS Extended Support** (no downtime, frozen on the
  `5.7.44-RDS.<date>` line with AWS-backported critical/high CVE fixes).
- **Extended Support is paid and time-boxed:** billing started 2024-03-01; years 1–2 ran to
  2026-02-28, year-3 pricing from 2026-03-01, and **RDS Extended Support ends 2027-02-28** — after
  which RDS will **force-upgrade the major version** for you. Pricing is per-vCPU-hour and roughly
  *doubles* in year 3 (e.g. us-east-2 ~$0.10 → ~$0.20 /vCPU-hour; varies by region — check the RDS
  MySQL pricing page).
- **Why move now:** cost scales with vCPU count (large fleets get expensive fast), you only get
  critical-CVE fixes (no feature/bug work), and 5.7 → 8.0 is itself a heavy upgrade (data-dictionary
  rebuild, charset/collation, auth) — better planned than done under a force-upgrade deadline. For a
  fresh migration, target **8.4 LTS** directly rather than 8.0.

### MariaDB 10.6 → 10.11 (LTS → LTS, low-risk path)
Both LTS; 10.11 has a much longer support runway (RDS minors to 10.11.18, EOSS into mid-2027 vs the
10.6 track winding down sooner). Staying inside 10.x **avoids the 11.0 optimizer rewrite and the
SUPER-split breakage** — the conservative choice. RDS supports a direct in-place 10.6 → 10.11 hop.

- **`explicit_defaults_for_timestamp` default OFF → ON (10.10)** — *most impactful silent change in
  this path.* No implicit `DEFAULT CURRENT_TIMESTAMP` / `ON UPDATE`, NULL handling changes. Apps
  relying on legacy auto-timestamp behavior break silently — test all `TIMESTAMP` DDL.
- **New reserved word `ROW_NUMBER` (10.7)** — audit for columns/aliases named `row_number` (`OFFSET`
  was already reserved at 10.6). No further general reserved-word additions through 10.11.
- **Other default changes:** `--ssl` becomes the `mariadb` CLI default (10.10);
  `innodb_buffer_pool_chunk_size` autosized (10.11).
- **Deprecations/removals:** `DES_ENCRYPT`/`DES_DECRYPT` deprecated (10.10); `innodb_disallow_writes`
  removed (10.9); `innodb_change_buffering` deprecated (10.9 — foreshadows its 11.0 removal);
  `innodb_log_file_size` made dynamic (10.9). `utf8` still aliases `utf8mb3` (lever:
  `old_mode=UTF8_IS_UTF8MB3`).
- **New features you can adopt:** `UUID`/`INET4` data types, `JSON_EQUALS`/`JSON_OVERLAPS`/
  `JSON_NORMALIZE`, `NATURAL_SORT_KEY()`, `RANDOM_BYTES()`.
- **Compression caveat:** InnoDB/Mroonga tables using a **non-zlib** compression algorithm on 10.6
  are unreadable on 10.11 unless the matching library is present (managed on RDS, but verify).
- RDS runs the equivalent of `mariadb-upgrade` automatically as part of the managed upgrade.

### MariaDB 10.x → 11.x (major architecture change)
11.4 is the current LTS on RDS (RDS also offers 11.8). **11.0 is the breaking boundary** — treat this
as more than a version bump. RDS supports a direct in-place upgrade from any 10.6/10.11 instance
straight to 11.4/11.8.

- **New optimizer cost model (11.0) — the single biggest app-impacting change.** Cost is now
  *time-based* (storage-engine ops in ms, user costs in µs). The old hard-coded heuristics are gone
  (the "key lookups capped at 10% of rows" rule and InnoDB's "50% of rows in a range" cap). **Query
  plans can change after upgrade** — usually better, sometimes not. Worst-hit shapes: multi-table
  joins, ranges over >10% of a table, indexes with many duplicates, mixed-storage-engine queries.
  **#1 regression-test item** — capture `EXPLAIN`/`EXPLAIN ANALYZE` baselines before cutover. New
  tunables exist (`optimizer_disk_read_cost`, `optimizer_where_cost`, …; engine costs cache at
  table-load, so `FLUSH TABLES` after changing them).
- **SUPER privilege fully de-coupled (11.0.1)** — the granular privileges carved out in 10.5
  (`SET USER`, `CONNECTION ADMIN`, `BINLOG ADMIN`, `REPLICATION SLAVE ADMIN`, `READ_ONLY ADMIN`, …)
  are **no longer implied by `SUPER`**. Accounts/automation relying on `GRANT SUPER` for replication
  admin, read-only toggling, etc. start getting **access-denied** until granted explicitly. Most
  common admin/automation breakage.
- **InnoDB Change Buffer removed (11.0)** — `innodb_change_buffering` and
  `innodb_change_buffer_max_size` are **removed**; left in a parameter group they **block startup**
  (strip them from the 11.x parameter group). Behavioral change for write-heavy secondary-index
  workloads.
- **Default changes:** `innodb_undo_tablespaces` 0 → 3 (11.0); `histogram_type`
  `DOUBLE_PREC_HB` → `JSON_HB` (11.0, affects optimizer stats); `innodb_purge_batch_size` 300 → 1000
  (by 11.4). `utf8` still aliases `utf8mb3` (silent 4-byte/emoji truncation risk for teams expecting
  `utf8mb4`). No forced auth-plugin change at 11.0 (`ed25519`/`mysql_native_password` remain; 11.4
  adds `parsec`). *(`require_secure_transport=1` becomes default at **11.8**, not 11.4.)*
- **Reserved words:** `ROW_NUMBER` (from 10.7), `VECTOR` (from 11.6 — relevant only if target ≥ 11.6).
  No new general reserved words specifically at 11.0–11.4.
- **Deprecations:** all `innodb_defragment*` options, `innodb_file_per_table`, `innodb_flush_method`,
  `tx_isolation`/`tx_read_only` (→ `transaction_isolation`/`transaction_read_only`, 11.4). The
  `mysql*`-named CLI tools are aliases for `mariadb*` (e.g. `mysqldump` → `mariadb-dump`); legacy
  symlinks still ship but migrate scripts to the `mariadb-*` names.
- **RDS notes:** major upgrade requires a new/custom parameter group for the 11.x family and a
  customer-initiated reboot; RDS takes pre/post snapshots. **Use Blue/Green** to validate the new
  cost-model plans against production traffic before cutover.

### MySQL/MariaDB cross-engine note
MySQL and MariaDB diverged after 5.5 — treat **MySQL → MariaDB** (or vice-versa) as more than a
version bump: JSON type semantics, sequence support, auth plugins, and some functions differ.
Validate carefully.

---

## PostgreSQL

> RDS supports **skip-version** major upgrades (e.g. 13 → 15 in one step) via `pg_upgrade` or
> **Blue/Green** (Blue/Green switches to *logical* replication when the green is a higher major —
> needs source minor roughly ≥ 16.1 / 15.4 / 14.9). Targets are version-pair specific — confirm with
> `aws rds describe-db-engine-versions`. As of 2026-06, PG12 is Extended-Support-only (standard
> support ended 2025-02-28).

### PostgreSQL generic checklist
- **`pg_dump` from the lower version, restore into the higher** — run a `pg_dump` whose client
  version **matches or exceeds the target** server version.
- Watch **removed/renamed system catalogs and functions** across majors (e.g. `pg_stat_*` column
  renames; removed operators; `xml`/`json` handling changes).
- **`standard_conforming_strings`**, default `timezone`, and `bytea_output` defaults have shifted
  historically — confirm app assumptions.
- New **reserved keywords** are added per major — quote colliding identifiers.
- Re-run `ANALYZE` after import (statistics are not dumped).
- **Pre-upgrade `pg_upgrade` blockers:** open prepared transactions, unsupported `reg*` types,
  logical replication slots (must be dropped pre-PG17), invalid databases (`datconnlimit = -2`),
  millions of large objects. Single-AZ/Multi-AZ-instance read replicas upgrade with the primary;
  **Multi-AZ DB cluster** readers must be recreated.

#### PostgreSQL 12 → 13
- **btree deduplication** (space/perf win for low-cardinality indexes) — **not applied to indexes
  carried over by `pg_upgrade`**; `REINDEX` to benefit. Incremental sort (on by default) and
  hash-aggregate disk spill (`hash_mem_multiplier`) can change plans/memory.
- **Partitioning** improvements: better pruning, partitionwise joins across non-identical bounds,
  row-level BEFORE triggers, logical replication of partitioned tables.
- **App-breaking / silent:** `SIMILAR TO ... ESCAPE NULL` and `substring(... ESCAPE NULL)` now return
  **NULL**; `ALTER FOREIGN TABLE`/`ALTER MATERIALIZED VIEW ... RENAME COLUMN` return their own command
  tags (was `ALTER TABLE`) — breaks tag-parsing tooling; several wait events renamed.
- **Defaults:** `wal_keep_segments` **renamed** `wal_keep_size` (MB) — update custom parameter
  groups; `ssl_min_protocol_version` → TLSv1.2; `effective_io_concurrency` semantics changed
  (re-tune).
- **Removed:** `CREATE EXTENSION ... FROM` (package extensions *before* upgrading);
  pre-8.0 operator-class and `posixrules` timezone files. *(`WITH OIDS` and `recovery.conf` removal
  were PG12, not 13.)* No notable new reserved words.
- **RDS:** cleanest hop of the set; PG12 is Extended-Support-only so 12 → 13 is often forced — many
  customers skip straight to 15+.

#### PostgreSQL 13 → 14
- **`password_encryption` default → `scram-sha-256`** (was `md5`) — **biggest operational gotcha.**
  Affects only *newly set* passwords; old md5 hashes still work, but legacy drivers (old JDBC, libpq,
  pgbouncer) may fail SCRAM — verify client versions. On RDS the default is set by the parameter group
  (new PG14 default groups use scram-sha-256).
- **App-breaking / silent (heavy release):**
  - **`EXTRACT()` now returns `numeric`** (was `float8`) — silent type/precision shift downstream.
  - **Array functions changed `anyarray` → `anycompatiblearray`** (`array_append`, `array_cat`,
    `array_position`, …) — **user-defined aggregates/operators referencing them must be dropped &
    recreated** (`pg_upgrade` surfaces these).
  - **Postfix (right-unary) operators removed** — custom `CREATE OPERATOR (RIGHTARG=...)` invalid;
    **factorial `!`/`!!` removed** (use `factorial()`). Geometric `@`/`~` removed (→ `@>`/`<@`).
  - `pg_hba.conf` `clientcert=1/0/no-verify` removed → `verify-ca|verify-full`. Protocol v2 / SSL
    compression removed.
- **Defaults:** `checkpoint_completion_target` 0.5 → **0.9**. New predefined roles `pg_read_all_data`,
  `pg_write_all_data`, `pg_database_owner`. New features: multirange types, CTE `SEARCH`/`CYCLE`,
  JSONB subscripting, libpq pipeline mode.
- **Reserved words:** PG14 **reduced** keyword restrictions — no new hard-reserved words of concern.
- **RDS:** audit driver SCRAM support; array-function and postfix-operator breaks are the most common
  `pg_upgrade` failures here — run the precheck, drop/recreate dependent objects first.

#### PostgreSQL 14 → 15  ⚠️ HIGH-RISK PAIR
- **🚨 `public` schema `CREATE` revoked from `PUBLIC`** — the single most app-breaking change in the
  whole 12→17 range (CVE-2018-1058 mitigation). Non-owner roles **can no longer create objects in
  `public`** by default → `permission denied for schema public`. `public` is now owned by
  `pg_database_owner`.
  - **Critical RDS nuance:** applies to databases **created on** PG15. An in-place `pg_upgrade` of an
    *existing* DB **preserves the old permissive ACL** — so the break often surfaces only later, when
    teams create a *new* DB, restore a dump into a fresh DB, or stand up a Blue/Green green that
    re-initializes. **Test new-database creation explicitly.** Restore per-DB with
    `GRANT CREATE ON SCHEMA public TO PUBLIC;` (better: grant to the specific app role).
- **Stats collector process removed** — cumulative stats now in **shared memory**;
  **`stats_temp_directory` removed** (strip from custom parameter groups or the group is rejected).
- **App-breaking / silent:** login roles no longer get ADMIN OPTION on their own role by default;
  `123abc` (number+letters) is now a **syntax error**; interval fractional units round
  (`1.99 years` → `2 years`) — silent value change; `chr()` errors on negative input.
- **Defaults:** `log_checkpoints` off → **on**; `log_autovacuum_min_duration` -1 → **10min**;
  `hash_mem_multiplier` 1.0 → **2.0** (watch memory). New: **MERGE**, logical-replication row
  filters/column lists, server-side base-backup compression, `jsonlog`.
- **Removed:** **exclusive backup mode** (`pg_start_backup`/`pg_stop_backup` → `pg_backup_start`/
  `pg_backup_stop`); **PL/Python 2** (`plpythonu`/`plpython2u` → `plpython3u`); `stats_temp_directory`.
- **RDS:** highest-risk hop. Mandatory tests: (1) new-DB `public` CREATE behavior, (2) no
  `stats_temp_directory` in the parameter group, (3) PL/Python on Python 3, (4) plan for increased
  log volume.

#### PostgreSQL 15 → 16
- **Logical replication from a standby** (logical decoding on read replicas); parallel apply of large
  transactions. New **`pg_stat_io`** view for per-backend-type I/O observability.
- **ICU collation provider** can be the default and is now built by default — new PG16 databases may
  default to ICU; **collation-provider mismatches can change sort order and break collation-dependent
  indexes/queries.** Verify locale/collation provider on new RDS databases.
- **App-breaking / silent (privilege/role traps):**
  - **CREATEROLE no longer god-mode** — a CREATEROLE role can only grant/alter roles it has ADMIN
    OPTION on, and can only set CREATEDB/REPLICATION/BYPASSRLS if it holds them. Role-provisioning
    automation written for ≤15 frequently breaks. Interacts with `rds_superuser`.
  - **Role inheritance set at GRANT time** (`WITH INHERIT TRUE/FALSE`) per-membership — changes
    effective privileges after upgrade in mixed setups.
  - **Logical-replication apply now runs as the table owner** (not subscription owner); restore old
    behavior with `WITH (run_as_owner = true)`.
  - `REINDEX DATABASE`/`SYSTEM` no longer reindex system catalogs by default (use `reindexdb --system`).
- **Removed:** `force_parallel_mode` → `debug_parallel_query`; **`promote_trigger_file` removed**
  (use `pg_promote()`); `vacuum_defer_cleanup_age` removed. No notable new reserved words.
- **RDS:** lower app-surface risk than 14→15, but **role/privilege automation is the trap** — audit
  any tooling that provisions roles or owns subscriptions; confirm collation provider on new DBs.

#### PostgreSQL 16 → 17 (latest standard target; RDS also offers 18)
- **New `MAINTAIN` privilege + `pg_maintain` role** — delegate `VACUUM`/`ANALYZE`/`REINDEX`/
  `REFRESH MATERIALIZED VIEW`/`CLUSTER` without ownership (off the owner/`rds_superuser`).
- **Incremental file-system backup** (`pg_basebackup --incremental` + `pg_combinebackup`); VACUUM
  memory mgmt rewritten (no longer 1 GB-capped). **SQL/JSON**: `JSON_TABLE()`, `JSON_QUERY()`,
  `JSON_VALUE()`, `JSON_EXISTS()`. `pg_upgrade` now **preserves logical replication slots** (source
  17+) — eases CDC/DMS migrations.
- **App-breaking / silent:**
  - **Restricted `search_path` during maintenance ops** (`ANALYZE`/`CLUSTER`/`REINDEX`/`VACUUM`/
    `CREATE INDEX`/MV refresh). **Functions in expression indexes / materialized views that rely on a
    non-default schema can fail** unless they `SET search_path` at creation — common real-world break
    for index/MV-heavy schemas.
  - **`pg_stat_statements` columns renamed** (`blk_read_time` → `shared_blk_read_time`, etc.) and
    `pg_stat_bgwriter` loses `buffers_backend*` (moved to new **`pg_stat_checkpointer`**) — **breaks
    monitoring dashboards.**
  - `SET SESSION AUTHORIZATION` now checks superuser status at command time.
- **Defaults:** new params `transaction_timeout`, `allow_alter_system`, `summarize_wal`,
  `sync_replication_slots`. No notable new reserved words.
- **Removed:** **`adminpack` contrib removed** (drop it before upgrading — `pg_upgrade` blocker);
  **`old_snapshot_threshold` removed**; `db_user_namespace` removed.
- **RDS:** must-dos: (1) **drop `adminpack`** if present; (2) fix monitoring queries for renamed
  `pg_stat_statements`/`pg_stat_bgwriter` columns; (3) audit functions used in expression indexes/MVs
  for explicit `search_path`.

#### Extensions that may break across versions
- **The engine upgrade does NOT upgrade extensions.** After every major, for each one run
  `ALTER EXTENSION <name> UPDATE;`. Inventory with `SELECT * FROM pg_extension;`; check targets on the
  **target** engine via `SELECT * FROM pg_available_extension_versions;` (requires `rds_superuser`).
- **Minimum supported version per major** — an extension version fine on PG14 may be too old to load
  on PG16/17, so you may need to bump it *as part of* the upgrade. AWS may also **drop** an extension
  (≥12-month notice) — check it still exists on the target.
- **Special-case (don't just `ALTER EXTENSION`):** **PostGIS** has its own multi-step upgrade
  (`postgis_extensions_upgrade()`) — highest-risk extension; **`pg_repack`** must be dropped &
  recreated; **`pglogical`** replication slots must be dropped before the engine upgrade.
- **Validate explicitly:** `pg_stat_statements` (PG16→17 column renames — re-baseline dashboards);
  **`pgvector`** (fast-moving — confirm the target offers the index types/operators your app uses
  (e.g. HNSW), then `ALTER EXTENSION ... UPDATE`; reindex if the build format changed); `pg_cron`
  (confirm compatible version, re-validate jobs).
- **Process:** snapshot `pg_extension` (name+version) pre-upgrade, confirm each has a supported target
  version, sequence PostGIS/`pg_repack`/`pglogical` specially, then `ALTER EXTENSION ... UPDATE`
  everything immediately post-upgrade and smoke-test.

---

## Oracle

> **RDS for Oracle supports only 19c and 21c.** **19c is the long-term support release** (Premier
> support via RDS to 2029-12-31; BYOL Extended/ULA to 2032-12-31). **21c is an Innovation Release with
> a short runway** (RDS support ~2027-07; **not eligible for Extended Support**) — most customers
> should land on **19c** and skip 21c, or wait for 23ai. **23ai is not GA on standard RDS for Oracle**
> as of 2026-06 (verify at delivery time). RDS gives ≥12 months notice before a forced major upgrade.

> **Real-world method.** RDS has no host access, so you **cannot run DBUA / AutoUpgrade / `catctl.pl`**.
> Cross-version Oracle migrations to RDS typically use **AWS DMS (CDC)** or **Data Pump
> (`expdp`/`impdp`** via `DATA_PUMP_DIR` + the RDS-S3 integration option), optionally **GoldenGate**
> for near-zero downtime. This lets you provision a fresh target (CDB, AL32UTF8) and move data in —
> the logical-migration model this doc is about.

#### Oracle 12c → 19c (common upgrade path)
12.1/12.2 reached Oracle end-of-support in 2022 and are force-upgraded on RDS — the dominant
real-world migration.

- **Case-sensitive passwords / `ORA-28040: No matching authentication protocol`** — *the* most common
  post-upgrade failure. 19c phases out the 10G verifier and raises
  `SQLNET.ALLOWED_LOGON_VERSION_SERVER`; old clients/JDBC are rejected and accounts whose passwords
  were set under `SEC_CASE_SENSITIVE_LOGON=FALSE` (10G-only verifier) **must be reset**. Pre-check
  `DBA_USERS.PASSWORD_VERSIONS` for accounts showing only `10G`; upgrade clients/drivers.
  `SEC_CASE_SENSITIVE_LOGON` is **deprecated**.
- **Optimizer plan changes** — 19c CBO can produce different/worse plans. Mitigate with
  `OPTIMIZER_FEATURES_ENABLE='12.2.0.1'` and SQL Plan Baselines; **gather dictionary + fixed-object +
  system stats before cutover** (RDS docs explicitly recommend this to cut downtime).
- **App-breaking removals:** `DBMS_LOGMNR ... CONTINUOUS_MINE` **removed** (breaks legacy
  LogMiner/CDC — use DMS/GoldenGate); **`PRODUCT_USER_PROFILE` (PUP)** desupported (SQL*Plus command
  restrictions no longer enforced); **Oracle Multimedia (`ORDIM`)** desupported (store media as
  SecureFiles LOBs); **Oracle Streams** desupported; original `exp` deprecated; `DBMS_JOB` deprecated
  (→ `DBMS_SCHEDULER`).
- **Architecture:** **non-CDB is deprecated** (since 12.2) — direction is multitenant (CDB/PDB). On
  RDS you connect to the **PDB/tenant**, not `CDB$ROOT`.
- **Init params:** scrub **deprecated parameters** (`O7_DICTIONARY_ACCESSIBILITY`, older `SEC_*`,
  etc.) from the custom parameter group or the upgrade can stall;
  `SQLNET.ALLOWED_LOGON_VERSION_SERVER` default tightened (root of ORA-28040).
- **RDS notes:** recreate the **option group + parameter group** for `oracle-ee-19` (or
  `oracle-ee-cdb-19`) — RDS won't auto-migrate custom groups across majors. **Character set is fixed
  at creation** (default **AL32UTF8**) — a logical (DMS/Data Pump) migration is the clean way to
  consolidate legacy single-byte charsets onto AL32UTF8. **Timezone/DST file** isn't auto-upgraded —
  add the **`TIMEZONE_FILE_AUTOUPGRADE`** option (Data Pump raises **`ORA-39405`** if target TZ <
  source). Backup retention > 0 so RDS takes pre/post snapshots (no rollback — restore the
  pre-upgrade snapshot to a new instance).

#### Oracle 19c → 21c on RDS
21c is an Innovation Release; the only in-place RDS path is a **19c CDB → 21c** (21c is CDB-only).
SAs frequently advise **staying on 19c** given 21c's short runway and no Extended Support.

- **21c is CDB-only** — the non-CDB architecture is fully removed. A **19c non-CDB cannot go
  directly**: first convert it to a 19c CDB (requires the April 2021 RU or higher,
  `oracle-cdb-converting`), then upgrade. App/scripts assuming a non-CDB must be CDB/PDB-aware (RDS
  already connects you to a PDB endpoint, which softens this).
- **Removals/deprecations:** **Oracle Multimedia fully removed**; traditional auditing **deprecated**
  (use Unified Auditing — on RDS, enable auditing **per-PDB**, not from `CDB$ROOT`); original `exp`
  deprecated; OLAP deprecation continues. **`IGNORECASE` (ORAPWD) desupported in 21c** (10G-verifier
  removal continues).
- **New in 21c** (relevant to target choice): **native `JSON` datatype**, **blockchain/immutable
  tables**, enhanced In-Memory/sharding/in-DB ML — but these also exist/expand in 23ai, another reason
  to skip short-lived 21c.
- **Optimizer regressions** — same `OPTIMIZER_FEATURES_ENABLE` discipline; test SQL.
- **RDS notes:** target RU must be same-month-or-later than current; recreate option/parameter groups
  for the **CDB families** `oracle-ee-cdb-21` / `oracle-se2-cdb-21` (strip deprecated params first).
  Character set AL32UTF8, immutable; **CDB char set is always AL32UTF8** (only PDB can differ).
  **CDB limitations to flag:** connect to PDB endpoint only; auditing per-PDB; no Database Activity
  Streams in a CDB; option/parameter groups apply at CDB level. **SPBs (Supplemental Patch Bundles)
  are 19c-only** — not available for 21c.

> **Accuracy flag:** Oracle's live upgrade-guide URLs now redirect to a consolidated "26ai" page that
> attributes desupports to the latest release. For a customer-facing doc, re-verify the exact "first
> desupported in" version per item (esp. 21c reserved-word additions and the full 21c
> desupported-parameter list) against the 19c/21c New Features Guide for your specific RU. No major
> app-breaking SQL reserved-word additions are commonly cited for either hop — quote borderline
> identifiers regardless.

---

## SQL Server

> **RDS supports 2016, 2017, 2019, 2022** (2008/2012/2014 are gone). A single `modify` can hop
> multiple majors (e.g. 2016 → 2022 directly). Major upgrades are **manual only** and **irreversible**
> (RDS snapshots pre/post; rollback = restore the pre-upgrade snapshot to a new instance). EOS:
> 2016 → 2026-07-14, 2017 → 2027-10-12, 2019 → 2030-01-08, 2022 → 2033-01-11. **License Included only**
> (no BYOL); Web/Express don't support Multi-AZ.
>
> Version ↔ compat level: 2016 = 130, 2017 = 140, **2019 = 150, 2022 = 160**.

#### SQL Server compatibility level concept (the key risk lever)
- **What it controls** (per-database, independent of the engine binary): query-optimizer fixes (TF4199
  auto-on at the new default level), **Cardinality Estimator (CE) version**, and certain T-SQL
  semantics. Microsoft intentionally adds new plan-affecting behavior only at the new level to
  minimize upgrade risk.
- **RDS preserves each DB's compat level on upgrade** — existing DBs do **not** auto-jump to the new
  engine's level; only freshly created DBs (and `model`) get it. So a **2016-era DB (compat 130) runs
  fine on a 2022 engine**, deferring optimizer-plan regression risk.
- **Recommended workflow ("Query Store as flight recorder," SQL 2016+):** (1) upgrade the engine
  **keeping the old compat level** (gains Query Store + features, *not* new plans); (2) enable Query
  Store; (3) capture a baseline at the old level over a full business cycle; (4) **raise compat level**
  to expose the new optimizer; (5) use Query Store to find/fix regressions (force prior good plan; SQL
  2017+ Automatic Plan Correction automates this; revert compat level if needed). SSMS Query Tuning
  Assistant (QTA) compares across levels.
- **Force legacy CE without lowering compat:** `LEGACY_CARDINALITY_ESTIMATION = ON`
  (DB-scoped config / `USE HINT('FORCE_LEGACY_CARDINALITY_ESTIMATION')`).
- **Compat level does NOT restore discontinued functionality** — removed/renamed system objects and
  removed syntax (`*=`/`=*` joins, `FASTFIRSTROW`) error at any level. Minimum supported level on
  2016–2022 is **100**.

#### SQL Server 2016 → 2019
- **Default compat level 130 → 150** on new DBs (preserved on in-place/restore), silently activating
  the 2019 CE and all Intelligent Query Processing (IQP) features gated at 150.
- **Scalar UDF inlining (compat 150, ON by default)** — *largest single app-breakage surface.*
  Auto-rewrites scalar T-SQL UDFs into the calling query. Documented breakage: different query hash;
  previously-hidden warnings surface (divide-by-zero); some join hints become invalid; views over
  inlined UDFs can't be indexed; `SCOPE_IDENTITY()`/`@@ROWCOUNT`/`@@ERROR` can return different values
  (scope change); inlined-UDF variable used as `FORCESEEK` column → **error 8622**. Disable:
  `ALTER DATABASE SCOPED CONFIGURATION SET TSQL_SCALAR_UDF_INLINING = OFF`, per-function
  `WITH INLINE = OFF`, or `USE HINT('DISABLE_TSQL_SCALAR_UDF_INLINING')`. (`sys.sql_modules.is_inlineable`
  flags candidacy.)
- **Other IQP-at-150** (ON by default, no Query Store needed): **batch mode on rowstore** (plans can
  change; disable `USE HINT('DISALLOW_BATCH_MODE')`), **table-variable deferred compilation**
  (parameter-sniffing-style regressions if row counts vary; disable
  `USE HINT('DISABLE_DEFERRED_COMPILATION_TV')`), row-mode memory-grant feedback.
- **UTF-8 (`_UTF8`) collation support introduced** (char/varchar only; nchar/nvarchar stay UTF-16).
  **Silent truncation risk:** `varchar(n)` length `n` is *bytes* under UTF-8 — nvarchar→varchar(_UTF8)
  can truncate multi-byte chars. No default-collation change at install (opt-in).
- **Accelerated Database Recovery (ADR)** — opt-in; instant rollback / constant recovery, but more log
  + Persistent Version Store growth (size RDS storage accordingly).
- **Deprecated:** DQS, MDS. **Discontinued:** DB-scoped configs `DISABLE_BATCH_MODE_ADAPTIVE_JOIN`,
  `DISABLE_BATCH_MODE_MEMORY_GRANT_FEEDBACK`, `DISABLE_INTERLEAVED_EXECUTION_TVF`. No reserved-word
  additions (new syntax like `APPROX_COUNT_DISTINCT` is functions, not keywords).
- **RDS:** single-modify hop. **CLR migration blocker** — RDS supports CLR (SAFE only) on 2016 and
  lower but **not on 2017+**; CLR assemblies block a 2016 → 2017+ upgrade. 2016 EOS 2026-07-14 — plan
  now.

#### SQL Server 2019 → 2022 (latest on RDS)
- **Default compat level 160** for new DBs; restored/upgraded DBs **keep prior compat** (IQP/CE below
  activate only after explicit `ALTER DATABASE ... SET COMPATIBILITY_LEVEL = 160`). **Query Store ON
  by default** for newly-created DBs.
- **New IQP that silently changes plans (compat 160, ON by default):**
  - **Parameter Sensitive Plan (PSP) optimization** — multiple cached plans per parameterized
    statement. Variants run as prepared statements and **lose association with the parent module
    `object_id`** in `sys.dm_exec_*` (only resolvable via Query Store) — can break monitoring; can
    bloat plan cache. *Query Store AV under PSP fixed in 2022 CU7.* Disable:
    `PARAMETER_SENSITIVE_PLAN_OPTIMIZATION = OFF` / `USE HINT('DISABLE_PARAMETER_SENSITIVE_PLAN')`.
  - **CE feedback** (needs Query Store RW) — adjusts CE assumptions over executions; plans change
    silently. *Runaway plan-cache memory/CPU bug introduced CU8, fixed CU12 (2024-04-22) — relevant on
    builds CU8–CU11.* Disable `CE_FEEDBACK = OFF`.
  - **Memory-grant feedback percentile + persistence** — persisted in Query Store; may *increase*
    memory for oscillating workloads. **DOP feedback** is the only feedback feature **OFF by default**
    (enable `DOP_FEEDBACK = ON`).
- **Engine changes NOT gated by compat level (all editions):** VLF creation now 1 VLF (was 4) for
  growth ≤ 64 MB; **IFI applies to transaction-log growth up to 64 MB**; service "Automatic" silently
  runs as Automatic (Delayed Start).
- **Removed — big migration breaker:** **SQL Server Native Client (SNAC / SQLNCLI / SQLNCLI11)** is
  not shipped with 2022 — apps with SNAC/SQLNCLI connection strings break; migrate to **MSOLEDBSQL**
  (OLE DB Driver) or **MSODBCSQL** (ODBC Driver). **Deprecated:** Distributed Replay, Machine Learning
  Server, Stretch Database. **Discontinued:** Hadoop external data sources, PolyBase scale-out, Big
  Data Clusters. SQL-generated certs now default RSA 3072-bit (was 2048). No reserved-word additions
  (`DATE_BUCKET`, `GENERATE_SERIES`, `GREATEST`, `LEAST`, `JSON_OBJECT` are not reserved/gated).
- **RDS notes:** 2022 GA on RDS. **Many headline 2022 features are NOT supported on RDS:** S3
  backup/restore, External Data Source, TLS 1.3 & MS-TDS 8.0, SSAS, **database mirroring with Multi-AZ
  (Always On is the only Multi-AZ method on 2022)**. **Ledger** tables (off by default) block
  UPDATE/DELETE and can't be converted back.

#### Deprecated features per version (especially what breaks on RDS)
*DEPRECATED = still works, raises an event. DISCONTINUED = removed, breaks.* The 2016 engine-feature
deprecation list is still in force through 2022 (2019/2022 added no new per-version engine table).

- **App-breaking deprecated features:** **text / ntext / image** (+ `WRITETEXT`/`UPDATETEXT`/
  `READTEXT`/`TEXTPTR()`) → `varchar(max)`/`nvarchar(max)`/`varbinary(max)`; **`SET ROWCOUNT` for
  DML** → `TOP`; `SET FMTONLY` → `sp_describe_first_result_set`; legacy security procs
  (`sp_addlogin`, `sp_adduser`, `sp_grantdbaccess`, `sp_password`, …) → `CREATE/ALTER LOGIN/USER/ROLE`;
  `DBCC DBREINDEX`/`INDEXDEFRAG`/`SHOWCONTIG` → `ALTER INDEX` / `sys.dm_db_index_physical_stats`; all
  `sys*` legacy system tables → catalog views; SQL Trace → Extended Events; `SQLOLEDB` →
  `MSOLEDBSQL`; database mirroring → Always On AGs. Compat levels 100/110/120 are themselves
  deprecated.
- **SET options to be forced ON in a future version** (deprecated; "always ON" coming): `ANSI_NULLS
  OFF`, `ANSI_PADDING OFF`, `CONCAT_NULL_YIELDS_NULL OFF` — code relying on `= NULL` matching NULLs
  (`ANSI_NULLS OFF`) or trailing-blank trimming (`ANSI_PADDING OFF`) will break when this flips. *Not
  yet forced as of 2022.*
- **Discontinued (break now):** 2016 — 32-bit install, compat level 90, ActiveX subsystem,
  `BACKUP ... WITH PASSWORD/MEDIAPASSWORD`. Old-style outer joins `*=`/`=*` (only valid at compat 90)
  **fail on every supported version**.
- **Detection:** before upgrading, find real usage via
  `SELECT * FROM sys.dm_os_performance_counters WHERE object_name LIKE '%Deprecated Features%'`, or
  trace the `deprecation_announcement` / `deprecation_final_support` Extended Events.

#### SQL Server RDS — editions, collation, migration
- **No in-place edition change** (up or down): snapshot → create new instance of the target edition →
  migrate. Enterprise → Standard needs logical migration (native B/R or DMS) due to Enterprise-only
  feature deps; Standard capped at 24 cores.
- **Server collation set ONLY at instance creation** (`CharacterSetName`; default
  `SQL_Latin1_General_CP1_CI_AS`) and **cannot be changed afterward, even via snapshot restore** — to
  change it, create a new instance and migrate data in. Per-DB/table/column collation overridable via
  `COLLATE`; UTF-8 collations available at server level.
- **Native backup/restore (.bak via S3)** is the fastest migration method (RDS stored procs + IAM role
  + option group; single-DB granularity; supports TDE). Limits: bucket same Region; no native *log*
  backups; restore ≤ 64 TiB (5 TB/file); **Express restore capped at 10 GB**; max 2 concurrent tasks.
  For low-downtime/filtered migration use **AWS DMS**.
- **RDS does NOT support** (overlap with deprecated/blocked): xp_cmdshell/extended SPs, FILESTREAM &
  FileTables, Replication, Log Shipping, Maintenance Plans, PolyBase, Stretch DB, server-level
  triggers, `CREATE ENDPOINT`, database snapshots, DQS; **no OS/shell access, no sysadmin** (master
  user gets `db_owner`). **CLR — SAFE only on 2016 and lower, not on 2017+.**

---

## After the upgrade

> **Run the concrete post-upgrade validation checks in validation-patterns.md §4 →
> "### Post-Version-Upgrade Validation".** That subsection (gated on source major ≠ target major) has
> the actual queries/commands for the seven version-gap breaks — collation/sort-order *algorithm*
> change, auth-plugin compatibility, `sql_mode` strictness, reserved-word collisions in the schema,
> deprecated/removed functions in routines, default charset rendering (mojibake), and optimizer plan
> regression. Use the per-version detail below to know *which* of those apply to your specific hop.

1. `ANALYZE TABLE` (MySQL/MariaDB) / `ANALYZE` (PostgreSQL) / `DBMS_STATS` (Oracle) /
   `UPDATE STATISTICS` (SQL Server) — statistics are not carried by a dump.
2. Re-validate with object counts + `CHECKSUM TABLE`/aggregate checks across the version gap.
3. Run the application regression suite (or minimum CRUD round-trip per table) against the new
   version, watching for collation, sql_mode/optimizer, auth-plugin/protocol, and compatibility-level
   behavioral changes.
4. **Re-check query plans** against pre-upgrade `EXPLAIN` baselines (esp. MariaDB 11.x cost model,
   Oracle CBO, SQL Server CE / compat level) — silent plan regressions are the most common
   post-upgrade incident.
5. Recreate/update **extensions, option groups, parameter groups** on the target major; re-validate
   any monitoring queries that reference renamed catalog/DMV columns.
6. Record the source→target versions and any behavioral changes handled in `migration-plan.md`.
