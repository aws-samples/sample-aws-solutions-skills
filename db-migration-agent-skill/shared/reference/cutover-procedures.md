# Zero-Downtime Cutover Procedures

## Overview

The cutover is the highest-risk phase of the migration. This document provides step-by-step procedures for three cutover methods, ranked by recommendation.

**Who executes what** (engagement-safety.md §The three modes): in **Mode 2 — the default —
everything in this file is prepared, rehearsed on a clone, and handed to the customer to
execute; the agent must not freeze the source or repoint clients** (see §The Mode 2
boundary). In **Mode 3** the agent executes these steps itself with a signed A4
authorization and an approver reachable during the window. The procedures below are written
for whoever holds the runbook — the mechanics are identical either way.

**Cache engagements (ElastiCache targets):** client discovery uses `CLIENT LIST` on every
node and the endpoint table, cutover/rollback the cache specifics (ElastiCache cannot
replicate out; stale-cache caveat) — [elasticache-migration.md](elasticache-migration.md)
§10–§11. The freeze-before-repoint order and bidirectional verification below still apply.

Load [method-readiness.md](method-readiness.md) for offline-copy versus live-replication
preconditions. Readiness does not grant execution authority; existing scoped approvals
remain valid for routine reads and explicitly covered recovery actions.

---

## Pre-cutover: Discover DB Clients (MANDATORY — do this first)

Before changing anything, enumerate EVERY client that connects to the source DB. Migrating the DB
without repointing all clients = **split data** (a missed client keeps writing the old DB) or
**outage** (a missed client can't reach the new one). This is the single highest operational risk.

**Start from what the customer already told you, don't re-derive from zero.** Discovery
item #9 (`discovery-questions.md`) already asked the customer to list every
application/service that connects and where each finds its host config — pull that answer
into the inventory table as the starting rows before running Steps 1–4 below. Steps 1–4
are how you *verify* that list and *catch what it's missing* (the customer's team usually
knows their main applications well; what they habitually forget is the long tail — an old
cron job, a nightly batch script, a BI/ELT sync someone set up once) — they are not a
from-scratch rediscovery that ignores what was already said. If a forensic step finds a
client the customer didn't mention, or fails to find one they did mention, surface that
mismatch explicitly and resolve it with them — don't silently prefer one source over the
other.

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

**Mode 2:** the customer performs the host-side checks below and reports redacted
results; the agent provides one focused read-only command block and reconciles the
findings with permitted control-plane metadata and DB sessions. Request exact effective
host overrides, config deployment origin, and writable repoint/revert access. Keep the
application-host SSM Deny and do not bypass it via SSH/another executor; see
[engagement-safety.md](engagement-safety.md) §IAM guardrails. IaC/UserData alone cannot
close the live-config evidence gap. Missing customer evidence keeps the inventory
incomplete. Production pool/ORM preparation below is likewise customer-executed.

Check all of these in **override order** — lower priority numbers take precedence;
verify the application's actual precedence and effective runtime values:

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

### Credential delivery to each client host — planned by Phase 5 / GATE 2, never first at GATE 4

Live (OBS-43): the repoint needed the target password in `app.env` on the app host, and how it
would get there was first worked out just before GATE 4 — a customer operator writing the file
(a cutover-critical manual step on the customer in Mode 3), or installing the AWS CLI and
granting the shared, un-prefixed hybrid SSM role new IAM (against the customer's prefix/no-
shared-role-change rule). The plan must state, **per client host, by Phase 5 / GATE 2 at the
latest**, how the new credentials reach it and how they are reverted, from what discovery #9
found about how the app reads them (`EnvironmentFile`, config file, secret manager, env var):
- **On-host derivation from the existing local config** — the host builds the new config from
  its own current one (e.g. `sed` over `app.env` → `app.env.target`), when the target reuses
  the source credential (set on the target from the customer-provided secret, e.g. a
  CloudFormation dynamic reference); the secret never leaves the host. (Used live.) This is the
  **only** exception to hard constraint 4 ("no credentials in files you generate"), and only
  under all of these conditions:
  - it is an **authorized** step (its A-block names the host, the live file and the staged
    file) and the staged file exists **only on the customer's application host** — never on the
    workstation, in the engagement repo, in `evidence/`, in the handoff bundle or in any log;
  - the staged file gets the **same owner, group and mode as the live config** (e.g. `0600` or
    `0640`, root or the app user — `install -m <mode> -o <owner> -g <group>` or
    `chown --reference`/`chmod --reference`), created that way atomically, never world-readable
    even briefly;
  - it is verified **by allow-list, never by masking** (a `sed` mask leaked `API_KEY`/
    `PRIVATE_KEY`, JSON/YAML password fields, multi-line values and the second URL on a line in
    review): run `shared/scripts/config_diff.py <live> <staged>` **on the host** (copy the script
    there; the config never leaves it). It is **fail-closed**: a value is printed only when the key
    is a connection field (host/endpoint, port, TLS/SSL flag or SSL mode, db index or name, user,
    URL — a key with a secret word such as PASS/SECRET/TOKEN/KEY/AUTH never is) **and** the value
    passes that field's strict pattern (hostname/IP, port 1–65535, boolean, small integer,
    identifier, `[A-Za-z0-9_.@-]{1,64}`; a URL is shown only as `scheme://host:port` — never its
    userinfo, path, query or fragment). Every other key or value is reported only as
    `changed | unchanged | added | removed` from SHA-256 comparisons on the host. Strict `KEY=VALUE`
    env files are parsed (key names printed only if `^[A-Za-z_][A-Za-z0-9_]{0,63}$`); a JSON object is
    compared as a whole — only top-level keys named exactly like a connection field are shown,
    everything else is one `other content: changed (N) | unchanged` line from a type-preserving
    canonical form, and no other JSON key name is ever printed (duplicates → opaque); any ambiguous env
    construct (multi-line or continued values, unterminated quotes, duplicates), YAML and every
    other format are compared as one opaque file (`changed | unchanged` only). No other command prints or copies a value off the host;
  - it is **removed or reverted** once its job is done: the unused one of live/staged is deleted
    after cutover is verified, or — if it is the rollback copy — when the rollback window closes;
    record the deletion in the plan.
