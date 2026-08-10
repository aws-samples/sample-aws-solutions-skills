# Eval — Split-Region Scenario (Seoul data, Tokyo catalog + query)

A black-box checklist verifying the `data-platform-pipeline` skill handles a **split-region catalog + query layer** (follow-up #8). The evaluator does not inspect the skill's internals and judges solely on **user input → expected outputs**.

Reference: `reference/cross-region-query.md`. Gates: `SKILL.md` → Validation Gates.

---

## User input (simulated prompt)

```
우리 회사 ERP 데이터로 데이터 레이크를 만들고 싶습니다. 데이터는 서울 리전에
있어야 합니다 — 규정 때문입니다. 그런데 Amazon Quick의 AI 채팅 기능을 쓰고
싶은데 서울에서는 안 된다고 들었습니다. S3에 CSV 파일이 있고, 월별 불량률
추이와 공급업체별 불량 Top 5를 보고 싶습니다. prefix는 acme로 해주세요.
```

(Korean: data must stay in Seoul for regulatory reasons; wants Amazon Quick's AI chat, which is unavailable in Seoul; S3 CSV source; two business questions; prefix `acme`.)

Expected: the skill answers **in Korean**, recognizes this as the split-region case, reaches **GATE 1** with the residency acknowledgements, and — because the user said "규정 때문" (regulatory) — **disqualifies replication** rather than presenting it as an equal option.

---

## Expected output checklist

### A. Language & framing
- [ ] Every prose response is in **Korean** (`SKILL.md` Language rule); code, CDK, and CLI output remain English
- [ ] Does **not** treat "Amazon Quick unavailable in Seoul" as a blocker or propose abandoning Seoul

### B. Discovery → #8 detection
- [ ] Follow-up **#8 is asked** (or auto-answered **yes** from the prompt, then confirmed) — the split-region need is recognized, not missed
- [ ] `aws_region: ap-northeast-2` and `query_region` collected (Tokyo `ap-northeast-1` or `us-east-1` — either is acceptable if justified by Quick's supported regions)
- [ ] Storage pattern defaults to **Iceberg / S3 Tables**; the skill does **not** switch to Hive merely because the setup is cross-region
- [ ] `reference/cross-region-query.md` is read **before** any code generation

### C. 🛑 GATE 1 — residency acknowledgements (most important)
- [ ] The **mandate vs preference** question is asked explicitly, in those terms — not inferred from "규정 때문" alone, and not skipped
- [ ] Given a **mandate**, **Mechanism B (replication) is DISQUALIFIED and not offered** as a co-equal choice
- [ ] The **zero-egress follow-up** is asked: does the mandate permit aggregated result rows to transit and be cached in `{query_region}`?
- [ ] The cross-boundary flow is stated **accurately**: data files stay in Seoul, but **aggregated result rows transit and are cached in SPICE**, plus query metadata / chat transcripts. The skill does **NOT** claim "no data leaves Korea"
- [ ] If the user then says the mandate is zero-egress, the skill **STOPS** and recommends dashboards in Seoul without the agentic features — it does not build a split-region stack anyway
- [ ] Build does not proceed until the user confirms

### D. Mechanism A build correctness
- [ ] A **Glue resource link** is created in `{query_region}` — NOT DDL replay, NOT a cross-region `LOCATION` against the S3 Tables bucket
- [ ] `create-database` is run with `--region {query_region}` while `TargetDatabase.Region` is `ap-northeast-2` (the **target**) — the two are not reversed
- [ ] `TargetDatabase.CatalogId` uses the federated form `<account>:s3tablescatalog/acme-table-bucket`, not a bare account ID
- [ ] The resource-link database input carries **no `Description`** key
- [ ] Athena workgroup is created in `{query_region}`, with its results `OutputLocation` bucket **also in `{query_region}`** (`acme-analytics-{query_region}`) — never pointed at a Seoul bucket
- [ ] Workgroup result encryption is **SSE-S3**, not KMS (unchanged from §7)
- [ ] IAM: identity-based policy only — storage-region `s3tables:Get*`/`List*` on the table-bucket ARNs, storage-region `glue:Get*` on `catalog/s3tablescatalog/*`, query-region `glue:Get*` for the link. **No** S3 bucket policy, Glue catalog resource policy, or KMS key policy is added (same account)
- [ ] `s3tables:*` is scoped to the specific table-bucket ARN, never `*`
- [ ] Lake Formation is checked in **both** regions; with IAM-only mode, no LF grants are added
- [ ] **Marts are written by the Glue job in Seoul** — no CTAS is run in `{query_region}` (that would copy data and break residency)
- [ ] No `crossRegionReferences: true` / SSM-backed cross-region CFN references between the storage and query stacks

### E. Consumption coupling
- [ ] `platform.yaml` carries `query_region`, `cross_region_mechanism: resource_link`, and **`residency: mandate`**
- [ ] `consumption.requires_spice: true` is set — so the consumption skill cannot choose `DIRECT_QUERY`
- [ ] `ARCHITECTURE.md` contains a **Data Residency Disclosure** naming what crosses the boundary and who approved it
- [ ] The `ARCHITECTURE.md` Decisions row for the resource link has "Do NOT change to: replication (copies data out of the region)"
- [ ] The skill notes that the query-region reference form is `"AwsDataCatalog"."acme_db_link"."{table}"`, not the federated `s3tablescatalog/...` form

### F. ⛔ GATE 4 — cross-region reconciliation
- [ ] A `SELECT COUNT(*)` is run from `{query_region}` **through the link** and asserted **equal** to the same count in `ap-northeast-2`
- [ ] Source-vs-base row-count reconciliation (§8) is also run — cross-region success does not substitute for it
- [ ] Both marts answering the two business questions exist and return rows

### G. Cost honesty
- [ ] The skill mentions cross-region transfer is billed **per scan** under Mechanism A, and that SPICE (not `DIRECT_QUERY`) is required
- [ ] If mart size is small (Data Lab scale), the skill says the cost difference is immaterial rather than over-engineering the estimate
- [ ] Any dollar figures carry a "verify against the current price list" caveat

---

## Pass criteria (PASS conditions)

All of A–G satisfied, plus these decisive items:

1. **GATE 1 fires with both acknowledgements**, and a stated regulatory mandate **removes replication from the menu** (C).
2. **The residency claim is accurate** — result rows are disclosed as crossing the boundary; the skill never says "no data leaves Korea" (C).
3. **The resource link is built correctly** — right target region, federated `CatalogId`, no `Description`, local results bucket (D).
4. **Identity-based IAM only**, scoped to the table-bucket ARN (D).
5. **Cross-region row counts match** the storage region (F).
6. `requires_spice: true` and `residency: mandate` are recorded for downstream consumption (E).

---

## Addendum — C360 / multi-source checks (verified live, 2026-08)

If the scenario's downstream is C360 rather than BI, these additional items apply:

- [ ] **VPC endpoints created BEFORE the first Glue run** when any source is in a VPC: `s3` + `dynamodb` (gateway), `s3tables` + `glue` + `secretsmanager` (interface). Missing `s3tables` produces a job that reads every source then fails on the Iceberg write.
- [ ] **Name order asked per source**, not inferred — `family_first` / `given_first` / `given_only`. The skill must NOT guess from the string.
- [ ] Single-token Hangul given names (`민호`) are **not** split into `호`/`민`.
- [ ] Comma form (`Kim, Min-Ho`) parsed as family/given regardless of the source's declared order.
- [ ] Phone country code canonicalized: `+82 10-…` and `010-…` produce the same digits.
- [ ] `platform.yaml` records `er_apply_normalization: false` when names contain Hangul.
- [ ] Aurora/RDS engine version **probed**, not assumed.
- [ ] Channel variants left **unmerged** — one row per (customer × source).

## Failure signals (FAIL)

- Missing follow-up #8 entirely and building single-region in Seoul, silently dropping the Quick requirement.
- Presenting replication as a co-equal option after the user stated a regulatory mandate — or picking a mechanism **for** the user without asking.
- Claiming Mechanism A means "no data leaves the region" / "zero egress."
- Attempting a cross-region `LOCATION` or `metadata_location` against the S3 Tables bucket, or DDL replay, instead of a resource link.
- Reversing `--region` and `TargetDatabase.Region`; using a bare `CatalogId`; including `Description` on the link.
- Putting the Athena results bucket in Seoul while the workgroup is in the query region.
- Adding bucket/catalog/KMS resource policies for a same-account split.
- Running a mart CTAS in the query region.
- Falling back to Hive because the build is cross-region.
- Choosing `DIRECT_QUERY` for the downstream dataset, or omitting `requires_spice`.
- Declaring success without comparing cross-region row counts.
- Tearing down the storage region before the query region.
