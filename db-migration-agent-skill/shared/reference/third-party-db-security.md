# Korean DB Security Tools vs. RDS/Aurora — Migration-Blocker Playbook

> **Critical Reference Document** — Korean enterprises almost never run a bare database. They wrap it in a domestic **DB access-control / audit** appliance and a **DB encryption** product, both mandated in practice by PIPA, the Electronic Financial Supervision Regulation, and ISMS-P. These tools are the single most common reason a "simple" RDS/Aurora migration stalls. This document explains how each tool works, **what breaks when the OS and the network disappear under managed RDS**, and the AWS-native pattern that replaces it.

> **Companion documents**: [regulatory-compliance.md](regulatory-compliance.md) (why these tools are deployed in the first place), [rds-aurora-limitations.md](rds-aurora-limitations.md) (engine-level blockers), [heterogeneous-migration.md](heterogeneous-migration.md) (Tibero/CUBRID/Altibase and Oracle/SQL Server paths).

---

> **Scope note:** this playbook applies to third-party DB *security* tooling anywhere —
> the attachment-mode analysis below is vendor-agnostic (Guardium/Imperva/Thales are
> covered under §Global Enterprise Tools). The deep per-product detail focuses on the
> Korean market because those products dominate stalled migrations there. For the other
> third-party categories (backup, monitoring, HA, proxies), see the interference sweep in
> [source-assessment.md](source-assessment.md).

## The core thesis (read this first)

On a self-managed database (on-prem or EC2), DB security tools attach in one of four ways:

| Attachment mode | How it works | Survives on RDS/Aurora? |
|-----------------|--------------|-------------------------|
| **Network sniffing** (TAP / SPAN / port-mirror) | A sensor passively mirrors DB traffic off a switch span port or hardware TAP | 🔴 **Breaks** — AWS gives you no port mirroring / SPAN on the managed network |
| **Host agent** (OS daemon on the DB server) | A daemon on the DB host inspects local sessions, blocks bypass/console access, or hooks the engine | 🔴 **Breaks** — there is no OS access on managed RDS/Aurora |
| **DB-engine plug-in** (library loaded into the engine) | A shared library / plugin installed into the DBMS performs encryption or control inside the engine | 🔴 **Breaks** — you cannot install arbitrary engine plugins on managed RDS |
| **Inline gateway / proxy** ("") | Clients connect to the proxy; the proxy connects to the DB. All policy is enforced in the proxy | 🟢 **Survives** — deploy the proxy in the VPC in front of the RDS endpoint |
| **API / app-side library** (for encryption) | An encryption library runs on the *application* server, encrypting before the query reaches the DB | 🟢 **Survives** — it never touches the DB host/engine |

**The migration rule of thumb:**
1. **Access control / audit** → must move to the vendor's **gateway/proxy mode** in the VPC, and/or be replaced by **RDS Database Activity Streams (DAS)** for the audit trail + **RDS Proxy/IAM auth/security groups** for access enforcement.
2. **Encryption** → **plug-in mode breaks**; switch to the vendor's **API/app-side mode**, or replace with **RDS/Aurora KMS encryption-at-rest** (the TDE equivalent) plus **application-level column encryption** for field-level needs.
3. **DAS is the integration point.** AWS explicitly designed Database Activity Streams to be consumed by third-party compliance tools (it documents IBM Guardium and Imperva integration). This is the pattern Korean vendors must adopt on RDS — **consume the DAS Kinesis stream instead of sniffing the wire.**

> ⚠️ **Vendor naming — get this right with the customer.** **Chakra Max is a WareValley product.** **Petra and Petra Cipher are SINSIWAY products.** **DBSafer is PNPSECURE.** **D'Amo is Penta Security.** These are frequently conflated; getting the vendor wrong in a customer meeting costs credibility.

---

## 1. Access-Control & Audit Tools (network/gateway class)

These sit between users/applications and the DB to enforce who-can-run-what and to record 100% of access for audit. On managed RDS, the sniffing and host-agent modes die; the **gateway mode is the survivor**, backed by DAS.

### 1.1 Chakra Max — WareValley

