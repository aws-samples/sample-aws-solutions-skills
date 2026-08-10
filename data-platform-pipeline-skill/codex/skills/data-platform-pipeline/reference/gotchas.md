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

## Cross-region query layer (split-region only — see `reference/cross-region-query.md`)

Only relevant when follow-up #8 = yes. Each of these fails closed and several fail opaquely.

> ⚠️ **KMS keys are regional — the #1 silent breaker.** If the storage-region bucket is SSE-KMS encrypted, the key lives in `{aws_region}`. Athena running in `{query_region}` must call KMS *in the storage region* to decrypt: the query role needs `kms:Decrypt` on the **storage-region key ARN** (`arn:aws:kms:{aws_region}:...`) AND the key policy must allow that principal. A `{query_region}` key cannot decrypt storage-region objects. Use a **KMS multi-Region key** for portability. **CSE-KMS is not supported cross-region at all** (SSE-S3 and SSE-KMS are).

> **Athena resolves the Glue Data Catalog in its OWN region.** There is no parameter to point a workgroup at a remote catalog — you create a resource-link database in `{query_region}` whose `TargetDatabase` carries the target region. Likewise the results `OutputLocation` bucket must be in the workgroup's own region; only *source* data is remote.

> **S3 Tables cannot be reached by a cross-region `LOCATION`.** Table buckets expose no `s3://` path and the Glue integration is registered per-region, so neither an Athena `LOCATION` nor an explicit `metadata_location` can reference them. The Glue resource link is the **only** cross-region method for S3 Tables.

> **A resource-link database must NOT carry a `Description`** — Glue rejects it. Also: `CatalogId` must be the federated form `<account>:s3tablescatalog/<table-bucket>` (a bare account ID silently links the wrong catalog), and `TargetDatabase.Region` is the **target** region while `--region` is where the link is created. Reversing those two is the most common failure.

> **Same-account cross-region needs identity-based policy ONLY** — no S3 bucket policy, no Glue catalog resource policy, no KMS key policy. Those are required only cross-*account* (RAM share + LF grants). Adding them preemptively is extra surface that fails closed.

> **Cross-region resource links do NOT work for SAML-federated users.** Confirm the identity path (IAM Identity Center vs SAML) before committing to the query-in-place mechanism.

> **Redshift Spectrum cannot query Data Catalog tables from another region.** If `{query_region}` needs Spectrum, the resource link will not serve it — only replication will.

> **Replication cost tracks write churn, not table size.** Iceberg `MERGE INTO` rewrites data files and S3 Tables auto-compaction rewrites them again; every rewritten file replicates again. A MERGE-heavy table under aggressive compaction can move far more than its own size each month. Estimate from daily written/rewritten bytes, and treat compaction tuning as a replication-cost lever.

> **Do NOT use S3 Multi-Region Access Points.** No AWS doc supports MRAP ARNs as an Athena/Glue table `LOCATION`; control-plane requests are pinned to `us-west-2`; gateway VPC endpoints are unsupported; and an MRAP cannot serve an object from a bucket that lacks it (CRR is required anyway).

Also: the `{query_region}` console does not show source-region names for linked objects; cross-region Glue traffic may incur extra charges; China regions do not support cross-region queries; and the account must be opted into **both** regions.

---

## Downstream: AWS Entity Resolution / C360 (verified 2026-08, ap-northeast-2)

