# SAP Sources — OData, HANA, and CSV-export Reality

Read this whenever the user says the source is **SAP** (ECC, S/4HANA, BW, HANA, "our ERP"). Glue has **native SAP connectors** — do NOT route SAP to the generic JDBC branch by default, and do NOT assume a CSV export is the only option.

This matters beyond connectivity: **the dirty-data corruptions in `SKILL.md` §4 and `reference/scripts.md` are mostly SAP CSV-export artifacts** (trailing-minus negatives, MATNR leading zeros, mixed date formats). A typed OData or HANA connection avoids several of them at the source. Choosing the right ingress path is a data-quality decision, not just a plumbing one.

---

## 1. Which SAP path? — ask before building

```
How does the customer expose SAP data?

├── SAP OData service available (SAP Gateway / ODP configured)
│     → Glue native SAP OData connector. BEST: typed fields, delta support,
│       no file handling. §2
│
├── SAP HANA database reachable (direct DB access granted)
│     ⚠️ FIRST: does the SAP system actually RUN on HANA? Many ECC
│        landscapes run on Oracle / DB2 / SQL Server — there is no HANA
│        to connect to, so this branch is unavailable. Ask before offering it.
│     ⚠️ ALSO: direct DB access to SAP may carry licensing / support-policy
│        implications at the customer. Flag it for their SAP licensing
│        contact — do not assert it is fine, and do not assert it is blocked.
│     → Glue native SAP HANA connector (Glue 4.0+). Typed, SQL, raw tables. §3
│
├── SAP Datasphere / BW already exports to S3
│     → treat as source_type=s3. Standard §3 S3 branch. Already curated.
│
├── Only CSV/Excel exports available (most common in Data Lab reality)
│     → source_type=s3 + MANDATORY dirty-data handling. §4
│
└── Nothing exposed yet / Basis team unavailable during the engagement
      → §5. Don't block the build — start with a sample export, design for
        the connector later.
```

> 🔴 **The SAP path is a customer-prerequisite question, not a technical preference.** OData needs Basis-team work (activate services, grant authorizations, possibly install Gateway Foundation) that can take **days to weeks** in an enterprise. In a 4-day Data Lab, asking for it on day 1 usually fails. Confirm what *already exists* before designing around it — and if OData is not ready, build on exports now and document the connector as the production path (§5).

### Why "just use OData" is not a safe default

Ask, don't assume — the answer varies by SAP landscape, and the agent cannot infer it:

| Situation | Effect on OData readiness |
|---|---|
| **S/4HANA (modern)** | Best odds. Ships with many OData/CDS services, though the specific ones you need still require activation |
| **ECC (older, still widespread)** | Services often not activated; ODP may need Gateway Foundation configuration |
| **NetWeaver below 7.50 SP02** | **ODP delta is not possible at all.** Non-ODP needs 7.40 SP02+ |
| **Only OData V4 exposed** | Glue supports **V2.0 only** — V2 services must be activated, or use HANA |
| **Basis team is a separate org / outsourced** | Activation is a ticket in someone else's queue, not a conversation |
| **Change-control / regulated environment** | Any SAP-side change needs a change request; weeks, not days |
| **Security policy forbids direct SAP extraction** | Some customers mandate export-only or a middleware layer (SLT, Datasphere, PI/PO) |

The practical read: **assume nothing is activated until the customer confirms it**, and treat a "yes" as needing verification (`aws glue test-connection`) before you design around it. The cost of assuming wrong is asymmetric — assuming exports and finding OData available costs you a small rework; assuming OData and finding it unavailable costs the engagement its build window.

---

### OData vs HANA — they are different LAYERS, not two flavors of the same thing

| | **SAP OData** (§2) | **SAP HANA** (§3) |
|---|---|---|
| Connects at | **Application layer** (SAP Gateway) | **Database layer** (HANA JDBC) |
| You receive | Curated entities / CDS views, SAP business logic applied, business-meaningful field names | Raw ABAP tables (`MSEG`, `MARA`, `BKPF`), cryptic column names, no semantic layer |
| Delta / incremental | **Yes** — ODP `ENABLE_CDC` + `DELTA_TOKEN` | No — watermark column + `MERGE INTO` |
| Customer must provide | Basis work: activate services, grant extraction authorizations | A DB credential + network path |
| Hard precondition | OData **V2.0** services; ODP needs NetWeaver 7.50 SP02+ | **The SAP system must run on HANA** — Oracle/DB2/SQL Server landscapes have none |
| Glue version | — | **4.0+** |

