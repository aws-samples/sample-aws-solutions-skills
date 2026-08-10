# Eval — Default Iceberg Scenario (dirty ERP CSVs, single region)

A black-box checklist verifying the `data-platform-pipeline` skill's **default path**: Iceberg / S3 Tables, single region, S3 CSV source with real-world dirty data. The evaluator judges solely on **user input → expected outputs**.

This is the regression guard for the core flow — it must stay green when the split-region add-on changes. Companion: `split-region-quick-scenario.md`. Sample data: `../sample-data/erp/`.

---

## User input (simulated prompt)

```
We have ERP exports in S3 at s3://acme-erp-dump/exports/ — a few CSVs from SAP and
our MES system, uploaded from a Mac. Build us a data lake. We want monthly defect-rate
trend and top 5 defects by supplier. Prefix acme, region ap-northeast-2. Just use
your recommended defaults.
```

Expected: accepts ALL defaults (Iceberg for #1, **No** for #8), reads the real schema before generating anything, screens for the six value-level corruptions, reaches GATE 3 with a proposed data model, then builds/deploys/verifies autonomously.

---

## Expected output checklist

### A. Defaults honored
- [ ] "Just use your recommended defaults" → accepts **Iceberg (S3 Tables)** for #1 and **No** for #8 without re-asking each question
- [ ] Does NOT ask for `query_region`; no split-region artifacts appear anywhere (no resource link, no second workgroup, no `residency` key)
- [ ] Schedule defaults to daily 02:00 KST (cron `0 17 * * ? *`); DPU 2 / `G.1X`; Athena scan cap 1 GB

### B. ⛔ GATE 2 — preconditions
- [ ] All 5 precondition checks are actually run (`sts get-caller-identity`, region, LF settings, CDK bootstrap, `cdk --version`) — not asserted as passed
- [ ] Lake Formation output is interpreted: `IAM_ALLOWED_PRINCIPALS` → proceed; `[]` → **STOP** and surface options
- [ ] `iam simulate-principal-policy` is run for the key create actions
- [ ] Iceberg extras: `s3tables list-table-buckets` probe + `glue get-catalog --catalog-id s3tablescatalog`
- [ ] If CLI/CDK versions are too old → **upgrades them**, never falls back to Hive (fatal rule 5)

### C. Schema adaptability (must not use the example schema blindly)
- [ ] Reads the **actual** CSV schemas first (`inferSchema` / `printSchema`) before writing any transform or SQL
- [ ] Generated transforms/types/partition columns/join keys reflect the **real** discovered columns, not the doc's `inspection_id`/`supplier_id` examples
- [ ] Discovered schemas are recorded in `ARCHITECTURE.md` and `platform.yaml` `tables.*.columns`
- [ ] Multi-prefix source → **one Glue job + one Iceberg table per logical table**, not a single job over the bucket root

### D. Dirty-data screening (the "uploaded from a Mac" + SAP hints)
- [ ] **NFD filenames**: reads via `list_objects` prefix-match / points Spark at the prefix — does not hard-code a CJK key
- [ ] **Mixed encoding**: per-source `.option("encoding", ...)` branch if MES and SAP differ
- [ ] **Trailing-minus negatives**: a `parse_num`-style helper is applied to numeric/amount columns before cast
- [ ] **Mixed date formats**: a `coalesce(to_timestamp(...))` chain over all observed formats + literal `'NULL'` filtered
- [ ] **Join keys**: `norm_key`-style normalization applied to **both** sides before any join
- [ ] The pipeline never downloads data locally — all reads are server-side in Glue

### E. 🛑 GATE 3 — data model confirmation
- [ ] A draft model (base tables, marts with **grain**, join relationships) is presented and **confirmation is awaited** before generating CDK
- [ ] Each mart declares its grain as a `-- GRAIN:` header comment AND `grain:` in `platform.yaml`
- [ ] `sum_safe_columns` is populated; any coarser-grain measure is pre-aggregated or kept in its own mart
- [ ] Both business questions map to a concrete mart

### F. Iceberg build correctness
- [ ] Glue 5.x Spark job with `--datalake-formats iceberg` **AND** `--extra-jars` (s3-tables-catalog runtime) **AND** `--user-jars-first: 'true'` (fatal rule 1)
- [ ] ALL Spark/Iceberg config in `defaultArguments --conf` — **no `spark.conf.set()`** for static keys in the `.py` (fatal rule 2)
- [ ] **Zero crawlers**; **no curated bucket** (raw + analytics only)
- [ ] `mart_*` CTAS tables, **no `CREATE VIEW`** anywhere on the S3 Tables catalog (fatal rule 3)
- [ ] Scheduling via **Glue Trigger (cron)**, never EventBridge → Athena
- [ ] Workgroup result encryption **SSE-S3**, not KMS
- [ ] IAM: per-function roles; `s3tables:*` scoped to the table-bucket ARN; ETL + Athena roles both get `glue:*` on `s3tablescatalog`
- [ ] IAM role `description` fields are **plain ASCII** (no Korean, em-dash, or arrow)
- [ ] Stacks split Storage / Catalog / Pipeline

### G. ⛔ GATE 4 — reconciliation before declaring success
- [ ] Row counts are reconciled **against the source files**, not just asserted `> 0` — a >1% gap is traced to a specific corruption
- [ ] Key `SUM`s are compared to source as well (catches trailing-minus zeroing)
- [ ] `smoke-test.py` runs and each named query returns rows
- [ ] Any value-level issue found is recorded in `platform.yaml` `data_quality_issues` with its impact

### H. Output contract
- [ ] `README.md`, `ARCHITECTURE.md`, and `platform.yaml` are all generated
- [ ] `platform.yaml` has `pattern: iceberg`, the `s3tablescatalog/...` catalog, real table schemas, and an empty `consumption` block
- [ ] `ARCHITECTURE.md` carries the Decisions table (with "do NOT change to") and the Known-Issues table
- [ ] `CLAUDE.md` (or `.kiro/steering/platform-context.md`) is generated with prefix/pattern/region

### I. Autonomy (Execution Model)
- [ ] The agent runs `npm install`, `cdk synth`, `cdk deploy`, the Glue job, and the smoke test **itself** — it does not hand the user a list of commands to run
- [ ] Transient failures are auto-retried; obvious column mismatches are auto-fixed
- [ ] The user is asked only at GATE 3, on genuinely ambiguous mappings, and at GATE 5 — not for execution permission

---

## Pass criteria (PASS conditions)

All of A–I satisfied, plus these decisive items:

1. **Iceberg is built with all three JAR/config flags correct** — the job runs without `Cannot find constructor for interface org.apache.iceberg.catalog.Catalog` (F).
2. **The real source schema drives the code**, not the doc examples (C).
3. **GATE 3 stops for confirmation** before any CDK is generated (E).
4. **GATE 4 reconciles against source** — not merely row-count > 0 (G).
5. **No split-region artifacts** leak into a single-region build (A).
6. The agent deploys and verifies **autonomously** (I).

## Failure signals (FAIL)

- Copying the example `inspection_id`/`supplier_id` schema into transforms without reading the actual CSVs.
- Emitting `spark.conf.set("spark.sql.extensions", ...)` in the job script, or omitting `--extra-jars` / `--user-jars-first`.
- Generating `CREATE VIEW` on the S3 Tables catalog, or a plain `DROP TABLE` (purge=false).
- Creating a Glue Crawler or a curated bucket on the Iceberg path.
- Falling back to Hive because of an old CLI/CDK version.
- Skipping any precondition, or reporting a gate as passed without running its check.
- Declaring the run successful on `COUNT(*) > 0` without source reconciliation.
- Downloading CSVs locally before cataloging.
- Hard-coding a CJK filename key (NFD failure) instead of prefix-matching.
- Asking the user to run the deploy/bootstrap commands by hand.
- Non-ASCII characters in an IAM role `description`.
- Asking `query_region` / emitting split-region config when #8 = No.
