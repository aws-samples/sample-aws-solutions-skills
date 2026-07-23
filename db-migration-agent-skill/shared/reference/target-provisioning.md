# Target Provisioning — Aurora vs RDS, Immutable Settings, RDS Proxy, TLS Gate

> Read this during **Phase 4 (Provision Target)**. Wrong choices here are the #1 cause of
> "recreate the whole instance" rework — several settings are fixed at creation.
> CDK implementation of everything below: [../patterns/cdk-stacks.md](../patterns/cdk-stacks.md).

---

## Target Provisioning

### Aurora vs RDS Selection

| Factor | Aurora | RDS |
|--------|--------|-----|
| Availability | 99.99% (6 copies across 3 AZs) | 99.95% (Multi-AZ synchronous standby) |
| Auto-scaling storage | ✅ Up to 128 TiB | ❌ Must provision EBS |
| Read replicas | 15 (single reader endpoint) | 15 (individual endpoints) |
| Failover time | < 30 seconds | 60-120 seconds |
| Serverless option | ✅ Aurora Serverless v2 | ❌ |
| Cost (minimum) | ~$60/mo (db.t4g.medium) | ~$13/mo (db.t4g.micro) |
| Global Database | ✅ Cross-region < 1s lag | ❌ |
| Backtrack (MySQL) | ✅ Point-in-time rewind | ❌ |

### Instance Sizing During Migration

Size LARGER during migration for import throughput, then scale down:

| DB Size | Migration Instance | Steady-State Instance |
|---------|-------------------|----------------------|
| < 50 GB | db.r6g.large | db.r6g.medium or Serverless v2 |
| 50-500 GB | db.r6g.xlarge | db.r6g.large |
| 500 GB - 2 TB | db.r6g.2xlarge | db.r6g.xlarge |
| > 2 TB | db.r6g.4xlarge | db.r6g.2xlarge |

### Oracle / SQL Server Target Provisioning — Settings Fixed at Creation

