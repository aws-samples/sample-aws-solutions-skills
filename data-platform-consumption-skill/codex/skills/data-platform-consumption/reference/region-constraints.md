# Region Constraints, SPICE Capacity & Identity Region

Quick Sight dashboards are broadly available. **Amazon Quick's agentic AI features — chat agents, Dataset Q&A, and Topics — ship in a smaller, changing footprint.** This file is the decision flow when `{aws_region}` does not support those features, plus the two per-region gotchas (SPICE capacity, identity region) that bite cross-region setups.

---

## 1. Identity region vs resource region

> **⚠️ `describe-account-settings` (and several account-level QuickSight calls) must be called from the identity region** — where QuickSight was first enabled — NOT necessarily where your data/resources are deployed. Called from the wrong region you get: `Operation is being called from endpoint X, but your identity region is Y.` When discovering account settings, **try `us-east-1` first** — the most common identity region for Korean accounts that enabled QuickSight early — then the resource region:
> ```bash
> aws quicksight describe-account-settings --aws-account-id {account_id} --region us-east-1
> ```
> SPICE capacity, by contrast, is per-**resource**-region (§3) — don't confuse the two.

---

## 2. Region availability gate (do not skip)

Before generating any CDK or running setup, confirm whether `{aws_region}` supports the chat / Topics features:

```bash
aws quicksight list-topics --aws-account-id $(aws sts get-caller-identity --query Account --output text) --region {aws_region}
```

- **Succeeds (or returns an empty list):** `{aws_region}` supports Topics. Proceed in-region.
- **Fails with "not supported in this region" (or similar):** chat agents and Dataset Q&A are not available in `{aws_region}`. **Stop and walk the user through the Region Selection Flow (§4)** before generating any resources.

The probe is a **hint, not the source of truth**. If the user states a region is unsupported (e.g., direct experience or internal AWS guidance), **trust the user** — feature availability and API surface sometimes diverge during regional rollout.

> **Amazon Quick's regional availability evolves.** Always verify against the Amazon Quick pricing page (https://aws.amazon.com/quick/pricing/) or run the `list-topics` probe before proceeding.

### Known region support (as of 2026-05) — snapshot, not authoritative

| Region | Quick Sight dashboards | Amazon Quick chat / Dataset Q&A / Topics |
|---|---|---|
| `us-east-1` (N. Virginia) | ✅ | ✅ |
| `us-west-2` (Oregon) | ✅ | ✅ |
| `eu-west-1` (Ireland) | ✅ | ✅ |
| `ap-northeast-2` (Seoul) | ✅ | ❌ — dashboards only, no chat agents |
| _other regions_ | verify at deploy time | verify at deploy time |

```bash
# Re-confirm at deploy time (CLI namespace is still 'quicksight')
aws quicksight list-namespaces --aws-account-id {account_id} --region {aws_region} 2>&1 | head
aws quicksight list-topics --aws-account-id {account_id} --region {aws_region}
# If list-topics errors with "not supported in this region", chat / Topics are unavailable.
```

> **Windows + Korean:** On Windows, set `AWS_CLI_FILE_ENCODING=UTF-8` before running CLI commands that pass `file://` JSON containing Korean text (dashboard definitions, topic JSON, dataset names, persona descriptions). Without it: `text contents could not be decoded` errors. (`set AWS_CLI_FILE_ENCODING=UTF-8` in cmd, or `$env:AWS_CLI_FILE_ENCODING="UTF-8"` in PowerShell.)

---

## 3. SPICE capacity is per-region

Default SPICE capacity per account is 10 GB. Estimate per dataset:
```
SPICE size ≈ raw_rows × avg_row_size × compression_factor (~0.3 for typical data)
```

If projected usage exceeds default, purchase additional SPICE capacity in the target region. There is no single "set capacity to N GB" CLI call — buy capacity through the console (Manage QuickSight → SPICE Capacity), or enable automatic top-ups:

```bash
# Enable auto-purchase of SPICE capacity in the target region (separate cost line item)
aws quicksight update-spice-capacity-configuration \
  --aws-account-id {account_id} \
  --purchase-mode AUTO_PURCHASE \
  --region {aws_region}
```