**Decision rule:** ask *"do you have OData services activated for the data we need, or can you give us a HANA database credential?"* — then take whichever they can actually grant. **If both, choose OData**: the semantic layer and native delta are worth more than raw-table breadth, and HANA makes you own the SAP data model yourself. Choose HANA when OData is not activated, the customer can only offer DB access, or the analysis genuinely needs tables OData does not expose.

---

## 2. SAP OData connector (preferred when available)

Glue supports SAP OData **as both source and target**. Only **OData API version 2.0** is supported.

### Supported sources

| Category | Examples |
|---|---|
| **ODP** (Operational Data Provisioning) | BW Extractors (DataSources), CDS Views, SLT |
| **Non-ODP** | CDS View Services, RFC-based Services, custom ABAP Services |

ODP is the one to want — it is what enables **delta extraction** (§2.4).

### 2.1 Customer prerequisites — verify EVERY one before promising OData

These are SAP-side, owned by the customer's Basis team. Treat any gap as a blocker to surface at GATE 1, not something to discover mid-build.

- Catalog service **enabled** for service discovery
- ODP data sources **configured for extraction** in the SAP Gateway
- OData V2.0 catalog service(s) **and** the specific services activated via transaction `/IWFND/MAINT_SERVICE`
- The service must support client-side pagination (`$top`, `$skip`) and the `$count` system query option
- SAP user authorized to **discover services and extract data**
- **For ODP sources:** SAP Gateway Foundation installed locally in the ERP/BW stack or as a hub
  - ERP/BW: SAP NetWeaver AS ABAP **7.50 SP02+**
  - Remote hub: SAP NetWeaver AS ABAP **7.50 SP01+**
- **For non-ODP sources:** SAP NetWeaver stack **7.40 SP02+**
- **For OAuth 2.0:** OAuth enabled on the OData service + an OAuth client registered per SAP docs

### 2.2 Connection

Create in Glue Studio → connection type **SAP OData**. Required values (get ALL of these from the customer up front — a missing client number or service path blocks the build):

| Field | Example |
|---|---|
| Application host URL | `https://sap.example.com` |
| Application service path (= catalog service path) | `/sap/opu/odata/iwfnd/catalogservice;v=2` |
| Client number `[001-999]` | `010` |
| Port number | `443` |
| Logon language | `EN` |
| IAM role Glue assumes | — |

Auth: **Basic Authentication** or **OAuth 2.0**. Note SAP OData supports only the `AUTHORIZATION_CODE` grant type — which is interactive, so the connection is created through the console with a redirect to SAP login. **Plan for that**: it is not fully scriptable in CDK the way a Secrets Manager JDBC credential is.

> Glue accepts only the **catalog** service path here, not a specific object path. The specific entity goes in the job's `ENTITY_NAME`.

### 2.3 Reading in the Glue job

```python
sapodata_df = glueContext.create_dynamic_frame.from_options(
    connection_type="SAPOData",
    connection_options={
        "connectionName": "{prefix}-sap-odata-connection",
        "ENTITY_NAME": "/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder",
        "API_VERSION": "2.0",
        # Push filtering/projection DOWN to SAP — do not read everything and filter in Spark
        "SELECTED_FIELDS": ["SalesOrder", "SalesOrderDate", "TotalNetAmount"],
        "FILTER_PREDICATE": 'SalesOrderDate >= "2024-01-01T00:00:00.000Z"',
        # Parallelism
        "PARTITION_FIELD": "SalesOrderDate",
        "LOWER_BOUND": "2024-01-01T00:00:00.000Z",
        "UPPER_BOUND": "2025-01-01T00:00:00.000Z",
        "NUM_PARTITIONS": 4,
        "PAGE_SIZE": 50000,
    },
    transformation_ctx="sap_sales_order",
)
```

Then type/clean and `writeTo` the Iceberg table exactly as in `SKILL.md` §4 Case B. **Everything downstream of the read is unchanged** — this is a new ingress, not a new architecture.

Key options (full list in the AWS docs — verify at build time):

