# Quick Sight CDK Reference — Data Source, Dataset, Refresh, Dashboard

All CDK uses `aws-cdk-lib/aws-quicksight`. The CLI/SDK namespace is still `quicksight` despite the Amazon Quick rebrand.

> **🔴 Schema constraints — apply to EVERY `CfnDataSet` / `CfnDashboard`:**
> - **`PhysicalTableMap` / `LogicalTableMap` keys and column IDs must match `[0-9a-zA-Z-]*`** — letters, digits, hyphens only. **No underscores, no dots.** Use `quality-inspections`, never `quality_inspections`. An underscore in a map key fails with an unhelpful schema error.
> - **`permissions.actions` must be one of the two predefined action sets** (the full **Owner** set or the **Reader** set). A partial/custom subset is rejected with `Resultant state not supported`. Use the complete action lists below — never trim them.

---

## 1. Athena data source (most common path)

Create the data source in **CDK (`CfnDataSource`)**, not the CLI, so `CfnDataSet` can reference it by token (`athenaDataSource.attrArn`) and CloudFormation orders the dependency. The `permissions` principal points at an admin group — **create that group first** (`iam-permissions.md` §7, e.g. `create-group --group-name {prefix}-admins`); granting to a non-existent group fails. ARN form: `group/{namespace}/{group-name}`, where `{namespace}` is `{prefix}` if you created a project namespace in `iam-permissions.md` §7, else `default`.

```typescript
import * as quicksight from 'aws-cdk-lib/aws-quicksight';

const athenaDataSource = new quicksight.CfnDataSource(this, 'AthenaSource', {
  awsAccountId: cdk.Stack.of(this).account,
  dataSourceId: `${prefix}-athena-source`,
  name: `${prefix} Athena`,
  type: 'ATHENA',
  dataSourceParameters: {
    athenaParameters: { workGroup: workgroup },
  },
  permissions: [{
    principal: `arn:aws:quicksight:${region}:${account}:group/${namespace}/${prefix}-admins`,
    actions: [
      'quicksight:UpdateDataSourcePermissions', 'quicksight:DescribeDataSource',
      'quicksight:DescribeDataSourcePermissions', 'quicksight:PassDataSource',
      'quicksight:UpdateDataSource', 'quicksight:DeleteDataSource',
    ],
  }],
});
```

(`{namespace}` is the project namespace from `iam-permissions.md` §7, or `default` for single-tenant.)

---

## 2. Dataset — one per business domain (Hive `relationalTable`)

A "domain" maps to one Athena view typically (e.g., `v_quality_inspections`). Don't create one mega-dataset with everything joined — break by question area so SPICE refresh is incremental and topic scope is bounded.

```typescript
new quicksight.CfnDataSet(this, 'QualityInspectionsDataset', {
  awsAccountId: cdk.Stack.of(this).account,
  dataSetId: `${prefix}-quality-inspections`,
  name: 'Quality Inspections',
  importMode: 'SPICE', // SPICE, not DIRECT_QUERY
  physicalTableMap: {
    'qi-physical': {
      relationalTable: {
        dataSourceArn: athenaDataSource.attrArn,
        catalog: 'AwsDataCatalog',
        schema: `${prefix}_db`,
        name: 'v_quality_inspections',
        inputColumns: [
          { name: 'inspection_month', type: 'DATETIME' },
          { name: 'supplier_id', type: 'STRING' },
          { name: 'supplier_name', type: 'STRING' },
          { name: 'product_code', type: 'STRING' },
          { name: 'product_name', type: 'STRING' },
          { name: 'inspection_type', type: 'STRING' },
          { name: 'inspection_type_name', type: 'STRING' },
          { name: 'total_count', type: 'INTEGER' },
          { name: 'defect_count', type: 'INTEGER' },
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
          // tagColumnOperation attaches metadata, not type. columnGeographicRole is
          // ONLY for geo columns (COUNTRY/STATE/CITY/POSTCODE/LATITUDE/LONGITUDE) —
          // never a date. Use columnDescription for business-glossary text; set DATE
          // types via the inputColumns above (type: 'DATETIME').
          tagColumnOperation: {
            columnName: 'supplier_name',
            tags: [{ columnDescription: { text: 'Supplier name' } }],
          },
        },
      ],
    },
  },
  permissions: [/* Space-scoped principals — see iam-permissions.md §9 */],
});
```

**Why SPICE not direct query:**
- Sub-second dashboard rendering (vs. several seconds for direct Athena)
- No surprise Athena scan costs from each dashboard interaction
- Decouples BI consumption from pipeline freshness

The cost of SPICE is staleness ≤ refresh interval. For most Data Lab use cases, that's acceptable.

---

## 3. Dataset for S3 Tables (Iceberg) — Athena custom SQL (PRIMARY)

On the Iceberg path, **default to reaching S3 Tables through Athena's federated `s3tablescatalog/{prefix}-table-bucket` catalog** with a custom-SQL dataset. This is primary because **one Athena dataset powers BOTH the dashboard AND the chat agent** — the chat agent (Dataset Q&A) requires a SQL engine (Athena) anyway, so one dataset is built/refreshed/managed instead of two.

