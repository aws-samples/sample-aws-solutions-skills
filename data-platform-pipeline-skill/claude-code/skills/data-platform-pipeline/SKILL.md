---
name: data-platform-pipeline
description: |
  Build a production-ready serverless data lake pipeline on AWS. Creates S3 storage
  (3-bucket layout), Glue Catalog, ETL jobs, IAM roles, and Athena query layer.
  Use for AWS Data Lab builds, new data platform setup, or adding data sources
  to an existing lake. Triggers: "build data pipeline", "data lake setup",
  "ingest data", "ETL pipeline", "Glue job", "data platform", "serverless analytics".
---

# Data Platform Pipeline (AWS Serverless)

This skill builds the **ingestion → storage → catalog → query** layers of a serverless data platform. The output is a working CDK TypeScript project plus the Glue scripts and Athena DDL to run it. Downstream consumption (Quick Sight, dashboards, chat agents) is out of scope — this skill stops at "data is queryable in Athena."

The skill is opinionated: best practices are baked in, not presented as options. If a choice has a clear winner for serverless analytics on AWS, the skill picks it.

> **One deliberate exception.** If the catalog + query layers must live in a different region from storage (follow-up #8), the choice between the two mechanisms is **presented to the user, not decided** — it turns on a legal question (is data residency a mandate or a preference?) and a cost question that depends on customer-specific volume. See `reference/cross-region-query.md` §2.

---

## 🔴 CRITICAL RULES (never violate)

1. **S3 Tables catalog needs `--extra-jars` + `--user-jars-first: 'true'`** — Glue hard-fails (`Cannot find constructor for interface org.apache.iceberg.catalog.Catalog`) without the `s3-tables-catalog-for-iceberg-runtime` JAR. `--datalake-formats iceberg` alone is NOT enough.
2. **`spark.sql.extensions` is STATIC in Glue 5** — set ALL Spark/Iceberg config via the job's `--conf` in `defaultArguments`, NEVER via `spark.conf.set()` at runtime (fails with `Cannot modify the value of a static config`).
3. **No views on the S3 Tables catalog** — `CREATE VIEW` is unsupported (incl. cross-catalog refs from `AwsDataCatalog`). Use `mart_*` CTAS tables instead of `v_*` views on the Iceberg path.
4. **`DROP TABLE` (purge=false) is unsupported on S3 Tables** — use `DROP TABLE ... PURGE` or the S3 Tables API (`aws s3tables delete-table`).
5. **NEVER switch architecture due to tool versions** — upgrade the CLI/CDK instead. Tool versions are fixable in 2 minutes; architecture is permanent. Fall back to Hive only when the user explicitly opts in, or S3 Tables is genuinely unavailable in-region.

> Full failure modes, the supported/unsupported Athena DDL list, encoding rules, CDK gotchas, and the known-issues table → **`reference/gotchas.md`**.

---

> **Language**: Always respond in the language the user uses. Korean in → Korean out; English in → English out. Code and CDK output are always in English regardless of conversation language.

> **Execution Model**: This skill does NOT just generate code for the user to run manually. You ARE the builder — you have terminal access. Generate the CDK project, then:
> 1. Install dependencies (`npm install`)
> 2. Synthesize (`cdk synth`) — fix any errors before proceeding
> 3. Deploy (`cdk deploy --all --require-approval never`)
> 4. Run post-deploy verification (crawlers, queries, smoke tests)
> 5. If anything fails, diagnose, fix, and retry automatically
> 6. Only ask the user when a DECISION is needed (not for execution permission)
>
> The user provides business context and approves architecture decisions. YOUR role is to build, deploy, verify, and iterate until it works.
>
> | Agent does silently | Agent asks user |
> |---|---|
> | `npm install`, `cdk synth`, `cdk deploy` | "Deploy to production?" (if environment=production) |
> | Run crawlers, check schemas | "Column names don't match any known pattern — which mapping is correct?" |
> | Fix column mismatches (if obvious mapping) | "I found 3 possible interpretations. Which one?" |
> | Run smoke tests | Report results: "✅ All tables have data, views working" |
> | Auto-retry on transient errors | "This error persists after 3 retries: [error]. Need your input." |
> | Update ARCHITECTURE.md | — |

---

## Validation Gates

Five checkpoints where the build MUST stop. Two kinds, and the difference matters — see the Execution Model above:

- **⛔ AGENT-BLOCKING** — the agent verifies, fixes, and re-verifies **itself**. Do NOT ask the user for permission to proceed; ask only if a fix fails after retries. Never skip the check and never continue on a failure.
- **🛑 USER-BLOCKING** — the agent MUST present, stop, and **wait for an explicit answer**. These are business/legal/irreversible decisions the agent cannot make.

| Gate | Kind | When | Passes when | Detail |
|---|---|---|---|---|
| **GATE 1 — Scope & residency** | 🛑 USER | After §1 inputs + follow-ups, before ANY build | Storage pattern confirmed; `aws_region` set; **if #8 = yes**, `query_region` set AND the residency question answered *mandate* or *preference* AND the cross-boundary data-flow acknowledged | §1, `reference/cross-region-query.md` §2 |
| **GATE 2 — Preconditions** | ⛔ AGENT | Before `cdk synth` | All 5 precondition checks pass; LF not in strict mode (or user chose a path); IAM simulate shows no denies; tooling versions meet minimums | §1 ⚠️ MANDATORY block |
| **GATE 3 — Data model** | 🛑 USER | Before generating CDK/scripts | User has confirmed the base/mart table list, grains, and join keys | §1 Interactive data model design |
| **GATE 4 — Reconciliation** | ⛔ AGENT | After every pipeline run, before declaring success | Row counts + key SUMs reconcile against source within ~1%; every mart's declared grain matches its SQL; split-region: `query_region` COUNT == `aws_region` COUNT | §8 |
| **GATE 5 — Teardown intent** | 🛑 USER | Before any destructive command | User has explicitly confirmed data deletion, having been told what is deleted | §11 |

> **Gate discipline.** Never announce a gate as passed without running its check. If a gate fails, report *which* gate, *why*, and the fix — do not proceed to the next phase with a known failure and a note to fix it later. A skipped GATE 2 surfaces as an opaque deploy failure; a skipped GATE 4 ships wrong numbers that pass every structural test.

---

## Reference files (load on demand)

The core below is the default flow. Pull in a reference file when you reach its topic:

| File | When to read |
|------|-------------|
| `reference/iceberg-cdk.md` | Building the Iceberg path — full CDK (table bucket, IAM grants, Glue 5.x job + trigger, JAR upload, maintenance, teardown) |
| `reference/scripts.md` | Need any Glue job script, mart/view SQL, `run-views.py`, `smoke-test.py`, quality-check SQL, **dirty-data handling** (NFD filenames, mixed encoding, trailing-minus numbers, mixed date formats, join-key normalization, cross-source bridges, Excel normalization) |
| `reference/hive-pattern.md` | User opted into Hive — full path (3 buckets, crawlers, transform job, crawler bootstrap) |
| `reference/sap-sources.md` | Source is **SAP** (ECC / S/4HANA / BW / HANA) — native OData & HANA connectors, ODP delta, prerequisites, and the CSV-export fallback |
| `reference/vpc-connectivity.md` | JDBC source is on-prem or in a private subnet |
| `reference/cross-region-query.md` | Do the catalog + query layers need to be in a different region from storage (#8 = yes)? → read before building anything |
| `reference/gotchas.md` | Hit an opaque failure, or before generating Athena DDL on S3 Tables |

---

## 1. Prerequisites & Inputs

### Current state assessment (ask FIRST, before other questions)

Determine what already exists before any work. Present as an interactive choice:

```
What is the current state of your data platform?
  a) Starting from scratch — nothing exists yet
  b) Some infrastructure exists — I have an ARCHITECTURE.md or similar doc describing it
  c) Partial build — S3 buckets exist but no Glue/ETL yet
  d) Glue Catalog is set up — raw data is already cataloged, need ETL + views
  e) Pipeline exists — adding a new data source to existing platform
  f) Let me describe the current state: ___
```

- **(b):** Ask for the path to the architecture doc. Read it and incorporate existing state — do NOT recreate what exists.
- **(c)–(e):** Ask which components exist. Skip those steps.
- **(f):** Let them describe, then confirm your understanding before proceeding.

**Key principle:** Never deploy infrastructure that already exists. Always check first.

### Primary inputs — collect ALL before proceeding

| Input | Example | Notes |
| --- | --- | --- |
| `project_prefix` | `acme` | Lowercase, kebab-friendly. Naming convention for every resource. |
| `aws_region` | `ap-northeast-2`, `us-west-2` | Where the data lake lives — storage + ETL, and also catalog + query unless split (#8). |
| `query_region` (optional) | `ap-northeast-1`, `us-east-1` | **Defaults to `aws_region`.** Set only when #8 = yes. Owns the Glue resource link, Athena workgroup, and results bucket → `reference/cross-region-query.md`. |
| `source_type` | `jdbc` / `s3` / `sap` / `cdc` | Drives the decision tree in §3. `sap` → `reference/sap-sources.md` (native connectors exist — don't force it to `jdbc`). |
| `source_details` | see below | DB endpoint + Secrets Manager ARN, OR existing S3 path. |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by supplier" | Drives table selection and Athena view/mart design. |
| `downstream_consumer` | `bi` / `c360` / `ml` / `unknown` | **ASK THIS — do not make the user volunteer it.** Determines the mart contract. See "Downstream consumer" below. |

### Downstream consumer — ASK, then configure it yourself

> **Ask this right after `business_questions`, before the follow-up questions.** The downstream consumer changes the mart design, and retrofitting it means rewriting the mart jobs. The user should NOT have to know or state the technical requirements — you derive them.

```
What will consume this data?
  a) BI dashboards / natural-language Q&A (QuickSight, Amazon Quick)
  b) Customer 360 / identity resolution (unify customers across channels)
  c) ML / feature engineering
  d) Not sure yet — general-purpose analytics
```

Then apply the profile **silently** — announce the consequences, don't ask about them:

| Answer | What YOU configure without being told |
|---|---|
| **(a) BI** | Marts shaped to the business questions; `sum_safe_columns` declared; single-row KPI mart for cards. Consumption skill takes it from here. |
| **(b) C360** | 🔴 The full profile below. |
| **(c) ML** | Wide denormalized feature marts; keep raw grain; no aggregation-only marts. |
| **(d) Unknown** | Default BI shape; note in `ARCHITECTURE.md` that mart design may need revisiting. |

**On (b) C360 / identity resolution — apply ALL of these automatically:**

1. **Build a `mart_er_input` table** on Entity Resolution's 12-column contract (all lowercase): `variantid` (unique, ≤38 chars), `firstname`, `lastname`, `email`, `phone`, `dateofbirth`, `loyaltynumber`, `street`, `city`, `state`, `postalcode`, `country`, `sourcechannel`.
2. **Do NOT deduplicate customers across channels.** One row per (customer × source channel) — `variantid` = `V-{channel}-{id}`. Resolving those variants is Entity Resolution's job; if you merge them, the C360 build has nothing to do. **Say this to the user explicitly** — it looks like a bug otherwise.
3. **ASK the name order of EVERY source — it cannot be inferred.** `KIM MINHO` and `MINHO KIM` are indistinguishable as strings; only the source system knows its convention. Ask per source and pass it in: `family_first` (Korean-UI apps, SAP romanized exports), `given_first` (call-center free text, Western-facing forms), or `given_only` (app display names). Guessing produces opposite first/last names per channel, and a Name+Email rule then fails even with identical emails. Worked helper → `reference/gotchas.md`.
4. **Normalize phone country codes**, not just digits: `+82-10-…` and `010-…` must produce the same string.
5. **NULL out OTA/relay emails** (`*@booking.com`, `*@expedia*`) — they are per-booking aliases and will never match.
6. **Make the marts incremental** — `MERGE INTO` on a stable business key with an `updated_at` watermark, not a full CTAS rebuild. Customer attributes churn continuously.
7. **Add the ER export bridge** — Entity Resolution CANNOT read Iceberg/S3 Tables. Athena `UNLOAD` → Parquet → classic 2-level Glue table, on a dated prefix. Full mechanics + failure modes → `reference/gotchas.md` → "Downstream: AWS Entity Resolution".
8. **Tell the consumer to set `applyNormalization: false`** if any name may contain Hangul or other non-Latin script — Entity Resolution's own normalizer **strips Hangul entirely**, blanking the name fields. Record it as `er_apply_normalization: false` in `platform.yaml`.
9. **Record it** in `platform.yaml` as `downstream: {consumer: c360, er_input_table: ..., er_glue_table: ...}` so the C360 skill can discover it without being told.

> **If any source is in a VPC (Aurora, RDS, SQL Server on EC2, on-prem), create the VPC endpoints FIRST** — `s3` + `dynamodb` gateway, plus `s3tables` + `glue` + `secretsmanager` interface. Missing `s3tables` lets the job read every source successfully and then fail on the Iceberg write ~90 seconds in. Verified failure mode → `reference/vpc-connectivity.md`.

Then tell the user, in one line: *"I'll add a `mart_er_input` table shaped for Entity Resolution and keep channel variants unmerged — that's what the C360 step resolves."*

**`source_details` by type:**
- **JDBC**: `{ engine: "sqlserver"|"mysql"|"postgresql"|"oracle", host, port, database, secret_arn, tables: [...] }` — for Aurora/RDS, **probe the engine version rather than assuming one**: `aws rds describe-db-engine-versions --engine aurora-postgresql --region {region}` (a guessed version fails with `InvalidParameterCombination: Cannot find version …`).
- **Per-source name convention** (collect when the downstream is C360): `name_order: "family_first"|"given_first"|"given_only"` per source. Not inferable from the data — see the Downstream consumer section.
- **S3**: `{ bucket, prefix, format: "csv"|"json"|"parquet" }`
- **SAP**: `{ access: "odata"|"hana"|"s3_export"|"none_yet", instance_url, service_path, client_number, port, logon_language, auth: "basic"|"oauth2", entities: [...] }` — collect ALL of these for `odata`; a missing client number or service path blocks the build. See `reference/sap-sources.md` §2.1 for the customer-side prerequisite list.
- **CDC**: Out of scope — see §3. (Exception: **SAP ODP delta** is a connector option on a batch Glue job, not a streaming architecture — in scope, see `reference/sap-sources.md` §2.4.)

### Follow-up questions (ask after primary inputs, ONE AT A TIME, with a recommended default)

| # | Question | Recommended default |
|---|----------|---------------------|
| 1 | Storage pattern? | **Iceberg (S3 Tables)** — auto-compaction, ACID, time travel, schema evolution, no crawler. See §4. |
| 2 | Data volume? (rows/day + total) | **<1M rows/day, <100GB total** (DPU 2, standard for Data Lab) |
| 3 | Run frequency? (daily/hourly/weekly) | **Daily at 02:00 KST (17:00 UTC)** (off-peak) |
| 4 | Table relationships? (join columns) | **Infer from column names** (`product_code`, `supplier_id`, …; ask if ambiguous) |
| 5 | Code-to-name mappings? (status codes etc.) | **Yes, generate from source data** (query DISTINCT, propose mappings) |
| 6 | Partitioning strategy? | **Partition by date (year/month)** — optimal for time-series |
| 7 | Sensitive columns to mask/exclude? | **None** (add masking later via Lake Formation governance) |
| 8 | Catalog + query layer in a different region from storage? | **No — same region as storage** |

For question #1, present the two patterns explicitly:

```
Which storage pattern would you like to use?
  a) Iceberg / S3 Tables (recommended ✓) — automatic maintenance, ACID, time travel, schema evolution
  b) Hive (existing pattern) — S3 + Glue Crawler + Parquet. Use when you need existing Hive infra / compatibility
```

The choice determines the build: **Iceberg (default)** uses §4 + `reference/iceberg-cdk.md`; **Hive (opt-in)** follows `reference/hive-pattern.md`. Both share the `{prefix}_db` interface so the consumption layer is unaffected.

For question #8, present it as:

```
Do the catalog + query layers need to live in a different region from storage?
  a) No (recommended ✓) — everything in {aws_region}. Simplest, no cross-region cost.
  b) Yes — data must stay in {aws_region}, but a consumer service (BI, an AI/agentic
     feature, a downstream engine) is only available in another region
```

On **(a)** — the overwhelming majority — nothing changes; ignore `reference/cross-region-query.md` entirely. On **(b)**: collect `query_region`, then read **`reference/cross-region-query.md`** BEFORE generating anything. Ask its §2 Step 1 question ("is residency a regulatory **mandate** or a **preference**?") early — the answer eliminates one mechanism, and a zero-egress mandate means neither works and the build should stop.

> **Interaction pattern:** Present each question as a one-at-a-time multiple-choice prompt with the default highlighted. Do NOT dump all questions at once. If the user says "just use the defaults", accept ALL defaults (including **Iceberg** for #1 and **No** for #8) and proceed.

### 🛑 GATE 1 (USER-BLOCKING) — scope & residency confirmation

Summarize what you're about to build and **await explicit confirmation**. Do not run preconditions or generate code before the user answers.

```
Build plan:
  Prefix: {prefix} · Region: {aws_region} · Environment: {environment}
  Storage pattern: {Iceberg (S3 Tables) | Hive}
  Downstream: {BI | C360 / identity resolution | ML | general-purpose}
  Source: {source_type} → {n} table(s)
  Schedule: {cron} · Volume: {rows/day}
  Split-region: {No | Yes — catalog + query in {query_region}}
Proceed?
```

**If #8 = yes, GATE 1 additionally requires TWO acknowledgements** — these are policy decisions, never assumed:

1. **Residency driver** — the user must state whether keeping data in `{aws_region}` is a **regulatory mandate** or a **preference**. This is not a preamble; it eliminates a mechanism and gets recorded in `platform.yaml` as `residency`. Do not infer it from the region choice or the customer's industry.
2. **Cross-boundary data flow** — the user must acknowledge, in concrete terms, what leaves `{aws_region}`:
   - *Query in place:* data files stay put, but **aggregated result rows transit and are cached in `{query_region}`** (plus query metadata and any chat transcripts). This is **not** zero egress.
   - *Replication:* a **full copy of the mart tables** lives in `{query_region}`.

   If the mandate turns out to be **zero-egress**, STOP — neither mechanism qualifies. Recommend dashboards in `{aws_region}` without the region-restricted features rather than building something that does not meet the constraint.

Full decision flow, mechanism trade-offs, and the numbers to present → **`reference/cross-region-query.md`** §2.

**If the source is SAP, GATE 1 also requires the access path settled** — which SAP access **already exists** (OData service activated / HANA credentials granted / S3 export / nothing yet). OData and HANA prerequisites are SAP-side work owned by the customer's Basis team and can take days to weeks, so they cannot be assumed inside a short engagement. If nothing is ready, say so at GATE 1, build on a representative export, and hand over the prerequisite list as an action item — do NOT block the build, and do NOT present an export-only design as the production architecture. → `reference/sap-sources.md` §1, §5.

### ⛔ GATE 2 (AGENT-BLOCKING) — run ALL precondition checks before building

Do NOT skip any precondition, and do NOT start the build until every one passes. If ANY fails: (1) report which and why, (2) give the fix command, (3) STOP and wait — or fix it yourself if safe (e.g. `cdk bootstrap`), (4) re-run to confirm, (5) only then build. A missing prerequisite surfaces later as an opaque deploy/runtime failure that is far more expensive to debug.

```bash
# 1. Active identity matches the target account
aws sts get-caller-identity
# 2. Region is set
aws configure get region
# 3. Lake Formation Data Lake Settings (interpret below)
aws lakeformation get-data-lake-settings --region {aws_region} \
  --query 'DataLakeSettings.CreateDatabaseDefaultPermissions' --output json
# 4. CDK bootstrap status
aws cloudformation describe-stacks --stack-name CDKToolkit --region {aws_region} 2>/dev/null \
  || echo "CDK NOT BOOTSTRAPPED — run: cdk bootstrap aws://ACCOUNT_ID/{aws_region}"
# 5. CDK CLI ≥ aws-cdk-lib version pinned in package.json (mismatch → opaque synth failures)
cdk --version
```

**Lake Formation result:** output contains `"IAM_ALLOWED_PRINCIPALS"` → good. Output is `[]` → **strict mode** — STOP and surface options to the user (see `reference/gotchas.md` → Lake Formation strict mode).

**IAM permissions** — simulate the key create actions; STOP if any show implicit/explicit deny:
```bash
aws iam simulate-principal-policy \
  --policy-source-arn $(aws sts get-caller-identity --query Arn --output text) \
  --action-names iam:CreateRole s3:CreateBucket glue:CreateJob glue:CreateDatabase \
                 athena:CreateWorkGroup lakeformation:GrantPermissions --output table
```
(If `simulate-principal-policy` is itself denied, confirm the identity is admin/PowerUser with the user, or read the first AccessDenied from a no-op deploy.)

### Additional preconditions for the Iceberg path (default)

**Tooling versions:** AWS CLI ≥ 2.22, aws-cdk-lib ≥ 2.173. Detect, and **upgrade rather than fall back** (fatal rule 5) — full detect/upgrade commands in `reference/gotchas.md`.

```bash
# S3 Tables reachable in-region?
aws s3tables list-table-buckets --region {aws_region} >/dev/null 2>&1 \
  && echo "S3 Tables reachable" || echo "S3 Tables NOT available/no perm — confirm regional availability"
# Glue Data Catalog integration enabled? (REQUIRED for Athena to query S3 Tables)
aws glue get-catalog --catalog-id s3tablescatalog --region {aws_region} >/dev/null 2>&1 \
  && echo "S3 Tables Glue integration enabled" || echo "NOT integrated — enable before Athena can query"
```

- **Regional availability:** if `aws_region` doesn't support S3 Tables, offer to (a) switch region or (b) fall back to Hive.
- **Glue Data Catalog integration (REQUIRED):** one-time per account+region; registers the `s3tablescatalog` federated catalog. If missing, enable it before deploy — `aws glue create-catalog` command in `reference/iceberg-cdk.md`. Without it, the Glue write and any Athena query against `s3tablescatalog/...` fail.
- **IAM granularity:** S3 Tables IAM is **table-level** (`s3tables:*` on table-bucket/namespace/table ARNs). Grant ETL + Athena roles `s3tables:*` on the table bucket ARN plus `glue:*` on `s3tablescatalog` (see §5 / `reference/iceberg-cdk.md`).

### 🛑 GATE 3 (USER-BLOCKING) — interactive data model design

Before generating CDK/scripts, propose the data model plan and **wait for confirmation**:

**Step 1: Propose tables + marts with recommendations**

Based on the user's source data and business questions, propose:
```
Draft data model:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📁 Base tables (raw → cleaned):
  - base_{source}_{table} — [description]
  ...

📁 Mart tables (business-ready):
  - mart_{name} — grain: (col1, col2) — [description, which question it answers]
  ...

🔗 Join relationships:
  - base_X.col_a → base_Y.col_b
  ...

"Please confirm this structure is sufficient to answer your business questions.
Let me know if you need any additional analysis or changes."
```

**Step 2: Iterate**

User might say:
- "Add a per-supplier aggregation to mart_quality too"
- "This table isn't needed"
- "I need one more mart that joins raw_orders and raw_products"

→ Adjust and show updated plan.

**Step 3: Confirm + execute**

"Final data model confirmation: [plan]. Shall I start the build with this?"
→ Once approved, generate CDK + scripts + deploy autonomously.

---

## 2. Architecture Overview

Two storage patterns (chosen via follow-up #1):

- **Iceberg / S3 Tables (default)** — a **Glue 5.x Spark Job (Iceberg connector)** reads the source (raw S3 files in `{prefix}-raw-zone`, or a JDBC DB directly) and writes **straight into Iceberg tables in an S3 Table Bucket**, scheduled by a Glue Trigger (cron). No curated bucket, no crawler at any stage: types are declared in job code, Iceberg auto-registers schema on write, S3 Tables compacts automatically. Athena is query-only. Adds ACID, MERGE upserts, time travel. Data-flow diagram in §4.
- **Hive (opt-in)** — classic three-bucket layout (raw → curated Parquet → Athena), cataloged by Glue Crawlers. Diagram + full path in `reference/hive-pattern.md`.

Plus one optional topology add-on (chosen via follow-up #8):

- **Split-region catalog + query (opt-in)** — storage + ETL stay in `{aws_region}`; the catalog + query layers run in `{query_region}`. Layers on top of EITHER storage pattern. Two mechanisms — query in place via a Glue resource link, or replicate the mart layer — and **the user chooses; neither is a default.** Full path in `reference/cross-region-query.md`.

---

## 3. Decision Trees

### Source type → ingestion approach

```
User's source type?
├── SAP (ECC / S/4HANA / BW / HANA — "our ERP")
│   ├── ⚠️ Do NOT default to generic JDBC or assume CSV export is the only option.
│   │     Glue has NATIVE SAP connectors: SAP OData (source+target, ODP delta) and SAP HANA.
│   ├── Ask what already exists: OData service activated? HANA access granted?
│   │     Datasphere/BW already exporting to S3? Or only CSV/Excel exports?
│   ├── Prerequisites are SAP-side (Basis team, days-to-weeks) — surface at GATE 1,
│   │     and do NOT block the build if they aren't ready
│   └── → reference/sap-sources.md (decision tree, prereqs, connection options, delta)
│
├── JDBC (SQL Server / MySQL / PostgreSQL / Oracle) — RDS **or self-managed on EC2 / on-prem**
│   ├── Create Glue Connection (in VPC if source is private/on-prem — reference/vpc-connectivity.md)
│   ├── ℹ️ Self-managed is fully supported. If the user says "my DB isn't in the Glue Studio
│   │     list": the visual editor's "Amazon RDS" node IS the JDBC node, and this skill
│   │     generates code-based jobs anyway. NO crawler, NO source catalog table needed.
│   ├── Iceberg (default): Glue 5.x JDBC job (ingest-jdbc-iceberg.py) → writes Iceberg directly (CAN SKIP raw S3)
│   │     Hive (opt-in): ingest-jdbc.py → raw Parquet, then transform.py → curated
│   ├── Schedule via Glue Trigger (cron, daily 02:00 KST default) — NOT EventBridge + Athena
│   └── Output: Iceberg table in s3tablescatalog/{prefix}-table-bucket (Hive: Parquet in raw-zone)
│
├── Existing S3 data (CSV / JSON / Parquet)
│   ├── Iceberg (default): Glue 5.x Spark job (ingest-iceberg.py) reads raw S3 → writes Iceberg. No crawler.
│   │     Hive (opt-in): Glue Crawler on S3 path; CSV/JSON → transform → Parquet; Parquet → catalog only
│   ├── Schedule via Glue Trigger (cron)
│   └── ⚠ Multi-CSV under one prefix collapses into one table on the Hive crawler path — see reference/hive-pattern.md
│
└── CDC (Change Data Capture)
    └── OUT OF SCOPE. Recommend AWS DMS → S3 (Parquet), full-load + ongoing CDC,
        then re-run this skill with source_type="s3".
```

> **Multi-prefix → multi-job (one job per logical table).** A Glue job ingests ONE logical table — its files share a schema and land under one prefix (`{prefix}-raw-zone/{source}/{table}/`). When the source spans multiple prefixes that each hold a DIFFERENT schema (e.g. `.../sales/`, `.../suppliers/`, `.../inspections/`), generate **one ingest job + one Iceberg table per prefix**, not a single job over the bucket root. Each job declares its own typed transform and writes its own table into `{prefix}_db`; schedule them under one trigger (or chain via CONDITIONAL triggers if order matters). Only files that genuinely share a schema (e.g. monthly `2024-01.csv`, `2024-02.csv` under one prefix) belong to the same job. Mirror each as its own entry in `platform.yaml` `tables:` and its own `written_by` job.

### Format → transform needed?

```
Source format?
├── Parquet → catalog only (no transform job)
├── CSV / JSON / TSV / fixed-width → transform → Parquet+Snappy (Hive) / typed in Spark (Iceberg)
├── XLSX / Excel → Glue Python Shell job (pandas + openpyxl → Parquet). Spark has no native .xlsx reader.
│     Max 1 DPU. Files > 1GB → ask user to convert to CSV first.
└── XML / proprietary → custom Glue script with appropriate library
```

> **The pipeline never downloads data.** All processing is server-side via Glue (Spark). CSVs stay in S3 and are read directly — including multi-GB files. Never suggest downloading a CSV locally before cataloging.

### Mixed source formats (csv + xlsx under one bucket)

When the source contains BOTH CSV and Excel files, generate **two** Glue jobs chained by a CONDITIONAL trigger: `{prefix}-excel-preprocess` (Python Shell, 1 DPU) reads .xlsx → Parquet in the raw zone FIRST, then `{prefix}-iceberg-ingest` (Spark, 2 DPU) reads everything (original CSVs + converted Parquets) → writes Iceberg.

---

## 4. Storage Pattern: Iceberg (Default) — CORE FLOW

This is the **first architecture decision** (follow-up #1). **Iceberg is the default** — present it first. Fall back to Hive only on explicit opt-in (existing Hive infra, compatibility, or S3 Tables unavailable in-region) — then follow `reference/hive-pattern.md`.

### Iceberg vs Hive at a glance

| Component | Hive (opt-in) | Iceberg (default) |
|-----------|--------------|-------------------|
| Storage | S3 General Purpose (curated bucket) | S3 Table Bucket + Namespace |
| Cataloging | Glue Crawler (scheduled) | Iceberg auto-registers on write |
| ETL | Glue PySpark → raw, then transform → curated | **Glue 5.x Spark Job → writes Iceberg directly** |
| Incremental | Job Bookmark | MERGE INTO (native upsert) |
| Maintenance | Manual compaction | S3 Tables automatic compaction |
| Format | Parquet + Snappy | Parquet + ZSTD |
| DML / time travel / ACID | Overwrite partition / ❌ / ❌ | MERGE/DELETE/UPDATE / ✅ / ✅ |

> **Which path uses what?**
> - **Iceberg (default):** §4 core flow here + **`reference/iceberg-cdk.md`** (all CDK) + `reference/scripts.md` (job scripts) + §5 (IAM, add `s3tables:*`) + §7 (Athena workgroup) + §8–§13. **Skip ALL crawlers and the curated bucket.**
> - **Hive (opt-in):** **`reference/hive-pattern.md`** end-to-end (3 buckets, crawlers, transform job, crawler bootstrap).

### Schema adaptability — adapt to ANY schema, never freak out

The worked examples use the ERP **quality-inspection** sample schema (`inspection_id`, `supplier_id`, `result`, `inspection_date`, …). That is **illustrative only** — a template, not a contract.

- **Read the actual source schema FIRST**, before any transform or SQL. S3: `spark.read.csv(path, header=True, inferSchema=True).printSchema()`. JDBC: driver metadata / `SHOW COLUMNS`. Cataloged: `aws glue get-table`.
- **Adapt ALL transforms, types, partition columns, JOIN keys, mart SQL** to the real columns. Don't blindly copy the example columns.
- **If the schema differs from the examples, DO NOT error or refuse** — adapt to whatever tabular schema is present.
- **Pick the partition column from the real schema** — the most-queried date/timestamp column; else a low-cardinality dimension, or unpartitioned for small reference data.
- **Record the discovered schema in `ARCHITECTURE.md`** (column names + types per table).

Only STOP and ask when a column mapping is genuinely ambiguous (multiple plausible interpretations).

### Dirty real-world data — VALUE-level corruption (not just schema)

> 🔴 Schema adaptability above only handles **column-name / type** drift. Real Korean manufacturing ERP exports also carry **value-level** corruption that passes every structural check (row-count > 0, null-check, STRICT) yet silently produces WRONG numbers — e.g. a date format the parser misses dropped 16% of production rows, and trailing-minus costs cast to NULL zeroing every cost KPI. You MUST screen for these BEFORE trusting any aggregate. Full helpers + Spark snippets → **`reference/scripts.md` → "Dirty real-world data handling"**.

> **Most of these are SAP CSV-export artifacts.** If the source is SAP, check whether a **native connector** (SAP OData / SAP HANA) is available first — typed fields eliminate the numeric and date corruption at the source rather than parsing around it. → `reference/sap-sources.md`

**Screen for these six** — each has a worked Spark fix, numbered identically, in `reference/scripts.md` → "Dirty real-world data handling":

1. **Unicode NFD filenames** (CJK/Korean decomposed on macOS) → `NoSuchKey` on every Korean filename from a Mac
2. **Mixed encoding per source** (MES=EUC-KR + SAP=UTF-8 in one pipeline) → mojibake in dimensions
3. **SAP trailing-minus negatives** (`150.000-` = -150, often >50% of rows) → cast to NULL → cost KPIs become 0
4. **Mixed date formats** (incl. non-zero-padded `yyyy/M/d` and literal `'NULL'`) → rows silently dropped (16% loss seen)
5. **Join-key leading-zero / whitespace** (`10010015` vs `000…010010002`) → joins return 0 rows → empty dimensions
6. **Cross-source bridge, no common key** → the two sources cannot be joined at all

> **Rule:** the Iceberg/Spark default path reads empty CSV fields as `null` automatically, but it does NOT auto-fix any of the six. After ingest, run the GATE 4 reconciliation in §8 (source vs base table) — a gap means one of these silently dropped or zeroed rows.

### Mart grain declaration + downstream SUM safety

Single-grain marts hide this, but real marts mix grains. Putting a **coarser-grain** measure into a **finer-grain** mart duplicates it on SUM — e.g. a material-level QMEL notification count placed in a `(material × defect_code)` grain mart was duplicated by the number of defect codes per material (426 → 3,527, an 8.3× overcount) and every validation still passed.

**Every mart MUST declare its grain explicitly:**
- In the mart SQL: a header comment `-- GRAIN: (material_key, defect_code)`
- In `platform.yaml`: `grain: [material_key, defect_code]` (§10)

**Rule — never put a measure from a COARSER grain into a FINER grain mart without pre-aggregating first.**
- ❌ WRONG: `material_qmel_count` in a `(material × defect_code)` grain mart → duplicates on SUM
- ✅ RIGHT: pre-aggregate to material grain FIRST, then join as a 1:1 lookup, OR keep it in a separate material-grain mart

**Rule — if downstream will SUM a column, it MUST be valid to SUM across the mart's grain.** List the SUM-safe measures in `platform.yaml` under `sum_safe_columns: [...]` (§10). Any measure NOT in that list is correct only with `MAX`/`AVG`/`COUNT(DISTINCT)` or after collapsing to its native grain — the consumption skill reads `sum_safe_columns` and pre-aggregates KPI datasets accordingly.

> The single safest pattern for KPI cards is a **dedicated single-row KPI mart** (one row, every measure pre-aggregated with the correct function incl. `COUNT(DISTINCT …)`). Build it here in the Glue job; the consumption skill points KPI visuals at it. SQL → `reference/scripts.md`.

### Iceberg data flow

```
  ┌──────────────┐                                  ┌────────────────────────────┐
  │ S3 file src  │  ┌──────────────────────┐        │ S3 Table Bucket (Iceberg)  │
  │  CSV / JSON  │─▶│ {prefix}-raw-zone/   │─┐      │ {prefix}-table-bucket      │
  └──────────────┘  │   {source}/{table}/  │ │      │  namespace: {prefix}_db    │
                    └──────────────────────┘ │      │  tables: {table} (ICEBERG) │
                          ┌──────────────────▼────┐ │                            │
  ┌──────────────┐        │ Glue 5.x Spark Job    │─┼────────────────────────────│
  │ DB source    │───────▶│ (Iceberg connector):  │ │ writeTo / MERGE INTO       │
  │  JDBC        │  JDBC  │ read → transform →    │ │ (direct, schema-on-write)  │
  └──────────────┘  (skip │ write Iceberg         │ └──────────────┬─────────────┘
                    raw S3)└───────────────────────┘                │
                          ▲                                          ▼
                  Glue Trigger (cron, §6)        ┌─────────────────────────────┐
                                                 │ Athena Workgroup (QUERY ONLY)│
                                                 │  catalog: s3tablescatalog/…  │
                                                 │  auto-compaction by S3 Tables│
                                                 └─────────────────────────────┘
```

### Storage (Iceberg)

- **S3 Table Bucket** `{prefix}-table-bucket` + **Namespace** `{prefix}_db` (replaces the Glue Database, same name).
- Raw landing bucket `{prefix}-raw-zone` (CSV/JSON before ETL) + analytics bucket `{prefix}-analytics-zone` — created as in the Hive storage stack, just **omit the curated bucket**.
- **No curated bucket** — the Iceberg tables ARE the curated layer.

CDK for the table bucket + namespace → **`reference/iceberg-cdk.md`**.

### ETL (Iceberg) — Glue 5.x Spark Job writes directly to Iceberg

The default ETL is **one Glue 5.x Spark Job** that reads the source (raw S3 files, or a JDBC DB), types/cleans/filters in Spark, and writes **directly into the Iceberg table**. Athena is the **query engine only**. This matches AWS prescriptive guidance for batch ingestion into Iceberg/S3 Tables. Scheduled by a **Glue Trigger (cron)** (§6). Athena CTAS/INSERT is reserved as a **fallback for one-time exploratory loads**.

The Iceberg path uses **zero crawlers** — schema-on-write replaces inference, avoiding `_csv` suffixes, SerDe drift, type misdetection, and multi-CSV collapse. Types are declared in the `.py` script, version-controlled.

The connector reaches S3 Tables through the `s3tablescatalog` Spark catalog. Per 🔴 rules 1–2, ALL Spark/Iceberg config (`--datalake-formats`, `--conf`, `--extra-jars`, `--user-jars-first`) is set in the job's `defaultArguments`, NOT in the Python. Then `df.writeTo(...)` / Spark SQL `MERGE INTO` against `s3tablescatalog.{namespace}.{table}`.

**Three cases** (full script bodies in `reference/scripts.md`):
- **Case A — S3 file source:** `ingest-iceberg.py` reads raw CSV/JSON → writes Iceberg.
- **Case B — DB source:** `ingest-jdbc-iceberg.py` reads JDBC → writes Iceberg directly (skip raw S3 unless you need an immutable landing copy for replay/audit).
- **Case C — incremental/upsert:** Spark `MERGE INTO` inside the same job (no Job Bookmark — Iceberg snapshots self-track).

**No crawlers at any stage**, no Job Bookmark. Schema evolution lives on the table (`ALTER TABLE ADD/DROP COLUMNS`) — update the Glue job transform and the table evolves on next write.

**Fallback** — Athena CTAS/INSERT for one-time exploratory loads only (declare raw CSV as an explicit external table, then CTAS — and wrap every `CAST` in `NULLIF(col,'')`, see `reference/gotchas.md`). Not the production path. SQL in `reference/scripts.md`.

### No views — use materialized mart tables (CTAS)

Per 🔴 rule 3, `CREATE VIEW` doesn't work across the S3 Tables catalog. Materialize enrichment/aggregation as a `mart_*` CTAS table in the same namespace (best: as an extra `writeTo` inside the Glue job, so it's typed in code and refreshed on schedule). The consumption layer reads `mart_*` exactly as it would have read `v_*`. SQL in `reference/scripts.md`. On the **Hive** path, `v_{table}` views work normally — no mart needed.

### Maintenance, time travel, Athena S3-Tables constraints

- **Maintenance:** S3 Tables compacts automatically. Tune snapshot retention / file size via the maintenance API (`put-table-maintenance-configuration`) — NOT SQL. `OPTIMIZE`/`VACUUM` unsupported. Commands in `reference/iceberg-cdk.md`.
- **Time travel:** `FOR TIMESTAMP AS OF`, `$snapshots` metadata table. Examples in `reference/scripts.md`.
- **Athena DDL constraints (what you MUST NOT generate vs what IS supported):** see `reference/gotchas.md`.

### Convention coupling & catalog name (BOTH patterns)

`{prefix}_db` is the interface between pipeline and consumption, regardless of pattern. The only downstream difference is the **Athena catalog name**:

| Pattern | Athena catalog | Consumption connects to |
|---------|----------------|------------------------|
| Iceberg (default) | `s3tablescatalog/{prefix}-table-bucket` | `mart_*` CTAS tables |
| Hive (opt-in) | `AwsDataCatalog` | `v_*` views |
| Split-region, query in place (#8) | `AwsDataCatalog` in `{query_region}`, database `{prefix}_db_link` | `mart_*` through the resource link |
| Split-region, replicated (#8) | `s3tablescatalog/{prefix}-table-bucket` in `{query_region}` (local replica) | replicated `mart_*` |

Record which pattern was used in `ARCHITECTURE.md` (§10) — the consumption skill reads it to pick the catalog. Cross-catalog reference format:
- S3 Tables: `"s3tablescatalog/{prefix}-table-bucket"."{prefix}_db"."{table}"`
- Raw (AwsDataCatalog, fallback only): `"AwsDataCatalog"."{prefix}_raw"."{table}"`

---

## 5. IAM & Security (Lake Formation)

> ⚠ **IAM `description` is ASCII Latin-1 only** — Korean/em-dash/arrow rolls back the whole stack. Encoding rules table → `reference/gotchas.md`.

### Per-function IAM roles (mandatory)

| Role | Used by | Permissions (summary) |
| --- | --- | --- |
| `{prefix}-glue-crawler-role` | Glue Crawlers (Hive only) | S3 read on raw+curated, Catalog write |
| `{prefix}-glue-etl-role` | Glue ETL Jobs | S3 read/write on buckets, Catalog read+write, Secrets Manager (JDBC), CloudWatch Logs |
| `{prefix}-athena-query-role` | Athena queries | Catalog read, S3 read on curated, S3 write on analytics |
| `{prefix}-quicksight-role` | VPC/federated-query scenarios only | Usually NOT needed — see `reference/gotchas.md` |
| `{prefix}-query-role-{query_region}` | Athena in `{query_region}` (split-region only, #8) | Storage-region `s3tables:Get*`/`glue:Get*` + local results-bucket write → `reference/cross-region-query.md` §A4 |

### Lake Formation IAM-only mode

LF has two coexisting models: IAM-based (legacy, `IAMAllowedPrincipals`) and LF-managed (explicit grants). For batch analytics where IAM suffices, this skill configures **IAM-only mode** for deterministic behavior. The precondition check is in §1; strict-mode handling + the explicit `IAMAllowedPrincipals` grant CDK → `reference/gotchas.md`.

### Glue ETL role (excerpt)

```typescript
const etlRole = new iam.Role(this, 'EtlRole', {
  roleName: `${prefix}-glue-etl-role`,
  assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
  managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole')],
});
etlRole.addToPolicy(new iam.PolicyStatement({
  actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
  resources: [
    `arn:aws:s3:::${prefix}-raw-zone`, `arn:aws:s3:::${prefix}-raw-zone/*`,
    `arn:aws:s3:::${prefix}-curated-zone`, `arn:aws:s3:::${prefix}-curated-zone/*`, // Hive only
    `arn:aws:s3:::${prefix}-analytics-zone`, `arn:aws:s3:::${prefix}-analytics-zone/*`,
  ],
}));
// Only if JDBC source:
etlRole.addToPolicy(new iam.PolicyStatement({
  actions: ['secretsmanager:GetSecretValue'],
  resources: [props.jdbcSecretArn],
}));
```

> **Iceberg path:** the bucket-level policies above cover only raw + analytics. The Iceberg tables need **table-level** `s3tables:*` grants on the table-bucket ARN plus `glue:*` on the `s3tablescatalog` integration catalog, on BOTH the ETL and Athena roles. Full grant block → `reference/iceberg-cdk.md`. Scope `s3tables:*` to the specific bucket ARN — never `*`.

---

## 6. ETL Pipeline (scheduling)

JDBC connectivity (Glue Connection, on-prem/VPC prerequisites, `test-connection`) applies to **both** patterns when the source is JDBC → **`reference/vpc-connectivity.md`**. For a public/reachable endpoint, create the Glue Connection directly.

**SAP sources** use a Glue **SAP OData** or **SAP HANA** connection instead of raw JDBC — same job shape (read → type → `writeTo` Iceberg), different `create_dynamic_frame` call. SAP is rarely internet-reachable, so expect VPC configuration too. → **`reference/sap-sources.md`**

- **Iceberg (default):** ONE Glue 5.x Spark job per source that reads and **writes Iceberg directly** (`ingest-iceberg.py` / `ingest-jdbc-iceberg.py`). No separate transform job, no curated bucket. Full CDK (job + JAR upload + `--conf`/`--extra-jars`/`--user-jars-first` + cron trigger) → **`reference/iceberg-cdk.md`**.
- **Hive (opt-in):** two-job `ingest-jdbc.py` → `transform.py` flow with conditional trigger chaining → **`reference/hive-pattern.md`**.

Both schedule with a **Glue Trigger (cron)** — never EventBridge + Athena (Athena has no scheduler).

---

## 7. Query Layer (Athena)

> **Split-region (#8 = yes)?** Two hard constraints shape everything below: (1) **Athena resolves the Glue Data Catalog in its OWN region** — there is no way to point a workgroup at a remote catalog, so `{query_region}` gets a **resource-link database** whose target carries `{aws_region}`; (2) the results `OutputLocation` bucket **must be in the workgroup's own region** — only source data is remote. Build the workgroup below in `{query_region}` instead, and follow **`reference/cross-region-query.md`**.

### Workgroup config

```typescript
import * as athena from 'aws-cdk-lib/aws-athena';

new athena.CfnWorkGroup(this, 'Workgroup', {
  name: `${props.prefix}-workgroup`,
  state: 'ENABLED',
  workGroupConfiguration: {
    enforceWorkGroupConfiguration: true,
    publishCloudWatchMetricsEnabled: true,
    bytesScannedCutoffPerQuery: 1_000_000_000, // 1 GB cap — see §9
    resultConfiguration: {
      outputLocation: `s3://${props.analyticsBucket}/athena-results/`,
      encryptionConfiguration: { encryptionOption: 'SSE_S3' }, // NOT KMS — breaks DML on S3 Tables
    },
  },
  tags: [
    { key: 'project', value: props.prefix },
    { key: 'environment', value: props.environment },
    { key: 'owner', value: props.owner },
  ],
});
```

> ⚠️ Use **SSE-S3**, not KMS, for workgroups that write to S3 Tables (KMS result encryption breaks INSERT/MERGE/UPDATE/DELETE — `reference/gotchas.md`).

### Views (Hive) / marts (Iceberg) + named queries

- **Hive:** `v_{table}` enrichment views (`CASE` for code→name, `LEFT JOIN` for dimensions, `NULLIF` for divide-by-zero, `DATE_TRUNC` for rollups). DDL in `reference/scripts.md`.
- **Iceberg:** `mart_*` CTAS tables instead (rule 3).
- One named query per business question. ⚠️ `CfnNamedQuery.workGroup` does NOT auto-create a CFN dependency — use the token or `addDependency` (`reference/gotchas.md`).
- Views/marts are applied post-deploy via `scripts/run-views.py` (no `CfnView` construct exists). Script in `reference/scripts.md`.

### Post-deploy bootstrap — execute yourself, do NOT hand off

After `cdk deploy --all --require-approval never` succeeds, run the bootstrap yourself. If a step fails, diagnose, fix the code, redeploy, retry. Report a single summary at the end; don't narrate every step.

**Iceberg bootstrap (DEFAULT)** — no crawlers, no `v_*` views:

1. **Upload raw source to S3** (S3 file sources only; JDBC sources skip — the job reads the DB directly):
   ```bash
   aws s3 cp ./data/ s3://{prefix}-raw-zone/{source}/{table}/ --recursive
   ```
2. **Run the Glue 5.x Spark job** and poll until `SUCCEEDED` — it reads, types/cleans/filters, and writes the Iceberg table (auto-registered):
   ```bash
   aws glue start-job-run --job-name {prefix}-ingest-iceberg --region {region}
   # Poll get-job-run until JobRunState in (SUCCEEDED, FAILED, STOPPED)
   ```
   **Schema reconciliation:** verify actual source columns vs the types declared in the job transform. Differ → fix the `.py` transform (not crawler output/DDL) and re-run. Obvious mapping → auto-fix. Ambiguous → STOP and ask.
3. **Build/verify the mart tables** (`mart_*` replace `v_*`). Preferred: the Glue job writes marts as an extra `writeTo`. If via Athena instead:
   ```bash
   python3 scripts/run-views.py {prefix}-workgroup {prefix}_db athena-views/marts.sql {region}
   ```
4. **Run the smoke test** (§8) and **update ARCHITECTURE.md + platform.yaml** (§10).

**Hive bootstrap** (crawler-based, opt-in only) → full numbered sequence in `reference/hive-pattern.md`.

---

## 8. Data Quality Checks

> ⛔ **GATE 4 (AGENT-BLOCKING) — reconciliation before declaring success.** Do not report a pipeline run as good until: (1) row counts and key SUMs reconcile against the source within ~1%; (2) every mart's declared `-- GRAIN` matches its actual SQL grain; (3) split-region only — a `COUNT(*)` from `{query_region}` **equals** the same count in `{aws_region}`. Diagnose and fix failures yourself, then re-run. "Validation passed" without reconciliation is the exact failure this gate exists to catch.

After every pipeline run, run row-count, null-rate, date-range, duplicate-PK, and referential-integrity checks. Generate one set per curated table in `athena-views/quality-checks.sql`. SQL templates in `reference/scripts.md`.

> 🔴 **Row-count > 0 and null-checks are NOT enough — reconcile COUNTS against the source.** The dirty-data failures above (mixed date formats, trailing-minus, NFD filenames, unnormalized join keys) all leave a structurally valid table with the WRONG number of rows or zeroed measures. For each base table, compare `COUNT(*)` (and key `SUM`s) against the raw source file/row count; a gap beyond ~1% means rows were silently dropped or zeroed — trace it to the specific corruption (date parse miss? cast-to-NULL? join miss?) before declaring the run good. This is the producer-side half of the consumption skill's mandatory **KPI Numerical Accuracy Verification** — the two together close the "validation passed ≠ correct answer" gap. Reconciliation SQL → `reference/scripts.md`.

### Post-deploy smoke test

```bash
python3 scripts/smoke-test.py --prefix {prefix} --region {region}
```
Asserts each named query returns `rows > 0`, each table has `partition_count > 0`, and `SELECT … LIMIT 10` reads from the workgroup role; exits non-zero on failure. Generate it alongside the CDK app — spec + invocation in `reference/scripts.md`.

---

## 9. Cost Guardrails

| Lever | Default | Rationale |
| --- | --- | --- |
| Glue Job `numberOfWorkers` | 2 (`G.1X`) | First runs are small; scale up after measuring |
| Glue Job `timeout` | 120 min | Prevents runaway jobs billing for hours |
| Athena `bytesScannedCutoffPerQuery` | 1 GB | Forces partitioning + column selection; bump to 10 GB only with cause |
| S3 raw lifecycle | IA @ 90d, Glacier @ 365d | Raw rarely re-read after curation |
| S3 analytics MPU cleanup | 7 days | Cancelled Athena queries leave orphaned MPUs |
| Glue trigger schedule | Daily, off-peak | Don't ingest during business hours unless real-time |

### Monitoring

```typescript
// Glue job failure
new cloudwatch.Alarm(this, 'IngestJobFailure', {
  metric: new cloudwatch.Metric({
    namespace: 'Glue',
    metricName: 'glue.driver.aggregate.numFailedTasks',
    dimensionsMap: { JobName: `${prefix}-ingest-iceberg` }, // or {prefix}-ingest-jdbc on Hive
    statistic: 'Sum', period: cdk.Duration.minutes(5),
  }),
  threshold: 1, evaluationPeriods: 1,
  alarmDescription: 'Glue ingest job has failed tasks',
});

// Athena scan exceeds budget
new cloudwatch.Alarm(this, 'AthenaScanBudget', {
  metric: new cloudwatch.Metric({
    namespace: 'AWS/Athena', metricName: 'ProcessedBytes',
    dimensionsMap: { WorkGroup: `${prefix}-workgroup` },
    statistic: 'Sum', period: cdk.Duration.hours(1),
  }),
  threshold: 100_000_000_000, evaluationPeriods: 1, // 100 GB/hour
});
```

---

## 10. Output Contract

The skill MUST generate a `README.md`, an `ARCHITECTURE.md`, and a `platform.yaml` at the project root. Downstream skills rely on these conventions. A consuming tool only needs `{prefix}` and `{region}` — everything else is derived.

### `README.md` — naming convention

```markdown
# {prefix} Data Platform — Pipeline Layer
## Output Contract
### Storage Pattern
- Pattern: `Iceberg (S3 Tables)`  OR  `Hive`     <-- record which one was built
### Naming Convention
- Project prefix: `{prefix}` · Region: `{region}` · Environment: `{environment}`
- Split-region (only if #8 = yes): storage + ETL in `{region}` · catalog + query in `{query_region}` · mechanism: `{resource_link|replication}`
```

**Storage + catalog — Iceberg (default):**
```markdown
### Storage (Iceberg)
- Table Bucket: `{prefix}-table-bucket` · Namespace: `{prefix}_db`
- Raw landing: `s3://{prefix}-raw-zone/` (S3 file sources only) · Analytics: `s3://{prefix}-analytics-zone/`
### Catalog
- S3 Tables catalog: `s3tablescatalog/{prefix}-table-bucket` · Database/Namespace: `{prefix}_db`
- Tables: `{table}` (Iceberg, PARQUET+ZSTD) — written by `{prefix}-ingest-iceberg`
- Marts: `mart_{table}` (CTAS) — replaces `v_{table}` views (unsupported across S3 Tables catalog)
- Raw external tables: none (only if Athena CTAS fallback used: `raw_{table}` in `{prefix}_raw`)
```

**Storage + catalog — Hive (opt-in):**
```markdown
### Storage (Hive)
- Raw: `s3://{prefix}-raw-zone/{source}/{table}/` · Curated: `s3://{prefix}-curated-zone/{table}/`
- Analytics: `s3://{prefix}-analytics-zone/athena-results/`
### Glue Catalog
- Database: `{prefix}_db` · Raw: `raw_{source}_{table}` · Curated: `{table}` · Views: `v_{table}`
```

**Shared for both:**
```markdown
### Athena
- Workgroup: `{prefix}-workgroup` · Results: `s3://{prefix}-analytics-zone/athena-results/` · Scan cap: 1 GB
### IAM Roles
- Crawler: `{prefix}-glue-crawler-role` (Hive) · ETL: `{prefix}-glue-etl-role`
- Athena: `{prefix}-athena-query-role` · Quick Sight: `{prefix}-quicksight-role` (only if VPC/federated)
### Validation
See `athena-views/quality-checks.sql`. Run after each pipeline run.
### Schedule
Daily ingest at 02:00 KST (17:00 UTC). Glue trigger: `{prefix}-daily-trigger`.
```

### `ARCHITECTURE.md` — human-readable architecture record

Create on first run; **READ it first** on re-run; update EVERY time infrastructure changes. Single source of truth for "what exists." Capture: current state (buckets, tables w/ row counts, jobs, schedules, **discovered schemas**), the chosen pattern + catalog name, a Decisions table (why + "do NOT change to"), and a Known-Issues table (copy from `reference/gotchas.md`), plus a change log. Template:

```markdown
# {prefix} Data Platform — Architecture Record
## Current State (Last updated: {YYYY-MM-DD})
### Architecture Pattern
- `Architecture Pattern: Iceberg (S3 Tables)`  OR  `Architecture Pattern: Hive`
  (consumption layer reads this to pick the Athena catalog)
### Storage Layer / Catalog / ETL / Query / IAM / Lake Formation
[fill per pattern — Iceberg: table bucket + raw + analytics; Hive: raw + curated + analytics]
- Catalog name in Athena: `s3tablescatalog/{prefix}-table-bucket` (Iceberg) OR `AwsDataCatalog` (Hive)
- Views/marts: Hive → `v_{table}`. Iceberg → `mart_{table}` CTAS.
- Trigger: `{prefix}-daily-trigger` (cron 0 17 * * ? *)
## Decisions (why, not just what)
| Decision | Rationale | Do NOT change to |
|----------|-----------|-----------------|
| Iceberg / S3 Tables | ACID + time travel + auto-compaction; no crawler drift | Hive (unless pre-existing Hive infra) |
| Glue 5.x Spark (not Athena CTAS) | Typed transforms in code, schedulable, MERGE | Athena-only ETL (no scheduler) |
| ZSTD compression | Better ratio than Snappy for analytics | Snappy (Hive default only) |
| Single DB `{prefix}_db` | Clean LF grant boundary; convention discovery | `default` database |
| Glue Trigger (cron) | Native Glue scheduler | EventBridge → Athena (no scheduler) |
[split-region only — pick the row matching the chosen mechanism:]
| Query in place (Glue resource link) | Data files never leave {region}; user chose this for residency | Replication (copies data out of {region}) |
| Replicated mart layer to {query_region} | Cheaper at this scan volume; residency is a preference, not a mandate | Resource link (needless per-scan transfer here) |
## Data Residency Disclosure
[split-region only] Mechanism: {resource_link|replication}. Residency driver: {mandate|preference}.
Stays in {region}: [source of record; base_* and mart_* data files].
Crosses to {query_region}: [aggregated result rows cached in SPICE; query metadata; chat transcripts
— plus a full copy of mart_* if replication]. Approved by: {name/role, date}.
## Known Issues & Gotchas
[copy the table from reference/gotchas.md]
## Change Log
| Date | Change | By |
```

### `platform.yaml` — machine-readable state manifest

Downstream tools and the consumption skill read this to auto-discover catalog, tables, column types, and join keys without parsing prose. Generate on first deploy; **READ + merge** if it exists; update on EVERY change. The `tables` section must reflect the **actual discovered schema**, not the examples. The `consumption` section starts empty (the consumption skill fills it).

```yaml
platform:
  prefix: "{prefix}"
  region: "{region}"
  pattern: "iceberg"  # iceberg | hive
  catalog: "s3tablescatalog/{prefix}-table-bucket"  # or AwsDataCatalog for Hive
  created: "YYYY-MM-DD"
  updated: "YYYY-MM-DD"
  # --- split-region block: OMIT ENTIRELY unless follow-up #8 = yes ---
  query_region: "{query_region}"                  # where catalog + query run
  cross_region_mechanism: "resource_link"          # resource_link | replication
  residency: "mandate"                             # mandate | preference — WHY the mechanism was chosen.
                                                   # Never "optimize" a mandate-driven resource_link into
                                                   # replication; it breaks a compliance constraint.
  query_catalog: "AwsDataCatalog"                  # in {query_region}
  query_database: "{prefix}_db_link"               # the resource link (resource_link only)
storage:
  table_bucket: "{prefix}-table-bucket"  # Iceberg only
  raw_zone: "{prefix}-raw-zone"
  analytics_zone: "{prefix}-analytics-zone"
tables:
  quality_inspections:  # example — adapt to actual tables
    type: iceberg_mart  # iceberg_mart | iceberg_base | hive_curated | hive_raw
    columns: {inspection_id: bigint, supplier_id: string, inspection_date: date}
    partition: "month(inspection_date)"
    join_keys: {supplier_id: "suppliers.supplier_id"}
    written_by: "{prefix}-ingest-iceberg"
  mart_defect_root_cause:  # multi-grain mart example — grain + SUM-safety are MANDATORY
    type: iceberg_mart
    grain: [material_key, defect_code]   # explicit grain — consumption reads this to avoid SUM duplication
    sum_safe_columns: [defect_count]     # columns valid to SUM across this grain
    # NOT sum-safe at this grain: material_qmel_count (coarser grain → duplicates on SUM)
    single_row: false                    # true only for a dedicated 1-row KPI-summary mart
    validation_sql: "SELECT SUM(defect_count) FROM mart_defect_root_cause"  # ground-truth aggregate
    written_by: "{prefix}-ingest-iceberg"
data_quality_issues:  # record every value-level issue found + its impact (read by consumption)
  - table: base_mes_production
    issue: "start_time NULL for 511 rows (4.1%)"
    impact: "Excluded from cycle-time calculations"
  - table: base_sap_orders
    issue: "planned_end_date missing for 133 orders (16%)"
    impact: "Cannot calculate delay for these orders"
etl:
  "{prefix}-ingest-iceberg":
    glue_version: "5.0"
    workers: 2
    schedule: "cron(0 17 * * ? *)"
lineage:
  - "raw_quality_inspections -> mart_quality_summary -> quality-dataset(SPICE) -> quality-dashboard"
downstream:                              # who consumes this — asked in §1, not volunteered
  consumer: "c360"                       # bi | c360 | ml | unknown
  er_input_table: "mart_er_input"        # c360 only — the 12-column ER contract mart
  er_export_prefix: "s3://{prefix}-analytics-zone/er-input/"
  er_glue_table: "{prefix}_er.er_input"  # classic 2-level Glue table ER reads (NOT Iceberg)
  variants_unmerged: true                # c360: one row per (customer x channel) — ER resolves them
consumption:
  quicksight_region: "{region}"          # = query_region when #8 = yes
  er_apply_normalization: false           # c360: FALSE when names contain Hangul/non-Latin —
                                          # ER's normalizer strips Hangul and blanks name fields
  requires_spice: false                  # TRUE when cross_region_mechanism=resource_link —
                                         # consumption MUST NOT use DIRECT_QUERY (every visual
                                         # interaction would bill a cross-region scan)
  spice_refresh: "DAILY 04:00 Asia/Seoul"
  datasets: {}
  dashboards: {}
  topics: {}
```

> **Why both?** `ARCHITECTURE.md` is for humans (prose, rationale); `platform.yaml` is for machines (parseable, no ambiguity). Keep them in sync.

---

## 11. Teardown

> **Iteration tip — separate stacks.** CFN rolls back the whole stack on any resource failure. Split into **StorageStack** (very stable), **CatalogStack** (stable: DB, crawlers, IAM), **PipelineStack** (changes every iteration: jobs, triggers, named queries, views). View/mart iteration won't roll back upstream resources. Cross-stack refs via `props`.

> 🛑 **GATE 5 (USER-BLOCKING) — teardown intent.** Never run a destructive command without an explicit confirmation that names what will be deleted: which buckets, which tables, and that the data is unrecoverable. "Clean up" or "we're done" is NOT sufficient authorization — state the blast radius and wait.

Teardown is destructive. **You run these yourself when asked, but always confirm intent first** ("This will delete S3 data + Glue catalog. Confirm?"). Buckets use `RemovalPolicy.RETAIN` so `cdk destroy` can't accidentally delete data.

```bash
# 1. Empty S3 buckets (CDK can't delete non-empty RETAIN buckets)
aws s3 rm s3://{prefix}-raw-zone --recursive
aws s3 rm s3://{prefix}-curated-zone --recursive   # Hive only
aws s3 rm s3://{prefix}-analytics-zone --recursive
# 2. Delete buckets if desired
aws s3 rb s3://{prefix}-raw-zone ; aws s3 rb s3://{prefix}-analytics-zone
# 3. Delete Glue database (drops all tables/views — confirm first)
aws glue delete-database --name {prefix}_db
# 4. Deregister Lake Formation resources (only if registered)
aws lakeformation deregister-resource --resource-arn arn:aws:s3:::{prefix}-raw-zone || true
# 5. CDK destroy — roles, jobs, crawlers, workgroup, triggers
cdk destroy --all
```

> **Iceberg adds a step:** delete the S3 Table Bucket contents (tables → namespace → table bucket) before `cdk destroy`. Commands in `reference/iceberg-cdk.md`.

> **Split-region adds a step, and it goes FIRST:** tear down `{query_region}` before touching storage — delete the resource link, empty + delete the query-region results bucket, `cdk destroy QueryStack` (and stop replication first under Mechanism B). Deleting a resource link touches no data. Full sequence → `reference/cross-region-query.md` §9. **Cross-account:** revoke the LF grant / RAM share (Mechanism A) or the destination bucket policy (Mechanism B) as well — the consumer account keeps access until you do.

For partial teardown (remove one source), use CDK context flags instead of destroy: `cdk deploy --context source:sqlserver=remove`.

---

## 12. Batch-Only Scope

This skill covers **batch** ingestion/transformation. For real-time/streaming: `Kinesis Data Streams → Firehose → S3 (Iceberg) → Athena` — a separate architecture, not covered here. Don't bolt Kinesis onto this pipeline. For sub-daily batch of small data, schedule the Glue job more frequently (hourly is fine); below 15 minutes, switch to streaming.

---

## 13. When to Add Governance

Don't enable LF strict mode / LF-TBAC / cross-account sharing by default. Triggers:

| Trigger | What to add |
| --- | --- |
| Downstream is **C360 / AWS Entity Resolution** | A `mart_er_input` table on ER's 12-column contract + an Athena `UNLOAD` → Parquet → classic Glue table bridge (**ER cannot read Iceberg**). Do NOT dedup customers — that is ER's job. → `reference/gotchas.md` → "Downstream: AWS Entity Resolution" |
| A second team needs a subset of tables | LF column/row-level grants per role |
| PII columns identified | LF column masking + `data-classification=confidential` tag |
| Cross-account sharing | LF cross-account grant + RAM share (or AWS DataZone) |
| Cross-**region** catalog + query | `reference/cross-region-query.md` (follow-up #8) — resource link or mart replication |
| Cross-account **AND** cross-region | Both supported and documented by AWS → `reference/cross-region-query.md` §8. ⚠️ Cross-account voids this skill's LF IAM-only default: Mechanism A requires RAM + LF grants, Mechanism B requires a destination-side table-bucket policy (+ KMS key policies on both ends) |
| "Data mesh" / "data products" | AWS DataZone (separate skill) |
| Audit/compliance | CloudTrail data events on S3 + LF audit trail |

The naming convention and tags this skill applies are preconditions for all of the above — cheap now, expensive to retrofit.

---

## Maintaining and extending the platform

`ARCHITECTURE.md` + `platform.yaml` are the single source of truth. When extending: (1) read them FIRST (§1 current-state question), (2) add incrementally without recreating (idempotent CDK, `RemovalPolicy.RETAIN`), (3) update both files. Schema changes: Iceberg → `ALTER TABLE ADD/DROP COLUMNS` + update the job transform; Hive → re-run the crawler. New source → one new Glue job + table (+ mart). Performance → check Athena scan cutoff (§9), partition design, S3 Tables compaction (`reference/iceberg-cdk.md`). Cost → Glue DPU/workers + trigger frequency (§9).

---

## Tool-Specific Context File Generation

After a successful deploy, generate a context file so future agent sessions understand the project without re-reading all infrastructure. Replace `{prefix}`, `{pattern}` (iceberg|hive), `{region}` with actual values.

**Claude Code → `CLAUDE.md` in project root:**
```markdown
# {prefix} Data Platform
Read `platform.yaml` before making any changes.
Pattern: {pattern}. Prefix: {prefix}. Region: {region}.
Update platform.yaml and ARCHITECTURE.md after every change.
```

**Kiro → `.kiro/steering/platform-context.md`:**
```markdown
---
inclusion: always
description: "Platform context for {prefix} data lake"
---
Read platform.yaml before making any changes.
Pattern: {pattern}. Prefix: {prefix}. Region: {region}.
Update platform.yaml and ARCHITECTURE.md after every change.
```

---

## Section Map

🔴 Critical Rules · **Validation Gates (5: scope&residency / preconditions / data model / reconciliation / teardown)** · Reference files (load-on-demand table) · §1 Prerequisites & Inputs · §2 Architecture Overview · §3 Decision Trees · §4 Storage Pattern: Iceberg (CORE FLOW) · §5 IAM & Security · §6 ETL Pipeline · §7 Query Layer · §8 Data Quality · §9 Cost Guardrails · §10 Output Contract · §11 Teardown · §12 Batch-Only Scope · §13 Governance · Reference files: `iceberg-cdk.md`, `scripts.md`, `hive-pattern.md`, `sap-sources.md`, `vpc-connectivity.md`, `cross-region-query.md`, `gotchas.md`
