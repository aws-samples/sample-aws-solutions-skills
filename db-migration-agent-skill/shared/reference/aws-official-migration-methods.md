# AWS Official Database Migration Methods to Amazon RDS & Amazon Aurora

## Comprehensive Reference Guide

**Source**: All information sourced exclusively from official AWS documentation  
**Last researched**: May 2026  
**Scope**: Every migration method documented by AWS for moving databases TO Amazon RDS and Amazon Aurora

---

## Table of Contents

1. [AWS Decision Guidance](#aws-decision-guidance)
2. [Aurora MySQL Migration Methods](#aurora-mysql-migration-methods)
3. [Aurora PostgreSQL Migration Methods](#aurora-postgresql-migration-methods)
4. [RDS for MySQL Migration Methods](#rds-for-mysql-migration-methods)
5. [RDS for PostgreSQL Migration Methods](#rds-for-postgresql-migration-methods)
6. [RDS for Oracle Migration Methods](#rds-for-oracle-migration-methods)
7. [RDS for SQL Server Migration Methods](#rds-for-sql-server-migration-methods)
8. [Cross-Engine & Universal Methods](#cross-engine--universal-methods)
9. [Quick Reference Matrix](#quick-reference-matrix)

---

## AWS Decision Guidance

### Physical vs. Logical Migration (AWS's Core Framework)

AWS categorizes all migration methods into two fundamental types:

**Physical Migration** (copies database files directly):
- Faster than logical migration, especially for large databases
- Database performance does not suffer when a backup is taken
- Can migrate everything in the source database, including complex database components
- **Limitations**: Requires `innodb_page_size` set to default (16KB), `innodb_data_file_path` configured with only one data file using default name "ibdata1:12M:autoextend", `innodb_log_files_in_group` must be set to default (2)

**Logical Migration** (applies logical database changes — inserts, updates, deletes):
- Can migrate subsets of the database (specific tables or parts of tables)
- Data can be migrated regardless of physical storage structure
- **Limitations**: Usually slower than physical migration; complex database components can slow or block migration

### AWS Prescriptive Guidance Decision Matrix

AWS provides a decision matrix comparing block-level replication (AWS Application Migration Service) vs. logical data-level replication (native tools or AWS DMS):

| Criteria | AWS Application Migration Service | Database Tools (Native/AWS DMS) |
|----------|----------------------------------|--------------------------------|
| Architecture | Physical (block level) | Logical, database engine level |
| Scale | Large-scale migration | Granular; scale limitations |
| Speed vs. complexity | Fast exit; reduced complexity | Slower, more complex; requires more planning |
| Timeline | Supports aggressive timeline | Requires additional effort and time |
| Migration type | Lift and shift (1:1 only) | Replatforming or modernization (1:many, many:1) |
| Pre-provisioning | Not required; automatic | Database and infra provisioning required |
| Downtime | Required, within RTO of minutes | Near-zero downtime possible (expensive) |

**Source**: https://docs.aws.amazon.com/prescriptive-guidance/latest/migration-database-rehost-tools/decision-matrix.html

### AWS's General Recommendations for PostgreSQL

From the official RDS documentation, AWS recommends native PostgreSQL tools when:
- You have a **homogeneous migration** (same database engine source and target)
- You are **migrating an entire database**
- The native tools allow you to migrate with **minimal downtime**

In most **other** cases, AWS states: *"performing a database migration using AWS Database Migration Service (AWS DMS) is the best approach"*

**Source**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.html

### AWS's MySQL-to-Aurora Decision Guide

From the DMS Step-by-Step Guide, AWS documents three approaches:
1. **Native/third-party tools for full load + MySQL replication for ongoing replication** — "Typically the simplest option"
2. **AWS DMS for full load + ongoing replication** — "provides migration-specific services such as data validation"
3. **Hybrid**: Native tools for full load + AWS DMS for ongoing replication — "delivers the simplicity of native tools along with additional DMS services"

**Source**: https://docs.aws.amazon.com/dms/latest/sbs/chap-manageddatabases.mysql2rds.html

---

## Aurora MySQL Migration Methods

### Method 1: Aurora Read Replica (from RDS for MySQL)

**Official AWS Name**: Migrating data from an RDS for MySQL DB instance to an Amazon Aurora MySQL DB cluster by using an Aurora read replica

**Migration Type**: Physical

**When AWS Recommends It**: 
- Migrating from an existing RDS for MySQL DB instance to Aurora MySQL
- AWS explicitly states: *"For migrating data from a MySQL DB Instance to an Amazon Aurora MySQL DB Cluster, we recommend to use a special type of node called an Aurora Read Replica"*
- Near-zero downtime requirement

**Prerequisites**:
- Source must be an RDS for MySQL DB instance
- Source must run MySQL 5.7 or higher compatible with target Aurora version
- Source DB instance must have automated backups enabled (backup retention > 0)
- Binary logging must be enabled on the source (set `binlog_format` to `ROW`)
- Source cannot be an Amazon Aurora DB instance
- Source cannot be a read replica itself
- Source cannot have cross-Region replicas

**Downtime Characteristics**:
- Near-zero downtime — only seconds of downtime during promotion
- Applications read from Aurora replica while it catches up
- When replica lag = 0, promote the replica to standalone cluster
- Promotion typically takes 1-2 minutes

**What Gets Migrated**:
- All data and schema objects from the source database
- InnoDB and MyISAM tables (MyISAM converted to InnoDB)

**What Doesn't Get Migrated**:
- The source DB instance itself remains unchanged
- Connection strings need updating after promotion

**Limitations**:
- Only available for RDS for MySQL → Aurora MySQL
- Cannot use from external MySQL databases
- Source cannot already have the maximum number of read replicas (15)
- Aurora read replica uses same instance class as source or larger
- Cross-Region Aurora read replicas require publicly accessible instances or VPC peering

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.RDSMySQL.Replica.html

---

### Method 2: RDS for MySQL Snapshot Migration to Aurora

**Official AWS Name**: Migrating an RDS for MySQL snapshot to Aurora

**Migration Type**: Physical

**When AWS Recommends It**:
- When you have an existing RDS for MySQL DB instance and can tolerate downtime
- As an alternative to the read replica method when near-zero downtime is not required
- When you want to create a new Aurora cluster from an existing RDS MySQL state

**Prerequisites**:
- Source must be an RDS for MySQL DB snapshot (manual or automated)
- MySQL version must be compatible with Aurora MySQL
- DB snapshot must be in the same AWS Region as target (or copied to that Region)
- Sufficient EBS volume space for format conversion during migration
- MyISAM and compressed tables must not exceed 8 TB individually

**Downtime Characteristics**:
- Full downtime during migration
- Duration depends on database size — includes snapshot creation + data format conversion + Aurora cluster creation
- No writes to source during migration period

**What Gets Migrated**:
- Complete database state at snapshot time
- All database objects, data, InnoDB tables
- MyISAM tables (converted to InnoDB)
- Compressed tables (expanded)

**What Doesn't Get Migrated**:
- Any changes after snapshot creation
- Source DB instance remains running (but data diverges)

**Limitations**:
- MyISAM tables require additional space during conversion (must not exceed 8 TB)
- Compressed tables (`ROW_FORMAT=COMPRESSED`) require additional space during expansion
- The `innodb_page_size` must be default (16KB)
- One snapshot copy per AWS account per Region at a time
- Cannot migrate from Aurora MySQL version < 8.0.11, 8.0.13, or 8.0.15 to Aurora MySQL 3.05+

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.RDSMySQL.Import.html

---

### Method 3: Percona XtraBackup + Amazon S3 (from External MySQL)

**Official AWS Name**: Physical migration from MySQL by using Percona XtraBackup and Amazon S3

**Migration Type**: Physical

**When AWS Recommends It**:
- Migrating from an external (on-premises or EC2) MySQL database to Aurora MySQL
- *"This option can be considerably faster than migrating data using mysqldump"*
- Large databases where speed is critical
- AWS DMS Step-by-Step guide states it's appropriate when: dataset is large, you have admin/system-level access to source, and you migrate 1-to-1 (one source = one target)

**Prerequisites**:
- Source must be MySQL 5.7 or 8.0
- Must use Percona XtraBackup version compatible with source MySQL version
- `innodb_page_size` set to default (16KB)
- `innodb_data_file_path` = `ibdata1:12M:autoextend` (single default data file)
- Source must support InnoDB or MyISAM tablespaces
- An Amazon S3 bucket for backup storage
- IAM role with S3 access for Aurora

**Downtime Characteristics**:
- Downtime from the point of final backup until Aurora cluster is available
- Can use binlog replication to sync Aurora cluster with source after restore (reducing effective downtime)
- AWS documents a "Synchronizing the Aurora MySQL DB cluster with the MySQL database" step for CDC after restore

**What Gets Migrated**:
- Complete physical database files
- All InnoDB tables and data
- All schema objects

**What Doesn't Get Migrated**:
- Changes after the backup unless binlog replication is configured post-restore
- Database users/permissions may need manual recreation

**Limitations**:
- Cannot migrate into an **existing** Aurora DB cluster — creates a new one
- Cannot migrate **multiple** source MySQL servers into a single Aurora DB cluster
- `innodb_data_file_path` cannot have two data files or non-default names
- `innodb_log_files_in_group` must be default (2)
- Cannot use if third-party software restricted by OS limitations
- Source database must use InnoDB or MyISAM tablespaces
- Cannot migrate to Aurora MySQL 3.05+ from MySQL 8.0.11, 8.0.13, or 8.0.15

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.S3.html

---

### Method 4: mysqldump (Logical Migration from External MySQL)

**Official AWS Name**: Logical migration from MySQL to Amazon Aurora MySQL by using mysqldump

**Migration Type**: Logical

**When AWS Recommends It**:
- Migrating from an external MySQL database to Aurora MySQL
- Smaller databases (AWS DMS guide says appropriate when dataset < 10 GB)
- When network connection between source and target is fast and stable
- When migration time is not critical
- When you don't need intermediate schema/data transformations
- When you need to migrate subsets of data

**Prerequisites**:
- Source must be MySQL-compatible
- Network connectivity between source and target (or dump file transfer mechanism)
- Aurora MySQL DB cluster must already exist
- Source database must support InnoDB or MyISAM tablespaces

**Downtime Characteristics**:
- Downtime during dump and restore for basic approach
- **Reduced downtime approach available**: mysqldump + binary log replication (documented separately)
  - Initial dump with `--master-data=2` captures binlog position
  - After restore, configure replication from source to catch up
  - Cut over when replica lag = 0

**What Gets Migrated**:
- Database schema and data (logical SQL statements)
- Can selectively include/exclude objects

**What Doesn't Get Migrated (by default)**:
- Routines/stored procedures — `mysqldump` **excludes routines by default**; pass `--routines` to include them
- Events — excluded by default; pass `--events` to include them
- Triggers — **included by default**; only excluded if you explicitly pass `--skip-triggers`
- System schemas (mysql, performance_schema, information_schema) are always excluded

**Recommended flags for a complete logical migration**: `mysqldump --single-transaction --routines --triggers --events --set-gtid-purged=OFF --column-statistics=0 ...` — this makes intent explicit and captures everything DMS would skip.

**Limitations**:
- Slower than physical migration for large databases
- Complex database components can slow or block migration
- Performance depends on network bandwidth
- The `mysqlpump` utility is deprecated as of MySQL 8.0.34
- If source uses memcached, must remove it before migration

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.mysqldump.html

---

### Method 5: LOAD DATA FROM S3 (Text File Import)

**Official AWS Name**: Loading data into an Amazon Aurora MySQL DB cluster from text files in an Amazon S3 bucket

**Migration Type**: Logical

**When AWS Recommends It**:
- Loading data from flat files into an existing Aurora MySQL DB cluster
- When data is available as text/CSV files in S3
- Supplemental data loading (not necessarily full database migration)
- In Aurora MySQL version 3 (MySQL 8.0-compatible), version 3.05 and higher

**Prerequisites**:
- Aurora MySQL DB cluster must already exist
- Data files in Amazon S3 bucket
- IAM role associated with the Aurora cluster granting S3 access
- The `aws_default_s3_role` or role ARN must be configured
- User must have `LOAD FROM S3` privilege granted via: `GRANT LOAD FROM S3 ON *.* TO 'user'@'domain-or-ip-address'`
- Target tables must already exist

**Downtime Characteristics**:
- No downtime for existing database — additive operation
- Loads data into existing tables
- Can run while database is online

**What Gets Migrated**:
- Data from text files (CSV, TSV, or custom-delimited)
- XML data (via LOAD XML FROM S3)

**What Doesn't Get Migrated**:
- Schema/table definitions (must pre-create)
- Stored procedures, triggers, events
- Users and privileges
- Indexes must be pre-created (or added after load)

**Limitations**:
- In Aurora MySQL version 3 (3.01 to 3.04): must grant `AWS_LOAD_S3_ACCESS` role to user
- In Aurora MySQL version 2: must grant `LOAD FROM S3` privilege
- Cannot load data from an S3 bucket in a different AWS Region
- Each `LOAD DATA FROM S3` statement loads from one manifest or one data file
- The database must have sufficient space for the data being loaded
- Uses MySQL `LOAD DATA INFILE` syntax with S3 URI

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Integrating.LoadFromS3.html

---

### Method 6: Binary Log (Binlog) Replication from External MySQL

**Official AWS Name**: Replication between Aurora and MySQL or between Aurora and another Aurora DB cluster (binary log replication)

**Migration Type**: Logical (continuous replication)

**When AWS Recommends It**:
- Setting up ongoing replication from external MySQL to Aurora MySQL
- Near-zero downtime migration from external MySQL
- Can be combined with other methods (e.g., Percona XtraBackup for initial load, then binlog replication for CDC)
- Cross-Region replication scenarios

**Prerequisites**:
- MySQL source version 5.5 or later recommended
- Binary logging enabled on source
- Only InnoDB tables (MyISAM must be converted first)
- Network connectivity between source and Aurora
- For Aurora MySQL version 2 and 3: supports GTID-based replication
- GTID-related parameters must be compatible between source and target

**Downtime Characteristics**:
- Near-zero downtime when combined with initial data load method
- Continuous replication keeps target in sync
- Cut over when replica lag = 0

**What Gets Migrated**:
- All data changes (inserts, updates, deletes) via binary log
- DDL changes

**What Doesn't Get Migrated**:
- Initial data (must use another method for baseline)
- Non-InnoDB tables cannot be replicated

**Limitations**:
- Not available for Aurora Serverless v1 clusters
- Only InnoDB tables supported
- Cross-Region requires publicly accessible instances or VPC peering
- Cannot use with Aurora Global Database as target

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Replication.MySQL.html

---

### Method 7: AWS DMS (for non-MySQL-compatible sources)

**Official AWS Name**: AWS Database Migration Service (AWS DMS)

**Migration Type**: Logical

**When AWS Recommends It**:
- Migrating from a database that is **not MySQL-compatible** to Aurora MySQL
- Heterogeneous migrations (e.g., Oracle → Aurora MySQL, SQL Server → Aurora MySQL)
- When you need data validation during migration
- When you need ongoing replication/CDC from any supported source

**Prerequisites**:
- DMS replication instance
- Source and target endpoints configured
- For heterogeneous migrations: AWS Schema Conversion Tool (SCT) for schema conversion
- Network connectivity from DMS instance to both source and target

**Downtime Characteristics**:
- Near-zero downtime possible with CDC
- Full load causes brief outage on target
- Full load + CDC: initial load followed by continuous replication

**What Gets Migrated**:
- Tables and primary keys (auto-created if not existing on target)
- Data (full load and/or ongoing changes)
- With SCT: schema objects including indexes, views, triggers

**What Doesn't Get Migrated**:
- Secondary indexes (not auto-created during full load for performance)
- Some database-specific objects require SCT
- Stored procedures, triggers may need manual migration or SCT

**Limitations**:
- Requires DMS replication instance (additional cost)
- Performance depends on replication instance size and network
- Some complex data types may not convert perfectly
- Check DMS documentation for source/target specific limitations

**Documentation**: https://docs.aws.amazon.com/dms/latest/userguide/Welcome.html

---

### Method 8: Aurora Console Auto-Migration (from EC2 databases)

**Official AWS Name**: Auto migrating EC2 databases to Amazon Aurora using AWS Database Migration Service

**Migration Type**: Logical (uses DMS under the covers)

**When AWS Recommends It**:
- Migrating MySQL databases running on EC2 instances to Aurora
- Source databases smaller than 1 TiB
- Simplifying the DMS setup process

**Prerequisites**:
- Source must be MySQL on EC2
- Source and target must be in the same VPC
- Aurora MySQL DB cluster must already exist
- MySQL 5.7 or higher on source
- DMS user with REPLICATION CLIENT and REPLICATION SLAVE privileges (for CDC)
- SELECT privileges on source tables

**Downtime Characteristics**:
- Three options:
  - **Full load**: Causes outage on Aurora database during load
  - **Full load + CDC**: Causes initial outage, then continuous replication
  - **CDC only**: No outage — continuous change replication

**What Gets Migrated**:
- Database tables and data
- Ongoing changes (with CDC option)

**Limitations**:
- Cannot migrate to: Aurora Global Database, Aurora Limitless Database, Aurora Serverless v1
- Cannot migrate from MySQL versions lower than 5.7
- EC2 instance and target must be in same VPC
- Maximum recommended source size: 1 TiB

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_DMS_migration.html

---

## Aurora PostgreSQL Migration Methods

### Method 9: RDS for PostgreSQL Snapshot Migration to Aurora PostgreSQL

**Official AWS Name**: Migrating an RDS for PostgreSQL DB instance using a snapshot

**Migration Type**: Physical

**When AWS Recommends It**:
- Migrating from an existing RDS for PostgreSQL DB instance to Aurora PostgreSQL
- When downtime is acceptable
- Simplest path from RDS PostgreSQL to Aurora PostgreSQL

**Prerequisites**:
- Source must be an RDS for PostgreSQL DB snapshot
- PostgreSQL version must be supported by Aurora PostgreSQL
- Strongly recommended: turn off auto minor version upgrades early in migration planning
- DB snapshot must be in same Region (or copied)

**Downtime Characteristics**:
- Full downtime during snapshot creation and restoration
- Duration proportional to database size

**What Gets Migrated**:
- Complete database state at snapshot time
- All data and schema objects

**What Doesn't Get Migrated**:
- Changes after snapshot time
- Kerberos authentication settings (cannot be enabled during migration)

**Limitations**:
- Migration may be delayed if RDS for PostgreSQL version isn't yet supported by Aurora PostgreSQL
- Kerberos authentication can only be enabled on standalone Aurora PostgreSQL cluster (not during migration)

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.html

---

### Method 10: Aurora Read Replica (from RDS for PostgreSQL)

**Official AWS Name**: Migrating data from an RDS for PostgreSQL DB instance to an Aurora PostgreSQL DB cluster using an Aurora read replica

**Migration Type**: Physical

**When AWS Recommends It**:
- Near-zero downtime migration from RDS for PostgreSQL to Aurora PostgreSQL
- When you need continuous sync before cutover
- AWS recommends this for minimal downtime scenarios

**Prerequisites**:
- Source must be RDS for PostgreSQL DB instance
- Source version must be compatible with Aurora PostgreSQL
- Automated backups enabled on source
- Source cannot already be at max read replica limit
- Auto minor version upgrades should be disabled early in planning

**Downtime Characteristics**:
- Near-zero downtime
- Only brief interruption during promotion (seconds)
- Monitor replica lag; promote when lag = 0

**What Gets Migrated**:
- Complete database via physical replication
- All data and schema objects
- Ongoing changes until promotion

**What Doesn't Get Migrated**:
- Kerberos authentication (must enable post-migration)
- Application connection strings need updating

**Limitations**:
- Only available from RDS for PostgreSQL (not external PostgreSQL)
- Migration may be delayed if version mismatch
- Same Region only for initial creation

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.RDSPostgreSQL.Replica.html

---

### Method 11: Import from Amazon S3 into Aurora PostgreSQL

**Official AWS Name**: Importing data from Amazon S3 into Aurora PostgreSQL

**Migration Type**: Logical

**When AWS Recommends It**:
- Loading data from S3 files into an existing Aurora PostgreSQL cluster
- Bulk data loading scenarios
- When data is available in S3 (e.g., exported from another system)

**Prerequisites**:
- Aurora PostgreSQL DB cluster must already exist
- Data files in Amazon S3
- IAM role with S3 access associated with the cluster
- `aws_s3` extension installed
- Target tables must already exist

**Downtime Characteristics**:
- No downtime — additive operation on existing cluster
- Can run while database is online

**What Gets Migrated**:
- Data from files in S3 into existing tables

**What Doesn't Get Migrated**:
- Schema definitions
- Database objects (procedures, functions, etc.)

**Limitations**:
- Target tables must be pre-created
- Data format must match table structure
- S3 bucket must be in accessible Region

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.html (referenced from main migration page)

---

### Method 12: AWS DMS (for non-PostgreSQL sources to Aurora PostgreSQL)

**Official AWS Name**: AWS Database Migration Service (for non-PostgreSQL-compatible sources)

**Migration Type**: Logical

**When AWS Recommends It**:
- Migrating from a database that is **not PostgreSQL-compatible** to Aurora PostgreSQL
- Heterogeneous migrations

**(Same DMS capabilities as described in Method 7 above, targeting Aurora PostgreSQL)**

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.html

---

## RDS for MySQL Migration Methods

### Method 13: Restore Backup from S3 (Percona XtraBackup)

**Official AWS Name**: Restoring a backup into an Amazon RDS for MySQL DB instance

**Migration Type**: Physical

**When AWS Recommends It**:
- *"If you are importing or exporting large amounts of data with a MySQL DB instance, it's more reliable and faster to move data in and out of Amazon RDS by using xtrabackup backup files and Amazon S3"*
- Large-scale external MySQL to RDS MySQL migrations

**Prerequisites**:
- Source MySQL database (external)
- Percona XtraBackup for full and incremental backups
- Amazon S3 bucket to store backup files
- IAM role granting RDS access to S3
- Source must be MySQL (not MariaDB — "Amazon RDS only supports importing from Amazon S3 for MySQL")

**Downtime Characteristics**:
- Downtime from point of backup to when new RDS instance is available
- Can use binary log replication after restore for CDC (reduced effective downtime)

**What Gets Migrated**:
- Complete physical database backup
- All InnoDB data and schema

**What Doesn't Get Migrated**:
- Changes after backup (unless binlog replication configured)
- Requires new RDS instance creation

**Limitations**:
- Creates a **new** RDS instance — cannot restore into existing instance
- Not supported for MariaDB — only MySQL
- File size and format constraints apply

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.html

---

### Method 14: mysqldump (Direct Import from External MySQL)

**Official AWS Name**: Importing data from an external MySQL database to an Amazon RDS for MySQL DB instance

**Migration Type**: Logical

**When AWS Recommends It**:
- Importing data from existing MariaDB or MySQL database to RDS
- Piping directly from source to target over network
- Simpler databases, smaller sizes

**Prerequisites**:
- mysqldump utility installed (included with MySQL client)
- Network connectivity from client to both source and target databases
- User access on both source and target
- For MariaDB 11.0.1+: must use `mariadb-dump` instead of `mysqldump`

**Downtime Characteristics**:
- Source remains available during dump (with `--single-transaction`)
- Target receives data during pipe
- Full consistency at a single point in time

**What Gets Migrated**:
- Database schema and table data
- Specified databases (`--databases` parameter)

**What Doesn't Get Migrated (by default)**:
- Routines/stored procedures — excluded by default; pass `--routines` to include
- Events — excluded by default; pass `--events` to include
- Triggers — **included by default**; use `--skip-triggers` to exclude
- System schemas (sys, performance_schema, information_schema) are always excluded

**Limitations**:
- Not suitable for very large databases (slow over network)
- Requires stable network connection
- Cannot include routines/triggers directly (must recreate on RDS)

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.SmallExisting.html

---

### Method 15: Reduced Downtime Import (mysqldump + Binary Log Replication)

**Official AWS Name**: Importing data to an Amazon RDS for MySQL database with reduced downtime

**Migration Type**: Logical (with ongoing replication)

**When AWS Recommends It**:
- When importing from an external MySQL/MariaDB database that supports a **live application**
- Very large databases
- When you need to minimize impact on application availability
- *"Use the following procedure to minimize the impact on availability of applications"*

**Prerequisites**:
- External MariaDB or MySQL source database
- Binary logging enabled on source
- `mysqldump` with `--master-data=2` and `--single-transaction`
- RDS for MySQL target instance (or Multi-AZ DB cluster)
- VPN, AWS Direct Connect, or network path between source and target
- For replication: `mysql.rds_set_external_master` stored procedure

**Downtime Characteristics**:
- Reduced downtime — only during final cutover
- Initial dump is consistent snapshot (source remains online)
- Binary log replication keeps target in sync after initial load
- Cut over when `Seconds_Behind_Master` = 0

**What Gets Migrated**:
- Full database via mysqldump (initial load)
- All subsequent changes via binary log replication
- Complete data synchronization before cutover

**What Doesn't Get Migrated**:
- Stored procedures, triggers, events (must recreate manually)
- System schemas

**Limitations**:
- Requires configuring external replication (`mysql.rds_set_external_master`)
- Must stop replication and reset with `mysql.rds_reset_external_master` after cutover
- Network stability between source and target is critical during replication
- Replication lag monitoring is essential before cutover

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.NonRDSRepl.html

---

### Method 16: LOAD DATA LOCAL INFILE (Flat File Import)

**Official AWS Name**: Importing data from any source to an Amazon RDS for MySQL DB instance

**Migration Type**: Logical

**When AWS Recommends It**:
- Loading data from flat files (CSV, text) into RDS MySQL
- When data comes from any source (not necessarily MySQL)
- Bulk loading scenarios

**Prerequisites**:
- Flat files in CSV or similar format
- Files split to < 1 GiB each (recommended)
- Data ordered by primary key (recommended for performance)
- mysql shell access to RDS instance
- Target tables must already exist

**Downtime Characteristics**:
- AWS recommends: "Stop any applications accessing the target DB instance" before load
- Turning off automated backups reduces load time by ~25%
- Load time depends on file sizes and network

**What Gets Migrated**:
- Data from flat files into existing tables

**What Doesn't Get Migrated**:
- Schema (must pre-create tables)
- Database objects (procedures, triggers, etc.)

**Limitations**:
- Files > 1 GiB should be split into multiple files
- Must invoke mysql shell from same location as files (or use absolute path)
- Turning off backups erases existing backups and disables point-in-time recovery
- DB instance restart required when changing backup settings

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.AnySource.html

---

## RDS for PostgreSQL Migration Methods

### Method 17: pg_dump / pg_restore (from EC2 or External PostgreSQL)

**Official AWS Name**: Importing a PostgreSQL database from an Amazon EC2 instance

**Migration Type**: Logical

**When AWS Recommends It**:
- Homogeneous PostgreSQL migration
- Migrating entire database
- When native tools provide minimal downtime

**Prerequisites**:
- `pg_dump` utility available
- Network connectivity to target RDS instance
- Target RDS instance created with backup retention = 0 and Multi-AZ disabled (for faster import)
- Cannot use `pg_dumpall` (requires super_user not available in RDS)

**Downtime Characteristics**:
- Source available during dump (consistent snapshot)
- Duration proportional to database size
- Recommended optimizations: disable backups, disable Multi-AZ during import

**What Gets Migrated**:
- Database schema (tables, indexes, foreign keys)
- All table data
- Can restore to same-name or different-name database

**What Doesn't Get Migrated**:
- Data requiring `pg_dumpall` (global objects like roles across databases)
- Changes after dump unless replication configured

**Recommended Parameters During Import** (from AWS docs):
| Parameter | Recommended Value | Purpose |
|-----------|------------------|---------|
| `maintenance_work_mem` | 512MB - 4GB | Speed up CREATE INDEX |
| `max_wal_size` | 256 (v9.6) / 4096 (v10+) | Less frequent checkpoints |
| `checkpoint_timeout` | 1800 | Less frequent WAL rotation |
| `synchronous_commit` | Off | Speed up writes |
| `wal_buffers` | 8192 | WAL generation speed |
| `autovacuum` | 0 | Don't consume resources during load |

**Limitations**:
- `pg_dumpall` not usable (no super_user in RDS)
- Must revert parameter changes after import
- Must re-enable backups and Multi-AZ after import

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.EC2.html

---

### Method 18: \copy Command (psql Meta-Command)

**Official AWS Name**: Using the \copy command to import data to a table on a PostgreSQL DB instance

**Migration Type**: Logical

**When AWS Recommends It**:
- Importing CSV data into existing tables
- Client-side data loading
- When data is available as CSV files on local workstation

**Prerequisites**:
- `psql` client connected to target RDS PostgreSQL instance
- CSV files on local workstation
- Target tables must already exist with correct structure

**Downtime Characteristics**:
- No downtime — additive operation
- Loads data while database is online

**What Gets Migrated**:
- Data from CSV files into specified tables

**What Doesn't Get Migrated**:
- Schema definitions
- Any database objects

**Limitations**:
- Client-side operation (data goes through client machine)
- Limited by client network bandwidth
- Tables must be pre-created

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.Copy.html

---

### Method 19: Import from Amazon S3 into RDS for PostgreSQL

**Official AWS Name**: Importing data from Amazon S3 into an RDS for PostgreSQL DB instance

**Migration Type**: Logical

**When AWS Recommends It**:
- Bulk loading data from S3 into RDS PostgreSQL
- When data is already in S3 (exported from another system)
- Server-side import (faster than client-side \copy)

**Prerequisites**:
- `aws_s3` extension installed on RDS PostgreSQL
- IAM role with S3 access associated with the DB instance
- Data files in S3 bucket
- Target tables must exist

**Downtime Characteristics**:
- No downtime — additive operation
- Server-side operation (bypasses client)

**What Gets Migrated**:
- Data from S3 files into existing tables

**What Doesn't Get Migrated**:
- Schema definitions
- Database objects

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.html (subsection)

---

### Method 20: PostgreSQL Transportable Databases (pg_transport)

**Official AWS Name**: Transporting PostgreSQL databases between DB instances

**Migration Type**: Physical

**When AWS Recommends It**:
- *"A very fast way to migrate large databases between different DB instances"*
- Moving databases between RDS for PostgreSQL instances
- When speed is critical for large databases

**Prerequisites**:
- Both source and destination must be RDS for PostgreSQL instances
- Both must run the **same major version** of PostgreSQL
- `pg_transport` extension installed on both instances
- Source can only have `pg_transport` extension installed (no other extensions)
- All source database objects must be in default `pg_default` tablespace
- Available in RDS for PostgreSQL 11.5+, and 10.10+

**Downtime Characteristics**:
- Source database is put into **read-only mode** during transport
- Source database sessions are ended when transport begins
- Source database allows read-only queries during transport
- Write-enabled queries blocked on source during transport
- Destination not available for point-in-time recovery during transport (backup taken after)

**What Gets Migrated**:
- Complete database via physical transport (file-level copy)
- Much faster than dump and load

**What Doesn't Get Migrated**:
- Access privileges and ownership (not carried over)
- All objects created and owned by destination user
- `reg` data types (depend on OIDs that change during transport)

**Limitations**:
- **RDS for PostgreSQL instances only** — cannot use with on-premises or EC2 databases
- Cannot use on read replicas or parent instances of read replicas
- Cannot use `reg` data types in transported tables
- All objects must be in `pg_default` tablespace
- Both instances must be same PostgreSQL major version
- Source can only have `pg_transport` extension
- Up to 32 concurrent transports per instance
- If transport fails, source may remain in read-only mode (requires manual fix)

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.TransportableDB.html

---

## RDS for Oracle Migration Methods

### Method 21: Oracle SQL Developer

**Official AWS Name**: Importing using Oracle SQL Developer

**Migration Type**: Logical

**When AWS Recommends It**:
- Small databases (~20 MB)
- Simple migrations
- Using the Database Copy command

**Prerequisites**:
- Oracle SQL Developer installed (free from Oracle)
- Network connectivity to both source and target
- Credentials for both databases

**Downtime Characteristics**:
- Source remains available during copy
- Duration depends on database size

**What Gets Migrated**:
- Data and schema (via Database Copy command)
- Can also migrate from MySQL/SQL Server to Oracle

**Limitations**:
- Best for **small databases** only
- GUI-based tool

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.SQLDeveloper.html

---

### Method 22: Oracle Data Pump

**Official AWS Name**: Importing using Oracle Data Pump

**Migration Type**: Logical

**When AWS Recommends It**:
- *"The recommended way to move large amounts of data from an Oracle database"*
- Complex databases
- Databases of several hundred megabytes to several terabytes
- Long-term replacement for Oracle Export/Import utilities

**Two Sub-Methods**:
1. **Oracle Data Pump with Amazon S3**: Export dump file, upload to S3, import via S3 integration
2. **Oracle Data Pump with Database Link**: Direct import over database link from source to target

**Prerequisites**:
- Oracle Data Pump utilities (expdp/impdp)
- For S3 method: S3 bucket, IAM role, Amazon S3 integration enabled on RDS instance
- For DB Link method: Network connectivity and database link between source and target
- Sufficient storage for dump files

**Downtime Characteristics**:
- Source remains available during export
- Import duration depends on data volume
- No built-in CDC — point-in-time migration

**What Gets Migrated**:
- Full schemas or specific objects
- Data, indexes, constraints
- PL/SQL objects (when compatible)

**Limitations**:
- Dump file size constraints based on instance storage
- Not all Oracle features supported identically in RDS
- Database link requires network path between source and RDS

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.DataPump.html

---

### Method 23: Oracle Transportable Tablespaces

**Official AWS Name**: Migrating using Oracle transportable tablespaces

**Migration Type**: Physical

**When AWS Recommends It**:
- Moving tablespaces from on-premises Oracle to RDS for Oracle
- Large datasets where Data Pump would be too slow
- Can use Amazon S3 or Amazon EFS for data file transfer

**Prerequisites**:
- Amazon S3 integration or Amazon EFS integration configured
- Compatible Oracle versions
- Tablespace in read-only mode for transport

**What Gets Migrated**:
- Complete tablespace data files
- Metadata via Data Pump

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.html (references transportable tablespaces)

---

### Method 24: Oracle SQL*Loader

**Official AWS Name**: Importing using Oracle SQL*Loader

**Migration Type**: Logical

**When AWS Recommends It**:
- *"Large databases that contain a limited number of objects"*
- Bulk loading data from flat files
- When schema is relatively simple

**Prerequisites**:
- Oracle SQL*Loader utility (via Oracle Instant Client)
- Control file defining data format
- Data exported as flat files from source
- Target tables must exist on RDS Oracle

**Downtime Characteristics**:
- No downtime on target — additive operation
- Source export may require brief lock

**What Gets Migrated**:
- Data from flat files into existing tables

**What Doesn't Get Migrated**:
- Schema (must pre-create)
- PL/SQL objects
- Indexes (must pre/post-create)

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.SQLLoader.html

---

### Method 25: Oracle Materialized Views

**Official AWS Name**: Migrating with Oracle materialized views

**Migration Type**: Logical (with ongoing replication)

**When AWS Recommends It**:
- *"To migrate large datasets efficiently"*
- When you need ongoing synchronization before cutover
- When you want to switch over to Amazon RDS later

**Prerequisites**:
- Database link from RDS Oracle target to source database
- Access rules on source allowing RDS target to connect via SQL*Net
- User account on both source and target with same password
- Materialized view log on source tables
- `CREATE SESSION`, `SELECT ANY TABLE`, `SELECT ANY DICTIONARY` privileges

**Downtime Characteristics**:
- Near-zero downtime
- Materialized views keep target synchronized
- Cut over by: refreshing final time → dropping materialized view with `PRESERVE TABLE`
- The retained table has same name as dropped materialized view

**What Gets Migrated**:
- Table data via materialized view replication
- Ongoing changes until cutover

**What Doesn't Get Migrated**:
- Schema objects other than tables (must create separately)
- PL/SQL packages, procedures

**Limitations**:
- Requires direct network access from RDS to source
- Source must have materialized view logs created
- Only works for Oracle-to-Oracle migration

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.Materialized.html

---

### Method 26: Oracle Export/Import (Legacy)

**Official AWS Name**: Importing using Oracle Export/Import

**Migration Type**: Logical

**When AWS Recommends It**:
- Legacy method; Oracle Data Pump is the recommended replacement
- May be needed for very old Oracle versions

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.html

---

## RDS for SQL Server Migration Methods

### Method 27: Native Backup and Restore (.bak files via S3)

**Official AWS Name**: Importing and exporting SQL Server databases using native backup and restore

**Migration Type**: Physical

**When AWS Recommends It**:
- *"If your database can be offline while the backup file is created, copied, and restored, we recommend that you use native backup and restore to migrate it to RDS"*
- *"Usually the fastest way to back up and restore databases"*
- When database can tolerate downtime during migration

**Prerequisites**:
- Full backup file (.bak) from source SQL Server
- Amazon S3 bucket to store backup files
- IAM role with S3 access for RDS
- Option group with `SQLSERVER_BACKUP_RESTORE` option configured
- Symmetric encryption AWS KMS key (if encrypting)

**Downtime Characteristics**:
- Database offline from backup start to restore completion
- AWS states this is the fastest method when offline is acceptable

**What Gets Migrated**:
- Complete database including: data, schemas, stored procedures, triggers, and other database code
- Single databases (not entire instance)
- TDE-encrypted databases supported

**What Doesn't Get Migrated**:
- Instance-level objects
- SQL Server Agent jobs
- Linked servers
- Login information (at instance level)

**Limitations**:
- Cannot backup/restore from S3 in different AWS Region than the RDS instance
- Cannot restore a database with same name as existing database
- Maximum 5 TB per file; larger databases use multifile backup
- Maximum 10 backup files simultaneously
- Cannot do native log backups from RDS SQL Server
- On Multi-AZ: can only natively restore databases backed up in **full recovery model**
- Up to 2 backup/restore tasks simultaneously
- Cannot restore during maintenance window
- FILESTREAM file groups not supported
- Differential backups cannot have snapshots between full backup and differential

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Procedural.Importing.html

---

### Method 28: SQL Server Import via Snapshot (Generate and Publish Scripts / Import Wizard)

**Official AWS Name**: Importing data into RDS for SQL Server by using a snapshot

**Migration Type**: Logical

**When AWS Recommends It**:
- Alternative to native backup when database cannot be offline
- Using SQL Server tools for migration

**Sub-Methods**:
- **Generate and Publish Scripts Wizard**: Creates SQL scripts for schema + data
- **Import and Export Wizard**: SSIS-based data movement
- **Bulk Copy (BCP)**: Command-line bulk data transfer

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Procedural.Importing.html

---

### Method 29: BCP Utility (from Linux)

**Official AWS Name**: Using BCP utility from Linux to import and export data

**Migration Type**: Logical

**When AWS Recommends It**:
- Bulk importing/exporting data to/from RDS for SQL Server from Linux environments
- When native backup isn't suitable

**Prerequisites**:
- SQL Server command-line tools installed on Linux
- `mssql-tools` package
- Network connectivity to RDS SQL Server instance

**Documentation**: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Procedural.Importing.html

---

## Cross-Engine & Universal Methods

### Method 30: AWS Database Migration Service (AWS DMS) — Universal

**Official AWS Name**: AWS Database Migration Service

**Migration Type**: Logical

**When AWS Recommends It**:
- Heterogeneous migrations (different source and target engines)
- When near-zero downtime is required
- When you need **data validation** during migration
- When you need ongoing replication
- *"In most other cases, performing a database migration using AWS DMS is the best approach"*
- *"If your on-premises database can't be offline, we recommend that you use AWS Database Migration Service"*

**Prerequisites**:
- DMS replication instance in same VPC or with connectivity to target
- Source and target endpoints
- For heterogeneous: AWS Schema Conversion Tool (SCT) or DMS Schema Conversion
- Network connectivity (VPN, Direct Connect, or public internet)

**Downtime Characteristics**:
- **Full load only**: Downtime during load
- **Full load + CDC**: Brief downtime during initial load, then continuous replication
- **CDC only**: No downtime (requires pre-loaded baseline)
- Near-zero downtime achievable with CDC

**What Gets Migrated**:
- Tables and associated primary keys (auto-created)
- Data (all rows or filtered subsets)
- Ongoing changes (with CDC)
- With SCT: schema objects, indexes, views, triggers, stored procedures

**What Doesn't Get Migrated** (without SCT):
- Secondary indexes (not auto-created during full load)
- Triggers, views, stored procedures (require SCT or manual migration)
- Some database-specific objects

**Supported Sources → RDS/Aurora Targets**:
- Oracle → Any RDS/Aurora engine
- SQL Server → Any RDS/Aurora engine
- MySQL → RDS MySQL, Aurora MySQL
- PostgreSQL → RDS PostgreSQL, Aurora PostgreSQL
- MariaDB → RDS MariaDB, Aurora MySQL
- MongoDB → (with limitations)
- IBM Db2 → Any RDS engine
- SAP ASE → Any RDS engine
- And many more (see DMS documentation)

**Limitations**:
- Requires separate DMS replication instance (cost and management)
- LOB columns may require special handling
- Some data type conversions may lose fidelity
- Performance depends on replication instance size, network, and source load
- CDC requires source database to have logging enabled (binlog, WAL, redo logs, etc.)

**Documentation**: https://docs.aws.amazon.com/dms/latest/userguide/Welcome.html

---

### Method 31: AWS Schema Conversion Tool (AWS SCT)

**Official AWS Name**: AWS Schema Conversion Tool

**Migration Type**: Schema conversion (companion to DMS)

**When AWS Recommends It**:
- Heterogeneous migrations where schema objects need conversion
- Used alongside DMS for complete migration
- Converting stored procedures, functions, views between engines

**What It Does**:
- Converts schema objects from source engine format to target engine format
- Identifies conversion issues and provides action items
- Generates converted DDL for target engine

**Documentation**: Referenced from DMS documentation and Aurora migration guides

---

### Method 32: AWS Application Migration Service (MGN) — Block-Level

**Official AWS Name**: AWS Application Migration Service

**Migration Type**: Physical (block-level replication)

**When AWS Recommends It** (from Prescriptive Guidance):
- Large-scale lift-and-shift migrations
- Aggressive timelines
- When simplicity is paramount (reduced complexity vs. database tools)
- 1-to-1 server migrations only

**Downtime Characteristics**:
- Downtime required during cutover
- RTO of minutes

**Limitations**:
- Doesn't support clustered systems (NAS, NFS, CIFS/SMB)
- Only supports x86 platforms (Windows or Linux)
- Only supports block-level storage directly attached to migrated system
- Cannot replatform or modernize (lift and shift only)
- After migration, database runs on EC2 (not RDS/Aurora) — additional migration to managed service still needed

**Documentation**: https://docs.aws.amazon.com/prescriptive-guidance/latest/migration-database-rehost-tools/decision-matrix.html

---

### Method 33: mydumper/myloader (Third-Party, AWS-Documented)

**Official AWS Name**: Referenced in AWS DMS Step-by-Step Guide as "mydumper"

**Migration Type**: Logical

**When AWS Recommends It**:
- When migration time is critical
- When Percona XtraBackup cannot be used
- Multithreaded schema and data migration
- Better performance than mysqldump for large datasets

**Key Advantages Over mysqldump** (per AWS docs):
- Parallel backups
- Consistent reads
- Built-in compression
- Each table in separate file

**When NOT to use** (per AWS):
- When migrating from RDS for MySQL or MySQL 5.5/5.6 (XtraBackup may be better)
- When OS limitations prevent third-party software
- When intermediate dump files need flat-file format (mydumper uses SQL format)

**Documentation**: https://docs.aws.amazon.com/dms/latest/sbs/chap-manageddatabases.mysql2rds.fullload.html

---

## Quick Reference Matrix

### Migration Methods by Source → Target

| Source | Target | Methods Available |
|--------|--------|-------------------|
| RDS MySQL | Aurora MySQL | Read Replica (M1), Snapshot (M2), Binlog Replication (M6), DMS (M7) |
| External MySQL | Aurora MySQL | Percona XtraBackup+S3 (M3), mysqldump (M4), LOAD DATA FROM S3 (M5), Binlog Replication (M6), DMS (M7), mydumper (M33) |
| EC2 MySQL | Aurora MySQL | Console Auto-Migration (M8), Percona XtraBackup+S3 (M3), mysqldump (M4), DMS (M7) |
| RDS PostgreSQL | Aurora PostgreSQL | Snapshot (M9), Read Replica (M10), DMS (M12) |
| External PostgreSQL | Aurora PostgreSQL | S3 Import (M11), DMS (M12), pg_dump/pg_restore |
| External MySQL | RDS MySQL | Percona XtraBackup+S3 (M13), mysqldump (M14), Reduced Downtime (M15), LOAD DATA (M16), DMS (M30) |
| External PostgreSQL | RDS PostgreSQL | pg_dump/pg_restore (M17), \copy (M18), S3 Import (M19), DMS (M30) |
| RDS PostgreSQL | RDS PostgreSQL | Transportable Databases (M20) |
| External Oracle | RDS Oracle | SQL Developer (M21), Data Pump (M22), Transportable Tablespaces (M23), SQL*Loader (M24), Materialized Views (M25), DMS (M30) |
| External SQL Server | RDS SQL Server | Native Backup/Restore (M27), BCP (M29), DMS (M30) |
| Any non-compatible engine | Any RDS/Aurora | DMS (M30) + SCT (M31) |

### By Downtime Requirement

| Downtime Tolerance | Recommended Methods |
|-------------------|-------------------|
| **Near-zero downtime** | Read Replica (M1, M10), Binlog Replication (M6, M15), Materialized Views (M25), DMS with CDC (M30) |
| **Minutes of downtime** | Read Replica promotion, MGN block-level cutover |
| **Moderate downtime** | Snapshot migration (M2, M9), Percona XtraBackup+S3 (M3, M13), Native Backup/Restore SQL Server (M27) |
| **Extended downtime OK** | mysqldump (M4, M14), pg_dump (M17), Data Pump (M22), SQL*Loader (M24) |

### By Database Size (AWS Recommendations)

| Database Size | Recommended Methods |
|--------------|-------------------|
| **< 10 GB** | mysqldump (M4, M14), pg_dump (M17), SQL Developer (M21), \copy (M18) |
| **10 GB - 1 TB** | Percona XtraBackup+S3 (M3, M13), Read Replica (M1, M10), DMS (M30), mydumper (M33), Console Auto-Migration (M8) |
| **> 1 TB** | Percona XtraBackup+S3 (M3, M13), Read Replica (M1, M10), Transportable Tablespaces (M23), Transportable Databases (M20), Native Backup/Restore (M27) |

### By Migration Type

| Type | Methods |
|------|---------|
| **Physical** | Read Replica (M1, M10), Snapshot (M2, M9), Percona XtraBackup+S3 (M3, M13), Transportable Databases (M20), Transportable Tablespaces (M23), Native Backup/Restore SQL Server (M27), MGN (M32) |
| **Logical** | mysqldump (M4, M14), LOAD DATA FROM S3 (M5), Binlog Replication (M6), DMS (M7, M30), pg_dump (M17), \copy (M18), Data Pump (M22), SQL*Loader (M24), Materialized Views (M25), BCP (M29), mydumper (M33) |

---

## Key Documentation Links (Complete Index)

| Topic | URL |
|-------|-----|
| Aurora MySQL Migration Overview | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.html |
| Aurora PostgreSQL Migration Overview | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.html |
| Aurora MySQL Read Replica Migration | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.RDSMySQL.Replica.html |
| Aurora MySQL Snapshot Migration | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.RDSMySQL.Import.html |
| Percona XtraBackup + S3 (Aurora) | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.S3.html |
| mysqldump to Aurora MySQL | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.ExtMySQL.mysqldump.html |
| LOAD DATA FROM S3 (Aurora MySQL) | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Integrating.LoadFromS3.html |
| Aurora MySQL Binlog Replication | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Replication.MySQL.html |
| Aurora PostgreSQL Read Replica Migration | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Migrating.RDSPostgreSQL.Replica.html |
| Aurora Console Auto-Migration (EC2) | https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_DMS_migration.html |
| RDS MySQL Backup Restore from S3 | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.html |
| RDS MySQL mysqldump Import | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.SmallExisting.html |
| RDS MySQL Reduced Downtime Import | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.NonRDSRepl.html |
| RDS MySQL Flat File Import | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Procedural.Importing.AnySource.html |
| RDS PostgreSQL Import Overview | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.html |
| RDS PostgreSQL pg_dump Import | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.EC2.html |
| RDS PostgreSQL \copy Import | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Procedural.Importing.Copy.html |
| PostgreSQL Transportable Databases | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.TransportableDB.html |
| RDS Oracle Import Overview | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.html |
| Oracle SQL Developer | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.SQLDeveloper.html |
| Oracle Data Pump | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.DataPump.html |
| Oracle SQL*Loader | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.SQLLoader.html |
| Oracle Materialized Views | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Oracle.Procedural.Importing.Materialized.html |
| SQL Server Native Backup/Restore | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Procedural.Importing.html |
| RDS Read Replicas | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_ReadRepl.html |
| AWS DMS Overview | https://docs.aws.amazon.com/dms/latest/userguide/Welcome.html |
| DMS MySQL Migration Guide | https://docs.aws.amazon.com/dms/latest/sbs/chap-manageddatabases.mysql2rds.html |
| DMS MySQL Full Load Options | https://docs.aws.amazon.com/dms/latest/sbs/chap-manageddatabases.mysql2rds.fullload.html |
| AWS Prescriptive Guidance Decision Matrix | https://docs.aws.amazon.com/prescriptive-guidance/latest/migration-database-rehost-tools/decision-matrix.html |

---

*This document covers 33 distinct migration methods/tools documented by AWS across all RDS and Aurora engine types. All information sourced directly from official AWS documentation.*