The `relationalTable` block (Hive, above) does **NOT** work here: the catalog name contains a slash, and `relationalTable.catalog`/`schema`/`name` cannot express the three-part federated reference. The table picker pane won't list these tables either — you are forced into a **custom-SQL dataset**, and the Quick Sight service role needs the `s3tables:*` + `glue:*` grants in `iam-permissions.md`:

```json
"PhysicalTableMap": {
  "quality-data": {
    "CustomSql": {
      "DataSourceArn": "arn:aws:quicksight:{region}:{account}:datasource/{prefix}-athena-source",
      "Name": "quality_inspections",
      "SqlQuery": "SELECT * FROM \"s3tablescatalog/{prefix}-table-bucket\".\"{prefix}_db\".\"mart_quality_summary\"",
      "Columns": [
        { "Name": "inspection_month", "Type": "DATETIME" },
        { "Name": "supplier_id", "Type": "STRING" },
        { "Name": "total_count", "Type": "INTEGER" },
        { "Name": "defect_count", "Type": "INTEGER" },
        { "Name": "defect_rate_pct", "Type": "DECIMAL" }
      ]
    }
  }
}
```

In CDK, set `physicalTableMap` with a `customSql` block (instead of `relationalTable`) on the `CfnDataSet`. The custom-SQL `SELECT *` against the mart gives the same columns the Hive `v_*` view would have.

> **Reference `mart_*` tables, not `v_*` views.** On the Iceberg path the pipeline materializes analytical **`mart_*` CTAS tables** in the S3 Tables namespace — Athena views are not supported across the S3 Tables catalog boundary, so there is no `v_*` to point at. Confirm the namespace + table name from the pipeline's `ARCHITECTURE.md` (it lists `mart_*` tables under Catalog).

**Why custom SQL through Athena is the default:**
- **One dataset, two consumers** — the same Athena dataset feeds the dashboard *and* the chat agent. The native connector cannot serve the chat agent (no SQL engine), so using it would force a second, separate data source just for the dashboard.
- **Complex queries possible** — JOINs, window functions, ad-hoc analysis not pre-materialized in `mart_*`.
- **Consistent with the pipeline's output** — the Athena workgroup already exists from the pipeline layer.

> 🔴 **KPI cards need a separate single-row dataset.** A KPI card aggregates over the WHOLE dataset; pointing it at a multi-grain mart double/triple-counts on SUM (see `dashboard-patterns.md` §8, §10 — this shipped a 3,527-vs-426 overcount). Build a second custom-SQL dataset over the pipeline's single-row `mart_kpi_summary` and point KPI visuals at it; trend/ranking visuals keep using the grain-level mart dataset above.
>
> ```json
> "PhysicalTableMap": {
>   "kpi-summary": {
>     "CustomSql": {
>       "DataSourceArn": "arn:aws:quicksight:{region}:{account}:datasource/{prefix}-athena-source",
>       "Name": "kpi_summary",
>       "SqlQuery": "SELECT * FROM \"s3tablescatalog/{prefix}-table-bucket\".\"{prefix}_db\".\"mart_kpi_summary\"",
>       "Columns": [
>         { "Name": "defect_materials", "Type": "INTEGER" },
>         { "Name": "total_defects", "Type": "INTEGER" },
>         { "Name": "delayed_orders", "Type": "INTEGER" },
>         { "Name": "avg_delay_days_late", "Type": "DECIMAL" }
>       ]
>     }
>   }
> }
> ```

---

## 4. Optional: S3 Tables native connector (dashboard-only shortcut)

If you **only** need dashboards (no chat agent), Quick Sight's native "Amazon S3 Tables (Apache Iceberg)" connector is simpler — no Athena, managed IAM role, working table picker. Use it only when chat / Dataset Q&A is explicitly out of scope (e.g., a region without chat agents — see `region-constraints.md` Option A):

1. **Enable S3 Tables access** (one-time): Manage Quick Sight → Security & permissions → AWS resources → toggle S3 Tables ON. Auto-creates the managed role `aws-quicksight-s3-tables-role-v0`.

2. **Create data source:**
   - Console: New data source → "Amazon S3 Tables" → paste table bucket ARN (`arn:aws:s3tables:{region}:{account}:bucket/{prefix}-table-bucket`)
   - CDK:
     ```typescript
     new quicksight.CfnDataSource(this, 'S3TablesSource', {
       awsAccountId: account,
       dataSourceId: `${prefix}-s3tables`,
       name: `${prefix} S3 Tables`,
       type: 'S3_TABLES',
       dataSourceParameters: {
         s3TablesParameters: {
           tableBucketArn: `arn:aws:s3tables:${region}:${account}:bucket/${prefix}-table-bucket`,
         },
       },
     });
     ```

3. **Create dataset:** Select namespace → select table from picker UI (or API). Direct Query or SPICE. Can join multiple tables from the same namespace.

