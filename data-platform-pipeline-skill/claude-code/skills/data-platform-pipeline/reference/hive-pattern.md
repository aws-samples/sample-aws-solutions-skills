# Hive Pattern (Opt-in) — Full Reference

The classic three-bucket layout (raw → curated Parquet → Athena), cataloged by Glue Crawlers. Use **only** when the user explicitly opts in: existing Hive infra, compatibility needs, or S3 Tables unavailable in-region. The default path is Iceberg (see `SKILL.md` core flow + `reference/iceberg-cdk.md`).

This file is self-contained for the Hive path: storage, catalog/crawlers, ETL CDK, scheduling, and the crawler-based bootstrap. Glue job script bodies (`ingest-jdbc.py`, `transform.py`, `views.sql`) are in `reference/scripts.md`. JDBC/VPC connectivity (shared with Iceberg) is in `reference/vpc-connectivity.md`. The shared IAM block (ETL/crawler/Athena roles, Lake Formation IAM-only mode) is in `SKILL.md` §IAM.

---

## Architecture diagram (Hive)

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
                        └─────────────────────────────────────────────────┘
                                        │
                                        ▼
                              [downstream consumption layer]
```

---

## Storage Layer — three buckets, always

| Bucket | Purpose | Format | Lifecycle |
| --- | --- | --- | --- |
| `{prefix}-raw-zone` | Landing zone, unmodified or near-original | Source format or Parquet | IA @ 90d, Glacier @ 365d |
| `{prefix}-curated-zone` | Cleaned, transformed, joined | **Parquet + Snappy always** | None (active data) |
| `{prefix}-analytics-zone` | Athena results, scratch, exports | Mixed | Delete incomplete MPU @ 7d |

### `lib/storage-stack.ts`

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

> The Iceberg path reuses this stack's raw + analytics buckets but omits the curated bucket. Encryption: default SSE-S3; for a CMK, parameterize a `kmsKeyArn` prop, switch to `BucketEncryption.KMS`, and grant Glue/Athena roles `kms:Decrypt` + `kms:GenerateDataKey`.

---

## Catalog & Schema (Hive)

Under Iceberg the Namespace replaces the Glue Database and tables auto-register on write — skip **all** crawlers and use the Iceberg path instead. A `{prefix}_raw` external-table catalog is created (explicit `CREATE EXTERNAL TABLE` DDL, never a crawler) only for the Athena CTAS fallback.

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

### `lib/catalog-stack.ts`

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

> **Multi-CSV under one prefix:** if a single S3 prefix contains multiple CSVs with DIFFERENT schemas (common for ERP exports), the crawler collapses them into ONE combined table. Use one folder per logical table (`s3://{bucket}/erp/quality_inspections/quality_inspections.csv`, …), or give each file its own `s3Targets` entry when a flat folder can't be reorganized.

---

## ETL Pipeline (Hive) — `lib/pipeline-stack.ts`

The two-job `ingest-jdbc.py` → `transform.py` flow (scripts in `reference/scripts.md`). JDBC connectivity (Glue Connection, on-prem/VPC, connectivity test) applies to both patterns — see `reference/vpc-connectivity.md`.

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

// Raw -> Curated transform job (runs transform.py). The post-deploy bootstrap
// and the CloudWatch alarm reference this job by name as `{prefix}-transform`.
new glue.CfnJob(this, 'TransformJob', {
  name: `${props.prefix}-transform`,
  role: props.etlRoleArn,
  command: {
    name: 'glueetl',
    pythonVersion: '3',
    scriptLocation: `s3://${props.scriptsBucket}/glue-scripts/transform.py`,
  },
  glueVersion: '4.0',
  numberOfWorkers: 2,
  workerType: 'G.1X',
  timeout: 120, // minutes
  defaultArguments: {
    '--DATABASE': `${props.prefix}_db`,
    '--RAW_TABLE': props.rawTableName,         // e.g. raw_sqlserver_quality_inspections
    '--CURATED_TABLE': props.curatedTableName, // e.g. quality_inspections
    '--TARGET_BUCKET': props.curatedBucketName,
    '--enable-metrics': 'true',
    '--enable-continuous-cloudwatch-log': 'true',
  },
  tags: { project: props.prefix, environment: props.environment, owner: props.owner },
});

// Native Glue scheduler (SCHEDULED, cron) — the standard way to schedule a Glue
// job. Do NOT use EventBridge + Athena: Athena has no scheduler, and a Glue
// Trigger is the right tool for a Glue-only batch pipeline.
new glue.CfnTrigger(this, 'IngestTrigger', {
  name: `${props.prefix}-daily-trigger`,
  type: 'SCHEDULED',
  schedule: 'cron(0 17 * * ? *)', // 02:00 KST = 17:00 UTC, daily
  startOnCreation: true,
  actions: [{ jobName: `${props.prefix}-ingest-jdbc` }],
});

// Hive only: chain transform after ingest succeeds. (Iceberg has no transform job.)
new glue.CfnTrigger(this, 'TransformTrigger', {
  name: `${props.prefix}-transform-trigger`,
  type: 'CONDITIONAL',
  startOnCreation: true,
  predicate: {
    conditions: [{
      logicalOperator: 'EQUALS',
      jobName: `${props.prefix}-ingest-jdbc`,
      state: 'SUCCEEDED',
    }],
  },
  actions: [{ jobName: `${props.prefix}-transform` }],
});
```

---

## Hive post-deploy bootstrap (crawler-based)

Execute yourself after `cdk deploy --all --require-approval never` — do NOT hand off to the user. If a step fails, diagnose, fix the code, redeploy, retry.

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
   - **Obvious mapping** (`inspectionId` ↔ `inspection_id`, case/snake/camel): auto-update `views.sql` JOIN keys + SELECT columns and `transform.py` column references, then redeploy `PipelineStack` and retry.
   - **Ambiguous** (multiple plausible mappings, or columns the source doesn't have): STOP and ask the user — present actual columns vs. what the views expect, let the user pick.

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

8. **Update ARCHITECTURE.md** with the actual table list, partition counts, and any column rename decisions from step 3.

Report a single summary at the end:
> ✅ Pipeline deployed. Tables: [list]. Smoke test: passed. Views: [list]. Renames applied: [list or "none"].
