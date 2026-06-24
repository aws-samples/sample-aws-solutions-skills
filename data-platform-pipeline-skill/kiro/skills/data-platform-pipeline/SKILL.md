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

This skill builds the **ingestion → storage → catalog → query** layers of a serverless data platform. The output is a working CDK TypeScript project plus the Glue scripts and Athena DDL needed to run it. Downstream consumption (Quick Sight, dashboards, chat agents) is out of scope here — this skill stops at "data is queryable in Athena."

The skill is opinionated: best practices are baked in, not presented as options. If a choice has a clear winner for serverless analytics on AWS, the skill picks it.

> **Language**: Always respond in the language the user uses. If the user writes in Korean, respond in Korean. If English, respond in English. Code and CDK output are always in English regardless of conversation language.

> **Execution Model**: This skill does NOT just generate code for the user to run manually.
> You ARE the builder. You have terminal access. Generate the CDK project, then:
> 1. Install dependencies (`npm install`)
> 2. Synthesize (`cdk synth`) — fix any errors before proceeding
> 3. Deploy (`cdk deploy --all --require-approval never`)
> 4. Run post-deploy verification (crawlers, queries, smoke tests)
> 5. If anything fails, diagnose, fix, and retry automatically
> 6. Only ask the user when a DECISION is needed (not for execution permission)
>
> The user's role is to provide business context and approve architecture decisions.
> YOUR role is to build, deploy, verify, and iterate until it works.
>
> | Agent does silently | Agent asks user |
> |---|---|
> | `npm install`, `cdk synth`, `cdk deploy` | "Deploy to production?" (if environment=production) |
> | Run crawlers, check schemas | "Column names don't match any known pattern — here's what I see: […]. Which mapping is correct?" |
> | Fix column mismatches (if obvious mapping) | "I found 3 possible interpretations. Which one?" |
> | Run smoke tests | Report results: "✅ All tables have data, views working" |
> | Auto-retry on transient errors | "This error persists after 3 retries: [error]. Need your input." |
> | Update ARCHITECTURE.md | — |

---

## 1. Prerequisites & Inputs

### Current state assessment (ask FIRST, before other questions)

Before starting any work, determine what already exists. Present as interactive choice:

```
What is the current state of your data platform?
  a) Starting from scratch — nothing exists yet
  b) Some infrastructure exists — I have an ARCHITECTURE.md or similar doc describing it
  c) Partial build — S3 buckets exist but no Glue/ETL yet
  d) Glue Catalog is set up — raw data is already cataloged, need ETL + views
  e) Pipeline exists — adding a new data source to existing platform
  f) Let me describe the current state: ___
```

**If user picks (b):** Ask for the path to the architecture doc. Read it and incorporate existing state — do NOT recreate what already exists.

**If user picks (c)–(e):** Ask which specific components exist. Skip those steps in the workflow.

**If user picks (f):** Let them describe, then confirm your understanding before proceeding.

**Key principle:** Never deploy infrastructure that already exists. Always check first.

### Ask the user for these inputs at the start. Do not proceed until all are collected.

| Input | Example | Notes |
| --- | --- | --- |
| `project_prefix` | `acme` | Lowercase, kebab-friendly. Used as the naming convention for every resource. |
| `aws_region` | `ap-northeast-2`, `us-west-2` | Must match where the customer wants the data lake. |
| `source_type` | `jdbc` / `s3` / `cdc` | Drives the decision tree in §3. |
| `source_details` | See below | DB endpoint + Secrets Manager ARN, OR existing S3 path. |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by vendor" | Drives table selection and Athena view design. |

**`source_details` shape by source type:**

- **JDBC**: `{ engine: "sqlserver"|"mysql"|"postgresql"|"oracle", host, port, database, secret_arn, tables: [...] }`
- **S3**: `{ bucket, prefix, format: "csv"|"json"|"parquet" }`
- **CDC**: Out of scope — see §3.

### Follow-up questions (ask after receiving initial inputs)

After collecting the primary inputs, ask these follow-up questions to refine the pipeline design. **Always provide a recommended default** — the user can accept the recommendation or override.

| # | Question | Why it matters | Recommended default |
|---|----------|----------------|---------------------|
| 1 | "What's the approximate data volume? (rows per day and total historical size)" | Determines Glue DPU allocation and cost guardrails | **Recommended: <1M rows/day, <100GB total** (adjusts DPU to 2, standard for most Data Lab builds) |
| 2 | "How often should the pipeline run? (daily / hourly / weekly)" | Sets the Glue trigger schedule | **Recommended: Daily at 02:00 UTC** (off-peak, sufficient for most batch analytics) |
| 3 | "What are the relationships between tables? (which columns join them?)" | Drives Athena view JOIN design | **Recommended: Infer from column names** (skill will match `product_code`, `supplier_id`, etc. If ambiguous, ask for clarification) |
| 4 | "Are there code-to-name mappings needed? (e.g., status codes like 1='active', 2='inactive')" | Drives CASE statement generation in Athena views | **Recommended: Yes, generate from source data** (skill will query DISTINCT values and propose mappings) |
| 5 | "Preferred partitioning strategy for curated data?" | Affects query cost and performance in Athena | **Recommended: Partition by date (year/month)** — optimal for time-series queries which are the most common pattern |
| 6 | "Any sensitive columns that need masking or exclusion?" | Drives which columns to drop or hash in the transform step | **Recommended: None** (include all columns; add masking later via Lake Formation when governance is enabled) |

If the user says "just use the defaults" or "go with your recommendations", accept ALL defaults and proceed without further questions.

> **Interaction pattern:** Present each question ONE AT A TIME as a multiple-choice prompt with the recommended default highlighted. Do NOT dump all questions at once as a text block. Example format:
>
> ```
> [1/6] What's the approximate data volume?
>   a) < 1M rows/day, < 100GB total (recommended ✓)
>   b) 1M-10M rows/day, 100GB-1TB
>   c) > 10M rows/day, > 1TB
>   d) Custom input: ___
> ```
>
> Accept "use the recommendations" or "defaults" to skip all remaining questions.