Relevant when the consumer is AWS Entity Resolution (the `unified-customer-profile` skill's C360 flow). **ER cannot read Iceberg / S3 Tables** — confirmed empirically against a real Iceberg table, both access paths rejected:

- `arn:aws:glue:...:table/s3tablescatalog/{bucket}/{ns}/{table}` → `ValidationException` (4-segment federated ARN fails the API regex)
- `arn:aws:s3tables:...:bucket/{bucket}/table/{table}` → `ValidationException`: *"inputSourceArn ... is not valid. Check that it follows the pattern: arn:...:glue:...:table/{glue_database_name}/{glue_table_name}"*

> ⚠️ **Do not trust the published `InputSource` API reference here.** It lists three ARN alternatives; the live API regex has **four**, including an `s3tables:` pattern. That pattern passes the regex and is then rejected by a second validation layer. ER supports **CSV and Parquet only**, and its docs state it "doesn't currently support Amazon S3 locations registered with AWS Lake Formation" — which the S3 Tables integration path typically creates.

**The bridge (keep Iceberg as the system of record; give ER a disposable Parquet projection):**

```sql
UNLOAD (SELECT variantid, firstname, lastname, email, phone,
               CAST(dateofbirth AS varchar) dateofbirth, loyaltynumber,
               street, city, postalcode, country, sourcechannel
        FROM "s3tablescatalog/{prefix}-table-bucket".{prefix}_db.mart_er_input)
TO 's3://{prefix}-analytics-zone/er-input/run={YYYY-MM-DD}/'
WITH (format='PARQUET', compression='SNAPPY')
```

Then declare a **classic 2-level Glue table** over that prefix with `CREATE EXTERNAL TABLE ... STORED AS PARQUET` — **no crawler**: the schema is already known from the SELECT, so a crawler adds only runs, cost, and type-misdetection risk.

> 🔴 **`UNLOAD` fails into a non-empty prefix** (`HIVE_PATH_ALREADY_EXISTS`) — it neither overwrites nor appends, so a scheduled refresh succeeds once then fails forever. Either `aws s3 rm <prefix> --recursive` first, or write to a **dated prefix** and `ALTER TABLE ... SET LOCATION` (preferred — no window where the table points at nothing, and prior runs stay available for comparing match results while tuning rules).

**ER input contract** (the mart must match exactly, all lowercase): `variantid` (UNIQUE_ID, **≤38 chars** for matching workflows), `firstname`, `lastname`, `email`, `phone`, `dateofbirth`, `loyaltynumber`, `street`/`city`/`state`/`postalcode`/`country`, `sourcechannel`. Max 34 columns (24 matchable). Reserved names to avoid: `MatchId`, `MatchRule`, `RecordId`, `SourceId`, `TargetId`. **Partition pruning does not work for matching workflows** (ID mapping only) — scope rows in the `UNLOAD` SELECT instead.

**Incremental on both sides (the C360 default — customer data churns):** Iceberg `MERGE INTO` on a stable business key upstream; ER `incrementalRunConfig: {"incrementalRunType": "IMMEDIATE"}` (console label "Automatic") downstream, which needs **EventBridge notifications enabled** on the input bucket. ⚠️ ER incremental is **rule-based only — not supported for `ML_MATCHING` or `PROVIDER`**; those are batch.

🔴 **Do NOT deduplicate customers across channels in the pipeline.** ER's entire job is resolving channel variants into one identity. One row per (customer × source channel). If the pipeline merges them first, ER has nothing to match.

🔴 **Two normalization bugs that pass row-count reconciliation and still produce wrong C360 results** (both hit in live testing):

**1. Name order — CANNOT be inferred from the string. It must be DECLARED per source.**

`KIM MINHO` and `MINHO KIM` are both valid romanized Korean names; nothing in either string says which token is the family name. Only the *source system* knows its convention. A heuristic ("last word = given name") is right for one source and backwards for the next, and the same person then gets opposite first/last names per channel — so a `Name AND Email` rule fails **even with byte-identical emails** (keys inside a rule are AND-ed).

Ask per source, then pass the answer into the transform. Three modes cover real Korean data:

| Mode | Source examples | `김민호` | `KIM MINHO` | `민호` |
|---|---|---|---|---|
| `family_first` | Korean-UI web/ERP, SAP romanized exports | 민호 / 김 | MINHO / KIM | 민호 / — |
| `given_first` | call-center free text, Western-facing forms | 민호 / 김 | MINHO / — ⚠ | 민호 / — |
| `given_only` | app display names, nicknames | — | — | 민호 / — |

```python
HANGUL = r'^[가-힣]+$'
def split_name(c, order):   # order DECLARED per source — never inferred
    t = trim(coalesce(c, lit('')));  ntok = size(split(t, ' '))
    is_h1 = t.rlike(HANGUL) & (ntok == lit(1))          # single Hangul token
    h_fam = when(length(t) >= 2, t.substr(lit(1), lit(1))).otherwise(lit(''))
    h_giv = when(length(t) >= 2, t.substr(lit(2), lit(20))).otherwise(t)
    c_giv = trim(element_at(split(t, ','), 2)); c_fam = trim(element_at(split(t, ','), 1))
    tok1 = element_at(split(t, ' '), 1); tokN = element_at(split(t, ' '), -1)
    if order == 'given_only':     # never split a single token
        giv = when(t.contains(','), c_giv).when(ntok == lit(1), t).otherwise(tok1)
        fam = when(t.contains(','), c_fam).when(ntok == lit(1), lit('')).otherwise(tokN)
    elif order == 'family_first':
        giv = when(t.contains(','), c_giv).when(is_h1, h_giv).when(ntok == lit(1), t).otherwise(tokN)
        fam = when(t.contains(','), c_fam).when(is_h1, h_fam).when(ntok == lit(1), lit('')).otherwise(tok1)
    else:                          # given_first
        giv = when(t.contains(','), c_giv).when(is_h1, h_giv).when(ntok == lit(1), t).otherwise(tok1)
        fam = when(t.contains(','), c_fam).when(is_h1, h_fam).when(ntok == lit(1), lit('')).otherwise(tokN)
    return giv, fam
```

Three rules this encodes, each a bug found in testing: the **comma form** (`Kim, Min-Ho`) always wins regardless of mode; a **multi-char Hangul token** splits as family(1) + given(rest); a **single-token given name** (`민호`) must NOT be split into `호/민`.

**2. Phone country code.** `+82-10-1234-5678` → `821012345678` vs `010-1234-5678` → `01012345678`. Digit-stripping is not normalization:

```python
d = regexp_replace(coalesce(c, lit('')), r'[^0-9]', '')
phone = when(d.startswith('82'), concat(lit('0'), d.substr(lit(3), lit(20)))).otherwise(d)
```

Both leave counts reconciling perfectly while silently failing to merge customers. Verify by **inspecting MatchID groups**, not job status: `matchIDs` close to `inputRecords` means almost nothing merged.

🔴 **Entity Resolution `applyNormalization: true` STRIPS Hangul.** Verified live: a mart containing `민호 / 김` came back from ER with **empty** first/last name fields, and those records matched nothing on name rules. Set **`applyNormalization: false`** whenever names may contain Hangul (or any non-Latin script) and do the normalization upstream in the Glue job, where you control it. On the same 15-record dataset this moved results from 4 merged groups to 5.

🔴 **ER matching rules are a WATERFALL — order changes the answer.** Same data, same 15 records, only the rule order differed:

| Rule order | Groups (ideal = 5) |
|---|---|
| `LoyaltyOnly` → `EmailAndPhone` → `PhoneAndDOB` → `NameAndPhone` | **8** |
| `PhoneAndDOB` → `EmailAndPhone` → `LoyaltyOnly` → `NameAndPhone` | **6** |

Counter-intuitively, putting the *strong unique key first* produced **worse** merging: records grouped by `LoyaltyOnly` stopped being considered by later rules, so a call-center record with an identical email and phone formed its own group. Order rules by **the widest population they can join**, not by how authoritative the key feels — and always compare at least two orderings against the same data before fixing the rules. (Rule design lives in the consumption/C360 skill; this note exists so the pipeline's mart carries enough columns to support more than one ordering.)

Other ER limits: available in `ap-northeast-2` incl. ML matching, but **ML record cap is 150M in Seoul** vs 600M in tier-1 regions; **1 concurrent matching job** (not adjustable).

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
| SSE-KMS bucket + cross-region Athena (split-region) | Queries fail to decrypt — key is in the storage region | `kms:Decrypt` on the storage-region key ARN + key policy allows the principal; or multi-Region key |
| Resource link created with `Description`, or bare `CatalogId` | Glue rejects the link, or it silently targets the wrong catalog | Omit `Description`; use `<account>:s3tablescatalog/<bucket>`; `TargetDatabase.Region` = target, `--region` = where the link lives |
| Cross-region resource link + SAML-federated users | Link access unsupported — queries fail for those users | Confirm IAM Identity Center vs SAML before choosing query-in-place |
| `DIRECT_QUERY` dataset on a split-region resource link | Every visual interaction bills a cross-region scan (1000×+ cost) | Force SPICE; set `requires_spice: true` in `platform.yaml` |
| Full-refresh SPICE on a split-region resource link | ~45× the cross-region transfer of a replicated setup | Design marts incrementally (MERGE + watermark) from the start — not retrofittable cheaply |
| S3 Tables replication + high MERGE/compaction churn | Replication transfer far exceeds table size | Estimate from daily rewritten bytes; tune compaction; replicate `mart_*` only, never `base_*` |
| Cross-account replication configured before the destination bucket policy exists | `put-table-replication` succeeds, then status is `FAILED` "Insufficient permissions" — looks broken, is just out of order | Write the destination table-bucket policy FIRST; the service auto-retries `FAILED`→`PENDING`→`COMPLETED` after the fix — do not recreate the config |
| Replica table serving stale data | Dashboards keep working with old numbers; a row-count mismatch is misread as a data bug | Assert `get-table-replication-status` = `COMPLETED` before GATE 4 reconciliation |
| Replicating a V2→V3 upgraded table, or one with tags/branches | Replication unsupported for that table — discovered at build time | Check table shape before proposing replication; fall back to the resource link |
| SAP source routed to generic JDBC | Misses the native SAP OData / SAP HANA connectors and ODP delta | Ask which SAP access exists first → `reference/sap-sources.md` |
| SAP OData `NUM_PARTITIONS` left at default 1 | Large entity reads single-threaded — "why is this so slow" | Set `PARTITION_FIELD` + `LOWER_BOUND`/`UPPER_BOUND` + `NUM_PARTITIONS` |
| SAP OData `SELECT *` on a wide entity | Wasted read time + Athena scan budget downstream | Always set `SELECTED_FIELDS`; push filters via `FILTER_PREDICATE` |
| Expired/invalid SAP ODP delta token | **Silent partial load**, not an error | Reconcile counts every run (GATE 4); never trust the token alone |
| SAP OData OAuth 2.0 (`AUTHORIZATION_CODE` only) | Interactive login — not a clean CDK deploy | Create the connection via console; document as a manual post-deploy step |
| SAP OData V4-only services exposed | Glue supports OData **2.0** only | Have Basis activate V2 services, or use the SAP HANA connector |
| SAP HANA connector on Glue < 4.0 | Connector unavailable | Glue 4.0+ required — upgrade, don't work around (fatal rule 5) |
| Proposing the SAP HANA connector for a non-HANA SAP landscape | Dead end — many ECC systems run on Oracle/Db2/SQL Server, so there is no HANA endpoint | Confirm the SAP database platform first; if not HANA, use OData or exports |
| Glue Studio visual editor appears to support only "Amazon RDS" + "Glue Data Catalog" | User concludes self-managed SQL Server on EC2 is unsupported and asks to change architecture | The "Amazon RDS" node IS the JDBC node (docs: RDS "or external to Amazon RDS"). This skill uses code-based `from_options(connection_type="sqlserver")` anyway — the visual picker is irrelevant |
| Crawling a JDBC source into the Catalog before the Iceberg job | Needless crawler runs + type-misdetection + schema drift vs the job's typed transform | Iceberg path uses ZERO crawlers: `from_options` reads the DB directly; only output tables are cataloged |
| Pointing AWS Entity Resolution at an Iceberg / S3 Tables table | `ValidationException` — ER reads CSV/Parquet only, and rejects both the federated and native s3tables ARN | Athena `UNLOAD` → Parquet → classic 2-level Glue table (see "Downstream: AWS Entity Resolution") |
| `UNLOAD` into a non-empty prefix | `HIVE_PATH_ALREADY_EXISTS` — scheduled refresh works once, then fails every run | Clear the prefix first, or use a dated prefix + `ALTER TABLE SET LOCATION` |
| Pipeline deduplicates customers before Entity Resolution | ER has no variants left to resolve — the C360 build produces nothing | One row per (customer × source channel); dedup is ER's job |
| Inconsistent name order / phone country code across channels | Counts reconcile but customers silently fail to merge; `Name AND Email` fails despite identical emails | Canonicalize name order per source and normalize country codes; inspect MatchID groups, not job status |
| ER incremental requested with ML matching | `ML_MATCHING`/`PROVIDER` do not support incremental processing | Rule-based for incremental; ML is batch-only |
| Glue job in a VPC without `s3tables` interface endpoint | Reads succeed, then `writeTo` dies ~90s in with `Connect to s3tables… Connect timed out` | Create `s3tables` + `glue` + `secretsmanager` interface endpoints and `s3` + `dynamodb` gateway endpoints BEFORE the first run (`reference/vpc-connectivity.md`) |
| Glue job in a VPC without S3 gateway endpoint | Fails at submit: `Could not find S3 endpoint or NAT gateway for subnetId` | Same — S3 gateway endpoint is mandatory, an IGW does not substitute |
| Name order inferred from the string | `KIM MINHO` vs `MINHO KIM` are indistinguishable; same person gets opposite first/last per source and never merges | DECLARE order per source (`family_first`/`given_first`/`given_only`) |
| Single-token Hangul given name (`민호`) split by char | Becomes `호`/`민` — matches nothing | `given_only` mode; never split a single token |
| ER `applyNormalization: true` with Hangul names | Name fields return **empty**; name rules match nothing | Set `applyNormalization: false`; normalize upstream in Glue |
| ER strong unique key placed first in rule order | Fewer merges, not more — early grouping excludes records from later rules | Order rules by widest joinable population; test ≥2 orderings |
| Aurora `--engine-version` guessed | `InvalidParameterCombination: Cannot find version X for aurora-postgresql` | Probe first: `aws rds describe-db-engine-versions --engine aurora-postgresql` |
| Re-running a Glue job while one is in flight | `ConcurrentRunsExceededException` — default max concurrency is 1 | Poll for `RUNNING`=0 first, or raise `--execution-property MaxConcurrentRuns` |
| `psycopg2` in a Lambda/Glue package built on the workstation | `No module named 'psycopg2._psycopg'` | `pip install --platform manylinux2014_aarch64 --only-binary=:all:` and set `--architectures arm64` |
| SAP HANA raw ABAP tables without client filter | Rows from other SAP clients silently included → inflated aggregates | Filter on `MANDT` (client); confirm which client with the customer |