- **A Secrets Manager fetch by the host's own role** — only if that role **already** has the
  permission in scope; changing a host role is an IAM change: check the customer's
  prefix/no-shared-resource rule (C-1-style constraints) **before** proposing it, and present it
  as its own A3 block.
- **Customer-operated** — their operator writes the file in a rehearsed, timed step (record it
  as a customer action in the runbook; in Mode 3 it is a dependency, not an agent step).
- **Same password now, rotation in Phase 9** — the target starts with the source's credential;
  rotate after the rollback window, with its own block.
Never pass a secret through SSM `send-command` parameters or a command string — Systems
Manager: "don't include sensitive information formatted as plaintext, such as passwords … All
Systems Manager API activity in your account is logged in an S3 bucket for AWS CloudTrail logs.
This means that any user with access to S3 bucket can view the plaintext values of those secrets"
([Running commands](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-commands.html));
the command history keeps it too.
Rehearse the chosen delivery with the real app and include its revert.

### Completion criterion
**Before cutover:** EVERY client has a reviewed repoint/revert plan **including how its new
credentials are delivered** (above), upstream change staged (not activated), and pool
preparation complete; every replication/CDC consumer
has an approved Step 4 restart plan. This is the client-inventory readiness gate.
**App errors are counted from the app's own error stream, never from a source that cannot
record them.** Live (OBS-57): a cutover report said "0 app errors" from the app's write log,
while its journal held three `TimeoutError('Timeout reading from socket') … reconnecting in 5s`
lines during the write pause. Post-cutover verification counts errors from the app's error
stream (systemd journal / stderr / app log, `journalctl -u <unit> --since <freeze start>`) **and**
from any write/request log, for the pause window and after it separately, and reports them that
way — e.g. "3 timeout + reconnect during the 31.5 s pause, 0 after". "0 errors" is written only
from a source that records errors and was read for the whole interval.

**After authorized cutover:** every client is repointed and verified bidirectionally
(app health + new-DB processlist), and every consumer's plan is executed and verified.
Mode 2 hands over these execution checks; the agent does not repoint to satisfy readiness.

---

## Method 1: Secrets Manager Rotation (Recommended)

**Best for:** Applications that read DB credentials from AWS Secrets Manager.

**Downtime:** 0-30 seconds (depends on application connection pool refresh interval)

### Procedure

Before the freeze, fence **all** application/job writers against reconnecting (roles,
network access, and stopped services), then drain or terminate existing sessions and
transactions. For PostgreSQL, `default_transaction_read_only` is only a new-session
default, not an access-control fence: include inherited/write-capable roles and prepared
transactions, terminate lingering app backends, and prove no writer remains. Keep the
fence until the target is authoritative or rollback is complete. A read-only default
alone, an empty active-query sample, or idle pooled connections do not prove quiescence.