### Account preconditions to verify before generating CDK

Run these checks (or instruct the user to run them) before writing any code:

```bash
# 1. Confirm active AWS identity matches the target account
aws sts get-caller-identity

# 2. Confirm region is set
aws configure get region

# 3. Lake Formation Data Lake Settings (see §6 for full handling)
aws lakeformation get-data-lake-settings --region {aws_region}

# 4. CDK bootstrap status
aws cloudformation describe-stacks --stack-name CDKToolkit --region {aws_region} 2>/dev/null \
  || echo "CDK NOT BOOTSTRAPPED — run: cdk bootstrap aws://ACCOUNT_ID/{aws_region}"

```

If any precondition fails, stop and surface the specific error to the user with remediation steps.

---

## 2. Architecture Overview

```
                        ┌─────────────────────────────────────────────────┐
                        │                  AWS Account                    │
                        │                                                  │
  ┌──────────────┐      │  ┌─────────────┐      ┌──────────────────────┐ │
  │ Source       │      │  │ Glue        │      │ S3                   │ │
  │ - SQL Server │──────┼─▶│ ETL Job     │─────▶│ {prefix}-raw-zone/   │ │
  │ - MySQL      │ JDBC │  │ (ingest)    │      │   {source}/{table}/  │ │
  │ - Oracle     │      │  └─────────────┘      └──────────┬───────────┘ │
  └──────────────┘      │         │                        │             │
                        │         │ Crawler                │             │
                        │         ▼                        ▼             │
                        │  ┌──────────────┐      ┌──────────────────┐   │
                        │  │ Glue Catalog │◀─────│ Glue ETL         │   │
                        │  │ {prefix}_db  │      │ (transform)      │   │
                        │  │ raw_* tables │      └────────┬─────────┘   │
                        │  └──────┬───────┘               │             │
                        │         │                       ▼             │
                        │         │             ┌────────────────────┐  │
                        │         │             │ S3 curated-zone/   │  │
                        │         │             │  Parquet + Snappy  │  │
                        │         │             └─────────┬──────────┘  │
                        │         │                       │             │
                        │         ▼                       ▼             │
                        │  ┌─────────────────────────────────────────┐ │
                        │  │ Athena Workgroup ({prefix}-workgroup)   │ │
                        │  │   - Tables: {table}                      │ │
                        │  │   - Views:  v_{table} (enriched)         │ │
                        │  │   - Results: s3://{prefix}-analytics-…/  │ │
                        │  └─────────────────────────────────────────┘ │
                        │                                                │
                        └─────────────────────────────────────────────────┘
                                        │
                                        ▼
                              [downstream consumption layer]

```

---

## 3. Decision Trees

### Source type → ingestion approach

```
User's source type?
├── JDBC (SQL Server / MySQL / PostgreSQL / Oracle)
│   ├── Create Glue Connection (in VPC if source is private)
│   ├── Create Glue ETL Job using ingest-jdbc.py
│   ├── Schedule via Glue trigger (daily 02:00 KST default)
│   └── Output: Parquet in s3://{prefix}-raw-zone/{source}/{table}/
│
├── Existing S3 data (CSV / JSON / Parquet)
│   ├── Create Glue Crawler on existing S3 path
│   ├── If CSV/JSON: create transform job → Parquet
│   ├── If already Parquet: skip transform, only catalog
│   └── Output: Tables in Glue Catalog
│
│   ⚠ Multi-CSV under one prefix: if a single S3 prefix contains multiple CSVs
│      with DIFFERENT schemas (common for ERP exports), the crawler will collapse
│      them into ONE combined table. Use one of these patterns instead:
│
│      Preferred: one folder per logical table
│        s3://{bucket}/erp/quality_inspections/quality_inspections.csv
│        s3://{bucket}/erp/production_orders/production_orders.csv
│        s3://{bucket}/erp/suppliers/suppliers.csv
│
│      Or: separate s3Targets entries per file (when a flat folder can't be reorganized)
│        s3Targets: [
│          { path: `s3://${rawBucket.bucketName}/erp/quality_inspections/` },
│          { path: `s3://${rawBucket.bucketName}/erp/production_orders/` },
│          { path: `s3://${rawBucket.bucketName}/erp/suppliers/` },
│        ]
│
└── CDC (Change Data Capture)
    └── OUT OF SCOPE for this skill's main flow.
        Recommend: Use AWS DMS with S3 target (Parquet format).
        Point DMS at the source DB, configure full-load + ongoing CDC.
        Once data lands in S3, re-run this skill with source_type="s3".

```

### Format → transform needed?

```
Source format?
├── Parquet → catalog only (no transform job)
├── CSV / JSON / TSV / fixed-width → transform → Parquet+Snappy
├── XLSX / Excel → reject. Tell user to export to CSV first.
└── XML / proprietary → custom Glue script with appropriate library

```

> **The pipeline never downloads data.** All processing happens server-side via Glue (Spark). CSVs stay in S3 and are read directly by the Crawler and ETL Jobs — including multi-GB files. There is no scenario where this skill should suggest downloading a CSV locally before cataloging it.

---

## 4. Storage Layer

### Three buckets, always

| Bucket | Purpose | Format | Lifecycle |
| --- | --- | --- | --- |
| `{prefix}-raw-zone` | Landing zone, unmodified or near-original | Source format or Parquet | IA @ 90d, Glacier @ 365d |
| `{prefix}-curated-zone` | Cleaned, transformed, joined | **Parquet + Snappy always** | None (active data) |
| `{prefix}-analytics-zone` | Athena results, scratch, exports | Mixed | Delete incomplete MPU @ 7d |

### `lib/storage-stack.ts` (CDK pattern)

```typescript
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface StorageStackProps extends cdk.StackProps {
  prefix: string;
  environment: 'production' | 'development';
  dataClassification: 'internal' | 'confidential' | 'public';
  owner: string;
}