| Aspect | Detail |
|--------|--------|
| **Function** | DB access control + server/system access control + integrated account management + 100% audit logging, SQL-level control, data masking. Marketed as "Korea's first database access control." |
| **On-prem architecture** | Supports **agent**, **agent-less TAP/sniffing**, and a **"Software TAP"** capture mode marketed for cloud/virtualized environments. Integrated management across on-prem + cloud + virtualized. |
| **RDS/Aurora support** | **Claimed yes** — WareValley's supported-DBMS list explicitly includes **Aurora** (and Redshift), and lists AWS/Azure/GCP as supported clouds. The viable mechanism on managed RDS is the **Software TAP / proxy-style capture**, since hardware TAP/SPAN is impossible. |
| **What breaks** | Hardware-mirroring/SPAN sniffing 🔴; host agent on the DB OS 🔴. |
| **What survives** | Software TAP / inline gateway deployed in the VPC 🟢. |
| **RDS pattern** | Run Chakra Max in **Software-TAP / proxy mode inside the VPC** in front of the RDS endpoint; pair with **RDS Database Activity Streams** for the tamper-resistant audit trail and **RDS Proxy + IAM auth** for connection control. |
| **Confidence** | Aurora/cloud support: **verified** (vendor product page). Specific managed-RDS deployment mode: **inference** from how each mode works — request WareValley's "AWS RDS deployment guide" to confirm it is *certified*, not merely *possible*. |

### 1.2 DBSafer — PNPSECURE

| Aspect | Detail |
|--------|--------|
| **Function** | DB access control & audit (market-leading in Korea). Product family: DBSAFER DB / AM / OS / IM, plus INFOSAFER, DATACRYPTO, FaceLocker. |
| **On-prem architecture** | **Primary mode is Gateway/Proxy** — " / Gateway(Proxy) ." A **Server Agent** additionally catches bypass/direct (console) connections that don't traverse the gateway. Client component "PC Assist." |
| **RDS/Aurora support** | **Yes — architecturally the best fit of the access-control tools**, because its native mode is *already* gateway/proxy, not sniffing. "DBSAFER for Cloud" offers gateway-mode DB access control plus a server-agent variant; supports ~10 clouds. |
| **What breaks** | Server-Agent OS-level bypass control 🔴 (works on EC2-hosted DBs, not managed RDS — no OS). |
| **What survives** | Gateway/Proxy mode 🟢 — the recommended deployment. |
| **RDS pattern** | Deploy the **DBSAFER Gateway in the VPC** in front of RDS. Replace the lost OS-level bypass agent with **network enforcement**: security groups + route design so *all* client traffic is forced through the gateway and there is no direct path to the RDS endpoint. Add DAS for tamper-resistant audit. |
| **Confidence** | Gateway = native mode and RDS-viable: **high**. Specific managed-RDS certification: **medium** — confirm with vendor. |

### 1.3 Petra — SINSIWAY