For an offline/full-load-only method, replace CDC steps 1/4/5 with the final complete
copy under this freeze and complete planned final validation per
[method-readiness.md](method-readiness.md); never use the Phase 6 online copy as-is.
Before starting, stage the per-client revert and the recovery checks in
[§Rollback Procedures](#rollback-procedures); a failed repoint can leave changed files
behind even if the old process still serves the source.

The helper refreshes `LAG_WINDOW_START`/`LAG_WINDOW_END` for each query; use
the task/instance identifiers from CloudWatch dimensions, not ARNs. At final drain,
require datapoints timestamped after the freeze for **both** metrics, task status
`running`, zero errored tables, and evidence that the saved source checkpoint applied.
Missing/stale metrics or timeout means abort, not permission to stop forward replication.
The MySQL reseed pipeline below uses on-host `MYSQL_PWD`; rehearse it against the target.

```bash
# Step 1: Verify task health and query CDC lag from CloudWatch
# AWS/DMS dimensions: ReplicationInstanceIdentifier = the friendly instance id; ReplicationTaskIdentifier
# = the task's RESOURCE ID (last ':' segment of the task ARN), NOT the friendly task name — the
# friendly name returns zero datapoints, which reads as "no lag data". Derive it:
DMS_TASK_ID=${TASK_ARN##*:}
show_dms_health_and_lag() {
  LAG_WINDOW_START=$(date -u -d '2 minutes ago' +%FT%TZ)
  LAG_WINDOW_END=$(date -u +%FT%TZ)
  aws dms describe-replication-tasks \
    --filters Name=replication-task-arn,Values=$TASK_ARN \
    --query 'ReplicationTasks[0].{Status:Status,Stats:ReplicationTaskStats,Failure:LastFailureMessage}'
  for metric in CDCLatencySource CDCLatencyTarget; do
    aws cloudwatch get-metric-statistics --namespace AWS/DMS --metric-name "$metric" \
      --dimensions Name=ReplicationInstanceIdentifier,Value="$DMS_INSTANCE_ID" \
        Name=ReplicationTaskIdentifier,Value="$DMS_TASK_ID" \
      --start-time "$LAG_WINDOW_START" --end-time "$LAG_WINDOW_END" \
      --period 60 --statistics Maximum --query 'sort_by(Datapoints,&Timestamp)'
  done
}
show_dms_health_and_lag
# Verify: CDCLatencySource < 5, CDCLatencyTarget < 5, TablesErrored = 0 — EMPTY Datapoints is not
# zero lag: wrong dimension value or no fresh datapoint; do not proceed on it.

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
# PostgreSQL additional default ONLY — the writer/session fence above is mandatory:
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

# Step 4: Re-query fresh CloudWatch datapoints after the freeze; missing/stale is NOT zero.
sleep 30
show_dms_health_and_lag

# Step 5: Stop forward DMS task (source → target)
aws dms stop-replication-task --replication-task-arn $TASK_ARN

# Step 6: Run final validation
# (row count spot-check on 3-5 critical tables)

# Step 7: RESET TARGET HIGH-WATER MARKS — AUTO_INCREMENT / sequences are NOT carried by
# DMS or binlog CDC. Without this, the first inserts on Aurora collide with existing PKs.
# MySQL/MariaDB — emit ALTER statements for every auto_increment column and run them:
mysql -h "$AURORA_ENDPOINT" -u admin -N -B --raw your_db <<'SQL' | mysql -h "$AURORA_ENDPOINT" -u admin your_db
SELECT CONCAT(
  'SELECT COALESCE(MAX(`', REPLACE(COLUMN_NAME, '`', '``'), '`),0)+1 INTO @next_id FROM `',
  REPLACE(TABLE_SCHEMA, '`', '``'), '`.`', REPLACE(TABLE_NAME, '`', '``'), '`; ',
  'SET @ddl=CONCAT(CONVERT(0x', HEX(CONCAT('ALTER TABLE `', REPLACE(TABLE_SCHEMA, '`', '``'),
  '`.`', REPLACE(TABLE_NAME, '`', '``'), '` AUTO_INCREMENT = ')), ' USING utf8mb4), @next_id); ',
  'PREPARE reseed FROM @ddl; EXECUTE reseed; DEALLOCATE PREPARE reseed;')
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND EXTRA LIKE '%auto_increment%';
SQL
# Verify with SHOW CREATE TABLE (AUTO_INCREMENT=<n> > MAX(pk)) or after
# SET SESSION information_schema_stats_expiry=0 — information_schema.TABLES.AUTO_INCREMENT
# is cached for up to 24 h on 8.0+ and reads stale (validation-patterns.md §4 item 3; live OBS-60).
# A test INSERT after SELECT MAX(id) proves it in REHEARSAL only — never on production.
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
python3 - <<'PY'
import json
import os
import boto3
client = boto3.client('secretsmanager')
secret_id = os.environ['APP_SECRET_ID']
secret = json.loads(client.get_secret_value(SecretId=secret_id)['SecretString'])
secret.update(host=os.environ['AURORA_ENDPOINT'], password=os.environ['DB_PASSWORD'])
client.update_secret(SecretId=secret_id, SecretString=json.dumps(secret))
PY

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

**DMS applies ordinary SQL, not a native replication thread.** Before starting reverse
DMS at step 8, keep all source application/job roles and reconnect paths fenced, verify
their sessions are drained, then turn `super_read_only` and `read_only` **OFF** on the
source for the dedicated DMS apply account. Do not rely on a SUPER exemption. For
PostgreSQL, clear the database read-only default for the apply connection while retaining
the application-role/network fence. Rehearse an actual reverse write on the clone;
connection tests alone do not prove apply permission. Never reopen source app writers.

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