export class StorageStack extends cdk.Stack {
  readonly rawBucket: s3.IBucket;
  readonly curatedBucket: s3.IBucket;
  readonly analyticsBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    this.rawBucket = this.createBucket('Raw', `${props.prefix}-raw-zone`, props, [
      {
        id: 'archive-old-raw',
        transitions: [
          { storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(90) },
          { storageClass: s3.StorageClass.GLACIER, transitionAfter: cdk.Duration.days(365) },
        ],
      },
    ]);

    this.curatedBucket = this.createBucket('Curated', `${props.prefix}-curated-zone`, props, []);

    this.analyticsBucket = this.createBucket('Analytics', `${props.prefix}-analytics-zone`, props, [
      { id: 'cleanup-mpu', abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
    ]);
  }

  private createBucket(
    id: string,
    name: string,
    props: StorageStackProps,
    rules: s3.LifecycleRule[],
  ): s3.Bucket {
    const bucket = new s3.Bucket(this, id, {
      bucketName: name,
      encryption: s3.BucketEncryption.S3_MANAGED, // SSE-S3 default; parameterize for KMS CMK
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: rules,
      removalPolicy: cdk.RemovalPolicy.RETAIN, // never auto-delete data
    });
    cdk.Tags.of(bucket).add('project', props.prefix);
    cdk.Tags.of(bucket).add('environment', props.environment);
    cdk.Tags.of(bucket).add('data-classification', props.dataClassification);
    cdk.Tags.of(bucket).add('owner', props.owner);
    return bucket;
  }
}

```

> **Importing existing buckets:** If the customer already has S3 buckets they want to reuse (not created by this CDK app), replace the `createBucket` call with `s3.Bucket.fromBucketName(this, id, existingBucketName)` for those specific buckets. On a routine redeploy, CDK identifies buckets it owns by their logical ID — `RemovalPolicy.RETAIN` ensures the physical bucket is preserved across stack updates and destroys.

### Encryption

- Default: **SSE-S3** (`BucketEncryption.S3_MANAGED`).
- If customer requires CMK: parameterize a `kmsKeyArn` prop and switch to `BucketEncryption.KMS` with the imported key. Grant Glue/Athena roles `kms:Decrypt` and `kms:GenerateDataKey`.

---

## 5. Catalog & Schema

### Glue Database

- One database per project: `{prefix}_db` (e.g., `acme_db`).
- For multi-domain projects, use one DB per domain: `{prefix}_quality_db`, `{prefix}_sales_db`.
- Domain separation matters: Lake Formation permissions are per-database, and downstream tools (Quick Sight, Redshift Spectrum) bind to a database.

### Table naming convention

| Layer | Prefix | Example |
| --- | --- | --- |
| Raw (untransformed, from source) | `raw_{source}_` | `raw_sqlserver_quality_inspections` |
| Curated (cleaned Parquet) | *(no prefix)* | `quality_inspections` |
| Views (enriched, code→name joins) | `v_` | `v_quality_inspections` |

> ⚠ **Glue Crawler appends file extensions to table names** when crawling raw S3 files (e.g. `quality_inspections.csv` → `raw_quality_inspections_csv`, not `raw_quality_inspections`). Pick one:
> - **Accept the suffix** — update `TABLE_CONFIG` references and Athena view DDL to use the `_csv` form. Simplest.
> - **Override via crawler `Configuration`** — set `TableLevelConfiguration` and a `TableNameSeparator` policy in the crawler's `Configuration` JSON.
> - **Pre-organize without extensions** — store raw files under directory paths only (`s3://.../quality_inspections/part-0001` with no `.csv` suffix on the object key). Crawler then names tables from the directory.

### Crawlers

- One crawler per source domain (not per table — let it discover).
- Schedule: on-demand for first run, then daily after ETL job completes (chained via EventBridge).
- Skip if table already in catalog: handled via Crawler's built-in schema-change behavior (`UpdateBehavior: LOG`).

### Partitioning guidance

| Data shape | Partition by |
| --- | --- |
| Time-series, query by date | `year`, `month`, `day` (Hive-style: `year=2025/month=11/...`) |
| Multi-tenant SaaS | `tenant_id`, then `year/month` |
| Small reference data (<1 GB) | No partitions |
| Multi-region data | `region`, then time |

Rule of thumb: aim for 100 MB – 1 GB Parquet files per partition. Too small = many file overhead; too large = no parallelism benefit.

### `lib/catalog-stack.ts` (CDK pattern)

