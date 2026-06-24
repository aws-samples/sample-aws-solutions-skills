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

> **Naming note (as of 2026):** The product formerly known as "Amazon QuickSight" is now branded as **Amazon Quick** (the platform), with **Quick Sight** as the BI feature inside it (dashboards, SPICE, embedded analytics). The natural-language analytics features (chat agents, Dataset Q&A) are part of Amazon Quick — there is no separate sub-brand. **Topics** remain Topics: a Quick Sight feature for curating semantic models that the chat agents and Dataset Q&A read from. The AWS CLI namespace and SDK action names have NOT been renamed — they are still `aws quicksight ...` and `quicksight.*`. This skill uses the new display names in prose and the legacy names in code/CLI examples.

This skill takes a queryable data source — typically Athena over a Glue Catalog,
but optionally Redshift, S3, or RDS — and produces a working consumption layer:
a Quick Sight account with datasets in SPICE, dashboards built around the
customer's business questions, an Amazon Quick chat agent grounded in a semantic
model, and Space-based access control for multi-team isolation.

The skill is opinionated. It picks SPICE over direct query, pins SPICE refresh
to off-peak, requires named synonyms for natural-language matching, and defaults
to one Space per team. These decisions can be overridden but the skill will not
present them as menu options.

This skill is self-contained: it never assumes any other skill has run. It creates
its own IAM role (`{prefix}-quicksight-role`) with minimum required permissions.
If another stack already owns a role of the same name (e.g., the pipeline layer
created a placeholder), see §11 for the import-vs-create handoff pattern.

> **Language**: Always respond in the language the user uses. If the user writes in Korean, respond in Korean. If English, respond in English. Code and CDK output are always in English regardless of conversation language.

> **Execution Model**: This skill does NOT just generate code for the user to run manually.
> You ARE the builder. You have terminal access. Generate the CDK project, then:
> 1. Install dependencies (`npm install`)
> 2. Synthesize (`cdk synth`) — fix any errors before proceeding
> 3. Deploy (`cdk deploy --all --require-approval never`)
> 4. Run post-deploy verification (Quick Sight CLI calls, dashboard validation, SPICE ingestion check)
> 5. If anything fails, diagnose, fix, and retry automatically
> 6. Only ask the user when a DECISION is needed (not for execution permission)
>
> The user's role is to provide business context (which questions to answer, which visuals look right) and approve architecture decisions.
> YOUR role is to build, deploy, verify, and iterate until it works.
>
> | Agent does silently | Agent asks user |
> |---|---|
> | `npm install`, `cdk synth`, `cdk deploy` | "Deploy to production?" (if environment=production) |
> | `aws quicksight create-data-source` / `create-data-set` / `create-dashboard` | "What visuals do you want on the executive sheet?" |
> | `--validation-strategy STRICT` dashboard validation | "Does this dashboard layout look right?" (after sharing a preview) |
> | `list-ingestions` to confirm SPICE ingestion succeeded | "I found 3 possible synonym mappings. Which one?" |
> | Auto-retry on transient errors | "This error persists after 3 retries: [error]. Need your input." |
> | Update ARCHITECTURE.md | — |
>
> **One genuine exception that DOES require user action:** `AWSQuickSightS3Policy` bucket allowlist (§11). The Quick Sight console toggle for "QuickSight access to AWS services" cannot be flipped from a CLI/CDK in all account configurations. If you detect the resulting "Could not get query execution ID" error after a dashboard creation, surface the exact remediation steps to the user (console path + the IAM policy patch fallback) and wait for confirmation before retrying.

---

## 1. Prerequisites & Inputs

### Current state assessment (ask FIRST, before other questions)

Before starting any work, determine what already exists. Present as interactive choice:

```
What is the current state of your analytics layer?
  a) Starting from scratch — Quick Sight not set up yet
  b) Architecture doc exists — I have an ARCHITECTURE.md or similar
  c) Quick Sight account exists — need datasets and dashboards
  d) Datasets exist in SPICE — need dashboards and/or chat agent
  e) Dashboards exist — adding chat agent / new datasets
  f) Let me describe the current state: ___
```

**If user picks (b):** Ask for the path to the architecture doc. Read it and incorporate existing state — do NOT recreate what already exists.

**If user picks (c)–(e):** Ask which specific components exist. Skip those steps in the workflow.

**If user picks (f):** Let them describe, then confirm your understanding before proceeding.

**Key principle:** Never deploy infrastructure that already exists. Always check first.

### Ask the user for these inputs at the start. Do not proceed until all are collected.

| Input | Example | Notes |
|---|---|---|
| `data_source_type` | `athena` / `redshift` / `s3` / `other` | Drives data source connector. |
| `data_source_details` | See below | Glue DB + workgroup, OR Redshift endpoint, OR S3 manifest. |
| `project_prefix` | `acme` | Optional. If set and matches `{prefix}_db` in Glue, the skill auto-discovers tables. |
| `aws_region` | `ap-northeast-2` | Where data lives. Amazon Quick chat features may need a different region — see §12. |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by vendor, next-month defect-rate forecast" | Drives dashboards, topics, and chat agent test cases. |
| `target_users` | "5 quality-team members, 2 executives" | Drives Space layout and persona scoping. |

**`data_source_details` shape by source type:**
- **Athena**: `{ glue_database: string, workgroup: string, results_bucket: string }`
- **Redshift**: `{ endpoint, port, database, secret_arn, vpc_id, subnet_ids[] }`
- **S3**: `{ manifest_uri: "s3://.../manifest.json", format: "csv"|"parquet" }`
- **Other**: `{ description }` — skill will recommend Athena federated query as a bridge.

### Follow-up questions (ask after receiving initial inputs)

After collecting the primary inputs, ask these follow-up questions to refine the consumption layer. **Always provide a recommended default** — the user can accept the recommendation or override.