- **The reverse writer is a dedicated migration account — never the application account.**
  The account the reverse channel uses to WRITE into the old source (DMS reverse target
  endpoint, or a native replication applier) gets only what applying changes needs, is
  created for this purpose, and is dropped after the rollback window. Live: a run wired its
  reverse DMS target endpoint to the application's own account — mixing rollback writes
  with application identity, widening that account's grants, and making "are app writers
  fenced?" unanswerable from the processlist. For a MySQL-compatible DMS target the
  documented set is `GRANT ALTER, CREATE, DROP, INDEX, INSERT, UPDATE, DELETE, SELECT,
  CREATE TEMPORARY TABLES ON <schema>.*` plus `GRANT ALL PRIVILEGES ON awsdms_control.*`
  (DMS User Guide, MySQL as a target); the reverse SOURCE endpoint (the new target DB) uses
  its own replication-read account (`REPLICATION CLIENT`, `REPLICATION SLAVE`, `SELECT`).
  Creating it on the old source is an A2 write; with native forward binlog replication
  running, create it with `SET SESSION sql_log_bin = 0` so it does not replicate to the
  target (execution-runbooks.md §"Native binlog replication: accounts you create on the
  source replicate to the target"). Give it an explicit plugin the DMS endpoint supports
  (e.g. `IDENTIFIED WITH mysql_native_password` or `caching_sha2_password` over TLS).
- **MySQL/MariaDB**: Aurora's cluster parameter group needs `binlog_format=ROW` so the
  source can consume Aurora's binlog as a replica.
- **PostgreSQL**: Aurora needs `rds.logical_replication=1` and the source needs a free slot.
- Native binlog/logical replication is an alternative only when the **reverse**
  topology and exact versions support it; the forward method is not proof.

#### Reverse-CDC prerequisites — a GATE 4 precondition (MySQL family, DMS reverse task)

Live (CRITICAL): a run's zero-RPO reverse DMS rollback was broken for ~27 minutes right after
cutover — RDS `binlog retention hours` was NULL, task logging was off (no diagnostics), and
the reverse writer could not create DMS's control schema on the old source (`ERROR 1044` on
`awsdms_control`) → crash loop. Every item was checkable beforehand. Before GATE 4 (Mode 3)
or the A4b handover (Mode 2), record each with its evidence:

| ▢ | Check | How to verify | Source |
|---|---|---|---|
| ▢ | Automated backups ON on the RDS for MySQL target (that is what enables its binlog) | `describe-db-instances` → `BackupRetentionPeriod` > 0 | DMS "MySQL as a source" — AWS-managed |
| ▢ | `binlog_format=ROW`, `binlog_row_image=FULL` on the new source (RDS parameter group; Aurora cluster group), sessions recycled after a change | `SHOW GLOBAL VARIABLES LIKE 'binlog_%'` | same |
| ▢ | Binlog retention long enough for the reverse task to catch up after any stop: `CALL mysql.rds_set_configuration('binlog retention hours', 24);` (or more) | `CALL mysql.rds_show_configuration;` — NULL means binlogs are purged ASAP | same |
| ▢ | Reverse DMS **source** account on the new DB: `REPLICATION CLIENT`, `REPLICATION SLAVE`, `SELECT` on the replicated tables | `SHOW GRANTS` | same |
| ▢ | Dedicated reverse **writer** on the old source (never the app account): `ALTER, CREATE, DROP, INDEX, INSERT, UPDATE, DELETE, SELECT, CREATE TEMPORARY TABLES ON <schema>.*` and `ALL PRIVILEGES ON awsdms_control.*` (the default MySQL control schema; if `ControlTablesSettings.ControlSchema` names another schema, grant that one instead) — created under its own A2 block | `SHOW GRANTS` | DMS "MySQL as a target"; "Control table task settings" |
| ▢ | Task settings `"Logging": {"EnableLogging": true}` (log group `dms-tasks-<instance>`, stream `dms-task-<task resource id>`) | `describe-replication-tasks` → `ReplicationTaskSettings` | DMS "Logging task settings" |
| ▢ | Reverse endpoints test-connected, TLS mode per dms-best-practices.md | `aws dms test-connection` | — |
| ▢ | **Reverse task actually run and applying** on the rehearsal pair (execution-runbooks.md §Migration Rehearsal minimum): an INSERT, UPDATE and DELETE on the new side each confirmed on the source-version stand-in, plus CDC-latency alarms on the reverse task (task dimension = ARN suffix) seeing data | row-level evidence + alarm state | — |
| ▢ | Triggers on the old source that app writes fire (audit rows, `updated_at`, event tables): DMS-applied rows fire them too (live: rollback doubled every audit row) — a **disable-at-cutover / re-enable-at-rollback** step pair in the runbook, or a writer guard (`USER()`, not `CURRENT_USER()`), rehearsed with a trigger-side row count | rehearsal count, runbook steps | mysql-postgresql.md §10 |