```typescript
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface CatalogStackProps extends cdk.StackProps {
  prefix: string;
  rawBucketName: string;
  curatedBucketName: string;
  environment: string;
  owner: string;
}

export class CatalogStack extends cdk.Stack {
  readonly database: glue.CfnDatabase;
  readonly crawlerRole: iam.Role;

  constructor(scope: Construct, id: string, props: CatalogStackProps) {
    super(scope, id, props);

    this.database = new glue.CfnDatabase(this, 'Database', {
      catalogId: cdk.Stack.of(this).account,
      databaseInput: {
        name: `${props.prefix}_db`,
        description: `${props.prefix} data lake catalog`,
      },
    });

    this.crawlerRole = new iam.Role(this, 'CrawlerRole', {
      roleName: `${props.prefix}-glue-crawler-role`,
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole')],
    });
    this.crawlerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [
        `arn:aws:s3:::${props.rawBucketName}`,
        `arn:aws:s3:::${props.rawBucketName}/*`,
        `arn:aws:s3:::${props.curatedBucketName}`,
        `arn:aws:s3:::${props.curatedBucketName}/*`,
      ],
    }));

    new glue.CfnCrawler(this, 'RawCrawler', {
      name: `${props.prefix}-raw-crawler`,
      role: this.crawlerRole.roleArn,
      databaseName: `${props.prefix}_db`,
      tablePrefix: 'raw_',
      targets: { s3Targets: [{ path: `s3://${props.rawBucketName}/` }] },
      schemaChangePolicy: { updateBehavior: 'LOG', deleteBehavior: 'LOG' },
      tags: { project: props.prefix, environment: props.environment, owner: props.owner },
    });

    new glue.CfnCrawler(this, 'CuratedCrawler', {
      name: `${props.prefix}-curated-crawler`,
      role: this.crawlerRole.roleArn,
      databaseName: `${props.prefix}_db`,
      targets: { s3Targets: [{ path: `s3://${props.curatedBucketName}/` }] },
      schemaChangePolicy: { updateBehavior: 'LOG', deleteBehavior: 'LOG' },
      tags: { project: props.prefix, environment: props.environment, owner: props.owner },
    });

    cdk.Tags.of(this).add('project', props.prefix);
    cdk.Tags.of(this).add('environment', props.environment);
    cdk.Tags.of(this).add('owner', props.owner);
  }
}

```

---

## 6. IAM & Security (Lake Formation)

> ⚠ **IAM `description` field allows only ASCII Latin-1 characters** (`[ -~¡-ÿ]`). Korean characters, em dashes (—), and arrows (→) will be rejected, causing the entire IAM stack to roll back. Use plain ASCII in role descriptions, then put Korean copy in CDK `Tags` or a separate `Glue table comment` instead.

### Korean / non-ASCII encoding rules across services

| Service | Korean / non-ASCII OK? | Notes |
|---|:---:|---|
| IAM role description | ❌ | ASCII Latin-1 only — rolls back the whole stack on violation |
| Glue table comments | ✅ | UTF-8 |
| Athena named query description | ✅ | UTF-8 |
| Quick Sight dashboard / visual titles | ✅ | UTF-8 |
| CDK feature flag values | ❌ | ASCII only |

### Per-function IAM roles (mandatory)

| Role | Used by | Permissions (summary) |
| --- | --- | --- |
| `{prefix}-glue-crawler-role` | Glue Crawlers | S3 read on raw+curated, Catalog write |
| `{prefix}-glue-etl-role` | Glue ETL Jobs | S3 read/write on all 3 buckets, Catalog read+write, Secrets Manager (JDBC), CloudWatch Logs |
| `{prefix}-athena-query-role` | Athena human/service queries | Catalog read, S3 read on curated, S3 write on analytics |
| `{prefix}-quicksight-role` | VPC connection / federated query scenarios only | Athena query, Catalog read, S3 read on curated + write on analytics results |

> **About `{prefix}-quicksight-role`:** This role is only needed when Quick Sight has to assume a custom role — typically VPC connections (private Redshift, RDS) or federated query setups. **A vanilla Athena-on-S3 dashboard does NOT use this role** — it uses the AWS-managed `aws-quicksight-service-role-v0` instead, with the `AWSQuickSightS3Policy` customer-managed policy controlling S3 bucket access. The consumption skill configures Quick Sight's S3 access separately (see consumption skill §11). Keep this role definition only if you know you'll need a VPC connection or federated query; otherwise omit it.

### Lake Formation IAM-only mode (the part everyone gets wrong)

Lake Formation has two coexisting permission models:

1. **IAM-based** (legacy default): IAM policies + the magic `IAMAllowedPrincipals` grant.
2. **LF-managed**: Explicit grants via `lakeformation:Grant*`.

For batch analytics where IAM is sufficient, the skill explicitly configures **IAM-only mode** so behavior is deterministic across accounts.

### Precondition check (run before deploy)

```bash
aws lakeformation get-data-lake-settings --region {aws_region} \
  --query 'DataLakeSettings.CreateDatabaseDefaultPermissions' --output json
```

**Interpret the result:**

- Output contains `"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}` → **good**, default IAM grant is in place.
- Output is `[]` (empty array) → **WARN the user**:

  > ⚠️ **Lake Formation strict mode detected.**
  > This account has `IAMAllowedPrincipals` revoked at the account level.
  > Glue tables created by this skill will NOT be accessible via IAM policies alone.
  >
  > **Options (pick one — do not let the skill auto-remediate):**
  >
  > 1. Ask your account/security admin to re-grant `IAMAllowedPrincipals` on `CreateDatabaseDefaultPermissions` and `CreateTableDefaultPermissions`. This is the simplest fix.
  > 2. Add explicit Lake Formation grants for each role this skill creates (Crawler, ETL, Athena, Quick Sight) on the database and tables. The skill will generate the LF grant CDK on request.
  > 3. Continue and accept that queries will fail with `Insufficient Lake Formation permissions` until grants are added.
  >
  > **Do not proceed with deploy until you confirm which option to take.**

If the precondition is OK, the skill explicitly adds an `IAMAllowedPrincipals` grant on each new database it creates (defense-in-depth — don't rely on account-level default).

### CDK pattern: explicit IAM-only grant on the database

```typescript
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';

new lakeformation.CfnPermissions(this, 'IAMAllowedDbPerm', {
  dataLakePrincipal: { dataLakePrincipalIdentifier: 'IAM_ALLOWED_PRINCIPALS' },
  resource: {
    databaseResource: {
      catalogId: cdk.Stack.of(this).account,
      name: `${props.prefix}_db`,
    },
  },
  permissions: ['ALL'],
}).addDependency(this.database);

```

### `lib/iam-roles` excerpt — Glue ETL role

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
    `arn:aws:s3:::${prefix}-curated-zone`, `arn:aws:s3:::${prefix}-curated-zone/*`,
    `arn:aws:s3:::${prefix}-analytics-zone`, `arn:aws:s3:::${prefix}-analytics-zone/*`,
  ],
}));
// Only if JDBC source:
etlRole.addToPolicy(new iam.PolicyStatement({
  actions: ['secretsmanager:GetSecretValue'],
  resources: [props.jdbcSecretArn],
}));

```

---

## 7. ETL Pipeline

### `glue-scripts/ingest-jdbc.py` — JDBC → S3 raw

```python
"""
JDBC ingestion to S3 raw zone.
Reads each table from the source DB and writes Parquet partitioned by ingestion_date.
"""
import sys
from datetime import datetime
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import lit
from awsglue.context import GlueContext
from awsglue.job import Job

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'CONNECTION_NAME', 'DATABASE', 'TABLES', 'TARGET_BUCKET', 'SOURCE_NAME', 'ENGINE',
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

ingestion_date = datetime.utcnow().strftime('%Y-%m-%d')
tables = args['TABLES'].split(',')
# Engine mapping: sqlserver→sqlserver, mysql→mysql, postgresql→postgresql, oracle→oracle
connection_type = args.get('ENGINE', 'sqlserver')

for table in tables:
    table = table.strip()
    print(f"Ingesting {args['DATABASE']}.{table}")
    df = glueContext.create_dynamic_frame.from_options(
        connection_type=connection_type,
        connection_options={
            'useConnectionProperties': 'true',
            'connectionName': args['CONNECTION_NAME'],
            'dbtable': table,
            'database': args['DATABASE'],
        },
    )
    df = df.toDF().withColumn('ingestion_date', lit(ingestion_date))
    output_path = f"s3://{args['TARGET_BUCKET']}/{args['SOURCE_NAME']}/{table}/"
    (df.write
       .mode('overwrite')
       .partitionBy('ingestion_date')
       .parquet(output_path))
    print(f"  → {output_path}")

job.commit()

```

### `glue-scripts/transform.py` — Raw → Curated

```python
"""
Raw → Curated transformation.
Cleans, casts, filters, and writes Parquet+Snappy to curated zone.
Customize the transform_table() function per business domain.
"""
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, trim, to_date, when

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'DATABASE', 'RAW_TABLE', 'CURATED_TABLE', 'TARGET_BUCKET',
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

raw = glueContext.create_dynamic_frame.from_catalog(
    database=args['DATABASE'], table_name=args['RAW_TABLE']
).toDF()

# Domain-specific cleanup — replace with actual transforms
curated = (raw
  .filter(col('deleted_flag') != 'Y')
  .withColumn('inspected_at', to_date(col('inspected_at')))
  .withColumn('vendor_id', trim(col('vendor_id')))
  .dropDuplicates(['inspection_id'])
)

(curated.write
   .mode('overwrite')
   .option('compression', 'snappy')
   .partitionBy('inspected_at')
   .parquet(f"s3://{args['TARGET_BUCKET']}/{args['CURATED_TABLE']}/"))

job.commit()

```

### `lib/pipeline-stack.ts` — Glue Job + Glue Trigger schedule

```typescript
new glue.CfnJob(this, 'IngestJob', {
  name: `${props.prefix}-ingest-jdbc`,
  role: props.etlRoleArn,
  command: {
    name: 'glueetl',
    pythonVersion: '3',
    scriptLocation: `s3://${props.scriptsBucket}/glue-scripts/ingest-jdbc.py`,
  },
  glueVersion: '4.0',
  numberOfWorkers: 2,
  workerType: 'G.1X',
  timeout: 120, // minutes
  defaultArguments: {
    '--CONNECTION_NAME': `${props.prefix}-jdbc-connection`,
    '--DATABASE': props.sourceDatabase,
    '--TABLES': props.sourceTables.join(','),
    '--TARGET_BUCKET': props.rawBucketName,
    '--SOURCE_NAME': props.sourceName,
    '--ENGINE': props.sourceEngine, // sqlserver | mysql | postgresql | oracle
    '--enable-metrics': 'true',
    '--enable-continuous-cloudwatch-log': 'true',
  },
  tags: { project: props.prefix, environment: props.environment, owner: props.owner },
});

// Native Glue scheduler — simpler than EventBridge for Glue-only triggers.
new glue.CfnTrigger(this, 'IngestTrigger', {
  name: `${props.prefix}-daily-trigger`,
  type: 'SCHEDULED',
  schedule: 'cron(0 17 * * ? *)', // 02:00 KST = 17:00 UTC, daily
  startOnCreation: true,
  actions: [{ jobName: `${props.prefix}-ingest-jdbc` }],
});

```

---

## 8. Query Layer (Athena)

### Workgroup config

```typescript
import * as athena from 'aws-cdk-lib/aws-athena';

new athena.CfnWorkGroup(this, 'Workgroup', {
  name: `${props.prefix}-workgroup`,
  state: 'ENABLED',
  workGroupConfiguration: {
    enforceWorkGroupConfiguration: true,
    publishCloudWatchMetricsEnabled: true,
    bytesScannedCutoffPerQuery: 1_000_000_000, // 1 GB cap — see §10
    resultConfiguration: {
      outputLocation: `s3://${props.analyticsBucket}/athena-results/`,
      encryptionConfiguration: { encryptionOption: 'SSE_S3' },
    },
  },
  tags: [
    { key: 'project', value: props.prefix },
    { key: 'environment', value: props.environment },
    { key: 'owner', value: props.owner },
  ],
});

```

### `athena-views/views.sql` — view DDL pattern

```sql
-- Enrichment view: join codes to human-readable names, derive metrics
CREATE OR REPLACE VIEW v_quality_inspections AS
SELECT
  qi.inspection_id,
  qi.inspected_at,
  qi.vendor_id,
  v.vendor_name,
  qi.product_id,
  p.product_name,
  qi.inspection_item_code,
  CASE qi.inspection_item_code
    WHEN '01' THEN '외관'
    WHEN '02' THEN '치수'
    WHEN '03' THEN '성분'
    ELSE '기타'
  END AS inspection_item_name,
  qi.defect_count,
  qi.total_count,
  CAST(qi.defect_count AS DOUBLE) / NULLIF(qi.total_count, 0) * 100 AS defect_rate_pct
FROM quality_inspections qi
LEFT JOIN vendors v ON qi.vendor_id = v.vendor_id
LEFT JOIN products p ON qi.product_id = p.product_id;

```

Patterns to apply:

- `CASE` statements for code→Korean name enrichment (matches business question phrasing)
- `LEFT JOIN` for dimension lookups, never `INNER JOIN` (avoid silent row drops)
- `NULLIF(denom, 0)` to avoid divide-by-zero
- `DATE_TRUNC('month', date_col) AS month` for time-series rollups

### Named queries for the user's business questions

For each business question collected as input, generate one named query in Athena:

```sql
-- Q: Top 5 defects by vendor (FY 2025)
SELECT vendor_name, SUM(defect_count) AS total_defects
FROM v_quality_inspections
WHERE inspected_at >= DATE '2025-01-01'
GROUP BY vendor_name
ORDER BY total_defects DESC
LIMIT 5;
```

> **CDK gotcha — `CfnNamedQuery.workGroup` does NOT auto-create a CloudFormation dependency.** Passing the workgroup name as a string literal lets CFN try to create the named query and the workgroup in parallel, which fails on fresh stacks. Either reference the workgroup token, or add the dependency explicitly:
>
> ```typescript
> const workgroup = new athena.CfnWorkGroup(this, 'Workgroup', { /* … */ });
>
> const namedQuery = new athena.CfnNamedQuery(this, 'ValidationQuery', {
>   workGroup: workgroup.ref,            // token — establishes a CFN dependency
>   database: `${props.prefix}_db`,
>   queryString: '...',
>   name: `${props.prefix}-vendor-top5`,
> });
> // OR, if you must use a string literal for the workgroup name:
> // namedQuery.addDependency(workgroup);
> ```

### Creating views from CDK — use a Python helper, not bash

Athena views can't be declared as CDK constructs (no `CfnView`). The skill provides a Python script that the CDK app invokes via `BucketDeployment` + a `CustomResource`, or the operator runs manually after `cdk deploy`.

```python
# scripts/run-views.py
"""
Apply CREATE OR REPLACE VIEW statements from a .sql file via Athena.
Use Python — bash awk-based SQL splitting is fragile with multi-line CASE statements.
"""
import boto3, re, sys, time