These cannot be changed after the instance is created — get them right up front (they're a frequent cause of "recreate the whole instance" rework):

| Engine | Set-at-creation, immutable | CLI flag | Notes |
|--------|----------------------------|----------|-------|
| **RDS Oracle** | DB character set | `--character-set-name` | Default `AL32UTF8`. Match source (e.g. `KO16MSWIN949`). CDB DB charset is always `AL32UTF8` — set non-default on the PDB only. |
| **RDS Oracle** | NCHAR character set | `--nchar-character-set-name` (CLI v2) | `AL16UTF16` (default) or `UTF8`. |
| **RDS Oracle** | `DB_BLOCK_SIZE` | parameter group at create | Default 8 KB; cannot change later. |
| **RDS Oracle** | Edition + license model | `--engine oracle-ee`/`oracle-se2`, `--license-model` | LI = SE2 only; EE = BYOL only. |
| **RDS SQL Server** | Server/instance collation | `--collation` (via parameter or console) | DB/column collations ride in the `.bak`; the *instance* default can't change later. |
| **RDS SQL Server** | Edition + license model | `--engine sqlserver-ee`/`-se`/`-web`/`-ex`, `--license-model` | License-Included or BYOM (License Mobility + Software Assurance). |

**Option groups (required for the native paths):** RDS Oracle Data Pump via S3 needs the **`S3_INTEGRATION`** option + an IAM role on `S3_INTEGRATION`; RDS SQL Server native backup/restore needs the **`SQLSERVER_BACKUP_RESTORE`** option with an IAM role; TDE on either engine needs the **`TDE`** option (permanent + persistent — cannot be removed once attached). Set these on the option group before execution (Phase 6).

### RDS Proxy on the Target (provision BEFORE cutover for minimal-downtime / future failover)

For minimal-downtime workloads, stand up **RDS Proxy in front of the target cluster** during provisioning — before Phase 8 — so the app connects through the proxy endpoint from the moment of cutover. The proxy does **not** remove the brief *initial* EC2→RDS cutover pause, but once in place it makes every subsequent failover/maintenance event a **< 1s reconnect with no app restart and no pool refresh** (the proxy holds client connections and re-points to the new writer), and it absorbs the reconnect storm when the app's pool refreshes at cutover.

```bash
# Create a proxy targeting the new cluster; creds come from Secrets Manager (the same secret you rotate at cutover)
aws rds create-db-proxy \
  --db-proxy-name your-app-proxy \
  --engine-family MYSQL \
  --auth '[{"AuthScheme":"SECRETS","SecretArn":"arn:aws:secretsmanager:REGION:ACCT:secret:your-app/db-credentials","IAMAuth":"DISABLED"}]' \
  --role-arn arn:aws:iam::ACCT:role/rds-proxy-secrets-role \
  --vpc-subnet-ids subnet-aaa subnet-bbb \
  --require-tls
aws rds register-db-proxy-targets \
  --db-proxy-name your-app-proxy \
  --db-cluster-identifier your-aurora-cluster
```

Then point clients at the **proxy endpoint** (`your-app-proxy.proxy-xxxx.<region>.rds.amazonaws.com`) in Phase 7.5/8, not the cluster endpoint. PostgreSQL: use `--engine-family POSTGRESQL`. (RDS Proxy requires the target to be RDS/Aurora — it's a target-side construct, so it works for EC2→RDS even though the *source* can't be proxied.)

### TLS-Enforcement Gate (check the target parameter group BEFORE loading or cutover)

Compliance baselines (K-ISMS, PCI, internal hardening) commonly set **`require_secure_transport=ON`** (MySQL/MariaDB) or **`rds.force_ssl=1`** (PostgreSQL) on the target parameter group. When enforced, **every** connection must use TLS — and that breaks two things if you don't plan for it:

1. **The migration load tool** — `mysqldump`/`mysql` import and `psql` restore must connect with TLS, or the import fails at connect time.
2. **The application connector** — the app's JDBC/driver string must request TLS *and* must be a version that supports the parameter, or the app can't reconnect after cutover.

**Check first:**
```sql
SHOW VARIABLES LIKE 'require_secure_transport';   -- MySQL/MariaDB: should be ON if enforced
-- PostgreSQL: parameter rds.force_ssl = 1
```
Then verify both the load tool and the app connector are configured for TLS. Connector parameter reference:

| Connector | TLS parameter |
|-----------|---------------|
| `mysql` / `mariadb` CLI | `--ssl` (optionally `--ssl-ca=rds-combined-ca-bundle.pem` to verify the server cert) |
| Connector/J 8.x–9.x | `sslMode=REQUIRED` (use `VERIFY_CA` + `trustCertificateKeyStoreUrl` to validate the cert) |
| Connector/J 5.1.x | `useSSL=true&requireSSL=true` |
| Node `mysql2` | `ssl: { rejectUnauthorized: false }` (or `{ ca: fs.readFileSync('rds-combined-ca-bundle.pem') }` to verify) |
| Python `mysqlclient` | `ssl={'ca': '/path/to/rds-combined-ca-bundle.pem'}` |
| `psql` / libpq | `sslmode=require` (or `verify-ca` / `verify-full` with `sslrootcert=`) |

> **RDS Proxy presents a different certificate chain than the cluster endpoints.** If the
> app connects through a proxy endpoint with certificate verification on, validate the CA
> bundle against the PROXY endpoint, not (only) the writer endpoint — use the combined
> global `rds-combined-ca-bundle`/AmazonRootCA trust store — a CA bundle validated only
> against the writer endpoint will abort the cutover at the proxy connection probe.

> **Watch the default mismatch.** Connector/J ≥ 8.0.13 defaults to `sslMode=PREFERRED` — it *tries* TLS but **silently falls back to plaintext if the server allowed it**. With `require_secure_transport=ON` the server refuses plaintext, so PREFERRED can still connect — but make the intent explicit with `sslMode=REQUIRED` so behavior doesn't depend on negotiation. The `rds-combined-ca-bundle.pem` is downloadable from the AWS docs (region-specific bundles also available).