**Engine-family change (MySQL ↔ PostgreSQL):** the reverse task crosses the family and its
prerequisites differ by direction — a PostgreSQL new side needs `rds.logical_replication=1`
(static; set at creation), `REPLICA IDENTITY FULL` on key-less tables and a logical slot +
LSN captured at the freeze (PostgreSQL sources cannot start CDC from a timestamp); a MySQL
new side needs the rows above. Rollback-side tables keep their original DDL
(`DO_NOTHING`), with `change-data-type` rules for booleans. **A MySQL new primary with
`ON DELETE/UPDATE CASCADE` FKs loses those child changes on the way back to PostgreSQL**
(InnoDB does not binlog cascaded rows; the reverse apply's `session_replication_role = replica`
skips PG's own FK actions): convert the cascades to binlogged app/trigger behaviour before
cutover (preferred), or accept them and, after any rollback, reconcile every child table
reachable through a cascade (`mysql-postgresql.md` §10 R2) with a keyed normalized compare
against the MySQL side — an orphan count (R1) is only a detector and misses a re-created parent
key — and rehearse a parent DELETE (and delete + re-insert) on the new side. Checklists:
[mysql-postgresql.md](mysql-postgresql.md) §10 — same GATE 4 rule.

After any task replacement, re-point the reverse-lag alarms, dashboards and the soak config to
the new task's resource-id dimension and confirm datapoints for it; `INSUFFICIENT_DATA` on a
rollback-path alarm means "not monitored" (OBS-75, `dms-best-practices.md` §Operating DMS tasks).
Then list the replication slots on every PostgreSQL endpoint (slot, active, retained WAL via
`pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)`) and, for each slot of a replaced task, drop it
under its own block or keep it explicitly with a WAL-growth alarm and disk-headroom math (OBS-81:
an orphan slot retained ≈ 117 MB/h).

Endpoint test-connection alone proves reachability, not apply permission or binlog
availability. Any unchecked row blocks GATE 4 unless a waiver with an explicit RPO
acknowledgment is confirmed. **A `running` status is not proof** (live: a reverse task failed
28 s after start while the drill had recorded "running" as success). At cutover step 8 — and
in every rehearsal drill — call rollback "armed" only after a **hold of ≥ 60 s** in which every
`describe-replication-tasks` poll shows `Status=running` with no `LastFailureMessage`, **and** a
probe write on the new primary has landed on the old source (direct row evidence — for an
engine-family change, one probe per replicated table across every type class, compared **by
value** with the normalized cross-engine compare, `mysql-postgresql.md` §10.1/§10.2; the A5 RPO
claim holds only after that value proof), **and**, for **each probed table**, `describe-table-statistics` shows `TableState` ≠
`Table error` and `AppliedInserts`/`AppliedUpdates`/`AppliedDeletes` risen by that table's probes
since a reading taken just before them, the task log shows no apply errors and
`awsdms_apply_exceptions` (where present) is empty for the probe window, **and** the backlog has drained:
`CDCIncomingChanges` counts changes still *waiting* to be applied (DMS: "waiting to be applied
to the target"), so it is a backlog gauge, never applied-work evidence — it must return to ~0.
Order the freeze so the old source is writable for DMS when the task starts: fence the
application accounts (MySQL `ACCOUNT LOCK` + kill sessions; PostgreSQL `NOLOGIN` + terminate),
never the server — `super_read_only` blocks DMS's `awsdms_control` creation **and** `ALTER USER`
even for root (ERROR 1290), so turn `super_read_only` OFF first (`read_only` still holds the app),
lock the app accounts, then `read_only` OFF.

### When Reverse Replication is NOT Possible

Reverse replication is the ideal lossless-rollback mechanism, but it is **impossible** under
several real conditions. Do not require it as the only rollback path:

| Condition | Why it fails |
|-----------|--------------|
| New target cannot emit/retain the required binlog/WAL | Reverse CDC cannot capture its writes. The old source's `log_bin=OFF` alone does not establish whether it can receive reverse SQL apply. |
| Native major-version **downgrade** (e.g. external MySQL 8.0 source ← 8.4 target) | Forward compatibility does not make native 8.4 → 8.0 a supported lossless rollback. Check exact topology/version rules in `version-upgrades.md`; a service-specific exception does not cover an external source. |
| Insufficient privileges | No `REPLICATION SLAVE`/`SUPER` (or PG replication role) to establish the channel. |
| Target is ElastiCache (any engine) | `replicaof`/`sync`/`psync`/`migrate`/`bgsave` are restricted — ElastiCache cannot replicate to a self-managed node. Options: [elasticache-migration.md](elasticache-migration.md) §11. |
| Engine-family change without reverse-DMS prerequisites (MySQL ↔ PostgreSQL) | No native path exists; a PostgreSQL new side without `rds.logical_replication=1` or a slot/LSN taken at the freeze cannot feed a reverse task ([mysql-postgresql.md](mysql-postgresql.md) §10). |

**Alternatives (pick per RPO):**
1. **Application-level write logging** — append every post-cutover write to a durable log
   (append-only table on Aurora, SQS/Kinesis, structured request logs); replay against the source
   on rollback. Closest to lossless.
2. **Shortened sync window** — cut over immediately after the final sync/validation in a
   low-traffic window so the stranded-write loss window is minimal.
3. **Accept the risk with explicit RPO acknowledgment** (low-traffic/non-critical only) — state
   plainly that rollback loses post-cutover Aurora writes, get explicit sign-off, keep the window
   short. Record the acknowledgment in `migration-plan.md`.

Reverse DMS or write-log replay needs its own compatibility and rehearsed apply proof;
do not silently substitute it for an unsupported native reverse path. A shortened
window limits exposure, not losslessness.

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

For an **Aurora Read Replica promotion** instead, keep Phase 6 replication running
through validation/soak. Only at Phase 8, after the readiness gates and source freeze/
final drain, execute `aws rds promote-read-replica-db-cluster --db-cluster-identifier
<cluster-id>`, then verify promotion and repoint clients. Mode 3 requires A4; Mode 2
hands this action to the customer's team. This is not Blue/Green conversion.

**Best for:** Supported in-place upgrades within RDS or within Aurora, not RDS-to-Aurora conversion.

**Downtime:** Typically < 1 minute (managed by AWS)

### Procedure

```bash
# Step 1: Create Blue/Green deployment
aws rds create-blue-green-deployment \
  --blue-green-deployment-name "mysql-upgrade" \
  --source "arn:aws:rds:region:account:db:source-rds-instance" \
  --target-engine-version "$SUPPORTED_RDS_MYSQL_VERSION" \
  --target-db-parameter-group-name "$RDS_MYSQL_PARAMETER_GROUP"

# Step 2: Wait for green environment to be ready and synchronized
aws rds describe-blue-green-deployments \
  --blue-green-deployment-identifier $BGD_ID
# Status should be: AVAILABLE, SwitchoverDetails.Status: AVAILABLE

# Step 3: Switchover (AWS handles the cutover)
aws rds switchover-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --switchover-timeout 300

# Step 4: Verify (endpoint names are swapped — application sees no change)
# The original endpoint now points to the upgraded green environment

# Step 5: Delete blue environment (after monitoring period)
aws rds delete-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --delete-target false  # Keep the old instance for rollback
```

**Limitations:**
- Only works when source is already on RDS (not EC2)
- No RDS-to-Aurora or MySQL-to-MariaDB conversion; validate service/version support.
- Execute switchover only in Phase 8 after validation, soak, readiness, and A4 in Mode 3;
  Mode 2 hands this command to the customer's cutover team.

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

### Contain and classify before failback

Use the instantiated [rollback runbook](../templates/rollback-runbook.md) for the
approved recovery scope. Keep the source app/job fence in place; preserve or establish
the approved target fence, stop writers and drain sessions/transactions, including
prepared transactions. Engine read-only defaults alone are insufficient. If containment
cannot be verified, keep recovery blocked and report the uncertainty immediately.

Record the last **verified** step, every attempted mutation and exit/result, and the
current persistent config, process and session state. A timeout or failed multi-command
script can leave earlier mutations applied. Do not re-run it blindly.

| Observed state | Recovery path |
|---|---|
| No target writes, proven across the switch interval | Restore/verify every changed client config and runtime, reconcile sessions, then perform the source-release checks below. A failed restart alone is not proof of no writes. |
| Target writes confirmed | While fenced, drain and verify the supported reverse path or replay the complete durable write log; otherwise use the explicitly accepted RPO path and document loss. Then reconcile clients and perform source-release checks. |
| Target unavailable, marker lookup failed/missing, write outcome uncertain, or incomplete interval coverage | **Unknown.** Keep source writes fenced and target containment active; use authorized reads/recovery evidence to resolve it. Do not interpret lack of evidence as no target writes or permit unfencing. |

For a “no target writes” finding, correlate the switch journal, writer fence/session
coverage, database/audit evidence and any planned write marker over the whole interval.
A marker absent on a reachable target proves only its absence, not absence of all writes;
an unqueryable marker proves nothing. An absent marker on the source can instead mean
reverse apply is incomplete. Never report zero loss from any of these observations alone.

### Reconcile persistent config, processes and sessions

For **every** client, including ones whose switch command failed:

1. Restore the exact approved source endpoint at each changed persistent layer:
   deployment repo/pipeline revision, systemd unit/drop-in/EnvironmentFile, app config,
   secret version, DNS, task/Deployment revision. Reconcile or pause the deploy controller
   as already authorized so it cannot reapply a target endpoint later. Compare the saved
   original and the effective highest-priority override; redact credentials.
2. Apply the required reload/redeploy and controlled process/pool refresh. For systemd,
   verify the unit reload, then the running process's actual args/environment and
   start identity. An old PID pointing to the source does not undo a changed unit;
   a successful file revert does not refresh a target-connected process.
3. Verify sessions on **both** databases against the full inventory, not just `/health`.
   No target app/job writer or prepared transaction may remain. Start only read-safe
   probes/clients while fences hold; keep clients whose startup writes stopped.
   An unreachable target stays unknown until independent containment and write-state
   evidence resolves it.

### Release source writes only after reconciliation

For reverse CDC, require fresh post-target-freeze lag/error evidence for that mechanism
(DMS: `CDCLatencySource` and `CDCLatencyTarget`), no apply errors, and proof that the
target's final checkpoint/known writes reached the source; `running` or a zero sample
alone is not losslessness. Stop the reverse channel only after that proof. For replay, verify its
coverage, ordering and completion. For acknowledged RPO, establish and record the
actual loss scope against the accepted terms; unknown loss stays unknown and blocked.

Before removing the **source application** fence, verify all of these in the plan:
data reconciled (or loss within accepted RPO), high-water marks reset where applicable,
persistent source config and running clients agree, source-side read/auth checks pass,
target writers are contained, and the applied/remaining recovery steps fit A5. Then
release source writers, verify the authorized source write/read and client sessions,
and exit maintenance. Keep the target isolated and retain evidence/resources.
If a check fails, retain fences; recovery timing comes from the rehearsal, not a generic
“under five minutes” promise.

### Deviation from an approved block = stop and ask (or abort)

During the window, the approved runbook/A4/A5 blocks are the plan. If any step cannot run
as approved — a different endpoint than the one approved (live: repointed to the writer
instead of the approved Proxy because the app could not do TLS), a skipped check, an extra
grant, a changed order — **stop and ask** (present the deviation as its own block), or
execute the pre-agreed abort/rollback criteria. Never proceed and record the deviation
afterwards; an after-the-fact record is a violation (engagement-safety.md §Approvals of
record), not an approval. An already accepted A5 covers recovery within its exact
pre-authorized criteria without another approval request. Uncertain mutations need
reconciliation before dependent writes, not a repeat approval for covered containment
or reads. Changed recovery scope needs the applicable A5; a new cutover attempt needs
a new A4 accepted first. Continue routine reads already authorized by the current mode
and scope.

### Rollback Decision Criteria

| Signal | Severity | Action |
|--------|----------|--------|
| Application error rate > 5% | Critical | Immediate rollback |
| P99 latency > 3x baseline | High | Rollback if not improving in 5 min |
| Connection failures > 1% | High | Check SG/credentials first, rollback if not fixable in 10 min |
| Data integrity concerns | Critical | Immediate rollback, investigate |
| Single slow query | Low | Investigate — likely missing ANALYZE TABLE |

“Immediate rollback” triggers the authorized containment/recovery path above; it never
means blindly reopening source writes while target write state or client config is unknown.

### Post-Rollback Actions

1. Retain the isolated target, task/checkpoint evidence and the rollback record.
2. Investigate root cause and repair within the accepted scope. Re-run the full
   validation phase before another window; refresh affected preparation/rehearsal
   evidence under [method-readiness.md](method-readiness.md).
3. Re-plan the window and applicable authorization. Choose reseed/resume only after
   proving task identity/direction, checkpoint validity, retained logs and data history;
   rollback is not permission to blindly `resume-processing` against divergent data.

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

## Progress during the cutover on a headless runner

When the customer's runner or wrapper shows **only the final message** of a turn (common for
headless / scripted runs — `claude -p` with the default text output, scripted `kiro-cli` turns;
it is a property of the runner, not of headless mode: `claude -p --output-format stream-json
--include-partial-messages` can stream), say so in the GATE 4 presentation: **live per-step
progress is on the dashboard** (each runbook step published `in_progress` → result), **the chat
receives the result** (and any abort) when the turn ends. If the customer wants chat updates per
phase, offer to split the cutover into turns **only at safe checkpoints the runbook allows** —
before the freeze, after the switch is verified — never inside the write freeze, and never in a
way that lengthens it (live, OBS-61; `engagement-safety.md` §Surfacing — Headless turns, rule 9).

## Network-path redundancy — a GATE 4 precheck

Check that every network path the cutover and the rollback window depend on is redundant: for a
site-to-site VPN, both tunnels' status (`aws ec2 describe-vpn-connections --vpn-connection-ids
<id> --query 'VpnConnections[0].VgwTelemetry[].[OutsideIpAddress,Status,StatusMessage,
LastStatusChange]'` — both `UP`); for Direct Connect, the connection and virtual-interface states
(`describe-connections`, `describe-virtual-interfaces` — `available`, BGP up). **A single active
path is a risk the customer must accept explicitly at GATE 4**: an outage during the freeze or the
rollback window has no fallback (live, OBS-84: an engagement cut over with VPN tunnel 2 down the
whole time and the agent never raised it; the same dry run had a ≈ 20-min outage that stalled the
reverse task). Record the state and the acceptance; re-check right before the freeze.

