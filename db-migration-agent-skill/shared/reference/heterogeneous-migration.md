# All-Engine & Heterogeneous Migration Reference

> **Scope** — every engine RDS/Aurora supports; **heterogeneous migrations** (Oracle/SQL Server → Aurora, engine family changes); and the **Korean-market source engines** (Tibero, CUBRID, Altibase) that have **no native AWS migration tooling**. For schema/code conversion, prefer chaining the official `dms-schema-conversion` skill ([mcp-and-tooling.md](mcp-and-tooling.md) §Chaining) and use this document for the decision framing, target selection, and the manual-conversion long tail.

> **Companion documents**: [rds-aurora-limitations.md](rds-aurora-limitations.md), [aws-official-migration-methods.md](aws-official-migration-methods.md), [third-party-db-security.md](third-party-db-security.md), [regulatory-compliance.md](regulatory-compliance.md).

---

## 1. RDS / Aurora Engine Coverage

**Amazon RDS** supports **8 engines** (incl. Aurora):

| Engine | Notes |
|--------|-------|
| **PostgreSQL** | Versions ~13–17 GA; 11–12 via Extended Support. |
| **MySQL** | 5.7 / 8.0+ (version & instance-class dependent). |
| **MariaDB** | 10.x / 11.x. |
| **SQL Server** | Express / Web / Standard / Enterprise (License Included or BYOM). |
| **Oracle** | SE2 (LI or BYOL); EE (BYOL only). |
| **Db2** | Most recent addition to the RDS family. |
| **Aurora MySQL-Compatible** | ~5× MySQL throughput, storage to 128 TiB (verify current limit), up to 15 replicas, Global Database. |
| **Aurora PostgreSQL-Compatible** | ~3× PostgreSQL throughput; supports **Babelfish** (T-SQL/TDS). |

---

## 2. Recommended Migration Target per Source Engine

| Source | Recommended AWS target | Path / tooling |
|--------|------------------------|----------------|
| **Oracle** | Aurora PostgreSQL (cost / lock-in exit) **or** RDS for Oracle (lift-and-shift) | Heterogeneous: SCT or DMS Schema Conversion + DMS (CDC). Homogeneous: DMS or Data Pump/RMAN to RDS Oracle |
| **MS SQL Server** | Aurora PostgreSQL **via Babelfish** (minimal app change) **or** RDS for SQL Server | Babelfish + DMS; or SCT/DMS Schema Conversion + DMS |
| **MySQL** | Aurora MySQL or RDS for MySQL | Homogeneous — native dump/restore, XtraBackup+S3, snapshot, or DMS |
| **MariaDB** | RDS for MariaDB or Aurora MySQL | Homogeneous (MariaDB treated as MySQL-compatible by DMS) |
| **PostgreSQL** | Aurora PostgreSQL or RDS for PostgreSQL | Native pg_dump/restore, logical replication, snapshot/RR import, or DMS |
| **Tibero** (TmaxData) | **Aurora PostgreSQL** (or RDS Oracle if staying Oracle-compatible) | **No native DMS/SCT support — see §5** |
| **CUBRID** (Korean OSS) | Aurora MySQL or Aurora PostgreSQL | **No native connector — JDBC extraction — see §5** |
| **Altibase** (Korean in-memory/hybrid) | Aurora PostgreSQL / Aurora MySQL | **No native connector — JDBC/custom — see §5** |

> The cost-driven re-platform thesis: AWS positions **Aurora PostgreSQL/MySQL** as the open-source target that **removes commercial Oracle/SQL Server licensing entirely** — the primary economic driver for heterogeneous migration in Korea.

---

## 3. AWS Migration Tooling

### 3.1 AWS DMS (Database Migration Service)

- **Homogeneous** (Oracle→Oracle) and **heterogeneous** (Oracle→Aurora) migrations; keeps source live; **CDC** for continuous replication; Multi-AZ.
- **Sources** include Oracle (10.2–19c), SQL Server (2008–2022, not Express), MySQL (5.5–8.4), MariaDB (10.x–11.4), PostgreSQL (9.4–18.x), MongoDB, SAP ASE, Db2 LUW & z/OS, plus Azure/Google/OCI managed DBs.
- **Targets** include all RDS engines, Aurora MySQL/PostgreSQL (incl. Serverless v2 and Aurora PostgreSQL Limitless), Redshift, S3, DynamoDB, OpenSearch, Kafka/MSK, Neptune, DocumentDB, and **Babelfish for Aurora PostgreSQL**.
- **DMS Serverless** auto-provisions/scales replication capacity — worth offering when you don't want to size a replication instance.
- ⚠️ **DMS migrates data, not schema objects** — stored procedures, triggers, views, events, sequences, and grants are **not** carried by DMS. Convert schema separately (SCT / DMS Schema Conversion / native dump).

