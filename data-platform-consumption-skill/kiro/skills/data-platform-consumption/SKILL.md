---
name: data-platform-consumption
description: |
  Connect existing data sources to Amazon Quick (Quick Sight + chat agents) for
  visualization and natural language analytics. Works with any queryable data:
  Glue Catalog, Athena, Redshift, or S3. Sets up dashboards, SPICE caching,
  and AI-powered chat agents for business users. Triggers: "Quick Sight setup",
  "dashboard", "Amazon Quick", "data visualization", "BI setup", "chat agent",
  "natural language analytics", "connect to Quick Sight".
---

# Data Platform Consumption (Amazon Quick)

This skill takes a queryable data source — typically Athena over a Glue Catalog, but optionally Redshift, S3, or RDS — and produces a working consumption layer: a Quick Sight account with datasets in SPICE, dashboards built around the customer's business questions, an Amazon Quick chat agent grounded in a semantic model, and Space-based access control for multi-team isolation. The output is a working CDK TypeScript project plus the topic/dashboard definitions to run it.

The skill is opinionated: it picks SPICE over direct query, pins SPICE refresh to off-peak, requires named synonyms for natural-language matching, and defaults to one Space per team. These can be overridden but the skill will not present them as menu options.

This skill is self-contained: it never assumes any other skill has run. It creates its own IAM role (`{prefix}-quicksight-role`) with minimum required permissions. If another stack already owns a role of the same name (e.g. the pipeline layer created a placeholder), see the import-vs-create handoff in `reference/iam-permissions.md`.

> **Naming note (as of 2026):** The product formerly known as "Amazon QuickSight" is now **Amazon Quick** (the platform), with **Quick Sight** as the BI feature (dashboards, SPICE, embedded analytics). Natural-language features (chat agents, Dataset Q&A) are part of Amazon Quick. **Topics** remain a Quick Sight feature for curating semantic models that chat agents read from. The AWS CLI/SDK namespace is NOT renamed — still `aws quicksight ...` and `quicksight.*`. This skill uses the new display names in prose and the legacy names in code/CLI.

---

## 🔴 CRITICAL RULES (never violate)

1. **The MANAGED service role — not your custom role — needs `s3tables:*` on the Iceberg path.** For a vanilla Athena-on-S3(+S3 Tables) dashboard, Quick Sight assumes an AWS-managed role (`aws-quicksight-s3-consumers-role-v0`, falling back to `aws-quicksight-service-role-v0`), NOT `{prefix}-quicksight-role`. The connection test passes while dashboard queries fail at render time. Patch the managed role.
2. **The Topic `Description` field is ≤ 256 chars.** Pasting the full persona/rules block fails the `create-topic` call. Condense to one sentence; encode agent quality via the semantic model (synonyms, calculated fields, named entities).
3. **`PhysicalTableMap`/`LogicalTableMap` keys + column IDs must match `[0-9a-zA-Z-]*`** — letters, digits, hyphens only. No underscores, no dots. Use `quality-inspections`, never `quality_inspections`.
4. **`permissions.actions` must be a complete predefined action set** (full Owner or full Reader set). A partial/custom subset is rejected with `Resultant state not supported`.
5. **`AWSQuickSightS3Policy` bucket allowlist is the #1 silent failure.** Missing buckets surface as the misleading `"Could not get query execution ID"`. The analytics-results bucket needs read **AND** write (with the "Write permission for Athena Workgroup" checkbox).

> Full IAM blocks, the managed-role patch, the S3 allowlist fix, and the Lake Formation grant → **`reference/iam-permissions.md`**. Topic 256-char handling + semantic model → **`reference/chat-agent.md`**. Dashboard schema gotchas + STRICT validation → **`reference/dashboard-patterns.md`**.

---

> **Language**: Always respond in the language the user uses. Korean in → Korean out; English in → English out. Code and CDK output are always in English regardless of conversation language.