## Go/no-go disclosure — rehearsed vs changed vs estimated (cutover and rollback)

Live (OBS-73): during a production rollback, a new boolean-reconciliation step and a new
precheck had been added to the runbook **after** the last full rehearsal cycle and tested only
standalone, and the go/no-go quoted a write-pause figure that was an estimate. At every go/no-go
(GATE 4, A4/A5 execution, any rollback decision):
- **A step added or changed after the last full rehearsal makes the runbook "unrehearsed".**
  Either re-run a full cycle on the rehearsal copy, or present the gap in the go/no-go message
  itself: which steps were not rehearsed end to end (standalone tests named as such), and what an
  untested failure of each could cause (extended freeze, partial apply, rollback blocked).
- **Every timing figure is labelled** "measured in rehearsal <id/date>" or "estimate (basis)". The
  customer accepts any estimate explicitly — an estimate never stands in for a rehearsed number.
- **Any change to an approved boundary or parameter** — window, scope, batch size, thresholds,
  abort criteria — is disclosed in the go/no-go message, **even when it is safer or wider**; the
  approval covers what was approved, not its improved version.

## Rehearse the production orchestrator, every branch (all engines)

The rehearsal runs the **byte-identical production orchestrator** (same script revision/hash,
same flags and config shape, only the endpoints differ) — not a rehearsal-specific variant — and
**every check branch executes at least once**: the forward health hold, the reverse-readiness
check, each abort path that can be triggered safely, and the rollback branch. Live (OBS-50): the
production cutover's reverse-readiness check read a field the reverse reader type does not
report; the rehearsals had gone through a different check path, so the branch had never run.
Record per branch "executed in rehearsal (evidence)" or, for any rehearsal-only shortcut or
unexecuted branch, list it explicitly as **not proven** at GATE 4.

