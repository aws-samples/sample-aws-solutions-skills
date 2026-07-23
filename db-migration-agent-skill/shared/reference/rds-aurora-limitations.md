# RDS & Aurora Migration: Limitations, Incompatibilities & Breaking Changes

> **Critical Reference Document** — What breaks, what won't work, and what requires changes when migrating from self-managed databases to Amazon RDS or Aurora.

---

## Table of Contents

1. [Encryption Incompatibilities](#1-encryption-incompatibilities) — incl. §1.6 Korean encryption tools, §1.7 Korean access-control/audit appliances
2. [Storage Engine Limitations](#2-storage-engine-limitations)
3. [Authentication Limitations](#3-authentication-limitations)
4. [Feature Limitations (Privilege & Access)](#4-feature-limitations--privilege--access-restrictions)
5. [Replication & Topology Limitations](#5-replication--topology-limitations)
6. [Network & Connectivity Limitations](#6-network--connectivity-limitations)
7. [Operational Limitations](#7-operational-limitations)
8. [Data Type, Charset & Timezone Issues](#8-data-type-charset--timezone-issues)
9. [PostgreSQL-Specific Limitations](#9-postgresql-specific-limitations)
10. [Summary: Blocker vs. Adjustment Matrix](#10-summary-blocker-vs-adjustment-matrix)

---

## 1. Encryption Incompatibilities

### 1.1 MySQL Transparent Data Encryption (TDE) — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | MySQL InnoDB Tablespace Encryption (TDE) is explicitly **not supported** on RDS for MySQL or Aurora MySQL. The `ENCRYPTION='Y'` clause on `CREATE TABLE` / `ALTER TABLE` and the InnoDB tablespace encryption feature will not function. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** — must redesign encryption approach |
| **Workaround** | Use RDS/Aurora **at-rest encryption** (AES-256, AWS KMS-managed) which encrypts the entire storage volume, including backups, replicas, and snapshots. This is enabled at instance creation and cannot be changed after. Alternatively, use application-level encryption (encrypt before writing). |
| **Source** | [AWS Docs: MySQL features not supported](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html) — "InnoDB Tablespace Encryption" listed as unsupported. |

### 1.2 MySQL Keyring Plugin — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | The MySQL `keyring_file`, `keyring_encrypted_file`, `keyring_aws` (Amazon Web Services Keyring Plugin), and all other keyring plugins are **not supported** on RDS/Aurora. Any feature relying on the keyring subsystem (including TDE, encrypted general tablespace, encrypted binary logs) will fail. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** — if your application relies on keyring-based encryption |
| **Workaround** | Migrate to AWS KMS-based volume encryption. For column-level encryption needs, implement application-side encryption using AWS Encryption SDK, or use functions like `AES_ENCRYPT()`/`AES_DECRYPT()` in MySQL (managing keys externally in AWS Secrets Manager or KMS). |
| **Source** | [AWS Docs: Known Issues](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) — "MySQL Keyring Plugin not supported" |

### 1.3 Third-Party Encryption Solutions (Vormetric, Thales CipherTrust, etc.)

| Aspect | Detail |
|--------|--------|
| **What breaks** | Solutions like Vormetric/Thales CipherTrust that require OS-level agents, file-system level encryption, or kernel modules **cannot be installed** on RDS/Aurora because there is no OS-level access. Solutions requiring custom MySQL plugins also cannot work. |
| **Affected service** | RDS + Aurora (all engines) |
| **Severity** | 🔴 **Blocker** — agent-based solutions are incompatible |
| **Workaround** | Replace with: (1) AWS KMS for at-rest encryption, (2) Application-level encryption with external key management, (3) AWS CloudHSM for FIPS 140-2 Level 3 key storage if regulatory compliance requires it. Some CipherTrust features can map to AWS-native services. |

### 1.4 Column-Level Encryption Approaches

| Aspect | Detail |
|--------|--------|
| **What breaks** | MySQL `AES_ENCRYPT()`/`AES_DECRYPT()` functions still work. However, approaches that depend on keyring plugins for key management, or that use InnoDB-native column encryption features, will not function. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Continue using `AES_ENCRYPT()`/`AES_DECRYPT()` with keys stored in AWS Secrets Manager. For PostgreSQL, `pgcrypto` extension IS supported and works normally on both RDS and Aurora PostgreSQL. |

### 1.5 PostgreSQL pgcrypto — SUPPORTED (with caveats)

| Aspect | Detail |
|--------|--------|
| **What breaks** | `pgcrypto` extension works on both RDS PostgreSQL and Aurora PostgreSQL. However, you cannot use OS-level key files or custom file paths. Keys must be passed in SQL or managed by the application. |
| **Affected service** | N/A — this works |
| **Severity** | 🟢 **Works** — minor adjustment to key management |
| **Workaround** | Store encryption keys in AWS Secrets Manager rather than OS files. |

### 1.6 Korean DB Encryption Solutions (Petra Cipher, D'Amo, CUBE-One) — MODE-DEPENDENT

| Aspect | Detail |
|--------|--------|
| **What breaks** | Korean DB encryption products attach in several modes. **DB-engine plug-in mode** (Petra Cipher plug-in; D'Amo DE) and **OS/volume mode** (D'Amo KE) require installing a library into the DBMS engine or onto the host OS — **impossible on managed RDS/Aurora** (no engine-plugin install, no OS access). CUBE-One's install mode is vendor-confirm-required. |
| **Affected service** | RDS + Aurora (all engines) |
| **Severity** | 🔴 **Blocker** for plug-in / OS-volume modes |
| **Workaround** | Switch to the vendor's **API / app-side mode** (Petra Cipher API; D'Amo BA-SCP), which runs encryption on the application server before the query reaches the DB — this survives. Or replace with **RDS/Aurora KMS encryption-at-rest** (the managed TDE equivalent) plus **app-side column encryption** (AWS Encryption SDK + KMS) for field-level needs. ⚠️ If a Korean auditor mandates **SEED/ARIA** for specific fields, that must be done at the app/column layer with a vendor API supporting those ciphers — KMS-at-rest (AES-256) alone does not satisfy a SEED/ARIA *field* mandate. |
| **Detail** | See [third-party-db-security.md](third-party-db-security.md) §2 and [regulatory-compliance.md](regulatory-compliance.md) §1. |

### 1.7 Korean DB Access-Control / Audit Appliances (Chakra Max, DBSafer, Petra) — MODE-DEPENDENT

| Aspect | Detail |
|--------|--------|
| **What breaks** | Korean DB access-control/audit tools attach via **network sniffing** (TAP/SPAN/port-mirror) and/or a **host agent** on the DB OS. Managed RDS provides **no port mirroring/SPAN** and **no OS access**, so both modes break. |
| **Affected service** | RDS + Aurora (all engines) |
| **Severity** | 🔴 **Blocker** for sniffing / host-agent modes |
| **Workaround** | Move to the vendor's **inline gateway/proxy mode** deployed in the VPC in front of the RDS endpoint (DBSafer's native mode; Chakra Max Software-TAP; Petra Gateway), and force all client traffic through it via security groups + route design. Replace the audit trail with **Amazon RDS/Aurora Database Activity Streams (DAS)** — explicitly designed for third-party compliance-tool integration — plus engine audit (`pgaudit`, MariaDB Audit Plugin, SQL Server Audit) and **RDS Proxy + IAM auth** for connection control. |
| **Detail** | See [third-party-db-security.md](third-party-db-security.md) §1 and §4. |

---

## 2. Storage Engine Limitations

### 2.1 MyISAM Tables

| Aspect | Detail |
|--------|--------|
| **What breaks** | **On RDS MySQL**: MyISAM tables are technically supported but with significant caveats — they do NOT support reliable crash recovery, Point-In-Time Restore (PITR), or snapshot restore. Data can be lost/corrupted after crash recovery. **On Aurora MySQL**: MyISAM is **NOT supported for user data tables**. Aurora ONLY supports InnoDB for user tables. All MyISAM tables must be converted to InnoDB before migration. |
| **Affected service** | RDS MySQL (degraded), Aurora MySQL (blocked) |
| **Severity** | 🔴 **Blocker for Aurora**, 🟡 **Risky on RDS** |
| **Workaround** | Convert all MyISAM tables to InnoDB before migration: `ALTER TABLE schema.table_name ENGINE=InnoDB, ALGORITHM=COPY;` — Note: InnoDB tables may be larger than MyISAM equivalents and have different locking/performance characteristics (row-level vs table-level locking). |
| **Source** | [AWS Docs: Supported storage engines](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html), [Aurora migration prechecks](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.Prechecks.html) |

### 2.2 FEDERATED Storage Engine — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | The FEDERATED storage engine is explicitly not supported on RDS for MySQL. Tables using this engine cannot be created or migrated. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** if you use FEDERATED tables |
| **Workaround** | Replace with: (1) MySQL `mysql_fdw` / direct queries over network, (2) Application-level data federation, (3) AWS Database Migration Service for data synchronization, (4) Views over linked tables using other mechanisms. |
| **Source** | [AWS Docs: MySQL feature support](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html) |

### 2.3 MEMORY (HEAP) Engine Tables

| Aspect | Detail |
|--------|--------|
| **What breaks** | MEMORY engine tables work on RDS MySQL but data is lost on restart/failover (as expected for MEMORY engine). On Aurora, MEMORY tables have the same behavior. The issue is that in Aurora's shared-storage architecture with potential automatic failover, data loss from MEMORY tables becomes more likely and less predictable. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Requires adjustment** — consider architecture implications |
| **Workaround** | Replace MEMORY tables with InnoDB tables or use ElastiCache (Redis/Memcached) for caching workloads. For temporary processing, use InnoDB temp tables. |

### 2.4 ARCHIVE and BLACKHOLE Engines

| Aspect | Detail |
|--------|--------|
| **What breaks** | ARCHIVE and BLACKHOLE engines are available on RDS MySQL but are not officially recommended. On Aurora MySQL, only InnoDB is supported for user tables — ARCHIVE and BLACKHOLE will not work. |
| **Affected service** | Aurora MySQL (blocked) |
| **Severity** | 🟡 **Requires adjustment for Aurora** |
| **Workaround** | ARCHIVE → Migrate data to InnoDB with appropriate compression, or archive to S3 via Data Pipeline. BLACKHOLE → Replace with binlog replication filters or application logic. |

### 2.5 Compressed Tables / Pages (Aurora-specific)

| Aspect | Detail |
|--------|--------|
| **What breaks** | Aurora MySQL does NOT support compressed tables (`ROW_FORMAT=COMPRESSED`) or page compression (`COMPRESSION = 'zlib'|'lz4'`). Tables created with these options must be decompressed before migration. |
| **Affected service** | Aurora MySQL |
| **Severity** | 🔴 **Blocker for Aurora** if using compressed tables |
| **Workaround** | Before migration, alter tables: `ALTER TABLE t ROW_FORMAT=DYNAMIC;` and `ALTER TABLE t COMPRESSION='none';` — Note: This will increase storage usage. |
| **Source** | [Aurora MySQL migration prechecks](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.Prechecks.html) |

### 2.6 InnoDB Page Compression (RDS-specific)

| Aspect | Detail |
|--------|--------|
| **What breaks** | Starting February 2024, all newly created RDS MySQL DB instances have a file system block size of 16KB. InnoDB page compression requires the file system block size to be smaller than the InnoDB page size. Since the default InnoDB page size is also 16KB, page compression **does not work** on new RDS instances. |
| **Affected service** | RDS MySQL (new instances from Feb 2024) |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Do not rely on InnoDB page compression. Use standard InnoDB compression (`ROW_FORMAT=COMPRESSED`) on RDS, or accept slightly higher storage usage. |
| **Source** | [AWS Known Issues](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) |

### 2.7 Custom/Third-Party Storage Engines

| Aspect | Detail |
|--------|--------|
| **What breaks** | Any custom-compiled or third-party storage engines (TokuDB, RocksDB/MyRocks, Spider, etc.) are **not available** on RDS or Aurora. You cannot install custom engine plugins. |
| **Affected service** | RDS + Aurora (all MySQL variants) |
| **Severity** | 🔴 **Blocker** if dependent on custom engines |
| **Workaround** | Migrate to InnoDB. For TokuDB compression benefits, consider Aurora's storage efficiency or RDS with gp3 storage. For RocksDB write-optimization, test InnoDB with appropriate buffer pool tuning. |

---

## 3. Authentication Limitations

### 3.1 PAM Authentication Plugin — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | PAM (Pluggable Authentication Modules) authentication is not available on RDS or Aurora. You cannot use OS-level PAM configurations for database authentication. |
| **Affected service** | RDS + Aurora (MySQL and PostgreSQL) |
| **Severity** | 🔴 **Blocker** if PAM is your primary auth method |
| **Workaround** | Replace with: (1) IAM Database Authentication (token-based, supported for MySQL and PostgreSQL), (2) Kerberos/Active Directory authentication (supported for both RDS and Aurora PostgreSQL and MySQL), (3) Standard MySQL native authentication. |

### 3.2 LDAP Authentication Plugin (MySQL) — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | The MySQL Enterprise LDAP authentication plugins (`authentication_ldap_simple`, `authentication_ldap_sasl`) are not available on RDS or Aurora MySQL. Direct LDAP authentication against MySQL is not possible. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** if using MySQL LDAP auth |
| **Workaround** | Use Kerberos authentication via AWS Managed Microsoft AD (which can trust on-premises AD/LDAP). This provides centralized authentication with similar functionality. Alternatively, use IAM database authentication with identity federation. |

### 3.3 Custom Authentication Plugins — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | Any custom-compiled authentication plugins cannot be installed. The only authentication plugins available are those pre-installed by AWS: `mysql_native_password`, `caching_sha2_password`, and `AWSAuthenticationPlugin` (for IAM auth). The "Authentication Plugin" category is explicitly listed as unsupported. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** if using custom auth plugins |
| **Workaround** | Migrate to supported authentication: native MySQL auth, IAM database auth, or Kerberos via AWS Directory Service. |
| **Source** | [AWS Docs: Features not supported](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html) |

### 3.4 Default Authentication Plugin Changes

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS MySQL 8.0.34+ forces `mysql_native_password` as default (you cannot change it). RDS MySQL 8.4+ uses `caching_sha2_password` by default. Client applications using older MySQL connectors that don't support `caching_sha2_password` will fail to connect to 8.4 instances. |
| **Affected service** | RDS MySQL |
| **Severity** | 🟡 **Requires adjustment** — update client libraries |
| **Workaround** | Update MySQL client connectors/drivers to versions supporting `caching_sha2_password`, or change the `authentication_policy` parameter for MySQL 8.4. Ensure TLS is configured (required for `caching_sha2_password` RSA key exchange). |

### 3.5 PostgreSQL pg_hba.conf — NO DIRECT ACCESS

| Aspect | Detail |
|--------|--------|
| **What breaks** | You cannot directly edit `pg_hba.conf` on RDS/Aurora PostgreSQL. Authentication methods configured via `pg_hba.conf` (like `cert`, `ident`, `peer`, custom PAM) cannot be configured in the traditional way. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Authentication is controlled via: (1) VPC Security Groups (network-level access), (2) `rds.force_ssl` parameter for SSL requirements, (3) IAM database authentication, (4) Kerberos via AWS Directory Service, (5) Standard password authentication. The `rds.accepted_password_auth_method` parameter controls allowed methods. |

### 3.6 Certificate-Based Client Authentication Differences

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS/Aurora provides server-side certificates for TLS but client certificate authentication (mutual TLS where the server validates client certs) is limited. You cannot configure arbitrary CA certificates for client validation. For MySQL, `REQUIRE X509` works but certificate management differs from self-managed. |
| **Affected service** | RDS + Aurora (both engines) |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Use IAM database authentication as a replacement for certificate-based auth. For MySQL, `REQUIRE SSL` and `REQUIRE X509` still function with RDS-provided certificates. |

---

## 4. Feature Limitations — Privilege & Access Restrictions

### 4.1 SUPER Privilege — NOT AVAILABLE

| Aspect | Detail |
|--------|--------|
| **What breaks** | The `SUPER` privilege is **never granted** to any user on RDS/Aurora. This breaks: (1) `SET GLOBAL` for certain variables, (2) Creating stored procedures/triggers/events with a different DEFINER, (3) `CHANGE MASTER TO` directly, (4) Killing sessions owned by other users directly, (5) Some `mysqldump` imports that include DEFINER clauses, (6) Setting `gtid_purged`, (7) Binary log administration. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Major impact** — affects many DBA operations |
| **Workaround** | (1) Use `rds_superuser_role` (MySQL 8.0.36+) which provides dynamic privileges replacing SUPER, (2) Remove DEFINER clauses from dump files or use `--set-gtid-purged=OFF`, (3) Use `CALL mysql.rds_kill(thread_id)` instead of `KILL`, (4) Use parameter groups for global variable changes, (5) Use stored procedures `mysql.rds_set_external_source()` for replication config. |
| **Source** | [Master user privileges](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.MasterAccounts.html) |

### 4.2 File System Access (LOAD DATA INFILE / SELECT INTO OUTFILE) — RESTRICTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | `LOAD DATA LOCAL INFILE` works (reads from client). `LOAD DATA INFILE` (server-side file read) does NOT work — no filesystem access. `SELECT ... INTO OUTFILE` does NOT work for writing to local filesystem. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** for workflows using server-side file operations |
| **Workaround** | (1) `LOAD DATA LOCAL INFILE` reads from client machine, (2) For Aurora MySQL: `LOAD DATA FROM S3` and `SELECT INTO OUTFILE S3` provide S3 integration, (3) For RDS MySQL: Import from S3 using backup/restore, use `mysqlimport` with `--local` flag, (4) For exports: Use `mysqldump`, or query and export from application. |
| **Source** | [Aurora S3 integration](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Integrating.SaveIntoS3.html) |

### 4.3 User-Defined Functions (UDFs) — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | C-language UDFs (compiled shared libraries loaded via `CREATE FUNCTION ... SONAME`) are **completely impossible** on RDS/Aurora. You cannot upload `.so` files or install native-code UDFs. This is distinct from stored functions (which ARE supported). |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** if using C-compiled UDFs |
| **Workaround** | (1) Rewrite UDF logic as stored functions (SQL/procedural), (2) Move logic to application layer, (3) Use AWS Lambda functions invoked from Aurora MySQL (`lambda_sync`/`lambda_async`), (4) For PostgreSQL: Use Trusted Language Extensions (pg_tle) for custom extensions in safe languages. |

### 4.4 Custom Plugins — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | No custom MySQL plugins can be installed. Explicitly unsupported: Password Strength Plugin, Rewriter Query Rewrite Plugin, InnoDB full-text parser plugin, X Plugin (mysqlx/port 33060), Group Replication Plugin, Semisynchronous replication plugin (except Multi-AZ DB clusters). |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🔴 **Blocker** if dependent on custom plugins |
| **Workaround** | Per-plugin alternatives: Password Strength → use application-side validation or AWS Secrets Manager password policies. Query Rewrite → ProxySQL/application-level rewriting. X Plugin → blocked, port 33060 is blocked on RDS. |
| **Source** | [AWS Docs: Features not supported](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html) |

### 4.5 Operating System Access — NONE

| Aspect | Detail |
|--------|--------|
| **What breaks** | No SSH, no shell access, no Telnet, no RDP. This means: (1) No cron jobs on the DB server, (2) No shell scripts, (3) No custom monitoring agents on the DB host, (4) No OS-level file manipulation, (5) No custom kernel tuning, (6) No direct access to data directory, (7) No manual binary log manipulation. |
| **Affected service** | RDS + Aurora (all engines) |
| **Severity** | 🔴 **Fundamental architecture difference** |
| **Workaround** | (1) Replace cron with Amazon EventBridge + Lambda, (2) Replace shell scripts with Lambda or Step Functions, (3) Use CloudWatch for monitoring, (4) Use Performance Insights for DB-level metrics, (5) Consider RDS Custom (for Oracle or SQL Server) which provides OS access if absolutely required. |

### 4.6 Binary Log Access Differences

| Aspect | Detail |
|--------|--------|
| **What breaks** | Direct access to binary logs is restricted. You cannot use `PURGE BINARY LOGS`, cannot directly manage binlog files, and `mysqlbinlog` utility access is available but controlled. Binary logs are retained according to the `binlog retention hours` setting (via stored procedure `mysql.rds_set_configuration`). You cannot access binlog files from replica instances on Aurora. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | (1) Use `mysql.rds_set_configuration('binlog retention hours', N)` — max 2160 hours (90 days), (2) Use `SHOW BINARY LOGS` and `mysqlbinlog` utility for download/streaming, (3) For Aurora: Enhanced binlog reduces performance overhead. |

### 4.7 Persisted System Variables — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | MySQL 8.0's `SET PERSIST` and `SET PERSIST_ONLY` commands do not work. The `mysqld-auto.cnf` file mechanism is unavailable. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Use Parameter Groups. All persistent configuration changes must be made through AWS Parameter Groups (DB or Cluster parameter groups). Dynamic parameters can be changed immediately; static parameters require reboot. |

### 4.8 Custom my.cnf / postgresql.conf Settings

| Aspect | Detail |
|--------|--------|
| **What breaks** | Not all MySQL/PostgreSQL configuration parameters are exposed in Parameter Groups. Some parameters are locked (e.g., `datadir`, `innodb_data_file_path`, `server_id`, `port` to some extent). Some parameters have restricted ranges. Parameters requiring SUPER privilege to set dynamically may need parameter group changes + reboot. |
| **Affected service** | RDS + Aurora (both engines) |
| **Severity** | 🟡 **Requires adjustment** |
| **Workaround** | Review all custom settings and map to available parameters. Use `aws rds describe-db-parameters` to see available/modifiable parameters. Accept that some low-level OS/filesystem tuning is managed by AWS. |

---

## 5. Replication & Topology Limitations

### 5.1 MySQL Group Replication — NOT SUPPORTED (standard RDS)

| Aspect | Detail |
|--------|--------|
| **What breaks** | The MySQL Group Replication plugin is not available on standard RDS MySQL or Aurora MySQL (version 2). However, RDS MySQL 8.4 introduced active-active clusters based on Group Replication. Aurora MySQL version 3 doesn't support the Group Replication plugin in the traditional sense — Aurora uses its own replication mechanism. |
| **Affected service** | Aurora MySQL (all versions), RDS MySQL (versions prior to 8.4) |
| **Severity** | 🟡 **Requires architecture change** |
| **Workaround** | (1) For Aurora: Aurora's native replication (shared storage) provides better availability than Group Replication, (2) For RDS MySQL 8.4+: Active-active clusters are available, (3) Aurora Global Database for cross-region replication. |

### 5.2 Galera Cluster Features — NOT AVAILABLE

| Aspect | Detail |
|--------|--------|
| **What breaks** | Galera Cluster (used by MariaDB Cluster, Percona XtraDB Cluster) is a synchronous multi-master replication system. It is completely unavailable on RDS/Aurora. Galera-specific features (wsrep, SST/IST, flow control, certification-based replication) do not exist. |
| **Affected service** | RDS + Aurora (all MySQL/MariaDB variants) |
| **Severity** | 🔴 **Blocker** — requires complete architecture redesign |
| **Workaround** | (1) Aurora multi-AZ with read replicas (one writer, up to 15 readers), (2) Aurora Global Database for cross-region, (3) RDS MySQL 8.4 active-active clusters, (4) For true multi-master write: consider Amazon DynamoDB or application-level sharding. |

### 5.3 Multi-Source Replication

| Aspect | Detail |
|--------|--------|
| **What breaks** | Multi-source replication (one replica receiving from multiple sources) was NOT supported on Aurora MySQL (version 2 explicitly lists it as unsupported). For RDS MySQL 8.0.35+, multi-source replication IS now supported. |
| **Affected service** | Aurora MySQL (not supported) |
| **Severity** | 🟡 **Requires adjustment** for Aurora |
| **Workaround** | For RDS MySQL: Use multi-source replication natively (supported 8.0.35+). For Aurora: Consolidate data from multiple sources at application level, or use intermediate RDS MySQL instance as aggregator before feeding to Aurora. |
| **Source** | [Aurora MySQL v2 limitations](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.CompareMySQL57.html), [RDS multi-source](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/mysql-multi-source-replication.html) |

### 5.4 Replication Filtering (Aurora MySQL v2)

| Aspect | Detail |
|--------|--------|
| **What breaks** | Aurora MySQL version 2 did NOT support replication filtering. Aurora MySQL version 3 (MySQL 8.0 compatible) DOES support replication filtering. |
| **Affected service** | Aurora MySQL version 2 (resolved in v3) |
| **Severity** | 🟢 **Resolved in current versions** |
| **Workaround** | Upgrade to Aurora MySQL version 3. |

### 5.5 Read Replica Limits

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS MySQL: Maximum 15 read replicas per primary (up from previous 5). Aurora: Maximum 15 Aurora Replicas per cluster. Cross-region read replicas have additional lag. You cannot create a replica of a replica (no cascading). |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟢 **Generally sufficient** — 15 is generous |
| **Workaround** | If you need more than 15 read endpoints, use RDS Proxy for connection pooling/multiplexing, or implement read-side caching (ElastiCache). |

---

## 6. Network & Connectivity Limitations

### 6.1 No Direct Server Access (SSH/SSM)

| Aspect | Detail |
|--------|--------|
| **What breaks** | No SSH, SSM Session Manager, Telnet, or RDP access to the DB host. This is a fundamental aspect of the managed service. You cannot troubleshoot at the OS level. |
| **Affected service** | RDS + Aurora (all engines) |
| **Severity** | 🟡 **Fundamental design — adjust workflows** |
| **Workaround** | Use Enhanced Monitoring (OS-level metrics), Performance Insights, CloudWatch, and database-level diagnostic queries. For OS-level access needs, consider RDS Custom (Oracle/SQL Server only). |

### 6.2 Custom Port Restrictions

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS does support custom ports (you can choose during creation). However, **port 33060 is explicitly blocked** for MySQL (X Protocol port). You must choose a different port if you were using 33060. Standard ports: MySQL 3306, PostgreSQL 5432. |
| **Affected service** | RDS MySQL |
| **Severity** | 🟢 **Minor** — custom ports work except 33060 |
| **Workaround** | Use any port except 33060 for MySQL. |
| **Source** | [AWS Known Issues](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) |

### 6.3 Multi-VPC Access Patterns

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS/Aurora instances reside in a specific VPC. Accessing from other VPCs requires explicit networking setup. Cross-VPC access is not automatic. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Requires networking setup** |
| **Workaround** | (1) VPC Peering between VPCs, (2) AWS Transit Gateway for hub-and-spoke, (3) PrivateLink/VPC Endpoints (available for RDS Proxy), (4) For Aurora: Custom endpoints can serve specific workloads but don't solve cross-VPC by themselves. |

### 6.4 On-Premises Connectivity

| Aspect | Detail |
|--------|--------|
| **What breaks** | Self-managed databases on-premises may have been directly accessible on the local network. RDS/Aurora requires AWS network connectivity. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Requires networking infrastructure** |
| **Workaround** | (1) AWS Direct Connect for dedicated private connectivity, (2) AWS Site-to-Site VPN, (3) RDS instances can be made publicly accessible (not recommended for production), (4) VPN + Security Groups for controlled access. |

### 6.5 Public IP Address Behavior

| Aspect | Detail |
|--------|--------|
| **What breaks** | When publicly accessible, RDS uses a DNS name that resolves to a public IP. The IP can change during failover. Applications hardcoding IPs will break. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Design consideration** |
| **Workaround** | Always use DNS endpoint names, never hardcode IPs. Use appropriate DNS TTL settings in clients. |

---

## 7. Operational Limitations

### 7.1 Maintenance Windows — Mandatory Patching

| Aspect | Detail |
|--------|--------|
| **What breaks** | AWS **can and will** apply mandatory security patches and OS updates during your maintenance window, even if you haven't requested them. In critical security cases, patching may occur even with auto minor version upgrade disabled. You cannot indefinitely defer mandatory patches. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Operational risk** — brief downtime during patching |
| **Workaround** | (1) Set maintenance window during lowest-traffic period, (2) Aurora supports Zero-Downtime Patching (ZDP) for some patches, (3) Use Multi-AZ for automatic failover during maintenance (30-60 sec), (4) Blue/Green Deployments for controlled major upgrades, (5) Subscribe to RDS events for advance notification. |
| **Source** | [Aurora maintenance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBInstance.Maintenance.html) |

### 7.2 Version Upgrade Paths — MySQL-family Cannot Skip Major Versions

| Aspect | Detail |
|--------|--------|
| **What breaks** | MySQL-family upgrades cannot skip major versions: **no direct 5.7 → 8.4**  — must go 5.7 → 8.0 → 8.4. Each major version upgrade requires prechecks and may require application changes. Aurora MySQL v2 → v3 is a major upgrade. Automatic minor version upgrades can be forced by AWS when a version reaches end-of-support. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Planning required** |
| **Workaround** | (1) Plan upgrade path early, (2) Test with Blue/Green Deployments, (3) Budget time for prechecks and application compatibility testing, (4) For Aurora: Use clones for testing upgrades. |
| **Source** | [AWS Known Issues](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) |

### 7.3 Storage Limits and Types

| Aspect | Detail |
|--------|--------|
| **What breaks** | **RDS storage limits**: gp2/gp3: 20 GiB–64 TiB, io1/io2: 100 GiB–64 TiB (io2 supports up to 256 TiB with additional volumes). **Storage cannot be decreased** once allocated — only increased. Storage autoscaling has thresholds. **Aurora storage**: Automatically scales up to 128 TiB per cluster. You cannot manually set storage size. Storage shrinks automatically when data is deleted (with dynamic resizing). |
| **Affected service** | RDS (EBS-backed), Aurora (custom storage) |
| **Severity** | 🟡 **Design consideration** |
| **Workaround** | For RDS: Enable storage autoscaling, set appropriate maximums. For Aurora: Storage is automatic — no action needed but understand that storage volume grows in 10 GB segments. |

### 7.4 IOPS Provisioning Differences

| Aspect | Detail |
|--------|--------|
| **What breaks** | **gp2**: IOPS tied to volume size (3 IOPS/GiB, burst to 3000), cannot provision independently. **gp3**: Baseline 3000 IOPS/125 MiB/s, can provision up to 64,000 IOPS independently. **io1**: Up to 64,000 IOPS. **io2**: Up to 256,000 IOPS. **Aurora**: IOPS are NOT user-provisioned — Aurora manages I/O automatically. Aurora I/O-Optimized pricing tier available for I/O-heavy workloads. Self-managed servers with local NVMe/SSD may have different IOPS profiles. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Performance tuning required** |
| **Workaround** | Benchmark on target instance class. For RDS: Choose appropriate storage type based on IOPS needs. For Aurora: I/O-Optimized cluster configuration for predictable costs. |

### 7.5 Backup Limitations

| Aspect | Detail |
|--------|--------|
| **What breaks** | (1) Automated backup retention: 0–35 days maximum (default 7), (2) Cannot take filesystem-level snapshots yourself, (3) Backups cannot overlap with maintenance window, (4) Cross-region automated backup replication limited to 20 per region, (5) Multi-AZ DB clusters do NOT support automated backup replication, (6) You cannot restore to the same instance — must create new instance from snapshot, (7) PITR (Point-in-Time Recovery) creates a new instance. |
| **Affected service** | RDS + Aurora |
| **Severity** | 🟡 **Operational adjustment** |
| **Workaround** | (1) Use AWS Backup for longer retention (up to 100 years) and cross-region/cross-account, (2) Manual snapshots have no retention limit (but count toward quota of 100), (3) Understand RTO: restore from snapshot creates new instance (DNS endpoint changes). |

### 7.6 Storage-Full Behavior (RDS MySQL)

| Aspect | Detail |
|--------|--------|
| **What breaks** | When storage becomes full, RDS automatically **STOPS** the DB instance to prevent metadata corruption. The instance becomes unavailable. Thresholds: <200 MiB free (for instances <20 GB), <1024 MiB free (>100 GB), or <1% free (20-100 GB). |
| **Affected service** | RDS MySQL |
| **Severity** | 🔴 **Can cause downtime** |
| **Workaround** | (1) Enable storage autoscaling with appropriate max, (2) Set CloudWatch alarms on `FreeStorageSpace`, (3) Monitor actively — unlike self-managed where the DB might slow down but remain accessible. |

---

## 8. Data Type, Charset & Timezone Issues

### 8.1 Character Set / Collation Issues During Migration

| Aspect | Detail |
|--------|--------|
| **What breaks** | (1) `lower_case_table_names=2` is NOT supported on RDS (only 0 or 1), (2) For MySQL 8.0/8.4: `lower_case_table_names` cannot be changed after instance creation, (3) Default charset changed from `latin1`→`utf8mb4` in MySQL 8.0 — if migrating from older self-managed instances, explicit charset settings are important, (4) Parameter group changes for charset require careful ordering (parameter group → create instance). |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Requires careful planning** |
| **Workaround** | (1) Set `lower_case_table_names` in parameter group BEFORE creating the instance, (2) Verify all table/column charsets match expectations, (3) Use `mysqldump --default-character-set=utf8mb4` for migration, (4) Test with production data queries before cutover. |
| **Source** | [AWS Known Issues](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html) |

### 8.2 Timezone Data Differences

| Aspect | Detail |
|--------|--------|
| **What breaks** | (1) RDS MySQL stores timestamps internally as UTC — timezone conversion happens at session level, (2) If migrating from a source database with non-UTC `system_time_zone`, timestamp data may appear shifted, (3) RDS populates timezone tables but they may not be updated as frequently as self-managed (use `mysql.rds_set_configuration` or parameter groups), (4) `TIMESTAMP` columns are affected by timezone settings during DMS migration. |
| **Affected service** | RDS MySQL + Aurora MySQL |
| **Severity** | 🟡 **Data integrity risk if not planned** |
| **Workaround** | (1) Ensure source and target timezone settings match during migration, (2) Use `DATETIME` instead of `TIMESTAMP` when timezone independence is needed, (3) Set session `time_zone` explicitly in applications, (4) For DMS migrations from non-UTC sources: see [AWS Knowledge Center guidance](https://www.repost.aws/knowledge-center/dms-migrate-mysql-non-utc). |

### 8.3 Spatial Data Type Support

| Aspect | Detail |
|--------|--------|
| **What breaks** | Spatial data types (`GEOMETRY`, `POINT`, `LINESTRING`, `POLYGON`) are generally supported on RDS MySQL and Aurora MySQL. However, Aurora parallel query now supports these types (since v3). Spatial indexes work. The main issue is with migration tools — some DMS versions handle spatial columns differently. |
| **Affected service** | Minimal impact |
| **Severity** | 🟢 **Generally supported** |
| **Workaround** | Test spatial queries after migration. Ensure DMS task settings handle spatial columns correctly. |

---

## 9. PostgreSQL-Specific Limitations

### 9.1 No Superuser Access

| Aspect | Detail |
|--------|--------|
| **What breaks** | The `postgres` superuser role is not available. Instead, `rds_superuser` is the highest-privileged role. Cannot: (1) `CREATE EXTENSION` for extensions not on the approved list, (2) Access `pg_hba.conf`, (3) Use `ALTER SYSTEM`, (4) Create C-language functions, (5) Load custom shared libraries, (6) Access the filesystem directly. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🟡 **Requires adjustment** — most DBA tasks have alternatives |
| **Workaround** | `rds_superuser` role can: create roles, databases, install supported extensions, manage replication slots, terminate backends. For extension management, use delegated extension support or Trusted Language Extensions (pg_tle). |

### 9.2 Unsupported/Unavailable PostgreSQL Extensions

| Aspect | Detail |
|--------|--------|
| **What breaks** | Only extensions listed in `rds.extensions` parameter are available. Notable MISSING extensions on RDS/Aurora PostgreSQL include: `pgrouting`, `timescaledb`, `citus` (community), `pg_partman` (available on some versions), custom C extensions, any extension requiring filesystem access or superuser. The list varies by PostgreSQL version. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🔴 **Blocker** if dependent on unavailable extensions |
| **Workaround** | (1) Check `SHOW rds.extensions;` to see available extensions, (2) Use Trusted Language Extensions (pg_tle) for custom functionality in safe languages (PL/pgSQL, JavaScript, Perl), (3) For unsupported extensions: rewrite functionality in application layer or available extensions. |

### 9.3 Custom C-Language Extensions — NOT SUPPORTED

| Aspect | Detail |
|--------|--------|
| **What breaks** | You cannot install any custom-compiled C extensions. `CREATE FUNCTION ... LANGUAGE C` requires superuser and filesystem access, both unavailable. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🔴 **Blocker** if using custom C extensions |
| **Workaround** | (1) Trusted Language Extensions (pg_tle) for custom PostgreSQL extensions in safe languages, (2) Rewrite in PL/pgSQL, PL/Python, or PL/Perl (available on RDS), (3) Move compute to Lambda/application layer. |

### 9.4 Logical Replication Restrictions

| Aspect | Detail |
|--------|--------|
| **What breaks** | RDS PostgreSQL supports logical replication but with restrictions: (1) Must set `rds.logical_replication=1` in parameter group, (2) Requires `wal_level=logical` (automatic when above is set), (3) `max_replication_slots` has limits per instance class, (4) Cannot use `pg_recvlogical` directly from the DB host. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🟡 **Requires configuration** |
| **Workaround** | Configure through parameter groups. Logical replication works well for migration scenarios (DMS uses it). Monitor replication slot growth. |

### 9.5 CHECKPOINT and Vacuum Control

| Aspect | Detail |
|--------|--------|
| **What breaks** | The `rds_superuser` can run `CHECKPOINT` but cannot tune checkpoint-related parameters beyond what's in parameter groups. Some vacuum parameters are adjusted by AWS automatically. You cannot run `VACUUM FULL` on system catalogs without `rds_superuser`. |
| **Affected service** | RDS PostgreSQL + Aurora PostgreSQL |
| **Severity** | 🟢 **Minor** |
| **Workaround** | `rds_superuser` role grants `CHECKPOINT` privilege. Tune vacuum via parameter groups (`autovacuum_*` parameters). |

---

## 10. Summary: Blocker vs. Adjustment Matrix

### 🔴 Hard Blockers (Must Resolve Before Migration)

| Item | Affected Service | Must Do |
|------|-----------------|---------|
| MySQL TDE / InnoDB Tablespace Encryption | RDS + Aurora | Switch to KMS volume encryption |
| MySQL Keyring Plugin | RDS + Aurora | Remove keyring dependencies |
| MyISAM tables | Aurora | Convert to InnoDB |
| Compressed tables (`ROW_FORMAT=COMPRESSED`) | Aurora | Decompress before migration |
| FEDERATED storage engine | RDS + Aurora | Redesign data access |
| Custom storage engines (TokuDB, RocksDB) | RDS + Aurora | Migrate to InnoDB |
| C-language UDFs | RDS + Aurora | Rewrite as stored functions or Lambda |
| Custom plugins | RDS + Aurora | Find managed alternatives |
| PAM/LDAP authentication | RDS + Aurora | Switch to IAM/Kerberos auth |
| Galera Cluster | RDS + Aurora | Architecture redesign |
| Third-party OS-level encryption agents | RDS + Aurora | Switch to AWS-native encryption |
| Korean DB encryption in plug-in / OS-volume mode (Petra Cipher, D'Amo DE/KE, CUBE-One) | RDS + Aurora | Switch to vendor API mode or KMS + app-side encryption — see [third-party-db-security.md](third-party-db-security.md) |
| Korean DB access/audit in sniffing / host-agent mode (Chakra Max, DBSafer agent, Petra sniffing) | RDS + Aurora | Vendor gateway mode in VPC + Database Activity Streams — see [third-party-db-security.md](third-party-db-security.md) |
| Custom C extensions (PostgreSQL) | RDS + Aurora | Use pg_tle or rewrite |
| Unsupported PostgreSQL extensions | RDS + Aurora | Verify with `SHOW rds.extensions` |

### 🟡 Requires Adjustment (Needs Work But Solvable)

| Item | Affected Service | Action |
|------|-----------------|--------|
| SUPER privilege loss | RDS + Aurora MySQL | Use `rds_superuser_role`, stored procedures |
| File system access (INFILE/OUTFILE) | RDS + Aurora | Use S3 integration, client-side loading |
| Binary log management | RDS + Aurora | Use RDS stored procedures |
| OS access (cron, scripts) | RDS + Aurora | EventBridge + Lambda |
| pg_hba.conf access | RDS + Aurora PG | Security Groups + parameter groups |
| Maintenance window patching | RDS + Aurora | Schedule + Multi-AZ + ZDP |
| Version upgrade path | RDS + Aurora | Plan sequential upgrades |
| Custom port 33060 blocked | RDS MySQL | Use different port |
| `lower_case_table_names=2` | RDS MySQL | Use 0 or 1 only |
| Persisted system variables | RDS + Aurora | Use Parameter Groups |
| Storage can only increase | RDS | Plan storage carefully, enable autoscaling |
| Timezone data migration | RDS + Aurora | Match timezone settings, test |
| Multi-source replication | Aurora MySQL | Use RDS MySQL or consolidate differently |

### 🟢 Works / Minor Differences

| Item | Notes |
|------|-------|
| pgcrypto | Fully supported on RDS/Aurora PostgreSQL |
| Kerberos authentication | Supported via AWS Managed AD |
| IAM database authentication | Supported for MySQL and PostgreSQL |
| Spatial data types | Supported |
| JSON functions (MySQL 8.0) | Supported |
| Custom ports (except 33060) | Supported |
| Read replicas (up to 15) | Supported |
| Replication filtering (Aurora v3) | Supported |
| TLS/SSL connections | Supported (TLS 1.2/1.3) |
| InnoDB buffer pool warming | Supported |

---

## References

- [RDS MySQL Known Issues and Limitations](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.KnownIssuesAndLimitations.html)
- [MySQL Feature Support on RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.FeatureSupport.html)
- [Aurora MySQL v2 vs MySQL 5.7](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.CompareMySQL57.html)
- [Aurora MySQL v3 (MySQL 8.0)](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.MySQL80.html)
- [Master User Account Privileges](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.MasterAccounts.html)
- [RDS Quotas and Constraints](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Limits.html)
- [Aurora MySQL Security](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Security.html)
- [Aurora PostgreSQL Extensions](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Extensions.html)
- [RDS PostgreSQL Common DBA Tasks](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.html)
- [Aurora MySQL Migration Prechecks](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.Prechecks.html)
- [RDS Storage Types](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Storage.html)
- [Aurora Maintenance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBInstance.Maintenance.html)
- [Trusted Language Extensions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL_trusted_language_extension.html)

---

*Document generated from AWS official documentation. Last verified: May 2026. Always check current AWS documentation for the latest information as features are regularly added.*
