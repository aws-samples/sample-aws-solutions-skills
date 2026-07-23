# Zero-Downtime Cutover Procedures

## Overview

The cutover is the highest-risk phase of the migration. This document provides step-by-step procedures for three cutover methods, ranked by recommendation.

---

## Pre-cutover: Discover DB Clients (MANDATORY — do this first)

Before changing anything, enumerate EVERY client that connects to the source DB. Migrating the DB
without repointing all clients = **split data** (a missed client keeps writing the old DB) or
**outage** (a missed client can't reach the new one). This is the single highest operational risk.

### Step 1 — Trace via Security Group

The source DB's security-group ingress reveals who is allowed to connect:
```bash
aws ec2 describe-security-groups --group-ids <db-sg> \
  --query "SecurityGroups[].IpPermissions[?ToPort==\`3306\`].[UserIdGroupPairs[].GroupId, IpRanges[].CidrIp]"
```
For each allowed source SG, list the resources attached to it (EC2 / ECS / Lambda-in-VPC via ENIs):
```bash
aws ec2 describe-instances --filters Name=instance.group-id,Values=<client-sg> \
  --query "Reservations[].Instances[].[InstanceId,PrivateIpAddress,Tags[?Key=='Name'].Value|[0]]"
aws ec2 describe-network-interfaces --filters Name=group-id,Values=<client-sg> \
  --query "NetworkInterfaces[].[PrivateIpAddress,Description,InterfaceType]"
```

### Step 2 — Inspect each client's connection config (config can live in MANY places)

Check all of these in **override order** — a later source overrides an earlier one:

| Priority (highest wins) — Location | How to check |
|------------------------------------|--------------|
| 1. Process args | `ps -eo args \| grep -iE "jdbc:\|DB_HOST\|datasource\|--spring"` |
| 2. Env files / vars | `cat <EnvironmentFile>`; `systemctl show <svc> -p Environment` |
| 3. systemd units | `grep -rn "jdbc:\|DB_HOST\|datasource" /etc/systemd/system/` |
| 4. App config | `application*.properties`/`.yml`, `.env`, `config.js/json` |
| 5. Secrets Manager | `aws secretsmanager get-secret-value --secret-id <id> --query SecretString` — **does it even contain `host`?** |
| 6. Hardcoded IPs | `grep -rn "<source-private-ip>" /etc /opt /home /srv 2>/dev/null` |

**Containerized clients** (the ENIs found in Step 1 often belong to these — same
override-order logic, different locations):

| Runtime | Where the DB host hides | How to check / repoint |
|---------|-------------------------|------------------------|
| **ECS** | Task definition `environment`/`secrets`, or the image itself | `aws ecs describe-task-definition --task-definition <fam> --query 'taskDefinition.containerDefinitions[].{env:environment,sec:secrets}'` → register a new revision + `aws ecs update-service --force-new-deployment` |
| **EKS / Kubernetes** | ConfigMap, Secret, Deployment env, Helm values | `kubectl get deploy <d> -o jsonpath='{..env}'`; `kubectl get configmap,secret -o yaml \| grep -i <source-ip-or-host>` → edit source-of-truth (GitOps repo/Helm values, not live objects) + `kubectl rollout restart deploy/<d>` |
| **Lambda (in-VPC)** | Function env vars or the secret it reads | `aws lambda get-function-configuration --function-name <f> --query 'Environment.Variables'` → `update-function-configuration` (next invocation picks it up) |
| **CI/batch runners** | Job env (Jenkins credentials, GitHub Actions secrets, Airflow connections) | Search the scheduler's connection store for the source host; these are the clients everyone forgets — they only connect at run time, so the processlist cross-check (Step 3) misses them outside their schedule. |

**On-premises clients** (no security group to trace): work Step 3 first — the processlist
shows connected on-prem IPs — then netstat on the DB host (`ss -tnp state established
'( sport = :3306 )'`) over a full business cycle (include the batch window: nightly/weekly
jobs are invisible in a 10-minute sample). Ask the network team for firewall/NAT logs to
the DB port as the authoritative list.

**The DB host itself is a client** — always inspect it. Local cron jobs, backup scripts,
and maintenance tasks connect via localhost, so they never appear in SG rules and only
appear in the processlist during their schedule window. They silently die when the host is
decommissioned (losing, e.g., the only backup) or keep dumping a stale DB. Check
explicitly — customers forget these exist until asked:
```bash
ls /etc/cron.d/ /etc/cron.daily/ /etc/cron.weekly/; crontab -l; \
  for u in root mysql postgres; do crontab -l -u $u 2>/dev/null; done
grep -rl "mysql\|psql\|mysqldump\|pg_dump" /etc/cron* /var/spool/cron 2>/dev/null
systemctl list-timers --no-pager | head -20
```
Each hit gets an inventory row and a disposition: retire (RDS automated backups replace a
local dump script), move to EventBridge Scheduler/Lambda, or re-home to another host
pointed at the new endpoint.

- **Every host-side change must land in the customer's source of truth, or it WILL be
  reverted by their own automation.** Live hosts are usually deploy artifacts: the systemd
  unit came from Ansible/Chef/a CI pipeline, the task definition from IaC, the ConfigMap
  from a GitOps repo. A change made only on the box survives until the next deploy — then
  the pipeline re-applies the old config and the app silently reconnects to the retired
  source (or dies with it). Rules: (1) at discovery, ask **where each client's config is
  deployed FROM** (repo/pipeline/IaC) — record it in the client inventory; (2) the cutover
  plan for that client is a **change to that repo deployed through their pipeline**, not
  (only) an SSH edit. Default mechanics: the agent produces an **exact change specification**
  (file, key, old value → new value, e.g. the one-line endpoint diff) and the **customer's
  team commits and merges it themselves** — the agent does not need or ask for repo access,
  and verifies the *deployed result* on the host afterwards. Only if the customer
  explicitly grants repo access (recorded in authorizations.md) may the agent raise a PR
  directly — and then PRs only, never direct pushes, with the customer merging; (3) a direct on-host edit is acceptable ONLY as the cutover-window
  mechanism itself, and then ONLY with a same-day follow-up task (tracked in
  migration-plan.md, verified before engagement close) confirming the same change is
  merged upstream; (4) if the customer has config drift detection (Ansible periodic runs,
  GitOps reconciliation like ArgoCD), an unmerged on-host edit isn't a risk, it's a
  countdown — reconciliation intervals are often minutes. Confirm the reconciler is
  paused for the window or the merge lands first.

- **Command-line args / env override bundled config** (Spring Boot `--spring.datasource.url`
  overrides `application-prod.properties`). Change the **highest-priority** source — often the
  systemd `ExecStart` line. Back up the unit first (`cp unit unit.bak.$(date +%s)`), then
  `systemctl daemon-reload`.
- **Verify the secret actually contains `host`.** If it holds only `username`/`password`, the
  Secrets Manager rotation method **alone will not repoint the app** — you must change the
  config/unit and backfill `host`/`port`/`dbname`/`engine` into the secret.
- **ALWAYS use the RDS DNS endpoint, never a resolved IP** — Multi-AZ failover keeps the DNS name
  but changes the underlying IP.
- **ORM auto-DDL:** if a client runs `spring.jpa.hibernate.ddl-auto=update` (or Rails/Django/
  Sequelize auto-migrate), its first connection to the new DB may attempt schema changes — set
  `validate`/`none` for production migrations.

### Step 3 — Cross-check live connections from the DB side
```sql
SELECT user, SUBSTRING_INDEX(host,':',1) AS client_ip, db, command, COUNT(*) AS conns
FROM information_schema.processlist GROUP BY user, client_ip, db, command;
```
Confirm the client IPs/counts match Steps 1–2. Any unexpected IP is a client you were about to miss.
`Binlog Dump` / walsender sessions in this output are **replication consumers** — handle
them in Step 4, not as app clients.

### Step 4 — Downstream replication/CDC consumers (not app clients, still break at cutover)

Anything reading the source's **binlog/WAL** — Debezium/Kafka Connect, downstream read
replicas, BI sync tools (Fivetran/Airbyte), custom binlog readers, audit forwarders —
stops working when the source freezes, and **cannot simply be repointed**: the target's
binlog/LSN positions are different coordinates. Plan per consumer:

| Consumer | Cutover plan |
|----------|--------------|
| Debezium / Kafka Connect | Stop the connector at freeze; deploy a NEW connector against the target with `snapshot.mode` appropriate to whether the topic can tolerate a re-snapshot (`schema_only` + fresh offsets if not); accept the offset reset — never copy old offsets. |
| Downstream MySQL/PG replicas of the source | Rebuild them as replicas/readers of the new target (Aurora readers usually replace them outright — often the simpler answer). |
| SaaS ELT (Fivetran/Airbyte/…) | Update the connection to the target endpoint; trigger a re-sync/historical re-load; verify the tool supports Aurora as a CDC source (binlog retention: `binlog retention hours` ≥ its sync interval). |
| Custom binlog/WAL readers | Treat like Debezium: restart from the target's current position; reconcile any gap from the freeze window at the application level. |

Record each consumer + its plan in the `migration-plan.md` client inventory (they get
their own rows).

### Completion criterion
EVERY discovered client is repointed to the new RDS DNS endpoint and verified connected
(bidirectionally — app health + DB-side processlist), and every replication/CDC consumer
has executed its Step 4 plan.

---

## Method 1: Secrets Manager Rotation (Recommended)

**Best for:** Applications that read DB credentials from AWS Secrets Manager.

**Downtime:** 0-30 seconds (depends on application connection pool refresh interval)

### Procedure

```bash
# Step 1: Verify CDC is caught up
aws dms describe-replication-tasks \
  --filters Name=replication-task-arn,Values=$TASK_ARN \
  --query 'ReplicationTasks[0].ReplicationTaskStats.{
    CDCLatencySource: CdcLatencySource,
    CDCLatencyTarget: CdcLatencyTarget,
    TablesLoaded: TablesLoaded,
    TablesErrored: TablesErrored
  }'
# Verify: CDCLatencySource < 5, CDCLatencyTarget < 5, TablesErrored = 0

# Step 2: Put application in read-only mode (disable write endpoints / feature flag)

# Step 3: FREEZE THE SOURCE — mandatory, prevents split-brain. Any write that reaches the
# source after this point would be lost (forward CDC is about to stop).
#
# PREREQUISITE for the read_only method: SUPER or READ_ONLY ADMIN privilege. A plain `admin`
# account frequently lacks it and SET GLOBAL read_only returns:
#   ERROR 1227 (42000): Access denied; you need (at least one of) the SUPER, READ_ONLY ADMIN privilege(s)
#
# 3a. PREFERRED (have the privilege): freeze the engine read-only.
mysql -h $SOURCE_HOST -u admin -p -e \
  "SET GLOBAL read_only = ON; SET GLOBAL super_read_only = ON;"
mysql -h $SOURCE_HOST -u admin -p -e "SELECT @@global.read_only, @@global.super_read_only;"
# PostgreSQL equivalent (new sessions only; also revoke INSERT/UPDATE/DELETE at app role):
#   ALTER DATABASE your_db SET default_transaction_read_only = on;
#
# 3b. FALLBACK (no privilege — equally valid first-class option): QUIESCE by stopping write clients.
# Stop ALL write clients discovered in the client-discovery step FIRST, then PROVE writers = 0:
#   sudo systemctl stop backend.service   # (and every other write client)
mysql -h $SOURCE_HOST -u admin -p -e "
  SELECT id, user, SUBSTRING_INDEX(host,':',1) AS ip, db, command, info
  FROM information_schema.processlist
  WHERE command NOT IN ('Sleep','Daemon','Binlog Dump') AND info IS NOT NULL;"
# Expect ZERO active write/DML threads. Stopping the clients becomes the freeze mechanism.

# Step 4: Wait for final CDC drain (10-30 seconds), then re-verify CDC latency = 0
sleep 30

# Step 5: Stop forward DMS task (source → target)
aws dms stop-replication-task --replication-task-arn $TASK_ARN

# Step 6: Run final validation
# (row count spot-check on 3-5 critical tables)

# Step 7: RESET TARGET HIGH-WATER MARKS — AUTO_INCREMENT / sequences are NOT carried by
# DMS or binlog CDC. Without this, the first inserts on Aurora collide with existing PKs.
# MySQL/MariaDB — emit ALTER statements for every auto_increment column and run them:
mysql -h $AURORA_ENDPOINT -u admin -p -N -e "
  SELECT CONCAT('ALTER TABLE \`', t.TABLE_NAME, '\` AUTO_INCREMENT = ',
                IFNULL((SELECT MAX(\`', k.COLUMN_NAME, '\`) FROM \`', t.TABLE_NAME, '\`),0)+1, ';')
  FROM information_schema.TABLES t
  JOIN information_schema.COLUMNS k
    ON k.TABLE_SCHEMA = t.TABLE_SCHEMA AND k.TABLE_NAME = t.TABLE_NAME
   AND k.EXTRA LIKE '%auto_increment%'
  WHERE t.TABLE_SCHEMA = 'your_db';" | mysql -h $AURORA_ENDPOINT -u admin -p your_db
# PostgreSQL — re-seed every owned sequence to its column max:
#   SELECT setval(seq, COALESCE(max_val, 1)) for each sequence via pg_get_serial_sequence.

# Step 8: START REVERSE REPLICATION (Aurora → source) — task created & connection-tested
# pre-cutover but NEVER RUN (see "Reverse Replication — Set Up BEFORE Cutover" below).
# This keeps the source a current standby for a lossless rollback.
#
# CDC START POSITION MATTERS: the reverse task must begin at the FREEZE POINT (now), not
# at some earlier checkpoint — an earlier start would replay forward-load rows back at the
# source. For a never-run task, starting it now (post-freeze, pre-app-writes) achieves
# exactly that: Aurora's binlog contains no committed writes between freeze and repoint.
# Pin it explicitly if you want belt-and-braces:
#   --cdc-start-position "$(mysql -h $AURORA_ENDPOINT ... -N -e 'SHOW BINARY LOG STATUS' | awk '{print $1":"$2}')"
aws dms start-replication-task \
  --replication-task-arn $REVERSE_TASK_ARN \
  --start-replication-task-type start-replication
# (If the reverse task HAS run before — e.g. a full rehearsal on this same pair — do NOT
# resume it: 'resume-processing' would continue from the rehearsal checkpoint. Delete and
# recreate the task, or use 'reload-target'-free restart with an explicit --cdc-start-position.)

# Step 9: Rotate the secret
aws secretsmanager update-secret \
  --secret-id "your-app/db-credentials" \
  --secret-string '{
    "username": "admin",
    "password": "'"$DB_PASSWORD"'",
    "host": "'"$AURORA_ENDPOINT"'",
    "port": "3306",
    "dbname": "your_db",
    "engine": "mysql"
  }'

# Step 10: Force credential refresh (application-specific)
# For Spring Boot with Secrets Manager rotation:
#   - Connections auto-refresh based on maxLifetime setting
# For ECS tasks:
#   - aws ecs update-service --force-new-deployment triggers refresh
# For Lambda:
#   - Next invocation picks up new secret automatically

# Step 11: Verify connectivity BIDIRECTIONALLY (single-direction curl is NOT sufficient).
# (a) App side — health endpoint shows the DB up:
curl -s https://your-app.example.com/actuator/health | jq '.components.db.status'   # "UP"
# (b) DB side — the NEW DB's processlist shows the expected client IPs + connection count:
mysql -h $AURORA_ENDPOINT -u admin -p -e "
  SELECT SUBSTRING_INDEX(host,':',1) AS client_ip, db, COUNT(*) AS conns
  FROM information_schema.processlist GROUP BY client_ip, db;"
# Confirm every discovered client IP appears with a sane connection count (≈ the app pool size).
# A green /health alone can mask a client that never repointed to the new endpoint.

# Step 12: Re-enable writes (clear maintenance mode)
```

### Reverse Replication — Set Up BEFORE Cutover (mandatory for lossless rollback)

The 7-day rollback window only protects you if the source stays current with the writes
Aurora accepts after cutover. Establish a reverse CDC channel **before** the cutover window:

```bash
# Pre-cutover: create (but do NOT start) a reverse task with Aurora as source.
aws dms create-replication-task \
  --replication-task-identifier reverse-aurora-to-source \
  --source-endpoint-arn $AURORA_ENDPOINT_ARN \
  --target-endpoint-arn $SOURCE_ENDPOINT_ARN \
  --replication-instance-arn $DMS_INSTANCE_ARN \
  --migration-type cdc \
  --table-mappings file://table-mappings.json \
  --replication-task-settings file://reverse-task-settings.json
# Verify BOTH endpoint connections (aws dms test-connection) — but do NOT start the task:
# a task that has run holds a CDC checkpoint, and resuming from a pre-cutover checkpoint
# at cutover would replay forward-load writes back into the source. Keep it never-run;
# it is STARTED (fresh, from the freeze point) at cutover step 8.
```

- **MySQL/MariaDB**: Aurora's cluster parameter group needs `binlog_format=ROW` so the
  source can consume Aurora's binlog as a replica.
- **PostgreSQL**: Aurora needs `rds.logical_replication=1` and the source needs a free slot.
- Native binlog/logical replication is an alternative to a reverse DMS task — pick whichever
  matches the forward method.

### When Reverse Replication is NOT Possible

Reverse replication is the ideal lossless-rollback mechanism, but it is **impossible** under
several real conditions. Do not require it as the only rollback path:

| Condition | Why it fails |
|-----------|--------------|
| Source `log_bin=OFF` (not enabled) | No binlog for a reverse CDC channel to consume/target. |
| Major version **downgrade** (target newer, e.g. 10.11 → 10.5) | Replicating back from newer to older major is unsupported/unreliable. |
| Insufficient privileges | No `REPLICATION SLAVE`/`SUPER` (or PG replication role) to establish the channel. |

**Alternatives (pick per RPO):**
1. **Application-level write logging** — append every post-cutover write to a durable log
   (append-only table on Aurora, SQS/Kinesis, structured request logs); replay against the source
   on rollback. Closest to lossless.
2. **Shortened sync window** — cut over immediately after the final sync/validation in a
   low-traffic window so the stranded-write loss window is minimal.
3. **Accept the risk with explicit RPO acknowledgment** (low-traffic/non-critical only) — state
   plainly that rollback loses post-cutover Aurora writes, get explicit sign-off, keep the window
   short. Record the acknowledgment in `migration-plan.md`.

### Connection Pool Settings for Fast Refresh

Set these **before** the cutover window (Phase 7.5 prep), targeting ~30-second recycle —
the full per-pool table and rationale are in §"Minimize the Write-Pause Window" below.

**HikariCP (Java/Spring Boot):**
```yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 20
      minimum-idle: 5
      max-lifetime: 30000     # 30s pre-cutover value — repoint takes effect within 30s
      keepalive-time: 30000
      connection-timeout: 5000
      validation-timeout: 5000
```
Restore `max-lifetime` to its normal production value (e.g. 30 min) after the T+24h watch.

**Node.js (mysql2/pg):**
```javascript
const pool = mysql.createPool({
  connectionLimit: 20,
  waitForConnections: true,
  maxIdle: 5,
  idleTimeout: 30000,   // 30s pre-cutover value
});
// After secret rotation, destroy and recreate pool to force immediate re-resolution
pool.end(() => { /* recreate with new credentials */ });
```

---

## Method 2: RDS Blue/Green Deployment

**Best for:** Migrations from RDS MySQL/PostgreSQL to Aurora (already on RDS).

**Downtime:** Typically < 1 minute (managed by AWS)

### Procedure

```bash
# Step 1: Create Blue/Green deployment
aws rds create-blue-green-deployment \
  --blue-green-deployment-name "mysql-to-aurora" \
  --source "arn:aws:rds:region:account:db:source-rds-instance" \
  --target-engine-version "8.0.mysql_aurora.3.07.1" \
  --target-db-cluster-parameter-group-name "aurora-mysql-params"

# Step 2: Wait for green environment to be ready and synchronized
aws rds describe-blue-green-deployments \
  --blue-green-deployment-identifier $BGD_ID
# Status should be: AVAILABLE, SwitchoverDetails.Status: AVAILABLE

# Step 3: Switchover (AWS handles the cutover)
aws rds switchover-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --switchover-timeout 300

# Step 4: Verify (endpoint names are swapped — application sees no change)
# The original endpoint now points to Aurora

# Step 5: Delete blue environment (after monitoring period)
aws rds delete-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --delete-target false  # Keep the old instance for rollback
```

**Limitations:**
- Only works when source is already on RDS (not EC2)
- Cross-engine only supported for MySQL → Aurora MySQL
- PostgreSQL Blue/Green doesn't support cross-engine (PG → Aurora PG)

---

## Method 3: DNS CNAME Swap (Route 53)

**Best for:** Applications using DNS hostnames for DB connectivity (not hardcoded IPs).

**Downtime:** DNS TTL propagation time (set TTL to 60s before migration)

### Procedure

```bash
# Step 1: Lower TTL 24 hours before cutover
aws route53 change-resource-record-sets \
  --hosted-zone-id $ZONE_ID \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "db.internal.example.com",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{"Value": "source-db-host.example.com"}]
      }
    }]
  }'

# Step 2: At cutover time — swap CNAME to Aurora
aws route53 change-resource-record-sets \
  --hosted-zone-id $ZONE_ID \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "db.internal.example.com",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{"Value": "your-cluster.cluster-xxxx.region.rds.amazonaws.com"}]
      }
    }]
  }'

# Step 3: Wait for TTL expiry (60 seconds)
sleep 60

# Step 4: Verify resolution
dig db.internal.example.com CNAME
# Should resolve to Aurora endpoint
```

**Limitations:**
- Applications caching DNS beyond TTL will have extended downtime
- Java applications with `networkaddress.cache.ttl` set high need JVM restart
- Not suitable if applications use IP addresses directly

---

## Rollback Procedures

### Quick Rollback (< 5 minutes) — Lossless when reverse replication is running

Because reverse replication (Aurora → source) has been running since cutover, the source is
current and failback loses no data. Rollback sequence:

1. Set Aurora read-only: `SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;`
   (PostgreSQL: `ALTER DATABASE your_db SET default_transaction_read_only=on;`)
2. Confirm reverse CDC drained to zero lag (`CdcLatencySource`/`CdcLatencyTarget` = 0), then
   stop the reverse task.
3. Reset AUTO_INCREMENT / sequences on the **source** above its new max (same query as
   cutover step 7, run against the source) so resumed writes don't collide.
4. Revert the secret / DNS to the source endpoint and refresh the app's connections.

| Method | Rollback Action | Time |
|--------|----------------|------|
| Secrets Manager | Freeze Aurora → drain reverse CDC → revert secret to source | < 2 min + pool refresh |
| Blue/Green | Create new B/G deployment to switch back | 5-10 min |
| DNS CNAME | Freeze Aurora → drain reverse CDC → swap CNAME back | TTL (60s) |
| DMS | Reverse task already running — just stop, then repoint app | < 2 min |

> **If reverse replication was NOT set up**, rollback is lossy: every write Aurora accepted
> after cutover is stranded on Aurora. Only acceptable if you can replay those writes from an
> application-side log. This is exactly why reverse replication is mandatory above.

### Rollback Decision Criteria

| Signal | Severity | Action |
|--------|----------|--------|
| Application error rate > 5% | Critical | Immediate rollback |
| P99 latency > 3x baseline | High | Rollback if not improving in 5 min |
| Connection failures > 1% | High | Check SG/credentials first, rollback if not fixable in 10 min |
| Data integrity concerns | Critical | Immediate rollback, investigate |
| Single slow query | Low | Investigate — likely missing ANALYZE TABLE |

### Post-Rollback Actions

1. Keep Aurora cluster running (do NOT delete)
2. Investigate root cause
3. Fix the issue
4. Re-plan cutover window
5. Re-sync DMS (restart task with `--start-replication-task-type resume-processing`)

---

## Execute the Freeze Window as ONE Pre-Staged Script per Host

A cutover driven as **interactive SSM send-command round-trips** adds 30–40 s of
dispatch latency *per step group* inside the freeze window — easily turning ~40 s of
actual DB work into a 300+ s write pause. Rules:

1. **Pre-stage the whole freeze-window sequence as one script on each host** (delivered
   during prep, verified with a `--dry-run` flag). At T-0 you issue ONE command per host,
   not one per step.
2. **Test every engine-specific command against the real target version before the
   window.** Known trap: Aurora MySQL 8.0 uses `SHOW MASTER STATUS` / `STOP REPLICA`;
   MySQL 8.4 renamed it `SHOW BINARY LOG STATUS`. A syntax error discovered mid-freeze
   forces in-window debugging — exactly what the rehearsal exists to prevent.
3. **Time-budget each step from the rehearsal** and put the numbers in the runbook; a step
   exceeding 2× its budget is an abort signal, not a "keep fiddling" signal.

## Minimize the Write-Pause Window (pre-cutover prep)


For "absolute minimal downtime" EC2 → RDS/Aurora, the cutover pause is the time between *freezing the source* and *the app writing to the target*. CDC catch-up (matrix rows 6–7 — XtraBackup seed + binlog/CDC catch-up, DMS Full Load + CDC — and their Oracle analog, row 15's Data Pump seed + DMS CDC) is what makes this a **pause measured in seconds, not the whole transfer** — the bulk load runs while the app stays up, and only the final CDC drain happens inside the window. Shrink that window with these prep steps:

**1. Connection-pool settings — set BEFORE cutover (not at cutover).** A stale pool keeps connections open to the old DB long after you repoint, stretching the effective pause. Tune the pool so it recycles fast:

| Pool | Pre-cutover setting | Effect |
|------|---------------------|--------|
| **HikariCP** (Spring Boot) | `maxLifetime=30000` (30s), `keepaliveTime=30000`, `validationTimeout=5000` | Connections recycle within 30s, so a Secrets-Manager/DNS repoint takes effect in ≤30s without a restart. Also set `connectionTimeout` low so failover fails fast rather than hanging. |
| **HikariCP — force immediate** | `dataSource.evictConnections()` / `HikariPoolMXBean.softEvictConnections()` via JMX at cutover | Drops idle connections now instead of waiting out `maxLifetime` — turns the 30s wait into ~immediate. |
| **PgBouncer** | `RECONNECT;` then `RELOAD;` on the admin console (or `server_lifetime` low) after changing the upstream `host=` in `pgbouncer.ini` | New server connections go to the target; existing ones drain. App pool need not restart. |
| **Node (`mysql2`/`pg`) pool** | low `idleTimeoutMillis` / `maxLifetime`; call `pool.end()` + recreate at cutover | Forces re-resolution of the new endpoint. |

Document the chosen values in `migration-plan.md` as a Phase 7.5 / pre-cutover prep item.

**2. Coordinated vs. rolling restart — pick deliberately.**

- **Coordinated (all-at-once) restart** — stop every write client, repoint, start them all. **Fastest total pause (~10s)** and there is **no split-brain window** because no client is writing during the swap. Use this when a brief full write-pause is acceptable (the default for minimal-downtime cutovers). This is faster than waiting on Secrets Manager pool TTL (up to ~5 min) — change the config/unit and restart directly.
- **Rolling restart** (restart instances one at a time behind a load balancer) — keeps *some* capacity serving, so no hard outage, **but** during the roll some instances point at the old DB and some at the new one → **split-brain writes**. Only safe if the source is already frozen read-only (cutover step 3) *before* the roll begins, so stragglers can't write the old DB. Prefer coordinated restart for write workloads; reserve rolling for read-heavy/stateless tiers.

**3. Deploy RDS Proxy on the TARGET before cutover.** The initial EC2 → RDS/Aurora cutover still incurs the brief pause above — **RDS Proxy does not eliminate the *initial* cutover pause** — but standing it up *before* cutover pays off two ways: (a) point the app at the **proxy endpoint** at cutover instead of the cluster endpoint, so all *future* failovers/maintenance are handled by the proxy (it holds the client connections and reconnects to the new writer in **< 1s, no app restart, no pool refresh**); and (b) the proxy multiplexes the reconnect storm at cutover, so the pool refresh doesn't hammer the new DB. After this migration, failovers become effectively zero-downtime even though *this* cutover paused briefly. (Provision it in Phase 4 — see "RDS Proxy on the target".) Note: true Blue/Green sub-second switchover requires the source to **already be on RDS/Aurora** — it's for RDS→Aurora upgrades, not this initial EC2→RDS move.