### 3.2 AWS SCT (Schema Conversion Tool — desktop)

Converts schema + code objects across a broad matrix:
- **Oracle (10.1+) →** Aurora MySQL/PostgreSQL, MariaDB, MySQL, PostgreSQL.
- **SQL Server (2008 R2–2022) →** Aurora MySQL/PostgreSQL, Babelfish (assessment report), MariaDB, MySQL, PostgreSQL.
- Also converts **data-warehouse** schemas, **application SQL** (C++/C#/Java), **ETL** (SSIS→Glue, Teradata BTEQ), and **NoSQL** (Cassandra→DynamoDB).
- Produces an **assessment report** + an **extension pack** (Lambda/Python) to emulate features that can't convert directly.

### 3.3 DMS Schema Conversion (console/managed — "web SCT")

- Narrower scope than SCT (no data-warehouse, big-data, application-SQL, or ETL conversion — use SCT for those).
- Heterogeneous **Oracle / SQL Server → MySQL / PostgreSQL**; homogeneous MySQL/PostgreSQL.
- Targets: Aurora MySQL 8.0.32, Aurora PostgreSQL 14–17, RDS MySQL/PostgreSQL, RDS Db2.
- Includes **generative-AI-assisted conversion**. Converts tables, views, stored procedures, functions, data types, synonyms; flags unconvertible objects for manual work.
- **When to use which**: SCT for the broad/complex jobs (DW, app code, ETL); DMS Schema Conversion for straightforward Oracle/SQL Server→PostgreSQL/MySQL schema work inside the console.

### 3.4 Babelfish for Aurora PostgreSQL

- Built-in Aurora PostgreSQL capability: accepts **SQL Server client connections over the TDS wire protocol** and understands **T-SQL** → SQL Server apps migrate with minimal code change.
- T-SQL on port 1433, PL/pgSQL on 5432; TDS 7.1–7.4; requires **Aurora PostgreSQL 13+**; no extra cost.
- **When to use**: migrating SQL Server when you want to **drop the SQL Server license** and keep most T-SQL/app code.
- ⚠️ **Caveats**: not 100% T-SQL coverage (gaps close each release); differences in schema names, permissions, collations, transactional semantics; backup is pg_dump/restore-based. **Run an SCT/DMS Schema Conversion Babelfish assessment report first.**

---

## 4. Oracle → Aurora PostgreSQL — Specific Challenges

The hard part of heterogeneous migration is **code**, not data. Budget for refactoring:

| Area | Oracle | Aurora PostgreSQL | Action |
|------|--------|-------------------|--------|
| Procedural code | PL/SQL | PL/pgSQL | Different variable decl, cursors, exception model |
| **Packages** | `CREATE PACKAGE` | **No direct equivalent** | Refactor into schemas + functions |
| Sequences / identity | `CREATE SEQUENCE` | `SERIAL` / `GENERATED AS IDENTITY` | Ownership & permission semantics differ |
| **Hierarchical queries** | `CONNECT BY … START WITH` | **No equivalent** | Rewrite as `WITH RECURSIVE` CTE |
| Functions | `DECODE`, `NVL`, `ROWNUM` | — | Replace with `CASE`, `COALESCE`, window/`LIMIT` |
| Bitmap indexes | supported | **unsupported** | Redesign indexing |
| Data types | `NUMBER`, `VARCHAR2`, `DATE`(w/ time), `CLOB`/`BLOB`, `CHAR` | `NUMERIC`, `VARCHAR`, `TIMESTAMP`/`DATE`, `TEXT`/`BYTEA` | Be explicit; CHAR padding differs |
| Materialized views / partitioning | Oracle semantics | PostgreSQL semantics | Refresh & partition differences |

**Two-step process**: (1) convert schema/code (SCT or DMS Schema Conversion + manual fix-ups for flagged objects), then (2) move data (DMS full-load + CDC with automatic data-type conversion).

---

## 5. Korean Source Engines With No Native AWS Tooling ⚠️

> **This is the single most important gap for a Korean-market playbook.** **Tibero, CUBRID, and Altibase are absent from both the AWS DMS supported-source list and the AWS SCT source list.** There is **no AWS-published migration guide** for any of them. Plan for a **PoC** and custom/JDBC data movement.

### 5.1 Tibero (TmaxData / TmaxSoft)

- Commercial RDBMS **engineered for bidirectional Oracle compatibility** (SQL, PL/SQL, data types, optimizer hints map closely to Oracle).
- **Recommended target**: **Aurora PostgreSQL** (open-source exit) or RDS for Oracle (if staying Oracle-compatible).
- **Practical path** (no native connector): because Tibero is Oracle-compatible, treat its schema/PL-SQL **like Oracle** for **manual or SCT-assisted conversion** to Aurora PostgreSQL (point SCT at the Oracle-compatible DDL/PL-SQL where extractable). Move **data** via **JDBC-based bulk extract → S3/CSV → load**, or custom ETL (Glue / scripts). A Tibero JDBC driver against the Oracle/generic DMS endpoint is **not officially supported**.
- **[Validate in PoC]** — no AWS-published Tibero guide exists.

### 5.2 CUBRID (Korean open-source DB)

- **Recommended target**: Aurora MySQL or Aurora PostgreSQL (CUBRID is MySQL-like in many respects).
- **Path**: **JDBC extraction → S3/CSV → load into Aurora**; schema converted manually. **[Validate in PoC]**

### 5.3 Altibase (Korean in-memory / hybrid DB)

- **Recommended target**: Aurora PostgreSQL or Aurora MySQL depending on the app's SQL dialect (Altibase has Oracle-like and ANSI SQL features).
- **Path**: **JDBC/custom extraction → S3 → load**; manual schema conversion. **[Validate in PoC]**

> **No silent caps**: when a plan involves Tibero/CUBRID/Altibase, **explicitly tell the customer** there is no native AWS tooling, a PoC is required, and the schema/code conversion is largely manual. Do not imply DMS/SCT will "just work."

---

## 6. License Implications

| Engine | Model | Editions |
|--------|-------|----------|
| **Oracle on RDS** | **License Included (LI)** — AWS bundles Oracle | **SE2 only** |
| | **BYOL** — your existing licenses | SE2 **and** Enterprise Edition (requires active Software Update License & Support) |
| **SQL Server on RDS** | **License Included (LI)** | Enterprise / Standard / Web / Express |
| | **BYOM** (License Mobility + Software Assurance) | Enterprise / Standard / Developer |
| **Aurora / open-source** | No commercial DB license | The cost-driven migration target |

---

## 7. Heterogeneous Migration — Task Additions for the Plan

When the move is heterogeneous (Oracle/SQL Server/Tibero → Aurora), the standard DB task list gains these phases:

- [ ] **Schema assessment** — run SCT / DMS Schema Conversion assessment report; quantify auto-convert % vs. manual objects.
- [ ] **Code conversion** — refactor packages, hierarchical queries, proprietary functions; convert PL/SQL → PL/pgSQL (or assess Babelfish T-SQL coverage for SQL Server).
- [ ] **Babelfish decision** (SQL Server only) — assessment report → Babelfish vs. full PostgreSQL refactor vs. RDS SQL Server.
- [ ] **Data type mapping validation** — verify NUMBER/DATE/CLOB conversions don't lose precision or truncate.
- [ ] **Application SQL remediation** — embedded SQL in the app that uses source-specific
  dialect. This skill does NOT edit the customer's application: it produces
  **`app-remediation-findings.md`** — a machine-readable findings list (file:line where
  detectable, offending construct, target-dialect replacement, severity) — as a **handoff
  artifact for a separate app-side engagement**. That engagement runs in the customer's
  application repo with THEIR review and CI tests, and can itself be agent-assisted (the
  same coding agent, pointed at their repo, consuming this findings list). Track it as a
  critical-path dependency in migration-plan.md: data can be ready long before the app is.
- [ ] **PoC** (Tibero/CUBRID/Altibase) — prove the JDBC extract/load path and conversion before committing the cutover plan.
- [ ] **Dual-read validation** — compare query results (not just row counts) between source and target for converted code.
- [ ] **License decommission** — confirm Oracle/SQL Server license retirement and cost savings post-cutover.

---

## 8. Honesty / Confidence Notes

- Engine support, DMS/SCT source-target matrices, Babelfish, and license models are grounded in current AWS documentation (research as of **2026-06**).
- **Tibero/CUBRID/Altibase absence from DMS/SCT is verified**; the recommended paths for them are **architecture-grounded inference** requiring a PoC.
- Re-verify engine versions and DMS Schema Conversion target versions before publishing — they move frequently.
