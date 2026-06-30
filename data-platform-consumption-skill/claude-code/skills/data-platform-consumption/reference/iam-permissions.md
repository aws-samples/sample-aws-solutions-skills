# IAM, Permissions & Access Control

## 1. `{prefix}-quicksight-role` — service role for Quick Sight

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

> **If the role already exists** (e.g., the pipeline layer created it with the same name): either import it via `iam.Role.fromRoleArn(this, 'QsRole', existingArn)` and skip the `new iam.Role(...)` block, or ensure both stacks define the role identically and let one own it. If both stacks try to create the same physical role, CloudFormation fails on the second deploy with "role already exists" — pick one owner and have the other import.

---

## 2. S3 Tables consumption IAM (Iceberg pattern)

**Athena custom-SQL path (default — manual grants required):** Because the default Iceberg path reaches S3 Tables through Athena's federated `s3tablescatalog` catalog (so one dataset serves both the dashboard and the chat agent), the Quick Sight role **needs the `s3tables:*` + `glue:*` grants below**. These are not optional on this path — without them, every Athena query Quick Sight runs against the Iceberg tables fails to resolve the federated catalog. The `GlueCatalogRead` + `S3DataRead` statements above cover the **Hive** pattern (`AwsDataCatalog` + `{prefix}-curated-zone`); for the Athena-federated Iceberg path the role needs these additional grants:

