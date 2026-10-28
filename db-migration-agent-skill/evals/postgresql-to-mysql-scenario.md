# Eval Scenario — EC2 PostgreSQL 15 → Aurora MySQL (heterogeneous), near-zero downtime

## Simulated user prompt

> "Our PostgreSQL 15 on EC2 (~60 GB) has to move to Aurora MySQL to match the rest of the
> company. Tables use `boolean`, `uuid` keys in some places, `jsonb`, `text[]` tags,
> `timestamptz`, unbounded `numeric` amounts, and a case-sensitive unique `handle` column.
> A few tables have no primary key. We need minutes of downtime at most, and a rollback
> path. Should we go to Aurora MySQL 8.4?"

## Expected behavior checklist

**Routing**
- [ ] Classifies as **heterogeneous PostgreSQL → MySQL**; loads `mysql-postgresql.md`
      (§PostgreSQL → MySQL) at Phase 2 and the **H-rows** at Phase 3
- [ ] Application SQL review mandatory (quoting, `||`, `LIMIT m,n`, upsert, booleans,
      isolation default READ COMMITTED → REPEATABLE READ) → `app-remediation-findings.md`

**Target decision**
- [ ] Presents Aurora MySQL 3 (8.0) vs 8.4 honestly: **DMS Schema Conversion targets only
      Aurora MySQL 8.0.32 / RDS MySQL 8.0.23** (converting for 8.4 is an unverified path to
      rehearse); 8.4 needs DMS 3.5.4+ and rejects `SslMode none`; 8.0 standard support ends
      2028-04-30
- [ ] `lower_case_table_names` decided **before** creating the cluster (SCT PG → MySQL needs
      `1`; fixed at Aurora MySQL 3 creation) — recorded as an immutable setting at GATE 2

**Assessment (read-only)**
- [ ] Runs §3 queries P1–P8; flags `boolean` (DMS default VARCHAR(5)), `jsonb`/`text[]`
      (DMS default text), unbounded `numeric` (DMS narrows it to NUMERIC(28,6) **on the
      source side** — a wider target DECIMAL doesn't help: declares NUMERIC(p,s) on the source
      as an A2 change or records an accepted loss, and proves it against real DMS for full load
      and CDC), `timestamptz` (offset lost), `timetz` (needs an explicit conversion decision),
      sequences/identity
- [ ] P7: finds `handle` values that collide under a `_ci` collation (e.g. `Bob`/`bob`) and
      chooses `utf8mb4_bin` for that column or escalates — not a silent load failure
- [ ] P2: key-less tables → their UPDATE/DELETE are ignored by DMS CDC unless
      `REPLICA IDENTITY FULL` (an A2 source change; test_decoding, not pglogical)

**Method**
- [ ] Recommends **H5: DMS Full Load + CDC**; source prerequisites `wal_level=logical`,
      slots/senders, `wal_sender_timeout`, heartbeat; notes pgloader cannot target MySQL
- [ ] PG BOOLEAN → TINYINT(1) via a pre-created column and/or a `change-data-type` rule;
      states `MapBooleanAsBoolean` does not apply to a MySQL target
- [ ] Rollback = **reverse DMS MySQL → PostgreSQL** with binlog ROW/FULL, `binlog retention
      hours` ≥ 24, start position from `SHOW BINARY LOG STATUS` (8.4) / `SHOW MASTER STATUS`
      (8.0), rollback-side PG tables kept with original types

**Validation**
- [ ] `cross_engine_compare.py` report per table (UUID ↔ CHAR(36), jsonb ↔ JSON, arrays ↔
      JSON compared canonically); explains that a DMS-default target would show type
      findings (boolean/array/JSON stored as text) and truncated `numeric` values
- [ ] Soak A/B available in cross-family mode; replication lag from DMS task metrics
      (task ARN resource-id dimension), not replica status

**Cutover**
- [ ] AUTO_INCREMENT reset on MySQL after the final drain; old PG sequences re-seeded on rollback
- [ ] Reverse task created stopped, connection-tested, rehearsed applying, GATE 4 precondition

## Anti-patterns (fail the eval if seen)
- Proposing a wider target DECIMAL as the fix for unbounded `numeric` truncation
- Calling `PluginName` `test_decoding` in a DMS endpoint setting (API value: `test-decoding`)
- Recommending pgloader for PostgreSQL → MySQL
- Assuming DMS Schema Conversion supports an 8.4 target
- Creating the Aurora MySQL cluster before settling `lower_case_table_names`
- Ignoring key-less tables in CDC planning
- Calling the copy validated from row counts alone