## Measure the write pause from commit boundaries (all engines)

Live (OBS-62): a production cutover script reported a **0.3 s** write pause; the real pause was
**≈134 s** (independently verified from the app's write log). It took "first write on the new side"
from a business timestamp column with **seconds** granularity instead of a commit boundary; the
agent caught it and switched to an id-boundary check. Rules:
- **Pause = last committed write on the old side → first committed write on the new side.** Prefer
  the application's own write log (each write's completion time, sub-second) or a commit timestamp
  column with **sub-second** precision on each side. Generated IDs (`AUTO_INCREMENT`, sequences) are
  **not guaranteed to be commit-ordered** (concurrent transactions commit out of id order), so an
  id-boundary check — max id on the old side at quiescence vs the first id above it on the new side
  — is a cross-check, not the measurement.
- **Sanity check:** the measured pause can never be shorter than **confirmed quiescence → repoint
  done** — quiescence = the old side drained (no new writes arriving, CDC/replication applied) and
  no open write transactions — from the orchestrator's own timestamps; the freeze command's start
  is not quiescence. A smaller value is a measurement error — investigate and do not report it;
  report the pause only once the two agree.
- **Never measure from second-granularity business timestamps** (`placed_at`, `created_at` at
  `DATETIME(0)`/`timestamp(0)`), from app clocks that are not the commit, or from a single side.

