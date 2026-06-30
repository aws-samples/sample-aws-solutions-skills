# Iceberg / S3 Tables — CDK Reference

CDK TypeScript blocks for the **Iceberg (default)** path. The thin core (`SKILL.md`) carries the core flow and the 🔴 critical rules; this file holds the full CDK code. Glue job script bodies live in `reference/scripts.md`; IAM/Lake-Formation grants common to both patterns are in the §5 (IAM & Security) block of `SKILL.md`.

---

## Storage Layer (Iceberg)

- Create **S3 Table Bucket**: `{prefix}-table-bucket`
- Create **Namespace**: `{prefix}_db` (replaces the Glue Database — same name, so the downstream interface is unchanged)
- Raw source data still lands in a regular S3 bucket: `{prefix}-raw-zone` (for landing CSV/JSON before ETL)
- **NO curated bucket needed** — the Iceberg tables ARE the curated layer
- Analytics bucket for Athena results: `{prefix}-analytics-zone` (same as the Hive path)

```typescript
import * as s3tables from 'aws-cdk-lib/aws-s3tables';

// S3 Table Bucket (Iceberg storage)
const tableBucket = new s3tables.CfnTableBucket(this, 'TableBucket', {
  tableBucketName: `${prefix}-table-bucket`,
});

// Namespace (= database)
new s3tables.CfnNamespace(this, 'Namespace', {
  tableBucketArn: tableBucket.attrTableBucketArn,
  namespace: `${prefix}_db`,
});
```

The raw landing bucket (`{prefix}-raw-zone`) and analytics bucket (`{prefix}-analytics-zone`) are created exactly as in the Hive storage stack (`reference/hive-pattern.md`) — just omit the curated bucket.

---

## Glue Data Catalog integration (one-time, account+region) — REQUIRED

Athena cannot query S3 Tables until the account's S3 Tables are integrated with the Glue Data Catalog. This registers the `s3tablescatalog` federated catalog. Enable it before deploying the Iceberg path:

```bash
# One-time per account+region: integrate S3 Tables with the Glue Data Catalog.
# This creates the federated "s3tablescatalog" catalog that Athena queries through.
# (Console equivalent: create a table bucket with "Enable integration" checked —
#  the first integrated bucket in a Region creates s3tablescatalog automatically.)
aws glue create-catalog \
  --name "s3tablescatalog" \
  --catalog-input '{
    "Description": "Federated catalog for S3 Tables",
    "FederatedCatalog": {
      "Identifier": "arn:aws:s3tables:{aws_region}:{account_id}:bucket/*",
      "ConnectionName": "aws:s3tables"
    },
    "CreateDatabaseDefaultPermissions": [{
      "Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
      "Permissions": ["ALL"]
    }],
    "CreateTableDefaultPermissions": [{
      "Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
      "Permissions": ["ALL"]
    }]
  }' \
  --region {aws_region}

# Verify: list the child catalogs auto-mounted under s3tablescatalog
aws glue get-catalogs --parent-catalog-id s3tablescatalog --region {aws_region}
```

If the integration is missing, surface it and stop — the Glue Spark job's `writeTo`/`MERGE INTO` and any Athena query against `s3tablescatalog/...` will fail until it is enabled.

---

## IAM — S3 Tables grants (Iceberg path only)

The bucket-level S3 policies in the shared IAM block cover only the raw landing and analytics buckets — the Iceberg tables live in an S3 Table Bucket with **table-level IAM** (`s3tables:*`). Grant both the ETL role and the Athena query role access to the table bucket ARN:

