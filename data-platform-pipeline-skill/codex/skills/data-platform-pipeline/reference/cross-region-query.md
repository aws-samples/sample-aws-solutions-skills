# Split-Region Catalog + Query Layer (Opt-in) — Full Reference

Storage and ETL stay in `{aws_region}`; the **catalog + query** layers run in a second region `{query_region}`. Use **only** when the user opts in via follow-up #8: data must stay in `{aws_region}`, but a consumer service (BI, an agentic AI feature, a downstream engine) is only available elsewhere. Default is `query_region == aws_region`, in which case ignore this file entirely.

This file is self-contained for the split-region add-on: mechanism choice, both build paths, IAM, cost model, limitations, cross-account deltas, and teardown. It **layers on top of** either storage pattern — the storage region is built exactly as in `SKILL.md` §4 (Iceberg) or `reference/hive-pattern.md` (Hive), unchanged.

Unless stated otherwise, §3–§7 assume **same account, two regions** (the common case). Both mechanisms also work **cross-account**, including cross-account and cross-region combined → **§8**.

**Out of scope** (do not build these here): DR / failover, active-active, multi-region writes, multi-region ETL. This component is **read-only query of storage-region tables from a second region**. A request for cross-region resilience is a different architecture — say so rather than extending this.

---

## 1. When this applies

| Driver | In scope? | What the driver tells you |
|---|---|---|
| **Regulatory residency mandate** + consumer service absent from `{aws_region}` | ✅ | Mechanism B is **disqualified** (it copies data out). §2 presents A only. |
| Residency **preference** + consumer service elsewhere | ✅ | Both mechanisms valid — **the user chooses** (§2). |
| Any downstream engine only available in another region | ✅ | Both valid. |
| Centralizing several storage regions into one query region | ✅ | Both valid; note B scales better across many sources. |
| End-user query **latency** in `{query_region}` | ✅ | Note A cannot help — it *adds* latency. Only B moves data closer. |
| DR / failover / active-active / multi-region writes | ❌ | **Out of scope.** Different architecture. |

### Worked example — Amazon Quick's agentic AI features

The motivating real-world case: Korean customer data in `ap-northeast-2` (Seoul), but **Amazon Quick's agentic AI surface** (chat agents, Spaces, Flows, Research, Generate Analysis, Dataset Chat) is not available there. Quick Sight dashboards work in Seoul; the agentic features need a fully-supported region such as `ap-northeast-1` (Tokyo) or `us-east-1`.

> **Verify at build time — this footprint moves.** Check https://docs.aws.amazon.com/quick/latest/userguide/regions.html. Note that page carries **two** tables: the main region table (an asterisk means *Quick Sight features only*) and a separate "Amazon Q in Quick" generative-BI table with different coverage. A region can lack the agentic Quick Suite while still having the older generative-BI layer — do not tell a customer a region has "no AI features" without checking both. As of 2026-08: Seoul is asterisked (Quick Sight only); Tokyo and `us-east-1` are fully supported. The AWS CLI/SDK namespace is still `quicksight:*` regardless.

Same pattern applies to any service with an uneven regional footprint. Nothing below is Quick-specific.

---

## 2. Two mechanisms — the USER chooses. Do NOT pick for them.

> This is a deliberate exception to the skill's "opinionated, not presented as options" stance (`SKILL.md`). The decision turns on a **legal** question (is residency a mandate or a preference?) and a **financial** one (mart size × refresh mode) that you cannot answer for the customer. Guessing wrong is either a compliance incident or a large recurring bill. **Neither mechanism is marked recommended. Neither is a default.**

- **Mechanism A — query in place.** A Glue **resource link** in `{query_region}` points at the storage-region database. Data files never move; Athena in `{query_region}` scans them remotely.
- **Mechanism B — replicate.** The **mart layer** is replicated into `{query_region}`, which then gets its own local catalog and queries locally. No link, no cross-region IAM.

> **These two questions are GATE 1 acknowledgement items** (`SKILL.md` → Validation Gates). They are policy decisions, so the build does not proceed until the user has answered them explicitly — not inferred from the region choice, the industry, or a phrase like "our data stays in Korea."

### Step 1 — eliminate invalid options (these are facts, not preferences)

```
Q1. Is keeping the bytes in {aws_region} a REGULATORY MANDATE
    (a regulator or contract forbids egress), or a PREFERENCE?

    ├── MANDATE → Mechanism B is DISQUALIFIED. It copies data out of
    │             the region. Do not offer it. Go to Q2.
    │
    └── PREFERENCE → both remain valid. Go to Step 2.

Q2. (mandate only) Does the mandate permit AGGREGATED RESULT ROWS to
    transit and be cached in {query_region}?

    ├── YES → Mechanism A is viable. Build it (§3/§4).
    │
    └── NO (zero egress) → NEITHER mechanism qualifies. STOP.
          Recommend Quick Sight dashboards in {aws_region} without the
          agentic features (consumption skill `region-constraints.md`
          §4 Option A). Do NOT sell a split-region build that does not
          actually satisfy the constraint.
```