> **Execution Model**: This skill does NOT just generate code for the user to run manually. You ARE the builder — you have terminal access. Generate the CDK project, then:
> 1. Install dependencies (`npm install`)
> 2. Synthesize (`cdk synth`) — fix any errors before proceeding
> 3. Deploy (`cdk deploy --all --require-approval never`)
> 4. Run post-deploy verification (Quick Sight CLI calls, dashboard validation, SPICE ingestion check)
> 5. If anything fails, diagnose, fix, and retry automatically
> 6. Only ask the user when a DECISION is needed (not for execution permission)
>
> The user provides business context (which questions to answer, which visuals look right) and approves architecture decisions. YOUR role is to build, deploy, verify, and iterate until it works.
>
> | Agent asks user (MUST stop and wait) | Agent does silently |
> |---|---|
> | **Architecture pattern choice** (Iceberg `s3tablescatalog` vs Hive `AwsDataCatalog`) | `npm install`, `cdk synth` |
> | **Region selection** (drives chat/Topics availability, SPICE capacity) | `cdk deploy` (non-production) |
> | **Dashboard plan** (sheets, visuals, structure) — BEFORE building | `cdk deploy` of `CfnDataSource` / `CfnDataSet` / `CfnDashboard` |
> | **Target metric values for gauges / reference lines** (Q10 — never fabricate) | Dataset creation + first SPICE ingestion (`create-ingestion`/`list-ingestions`) |
> | "Does this dashboard layout look right?" (after sharing a preview) | `--validation-strategy STRICT` dashboard validation + render-status check |
> | "Deploy to **production**?" (if environment=production) | Post-deploy KPI accuracy + layout-integrity validation (§7) |
> | Genuinely ambiguous synonym mapping ("I found 3 possible mappings. Which one?") | Auto-retry on transient errors; update `ARCHITECTURE.md` / `platform.yaml` |
> | "This error persists after 3 retries: [error]. Need your input." | — |
>
> Note: Features like AI commentary (Bedrock) are NOT proactively offered. They activate only when the user explicitly requests them (e.g., "add AI commentary"). See the reference-files routing table for keyword triggers.
>
> **KEY principle:** **Any decision that affects what the USER sees in the final dashboard MUST be approved first** (architecture pattern, region, dashboard plan, gauge targets, restricted topics). **Execution / infrastructure decisions are autonomous** (install, synth, deploy, IAM, SPICE refresh, dataset creation, validation). When unsure which side a decision falls on, ask: *"does the customer see the difference in the dashboard?"* — if yes, stop and confirm.
>
> **One genuine exception that DOES require user action:** the `AWSQuickSightS3Policy` bucket allowlist (🔴 rule 5). The console toggle for "QuickSight access to AWS services" cannot be flipped from CLI/CDK in all account configurations. If you detect the resulting "Could not get query execution ID" error after a dashboard creation, surface the exact remediation (console path + IAM policy patch fallback from `reference/iam-permissions.md`) and wait for confirmation before retrying.

---

## Reference files (load on demand)

The core below is the default flow. Pull in a reference file when you reach its topic:

| File | When to read |
|------|-------------|
| `reference/region-constraints.md` | Picking a region, hitting the chat/Topics availability gate, SPICE per-region capacity, or the identity-region error |
| `reference/quicksight-cdk.md` | Writing any CDK — `CfnDataSource`, `CfnDataSet` (Hive `relationalTable` or Iceberg `customSql`), `CfnRefreshSchedule`, `CfnDashboard`, native S3 Tables connector, RLS |
| `reference/dashboard-patterns.md` | Designing dashboards — domain layouts, the definition gotchas table, STRICT validation, the 3-step update flow |
| `reference/dashboard-definitions.md` | Building a dashboard? → read this for a complete working 4-sheet example — a real deployed, STRICT-clean definition (33 visuals across production-efficiency / quality / cost / delivery) annotated with every key pattern (KPI single-row dataset, gauge target, sparkline, TOP-N sorting) plus a "how to adapt" guide |
| `reference/chat-agent.md` | Building the chat agent — persona, topic creation, the 256-char limit, semantic model (synonyms/calculated fields/named entities), test cases |
| `reference/iam-permissions.md` | Any IAM — service role, managed-role patch, S3 allowlist, `s3tables:*` grants, Lake Formation, account/namespace setup, data-source discovery, Spaces/RLS |
| `reference/ai-commentary.md` | Need AI-generated commentary on the dashboard? → read `reference/ai-commentary.md` — Bedrock → InsightVisual `CustomNarrative` injection via `UpdateDashboard` + `UpdateDashboardPublishedVersion`, EventBridge/Lambda, no external DB |

---

## 1. Prerequisites & Inputs

### Current state assessment (ask FIRST, before other questions)

Determine what already exists before any work. Present as an interactive choice:

```
What is the current state of your analytics layer?
  a) Starting from scratch — Quick Sight not set up yet
  b) Architecture doc exists — I have an ARCHITECTURE.md or similar
  c) Quick Sight account exists — need datasets and dashboards
  d) Datasets exist in SPICE — need dashboards and/or chat agent
  e) Dashboards exist — adding chat agent / new datasets
  f) Let me describe the current state: ___
```

- **(b):** Ask for the path to the architecture doc. Read it and incorporate existing state — do NOT recreate what exists.
- **(c)–(e):** Ask which specific components exist. Skip those steps.
- **(f):** Let them describe, then confirm your understanding before proceeding.

**Key principle:** Never deploy infrastructure that already exists. Always check first.

### Ask FIRST — region

> Before `project_prefix`, before `data_source_type`, before anything else, ask:
> > "Which AWS region will you run Quick Sight in? (e.g., us-east-1, ap-northeast-2)"
>
> Region is first because it determines: the resource region, the SPICE capacity location, **chat-agent / Topics availability** (not all regions support them), and the QuickSight identity region. A wrong region invalidates almost every later decision. Pin it, then run the region availability gate (`reference/region-constraints.md` §2).

### Primary inputs — collect ALL before proceeding