```typescript
// Grant on the S3 Table Bucket (and the namespaces/tables beneath it)
const s3TablesStatement = new iam.PolicyStatement({
  actions: [
    's3tables:GetTableBucket', 's3tables:ListTableBuckets',
    's3tables:CreateNamespace', 's3tables:GetNamespace', 's3tables:ListNamespaces',
    's3tables:DeleteNamespace',
    's3tables:CreateTable', 's3tables:GetTable', 's3tables:ListTables',
    's3tables:DeleteTable',
    's3tables:GetTableData', 's3tables:PutTableData',
    's3tables:GetTableMetadataLocation', 's3tables:UpdateTableMetadataLocation',
  ],
  resources: [
    `arn:aws:s3tables:${region}:${account}:bucket/${prefix}-table-bucket`,
    `arn:aws:s3tables:${region}:${account}:bucket/${prefix}-table-bucket/*`,
  ],
});
etlRole.addToPolicy(s3TablesStatement);
athenaQueryRole.addToPolicy(s3TablesStatement);

// Glue Data Catalog integration: Athena reaches S3 Tables through the federated
// "s3tablescatalog" catalog (see above). The ETL and Athena roles need
// Glue access on that integration catalog and the databases/tables under it.
const glueIntegrationStatement = new iam.PolicyStatement({
  actions: ['glue:*'],
  resources: [
    `arn:aws:glue:${region}:${account}:catalog/s3tablescatalog`,
    `arn:aws:glue:${region}:${account}:catalog/s3tablescatalog/*`,
    `arn:aws:glue:${region}:${account}:database/s3tablescatalog/*`,
    `arn:aws:glue:${region}:${account}:table/s3tablescatalog/*`,
  ],
});
etlRole.addToPolicy(glueIntegrationStatement);
athenaQueryRole.addToPolicy(glueIntegrationStatement);
```

> The same `s3tables:*` + `glue:*` grants are what the **Glue 5.x Spark job** needs to write Iceberg directly (the default ETL) — attach them to `etlRole`. Only if you use the Athena CTAS **fallback** does the role also need `glue:Get*` on the `AwsDataCatalog` `{prefix}_raw` database that holds the external-table CTAS source. Scope `s3tables:*` to the specific table bucket ARN — do NOT grant `s3tables:*` on `*`. The `glue:*` grant above is scoped to the `s3tablescatalog` integration catalog only; without it, neither the Glue job nor Athena can resolve S3 Tables even when `s3tables:*` is present.

---

## Glue 5.x Spark Job + Glue Trigger (Iceberg)

A **single Glue 5.x Spark job** reads the source and writes Iceberg directly (the `ingest-iceberg.py` / `ingest-jdbc-iceberg.py` scripts in `reference/scripts.md`). Note `glueVersion: '5.0'`, the `--datalake-formats iceberg` argument, the `--extra-jars` + `--user-jars-first` pair that adds the **S3 Tables catalog JAR**, the `--conf` that sets ALL Spark/Iceberg config statically, and that there is **no transform job** — typing/cleaning happens inside this one job.

> 🔴 **Prerequisite — upload the S3 Tables catalog JAR before deploy.** `--datalake-formats iceberg` does NOT include the `S3TablesCatalog` class. Download the latest `s3-tables-catalog-for-iceberg-runtime` JAR (e.g. `s3-tables-catalog-for-iceberg-runtime-0.1.8.jar`) from [Maven Central](https://mvnrepository.com/artifact/software.amazon.s3tables/s3-tables-catalog-for-iceberg-runtime) (or build from the [AWS Labs repo](https://github.com/awslabs/s3-tables-catalog) — check there for the latest version), then upload it to your Glue assets bucket:
> ```bash
> # Glue auto-creates aws-glue-assets-{account}-{region}; reuse it or any bucket the ETL role can read.
> aws s3 cp s3-tables-catalog-for-iceberg-runtime-0.1.8.jar \
>   s3://aws-glue-assets-{account}-{region}/jars/ --region {region}
> ```
> Reference that exact S3 path in `--extra-jars` below. On Glue 5.0, `--user-jars-first: 'true'` is **required** so the added Iceberg/S3-Tables classes take precedence over Glue's bundled ones.

```typescript
// The S3 Tables catalog JAR you uploaded above (see prerequisite box). Not hosted by
// AWS — you upload it yourself. Version may differ; check the AWS Labs repo for latest.
const s3TablesJar =
  `s3://aws-glue-assets-${account}-${region}/jars/s3-tables-catalog-for-iceberg-runtime-0.1.8.jar`;

// ALL Spark/Iceberg config goes here (static — cannot be set via spark.conf.set in Glue 5).
// One --conf key whose value is the space-joined list of settings, each prefixed by --conf.
const icebergConf = [
  'spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions',
  '--conf spark.sql.catalog.s3tablescatalog=org.apache.iceberg.spark.SparkCatalog',
  '--conf spark.sql.catalog.s3tablescatalog.catalog-impl=software.amazon.s3tables.iceberg.S3TablesCatalog',
  `--conf spark.sql.catalog.s3tablescatalog.warehouse=arn:aws:s3tables:${region}:${account}:bucket/${props.prefix}-table-bucket`,
].join(' ');

new glue.CfnJob(this, 'IcebergIngestJob', {
  name: `${props.prefix}-ingest-iceberg`,
  role: props.etlRoleArn,
  command: {
    name: 'glueetl',
    pythonVersion: '3',
    // Case A (S3 source): ingest-iceberg.py. Case B (DB source): ingest-jdbc-iceberg.py.
    scriptLocation: `s3://${props.scriptsBucket}/glue-scripts/ingest-iceberg.py`,
  },
  glueVersion: '5.0',                 // Glue 5.x — required for the S3 Tables Iceberg connector
  numberOfWorkers: 2,
  workerType: 'G.1X',
  timeout: 120, // minutes
  defaultArguments: {
    '--datalake-formats': 'iceberg',  // loads Glue's bundled Iceberg runtime (1.7.1 on Glue 5.0)
    '--extra-jars': s3TablesJar,      // 🔴 adds software.amazon.s3tables.iceberg.S3TablesCatalog
    '--user-jars-first': 'true',      // 🔴 required on Glue 5.0 so the added JAR wins on the classpath
    '--conf': icebergConf,            // 🔴 static Spark/Iceberg config (NOT set at runtime)
    '--PREFIX': props.prefix,
    '--SOURCE_PATH': `s3://${props.rawBucketName}/${props.sourceName}/${props.sourceTable}/`,
    '--TABLE_BUCKET': `${props.prefix}-table-bucket`,
    '--NAMESPACE': `${props.prefix}_db`,
    '--TABLE_NAME': props.curatedTableName, // e.g. quality_inspections
    '--enable-metrics': 'true',
    '--enable-continuous-cloudwatch-log': 'true',
  },
  tags: { project: props.prefix, environment: props.environment, owner: props.owner },
});

// Schedule with a Glue Trigger (cron) — NOT EventBridge + Athena.
new glue.CfnTrigger(this, 'IcebergIngestTrigger', {
  name: `${props.prefix}-daily-trigger`,
  type: 'SCHEDULED',
  schedule: 'cron(0 17 * * ? *)', // 02:00 KST = 17:00 UTC, daily
  startOnCreation: true,
  actions: [{ jobName: `${props.prefix}-ingest-iceberg` }],
});
```

---

## Maintenance (Iceberg) — S3 Tables maintenance API

S3 Tables handles **compaction automatically** — no manual compaction job. Maintenance is configured through the **S3 Tables maintenance API**, NOT SQL. The `OPTIMIZE` and `VACUUM` SQL commands are **unsupported** on S3 Tables — do not generate them. Tune snapshot retention and compaction via `put-table-maintenance-configuration`:

```bash
# Snapshot management (controls how long old snapshots / time-travel history are kept)
aws s3tables put-table-maintenance-configuration \
  --table-bucket-arn {table-bucket-arn} \
  --namespace {prefix}_db \
  --name quality_inspections \
  --type icebergSnapshotManagement \
  --value '{"status":"enabled","settings":{"icebergSnapshotManagement":{"minSnapshotsToKeep":5,"maxSnapshotAgeHours":168}}}' \
  --region {region}

# Compaction (S3 Tables compacts automatically; use this only to tune target file size)
aws s3tables put-table-maintenance-configuration \
  --table-bucket-arn {table-bucket-arn} \
  --namespace {prefix}_db \
  --name quality_inspections \
  --type icebergCompaction \
  --value '{"status":"enabled","settings":{"icebergCompaction":{"targetFileSizeMB":512}}}' \
  --region {region}
```

---

## Iceberg teardown (extra step vs Hive)

For the Iceberg pattern there is no curated bucket to empty, but you must delete the S3 Table Bucket contents (namespaces → tables) before `cdk destroy` can remove the table bucket. **Confirm intent first**, then:

```bash
# Delete each Iceberg table, then the namespace, then the table bucket
aws s3tables list-tables --table-bucket-arn {table-bucket-arn} --namespace {prefix}_db \
  --query 'tables[].name' --output text | tr '\t' '\n' | while read t; do
    aws s3tables delete-table --table-bucket-arn {table-bucket-arn} --namespace {prefix}_db --name "$t"
  done
aws s3tables delete-namespace --table-bucket-arn {table-bucket-arn} --namespace {prefix}_db
aws s3tables delete-table-bucket --table-bucket-arn {table-bucket-arn}
```

The raw `{prefix}_raw` Glue database only exists if the Athena CTAS fallback was used; if present, drop it the same way as the Hive `{prefix}_db`. See `reference/hive-pattern.md` for the shared teardown sequence.