**Advantages (dashboard-only):** table picker WORKS (no custom SQL); managed IAM role (no manual `s3tables:*` grants); simpler.

**Why it's not the default:** no SQL engine, so it **cannot back the chat agent**. Using it for dashboards while running Athena for chat means **two data sources / two datasets** to manage.

> **Region availability gate.** The native S3 Tables connector is newer than plain Athena support — confirm it's available in `{aws_region}` ([Supported Regions for Quick](https://docs.aws.amazon.com/quicksuite/latest/userguide/regions.html)) and apply the same "verify at deploy time" discipline as `region-constraints.md`.

---

## 5. S3 manifest data source (text/JSON only)

Validate the manifest before referencing it:
```json
{
  "fileLocations": [
    { "URIPrefixes": ["s3://{prefix}-curated-zone/quality_inspections/"] }
  ],
  "globalUploadSettings": {
    "format": "CSV",
    "delimiter": ",",
    "containsHeader": "true"
  }
}
```

> ⚠ **Quick Sight's S3 manifest connector only reads delimited text (CSV/TSV), CLF/ELF log, or JSON — NOT Parquet.** A curated zone written as Parquet+Snappy cannot be consumed via an S3 manifest. For Parquet curated data, go through **Athena** instead — Athena reads the Parquet and Quick Sight queries it over the Glue Catalog. Reserve the S3 manifest path for genuinely text/JSON sources not already cataloged.

---

## 6. Redshift data source (VPC connection)

```bash
# Connectivity probe from a host that can reach the Redshift VPC
psql -h {endpoint} -p {port} -U {user} -d {database} -c "SELECT current_database(), current_user;"
```

If Redshift is private, set up the VPC connection first:
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

---

## 7. Refresh schedule (`CfnRefreshSchedule`)

```typescript
new quicksight.CfnRefreshSchedule(this, 'QualityRefresh', {
  awsAccountId: cdk.Stack.of(this).account,
  dataSetId: `${prefix}-quality-inspections`,
  schedule: {
    scheduleId: `${prefix}-quality-daily`,
    scheduleFrequency: {
      interval: 'DAILY',
      timeOfTheDay: '04:00',
      timeZone: 'Asia/Seoul', // CDK prop is timeZone (CFN: TimeZone) — note the capital Z
    },
    refreshType: 'FULL_REFRESH',
  },
});
```

> **CDK type definitions can lag CloudFormation.** If TypeScript rejects a property CloudFormation accepts (e.g. a newly added visual field like `axisOffset`), use `addPropertyOverride` to bypass the type checker while still emitting valid CloudFormation. Override property names are the **CloudFormation** names (PascalCase, e.g. `TimeZone`):
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
> refresh.addPropertyOverride('Schedule.ScheduleFrequency.TimeZone', 'Asia/Seoul');
> ```
>
> Verify against the latest CFN docs (`AWS::QuickSight::*`) when CDK rejects a property — the underlying CFN schema almost always supports it.

### Incremental refresh (large fact tables)

When a SPICE dataset > 10 GB or refresh time > 30 min, switch to incremental:
```typescript
schedule: {
  scheduleId: `${prefix}-quality-incremental`,
  scheduleFrequency: { interval: 'DAILY', timeOfTheDay: '04:00', timeZone: 'Asia/Seoul' },
  refreshType: 'INCREMENTAL_REFRESH',
  // Requires LookbackWindow on the dataset (see CreateDataSet API)
},
```

SPICE refresh policy + capacity sizing → core SKILL §6. SPICE per-region quota gotcha → `region-constraints.md`.

---

## 8. Dashboard (`CfnDashboard`)

```typescript
new quicksight.CfnDashboard(this, 'QualityDashboard', {
  awsAccountId: cdk.Stack.of(this).account,
  dashboardId: `${prefix}-quality-dashboard`,
  name: 'Quality Management Dashboard',
  sourceEntity: {
    sourceTemplate: {
      arn: templateArn, // built from a definition or imported
      dataSetReferences: [{
        dataSetPlaceholder: 'quality_inspections',
        dataSetArn: dataset.attrArn,
      }],
    },
  },
  permissions: spacePermissions, // see iam-permissions.md §9
  versionDescription: 'Initial Data Lab build',
});
```

For complex dashboards, build the definition JSON in the Quick Sight UI, export with `aws quicksight describe-dashboard-definition`, and check the JSON into the CDK project as the source of truth. Layout patterns, validation, and the update flow → `dashboard-patterns.md`.

### Row-level security dataset

To restrict rows per team on a shared fact table, define a permissions table as its own `CfnDataSet`, then set `rowLevelPermissionDataSet` on the protected `CfnDataSet` (pointing at the permissions dataset's ARN). RLS is filtered on every query — no per-team duplicate datasets. Example permissions CSV/query:
```sql
GroupName,supplier_id
quality-team-tier1,SUP-001
quality-team-tier1,SUP-002
quality-team-tier2,SUP-003
```