(`update-account-settings` configures the account's default namespace and notification email — it does NOT provision SPICE capacity.)

> ⚠ **SPICE quota is per-region.** The default 10 GB lives in the Quick Sight **identity region only**. If you create datasets in a different region (common: identity in `us-east-1`, data + Quick Sight resources in `ap-northeast-2`), the resource region starts at **0 GB SPICE** and dataset creation fails with a capacity error.
>
> Two options:
> - **Purchase SPICE capacity in the resource region** — via the console (Manage QuickSight → SPICE Capacity → switch to the resource region first), or `aws quicksight update-spice-capacity-configuration --aws-account-id {account_id} --purchase-mode AUTO_PURCHASE --region {resource_region}`, OR
> - **Use `DIRECT_QUERY` import mode** for cross-region datasets: set `importMode: 'DIRECT_QUERY'` on `CfnDataSet`. Trades sub-second SPICE rendering for live Athena queries, but avoids the capacity purchase.
>
> Verify before provisioning datasets via the console (Manage QuickSight → SPICE Capacity, with the **resource region** selected) — `describe-account-settings` does not report capacity. A dataset/ingestion failing with an insufficient-SPICE/capacity error in the resource region is the signal to purchase capacity (or enable `AUTO_PURCHASE`) or switch that dataset to `DIRECT_QUERY`.

> **Checking SPICE in preconditions:** `describe-account-settings` does NOT return capacity — it returns edition/namespace/email. The first SPICE dataset creation in a fresh region fails if capacity is 0; the most reliable check is the console (Manage QuickSight → SPICE Capacity, with the resource region selected). If scripted, attempt a small dataset/ingestion and treat a "capacity" / "insufficient SPICE" error as the signal to provision.

---

## 4. Region selection flow (when the region doesn't support chat)

```
Step 1. Does {aws_region} support Amazon Quick's agentic AI features
        (chat agents, Dataset Q&A, Topics)?

        ├── YES → Deploy everything in {aws_region}. Proceed with the full build.
        │
        └── NO  → Present the user with two options. Do not pick for them.
```

### Step 2 — present these options to the user

> **Option A — Dashboards only, in home region**
> - Deploy Quick Sight in `{aws_region}` for BI dashboards only
> - **No chat agent / Dataset Q&A**
> - Trade-off: simplest, lowest latency, no NL analytics

> **Option B — Full stack in a supported region**
> - Deploy Amazon Quick (Quick Sight + chat agents + Topics) in `us-east-1` (or another supported region the customer prefers)
> - Data stays in `{aws_region}` (S3, Glue, Athena unchanged)
> - Quick Sight queries the home-region Athena workgroup cross-region
> - Trade-off: full features; dashboard users connect to the supported region's console; cross-region Athena adds latency on direct-query datasets (less of an issue with SPICE)

### Step 3 — let the user choose, then adjust the rest of the build

| If user picks | Adjust |
|---|---|
| A (dashboards only) | Skip the chat agent setup + Topics (`chat-agent.md`). Skip the chat-agent test step in the post-deploy bootstrap and the chat-agent block in the validation checklist. |
| B (full stack in supported region) | All deploy commands run with `--region us-east-1` (or chosen region). Add cross-region Athena permissions to `{prefix}-quicksight-role`. Document data-residency: data stays in `{aws_region}`; query metadata and chat transcripts transit the BI region. |

> **Migration note:** If the customer starts on Option B and `{aws_region}` later gains chat support, consolidating to in-region is a CDK redeploy plus a one-time Topic export/import (`describe-topic` → `create-topic` in the new region) and a SPICE dataset re-ingest.

---

## 5. Cross-region Quick Sight (data in Seoul, Quick Sight in us-east-1)

**When to use:** Quick Sight chat agent / Topics / Dataset Q&A only available in us-east-1 (or another supported region), but data is in ap-northeast-2 (Seoul).

**Architecture:**
```
S3 data (ap-northeast-2) — stays in Seoul, no copy needed
    ↑ cross-region S3 read (Athena feature)
Glue Catalog table (us-east-1) — LOCATION points to Seoul S3
Athena workgroup (us-east-1) — queries execute here
Quick Sight (us-east-1) → SPICE (daily refresh) → Dashboard + Chat
```

**Setup steps:**

1. **Create Glue Database in us-east-1:**
```bash
aws glue create-database --database-input '{"Name":"{prefix}_db"}' --region us-east-1
```

2. **Recreate table definitions in us-east-1 (LOCATION → Seoul S3):**

Export DDL from Seoul:
```bash
aws athena start-query-execution \
  --work-group {prefix}-workgroup \
  --query-string "SHOW CREATE TABLE {prefix}_db.{table}" \
  --region ap-northeast-2
```

Run the same DDL in us-east-1 (LOCATION stays the same — points to Seoul S3):
```bash
aws athena start-query-execution \
  --work-group {prefix}-workgroup \
  --query-string "{DDL from above}" \
  --region us-east-1
```

> **⚠️ `SHOW CREATE TABLE` preserves `LOCATION` and `PARTITIONED BY`, but NOT the partition values.** The generated DDL includes the `LOCATION 's3://...'` clause and the partition *column* definitions, so it replays cleanly — but it does **not** emit the actual partition metadata. For a **partitioned** table the replayed table returns **zero rows** until you reload partitions in us-east-1:
> ```bash
> # Hive-style s3 layout (key=value/): reload all partitions
> aws athena start-query-execution \
>   --work-group {prefix}-workgroup \
>   --query-string "MSCK REPAIR TABLE {prefix}_db.{table}" \
>   --region us-east-1
> # Non-Hive layout: ALTER TABLE ... ADD PARTITION (...) LOCATION 's3://...' for each
> ```
> (If the table was created via the Glue `CreateTable` API without a `TableType`, `SHOW CREATE TABLE` can fail — set `TableType` on the source table first.)

3. **Create Athena workgroup in us-east-1:**
```bash
aws athena create-work-group \
  --name {prefix}-workgroup \
  --configuration '{"ResultConfiguration":{"OutputLocation":"s3://{prefix}-analytics-us-east-1/"},"BytesScannedCutoffPerQuery":1000000000}' \
  --region us-east-1
```

4. **Create results bucket in us-east-1:**
```bash
aws s3 mb s3://{prefix}-analytics-us-east-1 --region us-east-1
```

5. **Quick Sight setup in us-east-1 — proceed normally with the consumption skill**

**Cost implications:**
- S3 cross-region data transfer **OUT from Seoul (ap-northeast-2) → us-east-1: $0.08/GB** per Athena query scan (verified against the AWS price list, 2026). **Not** $0.02/GB — that is the rate *out of US regions*; Seoul is one of the higher-priced source regions at 4× the US rate. A single scan can move more than the dataset size (no partition pruning, wide SELECTs), so budget on scanned bytes, not table size.
- Mitigation: Use SPICE (daily refresh) → only 1 cross-region read per day
- Once in SPICE, all dashboard interactions are local (no cross-region)

**⚠️ Limitations:**
- `AthenaParameters` has NO region field — Quick Sight ALWAYS uses Athena in its own region (the only members are `WorkGroup`, `RoleArn`/`ConsumerAccountRoleArn`, `IdentityCenterConfiguration`). The workgroup, Glue catalog, and results bucket must all live in us-east-1; only the *S3 data layer* reaches Seoul.
- Athena query-**results** `OutputLocation` bucket must be in the **same region as the workgroup** (us-east-1). Only the source-data bucket is remote.
- S3 Tables native connector: same-region ONLY (no cross-region)
- Federated queries (Lambda connectors): same-region ONLY
- CSE-KMS encrypted S3: NOT supported cross-region (SSE-S3 and SSE-KMS *are* supported cross-region — see KMS gotcha below)

**⚠️ Cross-region permission gotchas (easy to miss — each fails closed):**
- **KMS keys are regional — the #1 silent breaker.** If the Seoul bucket is SSE-KMS encrypted, the key lives in `ap-northeast-2`. Athena running in us-east-1 must call KMS *in Seoul* to decrypt: the Quick Sight Athena role (default `aws-quicksight-s3-consumers-role-v0`) needs `kms:Decrypt` on the **Seoul key ARN** (`arn:aws:kms:ap-northeast-2:...`) AND the key policy must allow that principal. A us-east-1 key cannot decrypt Seoul-encrypted objects. For portability, use a **KMS multi-Region key** (same key material replicated to us-east-1).
- **IAM S3 perms.** The Quick Sight Athena service role and the Athena query-execution identity both need S3 *read* on the Seoul source bucket and *write* on the us-east-1 results bucket. (S3 ARNs are region-agnostic, so the policy *shape* is unchanged — the gotcha is remembering to grant it and, for cross-account, having the bucket owner grant access.) Grant via Manage Quick Sight → Security & permissions.
- **Account must be opted into the data region** (`ap-northeast-2`) for the cross-region read to work.
- **Glue Data Catalog is read in Athena's own region** (us-east-1). Either replay DDL into a us-east-1 catalog (above) or use Lake Formation cross-region resource links (below) — Athena does not read a remote-region catalog directly.

**⚠️ S3 Tables (Iceberg) special case:**
If Seoul data is in managed S3 Tables (not plain S3):
- Cross-region LOCATION trick does NOT work. S3 Tables aren't queried via an `s3://` LOCATION path — they're accessed through the `s3tablescatalog` Glue catalog / Iceberg REST endpoint, and the integration registers table buckets **per-region**, so the native connector is effectively same-region only.
- Options: (a) Deploy QS in Seoul, (b) **Lake Formation cross-region resource links**, (c) Replicate data to us-east-1.

  **How (b) actually works** (terminology, so the team can implement it): the source database/table stays in Seoul; in us-east-1 you create a **resource link** — a Glue database/table object whose `TargetDatabase`/`TargetTable` points back at the Seoul source (the `TargetDatabase` structure carries a `Region` field). For cross-*account*, the source first shares via **AWS RAM**, accepted in the source region. Then grant Lake Formation `DESCRIBE` on the link + `SELECT` on the underlying resource, and query the link from us-east-1 (works with Athena, EMR, Glue ETL). No data or metadata is copied. For S3 data not registered with Lake Formation, access falls back to IAM S3/Glue permissions rather than LF grants.

**Simplest recommendation:** If chat agent isn't needed, deploy Quick Sight in ap-northeast-2 (same region as data) — avoids all cross-region complexity.