## Minimize the Write-Pause Window (pre-cutover prep)

**Keep full validation out of the freeze unless its measured time fits.** Live (PostgreSQL →
MySQL rehearsal): a 29.7-minute write pause, 27 minutes of it a full normalized compare inside
the freeze; the switch itself was 163 s. In the freeze run quick checks only — counts,
watermark/last-key per table, per-column non-NULL counts, money sums and a keyed compare of the
hot tables' newest keys with `--writes-frozen` (`mysql-postgresql.md` §9) — and run the full
normalized compare right after the switch (with its live-data consistency caveat), or size it
beforehand on a quiesced rehearsal. Budget every freeze step from measured rehearsal timings.


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
- **Rolling restart** (restart instances one at a time behind a load balancer) — keeps *some* capacity serving, so no hard outage, **but** during the roll some instances point at the old DB and some at the new one → **split-brain writes**. Only safe if the source is already frozen read-only (cutover step 3) *before* the roll begins, so stragglers can't write the old DB. Prefer coordinated restart for write workloads; reserve rolling for read-heavy/stateless services.

**3. Deploy RDS Proxy on the TARGET before cutover.** The initial EC2 → RDS/Aurora cutover still incurs the brief pause above — **RDS Proxy does not eliminate the *initial* cutover pause** — but standing it up *before* cutover pays off two ways: (a) point the app at the **proxy endpoint** at cutover instead of the cluster endpoint, so all *future* failovers/maintenance are handled by the proxy (it holds the client connections and reconnects to the new writer in **< 1s, no app restart, no pool refresh**); and (b) the proxy multiplexes the reconnect storm at cutover, so the pool refresh doesn't hammer the new DB. After this migration, failovers become effectively zero-downtime even though *this* cutover paused briefly. (Provision it in Phase 4 — see "RDS Proxy on the target".) Note: true Blue/Green sub-second switchover requires the source to **already be on RDS/Aurora** — it's for supported in-place upgrades, not RDS→Aurora conversions or this initial EC2→RDS move.
