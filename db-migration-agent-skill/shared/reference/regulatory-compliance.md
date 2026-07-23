# Korean Compliance & Regulatory Requirements for DB Migration to AWS

> **Critical Reference Document** — Why Korean enterprises encrypt, audit, and network-separate their databases, and exactly how each requirement maps onto an RDS/Aurora landing zone. These rules are the *reason* the third-party tools in [third-party-db-security.md](third-party-db-security.md) exist; the AWS-native controls here are how you satisfy the same rules after migration.

> **Confidence labels**: claims marked **[Regulatory]** are well-established regulatory facts whose verbatim primary text (KISA/PIPC standards) should be re-confirmed against the current " " before publishing a customer plan. Framework-level claims are cited to authoritative legal/AWS sources.

---

## 1. PIPA — Personal Information Protection Act (Korea)

PIPA requires controllers to take technical/administrative/physical safeguards against loss, theft, leakage, alteration, or destruction of personal information. Concretely the law and its subordinate "Standards for Safety Measures" require: an internal control plan, **access-control devices (intrusion-blocking systems)**, measures to **prevent fabrication/alteration of access (log) records**, **encryption** for safe storage and transmission, and anti-malware controls.

### 1.1 Encryption is a statutory obligation

- **PIPA Article 24(3)** restricts handling of *unique identifying information* and requires "necessary measures … **including encryption**."
- The **Enforcement Decree** requires resident registration numbers to be **retained encrypted at rest**, regardless of controller size.

**Fields that must be encrypted** **[Regulatory]**:

| Field | Requirement |
|-------|-------------|
| (RRN) and other (passport #, driver's license #, foreigner reg. #) | Encrypted at rest **and** in transit |
| (passwords) | **One-way (irreversible)** encryption — hashing, not decryptable |
| (biometric data) | Encrypted |
| / (card / bank account numbers) | Encrypted |

**Algorithm guidance** **[Regulatory]**: KISA recognizes domestic and international symmetric block ciphers — **SEED, ARIA (Korean national standards), and AES** — for reversible encryption, and one-way functions in the **SHA-256 or stronger** family for passwords.

### 1.2 Access-log retention **[Regulatory]**

- Baseline: retain access/processing records for **at least 1 year**.
- Extended: **at least 2 years** for larger controllers (commonly: ≥1,000,000 data subjects, or handling unique-identifying/sensitive info, or telecom/financial controllers).
- Logs must be protected from alteration.

### 1.3 Current dates worth flagging to customers

- PIPC issued updated **Guidelines on Security Standards on 31 Oct 2024**.
- Major **PIPA amendments take effect 11 Sep 2026**.
- **ISMS-P certification becomes mandatory, enforced from 1 Jul 2027** for in-scope entities.

### 1.4 RDS/Aurora design implications

| PIPA requirement | RDS/Aurora control |
|------------------|--------------------|
| Encryption at rest | **RDS/Aurora KMS encryption** (AES-256). Enable at instance creation — cannot be added later. |
| Encryption in transit | Force TLS: `rds.force_ssl=1` (PostgreSQL), `require_secure_transport=ON` (MySQL/Aurora MySQL). |
| RRN / passwords / biometrics / account numbers | **Column-level**: passwords one-way hashed; sensitive fields app-side encrypted (AWS Encryption SDK + KMS, or vendor API mode supporting SEED/ARIA). KMS-at-rest alone does **not** satisfy a SEED/ARIA *field* mandate. |
| Access-control devices | RDS in **private subnets**, security groups, **IAM DB auth**, least-privilege roles, optional RDS Proxy / vendor gateway. |
| Prevent alteration of access records | Ship audit logs (DAS → Kinesis → S3, or engine audit → CloudWatch → S3) with **S3 Object Lock** for immutability; retain **1 or 2 years** per §1.2. |
| Audit logging | **Database Activity Streams** (separation of duties — DBAs can't touch the stream) + engine audit (`pgaudit`, MariaDB Audit Plugin, SQL Server Audit). |

---

## 2. Electronic Financial Supervision Regulation (FSC, Korea)

For financial institutions, the FSC regime historically mandated **strict network separation (network separation (mangbunri))** between systems handling sensitive data and external networks, plus access control, encryption, audit logging, and separation of duties.

### 2.1 The 2026 network-separation reform (directly relevant to cloud DB connectivity)

- On **20 Jan 2026**, the **FSC/FSS announced an exemption from the network-separation rule for cloud-based SaaS** — removing the prior case-by-case regulatory-sandbox approval for back-office/administrative SaaS.
- **The exemption explicitly excludes systems processing users' unique identification information or personal credit information** — those remain under full network-separation rules.
- Compensating controls required: pre-screening by the **Financial Security Institute**, strict authentication and **least-privilege access**, **network-layer encryption**, monitoring of critical information flows, and **semi-annual (6-month) compliance audits**.

**Implication for the migration plan:** A core banking / credit-data DB does **not** benefit from the SaaS exemption — plan it for the **stricter separated regime**. The reform mainly eases adoption of managed tooling for *non-sensitive* workloads.

---

## 3. Network Separation (mangbunri)

**What it is**: separation between internal systems handling sensitive data and external/internet-facing networks — physical or logical separation of terminals and information-processing systems (servers). Rooted in the Electronic Financial Transactions Act / financial supervisory regulations (origins ~2014).

### 3.1 Effect on connecting to RDS/Aurora — VPC design

- **Private subnets only** for RDS/Aurora (`PubliclyAccessible = false`). This is the de-facto way to honor data-tier/internet separation.
- **Private connectivity** for app→DB and admin→DB: within the VPC, across VPC peering / Transit Gateway, or from on-prem via **Direct Connect / Site-to-Site VPN**. **No internet egress from the DB tier.**
- **AWS service access via VPC endpoints / PrivateLink** (Secrets Manager, KMS, S3, monitoring) so the DB tier never needs an internet/NAT path.
- **Admin/operations segment**: DBA access via **SSM Session Manager** (no public bastion). Conceptually reproduces the internal/external separation inside AWS.
- For workloads still under full separation: an **isolated VPC with no internet gateway**, endpoint-only AWS access, and tightly CIDR-restricted security groups.

---

## 4. ISMS-P Certification

ISMS-P (Information Security Management System + Personal information protection) is Korea's combined security/privacy certification, **mandatory and enforced from 1 Jul 2027** for in-scope entities. DB-relevant controls track the PIPA Safety Measures:

| ISMS-P control area | RDS/Aurora evidence |
|---------------------|---------------------|
| Access control (authentication, least privilege, RRN/admin access restriction) | IAM DB auth, least-privilege roles, RDS Proxy/vendor gateway, security groups |
| Encryption (at rest + in transit) of personal/unique data | KMS at-rest + TLS in-transit + app-side column encryption |
| Audit-log retention (1/2-year access-record rule) | DAS/engine audit → Kinesis/CloudWatch → S3 (Object Lock), retention configured |

**[Regulatory]** Exact ISMS-P control IDs should be confirmed against the current catalog before a certification engagement.

---

## 5. Compliance → Migration-Task Mapping (drop into the plan)

| Requirement | Prerequisite / migration task |
|-------------|-------------------------------|
| Encryption at rest | Create RDS/Aurora **with KMS encryption enabled** (cannot retrofit — must be a creation-time task). |
| In-transit encryption | Set `force_ssl` / `require_secure_transport`; update app connection strings to require TLS. |
| Field-level (RRN/account/biometric) | Confirm vendor API-mode availability **or** implement AWS Encryption SDK + KMS; if SEED/ARIA mandated, vendor API mode. |
| Passwords | Verify one-way hashing in the app; never reversibly encrypt. |
| Audit trail + immutability | Enable **DAS**; route to S3 with **Object Lock**; set retention to **1 or 2 years**; wire to SIEM/vendor. |
| network separation (mangbunri) / financial sensitivity | Private subnets, no IGW for DB tier, PrivateLink endpoints, Direct Connect/VPN for on-prem; document separation in the To-Be architecture Compliance Matrix. |
| Certification timeline | Note PIPA 2026-09-11 and ISMS-P 2027-07-01 dates if the customer's go-live is near them. |

---

## 6. Honesty / Confidence Notes

- The regulatory **framework** (PIPA Art. 24(3) encryption mandate, network separation, ISMS-P timing, the 2026 FSC reform) is firmly attributable to authoritative legal sources.
- The **granular KISA specifics** (exact SEED/ARIA/AES/SHA-256 algorithm table and the precise 1-vs-2-year retention thresholds) are well-established but should be **verified against the current " "** before being stated as fact to a customer.
- Regulations change. This reflects research as of **2026-06**; re-verify dates and thresholds before publishing.