| # | Question | Why it matters | Recommended default |
|---|----------|----------------|---------------------|
| 1 | "How often should SPICE refresh? (daily / hourly / real-time via direct query)" | Determines data freshness vs. cost | **Recommended: Daily at 06:00 UTC** (data is ready before business hours; minimizes SPICE refresh cost) |
| 2 | "What language should the chat agent respond in?" | Sets persona language and synonym definitions | **Recommended: Korean** (match the target users' primary language) |
| 3 | "Preferred dashboard style? (executive summary with KPIs / detailed operational / both)" | Drives dashboard layout and visual selection | **Recommended: Both** (one executive summary sheet + one operational detail sheet per business question) |
| 4 | "Any existing dashboards or reports to replicate?" | If migrating from another BI tool, we can match layout | **Recommended: No — build fresh** (optimized for Quick Sight's native visual types) |
| 5 | "How many Spaces (isolated groups) do you need?" | Determines multi-tenant architecture complexity | **Recommended: 1 Space** (single team access; add more Spaces later as user base grows) |
| 6 | "Should the chat agent have any restricted topics? (questions it should refuse to answer)" | Configures guardrails in the system prompt | **Recommended: Refuse predictions and forecasts** ("next-month forecast" type questions — the agent should say "Forecasting is not supported") |
| 7 | "What kind of dashboard do you want? (KPI summary / detailed operations / trend analysis / comparison analysis)" | Drives dashboard layout and visual selection | **Recommended: KPI summary + trend** (one executive sheet with KPI cards, one trend sheet with time-series + comparisons) |
| 8 | "What insights do you expect from the data? (I can also review your data structure and make suggestions)" | Proactive insight suggestion based on schema | **Recommended: get suggestions based on the data structure** — when picked, scan the available tables/columns and suggest: time-series opportunities (date column + numeric column), comparison opportunities (category column + metric), anomaly candidates (high-variance columns), TOP-N rankings (groupable dimension + metric) |

When the user picks "get suggestions" on Q8, the skill should enumerate columns from the dataset and propose 4–6 specific insights using the column patterns above. Don't propose insights for columns that don't exist — confirm against `aws glue get-table` first.

If the user says "just use the defaults" or "go with your recommendations", accept ALL defaults and proceed without further questions.

### Account preconditions

```bash
# 1. Confirm active AWS identity matches the target account
aws sts get-caller-identity

# 2. Check Quick Sight account status (CLI still uses 'quicksight')
aws quicksight describe-account-settings --aws-account-id $(aws sts get-caller-identity --query Account --output text) 2>&1 \
  || echo "Quick Sight not yet enabled in this account"

# 3. Check Amazon Quick chat / Topic availability in the target region — see §12 region table
aws quicksight list-topics --aws-account-id $(aws sts get-caller-identity --query Account --output text) --region {aws_region} 2>&1 \
  || echo "Amazon Quick Topics may not be available in this region"

# 4. If data_source_type=athena: validate the workgroup and database exist
aws athena get-work-group --work-group {workgroup} --region {aws_region}
aws glue get-database --name {glue_database} --region {aws_region}

# 5. Check SPICE capacity in the TARGET region (not just the identity region)
aws quicksight describe-account-settings --aws-account-id {account_id} --region {aws_region}
# Look for SpiceCapacityInBytes — if 0 in this region, see §6 SPICE quota note.
```

If Quick Sight is not enabled, the skill can enable it (Enterprise edition) — but only after explicit user confirmation, since it has cost implications.

### Region availability gate (do not skip)

Quick Sight dashboards are broadly available, but **Amazon Quick's agentic AI features (chat agents, Dataset Q&A, Topics) ship in a smaller and changing set of regions.** Before generating any CDK or running any setup, confirm whether `{aws_region}` supports the chat / Topics features:

```bash
aws quicksight list-topics --aws-account-id $(aws sts get-caller-identity --query Account --output text) --region {aws_region}
```

- **Succeeds (or returns an empty list):** `{aws_region}` supports Topics. Proceed in-region.
- **Fails with "not supported in this region" (or similar):** chat agents and Dataset Q&A are not available in `{aws_region}`. **Stop and walk the user through §12 (Region Selection Flow)** before generating any resources. Do not assume the probe alone is authoritative — the user's own knowledge of the region's capability supersedes the probe (the API surface and feature availability sometimes diverge).

The user's stated knowledge of region capability (e.g., "ap-northeast-2 doesn't support chat agents") **overrides** the probe result. Trust the user.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AWS Account                                     │
│                                                                              │
│  ┌──────────────────┐                                                        │
│  │ Data Source      │                                                        │
│  │  - Athena        │                                                        │
│  │  - Redshift      │                                                        │
│  │  - S3 manifest   │                                                        │
│  │  - RDS/federated │                                                        │
│  └────────┬─────────┘                                                        │
│           │                                                                  │
│           │ (read via Quick Sight data source)                               │
│           ▼                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐           │
│  │ Amazon Quick — Quick Sight (Enterprise)                       │           │
│  │                                                                │           │
│  │   Data Source ─► Dataset (SPICE) ─► Topic / Semantic Model    │           │
│  │                       │                       │                │           │
│  │                       ▼                       ▼                │           │
│  │                 ┌──────────┐         ┌──────────────────┐     │           │
│  │                 │ Analysis │         │ Amazon Quick     │     │           │
│  │                 │     ↓    │         │  chat agent      │     │           │
│  │                 │ Dashboard│         │  - persona       │     │           │
│  │                 └────┬─────┘         │  - guardrails    │     │           │
│  │                      │               │  - synonyms      │     │           │
│  │                      ▼               └────────┬─────────┘     │           │
│  │              ┌─────────────────────────────────┐              │           │
│  │              │ Spaces (one per team / use case)│              │           │
│  │              │  - assigned datasets             │              │           │
│  │              │  - assigned users/groups         │              │           │
│  │              │  - per-space chat persona        │              │           │
│  │              └─────────────────────────────────┘              │           │
│  └──────────────────────────────────────────────────────────────┘           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Source Discovery

Before configuring datasets, enumerate what's available so dashboards and topics
target real tables and views.

### If `data_source_type=athena` and `project_prefix` is set

```bash
# List databases that match the project prefix convention
aws glue get-databases --region {aws_region} \
  --query "DatabaseList[?starts_with(Name, '{prefix}_')].Name" --output table

# List tables and views in the target database
aws glue get-tables --database-name {prefix}_db --region {aws_region} \
  --query 'TableList[].{Name:Name, Type:TableType, Location:StorageDescriptor.Location}' \
  --output table

# Test connectivity from Athena
aws athena start-query-execution \
  --work-group {workgroup} \
  --query-string "SELECT table_name FROM information_schema.tables WHERE table_schema = '{prefix}_db'" \
  --region {aws_region}
```

Prefer **views** (tables prefixed `v_`) over raw curated tables for dashboarding —
views encode the join + enrichment logic that maps to business terms.

### If `data_source_type=redshift`

Run a connectivity probe before adding the Quick Sight connection:
```bash
# From a host that can reach the Redshift VPC
psql -h {endpoint} -p {port} -U {user} -d {database} -c "SELECT current_database(), current_user;"
```

Quick Sight connects to Redshift either over public endpoint (if enabled) or
through a VPC connection — set up the VPC connection first if Redshift is private:
```bash
aws quicksight create-vpc-connection \
  --aws-account-id {account_id} \
  --vpc-connection-id {prefix}-redshift-vpc \
  --name "{prefix} Redshift VPC" \
  --subnet-ids subnet-xxx subnet-yyy \
  --security-group-ids sg-xxx \
  --role-arn arn:aws:iam::{account_id}:role/{prefix}-quicksight-vpc-role \
  --region {aws_region}
```

### If `data_source_type=s3`

Validate the manifest file before referencing it as a Quick Sight data source:
```json
{
  "fileLocations": [
    { "URIPrefixes": ["s3://{prefix}-curated-zone/quality_inspections/"] }
  ],
  "globalUploadSettings": {
    "format": "PARQUET"
  }
}
```

---

## 4. Quick Sight Account & Namespace Setup

### One-time account initialization

If the account has never used Quick Sight:

```bash
aws quicksight create-account-subscription \
  --aws-account-id {account_id} \
  --edition ENTERPRISE \
  --authentication-method IAM_AND_QUICKSIGHT \
  --account-name "{prefix}-quicksight" \
  --notification-email {owner_email} \
  --region {aws_region}
```

**Why Enterprise:** Standard edition does not support row-level security, hourly
SPICE refresh, embedded analytics, or Amazon Quick chat agents and Topics. Use Enterprise.

### Namespace

For multi-tenant or multi-customer setups, create a namespace per project:
```bash
aws quicksight create-namespace \
  --aws-account-id {account_id} \
  --namespace {prefix} \
  --identity-store QUICKSIGHT \
  --region {aws_region}
```

For single-tenant, the `default` namespace is fine.

### Identity & user provisioning

Default to **IAM federated identity** for end users — don't create native
Quick Sight users unless the customer has no IdP. With IAM federation:

```bash
# Create a QS group per Space (see §10)
aws quicksight create-group \
  --aws-account-id {account_id} \
  --namespace {prefix} \
  --group-name "quality-team" \
  --description "Quality Management Team" \
  --region {aws_region}

# Register users from federated identity
aws quicksight register-user \
  --aws-account-id {account_id} \
  --namespace {prefix} \
  --identity-type IAM \
  --user-role READER \
  --iam-arn arn:aws:iam::{account_id}:role/{prefix}-qs-reader-federated \
  --session-name {user_email} \
  --email {user_email} \
  --region {aws_region}
```

User roles:
- `ADMIN` — SA, data team
- `AUTHOR` — analysts who build dashboards
- `READER` — business users who consume dashboards and chat

---

## 5. Data Source & Dataset Configuration

### Athena data source (most common path)

```bash
aws quicksight create-data-source \
  --aws-account-id {account_id} \
  --data-source-id "{prefix}-athena-source" \
  --name "{prefix} Athena" \
  --type ATHENA \
  --data-source-parameters '{
    "AthenaParameters": {
      "WorkGroup": "{workgroup}"
    }
  }' \
  --permissions "[{
    \"Principal\": \"arn:aws:quicksight:{aws_region}:{account_id}:group/{prefix}/admins\",
    \"Actions\": [\"quicksight:UpdateDataSourcePermissions\", \"quicksight:DescribeDataSource\",
                  \"quicksight:DescribeDataSourcePermissions\", \"quicksight:PassDataSource\",
                  \"quicksight:UpdateDataSource\", \"quicksight:DeleteDataSource\"]
  }]" \
  --region {aws_region}
```

### Datasets — one per business domain

A "domain" maps to one Athena view typically (e.g., `v_quality_inspections`). Don't
create one mega-dataset with everything joined — break by question area so SPICE
refresh is incremental and topic scope is bounded.

CDK example using `aws-cdk-lib/aws-quicksight`:

```typescript
import * as quicksight from 'aws-cdk-lib/aws-quicksight';

new quicksight.CfnDataSet(this, 'QualityInspectionsDataset', {
  awsAccountId: cdk.Stack.of(this).account,
  dataSetId: `${prefix}-quality-inspections`,
  name: '품질 검사 (Quality Inspections)',
  importMode: 'SPICE', // SPICE, not DIRECT_QUERY
  physicalTableMap: {
    'qi-physical': {
      relationalTable: {
        dataSourceArn: athenaDataSource.attrArn,
        catalog: 'AwsDataCatalog',
        schema: `${prefix}_db`,
        name: 'v_quality_inspections',
        inputColumns: [
          { name: 'inspection_id', type: 'STRING' },
          { name: 'inspected_at', type: 'DATETIME' },
          { name: 'vendor_id', type: 'STRING' },
          { name: 'vendor_name', type: 'STRING' },
          { name: 'inspection_item_name', type: 'STRING' },
          { name: 'defect_count', type: 'INTEGER' },
          { name: 'total_count', type: 'INTEGER' },
          { name: 'defect_rate_pct', type: 'DECIMAL' },
        ],
      },
    },
  },
  // Calculated fields applied at dataset level
  logicalTableMap: {
    'qi-logical': {
      alias: 'Quality Inspections',
      source: { physicalTableId: 'qi-physical' },
      dataTransforms: [
        {
          createColumnsOperation: {
            columns: [
              {
                columnName: 'pass_rate_pct',
                columnId: 'pass-rate-pct',
                expression: '100 - {defect_rate_pct}',
              },
            ],
          },
        },
        {
          tagColumnOperation: {
            columnName: 'inspected_at',
            tags: [{ columnGeographicRole: 'COUNTRY' /* or DATE tag */ }],
          },
        },
      ],
    },
  },
  permissions: [/* Space-scoped principals — see §10 */],
});
```

**Why SPICE not direct query:**
- Sub-second dashboard rendering (vs. several seconds for direct Athena)
- No surprise Athena scan costs from each dashboard interaction
- Decouples BI consumption from pipeline freshness

The cost of SPICE is staleness ≤ refresh interval. For most Data Lab use cases,
that's acceptable.

---

## 6. SPICE Configuration & Refresh Schedule

### Default refresh policy

| Dashboard criticality | Refresh schedule | Rationale |
|---|---|---|
| Standard reporting | Daily 04:00 KST (after pipeline runs) | Pipeline finishes ~03:00, SPICE refresh after |
| Critical / leadership | Hourly during business hours | Limits SPICE compute cost |
| Real-time | Don't use SPICE — use direct query or rebuild on streaming pattern | SPICE is not for sub-minute |

### Refresh schedule via CDK

```typescript
new quicksight.CfnRefreshSchedule(this, 'QualityRefresh', {
  awsAccountId: cdk.Stack.of(this).account,
  dataSetId: `${prefix}-quality-inspections`,
  schedule: {
    scheduleId: `${prefix}-quality-daily`,
    scheduleFrequency: {
      interval: 'DAILY',
      timeOfTheDay: '04:00',
      timezone: 'Asia/Seoul',
    },
    refreshType: 'FULL_REFRESH',
  },
});
```

> **CDK type definitions can lag CloudFormation.** If TypeScript rejects a property that CloudFormation accepts (e.g., `timezone` on `ScheduleFrequency`, `axisOffset` on a visual), use `addPropertyOverride` to bypass the type checker while still emitting valid CloudFormation:
>
> ```typescript
> const refresh = new quicksight.CfnRefreshSchedule(this, 'QualityRefresh', {
>   awsAccountId: cdk.Stack.of(this).account,
>   dataSetId: `${prefix}-quality-inspections`,
>   schedule: {
>     scheduleId: `${prefix}-quality-daily`,
>     scheduleFrequency: { interval: 'DAILY', timeOfTheDay: '04:00' },
>     refreshType: 'FULL_REFRESH',
>   },
> });
> refresh.addPropertyOverride('Schedule.ScheduleFrequency.Timezone', 'Asia/Seoul');
> ```
>
> Verify against the latest CFN docs (`AWS::QuickSight::*` resource reference) when CDK rejects a property — the underlying CFN schema almost always supports it.

### Incremental refresh (for large fact tables)

When SPICE dataset > 10 GB or refresh time > 30 minutes, switch to incremental:
```typescript
schedule: {
  scheduleId: `${prefix}-quality-incremental`,
  scheduleFrequency: { interval: 'DAILY', timeOfTheDay: '04:00', timezone: 'Asia/Seoul' },
  refreshType: 'INCREMENTAL_REFRESH',
  // Requires LookbackWindow on the dataset (see CreateDataSet API)
},
```

### Capacity sizing

Default SPICE capacity per account is 10 GB. Estimate per dataset:
```
SPICE size ≈ raw_rows × avg_row_size × compression_factor (~0.3 for typical data)
```

If projected usage exceeds default: `aws quicksight update-account-settings --default-namespace …`
to provision additional SPICE capacity (purchasable, separate cost line item).

> ⚠ **SPICE quota is per-region.** The default 10 GB lives in the Quick Sight **identity region only**. If you create datasets in a different region (common pattern: identity in `us-east-1`, data + Quick Sight resources in `ap-northeast-2`), the resource region starts at **0 GB SPICE** and dataset creation will fail with a capacity error.
>
> Two options:
> - **Purchase SPICE capacity in the resource region** via `aws quicksight purchase-spice-capacity-in-region` (or the console — Manage QuickSight → SPICE Capacity → switch region first), OR
> - **Use `DIRECT_QUERY` import mode** for cross-region datasets: set `importMode: 'DIRECT_QUERY'` on `CfnDataSet`. Trades sub-second SPICE rendering for live Athena queries, but avoids the capacity purchase.
>
> Verify before provisioning datasets: `aws quicksight describe-account-settings --aws-account-id {account_id} --region {resource_region}` and check `SpiceCapacityInBytes`.

---

## 7. Dashboard Design Patterns

> **Recommended approach for dashboards with > 5 visuals: UI-first, then export.** Build the dashboard interactively in the Quick Sight UI (drag visuals, configure field wells, tweak formatting), then export the definition and check it into the CDK project as the source of truth:
>
> ```bash
> aws quicksight describe-dashboard-definition \
>   --aws-account-id {account_id} \
>   --dashboard-id {dashboard_id} \
>   --region {region} > dashboards/{name}-definition.json
> ```
>
> Inline-from-scratch CDK definitions should be reserved for **simple dashboards (≤ 5 visuals) or demo scenarios**. The Quick Sight definition schema is large and the validation errors are not actionable — building in the UI is dramatically faster than guessing the right shape from the schema.

The skill picks layouts based on the customer's domain, derived from
`business_questions` input. If the domain is ambiguous, ask the user once and pick.

### Manufacturing (quality management)

| Component | Visual | Notes |
|---|---|---|
| Top row | KPI cards | Overall defect rate, total inspections, pass rate, MoM change |
| Mid row L | Time series | Monthly defect rate trend with reference line at target threshold |
| Mid row R | Bar chart | Defect count by inspection item, TOP 5 |
| Bottom row | Table | Vendor-level defect comparison, sortable |
| Filters | Date range, vendor, product, defect type | Persist across visuals |
| Drill-down | Lot → Process → Defect type | Use parameters + actions |

### Retail (sales / inventory)

| Component | Visual |
|---|---|
| Top row | Total revenue, order count, AOV, conversion rate |
| Mid row L | Daily/weekly sales time series |
| Mid row R | Sankey: traffic source → category → conversion |
| Bottom L | Geo map: regional sales |
| Bottom R | Product performance ranking table |

### General (any domain) — fallback

| Component | Visual |
|---|---|
| Header | 3–5 KPI summary cards |
| Primary | Time series (the main metric over time) |
| Secondary | Bar or pie comparison (the main metric by category) |
| Detail | Table with search and filter |
| Always | Date filter, category filter, export-to-CSV button |

### Dashboard CDK pattern

```typescript
new quicksight.CfnDashboard(this, 'QualityDashboard', {
  awsAccountId: cdk.Stack.of(this).account,
  dashboardId: `${prefix}-quality-dashboard`,
  name: '품질 관리 대시보드',
  sourceEntity: {
    sourceTemplate: {
      arn: templateArn, // built from a definition or imported
      dataSetReferences: [{
        dataSetPlaceholder: 'quality_inspections',
        dataSetArn: dataset.attrArn,
      }],
    },
  },
  permissions: spacePermissions, // see §10
  versionDescription: 'Initial Data Lab build',
});
```

For complex dashboards, build the definition JSON via the Quick Sight UI, export
with `aws quicksight describe-dashboard-definition`, and check the JSON into the
CDK project as the source of truth.

### Common dashboard definition gotchas

These are the validation errors that bite every Data Lab build. Most surface only at deploy time and trigger full CFN rollback — fix them in the definition before deploying.

| Issue | Symptom | Fix |
|---|---|---|
| `KPIVisual` comparison requires `TargetValue` | CFN rollback "invalid visual configuration" | Add a `TrendGroup` or `TargetValue` field well; KPIs without one fail validation |
| `NumericalMeasureField` rejects STRING columns | "column type mismatch" even when the aggregate is `COUNT` | Use `categoricalMeasureField` for STRING columns (with `COUNT` aggregation); `numericalMeasureField` is INTEGER/DECIMAL only |
| `formatConfiguration` double-wrapping | "unexpected property" or schema-mismatch error | Correct shape: `{ formatConfiguration: { percentageDisplayFormatConfiguration: {…} } }`. Don't nest a second `formatConfiguration` inside. |
| `StringFilter` requires explicit `nullOption` | Validation error on filter | Always include `nullOption: 'ALL_VALUES'` (or `NON_NULLS_ONLY` if intentional) |

### Pre-flight dashboard validation (no deploy needed)

Run validation against a temp dashboard before committing the definition to CDK. Same validation engine as deploy; runs in ~2 seconds; no CFN rollback.

```bash
aws quicksight create-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id "validation-temp-$(date +%s)" \
  --name "Validation Test" \
  --definition file://dashboards/quality-definition.json \
  --validation-strategy STRICT \
  --region {region}

# Check the result, then delete:
aws quicksight delete-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id "validation-temp-..." \
  --region {region}
```

Iterate the definition against this command until clean, then commit and `cdk deploy`.

---

## 8. Amazon Quick Chat Agent Setup (Dataset Q&A)

Amazon Quick chat agents (Dataset Q&A on top of Quick Sight Topics) provide natural language Q&A grounded in topics
(semantic models). The chat agent's quality depends almost entirely on:
1. The persona / system prompt
2. Synonym coverage in the topic
3. Whether speculation guardrails are enforced

### Persona

Pick a domain expert role and write it as if briefing a new analyst:

| Domain | Persona |
|---|---|
| Manufacturing | "You are a quality analytics expert for cosmetics manufacturing. You help quality managers understand defect trends, vendor performance, and inspection outcomes." |
| Retail | "You are a sales and inventory analyst for an online bookstore. You help merchandisers understand product performance, regional trends, and customer behavior." |
| Finance | "You are a financial analyst supporting a corporate planning team. You answer questions about revenue, expenses, and budget variance using only verified ledger data." |

### System prompt rules (mandatory — paste verbatim into the topic description)

```
RULES (always follow):
1. Answer ONLY based on data available in the topic. Never speculate, estimate,
   or extrapolate beyond what the data shows.
2. If the data is insufficient to answer the question, explicitly state what
   additional data would be needed. Do not guess.
3. Always cite which dataset or table your answer comes from (e.g., "from
   v_quality_inspections, partition inspected_at=2025-11").
4. When showing numbers, always include the time period and any filters
   applied (e.g., "FY 2025 Q3, vendors in 'tier-1' segment").
5. If asked to predict, forecast, or estimate future values, refuse with:
   "I cannot make predictions — I can only summarize historical data. To
    forecast, please use a forecasting tool with this data as input."
6. If the user's terminology doesn't match the data dictionary, ask a
   clarifying question rather than guessing.
```

### Test questions (generated from `business_questions`)

For each customer domain, generate at least 5 test questions covering these categories:

| Category | Example (Manufacturing) | Expected behavior |
|---|---|---|
| Simple lookup | "2024년 검사 건수는?" | Returns single number with period |
| Trend analysis | "월별 불량률 추세를 보여줘" | Returns time series, cites table |
| Comparison | "거래처별 불량 TOP 5" | Returns top-N with sort and limit |
| Filter combination | "2025년 1분기에 외관 검사 불량률" | Multi-condition filter |
| Refusal — speculation | "다음 달 불량률을 예측해줘" | Refuses per rule 5, suggests forecasting tool |
| Refusal — out-of-scope | "이 거래처의 신용등급은?" | States data not in topic, suggests where to look |

Document these as a `test-cases.md` file alongside the topic so the customer
can re-validate after schema changes.

### Topic creation

> Verify against the latest Quick Sight CLI reference before running — the topic API structure (`Name`, `DataSets`, `NamedEntities`, etc.) evolves. Run `aws quicksight create-topic help` (CLI namespace is still `quicksight`) and confirm the `--topic` JSON shape matches the current shape.

```bash
aws quicksight create-topic \
  --aws-account-id {account_id} \
  --topic-id "{prefix}-quality" \
  --topic '{
    "Name": "품질 관리 토픽",
    "Description": "[paste persona + rules from above]",
    "DataSets": [{
      "DatasetArn": "arn:aws:quicksight:{region}:{account}:dataset/{prefix}-quality-inspections",
      "DatasetName": "Quality Inspections",
      "DatasetDescription": "월별 검사 결과 및 불량 데이터",
      "Filters": [],
      "Columns": [/* see §9 semantic model */],
      "CalculatedFields": [/* see §9 */],
      "NamedEntities": [/* see §9 synonyms */]
    }]
  }' \
  --region {aws_region}
```

### Response validation checklist

After topic creation, run each test question and verify:
- [ ] Agent uses data from the cited table
- [ ] Numbers include time period and filters
- [ ] Refusal questions are refused (not answered with speculation)
- [ ] Synonyms work (Korean and English variations)
- [ ] Out-of-scope questions explain what's missing rather than guessing

If any check fails, iterate on synonyms (§9) before iterating on the persona.

---

## 9. Semantic Model / Topic Definition

The semantic model is what bridges raw column names and business questions. Three pieces:

### A. Column → business term mapping

```yaml
# Saved to semantic-model.yaml alongside the topic
columns:
  vendor_id:
    business_name: "공급업체 ID"
    description: "Unique identifier for vendor"
    visible: false  # hide IDs from natural language, expose names
  vendor_name:
    business_name: "공급업체"
    synonyms: ["거래처", "supplier", "vendor"]
    description: "Vendor or supplier name"
  defect_count:
    business_name: "불량 건수"
    synonyms: ["불량", "defect count", "결함 수"]
    aggregation: SUM
  defect_rate_pct:
    business_name: "불량률"
    synonyms: ["불량률", "defect rate", "결함률"]
    aggregation: AVERAGE
    format: "percent_2_decimal"
  inspected_at:
    business_name: "검사일"
    synonyms: ["검사 시간", "inspection date", "검사일자"]
    semantic_type: DATE
```

### B. Calculated fields

Push business logic into the dataset, not into each visual:

```sql
-- defect_rate_pct already in the view, but topic adds derived metrics:
"YoY 불량률 변화" = ({defect_rate_pct} - LAG({defect_rate_pct}, 12) OVER (ORDER BY {month})) / LAG({defect_rate_pct}, 12) OVER (ORDER BY {month})

"Pass Rate" = 100 - {defect_rate_pct}

"Defect Severity Score" = {defect_count} * CASE {defect_severity}
  WHEN 'critical' THEN 10
  WHEN 'major' THEN 3
  WHEN 'minor' THEN 1
  ELSE 0
END
```

### C. Named entities & metrics

Metrics are columns the chat agent treats as first-class:

```yaml
metrics:
  total_defects:
    expression: "SUM({defect_count})"
    name: "총 불량 건수"
    synonyms: ["전체 불량", "불량 합계"]
    filterable_by: [vendor_name, inspected_at, inspection_item_name]
  avg_defect_rate:
    expression: "AVG({defect_rate_pct})"
    name: "평균 불량률"
    synonyms: ["평균 불량 비율"]

named_entities:
  vendor:
    columns: [vendor_id, vendor_name]
    primary_column: vendor_name
    synonyms: ["거래처", "공급업체", "supplier", "vendor"]
  inspection:
    columns: [inspection_id, inspected_at, inspection_item_name]
    primary_column: inspection_id
    synonyms: ["검사", "inspection"]
```

### Synonym coverage rule

For every business question in the input, identify the key terms and ensure each
has at least 2 synonyms in the topic:
- Original business term ("불량률")
- English equivalent ("defect rate")
- Common variant ("결함률")

Insufficient synonyms is the #1 cause of poor chat agent quality. Spend time here.

---

## 10. Space & Access Control

### Why Spaces matter

In multi-team environments, datasets, dashboards, and topics are scoped to Spaces.
Users in Space A cannot see Space B's data. This replaces ad-hoc folder permissions.

### Default Space layout

| Space | Members | Data | Notes |
|---|---|---|---|
| `{prefix}-admin` | SA, data team | All datasets and dashboards | Build/test environment |
| `{prefix}-{team}` | One team | Datasets relevant to their domain | One Space per business team |
| `{prefix}-leadership` | Executives | Curated KPI dashboard only | Read-only, no chat unless requested |

### Per-Space chat persona

The chat agent persona can differ per Space. Example:
- Quality team Space: "quality analytics expert" persona
- Sales team Space: "sales analyst" persona
- Leadership Space: "executive briefing analyst — concise, headline-oriented"

This is set by creating one Topic per Space and assigning each Topic a different
description (which carries the persona + rules).

### Space + group + permission CDK

```typescript
// One Quick Sight group per Space
// NOTE: There is no AWS::QuickSight::Group CloudFormation resource.
// Groups must be created via the QuickSight API. Use AwsCustomResource:
import * as cr from 'aws-cdk-lib/custom-resources';

const qualityGroup = new cr.AwsCustomResource(this, 'QualityGroup', {
  onCreate: {
    service: 'QuickSight',
    action: 'createGroup',
    parameters: { AwsAccountId: cdk.Stack.of(this).account, Namespace: prefix, GroupName: 'quality-team', Description: '품질팀 (Quality Team)' },
    physicalResourceId: cr.PhysicalResourceId.of(`${prefix}-quality-team-group`),
  },
  policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE }),
});

// Permissions on the dataset — only the quality group can see it
const qualityPermissions = [{
  principal: `arn:aws:quicksight:${region}:${account}:group/${prefix}/quality-team`,
  actions: [
    'quicksight:DescribeDataSet',
    'quicksight:DescribeDataSetPermissions',
    'quicksight:PassDataSet',
    'quicksight:DescribeIngestion',
    'quicksight:ListIngestions',
  ],
}];
// Pass these permissions to the CfnDataSet construct above.
```

### Row-level security (when one dataset spans multiple teams)

Sometimes you have one fact table that's used by multiple teams, each restricted
to their rows. Use a permissions dataset:

```sql
-- permissions table (CSV or Athena query)
GroupName,vendor_id
quality-team-tier1,V001
quality-team-tier1,V002
quality-team-tier2,V003
```

Apply via `aws quicksight create-data-set --row-level-permission-data-set ...`.
RLS is filtered on every query — no per-team duplicate datasets needed.

---

## 11. IAM & Permissions

### `{prefix}-quicksight-role` — service role for Quick Sight

> The IAM role name and CDK construct ID keep `quicksight` because the IAM service principal (`quicksight.amazonaws.com`) and the AWS CLI/SDK still use that legacy name. Renaming the role would break the trust relationship.

```typescript
// Always define the role. CDK manages it across deploys via its logical ID.
const qsRole = new iam.Role(this, 'QuickSightRole', {
  roleName: `${prefix}-quicksight-role`,
  assumedBy: new iam.ServicePrincipal('quicksight.amazonaws.com'),
});

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'AthenaAccess',
  actions: [
    'athena:BatchGetQueryExecution',
    'athena:GetQueryExecution',
    'athena:GetQueryResults',
    'athena:GetQueryResultsStream',
    'athena:ListQueryExecutions',
    'athena:StartQueryExecution',
    'athena:StopQueryExecution',
    'athena:ListWorkGroups',
    'athena:ListEngineVersions',
    'athena:GetWorkGroup',
    'athena:GetDataCatalog',
    'athena:GetDatabase',
    'athena:GetTableMetadata',
    'athena:ListDatabases',
    'athena:ListDataCatalogs',
    'athena:ListTableMetadata',
  ],
  resources: ['*'], // workgroup-level scoping below
}));

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'WorkgroupAccess',
  actions: ['athena:GetWorkGroup', 'athena:StartQueryExecution', 'athena:GetQueryExecution'],
  resources: [`arn:aws:athena:${region}:${account}:workgroup/${workgroup}`],
}));

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'GlueCatalogRead',
  actions: ['glue:GetDatabase', 'glue:GetDatabases', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
  resources: [
    `arn:aws:glue:${region}:${account}:catalog`,
    `arn:aws:glue:${region}:${account}:database/${prefix}_db`,
    `arn:aws:glue:${region}:${account}:table/${prefix}_db/*`,
  ],
}));

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'S3DataRead',
  actions: ['s3:GetObject', 's3:ListBucket', 's3:GetBucketLocation'],
  resources: [
    `arn:aws:s3:::${prefix}-curated-zone`,
    `arn:aws:s3:::${prefix}-curated-zone/*`,
  ],
}));

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'AthenaResultsWrite',
  actions: ['s3:PutObject', 's3:GetObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:AbortMultipartUpload'],
  resources: [
    `arn:aws:s3:::${prefix}-analytics-zone`,
    `arn:aws:s3:::${prefix}-analytics-zone/*`,
  ],
}));
```

> **If the role already exists** (e.g., another stack — the pipeline layer — created it with the same name): either import it via `iam.Role.fromRoleArn(this, 'QsRole', existingArn)` and skip the `new iam.Role(...)` block, or ensure both stacks define the role identically and let one own it. If both stacks try to create the same physical role, CloudFormation will fail on the second deploy with "role already exists" — pick one owner and have the other import.


### Cross-service binding

Quick Sight has an additional service-level resource permission concept (separate
from IAM): you must explicitly grant the Quick Sight service access to your S3
buckets and Athena workgroup via the Quick Sight admin console (or API):

```bash
aws quicksight update-account-settings ...
aws quicksight describe-account-settings ...
```

This is one of the rare AWS spots where IAM is necessary but not sufficient —
the Quick Sight service-level grant must also be in place. Document this in the
runbook.

### The real blocker: `AWSQuickSightS3Policy`

> Custom IAM role grants (the `{prefix}-quicksight-role` above) **don't matter for vanilla Athena-on-S3 dashboards** — Quick Sight uses the AWS-managed service role `aws-quicksight-service-role-v0` with a customer-managed policy named `AWSQuickSightS3Policy`. That policy contains an **explicit allowlist of S3 buckets**. If your buckets aren't on it, the dashboard fails with a misleading error.
>
> **Symptom:** When creating a DataSource or running a dashboard query, you see `"Could not get query execution ID"`. This looks like an Athena failure but is actually an S3 permission problem.
>
> **To fix:**
> 1. Quick Sight console → **Manage QuickSight** → **Security & Permissions** → **QuickSight access to AWS services** → under **S3**, add `{prefix}-curated-zone` and `{prefix}-analytics-zone` (and any other buckets the dashboards read).
> 2. **Or programmatically:** patch the policy directly.
>
> ```bash
> # Inspect the current policy version
> aws iam get-policy --policy-arn arn:aws:iam::{account_id}:policy/service-role/AWSQuickSightS3Policy
>
> # Get the document, add your buckets, create a new version, and set it as default
> aws iam get-policy-version \
>   --policy-arn arn:aws:iam::{account_id}:policy/service-role/AWSQuickSightS3Policy \
>   --version-id v1 > current-policy.json
> # …edit current-policy.json to include {prefix}-curated-zone and {prefix}-analytics-zone…
> aws iam create-policy-version \
>   --policy-arn arn:aws:iam::{account_id}:policy/service-role/AWSQuickSightS3Policy \
>   --policy-document file://current-policy.json \
>   --set-as-default
> ```
>
> Add this step to the deploy runbook — it is the most common silent failure during a Data Lab build.

### Lake Formation note

If the source account has Lake Formation in strict mode (IAMAllowedPrincipals
revoked), the IAM role above is not enough — you must also grant LF permissions
to the Quick Sight role:
```bash
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::{account}:role/{prefix}-quicksight-role \
  --resource '{"Database": {"Name": "{prefix}_db"}}' \
  --permissions SELECT DESCRIBE \
  --region {aws_region}
```

The skill detects this case via `aws lakeformation get-data-lake-settings` and
warns the user before deploying. Same precondition pattern as in the pipeline layer.

---

## 12. Region Constraints & Workarounds

Quick Sight dashboards are broadly available. **Amazon Quick's agentic AI features — chat agents, Dataset Q&A, and Topics — ship in a smaller, changing footprint.** This section is the decision flow when `{aws_region}` does not support those features.

### Known region support (as of 2026-05)

| Region | Quick Sight dashboards | Amazon Quick chat / Dataset Q&A / Topics |
|---|---|---|
| `us-east-1` (N. Virginia) | ✅ | ✅ |
| `us-west-2` (Oregon) | ✅ | ✅ |
| `eu-west-1` (Ireland) | ✅ | ✅ |
| `ap-northeast-2` (Seoul) | ✅ | ❌ — dashboards only, no chat agents |
| _other regions_ | verify at deploy time | verify at deploy time |

This table is a **snapshot, not authoritative**. Always confirm before generating CDK.

### Region selection flow

```
Step 1. Does {aws_region} support Amazon Quick's agentic AI features
        (chat agents, Dataset Q&A, Topics)?

        ├── YES → Deploy everything in {aws_region}. Skip to §5.
        │
        └── NO  → Present the user with two options. Do not pick for them.
```

#### Step 2 — present these options to the user when the region doesn't support chat

> **Option A — Dashboards only, in home region**
> - Deploy Quick Sight in `{aws_region}` for BI dashboards only
> - **No chat agent / Dataset Q&A**
> - Trade-off: simplest, lowest latency, no NL analytics

> **Option B — Full stack in a supported region**
> - Deploy Amazon Quick (Quick Sight + chat agents + Topics) in `us-east-1` (or another supported region the customer prefers)
> - Data stays in `{aws_region}` (S3, Glue, Athena unchanged)
> - Quick Sight queries the home-region Athena workgroup cross-region
> - Trade-off: full features; dashboard users connect to the supported region's console; cross-region Athena adds latency on direct-query datasets (less of an issue with SPICE)

#### Step 3 — let the user choose, then adjust the rest of the build

| If user picks | Adjust these sections |
|---|---|
| A (dashboards only) | Skip §8 (chat agent setup) and §9 Topics. Drop chat-related test cases from §14. |
| B (full stack in supported region) | All deploy commands run with `--region us-east-1` (or chosen region). Add cross-region Athena permissions to the `{prefix}-quicksight-role`. Document data-residency: data stays in `{aws_region}`; query metadata and chat transcripts transit the BI region. |

### Verify availability at deploy time

```bash
# Check Quick Sight region availability (CLI namespace is still 'quicksight')
aws quicksight list-namespaces --aws-account-id {account_id} --region {aws_region} 2>&1 | head

# Probe chat / Topics availability (Topics back the chat agent)
aws quicksight list-topics --aws-account-id {account_id} --region {aws_region}
# If this errors with "not supported in this region", Amazon Quick chat / Topics are not available.
```

The probe is a hint, not the source of truth. If the user states a region is unsupported (e.g., they have direct experience or internal AWS guidance), trust the user — feature availability and API surface sometimes diverge during regional rollout.

> **Amazon Quick's regional availability evolves.** Always verify against the Amazon Quick pricing page (https://aws.amazon.com/quick/pricing/) or run the `list-topics` probe above to confirm chat agent support before proceeding.

> **Migration note:** If the customer starts on Option B and `{aws_region}` later gains chat support, consolidating to in-region is a CDK redeploy plus a one-time Topic export/import (`describe-topic` → `create-topic` in the new region) and a SPICE dataset re-ingest.

---

## 13. Iteration Guide

> **Stack separation for fast iteration.** Deploy in three CDK stacks so dashboard iteration (~2 min/cycle) doesn't trigger rollback of upstream resources:
> 1. **DataSourceStack** — Athena/Redshift/S3 connection, Quick Sight IAM, AWSQuickSightS3Policy bucket allowlist (stable, rarely changes)
> 2. **DatasetStack** — SPICE datasets, refresh schedules, calculated fields (changes when schema evolves)
> 3. **DashboardStack** — analyses, dashboards, visuals, topics (changes frequently during development)
>
> Cross-stack references via `props` carry the dataset/data-source ARNs forward.

The first build is rarely the last. Document how to extend.

### Adding a new dataset (new domain comes online)

1. Confirm the source view exists (e.g., `v_sales_by_region`).
2. Add a new `CfnDataSet` referencing the view.
3. Add a refresh schedule (mirror existing cadence).
4. Either:
   - Add as a new SECTION on an existing dashboard (if related), or
   - Create a new dashboard.
5. Add to relevant Spaces' permissions.

### Adding a new dashboard

1. Build interactively in Quick Sight UI (faster iteration than CDK).
2. Once approved, export definition: `aws quicksight describe-dashboard-definition`.
3. Check JSON into the CDK project under `dashboards/{name}.json`.
4. Reference from CDK so the next deploy is reproducible.

### Adding to the chat agent's coverage

1. Identify gap from real user feedback (questions the agent failed to answer).
2. For each failed question, decide:
   - **Missing synonym** → add to topic's `NamedEntities`
   - **Missing calculated field** → add to dataset
   - **Missing data** → goes back to pipeline layer (out of scope here)
   - **Speculation drift** → strengthen rules in topic description
3. Re-test the failure case + a regression set of previously-working questions.

### Adding a new Space

1. Create Quick Sight group: `aws quicksight create-group ...`
2. Create dataset/dashboard permissions for the group.
3. If chat persona differs: create a new topic with its own description.
4. Register users into the group.

### Versioning dashboards

Dashboards have a `versionDescription` field. Use it. Bump on every deploy:
"v3 — added vendor drill-down per quality team request, 2026-01-15".

---

## 14. Post-deploy bootstrap sequence — execute yourself, do NOT hand off to user

After `cdk deploy --all --require-approval never` succeeds, run this sequence yourself. Do NOT tell the user to run these commands. If a step fails, diagnose, fix, redeploy, and retry.

1. **Confirm the data source is reachable**:

   ```bash
   aws quicksight describe-data-source \
     --aws-account-id {account_id} \
     --data-source-id "{prefix}-athena-source" \
     --region {region} \
     --query 'DataSource.{Status:Status,ErrorInfo:ErrorInfo}'
   # Expected: Status=CREATION_SUCCESSFUL, ErrorInfo=null
   ```

   If `Status` is `CREATION_FAILED` with an S3 error → `AWSQuickSightS3Policy` bucket allowlist is missing. STOP and surface §11 remediation to the user (this is the one genuine exception that needs human action). Otherwise auto-fix.

2. **Trigger and watch the first SPICE ingestion** for each dataset:

   ```bash
   aws quicksight create-ingestion \
     --aws-account-id {account_id} \
     --data-set-id {prefix}-quality-inspections \
     --ingestion-id "bootstrap-$(date +%s)" \
     --region {region}

   # Poll list-ingestions until IngestionStatus in (COMPLETED, FAILED, CANCELLED)
   aws quicksight list-ingestions \
     --aws-account-id {account_id} \
     --data-set-id {prefix}-quality-inspections \
     --region {region} \
     --query 'Ingestions[0].{Status:IngestionStatus,Rows:RowInfo,Err:ErrorInfo}'
   ```

3. **Pre-flight dashboard definition** with `--validation-strategy STRICT` BEFORE final commit (per §7):

   ```bash
   aws quicksight create-dashboard \
     --aws-account-id {account_id} \
     --dashboard-id "validation-temp-$(date +%s)" \
     --name "Validation Test" \
     --definition file://dashboards/quality-definition.json \
     --validation-strategy STRICT \
     --region {region}
   # Then delete-dashboard. If validation fails, fix the definition and retry.
   ```

4. **Verify the deployed dashboard**:

   ```bash
   aws quicksight describe-dashboard \
     --aws-account-id {account_id} \
     --dashboard-id {prefix}-quality-dashboard \
     --region {region} \
     --query 'Dashboard.Version.{Status:Status,Errors:Errors}'
   # Expected: Status=CREATION_SUCCESSFUL, Errors=[]
   ```

5. **Run topic chat agent test questions** (from §8) yourself via the API and assert each response category passes (lookup/trend/comparison/filter/refusal/out-of-scope). On a refusal failure (e.g., agent answers a forecasting question), iterate on synonyms and rules per §9 and re-run.

6. **Update ARCHITECTURE.md** with deployed datasets, dashboards, topics, Spaces, refresh schedules, and any synonym/persona iterations applied.

Report a single summary at the end:
> ✅ Quick Sight deployed. Datasets: [list, SPICE rows]. Dashboards: [list, status]. Topic test cases: passed/failed counts.

User-touch points (only):
- "What visuals do you want on the executive sheet?" (before §7)
- "Does this dashboard look right?" (after §7, with a preview link)
- `AWSQuickSightS3Policy` console fix if step 1 detects the S3 access error
- Any genuinely ambiguous synonym mapping that the agent cannot pick

Everything else is silent execution.

---

## 15. Architecture Record (`ARCHITECTURE.md`)

The skill MUST create and maintain an `ARCHITECTURE.md` file in the CDK project root. Update it every time infrastructure is added or modified.

This file serves three purposes:
1. **Team reference** — engineers can read it to understand what's deployed
2. **AI agent context** — future AI agents (including this skill on re-run) read it to understand existing state
3. **Change log** — records what was built and when

If `ARCHITECTURE.md` already exists (e.g., the pipeline skill created one), **append** the consumption-layer sections rather than overwriting. If it does not exist, create one with both pipeline placeholders (left empty if not deployed) and consumption sections.

Consumption-specific sections to add to the template:

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
```

Append a row to the Change Log table on each deploy:

```markdown
| {date} | Added Quick Sight datasets + dashboard for {domain} | {user/agent} |
```

**Rules:**
- Create `ARCHITECTURE.md` on first run if missing; otherwise append
- Update it EVERY time consumption-layer infrastructure is added/modified (new dataset, new dashboard, new topic, new Space)
- If `ARCHITECTURE.md` already exists, READ it first to understand current state before making changes
- The file is the single source of truth for "what exists"

---

## 16. Validation Checklist

Before handing off to the customer, verify all of these:

### Connectivity
- [ ] Quick Sight account is Enterprise edition
- [ ] Athena workgroup is reachable from Quick Sight
- [ ] Service-level S3 + Athena grants are configured (Quick Sight admin console)
- [ ] Lake Formation grants in place if account is in strict mode

### Datasets
- [ ] Each dataset is in SPICE (not direct query) unless explicitly justified
- [ ] Each dataset has a refresh schedule
- [ ] First refresh has succeeded (no SPICE ingestion errors in CloudWatch)
- [ ] Calculated fields render correctly (sample row inspection)

### Dashboards
- [ ] Each business question has at least one visual that answers it
- [ ] All visuals load in < 3 seconds
- [ ] Filters propagate across visuals as expected
- [ ] Export to CSV works
- [ ] Mobile view renders acceptably (test on phone)

### Chat agent (Amazon Quick / Dataset Q&A)
- [ ] Persona + rules in topic description (verbatim, not paraphrased)
- [ ] All 6 test question categories pass:
  - Simple lookup ✓
  - Trend ✓
  - Comparison ✓
  - Filter combination ✓
  - Speculation refusal ✓ (this is the most-missed)
  - Out-of-scope refusal ✓
- [ ] Korean and English synonyms both work
- [ ] Source citation appears in answers

### Access control
- [ ] Each user is in exactly one Space (no Space leakage)
- [ ] User in Space A cannot see Space B's dashboards (verified by login)
- [ ] Admin Space has full access
- [ ] Row-level security (if used) filters correctly per group

### Operational
- [ ] CloudWatch alarms on SPICE ingestion failures
- [ ] Cost: SPICE capacity sized appropriately, no surprise overages
- [ ] Runbook documents how to add a dataset, dashboard, and Space
- [ ] Customer has owner/admin access transferred (don't keep your SA account as sole admin)

### Smoke test (run after every deploy)

```bash
# Verify the dashboard rendered without errors (status, not just existence)
aws quicksight describe-dashboard \
  --aws-account-id {account_id} \
  --dashboard-id {prefix}-quality-dashboard \
  --region {region} \
  --query 'Dashboard.Version.{Status:Status,Errors:Errors}'
# Expected: Status=CREATION_SUCCESSFUL, Errors=[]

# Verify each dataset's last refresh succeeded
aws quicksight list-ingestions \
  --aws-account-id {account_id} \
  --data-set-id {prefix}-quality-inspections \
  --region {region} \
  --query 'Ingestions[0].{Status:IngestionStatus,Rows:RowInfo,Time:CreatedTime}'
# Expected: Status=COMPLETED with rows > 0
```

Wire this into the deploy script so a half-broken dashboard fails the build instead of being discovered by the customer in the demo.

---

## Section Map (for skill consumers)

1. Prerequisites & Inputs
2. Architecture Overview — ASCII
3. Data Source Discovery — enumerate Glue, validate Redshift, check S3 manifest
4. Quick Sight Account & Namespace Setup
5. Data Source & Dataset Configuration — Athena connector, SPICE
6. SPICE Configuration & Refresh Schedule
7. Dashboard Design Patterns — manufacturing, retail, general
8. Amazon Quick Chat Agent Setup (Dataset Q&A) — persona, rules, test questions
9. Semantic Model / Topic Definition — column mapping, calculated fields, synonyms
10. Space & Access Control — multi-tenant, RLS
11. IAM & Permissions — service role, cross-service, LF
12. Region Constraints & Workarounds — Amazon Quick chat / Topics availability
13. Iteration Guide — adding datasets, dashboards, topics, Spaces
14. Post-deploy bootstrap sequence — execute yourself, do NOT hand off to user
15. Architecture Record — ARCHITECTURE.md generation and maintenance
16. Validation Checklist — connectivity, datasets, dashboards, chat, access, ops