| Aspect | Detail |
|--------|--------|
| **Function** | DB access control, detailed SQL analysis, REST API, centralized policy. (SINSIWAY's *encryption* product is **Petra Cipher** — see §2.1.) |
| **On-prem architecture** | Supports **Gateway, Sniffing, Agent, and Hybrid** modes. |
| **RDS/Aurora support** | **Yes via Gateway mode.** SINSIWAY's cloud page lists AWS, Azure, NCP, KT, gabia and integrates access control + encryption for cloud. |
| **What breaks** | Sniffing 🔴, Agent 🔴, Hybrid (the sniffing/agent halves) 🔴. |
| **What survives** | Gateway mode 🟢. |
| **RDS pattern** | **Gateway mode in the VPC** + DAS for audit. |
| **Confidence** | Cloud/AWS support: **verified**. Managed-RDS specifics: **medium**. |

> **Why gateway mode is the universal survivor:** managed RDS exposes only a network endpoint. Anything that needs to see the wire passively (sniffing) or sit on the host (agent) loses its vantage point. A gateway *is* a network endpoint that clients dial instead of the DB — it relocates cleanly into the VPC. The migration cost is **forcing all traffic through it** (no direct route to RDS) and **sizing/HA-ing the proxy** as a new in-path component.

---

## 2. DB Encryption Tools (encryption class)

These satisfy the PIPA/ISMS-P mandate to encrypt resident registration numbers, passwords (one-way), account/card numbers, and biometrics. On managed RDS the **plug-in/engine and OS-volume modes break**; the **API/app-side mode survives**, and **KMS at-rest encryption is the managed TDE equivalent**.

### 2.1 Petra Cipher — SINSIWAY

| Aspect | Detail |
|--------|--------|
| **Function** | DB/file encryption, standardized key management, triple-key, duplicate-encryption prevention. |
| **On-prem architecture** | Two methods — **(1) Plug-in:** encryption/decryption **library installed on the DB server** (transparent to the app); **(2) API:** library installed on the **application server** (Java/C/PL-SQL), DBMS-independent. Explicitly *not* a gateway model. |
| **What breaks** | **Plug-in mode** 🔴 — you cannot install a library into the managed RDS engine/OS. |
| **What survives** | **API mode** 🟢 — runs on your app server, encrypts before sending to RDS. |
| **RDS pattern** | Use **API mode** for column/field encryption on RDS, **or** replace with **app-side AWS Encryption SDK + KMS**. For at-rest/"TDE" needs use **RDS/Aurora KMS encryption-at-rest** (see §4). |

### 2.2 D'Amo / D.AMO — Penta Security

| Aspect | Detail |
|--------|--------|
| **Function** | DB/data encryption; the market-leading Korean DB encryption brand. Supports column-level encryption and **Format-Preserving Encryption (FPE)**. |
| **On-prem architecture** | Multi-model: **API/app-side** (D.AMO BA-SCP — "API installed in the application server to encrypt data and then sends queries to the DBMS"); **DBMS-engine / column-level** (D.AMO DE); **OS/volume-level** (D.AMO KE). |
| **What breaks** | KE (OS/volume) 🔴; DE (DB-engine plug-in) 🟠 questionable on managed RDS. |
| **What survives** | **BA-SCP (API mode)** 🟢 — runs on the app server. |
| **RDS pattern** | **D.AMO BA-SCP API mode** for column-level encryption, or **AWS Encryption SDK + KMS**; plus **RDS KMS at-rest** for the TDE-equivalent layer. Confirm BA-SCP/RDS support with Penta. |

### 2.3 CUBE-One — Comtrue Technology

| Aspect | Detail |
|--------|--------|
| **Function** | High-performance, non-stop DB encryption with **searchable encrypted indexes**, **column-level** PII encryption, FIPS-140 / Korean NIS-validated crypto module, separate access-log server. De-identification/PII tooling in the portfolio. Supports Oracle, MS-SQL, PostgreSQL. |
| **On-prem architecture** | Column-level encryption; the **install mechanism (DB-server plug-in vs. app API) is not publicly confirmed** — this is the key question to ask the vendor. |
| **What breaks / survives** | **Unverified.** By analogy to other Korean encryption tools, expect **API/app-side to survive** and **DB-server-plugin to break**. PostgreSQL support is a positive signal for Aurora PostgreSQL. |
| **RDS pattern** | If an API/app-side mode exists, use it; otherwise app-side encryption + RDS KMS at-rest. **Flag as a gap — confirm install mode with Comtrue before committing the plan.** |

### 2.4 Other tools you may encounter

- **WareValley Log Catch** — personal-information access-record management (audit complement to Chakra Max).
- **WareValley Orange / Orange Ade / Trusted Orange** — DBA/dev IDE tools, *not* security controls. They are **network SQL clients** and migrate **trivially** to RDS (no OS/host access needed). 🟢
- **PNPSECURE DATACRYPTO** — encryption companion to DBSafer; treat like other encryption tools (API mode survives).
- **Penta Security / generic** — D'Amo covered above.

---

## 3. Decision Matrix (use this in the migration plan)

| Tool (vendor) | Function | On-prem mode | Survives on RDS/Aurora? | RDS approach |
|---|---|---|---|---|
| Chakra Max (WareValley) | Access ctrl + audit | TAP/sniffing, agent, Software-TAP | sniffing/agent 🔴; Software-TAP/proxy 🟢 (Aurora listed) | Proxy/Software-TAP in VPC + DAS |
| DBSafer (PNPSECURE) | Access ctrl + audit | **Gateway/Proxy** + server agent | gateway 🟢 (best fit); agent 🔴 | Gateway in VPC + DAS |
| Petra (SINSIWAY) | Access ctrl | Gateway/Sniffing/Agent/Hybrid | gateway 🟢; sniffing/agent 🔴 | Gateway mode + DAS |
| Petra Cipher (SINSIWAY) | Encryption | Plug-in (DB server) / API (app) | plug-in 🔴; API 🟢 | API mode or KMS at-rest |
| D'Amo (Penta) | Encryption | API / DB-engine / OS-volume | API (BA-SCP) 🟢; DE 🟠; KE 🔴 | BA-SCP API or KMS at-rest |
| CUBE-One (Comtrue) | Encryption + de-id | Column encryption (mode unconfirmed) | **unverified** — confirm | API/app-side if available; KMS at-rest |
| Orange family (WareValley) | DBA/dev tool | Network client | 🟢 (just a SQL client) | No change |

---

## Global Enterprise Tools

Korean enterprises (and Korean subsidiaries of multinationals) frequently run a **global**
DAM/audit platform — **IBM Guardium** or **Imperva** — alongside or instead of the domestic
tools above, especially in finance and regulated industries. These follow the *same survival
logic*: host agents and wire-sniffing die on managed RDS, while **inline proxy modes and
Database Activity Streams (DAS) consumption survive**. AWS explicitly documents DAS integration
for both, so they are the cleanest "consume DAS instead of sniffing" pattern.

### IBM Guardium

| Aspect | Detail |
|--------|--------|
| Modes | S-TAP (host agent) / External S-TAP (inline proxy) / Guardium Insights (SaaS+API) |
| S-TAP on RDS | 🔴 Red — breaks — no OS to install the host agent on |
| External S-TAP | 🟢 Green — survives — deploy as a proxy in the VPC in front of the RDS endpoint |
| Guardium Insights + DAS | 🟢 Green — survives — consume Database Activity Streams via Kinesis |
| RDS pattern | External S-TAP for inline blocking + Guardium Insights consuming DAS for the audit trail |

### Imperva DAM

> ⚠️ **Unverified vendor detail** — the mode/RDS-survival mapping below follows the same
> structure as IBM Guardium and AWS's documented Imperva↔DAS integration (§4.1), but has
> not been confirmed against Imperva's current AWS deployment guide.
> **Verify with the vendor before using in a customer plan.**

| Aspect | Detail |
|--------|--------|
| Modes | Agent (host-based gateway agent) / Network sniffing (SecureSphere gateway appliance) / Cloud — DAS consumption |
| Agent on RDS | 🔴 Red — breaks — no OS to host the agent |
| Network sniffing | 🔴 Red — breaks — no SPAN/port-mirror on the managed RDS network |
| DAS consumption | 🟢 Green — survives — Imperva consumes Database Activity Streams via Kinesis (AWS-documented for Aurora PostgreSQL) |
| RDS pattern | Imperva consuming DAS via Kinesis for monitoring/audit + network enforcement (security groups + RDS Proxy) for access control |

### Thales CipherTrust / Vormetric
| Aspect | Detail |
|--------|--------|
| **Modes** | CTE (Transparent Encryption — file/volume agent on host) / CADP (app-side encryption library) / CipherTrust Manager (central key management) |
| **CTE on RDS** | 🔴 Breaks — encrypts at OS volume level, no OS access on managed RDS |
| **CADP (app-side)** | 🟢 Survives — encryption happens in the application layer, not the DB host |
| **CipherTrust Manager + AWS KMS XKS** | 🟢 Survives — CipherTrust Manager acts as external key authority for KMS via External Key Store; RDS uses KMS, KMS delegates to CipherTrust |
| **RDS pattern** | CADP for column-level encryption + CipherTrust Manager via KMS External Key Store (XKS) for key authority. Maintains compliance requirement of external key custody. |

### Bulk Decrypt → Re-Encrypt Migration Procedure

When transitioning from plug-in/agent encryption to app-side or KMS:

1. **Inventory**: Identify all encrypted columns/tablespaces on source
2. **Decrypt in place**: On the source DB, bulk-decrypt data (requires the original encryption key)
3. **Migrate**: Move decrypted data to Aurora/RDS via chosen method
4. **Re-encrypt on target**: Enable KMS at-rest encryption (Aurora default) + implement app-side column encryption (CADP/pgcrypto/AES_ENCRYPT) for field-level needs
5. **Verify**: Confirm encrypted columns are correctly readable through the application
6. **Decommission old keys**: After rollback window expires, revoke old encryption keys

> ⚠️ This is often the longest task in the migration — budget 2-4 weeks for large databases with many encrypted columns. The decrypt step may require a maintenance window if the source DB is under load.

### Gateway/Proxy Deployment Checklist

When deploying security tools in gateway/proxy mode in front of RDS:

> ⚠️ **Baseline checklist** — derived from the gateway/proxy migration concerns in this
> document (§1.2 DBSafer, §4.2 access control). Extend it with the specific vendor's
> deployment guide before using in a customer plan.

- [ ] **Latency impact measured** (target the added round-trip vs. direct RDS connection; benchmark p50/p99 before committing)
- [ ] **High availability** — deploy the gateway across ≥2 AZs; a single proxy instance is now a single point of failure in front of the DB
- [ ] **Capacity / sizing** — size the proxy for peak concurrent connections + throughput; it is a new in-path bottleneck
- [ ] **No bypass path** — security groups + route tables force *all* client traffic through the gateway; RDS in private subnets only, `PubliclyAccessible = false`, no direct route to the RDS endpoint
- [ ] **Connection pooling interaction** — confirm the gateway coexists with RDS Proxy / app-side pools without double-pooling or idle-timeout conflicts
- [ ] **TLS termination** — decide where TLS terminates (client→gateway→RDS); enforce encryption in transit on both hops (`require_secure_transport` / `rds.force_ssl`)
- [ ] **Audit integration** — gateway logs forwarded to the SIEM, and/or DAS enabled in parallel for the tamper-resistant trail
- [ ] **Failover behavior** — define what happens if the gateway is unavailable (fail-closed for security vs. fail-open for availability — a compliance decision)
- [ ] **Vendor RDS certification confirmed** — the vendor's gateway mode is *certified* on managed RDS, not merely possible (request the AWS deployment guide)

---

## 4. AWS-Native Replacements

### 4.1 Audit — replace sniffing-based DB audit with Database Activity Streams (DAS)

**Amazon RDS / Aurora Database Activity Streams** captures, near-real-time: SQL commands, connection info, DML row counts, accessed objects, bind variables, session/network info. Passwords are redacted. The stream is pushed to an **auto-managed Kinesis** data stream, consumable by Firehose/Lambda.

- **Separation of duties is built in**: DBAs cannot access the stream collection/processing pipeline → tamper-resistant audit, which is exactly what the Korean audit appliances provide on-prem.
- **Supports async and sync modes** (sync blocks the DB if the stream can't keep up — choose per compliance strictness).
- **Third-party integration is a first-class design goal**: AWS documents DAS integration with IBM Security Guardium and Imperva for Aurora PostgreSQL. **Korean vendors plug into DAS/Kinesis the same way.**
- **Complementary AWS services**: CloudWatch Logs (engine audit logs — MariaDB Audit Plugin for MySQL/MariaDB, `pgaudit` for PostgreSQL, SQL Server Audit), CloudTrail (control-plane API audit), EventBridge, GuardDuty RDS Protection.

```bash
# Enable Database Activity Streams on an Aurora cluster (sync mode)
aws rds start-activity-stream \
 --resource-arn arn:aws:rds:ap-northeast-2:ACCOUNT:cluster:my-aurora-cluster \
 --mode sync \
 --kms-key-id arn:aws:kms:ap-northeast-2:ACCOUNT:key/KEY-ID \
 --apply-immediately
```

### 4.2 Access control — replace the inline DB-access appliance

- **RDS Proxy** — managed proxy that can **enforce IAM authentication**, integrates **Secrets Manager**, and provides private VPC-only access. This is the native analog of the Korean "" mode. ⚠️ It does **not** give the fine-grained SQL-command-level policy/masking the Korean tools provide — for those, run the vendor's **gateway mode** in the VPC alongside it.
- **Network enforcement** — security groups + route design so all traffic funnels through the proxy/gateway, replacing the lost OS-level bypass agent. RDS in **private subnets only**, `PubliclyAccessible = false`.
- **IAM database authentication** + least-privilege DB roles for the authentication mandate.

### 4.3 Encryption — replace TDE plug-in / OS-volume encryption

- **RDS/Aurora encryption at rest = the managed TDE equivalent**: AES-256, KMS-managed keys, covers storage + backups + replicas + snapshots, no app changes. ⚠️ **Must be enabled at instance creation and cannot be disabled later**; KMS keys are Region-specific. (Note: newer Aurora clusters are auto-encrypted at rest by default.)
- **Column/field-level encryption** (which at-rest encryption does *not* provide): app-side API encryption — vendor API mode (Petra Cipher API, D.AMO BA-SCP) or **AWS Encryption SDK + KMS**.
- **In transit**: force TLS (`rds.force_ssl=1` for PostgreSQL, `require_secure_transport=ON` for MySQL/Aurora MySQL).
- ⚠️ **Algorithm caveat for Korean auditors**: KMS uses AES-256. If a regulator insists on **SEED/ARIA** for specific fields, that must be done at the **application/column layer** with the vendor's API mode (which supports the Korean ciphers) — RDS at-rest cannot satisfy a SEED/ARIA mandate by itself. See [regulatory-compliance.md](regulatory-compliance.md).

---

## 5. Questions to Ask the Customer (assessment checklist)

When the as-is database is wrapped in Korean security tooling, surface these **before** choosing a migration method — they frequently change the target architecture:

- [ ] **Which access-control/audit product, and in which mode?** (sniffing / agent / gateway). If sniffing or agent only → gateway licensing/redesign is a prerequisite task.
- [ ] **Which encryption product, and in which mode?** (plug-in / API / OS-volume). If plug-in or OS-volume → app-side re-architecture or KMS migration is a prerequisite task.
- [ ] **Does the vendor have a *certified* AWS RDS/Aurora deployment guide?** Request it. "Possible" ≠ "supported."
- [ ] **What is encrypted today, with which algorithm?** (RRN, passwords, account numbers, biometrics; SEED/ARIA/AES). Maps to the field-level encryption plan.
- [ ] **What is the audit-log retention requirement?** (1 yr / 2 yr — see [regulatory-compliance.md](regulatory-compliance.md)). Drives Kinesis→S3/CloudWatch retention + S3 Object Lock immutability.
- [ ] **Is this a financial workload subject to network separation (mangbunri)?** If it touches unique-ID / personal-credit info, plan the stricter separated regime (private subnets, no IGW, PrivateLink, Direct Connect).
- [ ] **Will the existing audit/SIEM consume DAS?** Confirm the vendor or in-house SIEM can read the Kinesis stream, or plan a Firehose→S3/OpenSearch bridge.

---

## 6. Honesty / Confidence Notes

- Vendor product **existence and function** are verified from vendor pages. **Granular "does mode X work on *managed* RDS"** is rarely stated by vendors verbatim — those cells are **architecture-grounded inference** (how each mode interacts with the loss of OS + network visibility), not vendor warranties.
- For a customer-facing plan, **request each vendor's AWS RDS deployment guide** and verify whether their gateway/API mode is *certified* on managed RDS, not merely possible.
- CUBE-One's install mechanism and Comtrue's de-identification product specifics are **unconfirmed gaps** — flag and confirm with the vendor.
- This document reflects research as of **2026-06**. Vendor cloud support evolves quickly; re-verify before publishing a customer plan.