| Option | Notes |
|---|---|
| `ENTITY_NAME` | **Required.** The OData entity/EntitySet path |
| `SELECTED_FIELDS` | Default empty = `SELECT *`. Always narrow it — SAP entities are very wide |
| `FILTER_PREDICATE` | Spark SQL format. Pushes the filter to SAP |
| `QUERY` | Full Spark SQL alternative to the above |
| `PARTITION_FIELD` / `LOWER_BOUND` / `UPPER_BOUND` / `NUM_PARTITIONS` | Parallel read. `NUM_PARTITIONS` default **1** — a single-threaded read of a large entity is a common "why is this so slow" cause |
| `PAGE_SIZE` | Default **50,000**; max **500,000** (values above are silently clamped to the max) |
| `ENABLE_CDC` | `True`/`False` — run the job with change tracking |
| `DELTA_TOKEN` | Incremental pull from a supplied delta token, e.g. `D20241107043437_000463000` |

### 2.4 Delta / incremental extraction

With ODP sources, `ENABLE_CDC` + `DELTA_TOKEN` give SAP-native change tracking: the first run does a full pull and returns a delta token; subsequent runs pass that token to fetch only changes. Glue also documents a **SAP OData state management script** for persisting token state between runs.

> ⚠️ **Verify the delta mechanics against current AWS docs before building on them** — the exact token-persistence contract and its failure modes (expired/invalidated token, ODP subscription reset) are the part most likely to have changed, and a stale delta token generally means a **silent partial load**, not an error. Pair delta extraction with the GATE 4 source reconciliation (`SKILL.md` §8) rather than trusting the token.
>
> **This is the one place SAP CDC is in scope for this skill.** `SKILL.md` §3 routes generic CDC out of scope (→ DMS), and that still holds for database-level CDC. ODP delta is different: it is a *connector option on a batch Glue job*, not a streaming architecture — so it stays inside the batch scope of §12. Do not bolt DMS onto an OData source.

On the Iceberg path, land deltas with `MERGE INTO` (§4 Case C) keyed on the SAP business key — **normalized** per §4/`scripts.md` (MATNR-style leading zeros bite here too, and a delta merge on an unnormalized key silently inserts duplicates instead of updating).

---

## 3. SAP HANA connector

**Glue has built-in support for SAP HANA** (Glue **4.0 and later**) — a native connection type, not a Marketplace connector. Reads and writes are supported, and you define what to read with a **SQL query**. Under the hood it is HANA JDBC with credentials in Secrets Manager, which is why it is usually *easier to obtain* than OData: it needs a database credential and network path, not Basis configuration.

### Setup

1. Store the HANA credentials in a **Secrets Manager** secret.
2. Create a Glue **SAP HANA** connection referencing that secret. Required JDBC URL parameter: `databaseName` (the default database to connect to); plus `secretName`.
3. Grant the Glue job's IAM role permission to read `secretName`.
4. In the job config, provide the connection as an **Additional network connection**.

### Reading

```python
hana_df = glueContext.create_dynamic_frame.from_options(
    connection_type="saphana",
    connection_options={
        "connectionName": "{prefix}-sap-hana-connection",
        # Push projection AND filtering into HANA — do not SELECT * a fact table
        "query": """
            SELECT MATNR, WERKS, BUDAT, MENGE, DMBTR
            FROM SAPABAP1.MSEG
            WHERE BUDAT >= '20240101'
        """,
    },
    transformation_ctx="sap_hana_mseg",
)
```

Then type/clean and `writeTo` Iceberg per `SKILL.md` §4 Case B — unchanged.

### Notes and constraints

- **Glue 4.0+ only.** On an older Glue version, upgrade rather than working around it (fatal rule 5).
- **Confirm SAP actually runs on HANA before proposing this.** A large share of ECC landscapes run on Oracle, IBM Db2, or SQL Server. If so, there is no HANA endpoint — the options collapse to OData or exports. Ask early; it is the fastest way to eliminate a branch.
- **Direct database access may have licensing / support-policy implications** at the customer. Raise it as a question for their SAP licensing contact. Do not assert that it is permitted, and do not assert that it is forbidden — you are not the authority on their SAP agreement.
- **No ODP delta.** Do incremental the standard way: a watermark column in the `query` + `MERGE INTO` (§4 Case C). This is the main functional gap vs OData.
- **Writes fail if the target table already has data** — Glue raises an error rather than appending. Irrelevant for this skill (SAP is a source here), but do not offer HANA as a write-back target without accounting for it.
- **Network connectivity is on you.** If HANA is in a VPC, configure VPC/subnet/security group so the Glue job reaches it without traversing the public internet → `reference/vpc-connectivity.md`. This is the common enterprise case; SAP is rarely internet-facing.
- **Raw ABAP table access is a double-edged win.** HANA exposes underlying tables (`MSEG`, `MARA`, `BKPF`…) that OData's curated entities may not. That is powerful and it means *you* now own the SAP data model — cryptic column names, client (`MANDT`) filtering, and no semantic layer. Budget analysis time, and confirm which SAP client to filter on.

