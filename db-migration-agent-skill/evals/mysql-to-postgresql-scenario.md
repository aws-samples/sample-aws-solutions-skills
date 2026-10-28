# Eval Scenario — EC2 MySQL 8.0 → Aurora PostgreSQL (heterogeneous), near-zero downtime

## Simulated user prompt

> "We run MySQL 8.0 on EC2 (ap-northeast-2), ~120 GB, binlog ON (ROW). We want to move to
> Aurora PostgreSQL — licensing and standardization. The app is Spring Boot with JPA plus a
> few hand-written reports; it stores DATETIMEs in KST. We can pause writes for a couple of
> minutes and rollback must not lose data. Columns include `TINYINT(1)` flags, `BIGINT
> UNSIGNED` ids, `ENUM` status, `JSON` payloads, and an e-mail column with a unique index."

## Expected behavior checklist

**Routing**
- [ ] Classifies as **heterogeneous MySQL → PostgreSQL**; loads `mysql-postgresql.md` at
      Phase 2 (not only `heterogeneous-migration.md`) and the method-selection **H-rows**
      at Phase 3 — not homogeneous rows 1–18
- [ ] States up front that **application SQL review is mandatory** and produces
      `app-remediation-findings.md` as a handoff artifact (does not edit the app)
- [ ] Phase 0: Agent Toolkit / `dms-schema-conversion` chaining checked (heterogeneous prerequisite)

**Assessment (read-only)**
- [ ] Runs the §2 queries: risky types (A1), zero dates (A2), server semantics incl.
      `sql_mode`/`time_zone`/isolation (A3), case-insensitive uniques (A4), tables without
      PK (A5), generated/FULLTEXT/prefix indexes (A6), routines/triggers/events/views/
      AUTO_INCREMENT/cascading FKs/CHECKs (A7), identifiers vs PG reserved words and 63-byte
      limit (A8), binlog readiness (A9)
- [ ] Flags the e-mail unique index: enforced case-insensitively in MySQL, **not** in a
      deterministic PG collation → proposes `citext` / `lower()` unique index / ICU collation
- [ ] Notes DMS Schema Conversion supports MySQL ≤ 8.0 sources (fine here) and targets
      Aurora PG 14–17 (not 18) — target version chosen accordingly or manual DDL planned

**Method**
- [ ] Recommends **H3: DMS Full Load + CDC** into a **pre-created** schema
      (`TargetTablePrepMode` `DO_NOTHING`/`TRUNCATE_BEFORE_LOAD`), never `DROP_AND_CREATE`
- [ ] Rejects mysqldump/pg_dump/XtraBackup/binlog replication via the **engine-family gate**,
      and pgloader (H1) because it has no CDC
- [ ] Type decisions explicit: `TINYINT(1)` → `BOOLEAN` (not DMS's `SMALLINT`), `BIGINT
      UNSIGNED` → `NUMERIC(20)` (DMS's `BIGINT` overflows), `ENUM` → type/CHECK, `JSON` →
      `JSONB`, `DATETIME` semantics (naive KST) decided
- [ ] DMS replication instance C-family; LOB mode sized from measured JSON/TEXT maxima
- [ ] Rollback: **reverse DMS PostgreSQL → MySQL** presented with its real RPO terms (DML
      only, sequences re-seeded, no DDL), not "RPO 0" by assumption

**Provisioning**
- [ ] `rds.logical_replication=1` in the Aurora PG **cluster** parameter groups **at creation**
      (static; avoids a cutover-time reboot)
- [ ] DMS endpoints: MySQL source `verify-ca`/`verify-full` (never `require`), PG target
      `verify-full`, `AfterConnectScript` `session_replication_role=replica` for the full load

**Validation**
- [ ] Uses `cross_engine_compare.py` (not CHECKSUM TABLE vs md5) with
      `naive_timezone: "+09:00"`; explains the canonical forms and what MATCH proves
- [ ] DMS data validation enabled; `override-validation-function` only for deliberate transforms
- [ ] Soak (Phase 7.7) offers **A/B** (scripts support MySQL ↔ PG in cross-family mode, lag
      from DMS metrics), not C; `cross_engine_compare.py` included in the Lambda asset

**Cutover**
- [ ] Sequence/identity reset on PG after the final drain
- [ ] At the freeze: creates a logical slot on Aurora PG and records its LSN; reverse task
      starts from that LSN with `SlotName` (PG sources cannot start CDC from a timestamp)
- [ ] Reverse task rehearsed applying INSERT/UPDATE/DELETE onto the MySQL stand-in; reverse
      writer is a dedicated account with `awsdms_control` grants

## Anti-patterns (fail the eval if seen)
- Letting DMS create the target tables (`DROP_AND_CREATE`) for this pair
- Claiming the application will work unchanged
- Comparing `CHECKSUM TABLE` to `md5(string_agg(...))` as validation
- Promising a lossless rollback without `rds.logical_replication` set and a rehearsed reverse task
- Offering soak option C for a MySQL ↔ PostgreSQL pair as if the scripts couldn't check it