| Input | Example | Notes |
|---|---|---|
| `aws_region` | `ap-northeast-2` | **Ask FIRST.** Where data lives + where Quick Sight runs. Chat features may need a different region — `reference/region-constraints.md`. |
| `data_source_type` | `athena` / `redshift` / `s3` / `other` | Drives the data source connector. |
| `data_source_details` | see below | Glue DB + workgroup, OR Redshift endpoint, OR S3 manifest. |
| `project_prefix` | `acme` | Optional. If set and matches `{prefix}_db` in Glue, the skill auto-discovers tables. |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by supplier, next-month defect-rate forecast" | Drives dashboards, topics, chat agent test cases. |
| `target_users` | "5 quality-team members, 2 executives" | Drives Space layout and persona scoping. |

**`data_source_details` shape by type:**
- **Athena**: `{ glue_database, workgroup, results_bucket }`
- **Redshift**: `{ endpoint, port, database, secret_arn, vpc_id, subnet_ids[] }` — VPC connection setup in `reference/quicksight-cdk.md` §6.
- **S3**: `{ manifest_uri, format: "csv"|"tsv"|"json" }` — the S3 manifest connector does NOT support Parquet; for Parquet use `athena` instead (`reference/quicksight-cdk.md` §5).
- **Other**: `{ description }` — recommend Athena federated query as a bridge.

### Follow-up questions (ask after primary inputs, ONE AT A TIME, with a recommended default)