Ask Q1 in those words — "mandate or preference" — and get an explicit answer. **Never infer it** from the customer's region choice, their industry, or a phrase like "our data stays in Korea." Most customers who say that have a preference; a minority have an FSC/regulator requirement.

### 🔴 What Mechanism A does and does not guarantee — state this BEFORE promising residency

Mechanism A is **not zero egress**:

| | Stays in `{aws_region}` | Crosses to `{query_region}` |
|---|---|---|
| **A — resource link** | Source of record; all `base_*` + `mart_*` data files; the bulk bytes | **Query result rows** (normally derived aggregates), cached in SPICE; query metadata; chat/agent transcripts |
| **B — replication** | Source of record | A **full copy** of the replicated mart tables, plus everything above |

A's honest claim is: *"the data lake stays in `{aws_region}`; aggregated query results transit and are cached in `{query_region}`."* It is **not** "no data leaves the region." Derived aggregates are frequently acceptable to a regulator where raw records are not — but that is the customer's counsel's call, made on an accurate description. Give them the accurate description.

### Step 2 — gather the four facts before presenting anything

The point of §2 is to let the user choose between **numbers**, not adjectives. Do not present options until you have these:

| Fact | How to get it | Why it matters |
|---|---|---|
| Mart-layer size (GB) | `SELECT SUM(file_size_in_bytes) FROM "{table}$files"` per mart, or S3 Tables metadata | Sets the absolute cost delta. Under ~10 GB the whole question is worth $32–321/yr |
| Refresh cadence + mode | Ask the user; check whether marts have a usable watermark column | Full vs incremental is a ~10× swing (§6) |
| Mart build pattern | `platform.yaml` `written_by`, or read the mart SQL | Full CTAS rebuild forecloses incremental refresh unless redesigned |
| Write churn | Glue job schedule + MERGE frequency + compaction settings | B's ongoing cost; A's ongoing cost is reads (§6 crossover) |

### Step 3 — present both, neutrally, with the user's own numbers substituted

```
Your marts total {N} GB, refreshed {cadence}. Two ways to give
{query_region} query access. Both are supported; they trade
differently, so this is your call:

Option A — Query in place (Glue resource link)
  • Data files never leave {aws_region}; {query_region} holds only a
    catalog reference
  • Aggregated RESULT ROWS do transit and are cached in {query_region}
  • Cost: ~${X}/yr at your size and cadence — pays inter-region
    transfer on every refresh
  • Requires: SPICE (not DIRECT_QUERY), and incremental marts to
    avoid the ~45x full-refresh penalty
  • Ops: cross-region IAM + resource link + Lake Formation
    considerations to maintain

Option B — Replicate the mart layer
  • A second copy of the mart tables lives in {query_region}
  • Cost: ~${Y}/yr — pays transfer once per byte written, plus
    replica storage
  • Simpler: {query_region} gets its own integration and local
    catalog. No resource link, no cross-region IAM, no LF grants
  • Ops: replication configuration + monitoring replication lag
  • Freshness: replica typically current within minutes

Which fits your constraints?
```

Compute `{X}` and `{Y}` from §6 using the Step 2 facts. If marts are under ~10 GB, **say the cost difference is immaterial** and tell them to decide on residency and operational simplicity instead of pretending the numbers decide it.

### Side-by-side comparison

| | A — query in place | B — replicate |
|---|---|---|
| What crosses the boundary | Result rows only | Full mart copy + result rows |
| Destination storage cost | none | mart-layer size × $/GB-month |
| Per-scan cost | inter-region transfer **every scan** | none (local reads) |
| Residency posture | data files stay put | second copy exists in `{query_region}` |
| Ops surface | resource link + cross-region IAM + LF | replication config + lag monitoring |
| Freshness in `{query_region}` | live (always current) | minutes behind source |
| Failure mode | query fails (permissions/region) — no stale data | replication lag → silently stale results |
| Cross-account setup (§8) | **RAM invitation → destination accepts** | **No handshake** — destination pre-authorizes by bucket policy; source pushes |
| Table-shape restrictions | none | no V2→V3 upgraded tables, no tags/branches, no compaction on replicas (§5) |

Two notes that correct intuitions which reliably mislead:

- **B is less machinery, not more.** `{query_region}` gets its own `s3tablescatalog` integration and a local `{prefix}_db`; §3's resource link, cross-region IAM Sids, and LF grants all become unnecessary.
- **"Replication doubles my storage cost" is true and usually the smaller number.** Only the mart layer is replicated (§5), and both mechanisms pay the same per-GB transfer rate — B once per byte written, A on every scan (§6).

### Step 4 — record the choice AND the reasoning

Write `cross_region_mechanism`, `residency` (`mandate | preference`), and `query_region` into `platform.yaml`, plus a residency-disclosure block in `ARCHITECTURE.md` naming exactly what crosses the boundary and who approved it (`SKILL.md` §10). Without the `residency` key, a later session sees a resource link and "optimizes" it into replication — silently breaking a compliance constraint.

---

## 3. Mechanism A — Iceberg / S3 Tables

### 🔴 Hard constraints (violating any of these fails, often opaquely)

