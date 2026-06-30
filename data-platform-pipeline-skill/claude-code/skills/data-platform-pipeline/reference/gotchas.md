# Gotchas, Constraints & Known Issues

Every ⚠️ note, hard constraint, and known-issue list pulled out of the core. The thin core (`SKILL.md`) carries the TOP 5 fatal rules; everything else lives here. Consult this file whenever you hit an opaque failure or before generating Athena DDL on S3 Tables.

---

## Athena constraints on S3 Tables (Iceberg) — do NOT generate these

When the Iceberg path runs through Athena, the skill **MUST NOT** generate:

- `ALTER TABLE ... SET PARTITION SPEC` (Spark-only, not Athena)
- `ALTER TABLE ... WRITE ORDERED BY` (Spark-only)
- `ALTER TABLE ... RENAME` (not supported on S3 Tables)
- `ALTER DATABASE` (not supported on the S3 Tables namespace)
- `OPTIMIZE` and `VACUUM` (unsupported — S3 Tables compacts automatically and snapshot retention is managed via the maintenance API, see `reference/iceberg-cdk.md` → Maintenance)
- Plain `DROP TABLE` (i.e. `purge=false`) on an S3 Tables Iceberg table — S3 Tables does **not** support `DROP TABLE` with `purge=false`. To delete: run `DROP TABLE ... PURGE` in Athena (purge=true), or — the path this skill prefers for scripted teardown/idempotency — use the S3 Tables API: `aws s3tables delete-table --table-bucket-arn ... --namespace ... --name ...` (or `boto3` `s3tables.delete_table(...)`). Deletion is permanent and irreversible.
- `CREATE VIEW` on the S3 Tables catalog (not supported — and views in `AwsDataCatalog` that reference `s3tablescatalog/...` also fail with `CREATE VIEW statements are not yet supported for Cross-Account Glue DataCatalogs`. Use materialized `mart_*` CTAS tables in the same S3 Tables namespace instead.)
- Unqualified cross-catalog references (must use three-part: `"catalog"."db"."table"`)

> **Idempotency / re-creating a table:** A CTAS or Glue write that half-failed can leave a table that blocks re-creation. To re-run cleanly, delete the table first via the S3 Tables API (`boto3` `s3tables.delete_table(table_bucket_arn=..., namespace="{prefix}_db", name="{table}")`, or `aws s3tables delete-table ...`), then re-run the CTAS / Glue job. Do NOT rely on plain `DROP TABLE` — it fails on S3 Tables with `purge=false`.

What **IS** supported on Athena + S3 Tables:

- `CREATE TABLE ... WITH (table_type='ICEBERG')`, including `partitioning` with month/day/hour/bucket/truncate transforms
- `INSERT INTO`, `CTAS`
- `ALTER TABLE ADD/DROP COLUMNS`, `CHANGE COLUMN` — note: DDL like this requires the **Glue Data Catalog integration** path (see preconditions / IAM), not the data source registration path
- `MERGE INTO` (`WHEN MATCHED` / `WHEN NOT MATCHED`)
- `DELETE`, `UPDATE`
- Time travel: `FOR TIMESTAMP AS OF`, `FOR VERSION AS OF`
- Metadata tables: `$history`, `$snapshots`, `$files`, `$partitions`
- `ALTER TABLE SET TBLPROPERTIES`

> ⚠️ **KMS result encryption breaks DML on S3 Tables.** If your workgroup uses SSE-KMS or CSE-KMS result encryption, INSERT/UPDATE/DELETE/MERGE on S3 Tables will fail. Use SSE-S3 encryption for workgroups that write to S3 Tables.

### No views on the Iceberg path — use materialized mart tables (CTAS)

`CREATE VIEW` is unsupported on the `s3tablescatalog` catalog, **and** creating the view in `AwsDataCatalog` referencing an `s3tablescatalog/...` table also fails (`CREATE VIEW statements are not yet supported for Cross-Account Glue DataCatalogs`). So on the Iceberg path there is **no working `v_{table}` view** — materialize the enrichment/aggregation as a `mart_*` CTAS table in the same namespace (SQL in `reference/scripts.md`). On the **Hive** path, `v_{table}` views work normally and there is no need for mart tables.

---

## The two 🔴 Glue 5.x config rules (detail)

These are summarized as fatal rules 1–2 in `SKILL.md`. Full failure modes:

### S3 Tables catalog needs a separate JAR (`--extra-jars`)