Prefer **OData** when ODP delta or the SAP semantic layer (CDS views, business-meaningful entity names) matters. Prefer **HANA** when the customer can only grant DB access, or the analysis needs raw tables OData does not expose.

---

## 4. CSV / Excel exports (the common Data Lab reality)

Treat as `source_type=s3` and follow the standard §3 S3 branch — **plus mandatory dirty-data handling**, because these exports carry SAP-specific value-level corruption that passes every structural check:

| Corruption | Why SAP does it |
|---|---|
| **Trailing-minus negatives** (`150.000-` = −150) | SAP's default numeric display format. Often >50% of rows in a costs export |
| **MATNR / key leading zeros** (`000000000010010015` vs `10010015`) | SAP internal vs external key formatting differs per export path |
| **Mixed date formats**, incl. literal `'NULL'` | Locale/transaction-dependent formatting |
| **EUC-KR vs UTF-8 per source system** | Korean SAP GUI exports vs modern exports |
| **NFD filenames** | Whoever exported it used a Mac |
| **Subtotal / total rows + merged cells** | Exports taken from an ALV grid rather than a table dump |

All six have worked fixes in `reference/scripts.md` → "Dirty real-world data handling". **Do not skip source reconciliation (GATE 4)** on an SAP export — this is precisely the population where "validation passed ≠ correct answer."

> Numeric and date corruption largely **disappears** on the OData/HANA paths, because fields arrive typed rather than as display-formatted text. Key-format and cross-source-bridge issues can persist. If the customer is choosing between paths and data quality matters, that is a real argument for the connector.

---

## 5. When SAP access is not ready (do NOT block the build)

Common in a 4-day Data Lab: the Basis team cannot activate OData or grant HANA access inside the engagement window. Do not stall, and do not quietly ship an export-only design as if it were the target architecture.

1. **Build now** on a representative CSV/Excel export (§4), with full dirty-data handling.
2. **Design the ingest job so the source swaps cleanly** — keep the typed transform and the Iceberg `writeTo` intact; only the `create_dynamic_frame` read changes. One job per logical table (`SKILL.md` §3) makes this a per-table swap.
3. **Record the intended production path** in `ARCHITECTURE.md` Decisions and `platform.yaml`:
   ```yaml
   etl:
     "{prefix}-ingest-sap-sales":
       source: "sap_csv_export"        # sap_odata | sap_hana | sap_csv_export
       target_source: "sap_odata"      # the production path once SAP access lands
       sap_prereqs_pending: ["activate OData service via /IWFND/MAINT_SERVICE", "grant extraction authorization"]
   ```
4. **Hand the customer the §2.1 prerequisite list** as an explicit action item with owners. That list is the deliverable when the connector cannot be built during the engagement.

---

## 6. Gotchas

- **`NUM_PARTITIONS` defaults to 1** — a large entity reads single-threaded. Set `PARTITION_FIELD` + bounds for anything sizable.
- **`PAGE_SIZE` over 500,000 is silently clamped**, not rejected.
- **Only OData V2.0** is supported. A customer with only V4 services exposed needs V2 services activated, or use HANA instead.
- **OAuth 2.0 is `AUTHORIZATION_CODE` only** — interactive login, so connection creation is a console step, not a clean CDK deploy. Factor it into the deploy sequence and note it as a manual post-deploy step in `README.md`.
- **SAP entities are extremely wide.** Always set `SELECTED_FIELDS`; a `SELECT *` on an SAP entity wastes read time and Athena scan budget downstream.
- **Push filters down** via `FILTER_PREDICATE`, not in Spark — the point is to not transfer the rows at all.
- **Delta tokens fail silently.** An expired/invalid token yields a partial load, not an error. Reconcile counts every run (GATE 4).
- **SAP is rarely internet-reachable** — expect to need a VPC-configured Glue Connection (`reference/vpc-connectivity.md`) plus firewall changes on the customer side. Test with `aws glue test-connection` before building the job.
- **Business-key normalization still applies** on the connector paths — normalize keys on BOTH sides before any join or `MERGE INTO`.