1. **Athena resolves the Glue Data Catalog in its OWN region.** You never point a workgroup at a remote catalog — there is no region parameter for that. You create a **resource-link database in `{query_region}`** whose `TargetDatabase` carries the storage region.
2. **S3 Tables cannot be reached by a cross-region `LOCATION`.** Table buckets expose no `s3://` path and the Glue integration is registered per-region. The resource link is the **only** method.
3. **Athena results `OutputLocation` must be in the workgroup's own region.** Only *source* data is remote. A `{aws_region}` results bucket fails.
4. **Omit `Description`** when creating a resource-link database — Glue rejects a resource link that carries one.
5. **Athena federated (Lambda connector) queries and Quick's native S3 Tables connector are same-region only.** Neither works across regions; route through Athena + the resource link.

### Source form determines the method — three cases

| Case | Source form | Cross-region method | Complexity |
|---|---|---|---|
| 1 | Plain files (CSV/Parquet) in a general S3 bucket | Athena DDL: `CREATE EXTERNAL TABLE` with a foreign-region `LOCATION` | Low |
| 2 | Iceberg on a **general** S3 bucket | Glue `create-table` with explicit `metadata_location` (Athena DDL will not do this) | Medium |
| 3 | **S3 Tables Iceberg** (this skill's default) | **Glue resource link — mandatory, no alternative** | Medium |

Case 3 is why this file exists: S3 Tables is managed storage with no exposed object path and self-managed metadata, so neither a `LOCATION` nor a `metadata_location` can reference it. Access must traverse the `s3tablescatalog` federated catalog, and cross-region that means a resource link.

### A1 — Preconditions

```bash
# Both regions must be enabled for the account
aws ec2 describe-regions --region-names {aws_region} {query_region} \
  --query 'Regions[].RegionName' --output text

# Storage region: s3tablescatalog integration must exist (SKILL.md §1 already checks this)
aws glue get-catalog --catalog-id s3tablescatalog --region {aws_region} >/dev/null 2>&1 \
  && echo "storage region integrated" || echo "NOT integrated — enable before proceeding"

# Lake Formation posture in BOTH regions (strict mode in either one changes A5)
for R in {aws_region} {query_region}; do
  echo -n "$R: "
  aws lakeformation get-data-lake-settings --region $R \
    --query 'DataLakeSettings.CreateDatabaseDefaultPermissions' --output json
done
```

`{query_region}` does **not** need its own `s3tablescatalog` integration under Mechanism A — the link resolves to the storage region's catalog. (It *does* under Mechanism B.)

### A2 — Query-region stack

Reuse the `CfnWorkGroup` CDK from `SKILL.md` §7 **verbatim** — same scan cutoff, same SSE-S3 result encryption (KMS still breaks DML), same tags. Only two things change: the stack's region, and the results bucket.

```typescript
// bin/app.ts — a second stack, explicitly pinned to the query region
new QueryStack(app, 'QueryStack', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: constants.QUERY_REGION },
  prefix: constants.PREFIX,
  resultsBucket: `${constants.PREFIX}-analytics-${constants.QUERY_REGION}`,
});
```

> 🔴 **Do NOT use cross-stack references between the storage and query stacks.** Setting `crossRegionReferences: true` makes CDK provision SSM parameters and Lambda-backed custom resources to ferry values between regions — extra failure surface for no benefit here. Every value the query stack needs (`{prefix}`, both region names, the account ID, the table-bucket name) is derivable from `constants.ts`. Pass literals.

Create the results bucket in `{query_region}` (constraint 3):

```bash
aws s3 mb s3://{prefix}-analytics-{query_region} --region {query_region}
```

### A3 — The resource link

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > /tmp/resource-link.json <<EOF
{
  "Name": "{prefix}_db_link",
  "TargetDatabase": {
    "CatalogId": "${ACCOUNT_ID}:s3tablescatalog/{prefix}-table-bucket",
    "DatabaseName": "{prefix}_db",
    "Region": "{aws_region}"
  }
}
EOF

aws glue create-database \
  --database-input file:///tmp/resource-link.json \
  --region {query_region}
```

Three details that each cause a failure if missed:
- **`CatalogId` must be the federated form** `<account>:s3tablescatalog/<table-bucket>`, not the bare account ID. A bare account ID silently links to the wrong (regular Glue) catalog.
- **`Region` is the TARGET** (`{aws_region}`), while `--region` is where the link is created (`{query_region}`). Reversing these is the most common mistake.
- **No `Description` key** (constraint 4).

**CDK vs CLI:** `CfnDatabase.databaseInput.targetDatabase` may not expose `region` in the pinned `aws-cdk-lib`. Probe before committing to CDK:

```bash
node -e "const{CfnDatabase}=require('aws-cdk-lib/aws-glue');console.log('aws-glue loaded')" 2>/dev/null \
  && grep -rq 'region' node_modules/aws-cdk-lib/aws-glue/lib/glue.generated.d.ts \
  && echo "check DatabaseIdentifierProperty for a region field"
```

If `region` is absent, create the link via the CLI in the post-deploy bootstrap, or wrap the call in an `AwsCustomResource` so it stays inside the CDK lifecycle. Do **not** downgrade the architecture over this (fatal rule 5) — it is a one-call workaround.

### A4 — IAM (identity-based only)

Three Sids on the `{query_region}` Athena query role, plus results-bucket write. Note the ARNs point at `{aws_region}`:

```typescript
queryRole.addToPolicy(new iam.PolicyStatement({
  sid: 'StorageRegionS3TablesRead',
  actions: [
    's3tables:GetTableData', 's3tables:GetTableMetadataLocation',
    's3tables:GetTable', 's3tables:GetNamespace',
    's3tables:ListTables', 's3tables:ListNamespaces', 's3tables:GetTableBucket',
  ],
  resources: [
    `arn:aws:s3tables:${STORAGE_REGION}:${account}:bucket/${prefix}-table-bucket`,
    `arn:aws:s3tables:${STORAGE_REGION}:${account}:bucket/${prefix}-table-bucket/table/*`,
  ],
}));
queryRole.addToPolicy(new iam.PolicyStatement({
  sid: 'StorageRegionGlueCatalogRead',
  actions: ['glue:GetDatabase', 'glue:GetDatabases', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartitions'],
  resources: [
    `arn:aws:glue:${STORAGE_REGION}:${account}:catalog/s3tablescatalog/${prefix}-table-bucket`,
    `arn:aws:glue:${STORAGE_REGION}:${account}:catalog/s3tablescatalog*`,
    `arn:aws:glue:${STORAGE_REGION}:${account}:database/*`,
    `arn:aws:glue:${STORAGE_REGION}:${account}:table/*/*`,
  ],
}));
queryRole.addToPolicy(new iam.PolicyStatement({
  sid: 'QueryRegionGlueLocalRead',   // the link object itself lives here
  actions: ['glue:GetDatabase', 'glue:GetDatabases', 'glue:GetTable', 'glue:GetTables'],
  resources: [
    `arn:aws:glue:${QUERY_REGION}:${account}:catalog`,
    `arn:aws:glue:${QUERY_REGION}:${account}:database/${prefix}_db_link`,
    `arn:aws:glue:${QUERY_REGION}:${account}:table/${prefix}_db_link/*`,
  ],
}));
```

> **Same-account cross-region needs identity-based policy ONLY.** No S3 bucket policy, no Glue catalog resource policy, no KMS key policy. Do not add resource policies preemptively — they are extra surface that fails closed. **Cross-account is a different story: RAM + Lake Formation grants become mandatory → §8.**

Scope `s3tables:*` to the specific table-bucket ARN — never `*` (`SKILL.md` §5).

### A5 — Lake Formation

With this skill's default **IAM-only mode**, cross-region resource links need **no LF grants**: for S3 data not registered with Lake Formation, access is governed by IAM policies on S3 and Glue actions. That is the documented behavior and it is what makes A4 sufficient.

If A1 showed **strict mode** (`CreateDatabaseDefaultPermissions: []`) in **either** region, add explicit grants: `DESCRIBE` on the resource link in `{query_region}`, and `SELECT` on the underlying database/tables in `{aws_region}`. Handle strict mode per `reference/gotchas.md` → Lake Formation strict mode — stop and let the user pick, do not auto-remediate.

> ⚠️ **No cross-region resource-link calls by SAML users.** If the customer's analysts federate via SAML (not IAM Identity Center), cross-region link access is unsupported. Confirm the identity path before committing to Mechanism A.

### A6 — Marts stay in the storage region

🔴 Fatal rule 3 still applies, and residency reinforces it: **do not CTAS a mart in `{query_region}`.** A CTAS there writes result data into the query region — copying data, breaking the residency claim Mechanism A exists to preserve, and duplicating the mart. Marts are written by the Glue job in `{aws_region}` (`SKILL.md` §4) and read through the link.

The consumption layer reads `mart_*` through the link exactly as it would read them locally.

### A7 — Verification (part of the GATE 4 reconciliation contract)

```bash
# 1. The link resolves and exposes the storage-region tables
aws glue get-database --name {prefix}_db_link --region {query_region}
aws glue get-tables --database-name {prefix}_db_link --region {query_region} \
  --query 'TableList[].Name' --output text

# 2. Row counts must MATCH across regions — the cross-region half of SKILL.md GATE 4 / §8
aws athena start-query-execution \
  --work-group {prefix}-workgroup \
  --query-string 'SELECT COUNT(*) FROM "AwsDataCatalog"."{prefix}_db_link"."{table}"' \
  --region {query_region}
# compare against the same COUNT(*) run in {aws_region} — a mismatch means the link
# resolved to the wrong catalog or a stale target, NOT a data problem
```

> **Reference form from the query region is `"AwsDataCatalog"."{prefix}_db_link"."{table}"`** — the *local* catalog plus the link name. Do **not** use the federated `"s3tablescatalog/{prefix}-table-bucket"...` form there; that catalog does not exist in `{query_region}`. This differs from the storage region and is a common copy-paste failure.

---

## 4. Mechanism A — Hive / plain-S3 path

The resource link works identically for a regular Glue database — same `create-database` call as A3, except `CatalogId` is the bare account ID (there is no federated catalog):

```json
{
  "Name": "{prefix}_db_link",
  "TargetDatabase": { "CatalogId": "${ACCOUNT_ID}", "DatabaseName": "{prefix}_db", "Region": "{aws_region}" }
}
```

IAM drops the `s3tables:*` Sid and instead needs `s3:GetObject` + `s3:ListBucket` on the storage-region curated/analytics buckets. Everything else in A2–A7 is unchanged. **Prefer the link over DDL replay** — one mechanism for both storage patterns, and no schema drift.

### Fallback — DDL replay (use only if a resource link is unavailable)

Plain-S3 tables *can* be re-declared in `{query_region}` with a `LOCATION` pointing at the storage-region bucket (case 1 above). The consumption skill documents this sequence in `region-constraints.md` §5: `SHOW CREATE TABLE` in the storage region → replay the DDL in `{query_region}` → **`MSCK REPAIR TABLE`**, because `SHOW CREATE TABLE` preserves `LOCATION` and the partition *columns* but **not the partition values** — a partitioned table returns zero rows until partitions are reloaded.

> ⚠️ **Replay carries a drift liability the link does not.** Two independent copies of the schema now exist. Any `ALTER TABLE ADD COLUMNS` in the storage region must be replayed manually, or `{query_region}` silently serves a stale schema. A resource link always reflects the source. Treat replay as a workaround, and record it in `ARCHITECTURE.md` as technical debt.

---

## 5. Mechanism B — replicate the mart layer

Build this **only** if the user chose B in §2.

**S3 Tables cross-region replication** creates a read-only replica in `{query_region}`: commits (snapshots, metadata, data files) are applied in **source order**, typically current within minutes. Multiple destinations are supported, same- or cross-account. Snapshot retention on the replica is independent — a longer retention there buys extended time travel. Replicas can use Intelligent-Tiering.

Then `{query_region}` is an ordinary single-region setup: enable its **own** `s3tablescatalog` integration, and Athena queries a **local** database. §3 is skipped entirely — no resource link, no cross-region IAM, no LF cross-region grants.

For the **Hive** path, the equivalent is S3 Cross-Region Replication, plus **Batch Replication** to seed objects that already exist (CRR only covers new writes), then a crawler or DDL in `{query_region}`.

Cost lines: destination storage + replication PUT requests + table commits + object monitoring + one-time inter-region data transfer per byte.

### 🔴 Mechanism B eligibility — check these BEFORE proposing it

Replication has table-shape restrictions the resource link does not. Verify against the actual marts, not in the abstract:

| Restriction | Consequence |
|---|---|
| **Iceberg V2→V3 upgraded tables cannot replicate** | V2 and V3 tables each replicate fine; a table *upgraded* from V2 to V3 does not. Check before proposing B |
| **Tables with tags or branches are not supported** | Rules out replication for tables using Iceberg branching/tagging workflows |
| **Metadata files > 500 MB are not supported** | A concern only for very high-snapshot-count tables |
| **S3 Metadata tables / AWS-generated system tables** | Not replicable |
| **Compaction is NOT supported on replica tables** | Replicas receive the source's compacted snapshots instead — so compaction must be tuned at the **source**, and the replica inherits it. It also means every source compaction rewrite re-replicates (the churn cost in §6) |

If any mart hits one of these, Mechanism A is the remaining option regardless of the cost comparison — say so plainly rather than proposing B and discovering it at build time.

### 🔴 Replicate the MART layer, not the whole lake

This is the rule that makes B defensible on cost. The skill already separates `base_*` (raw grain, the bulk of the bytes) from `mart_*` (aggregated, typically 5–10% of total), and the consumption layer reads **only** `mart_*` (`SKILL.md` §4 → "No views — use materialized mart tables").

- Replicate `mart_*` and the single-row KPI mart into `{query_region}`.
- Leave `base_*` in `{aws_region}` only. Nothing downstream reads them.
- A `{query_region}` consumer that genuinely needs base-grain data is a signal to **build another mart**, not to widen replication.

Before quoting a cost, size the mart layer specifically — `SELECT SUM(file_size_in_bytes) FROM "{mart}$files"` per mart. Quote **that** number, never the whole-lake figure.

### 🔴 Replication cost scales with write churn + compaction, not table size

Iceberg `MERGE INTO` rewrites data files, and S3 Tables auto-compaction rewrites them again. **Every rewritten file replicates again.** An append-only daily-batch mart is cheap. A high-churn MERGE-heavy table under aggressive compaction can move materially more than its own size every month.

Estimate from **daily written/rewritten bytes**, not table size. This also makes compaction tuning (via the maintenance API — `reference/iceberg-cdk.md`) a replication-cost lever, not just a performance one.

### 🔴 Residency gate

Replication places a second copy of the data in `{query_region}`. If a regulator or contract forbids that, B is disqualified and must not be presented at all (§2 Step 1). Get an explicit mandate-vs-preference answer; never infer it from the customer's region choice or industry.

### Do NOT use S3 Multi-Region Access Points

No AWS documentation supports MRAP ARNs as an Athena or Glue table `LOCATION`; MRAP control-plane requests are pinned to `us-west-2`; gateway VPC endpoints are unsupported; and an MRAP cannot serve an object from a bucket that does not have it (so CRR is required anyway). Not a shortcut — skip it.

---

## 6. Cost model

> **Both mechanisms pay the same inter-region transfer rate. The only question is how often.**
> - **A** pays it on **every scan**, forever.
> - **B** pays it **once per byte written**, then storage to keep the copy.
>
> Storage is the cheap side: a GB-month costs roughly ¼ of one cross-region scan of that same GB. Athena's per-TB-scanned charge is identical either way and cancels out.
>
> Anchors: Seoul → other-region transfer ≈ **$0.08–0.09/GB**; S3 Tables storage ≈ **$0.025/GB-month**. These are order-of-magnitude anchors — **verify against the current price list at build time**, and note Seoul is one of the higher-priced source regions (~4× US rates).

Because B's one-time transfer is billed at the *same* rate as a single one of A's scans, **B repays itself in under two scans.** Per GB, static table, 12 months:

| Scans/year | A — in place | B — replicate | Winner |
|---|---|---|---|
| 365 (daily) | $32.85 | $0.39 | **B, ~84×** |
| 52 (weekly) | $4.68 | $0.39 | **B, ~12×** |
| 12 (monthly) | $1.08 | $0.39 | **B, ~2.8×** |
| 4 (quarterly) | $0.36 | $0.39 | ~tie |
| 1 (annual) | $0.09 | $0.39 | **A, ~4×** |

So on a static table, **A wins on cost only at ~1–2 scans per year.** Do not present A as "cheaper for infrequent access" at monthly or quarterly cadence — the arithmetic says otherwise.

### The SPICE refresh mode is the single biggest lever on A

Growing mart (~1%/day), daily refresh. **Full** refresh re-reads the whole mart each time; **incremental** reads only the new window:

| Mart size | A — full refresh | A — incremental | B — replicate |
|---|---|---|---|
| 1 GB | $33/yr | **$0.33/yr** | $0.72/yr |
| 10 GB | $329/yr ($27/mo) | **$3.30/yr** | $7/yr ($0.60/mo) |
| 100 GB | $3,285/yr ($274/mo) | **$33/yr** | $72/yr ($6/mo) |

- **Full refresh → B wins by ~45×**, independent of size.
- **Incremental refresh → A wins by ~2×.** Both move the same delta bytes; A simply has no replica storage line. So **A + incremental refresh is cost-optimal AND residency-optimal** — not a compromise.

🔴 **But this skill's default mart pattern forecloses incremental refresh.** Marts are built as CTAS / `writeTo` full rebuilds (`SKILL.md` §4, §7). Quick Sight incremental refresh needs a SQL-based SPICE dataset with a reliable date/timestamp watermark and append-or-MERGE semantics. A full daily rebuild forces full refresh — and the ~45× penalty.

**Therefore: when Mechanism A is chosen, design the marts incrementally from the start** — `MERGE INTO` on a stable key with an `updated_at` watermark, not a full CTAS rebuild — and record the watermark column in `platform.yaml` so the consumption skill can configure incremental refresh. Retrofitting means rewriting the mart jobs. Note also that incremental refresh does not pick up changed or deleted historical rows, so schedule a periodic full refresh (weekly or monthly) and count those scans in the estimate.

### Counting A's scans — SPICE vs Direct Query is the multiplier

A's cost is **per Athena scan**, so what triggers a scan downstream decides everything:

| Downstream mode | Cross-region scans | Effect on A |
|---|---|---|
| **SPICE, daily refresh** | 1/day regardless of viewer count | A is viable |
| **SPICE, hourly refresh** | 24/day | A is ~24× worse |
| **`DIRECT_QUERY`** | **every dashboard interaction, per visual** | A is ruinous — a 20-user dashboard can scan thousands of times/day |

🔴 **On Mechanism A, downstream datasets MUST be SPICE, not `DIRECT_QUERY`** — recorded as `requires_spice: true` in `platform.yaml` so the consumption skill honors it. Note this **conflicts with** the consumption skill's general advice (`region-constraints.md` §3) to use `DIRECT_QUERY` for cross-region datasets to avoid buying SPICE capacity in the resource region. That advice is correct for same-region and **wrong here**: buy the SPICE capacity, it is far cheaper than per-interaction cross-region scans.

### 🔴 The real crossover: bytes REWRITTEN vs bytes SCANNED

Not scan frequency. B's ongoing transfer is write churn (MERGE + compaction rewrites); A's is reads. Whichever is larger loses:

```
bytes rewritten per month > bytes scanned per month  → A
bytes scanned per month   > bytes rewritten per month → B
```

A stable mart serving dashboards is write-light and read-heavy → B by 10×+. A MERGE-heavy, aggressively compacted, rarely queried table → A. Estimate both sides from `platform.yaml` (`schedule`, mart size) plus the Athena `ProcessedBytes` metric already alarmed on in `SKILL.md` §9.

### Scale sanity check — do not over-engineer this

At Data Lab / PoC scale the decision is financially immaterial: a 1–10 GB mart layer is a **$32–321/year** difference, against Quick Enterprise at ~$18/user/month and Glue at $10–20/day. **Below ~10 GB of marts, tell the customer the cost difference is noise** and let them choose on residency and operational simplicity. It becomes a genuine engineering decision at 100 GB+ on full refresh.

**A's figures are a floor, not a ceiling** — a query "can result in more data transferred than the size of the dataset." Partition pruning and the 1 GB workgroup scan cutoff are therefore *cost* controls under A, not just scan controls. Lake Formation cross-region **metadata** traffic is not charged — do not let that read as "cross-region querying is free."

---

## 7. Limitations & gotchas

- **KMS keys are regional — the #1 silent breaker.** If the storage-region bucket is SSE-KMS encrypted, the key lives in `{aws_region}`. Athena in `{query_region}` must call KMS *in the storage region* to decrypt: the query role needs `kms:Decrypt` on the **storage-region key ARN**, and the key policy must allow that principal. A `{query_region}` key cannot decrypt storage-region objects. Use a **KMS multi-Region key** for portability.
- **CSE-KMS encrypted S3 is not supported cross-region.** SSE-S3 and SSE-KMS are.
- **Athena federated (Lambda connector) queries: same-region only.**
- **Redshift Spectrum cannot query Data Catalog tables from another region** — if the customer plans Spectrum in `{query_region}`, Mechanism A will not serve it; only B will.
- **SAML users cannot make cross-region resource-link calls.** Confirm the identity path (IAM Identity Center vs SAML) before committing to A.
- **The `{query_region}` console does not show source-region names** for linked databases/tables — expect confusing UI. To list tables under the link you must create the link first, then "View tables."
- **China regions do not support cross-region queries.**
- **Cross-region AWS Glue traffic may incur additional charges** beyond S3 transfer.
- **Glue/EMR jobs reaching cross-region S3 may need internet or VPC egress** depending on the VPC setup — relevant if anything beyond Athena reads across the boundary.
- **The account must be opted into both regions** for the cross-region read to work at all.

---

## 8. Cross-ACCOUNT as well as cross-region

**Both mechanisms support cross-account, including cross-account *and* cross-region combined** — AWS documents that combination directly. This section is the delta only; everything else in §3–§5 still applies.

> **Scope note.** Cross-account is a **governance** decision, not a topology one (`SKILL.md` §13: "Cross-account sharing → LF cross-account grant + RAM share"). This section exists so you don't tell a customer it's impossible, and so the same-account simplifications above aren't mistaken for universal. A full multi-account data-mesh build is out of scope for this skill.

### Mechanism A cross-account — the RAM + Lake Formation path

The documented workflow, in order (note **which region each step happens in** — this is where teams get stuck):

1. **Producer account, storage region** — share the database/table to the consumer account (Lake Formation grant → AWS RAM invitation).
2. **Consumer account, STORAGE region** — accept the RAM invitation. It must be accepted in the *source* region, not the query region.
3. **Consumer account, storage region** — data lake admin grants `SELECT` on the shared resource to the querying principal.
4. **Consumer account, QUERY region** — create the resource link pointing at the shared resource, and grant `DESCRIBE` on the link.
5. Query the link from the query region via Athena / EMR / Glue ETL.

A three-way variant is also supported: S3 data in account A / region A, the data location registered in a **central** account in region B, and consumers querying from region C.

> 🔴 **Cross-account voids this skill's IAM-only default.** Same-account cross-region needs an identity policy alone (§A4). Cross-account goes through **RAM + Lake Formation permissions** — so LF is no longer optional, and the `get-data-lake-settings` precondition (`SKILL.md` GATE 2) becomes a real branch rather than a formality. If the customer is on LF IAM-only mode today, moving to cross-account means adopting LF grants. Surface that as a governance change, not a config tweak.

Lake Formation supports LF-Tag-based access control, fine-grained (column/row) permissions, and account-level **or** direct-to-IAM-principal sharing on this path. The §7 limitations are unchanged — still no Redshift Spectrum, still **no SAML users**.

### Mechanism B cross-account — destination-side resource policies

> 🔴 **There is NO request/accept handshake. This is the opposite of Mechanism A.** A common wrong assumption is that the source account "requests" replication and the destination "accepts" it (RAM-style). It does not work that way:
> - **The source account PUSHES.** It calls `put-table-replication` naming the destination bucket ARN. That call **succeeds even if the destination has authorized nothing.**
> - **The destination PRE-AUTHORIZES by policy**, not by approving a request. It writes a table-bucket resource policy allowing the source account's replication role.
> - **Order is not enforced, but it matters operationally:** write the destination policy **first**. Configure the source first and replication goes `FAILED` with `"Insufficient permissions given to successfully complete replication."` — which reads like a broken setup rather than a missing step.
> - **Recovery is automatic.** After the root cause is fixed, the service retries on its own: `FAILED` → `PENDING` → `COMPLETED`. Do NOT delete and recreate the replication configuration to "kick" it.
>
> Contrast Mechanism A, which **does** have an accept step (AWS RAM invitation) — see above. Do not describe B to a customer as "we'll send you a share request to approve"; describe it as "you grant our replication role write access to your table bucket, then we start pushing."

Replication destinations may be in another region, another account, or both (up to 5 destinations; 1 rule at launch). Beyond §5:

1. **Source account** — the replication IAM role trusts `replication.s3tables.amazonaws.com` and holds read on the source table ARNs plus `CreateTable`/`CreateNamespace` on the *destination bucket ARN* and `PutTableData`/`UpdateTableMetadataLocation` on `destination-bucket/table/*`.
2. **Destination account** — apply a **table-bucket resource policy** granting the source account's replication role those same actions:
   ```bash
   aws s3tables put-table-bucket-policy \
     --table-bucket-arn arn:aws:s3tables:{query_region}:{dest_account}:bucket/{dest-bucket} \
     --resource-policy file://destination-bucket-policy.json \
     --profile {destination-account}
   ```
3. **If either side is encrypted** — KMS key policies on **both** ends must allow the replication role (`kms:Decrypt`/`GenerateDataKey` on the source key, `Encrypt` too on the destination key), plus `maintenance.s3tables.amazonaws.com` for compaction. Replicas may use a *different* KMS key than the source, which is often the point in a cross-account setup.

**Verify with the status API, not by assumption** — per destination, `COMPLETED` / `PENDING` / `FAILED`:

```bash
aws s3tables get-table-replication-status \
  --table-arn arn:aws:s3tables:{aws_region}:{account}:bucket/{prefix}-table-bucket/table/{mart}
```

`PENDING` means in-flight or new commits queued (compare `lastSuccessfulReplicatedUpdate.metadataLocation` against the source's current metadata location to tell "catching up" from "stuck"). `FAILED` carries a `failureMessage` — usual causes are the missing destination bucket policy, a wrong destination ARN, or a KMS policy gap. Note `get-table-replication` (configuration) and `get-table-replication-status` (state) are **different commands**; the first succeeding tells you nothing about whether data is flowing.

> 🔴 **Add replication-lag verification to GATE 4 under Mechanism B.** Unlike Mechanism A — where a stale read is impossible because the query hits the source — a replica can silently serve **stale** data: dashboards keep working, numbers are just old. Assert `replicationStatus == COMPLETED` (or a `PENDING` whose lag is within tolerance) **before** reconciling row counts, otherwise a count mismatch will be misdiagnosed as a data-quality bug when it is really replication lag.

### Which to choose across accounts

The §2 decision is unchanged in shape — residency mandate vs preference, then scan volume vs write churn — with two thumbs on the scale:

- **A** adds a governance dependency (LF + RAM) but still copies nothing. Right when the producer must retain control and prove data never left.
- **B** adds a second copy in an account you may not control, but the ongoing mechanics are simpler and the destination can re-key with its own KMS key. Right for partner/test-environment sharing.

⚠️ **A second account raises a question a single account doesn't: who pays, and who can delete it?** Cross-account data transfer and the destination storage bill land on different accounts than the source. Settle that before building, and record it in the `ARCHITECTURE.md` residency disclosure alongside what crosses the boundary.

---

## 9. Teardown — query region FIRST, then storage

Reverse of the build order. Deleting the resource link touches **no data** — that is the safety property of Mechanism A, and worth stating to the user before they authorize teardown.

```bash
# --- Query region first ---
# 1. Delete the resource link (metadata only — the storage-region database is untouched)
aws glue delete-database --name {prefix}_db_link --region {query_region}
# 2. Empty + delete the query-region results bucket
aws s3 rm s3://{prefix}-analytics-{query_region} --recursive --region {query_region}
aws s3 rb s3://{prefix}-analytics-{query_region} --region {query_region}
# 3. Destroy the query stack (workgroup, query role, named queries)
cdk destroy QueryStack

# --- Mechanism B only: stop replication before deleting the replica ---
# aws s3tables delete-table-replication --table-bucket-arn <dest-arn> ...   # verify exact CLI shape
# then delete replica tables → namespace → table bucket in {query_region}

# --- Then the storage region, per SKILL.md §11 ---
```

> **Confirm intent first**, as with all teardown (`SKILL.md` GATE 5 / §11). Then verify the storage region is intact: re-run the `SELECT COUNT(*)` from `{aws_region}` and confirm tables and row counts are unchanged. Under Mechanism A that must hold by construction; if it does not, something wrote across the boundary and A6 was violated.

> 🔴 **Cross-account teardown has an extra step that is easy to forget — and it leaves access open.** Deleting your own resources does NOT revoke the other account's access:
> - **Mechanism A:** revoke the Lake Formation grant and delete the AWS RAM share. The consumer account's resource link breaks, but the *share* persists until revoked.
> - **Mechanism B:** remove the destination table-bucket policy (`put-table-bucket-policy` with the statement removed, or `delete-table-bucket-policy`) and stop the replication rule. Otherwise the source account's role retains write access to the destination bucket.
> - **Either:** if the destination account owns the replica, **you cannot delete their data** — confirm who is responsible for deleting it, and say so explicitly rather than reporting teardown as complete.