| # | Question | Recommended default |
|---|----------|---------------------|
| 1 | SPICE refresh frequency? (daily / hourly / real-time via direct query) | **Daily 04:00 KST (19:00 UTC)** — just after the pipeline's nightly ingest (~03:00 KST), so SPICE is fresh before business hours (§6). |
| 2 | Chat agent response language? | **Korean** (match the target users' primary language) |
| 3 | Dashboard style? (executive summary / detailed operational / both) | **Both** — one executive summary sheet + one operational detail sheet per business question |
| 4 | Existing dashboards/reports to replicate? | **No — build fresh** (optimized for Quick Sight's native visual types) |
| 5 | How many Spaces (isolated groups)? | **1 Space** (single team; add more as the user base grows) |
| 6 | Chat agent restricted topics? | **Refuse predictions/forecasts** ("next-month forecast" → "Forecasting is not supported") |
| 7 | Dashboard format? (KPI summary / detailed operational / trend / comparison) | **KPI summary + trend** (one KPI-card sheet, one trend sheet with time-series + comparisons) |
| 8 | What insights do you expect from the data? (I can propose based on structure) | **Structure-based proposals** — scan tables/columns and suggest time-series (date+numeric), comparisons (category+metric), anomaly candidates (high-variance), TOP-N rankings (dimension+metric) |
| 9 | What data period do you have for trend analysis? (day/week/month/quarter) | **Check the distinct date count in the data and pick the right grain** — single-month data makes a "monthly trend" a single point; query the actual distinct-date count before choosing the trend grain. |
| 10 | Is there a target/threshold for each key metric? (e.g., utilization 85%, defect rate 2%) | **Use it if provided, otherwise proceed with no baseline** — gauges, reference lines, and conditional colors all need a real target. NEVER substitute a meaningless column (e.g. a line count) as a gauge target. |

When the user picks "get proposals" on Q8, enumerate columns from the dataset and propose 4–6 specific insights using those patterns. Don't propose insights for columns that don't exist — confirm against `aws glue get-table` first.

> **Q9 matters because** single-period data silently degrades trend visuals to one point — confirm the real date range (`SELECT COUNT(DISTINCT date_col)`) and pick day/week/month/quarter to match. **Q10 matters because** a gauge or reference line with no real target tempts the agent to fill garbage (a gauge "utilization target" was once set to a `line_count` of 11 instead of 85%). No target → omit the gauge/reference line, don't fabricate one.

> **Interaction pattern:** present each question as a one-at-a-time multiple-choice prompt with the default highlighted. Do NOT dump all questions at once. If the user says "just use the defaults", accept ALL defaults and proceed.

### Account preconditions — run before building

```bash
# 1. Active identity matches the target account
aws sts get-caller-identity

# 2. Quick Sight account status (CLI still uses 'quicksight')
aws quicksight describe-account-settings --aws-account-id $(aws sts get-caller-identity --query Account --output text) 2>&1 \
  || echo "Quick Sight not yet enabled in this account"

# 3. Chat / Topic availability in the target region — see region-constraints.md
aws quicksight list-topics --aws-account-id $(aws sts get-caller-identity --query Account --output text) --region {aws_region} 2>&1 \
  || echo "Amazon Quick Topics may not be available in this region"

# 4. If data_source_type=athena: validate workgroup + database exist
aws athena get-work-group --work-group {workgroup} --region {aws_region}
aws glue get-database --name {glue_database} --region {aws_region}

# 5. SPICE capacity in the TARGET region (per-region; describe-account-settings does NOT return capacity).
#    See region-constraints.md §3 — the first dataset creation in a fresh region fails if capacity is 0.
aws quicksight describe-account-settings --aws-account-id {account_id} --region {aws_region}
```

If Quick Sight is not enabled, the skill can enable it (Enterprise edition) — but only after explicit user confirmation, since it has cost implications. Account/namespace/user setup → `reference/iam-permissions.md` §7.

**Region availability gate (do not skip):** before generating any CDK, confirm `{aws_region}` supports chat / Topics. The probe is a hint — the user's stated knowledge of region capability **overrides** it. Full gate + the two-option flow → `reference/region-constraints.md`.

For cross-region setup (e.g., data in Seoul, Quick Sight in us-east-1) → see `reference/region-constraints.md` 'Cross-region Quick Sight' section

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          AWS Account                              │
│  ┌──────────────┐                                                 │
│  │ Data Source  │  Athena / Redshift / S3 manifest / RDS-fed      │
│  └──────┬───────┘                                                 │
│         │ (read via Quick Sight data source)                      │
│         ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ Amazon Quick — Quick Sight (Enterprise)                    │    │
│  │   Data Source ─► Dataset (SPICE) ─► Topic / Semantic Model │    │
│  │                      │                       │             │    │
│  │                      ▼                       ▼             │    │
│  │                 Analysis              Amazon Quick         │    │
│  │                  → Dashboard          chat agent           │    │
│  │                      │              (persona/guardrails/    │    │
│  │                      ▼               synonyms)              │    │
│  │              Spaces (one per team): datasets + users +     │    │
│  │              per-space chat persona                        │    │
│  └──────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────┘
```

**Default data path (Iceberg pipeline):** one Athena custom-SQL dataset over the `s3tablescatalog/{prefix}-table-bucket` federated catalog powers **both** the dashboard and the chat agent. The chat agent (Dataset Q&A) requires a SQL engine, so routing everything through Athena means one dataset to build, refresh, and manage. Catalog handoff (Iceberg vs Hive) + dataset CDK → `reference/iam-permissions.md` §8 + `reference/quicksight-cdk.md`.

---

## 3. Build Flow

The default end-to-end flow, with pointers to the reference file for each step:

1. **Region + preconditions** (§1) → `reference/region-constraints.md` for the availability gate and SPICE capacity.
2. **Discover the data source** — read the pipeline's `ARCHITECTURE.md` to pick the catalog (Iceberg `s3tablescatalog` → `mart_*`, or Hive `AwsDataCatalog` → `v_*`). Enumerate Glue tables. → `reference/iam-permissions.md` §8.
3. **Quick Sight account + namespace + groups** (create admin group before any data source) → `reference/iam-permissions.md` §7.
4. **IAM** — `{prefix}-quicksight-role`, the managed-role patch (🔴 rule 1), the `AWSQuickSightS3Policy` allowlist (🔴 rule 5) → `reference/iam-permissions.md`.
5. **Data source + dataset** — Athena `CfnDataSource`; Hive `relationalTable` or Iceberg `customSql` `CfnDataSet`; SPICE import mode → `reference/quicksight-cdk.md`.
6. **SPICE refresh schedule** (§6) → `reference/quicksight-cdk.md` §7.
7. **Dashboard** — decide structure (tabs vs separate, `reference/dashboard-patterns.md` §0), present the plan, get approval, build, STRICT-validate, verify render status, **then verify KPI numerical accuracy + layout integrity** (§7 steps 4–5) → `reference/dashboard-patterns.md`.
8. **Chat agent + semantic model** (skip if the region is dashboards-only) → `reference/chat-agent.md`.
9. **Spaces & access control** → `reference/iam-permissions.md` §9.
10. **Post-deploy bootstrap + validation** (§7) and **output contract** (§8).

---

## 4. Data Source & Dataset (summary)

- **Athena (most common):** create `CfnDataSource` (type `ATHENA`) in CDK so `CfnDataSet` references it by token. CDK → `reference/quicksight-cdk.md` §1.
- **Datasets — one per business domain.** A domain maps to one Athena view/mart. Don't build one mega-dataset — break by question area so SPICE refresh is incremental and topic scope is bounded.
- **Iceberg path (default):** Athena **custom-SQL** dataset over `s3tablescatalog/{prefix}-table-bucket` → `mart_*` tables. `relationalTable` can't express the slashed federated catalog. One dataset feeds dashboard + chat. → `reference/quicksight-cdk.md` §3.
- **Hive path:** `relationalTable` dataset over `AwsDataCatalog` → `v_*` views. → `reference/quicksight-cdk.md` §2.
- **Optional native S3 Tables connector:** dashboard-only shortcut (no Athena, managed role, working picker) — use ONLY when chat is out of scope. → `reference/quicksight-cdk.md` §4.
- **SPICE not direct query:** sub-second rendering, no surprise Athena scan costs, decoupled from pipeline freshness. Cost is staleness ≤ refresh interval — acceptable for most Data Lab use cases.

Schema constraints (no underscores in map keys; complete permission action sets) apply to every `CfnDataSet`/`CfnDashboard` — 🔴 rules 3–4, detail in `reference/quicksight-cdk.md`.

---

## 5. Dashboards (summary)

- **Decide structure FIRST** (before the plan): single dashboard + per-topic tabs (default ✓) vs separate dashboards. Ask before approval → `reference/dashboard-patterns.md` §0.
- **Design is an ITERATIVE collaboration, not a single approval gate** — propose → user feedback → adjust → confirm → THEN build autonomously. Full flow below.

### Interactive dashboard design (MANDATORY before building)

The dashboard design phase is ITERATIVE — not a single approval gate.

**Step 1: Propose initial plan with recommendations**

Based on the user's business questions, propose:
```
Dashboard draft:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Tab 1: [name] (why recommended: ...)
  - Visual 1: [type] — [description] (recommended: this is the most important metric, so a KPI card)
  - Visual 2: [type] — [description] (recommended: there's a time-series trend, so a Line chart)
  ...
📊 Tab 2: [name] (why recommended: ...)
  - Visual 1: ...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dataset: [name] (SPICE, refresh: daily)
Filters: [recommended common filters]

Explain the reasoning, then wait for the user's feedback.
"Does this layout look good? Let me know if there's anything you'd like to change."
```

**Step 2: Iterate based on user feedback**

User might say:
- "Add a TOP-N chart to Tab 2 as well"
- "Increase the KPI cards to 5"
- "Tab 3 isn't needed"
- "Show this metric as a gauge, target 85%"

→ Adjust the plan and show the updated version. Repeat until the user says "looks good" or "go ahead."

**Step 3: Confirm final plan**

Show the FINAL plan one more time:
"Final confirmation: [plan]. Shall I start the build with this?"

**Step 4: Execute autonomously**

Once approved, build everything without further questions (unless hitting a technical issue).

**Key rules:**
- NEVER build a dashboard without showing the plan first
- ALWAYS explain WHY you recommend each visual type (not just WHAT)
- If user's request is vague ("build me a sales dashboard"), YOU propose the structure based on the available data
- Suggest things the user might not have thought of (e.g., "this data has a time-series, so a trend chart would be a good addition")

Template + approval flow detail → `reference/dashboard-patterns.md` §1.
- **KPI cards read a single-row KPI dataset, not a multi-grain mart.** A KPI card aggregates over the whole dataset; if the dataset has multiple rows per entity, SUM/COUNT double- or triple-counts. Point KPI visuals at `mart_kpi_summary` (1 row, every measure pre-aggregated with the correct function incl. `COUNT(DISTINCT …)`); point trend/ranking visuals at the grain-level mart. → `reference/dashboard-patterns.md` §10.
- **Beautify before handoff** — currency/percentage/number formatting, TOP-N sort+limit, data labels, conditional formatting via aggregation expression. Checklist → `reference/dashboard-patterns.md` §11.
- **Numbers MUST be verified correct, not just rendered** — after STRICT + render, run KPI Numerical Accuracy Verification (§7 step 4, `reference/dashboard-patterns.md` §8). This is the single most important post-deploy step.
- **> 5 visuals → build in the UI, then export** `describe-dashboard-definition` and check the JSON into CDK. Inline-from-scratch CDK only for ≤ 5 visuals or demos.
- **Domain layouts** (manufacturing / retail / general fallback) → `reference/dashboard-patterns.md` §2.
- **Validate before deploy:** run `--validation-strategy STRICT` against a temp dashboard (~2s, no rollback). Then **verify render status** (`describe-dashboard` → `Status`/`Errors`) — STRICT misses `COLUMN_TYPE_INCOMPATIBLE` render-time errors. → `reference/dashboard-patterns.md` §4–5.
- **Definition gotchas table** (KPI `TargetValue`, STRING measures, `formatConfiguration` nesting, `StringFilter nullOption`) → `reference/dashboard-patterns.md` §3.
- **Updates are 3 calls** (`update-dashboard` → `update-dashboard-permissions` → `update-dashboard-published-version`); missing #2 → users can't see it, missing #3 → users see the OLD version. → `reference/dashboard-patterns.md` §6.

---

## 6. SPICE Configuration & Refresh

### Default refresh policy

| Dashboard criticality | Refresh schedule | Rationale |
|---|---|---|
| Standard reporting | Daily 04:00 KST (after pipeline runs) | Pipeline finishes ~03:00, SPICE refresh after |
| Critical / leadership | Hourly during business hours | Limits SPICE compute cost |
| Real-time | Don't use SPICE — direct query or streaming pattern | SPICE is not for sub-minute |

`CfnRefreshSchedule` CDK (incl. the `timeZone`/`TimeZone` `addPropertyOverride` workaround) and incremental refresh for large fact tables → `reference/quicksight-cdk.md` §7.

**Capacity sizing:** default SPICE is 10 GB. `SPICE size ≈ raw_rows × avg_row_size × ~0.3`. **SPICE quota is per-region** — a cross-region setup (identity `us-east-1`, resources `ap-northeast-2`) starts at 0 GB in the resource region and dataset creation fails. Purchase capacity in the resource region or use `DIRECT_QUERY`. Full per-region gotcha → `reference/region-constraints.md` §3.

---

## 7. Post-deploy Bootstrap — execute yourself, do NOT hand off

After `cdk deploy --all --require-approval never` succeeds, run this yourself. If a step fails, diagnose, fix, redeploy, retry. Report one summary at the end.

1. **Confirm the data source is reachable:**
   ```bash
   aws quicksight describe-data-source --aws-account-id {account_id} \
     --data-source-id "{prefix}-athena-source" --region {region} \
     --query 'DataSource.{Status:Status,ErrorInfo:ErrorInfo}'
   # Expected: Status=CREATION_SUCCESSFUL, ErrorInfo=null
   ```
   If `CREATION_FAILED` with an S3 error → `AWSQuickSightS3Policy` allowlist is missing. STOP and surface the `reference/iam-permissions.md` §5 remediation (the one genuine exception that needs human action). Otherwise auto-fix.

2. **Trigger and watch the first SPICE ingestion** for each dataset:
   ```bash
   aws quicksight create-ingestion --aws-account-id {account_id} \
     --data-set-id {prefix}-quality-inspections --ingestion-id "bootstrap-$(date +%s)" --region {region}
   # Poll list-ingestions until IngestionStatus in (COMPLETED, FAILED, CANCELLED)
   aws quicksight list-ingestions --aws-account-id {account_id} \
     --data-set-id {prefix}-quality-inspections --region {region} \
     --query 'Ingestions[0].{Status:IngestionStatus,Rows:RowInfo,Err:ErrorInfo}'
   ```

3. **Pre-flight the dashboard definition** with `--validation-strategy STRICT`, then **verify render status** (`describe-dashboard` → `Status`/`Errors`). Both commands → `reference/dashboard-patterns.md` §4–5.

4. **🔴 KPI Numerical Accuracy Verification (MANDATORY — do NOT skip).** STRICT pass + render success + row > 0 proves the dashboard is *structurally* valid — it does **NOT** prove the numbers are *correct*. In a real build, all of those passed while **6 KPIs showed wrong numbers** (an 8.3× grain-duplication overcount, a 16% row-loss undercount, `COUNT` vs `COUNT(DISTINCT)`, wrong population filter, negatives diluting an average, a meaningless gauge target). For **EVERY** KPI visual, reconcile three numbers — **source table** vs **mart** vs **dashboard-displayed** — and require all three to agree within 1%. Full procedure, example queries, and the six root-cause patterns → `reference/dashboard-patterns.md` §8.

5. **Layout integrity check** — parse the deployed definition and confirm no visual overlaps, no large orphan whitespace, every visual inside the 36-column grid, consistent heights per row. Catches "structurally valid but visually broken." → `reference/dashboard-patterns.md` §9.

6. **Chat agent validation (console / handoff — NOT programmatic):** generate a `test-cases.md` (lookup / trend / comparison / filter / speculation-refusal / out-of-scope) with expected answer patterns. There is **no API** to pose an NL question to a Topic and grade the answer — validate in-console or hand off. Do not claim to have "run the chat tests." → `reference/chat-agent.md` §4.

7. **Update `ARCHITECTURE.md` + `platform.yaml`** (§8) with deployed datasets, dashboards, topics, Spaces, refresh schedules, and any synonym/persona iterations.

Report a single summary:
> ✅ Quick Sight deployed. Datasets: [list, SPICE rows]. Dashboards: [list, status]. **KPI accuracy: [N/N KPIs reconciled source=mart=dashboard within 1%]**. Layout: [OK / issues]. Topic test cases: [passed/failed].

> ⚠️ Never report a dashboard as "done" on STRICT + render success alone. The build is done only when **every KPI's number is verified correct** (step 4) — that is the gap that shipped 6 wrong numbers to a user despite all prior validations passing.

User-touch points (only): "What visuals on the executive sheet?" (before §5) · "Does this dashboard look right?" (after §5, with a preview link) · the `AWSQuickSightS3Policy` console fix if step 1 detects the S3 error · any genuinely ambiguous synonym mapping. Everything else is silent execution.

---

## 8. Output Contract (`ARCHITECTURE.md` + `platform.yaml`)

The skill MUST create/maintain both files in the CDK project root. If the pipeline skill created them, **READ first and append/merge** — do not overwrite. `ARCHITECTURE.md` is for humans (prose, rationale); `platform.yaml` is for machines (parseable). Keep them in sync, update on EVERY change.

### `ARCHITECTURE.md` — consumption sections to add

```markdown
### Quick Sight
- Account: Enterprise, namespace: `{prefix}`
- Datasets: [list with SPICE/DIRECT_QUERY mode]
- SPICE refresh: [schedule]
- Dashboards: [list]

### Amazon Quick Chat Agent
- Region: {region}
- Persona: {description}
- Topics: [list]
- Spaces: [list with access groups]

### Decisions (why, not just what)
| Decision | Rationale | Do NOT change to |
|----------|-----------|-----------------|
| Athena custom-SQL dataset (Iceberg path) | One dataset powers both dashboard + chat agent | Native S3 Tables connector (dashboard-only, can't back chat) |
| SPICE (not Direct Query) | Sub-second rendering, no surprise Athena costs | Direct Query (unless SPICE capacity unavailable) |
| Enterprise edition | Required for Topics, RLS, hourly refresh, chat agents | Standard (lacks all agentic features) |
| One Topic per Space | Persona/guardrails scoped per team | Shared Topic (leaks cross-team context) |
| mart_* tables (Iceberg) / v_* views (Hive) | Pre-computed, refresh-stable source for SPICE | Raw base tables (schema drift breaks dashboards) |

### Known Issues & Gotchas
| Issue | Impact | Workaround |
|-------|--------|------------|
| `AWSQuickSightS3Policy` bucket allowlist | Dashboard fails with "Could not get query execution ID" | Add buckets via console or patch IAM policy (reference/iam-permissions.md §5) |
| Managed role needs s3tables:* (Iceberg) | Connection test passes but dashboard query fails | Patch `aws-quicksight-s3-consumers-role-v0` (reference/iam-permissions.md §3) |
| Topic Description ≤ 256 chars | Full persona block rejected | Condense to one sentence; encode rules via semantic model |
| `relationalTable` can't express federated catalog slash | Dataset creation fails for Iceberg tables | Use `CustomSql` physical table map (reference/quicksight-cdk.md §3) |
| SPICE capacity is per-region (0 GB in non-identity region) | Dataset creation fails cross-region | Purchase capacity or use DIRECT_QUERY (reference/region-constraints.md §3) |
| Chat agent not available in all regions | Topics API errors in unsupported regions | Region flow — Option A or B (reference/region-constraints.md §4) |
| STRICT + render pass but KPI numbers wrong | "Validation passed ≠ correct answer" — 6 wrong KPIs shipped | KPI Numerical Accuracy Verification (§7 step 4, reference/dashboard-patterns.md §8) |
| Multi-grain mart feeds a KPI card | SUM double/triple-counts (3,527 vs 426) | Single-row KPI dataset (reference/dashboard-patterns.md §10) |
| Gauge target set to a meaningless column | Gauge shows 11 instead of 85% | Provide a real target (Q10) or omit the gauge (dashboard-patterns.md §3) |
| `CATALOG_NOT_FOUND` despite `s3tables:*` granted | S3 Tables query fails on managed + custom role | Add `lakeformation:GetDataAccess` + nested-catalog glue grants (reference/iam-permissions.md §2–§3) |

## Change Log
| {date} | Added Quick Sight datasets + dashboard for {domain} | {user/agent} |
```

**Rules:** create on first run if missing (with empty pipeline placeholders + consumption sections); otherwise append. READ it first on re-run. Single source of truth for "what exists."

### `platform.yaml` — fill the `consumption` section

Read first to discover pipeline state (catalog name, table list, column types, join keys, **plus each mart's `grain` / `sum_safe_columns` / `single_row` / `validation_sql`**). Use those to decide which mart backs which visual: a `single_row: true` mart backs KPI cards; a multi-grain mart backs only trend/ranking visuals and its `sum_safe_columns` tell you which fields are safe to SUM. Fill the `consumption` section the pipeline left empty; extend `lineage` through SPICE → dashboard; update `updated` on every change. If it doesn't exist (consumption-only build), create it from what you can determine.

```yaml
platform:
  prefix: "{prefix}"
  region: "{region}"
  pattern: "iceberg"  # iceberg | hive — read from pipeline's ARCHITECTURE.md/platform.yaml
  catalog: "s3tablescatalog/{prefix}-table-bucket"  # or AwsDataCatalog for Hive
  updated: "YYYY-MM-DD"
consumption:
  quicksight_region: "{region}"
  spice_refresh: "DAILY 04:00 Asia/Seoul"
  datasets:
    "{prefix}-quality-inspections":
      source_table: "mart_quality_summary"   # grain-level mart → trend/ranking visuals
      import_mode: "SPICE"
      refresh: "DAILY 04:00 Asia/Seoul"
    "{prefix}-kpi-summary":
      source_table: "mart_kpi_summary"        # single_row mart → KPI cards (no SUM duplication)
      import_mode: "SPICE"
      refresh: "DAILY 04:00 Asia/Seoul"
  dashboards:
    "{prefix}-quality-dashboard":
      sheets: ["KPI Summary", "Trend Analysis"]
      datasets: ["{prefix}-quality-inspections", "{prefix}-kpi-summary"]
      kpi_accuracy_verified: true             # set true only after §7 step 4 reconciliation passes
  topics:
    "{prefix}-quality":
      persona: "quality analytics expert"
      datasets: ["{prefix}-quality-inspections"]
lineage:
  - "raw_quality_inspections -> mart_quality_summary -> quality-dataset(SPICE) -> quality-dashboard"
```

---

## 9. Iteration & Maintenance

> **Stack separation for fast iteration.** Deploy in three CDK stacks so dashboard iteration (~2 min/cycle) doesn't roll back upstream resources:
> 1. **DataSourceStack** — Athena/Redshift/S3 connection, Quick Sight IAM, `AWSQuickSightS3Policy` allowlist (stable)
> 2. **DatasetStack** — SPICE datasets, refresh schedules, calculated fields (changes when schema evolves)
> 3. **DashboardStack** — analyses, dashboards, visuals, topics (changes frequently)
>
> Cross-stack references via `props` carry the dataset/data-source ARNs forward.

`ARCHITECTURE.md` + `platform.yaml` are the single source of truth — read them FIRST (§1 current-state question), make changes incrementally (never recreate), update both after.

- **Adding a dataset (new domain):** confirm the source view/mart exists → add `CfnDataSet` → add refresh schedule → add to a dashboard (new section or new dashboard) → add to Spaces' permissions.
- **Adding a dashboard:** build in the UI → export the definition → check into CDK under `dashboards/{name}.json` → reference from CDK.
- **Chat agent gaps:** add synonyms / calculated fields / named entities to the topic before touching the persona → `reference/chat-agent.md` §7.
- **Source schema changed:** update the dataset's `inputColumns` / custom SQL, re-ingest SPICE, add new synonyms.
- **Performance:** check SPICE refresh timing vs pipeline finish time, SPICE capacity, and Athena scan limits on the workgroup.
- **New Space:** create group → dataset/dashboard permissions → optional per-Space topic → register users → `reference/iam-permissions.md` §9.
- **Versioning:** bump `versionDescription` on every dashboard deploy ("v3 — added vendor drill-down, 2026-01-15").

---

## 10. Validation Checklist

Before handing off to the customer:

**Connectivity** — Enterprise edition · Athena workgroup reachable · service-level S3 + Athena grants configured · Lake Formation grants if strict mode.

**Datasets** — each in SPICE (unless justified) · each has a refresh schedule · first refresh succeeded · calculated fields render correctly.

**Dashboards** — each business question has ≥1 answering visual · all visuals load < 3s · filters propagate · export to CSV works · mobile renders acceptably · **every KPI reconciled source = mart = dashboard within 1% (§7 step 4 — the most-missed check)** · layout integrity passes (no overlap/orphan whitespace, §7 step 5) · beautify checklist applied (`reference/dashboard-patterns.md` §11).

**Chat agent** (skip if dashboards-only region) — Topic description is the condensed ≤256-char guardrail (NOT the full block) · all 6 test categories pass (lookup, trend, comparison, filter, **speculation refusal** — most-missed, out-of-scope refusal) · Korean + English synonyms both work · source citation appears.

**Access control** — each user in exactly one Space · Space A can't see Space B (verified by login) · admin Space has full access · RLS filters correctly per group.

**Operational** — CloudWatch alarms on SPICE ingestion failures · SPICE capacity sized · runbook documents adding a dataset/dashboard/Space · customer has owner/admin access transferred.

### Smoke test (run after every deploy — wire into the deploy script)

```bash
# Dashboard rendered without errors (status, not just existence)
aws quicksight describe-dashboard --aws-account-id {account_id} \
  --dashboard-id {prefix}-quality-dashboard --region {region} \
  --query 'Dashboard.Version.{Status:Status,Errors:Errors}'
# Expected: Status=CREATION_SUCCESSFUL, Errors=[]

# Each dataset's last refresh succeeded
aws quicksight list-ingestions --aws-account-id {account_id} \
  --data-set-id {prefix}-quality-inspections --region {region} \
  --query 'Ingestions[0].{Status:IngestionStatus,Rows:RowInfo,Time:CreatedTime}'
# Expected: Status=COMPLETED with rows > 0

# KPI numerical accuracy — reconcile source vs mart for each KPI (the most-missed check).
# For each mart with a validation_sql in platform.yaml, run it and the source-equivalent and diff:
aws athena start-query-execution --work-group {workgroup} --region {region} \
  --query-string "SELECT SUM(daily_production_qty) FROM {prefix}_db.mart_daily_production"
# Compare the result with the SOURCE aggregate (SELECT SUM(quantity_good) FROM base_mes_production)
# and with the dashboard-displayed value. All three within 1% → pass. See dashboard-patterns.md §8.
```

A half-broken dashboard should fail the build, not be discovered by the customer in the demo. **A dashboard with WRONG NUMBERS is worse than half-broken — it looks finished and misleads the customer. Numerical accuracy (§7 step 4) is the gate, not STRICT.**

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

- **🔴 Critical Rules** — the 5 fatal rules (top of file)
- **Reference files** — when to load each `reference/*.md`
- §1 Prerequisites & Inputs — region first, current-state, inputs, follow-ups, preconditions
- §2 Architecture Overview — diagram + default data path
- §3 Build Flow — the 10-step flow with reference pointers
- §4 Data Source & Dataset — Athena/Iceberg/Hive/native, SPICE
- §5 Dashboards — plan-approval, validation, gotchas, update flow
- §6 SPICE — refresh policy, capacity (per-region)
- §7 Post-deploy Bootstrap — execute yourself
- §8 Output Contract — ARCHITECTURE.md + platform.yaml
- §9 Iteration & Maintenance — stack separation, extending
- §10 Validation Checklist — connectivity, datasets, dashboards, chat, access, ops
- Reference: `region-constraints.md`, `quicksight-cdk.md`, `dashboard-patterns.md`, `chat-agent.md`, `iam-permissions.md`
