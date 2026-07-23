# DMS Best Practices — Quick Reference

## Replication Instance Sizing

| Workload | Instance | RAM | Use Case |
|----------|----------|-----|----------|
| Dev/Test | dms.r6i.large | 16 GB | < 50 tables, testing |
| Small Production | dms.r6i.xlarge | 32 GB | 50-300 tables, < 500 GB |
| Medium Production | dms.r6i.2xlarge | 64 GB | 300-1000 tables, LOBs present |
| Large Production | dms.r6i.4xlarge | 128 GB | 1000+ tables, > 2 TB |

**Rules:**
- Allocate 50-70% of RAM for `MemoryLimitTotal`
- Never use T-family for production (CPU credit exhaustion)
- Start larger during full load, scale down for steady-state CDC
- Multi-AZ for production migrations (auto-failover)

## LOB Handling

| Mode | Max Size | Performance | Use When |
|------|----------|-------------|----------|
| Limited LOB | Configurable (e.g., 32 KB) | Fast (inline transfer) | LOB sizes are predictable and bounded |
| Full LOB | Unlimited | Slow (2 lookups per row) | LOBs vary wildly in size |
| Inline LOB | Configurable threshold | Best of both | Mixed LOB sizes (small + occasional large) |

**Query to profile LOB sizes:**
```sql
-- MySQL: Find actual max LOB sizes
SELECT table_name, column_name,
  MAX(LENGTH(column_name)) AS max_bytes
FROM your_db.your_table GROUP BY table_name, column_name;
```

**Recommendation:** Use Limited LOB Mode with `LobMaxSize` set to the 99th percentile of your actual LOB sizes. Only use Full LOB mode if you cannot afford to truncate any LOBs.

## Task Types

| Type | Downtime | Prerequisites | Migrates Views? |
|------|----------|---------------|-----------------|
| Full Load Only | Yes (duration = data size / throughput) | None | ✅ Yes |
| CDC Only | No (ongoing) | Data must already exist on target | ❌ No |
| Full Load + CDC | Near-zero (seconds at cutover) | Binary logging / logical replication | ❌ No (tables only) |

## Critical Settings

### Full Load Optimization
```json
{
  "MaxFullLoadSubTasks": 16,
  "CommitRate": 50000,
  "TargetTablePrepMode": "DROP_AND_CREATE",
  "CreatePkAfterFullLoad": true
}
```

`CreatePkAfterFullLoad: true` — creates primary keys AFTER data load, significantly speeding up full load (no index maintenance during bulk insert).

### CDC Optimization
```json
{
  "BatchApplyEnabled": true,
  "BatchApplyPreserveTransaction": true,
  "BatchApplyTimeoutMax": 30,
  "MinTransactionSize": 1000
}
```

`BatchApplyEnabled: true` — groups changes into batches instead of applying one-by-one. 2-5x throughput improvement.

## What DMS Does NOT Migrate

| Object Type | Alternative |
|-------------|-------------|
| Stored procedures | mysqldump --routines / pg_dump --schema-only |
| Triggers | mysqldump --triggers / pg_dump |
| Views | Full Load task only, OR mysqldump |
| Functions | mysqldump --routines / pg_dump |
| Events (MySQL) | mysqldump --events |
| Sequences (PostgreSQL) | pg_dump --schema-only |
| Indexes (optionally) | Created after full load for speed |
| User permissions/grants | Manual recreation |
| Custom data types (PG) | pg_dump --schema-only |

## Pre-Migration Assessment

Always run the DMS pre-migration assessment before starting:
```bash
aws dms create-replication-task \
  --replication-task-identifier "assessment-task" \
  --source-endpoint-arn $SOURCE_ARN \
  --target-endpoint-arn $TARGET_ARN \
  --replication-instance-arn $INSTANCE_ARN \
  --migration-type "full-load" \
  --table-mappings file://table-mappings.json \
  --enable-premigration-assessment-run
```

This checks:
- Source DB connectivity and permissions
- Unsupported data types
- Tables without primary keys (affects CDC validation)
- LOB column identification
- Binary logging configuration (MySQL)
- Replication slot availability (PostgreSQL)

## Monitoring Metrics

| Metric | Warning Threshold | Action |
|--------|------------------|--------|
| CDCLatencySource | > 30 seconds | Check source load, increase instance |
| CDCLatencyTarget | > 30 seconds | Enable BatchApply, scale target |
| FreeableMemory | < 2 GB | Scale up replication instance |
| SwapUsage | > 0 | Instance is undersized |
| CPUUtilization | > 80% sustained | Scale up or reduce parallelism |

## Engine-Specific Gotchas

### MySQL → Aurora MySQL
- `DEFINER` clauses in views/procedures may fail if the user doesn't exist on Aurora
- `SUPER` privilege not available on Aurora — use `rds_superuser_role` or remove DEFINER
- AUTO_INCREMENT values may differ slightly after CDC (higher on target is OK)
- `sql_mode` differences between versions can cause trigger/procedure failures

### MariaDB → Aurora MySQL
- MariaDB-specific SQL syntax (e.g., `RETURNING` clause) not supported in Aurora MySQL
- Sequence objects (MariaDB 10.3+) not available in Aurora MySQL — use AUTO_INCREMENT
- System-versioned tables (temporal tables) not supported in Aurora MySQL
- GIS spatial functions differ between MariaDB and MySQL 8.0

### PostgreSQL → Aurora PostgreSQL
- Not all extensions are available on Aurora (check `SELECT * FROM pg_available_extensions`)
- `pg_cron` requires Aurora-specific setup
- Large objects (lo) need special handling in DMS
- Custom C-language functions cannot be installed on Aurora
- Replication slots MUST be cleaned up to prevent WAL accumulation