`--datalake-formats iceberg` loads only Glue's *bundled* Iceberg runtime (Iceberg 1.7.1 on Glue 5.0). It does **NOT** include the S3 Tables catalog implementation. The `software.amazon.s3tables.iceberg.S3TablesCatalog` class lives in a separate open-source library, `s3-tables-catalog-for-iceberg-runtime` (AWS Labs). Without it the job hard-fails at session init / first write with:
```
Cannot find constructor for interface org.apache.iceberg.catalog.Catalog
```
Add the JAR explicitly via `--extra-jars`, and on Glue 5.0 also set `--user-jars-first: 'true'`. The JAR is **not** pre-hosted by AWS — download it (latest version, e.g. `s3-tables-catalog-for-iceberg-runtime-0.1.8.jar`) from [Maven Central](https://mvnrepository.com/artifact/software.amazon.s3tables/s3-tables-catalog-for-iceberg-runtime) or build it from the [AWS Labs repo](https://github.com/awslabs/s3-tables-catalog), upload it to your Glue assets bucket, and reference that S3 path (see `reference/iceberg-cdk.md`).

The class is `software.amazon.s3tables.iceberg.S3TablesCatalog` — do NOT substitute `org.apache.iceberg.aws.s3tables.S3TablesCatalog`, which does not exist in this library.

### `spark.sql.extensions` is static in Glue 5 — set via `--conf`, never at runtime

Glue 5 (Spark 3.5) treats `spark.sql.extensions` (and the catalog `--conf` keys) as **static** configs. Calling `spark.conf.set("spark.sql.extensions", ...)` *inside* the script fails with:
```
Cannot modify the value of a static config: spark.sql.extensions
```
ALL Spark/Iceberg config must be passed through the job's `--conf` argument in `defaultArguments`, never via `spark.conf.set(...)` in the Python. The scripts in `reference/scripts.md` therefore contain **no** `spark.conf.set` calls for these keys.

---

## Tooling version requirements (Iceberg / S3 Tables)

- AWS CLI ≥ 2.22 (for the `aws s3tables` subcommand and `aws glue create-catalog` / `get-catalog`)
- aws-cdk-lib ≥ 2.173 (for the `aws-cdk-lib/aws-s3tables` module — `CfnTableBucket` / `CfnNamespace`)
- `cdk` CLI version must match or exceed `aws-cdk-lib` (mismatch → opaque synth failures; fix with `npm install -g aws-cdk@latest`)

Detect:
```bash
aws s3tables help 2>/dev/null >/dev/null && echo "CLI supports s3tables" || echo "CLI too old — needs ≥ 2.22"
node -e "require('aws-cdk-lib/aws-s3tables')" 2>/dev/null && echo "CDK supports aws-s3tables" || echo "CDK too old — needs aws-cdk-lib ≥ 2.173"
```

⚠️ **DO NOT fall back to Hive because of tool versions** (fatal rule 5). Tool versions are fixable in 2 minutes; architecture decisions are permanent. If tools are too old, upgrade them:
```bash
# AWS CLI
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg" && sudo installer -pkg AWSCLIV2.pkg -target /
# CDK
npm install -g aws-cdk@latest
npm install aws-cdk-lib@latest  # in the project
```
Then re-verify before building. Falling back to Hive is appropriate only when the user explicitly opts in, or when S3 Tables is genuinely unavailable in the target region — never as a workaround for an old CLI/CDK.

**Bootstrap assets bucket deleted externally:** The CDK bootstrap assets bucket uses `RemovalPolicy: RETAIN`, so if it was deleted outside CloudFormation, CFN will NOT recreate it and deploys fail with a missing-bucket error. Re-bootstrap: `cdk bootstrap --force`.

---

## Korean / non-ASCII encoding rules across services

> ⚠ **IAM `description` field allows only ASCII Latin-1 characters** (`[ -~¡-ÿ]`). Korean characters, em dashes (—), and arrows (→) are rejected, **rolling back the entire IAM stack**. Use plain ASCII in role descriptions; put Korean copy in CDK `Tags` or a Glue table comment instead.

| Service | Korean / non-ASCII OK? | Notes |
|---|:---:|---|
| IAM role description | ❌ | ASCII Latin-1 only — rolls back the whole stack on violation |
| Glue table comments | ✅ | UTF-8 |
| Athena named query description | ✅ | UTF-8 |
| Quick Sight dashboard / visual titles | ✅ | UTF-8 |
| CDK feature flag values | ❌ | ASCII only |

> **Windows + Korean:** On Windows, set `AWS_CLI_FILE_ENCODING=UTF-8` before running CLI commands that pass `file://` JSON containing Korean text (Glue table comments, named-query descriptions, etc.). Without it: `text contents could not be decoded` errors.

---

## CDK gotchas

> **`CfnNamedQuery.workGroup` does NOT auto-create a CloudFormation dependency.** Passing the workgroup name as a string literal lets CFN try to create the named query and the workgroup in parallel, which fails on fresh stacks. Reference the workgroup token, or add the dependency explicitly:
> ```typescript
> const workgroup = new athena.CfnWorkGroup(this, 'Workgroup', { /* … */ });
> const namedQuery = new athena.CfnNamedQuery(this, 'ValidationQuery', {
>   workGroup: workgroup.ref,            // token — establishes a CFN dependency
>   database: `${props.prefix}_db`,
>   queryString: '...',
>   name: `${props.prefix}-supplier-top5`,
> });
> // OR, if you must use a string literal for the workgroup name:
> // namedQuery.addDependency(workgroup);
> ```

> **Athena views can't be CDK constructs** (no `CfnView`). Run `scripts/run-views.py` after `cdk deploy`, or wire it into the CDK app via `BucketDeployment` + a `CustomResource` for fully hands-off redeploys.

> **`{prefix}-quicksight-role` is usually NOT needed.** A vanilla Athena-on-S3 dashboard uses an AWS-managed service role (today `aws-quicksight-s3-consumers-role-v0`, falling back to `aws-quicksight-service-role-v0`), with `AWSQuickSightS3Policy` controlling S3 access. Keep the custom role only for VPC connections (private Redshift/RDS) or federated query. ⚠️ On the **Iceberg / S3 Tables** path the managed role also needs `s3tables:*` + `glue:*` (s3tablescatalog) grants or queries fail at render time even though the connection test passes — the consumption skill configures this separately.

---

## Lake Formation strict mode

If `get-data-lake-settings` → `CreateDatabaseDefaultPermissions` returns `[]` (empty), `IAMAllowedPrincipals` has been revoked account-wide and Glue tables created by this skill will NOT be accessible via IAM policies alone.

> ⚠️ **Lake Formation strict mode detected.** Options (pick one — do not let the skill auto-remediate):
> 1. Ask your account/security admin to re-grant `IAMAllowedPrincipals` on `CreateDatabaseDefaultPermissions` and `CreateTableDefaultPermissions`. Simplest fix.
> 2. Add explicit Lake Formation grants for each role this skill creates (Crawler, ETL, Athena, Quick Sight) on the database and tables. The skill generates the LF grant CDK on request.
> 3. Continue and accept that queries fail with `Insufficient Lake Formation permissions` until grants are added.
>
> **Do not proceed with deploy until the user confirms which option.**

If the precondition is OK, the skill still explicitly adds an `IAMAllowedPrincipals` grant on each new database it creates (defense-in-depth):

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

---

## Multi-CSV under one prefix (crawler collapse)

If a single S3 prefix contains multiple CSVs with DIFFERENT schemas (common for ERP exports), a Glue crawler collapses them into ONE combined table. Preferred: one folder per logical table. Or: separate `s3Targets` entries per file. (Hive path only — the Iceberg Glue job reads files directly and never has this problem.) Full detail in `reference/hive-pattern.md`.

---

## Known Issues table (for ARCHITECTURE.md)

| Issue | Impact | Workaround |
|-------|--------|------------|
| NFD (decomposed) Korean filenames on macOS | `NoSuchKey` on literal key match — every Korean/CJK filename from a Mac | `list_objects` prefix-match + use actual byte key; point Spark at the prefix (`reference/scripts.md` → Dirty data #1) |
| Mixed encoding per source (EUC-KR vs UTF-8) | Mojibake in Korean dimensions | Per-source `.option("encoding", ...)` branch (#2) |
| SAP trailing-minus negatives (`150.000-`) | `cast('double')`→NULL → cost/amount KPIs become 0 | `parse_num` helper — move sign to front before cast (#3) |
| Mixed date formats (incl. `yyyy/M/d H:m:s`, literal `'NULL'`) | Unparsed rows silently dropped → metric too low (16% loss seen) | `coalesce` of `to_timestamp` over ALL formats + filter literal `'NULL'` (#4) |
| Join-key leading-zero/whitespace mismatch | Joins return 0 rows → empty dimensions | `norm_key` (`regexp_replace(trim(c),'^0+','')`) both sides (#5) |
| Two sources share no key | Cannot join SAP groups ↔ finance categories | Domain-knowledge bridge table from name overlap (#6) |
| Excel subtotal/total rows + merged cells + currency strings | Double-counting, NULL amounts, wrong header | Excel normalization checklist (`reference/scripts.md`) |
| Coarser-grain measure in finer-grain mart | Duplicates on SUM (426→3,527, 8.3×); all validations still pass | Declare `-- GRAIN`/`platform.yaml grain` + pre-aggregate or single-row KPI mart (SKILL.md §4) |
| Row-count>0 / null-check pass but numbers wrong | "Validation passed ≠ correct answer" | Reconcile COUNT + key SUMs vs source (§8); consumption does KPI Numerical Accuracy Verification |
| S3 Tables catalog JAR not bundled in Glue 5.x | Job hard-fails without `--extra-jars` | Upload JAR manually + `--user-jars-first true` |
| `spark.sql.extensions` is static in Glue 5 | Cannot set at runtime → script fails | All Iceberg config in `defaultArguments --conf` only |
| Athena views unsupported across S3 Tables catalog | `CREATE VIEW` fails on s3tablescatalog refs | Use `mart_*` CTAS tables instead of `v_*` views |
| `DROP TABLE` (purge=false) fails on S3 Tables | Cannot do non-purge drop | Use S3 Tables API `delete-table` or `DROP TABLE ... PURGE` |
| CSV empty strings break Athena CAST | Entire CTAS aborts on one blank cell | Wrap every CAST in `NULLIF(col, '')` |
| Lake Formation strict mode (empty IAMAllowedPrincipals) | All Glue tables inaccessible via IAM | Detect in preconditions; re-grant or add explicit LF grants |
| KMS result encryption on workgroup | INSERT/MERGE/UPDATE/DELETE fail on S3 Tables | Use SSE-S3 result encryption |
| IAM description non-ASCII | Entire IAM stack rolls back | Plain ASCII in descriptions; Korean in Tags/comments |