def run_views(workgroup: str, database: str, views_file: str, region: str):
    client = boto3.client('athena', region_name=region)
    with open(views_file) as f:
        sql = f.read()

    # Split by CREATE OR REPLACE VIEW boundaries
    statements = re.split(r'(?=CREATE\s+OR\s+REPLACE\s+VIEW)', sql, flags=re.IGNORECASE)
    statements = [s.strip().rstrip(';') for s in statements if s.strip()]

    for stmt in statements:
        view_name = re.search(r'VIEW\s+(\S+)', stmt, re.IGNORECASE).group(1)
        print(f"Creating view: {view_name}")
        response = client.start_query_execution(
            QueryString=stmt,
            WorkGroup=workgroup,
            QueryExecutionContext={'Database': database},
        )
        exec_id = response['QueryExecutionId']
        while True:
            status = client.get_query_execution(QueryExecutionId=exec_id)
            state = status['QueryExecution']['Status']['State']
            if state in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
                break
            time.sleep(1)
        if state != 'SUCCEEDED':
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'unknown')
            print(f"  FAILED: {reason}")
            sys.exit(1)
        print(f"  OK")


if __name__ == '__main__':
    run_views(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
```

Invoke it after deploy:
```bash
python3 scripts/run-views.py {prefix}-workgroup {prefix}_db athena-views/views.sql {region}
```

### Post-deploy bootstrap sequence — execute yourself, do NOT hand off to user

After `cdk deploy --all --require-approval never` succeeds, run this sequence yourself. Do NOT tell the user to run these commands. If any step fails, diagnose the error, fix the code, redeploy, and retry.

1. **Start the raw crawler** and wait for completion:

   ```bash
   aws glue start-crawler --name {prefix}-raw-crawler --region {region}
   # Poll until State = "READY"
   while true; do
     state=$(aws glue get-crawler --name {prefix}-raw-crawler --region {region} --query 'Crawler.State' --output text)
     [ "$state" = "READY" ] && break
     sleep 10
   done
   ```

2. **Verify raw table schemas** match what the transform/views expect:

   ```bash
   aws athena start-query-execution \
     --work-group {prefix}-workgroup \
     --query-string "SHOW COLUMNS IN {prefix}_db.raw_{table}" \
     --region {region}
   # Wait for SUCCEEDED, then get-query-results to read columns
   ```

3. **Compare actual column names with expected names** in `views.sql` and `transform.py`. If they differ:
   - **Obvious mapping** (e.g., `inspectionId` ↔ `inspection_id`, common case differences, snake/camel): auto-update `views.sql` JOIN keys + SELECT columns and `transform.py` column references, then redeploy `PipelineStack` and retry.
   - **Ambiguous** (multiple plausible mappings, or columns the source doesn't have at all): STOP and ask the user — present the actual columns vs. what the views expect, and let the user pick the mapping.

4. **Run the transform job** and wait for completion:

   ```bash
   aws glue start-job-run --job-name {prefix}-transform --region {region}
   # Poll get-job-run until JobRunState in (SUCCEEDED, FAILED, STOPPED)
   ```

5. **Start the curated crawler**:

   ```bash
   aws glue start-crawler --name {prefix}-curated-crawler --region {region}
   # Poll until READY (same pattern as step 1)
   ```

6. **Create views**:

   ```bash
   python3 scripts/run-views.py {prefix}-workgroup {prefix}_db athena-views/views.sql {region}
   ```

7. **Run smoke test**:

   ```bash
   python3 scripts/smoke-test.py --prefix {prefix} --region {region}
   ```

8. **Update ARCHITECTURE.md** with the actual table list, partition counts, and any column rename decisions made in step 3.

Report a single summary at the end:
> ✅ Pipeline deployed. Tables: [list]. Smoke test: passed. Views: [list]. Renames applied: [list or "none"].

Do not narrate every step to the user — just the final summary plus any decision points where you actually had to stop.

---

## 9. Data Quality Checks

After every pipeline run, execute these checks. Bake them into the README and provide as a CloudWatch Synthetic Canary or scheduled Athena named query.

```sql
-- A. Row count: target should be within 1% of source
SELECT COUNT(*) AS target_count FROM {prefix}_db.{table};
-- (compare manually with source DB count)

-- B. Null rate on key columns (inspection_id, vendor_id, etc.)
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN {key_column} IS NULL THEN 1 ELSE 0 END) AS nulls,
  ROUND(SUM(CASE WHEN {key_column} IS NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS null_pct
FROM {prefix}_db.{table};

-- C. Date range — confirms the latest partition loaded
SELECT MIN({date_col}) AS earliest, MAX({date_col}) AS latest
FROM {prefix}_db.{table};

-- D. Duplicate primary key check
SELECT {pk_column}, COUNT(*) AS occurrences
FROM {prefix}_db.{table}
GROUP BY {pk_column}
HAVING COUNT(*) > 1
LIMIT 10;

-- E. Referential integrity (if there are FK relationships)
SELECT a.{fk_column}
FROM {prefix}_db.{table} a
LEFT JOIN {prefix}_db.{dim_table} b ON a.{fk_column} = b.{pk_column}
WHERE b.{pk_column} IS NULL
LIMIT 10;

```

Generate one set of these queries per curated table and include them in `athena-views/quality-checks.sql`.

### Post-deploy smoke test

After `cdk deploy`, run the smoke test to verify data actually flows end-to-end:

```bash
python3 scripts/smoke-test.py --prefix {prefix} --region {region}
```

The smoke test script should:
1. Run each named query in the workgroup and assert `rows > 0`.
2. Verify each curated Glue table exists with `partition_count > 0`.
3. Run a sample `SELECT * FROM {table} LIMIT 10` against each curated table to confirm read access from the Athena workgroup with the configured IAM role.
4. Exit non-zero on any failure so it can be wired into CI / a deploy hook.

Generate this script as part of the project scaffolding alongside the CDK app.

---

## 10. Cost Guardrails

| Lever | Default | Rationale |
| --- | --- | --- |
| Glue Job `numberOfWorkers` | 2 (`G.1X`) | First runs are small; scale up only after measuring |
| Glue Job `timeout` | 120 minutes | Prevents runaway jobs from billing for hours |
| Athena `bytesScannedCutoffPerQuery` | 1 GB | Forces partitioning + column selection. Bumps to 10 GB only with cause. |
| S3 raw lifecycle | IA @ 90d, Glacier @ 365d | Raw is rarely re-read after curation |
| S3 analytics MPU cleanup | 7 days | Athena cancelled queries leave orphaned multipart uploads |
| Glue trigger schedule | Daily, off-peak | Don't ingest during business hours unless real-time |

### Monitoring

```typescript
// CloudWatch alarm: Glue job failure
new cloudwatch.Alarm(this, 'IngestJobFailure', {
  metric: new cloudwatch.Metric({
    namespace: 'AWS/Glue',
    metricName: 'glue.driver.aggregate.numFailedTasks',
    dimensionsMap: { JobName: `${prefix}-ingest-jdbc` },
    statistic: 'Sum',
    period: cdk.Duration.minutes(5),
  }),
  threshold: 1,
  evaluationPeriods: 1,
  alarmDescription: 'Glue ingest job has failed tasks',
});

// CloudWatch alarm: Athena scan exceeds budget
new cloudwatch.Alarm(this, 'AthenaScanBudget', {
  metric: new cloudwatch.Metric({
    namespace: 'AWS/Athena',
    metricName: 'ProcessedBytes',
    dimensionsMap: { WorkGroup: `${prefix}-workgroup` },
    statistic: 'Sum',
    period: cdk.Duration.hours(1),
  }),
  threshold: 100_000_000_000, // 100 GB/hour
  evaluationPeriods: 1,
});

```

---

## 11. Output Contract

The skill MUST generate a `README.md` at the project root documenting exactly what was created. Downstream skills and tools rely on this convention.

```markdown
# {prefix} Data Platform — Pipeline Layer

## Output Contract

### Naming Convention
- Project prefix: `{prefix}`
- Region: `{region}`
- Environment: `{environment}`

### Storage
- Raw:       `s3://{prefix}-raw-zone/{source}/{table}/`
- Curated:   `s3://{prefix}-curated-zone/{table}/`
- Analytics: `s3://{prefix}-analytics-zone/athena-results/`

### Glue Catalog
- Database:  `{prefix}_db`
- Raw tables:     `raw_{source}_{table}`     (e.g., `raw_sqlserver_quality_inspections`)
- Curated tables: `{table}`                   (e.g., `quality_inspections`)
- Views:          `v_{table}`                 (e.g., `v_quality_inspections`)

### Athena
- Workgroup: `{prefix}-workgroup`
- Results:   `s3://{prefix}-analytics-zone/athena-results/`
- Scan cap:  1 GB per query

### IAM Roles
- Crawler:    `{prefix}-glue-crawler-role`
- ETL:        `{prefix}-glue-etl-role`
- Athena:     `{prefix}-athena-query-role`
- Quick Sight: `{prefix}-quicksight-role`  (created here, used by consumption layer)

### Validation Queries
See `athena-views/quality-checks.sql`. Run after each pipeline run.

### Schedule
Daily ingest job at 02:00 KST (17:00 UTC). Glue trigger: `{prefix}-daily-trigger`.

```

A consuming tool (BI, chat agent, downstream pipeline) only needs `{prefix}` and `{region}` — everything else is derived from this convention.

### Architecture Record (`ARCHITECTURE.md`)

The skill MUST create and maintain an `ARCHITECTURE.md` file in the CDK project root. Update it every time infrastructure is added or modified.

This file serves three purposes:
1. **Team reference** — engineers can read it to understand what's deployed
2. **AI agent context** — future AI agents (including this skill on re-run) read it to understand existing state
3. **Change log** — records what was built and when

Template:

```markdown
# {prefix} Data Platform — Architecture Record

## Current State
Last updated: {YYYY-MM-DD}

### Storage Layer
- Raw zone: `s3://{prefix}-raw-zone/` (SSE-S3, lifecycle: IA@90d, Glacier@365d)
- Curated zone: `s3://{prefix}-curated-zone/` (SSE-S3, Parquet+Snappy)
- Analytics zone: `s3://{prefix}-analytics-zone/` (Athena results)

### Catalog
- Database: `{prefix}_db`
- Tables: [list all tables with row counts and last crawl date]
- Views: [list all views]

### ETL
- Ingest jobs: [list with schedule]
- Transform jobs: [list]
- Trigger: `{prefix}-daily-trigger` (cron: 0 17 * * ? *)

### Query
- Athena workgroup: `{prefix}-workgroup` (scan limit: 1GB)
- Named queries: [list]

### IAM
- Crawler role: `{prefix}-glue-crawler-role`
- ETL role: `{prefix}-glue-etl-role`
- Query role: `{prefix}-athena-query-role`

### Lake Formation
- Mode: IAM-only (IAMAllowedPrincipals granted on {prefix}_db)

## Change Log
| Date | Change | By |
|------|--------|-----|
| {date} | Initial deployment — pipeline for {source_type} | {user/agent} |
```

**Rules:**
- Create `ARCHITECTURE.md` on first run
- Update it EVERY time infrastructure is added/modified (new table, new job, config change)
- If `ARCHITECTURE.md` already exists, READ it first to understand current state before making changes
- The file is the single source of truth for "what exists"

---

## 12. Teardown

> **Iteration tip — separate stacks for fast iteration.** CloudFormation rolls back the entire stack on any resource failure. For pipeline iteration (where Glue jobs and Athena views change frequently while storage and IAM stay stable), split into separate stacks:
> 1. **StorageStack** — S3 buckets, encryption, lifecycle (very stable)
> 2. **CatalogStack** — Glue database, crawlers, IAM roles (stable)
> 3. **PipelineStack** — Glue ETL jobs, triggers, named queries, views (changes every iteration)
>
> Dashboard / view iteration (~2 min/cycle) won't trigger rollback of upstream resources. Cross-stack references via `props` keep CDK happy.

Tearing down a data platform is a destructive operation. **You execute these commands yourself when the user asks for teardown — but always confirm intent first ("This will delete S3 data + Glue catalog. Confirm?") before running anything.** Buckets use `RemovalPolicy.RETAIN` precisely so `cdk destroy` cannot accidentally delete data.

```bash
# 1. Empty S3 buckets first (CDK can't delete non-empty buckets, and these have RETAIN)
aws s3 rm s3://{prefix}-raw-zone --recursive
aws s3 rm s3://{prefix}-curated-zone --recursive
aws s3 rm s3://{prefix}-analytics-zone --recursive

# 2. Manually delete buckets if desired
aws s3 rb s3://{prefix}-raw-zone
aws s3 rb s3://{prefix}-curated-zone
aws s3 rb s3://{prefix}-analytics-zone

# 3. Delete Glue database (drops all tables and views — confirm first)
aws glue delete-database --name {prefix}_db

# 4. Deregister Lake Formation resources (only if registered with LF)
aws lakeformation deregister-resource --resource-arn arn:aws:s3:::{prefix}-raw-zone || true
aws lakeformation deregister-resource --resource-arn arn:aws:s3:::{prefix}-curated-zone || true

# 5. CDK destroy — handles roles, jobs, crawlers, workgroup, Glue triggers
cdk destroy --all

```

For partial teardown (e.g., remove a single source from an existing lake), pass CDK context flags rather than running destroy:

```bash
cdk deploy --context source:sqlserver=remove

```

---

## 13. Batch-Only Scope

> **Scope:** This skill covers **batch** data ingestion and transformation. For real-time/streaming requirements, consider: `Kinesis Data Streams → Firehose → S3 (Iceberg format) → Athena`. That is a separate architecture pattern and is not covered here. Do not attempt to bolt Kinesis onto this pipeline — Iceberg + streaming compaction has different table-format and IAM requirements that warrant their own skill.

For sub-daily ingestion of small data: schedule the existing Glue job more frequently (hourly is reasonable). Below 15 minutes, switch to streaming.

---

## 14. When to Add Governance

Lake Formation strict mode, tag-based access control (LF-TBAC), and cross-account sharing add real complexity. Don't enable them by default. Trigger to add governance:

| Trigger | What to add |
| --- | --- |
| A second team needs access to a subset of tables | LF column/row-level grants per role |
| PII columns identified | Column masking via LF, plus tagging `data-classification=confidential` |
| Cross-account data sharing | LF cross-account grant + RAM share (or AWS DataZone) |
| "We need a data mesh" / "data products" | AWS DataZone (separate skill) |
| Audit/compliance requirement | CloudTrail data events on S3, plus LF audit trail |

The naming convention and tags this skill applies are pre-conditions for all of the above. Doing them now is cheap; retrofitting them later is expensive.

---

## Future-Proofing Summary

These choices were made now so that adding governance later is a CDK change, not a re-architecture:

- **Tags on every resource** → LF tag-based access control reads them directly.
- **Per-function IAM roles** → LF grants attach per-principal; one-role-per-job means clean grant boundaries.
- **One database per domain** (not `default`) → LF permissions are per-database; domain DBs map to teams.
- **Naming convention in README** → any tool (consumption, governance, monitoring) discovers resources without manual configuration.
- **Idempotent CDK** → same template handles greenfield and "add new source" without destroy/recreate.

---

## Section Map (for skill consumers)

1. Prerequisites & Inputs — what user provides, what must exist
2. Architecture Overview — ASCII diagram
3. Decision Trees — source type, format
4. Storage Layer — S3 buckets
5. Catalog & Schema — Glue DB, crawlers, naming, partitioning
6. IAM & Security — roles + Lake Formation IAM-only config
7. ETL Pipeline — Glue jobs, scripts, scheduling
8. Query Layer — Athena workgroup, views, named queries
9. Data Quality Checks — post-run validation
10. Cost Guardrails — DPU, scan caps, lifecycle, monitoring
11. Output Contract — what was created, naming, downstream discovery, ARCHITECTURE.md record
12. Teardown — destroy + manual cleanup
13. Batch-Only Scope — explicit non-goals
14. When to Add Governance — triggers for LF/DataZone/etc.