```typescript
qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'S3TablesRead',
  actions: ['s3tables:*'],
  resources: [`arn:aws:s3tables:${region}:${account}:bucket/${prefix}-table-bucket/*`],
}));

qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'GlueS3TablesCatalogRead',
  actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions', 'glue:GetCatalog'],
  resources: [
    `arn:aws:glue:${region}:${account}:catalog/s3tablescatalog`,
    `arn:aws:glue:${region}:${account}:catalog/s3tablescatalog/*`,  // NESTED catalog resources — required, see below
  ],
}));

// Required for S3 Tables: the federated catalog resolves through Lake Formation's
// data-access path. Without this, queries fail with CATALOG_NOT_FOUND even when
// s3tables:* is granted. Applies to BOTH this custom role and the managed role (§3).
qsRole.addToPolicy(new iam.PolicyStatement({
  sid: 'LakeFormationDataAccess',
  actions: ['lakeformation:GetDataAccess'],
  resources: ['*'],
}));
```

The equivalent JSON:

```json
{
  "Effect": "Allow",
  "Action": ["s3tables:*"],
  "Resource": "arn:aws:s3tables:{region}:{account}:bucket/{prefix}-table-bucket/*"
},
{
  "Effect": "Allow",
  "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions", "glue:GetCatalog"],
  "Resource": [
    "arn:aws:glue:{region}:{account}:catalog/s3tablescatalog",
    "arn:aws:glue:{region}:{account}:catalog/s3tablescatalog/*"
  ]
}
```

Without these, every Athena query Quick Sight runs against the Iceberg tables fails (the federated catalog can't be resolved). The S3 read on `{prefix}-curated-zone` is irrelevant on the Iceberg path — S3 Tables manages its own storage and is reached via `s3tables:*`, not bucket-level S3 actions. The `AWSQuickSightS3Policy` allowlist (§5) still applies to the **analytics results** bucket.

> **Optional native-connector path skips these grants.** *Only* if you take the optional dashboard-only S3 Tables native connector do these manual grants go away: enabling S3 Tables resource access in *Manage Quick Sight → Security & permissions → AWS resources* auto-creates the managed role **`aws-quicksight-s3-tables-role-v0`** with read access to the selected table buckets, which replaces the `s3tables:*` / `glue:*` statements above. This applies only to the dashboard-only shortcut — the default Athena path (one dataset for dashboard + chat) needs the manual grants.

---

## 3. 🔴 CRITICAL: the MANAGED service role needs Iceberg/S3 Tables permissions too

> **Symptom:** the Athena data source's **connection test passes ✅**, but the dashboard fails at render time with a SQL/`s3tablescatalog` resolution error (e.g. "SQL exception", "table not found", "could not resolve catalog"). Engineers chase the custom `{prefix}-quicksight-role` for hours — it is the wrong role.

For a **vanilla Athena-on-S3(+S3 Tables) dashboard**, Quick Sight does NOT assume your custom `{prefix}-quicksight-role`. It uses an **AWS-managed service role**, and the connection test only checks that the Athena workgroup + results bucket are reachable — it does **not** verify S3 Tables read access. So the test passes while actual queries against `s3tablescatalog` fail.

You MUST attach the `s3tables:*` + `glue:*` (s3tablescatalog) permissions to the **managed role**, not just your custom role.

> ⚠️ **Which managed role? Check BOTH — the name changed in 2026.** Per current AWS docs, for Athena / S3 / Athena Federated Query, Quick Sight uses **`aws-quicksight-s3-consumers-role-v0`** by default; **only if that role is absent** does it fall back to **`aws-quicksight-service-role-v0`**. Older accounts/runbooks reference only the `service-role` name. Detect which one your account uses and patch that one (patch both if both exist):
> ```bash
> # Both live under the service-role/ path. List what exists, then patch the one(s) present.
> for r in aws-quicksight-s3-consumers-role-v0 aws-quicksight-service-role-v0; do
>   aws iam get-role --role-name "$r" >/dev/null 2>&1 && echo "PRESENT: $r" || echo "absent:  $r"
> done
> ```

```bash
# Attach S3 Tables + Glue (s3tablescatalog) access to the managed role Quick Sight
# actually uses. Run for aws-quicksight-s3-consumers-role-v0 first; if it is absent,
# run for aws-quicksight-service-role-v0 instead (patch both if both are present).
aws iam put-role-policy \
  --role-name aws-quicksight-s3-consumers-role-v0 \
  --policy-name s3tables-access \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {"Effect": "Allow", "Action": "s3tables:*",
       "Resource": "arn:aws:s3tables:{region}:{account}:bucket/{prefix}-table-bucket/*"},
      {"Effect": "Allow",
       "Action": ["glue:GetDatabase","glue:GetTable","glue:GetTables","glue:GetPartitions","glue:GetCatalog"],
       "Resource": ["arn:aws:glue:{region}:{account}:catalog/s3tablescatalog",
                    "arn:aws:glue:{region}:{account}:catalog/s3tablescatalog/*"]},
      {"Effect": "Allow",
       "Action": ["lakeformation:GetDataAccess"],
       "Resource": "*"}
    ]
  }'
```

> ⚠️ **Also grant `lakeformation:GetDataAccess` and `glue:GetDatabase`/`glue:GetTable` on the NESTED catalog resources** (the `s3tablescatalog/*` ARNs above), not just the top-level catalog. Without these, Athena/Quick Sight queries against S3 Tables fail with **`CATALOG_NOT_FOUND`** *even when `s3tables:*` is already granted* — the federated catalog resolves through Lake Formation's data-access path, which `s3tables:*` alone doesn't cover. The `lakeformation:GetDataAccess` statement is included in the patch above for this reason.

This is **only required on the Iceberg / S3 Tables path via Athena custom SQL** (the default). On the pure-Hive path (`AwsDataCatalog` + `{prefix}-curated-zone`), the managed role only needs the bucket allowlist (§5), not `s3tables:*`. On the optional native-connector path, the auto-created `aws-quicksight-s3-tables-role-v0` covers it instead.

---

## 4. Cross-service binding

Quick Sight has an additional service-level resource permission concept (separate from the `{prefix}-quicksight-role` above): you must explicitly grant the Quick Sight **service** access to your S3 buckets and Athena. This is NOT done via `update-account-settings` — it is controlled by the **managed service role** (today `aws-quicksight-s3-consumers-role-v0`, falling back to `aws-quicksight-service-role-v0` when the consumers role is absent — see §3) and its attached `AWSQuickSightS3Policy` bucket allowlist, configured in the Quick Sight console under **Manage QuickSight → Security & Permissions → QuickSight access to AWS services**, or by patching the policy directly (§5).

This is one of the rare AWS spots where IAM is necessary but not sufficient — the Quick Sight service-level grant must also be in place. Document this in the runbook.

---

## 5. The real blocker: `AWSQuickSightS3Policy`

> Custom IAM role grants (the `{prefix}-quicksight-role` above) **don't matter for vanilla Athena-on-S3 dashboards** — Quick Sight uses an AWS-managed service role (today `aws-quicksight-s3-consumers-role-v0`, falling back to `aws-quicksight-service-role-v0`) with the service role's S3 access policy (historically surfaced as `AWSQuickSightS3Policy` — exact name may vary per account). That policy contains an **explicit allowlist of S3 buckets**. If your buckets aren't on it, the dashboard fails with a misleading error.
>
> **Symptom:** When creating a DataSource or running a dashboard query, you see `"Could not get query execution ID"`. This looks like an Athena failure but is actually an S3 permission problem.
>
> **To fix:**
> 1. Quick Sight console → **Manage QuickSight** → **Security & Permissions** → **QuickSight access to AWS services** → under **S3**, add the buckets — with the right access level per bucket:
>    - **Data buckets** (`{prefix}-curated-zone`, or `mart_*` reads on the Iceberg path): **read** access ✅
>    - **Athena results bucket** (`{prefix}-analytics-zone`): **read AND write** access ✅ — also tick the **"Write permission for Athena Workgroup"** checkbox next to this bucket. Athena writes every query's results here, so without write you get `Unable to verify/create output bucket` even though reads succeed.
> 2. **Or programmatically:** patch the policy directly (ensure the analytics-zone bucket has `s3:PutObject`/`s3:AbortMultipartUpload`, not just `s3:GetObject`).

> **Why a regular S3 bucket is still needed even with S3 Tables:** Athena always stores query **results** in a regular S3 bucket (`{prefix}-analytics-zone`), regardless of where the source data lives (S3 Tables manages its own storage, but results don't go there). That bucket needs S3 read **and** write for both the Quick Sight managed/consumer role and the Athena execution role, and must be on the `AWSQuickSightS3Policy` allowlist **with the write checkbox**. This is independent of the `s3tables:*` grants, which only cover reading the Iceberg table data itself.
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

---

## 6. Lake Formation note

If the source account has Lake Formation in strict mode (IAMAllowedPrincipals revoked), the IAM role above is not enough — you must also grant LF permissions to the Quick Sight role:
```bash
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::{account}:role/{prefix}-quicksight-role \
  --resource '{"Database": {"Name": "{prefix}_db"}}' \
  --permissions SELECT DESCRIBE \
  --region {aws_region}
```

The skill detects this case via `aws lakeformation get-data-lake-settings` and warns the user before deploying. Same precondition pattern as in the pipeline layer.

---

## 7. Quick Sight account & namespace setup

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

**Why Enterprise:** Standard edition does not support row-level security, hourly SPICE refresh, embedded analytics, or Amazon Quick chat agents and Topics. Use Enterprise.

### Namespace

For multi-tenant / multi-customer setups, create a namespace per project:
```bash
aws quicksight create-namespace \
  --aws-account-id {account_id} \
  --namespace {prefix} \
  --identity-store QUICKSIGHT \
  --region {aws_region}
```
For single-tenant, the `default` namespace is fine.

### Identity & user provisioning

Default to **IAM federated identity** for end users — don't create native Quick Sight users unless the customer has no IdP.

```bash
# Create a QS group per Space (§9)
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

User roles: `ADMIN` (SA, data team) · `AUTHOR` (analysts who build dashboards) · `READER` (business users who consume).

---

## 8. Data source discovery (catalog handoff)

Before configuring datasets, enumerate what's available so dashboards and topics target real tables and views.

### `data_source_type=athena` and `project_prefix` set

```bash
# Databases that match the project prefix convention
aws glue get-databases --region {aws_region} \
  --query "DatabaseList[?starts_with(Name, '{prefix}_')].Name" --output table

# Tables and views in the target database
aws glue get-tables --database-name {prefix}_db --region {aws_region} \
  --query 'TableList[].{Name:Name, Type:TableType, Location:StorageDescriptor.Location}' --output table

# Connectivity test from Athena
aws athena start-query-execution \
  --work-group {workgroup} \
  --query-string "SELECT table_name FROM information_schema.tables WHERE table_schema = '{prefix}_db'" \
  --region {aws_region}
```

Prefer **views** (`v_`) over raw curated tables for dashboarding — views encode the join + enrichment logic. **On the Iceberg path there are no `v_*` views** — point at the `mart_*` tables instead.

### ⚠️ Catalog name differs by pipeline pattern — read ARCHITECTURE.md FIRST

| Pipeline pattern | Data source / catalog | Dashboard/dataset source | How to discover |
|-----------------|----------------|--------------------------|-----------------|
| Iceberg via Athena (default) | `s3tablescatalog/{prefix}-table-bucket` | `mart_*` CTAS tables (no views), custom SQL | ARCHITECTURE.md → "Architecture Pattern: Iceberg" |
| Iceberg + native connector (optional, dashboard-only) | `S3_TABLES` data source (table bucket ARN) | `mart_*` tables — pick namespace/table, no custom SQL | ARCHITECTURE.md → "Architecture Pattern: Iceberg" |
| Hive (opt-in) | `AwsDataCatalog` | `v_*` views | ARCHITECTURE.md → "Architecture Pattern: Hive" |

**This skill MUST read the pipeline's `ARCHITECTURE.md`** to determine which catalog to use before creating any data source or dataset. The pattern dictates both the catalog name *and* whether you reference `v_*` views (Hive) or `mart_*` tables (Iceberg). For Iceberg, **default to the Athena custom-SQL dataset** (`quicksight-cdk.md` §3) — one Athena dataset powers BOTH the dashboard and the chat agent.

---

## 9. Space & access control

### Why Spaces matter

In multi-team environments, datasets, dashboards, and topics are scoped to Spaces. Users in Space A cannot see Space B's data. This replaces ad-hoc folder permissions.

### Default Space layout

| Space | Members | Data | Notes |
|---|---|---|---|
| `{prefix}-admin` | SA, data team | All datasets and dashboards | Build/test environment |
| `{prefix}-{team}` | One team | Datasets relevant to their domain | One Space per business team |
| `{prefix}-leadership` | Executives | Curated KPI dashboard only | Read-only, no chat unless requested |

### Space + group + permission CDK

```typescript
// One Quick Sight group per Space.
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
// Pass these permissions to the CfnDataSet construct (quicksight-cdk.md §2).
```

### Per-Space chat persona

The chat agent persona can differ per Space — set by creating one Topic per Space, each with its own description. See `chat-agent.md` §6.

### Row-level security (one dataset spanning multiple teams)

Use a permissions dataset + `rowLevelPermissionDataSet` on the protected `CfnDataSet`. CDK shape → `quicksight-cdk.md` §8.

### Adding a new Space

1. Create Quick Sight group: `aws quicksight create-group ...`
2. Create dataset/dashboard permissions for the group.
3. If chat persona differs: create a new topic with its own description.
4. Register users into the group.
