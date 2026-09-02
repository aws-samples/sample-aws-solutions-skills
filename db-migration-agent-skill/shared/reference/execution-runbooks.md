# Execution Runbooks — Method-Specific Migration Procedures

> Read the section for the **approved method only** during **Phase 6 (Execute)**.
> Covers: mysqldump/pg_dump, Percona XtraBackup + S3, DMS Full Load + CDC, Aurora Read
> Replica, Blue/Green, Oracle Data Pump via S3, SQL Server native backup/restore,
> schema-object migration (DEFINER stripping, logins/Agent jobs, TDE certificates), and
> the pre-production rehearsal. DMS deep configuration: [dms-best-practices.md](dms-best-practices.md).

## Method-Specific Procedures

### If mysqldump / pg_dump (small DBs, downtime OK)

```bash
# MySQL: Full export including all schema objects
mysqldump --single-transaction --routines --triggers --events \
  --set-gtid-purged=OFF --column-statistics=0 \
  -h $SOURCE_HOST -u admin -p your_db > full_dump.sql

# Import to Aurora
mysql -h $AURORA_ENDPOINT -u admin -p your_db < full_dump.sql
```

> **Logical dump = a clean major-version-upgrade opportunity.** Because mysqldump/pg_dump re-import the data from scratch, the target can be a **newer major version** than the source (e.g. MariaDB 10.5 → 10.11, MySQL 5.7 → 8.0, PostgreSQL 13 → 16) in the *same* migration — no separate upgrade project. Validate with `CHECKSUM TABLE` that bytes are identical across the version gap (they were 10.5→10.11 in the reference migration). **But version gaps introduce behavioral changes** — reserved-word additions, deprecated/removed functions, changed defaults (charset, sql_mode, auth plugin). Review **[version-upgrades.md](version-upgrades.md)** before choosing the target version. (Physical methods — XtraBackup, snapshot, Read Replica — cannot skip major versions; only logical dump can.)

```bash
# PostgreSQL: Parallel dump/restore
pg_dump -Fd -j 8 -h $SOURCE_HOST -U postgres -d your_db -f /backup/
pg_restore -Fd -j 8 -h $AURORA_ENDPOINT -U postgres -d your_db /backup/
```

> **Check the restore log for role errors, not just the exit code.** This form preserves
> object ownership and grants (unlike the `--no-owner --no-privileges` schema-only variant
> below) — but `ALTER ... OWNER TO`/`GRANT` statements target the SOURCE's role names, and
> `pg_restore` treats a missing target-side role as non-fatal: it logs `role "..." does not
> exist` and moves on, so a clean-looking exit code can still mean an object landed
> owned-by-the-wrong-user with grants silently dropped. Pre-create every role from the
> `source-assessment.md` §Check PostgreSQL role dependencies query on the target *before*
> restoring — this is PostgreSQL's DEFINER-clause equivalent, and the rehearsal (§Migration
> Rehearsal below) is where it should surface if skipped.

### If Percona XtraBackup + S3 (large MySQL, physical)

```bash
# 1. Full backup
export MYSQL_PWD=$(aws secretsmanager get-secret-value --secret-id $SECRET_ID --query SecretString --output text | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])')
xtrabackup --backup --target-dir=/backup --user=admin

# 2. Prepare backup
xtrabackup --prepare --target-dir=/backup

# 3. Upload to S3
aws s3 sync /backup/ s3://your-bucket/xtrabackup/ --sse aws:kms

# 4. Restore to Aurora (via console or CLI)
aws rds restore-db-cluster-from-s3 \
  --db-cluster-identifier your-aurora-cluster \
  --engine aurora-mysql \
  --engine-version 8.0.mysql_aurora.3.07.1 \
  --s3-bucket-name your-bucket \
  --s3-prefix xtrabackup/ \
  --s3-ingestion-role-arn arn:aws:iam::ACCOUNT:role/aurora-s3-role \
  --source-engine mysql \
  --source-engine-version 8.0.36 \
  --master-username admin \
  --master-user-password $NEW_PASS
```

**Requirements:**
- Source: MySQL 5.7 (XtraBackup 2.4) or MySQL 8.0 (XtraBackup 8.0)
- `innodb_file_per_table` must be enabled
- NO encrypted tablespaces (TDE must be off)
- InnoDB page size must be 16 KB (default)

### If DMS Full Load + CDC (zero-downtime)

See [dms-best-practices.md](dms-best-practices.md) for complete DMS configuration including:
- Replication instance sizing
- Source/target endpoint configuration
- Task settings (Full Load + CDC, parallel load, batch apply, LOB handling)
- Table mappings
- Monitoring

**Key prerequisite for CDC:**
- MySQL/MariaDB: `binlog_format=ROW`, `binlog_row_image=FULL`, `log_bin=ON`
- PostgreSQL: `wal_level=logical`, available replication slots

### CDC Proof Probe (any CDC-based method — required before GATE 3, pre-authorized at GATE 2)

A CDC task showing `running` with zero insert/update/delete counters proves the plumbing
is connected, not that it works — an all-zeros state is indistinguishable from "no changes
have happened yet" and "changes aren't actually propagating." This is common on a
just-migrated non-production database where nothing has written since the data was
generated/loaded. Don't sign GATE 3 on an unproven CDC path.

Approving a CDC-based method at GATE 2 pre-authorizes this probe — do not ask again here:

1. Create one throwaway object the CDC task will replicate — a scratch table
   (`<schema>._cdc_probe`, or the engine's equivalent) is cleaner than touching a real
   application table. Never use an existing table for this.
2. Exercise all three change paths against it: one INSERT, one UPDATE, one DELETE.
   UPDATE and DELETE specifically exercise primary-key mapping on the target, which INSERT
   alone does not.
3. Confirm each lands on the target and measure latency per operation — this produces the
   first genuine CDC-latency datapoints, which a zero-traffic parallel-run soak otherwise
   never generates on its own.
4. Drop the probe object on the source. **Verify it is also gone on the target** — some
   CDC mechanisms replicate DDL asymmetrically (DMS: CREATE TABLE replicates, DROP TABLE
   under a task's default settings may not) — don't assume a source-side drop cleaned up
   the target; check.
5. Record the measured latencies and the confirmation in the plan as the GATE 3 evidence
   for "CDC proven," distinct from "CDC running."

Scope stays tight: one throwaway object, created and dropped by the agent as part of
validation, never a real application table, never left behind. If DDL asymmetry is found
in step 4, log it as a pre-cutover schema-drift check for the runbook — any real schema
change during the migration window needs the same "verify both sides" discipline.

### Soak automation (Phase 7.7 — optional, offer it, don't set it up silently)

🔴 **Heterogeneous engagement? Decide this BEFORE recommending Lambda or handing over
`soak_check.py`.** Both scripts below compute row counts/checksums/column fingerprints
using ONE SQL dialect and diff the two sides' results directly — that comparison is only
meaningful when source and target normalize to the same family (MySQL-family or
PostgreSQL-family). For a genuinely heterogeneous pair (e.g. MySQL → Aurora PostgreSQL)
they raise a clear error rather than run the wrong dialect against one side (see
`_engine_family` in either script) — but by the time that fires, you've already promised
the customer automation that doesn't exist for their pair. Check this at Phase 3 method
selection, not at Phase 7.7 setup:
- **Heterogeneous + Full Load only (no CDC)** — there is no continuously-refreshing
  pipeline to soak-check in the first place. Don't propose a soak at all; propose the
  static-validation-window alternative (`engagement-safety.md` §Risk-tiered parallel-run
  default) and record it as a waiver.
- **Heterogeneous + Full Load + CDC** — the DMS task itself IS translating dialects
  continuously, but these scripts can't diff the result across dialects. Propose a
  manual/agent-reviewed process instead: DMS task-level CloudWatch metrics/alarms (engine-
  agnostic, keep using them) plus periodic agent-run dual-read spot checks with hand-
  written per-side queries — not a diffable checksum. Say this explicitly when you make
  the soak-length recommendation; don't let the customer assume "soak" means the same
  automated Lambda checklist a homogeneous engagement gets.

The 8-check soak-report checklist (`shared/templates/soak-report.md`) has two kinds of
check: native CloudWatch alarms (latency/CPU/memory/connections) already run unattended,
but row-count/checksum/schema-drift/storage-headroom don't — nothing autonomous runs them
today unless you set something up. Without automation, a daily soak report only gets
produced when a human re-invokes the agent that day, which is easy to forget across a
multi-day window.

`shared/scripts/soak_check.py` is the **reference implementation** of the mechanical half
of the checklist — row count, checksum, schema drift, alarm state, storage headroom,
replication lag, and replication errors — against source and target directly, no LLM
involvement needed for the routine all-green case. Keep using it as-is for a same-day
check run by hand, or for debugging: it's plain Python + the `mysql`/`psql`/`aws` CLIs,
easy to run from any machine that can reach both databases, over a TLS-verified
connection using a dedicated **read-only** credential (never the admin/master secret —
see §Dedicated read-only credential below). `replication_lag` is measured from DMS
CloudWatch metrics or a native `SHOW REPLICA STATUS`/PostgreSQL replay-lag query when
one of those is configured for the engagement, and is `"not_applicable"` (not `null`)
when neither is — see `dashboard.md`'s field notes for the full 4-state model
(`true`/`false`/`null`/`"not_applicable"`). `customer_test_suite` can never be measured
automatically — it's `"not_applicable"` unless a suite was actually documented as
provided at discovery Q18, in which case it legitimately stays `null` (a real
"needs review," not a bug) until you or the customer's test run supplies a value; never
guess it.

**`soak-config.json` schema** (written once by the agent at Phase 7.7 setup, next to
`status.json`):

```json
{
  "source": {"engine": "mysql", "host": "10.x.x.x", "port": 3306, "user": "soak_ro",
             "password": "...", "ssl_ca": null, "ssl_insecure": true},
  "target": {"engine": "mysql", "host": "target.rds.amazonaws.com", "port": 3306,
             "user": "soak_ro", "password": "..."},
  "tables": ["customers", "orders", "order_items"],
  "checksum_tables": ["customers"],
  "alarm_names": ["target-cpu-high", "target-replica-lag"],
  "target_db_instance_id": "my-target-instance",
  "dms_task_id": "MYTASKID1234", "dms_replication_instance_id": "my-dms-ri",
  "dms_task_arn": "arn:aws:dms:...:task:...",
  "mysql_replica_status_side": null, "pg_replication_lag_side": null,
  "customer_test_suite_provided": false,
  "region": "us-east-1", "n_total": 3
}
```

`source.engine`/`target.engine` are independent (they can legitimately differ across a
version gap; both must still normalize to the same MySQL-family-or-Postgres-family —
heterogeneous soak-checking across families is not yet supported). Set exactly ONE of
`dms_task_id`+`dms_replication_instance_id` (DMS CDC in play), `mysql_replica_status_side`
(`"source"`/`"target"`, whichever side runs `SHOW REPLICA STATUS` as the replica),
or `pg_replication_lag_side` for `replication_lag` — leave all three unset/`null` if this
engagement has no replication mechanism to measure, and the check reports
`"not_applicable"` instead of getting permanently stuck. `dms_task_arn` similarly drives
`replication_errors`.

**TLS — three tiers per side, chosen independently for `source`/`target`, never a
silent plaintext fallback:**
1. Neither `ssl_ca` nor `ssl_insecure` set (the default, and the right choice for an
   RDS/Aurora endpoint) — full verify-full/VERIFY_IDENTITY, chain-verified against the
   [Amazon RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
   bundled with this skill (`shared/assets/rds-global-bundle.pem` for `soak_check.py`; a
   sibling file of the handler in the Lambda asset for `soak_check_lambda.py` — see
   cdk-stacks.md §soak-stack.ts), plus hostname verification. **Not** the platform/OS
   default trust store — confirmed live against a real RDS PostgreSQL instance and a real
   Aurora PostgreSQL cluster that the OS default store does NOT contain the current Amazon
   RDS root CA (only the unrelated generic "Amazon Root CA 1-4" and legacy Starfield
   roots), so relying on it fails chain validation outright. A genuinely non-AWS
   source/target signed by a public WebPKI CA should use tier 2 (`ssl_ca`) instead of
   relying on this default.
2. `ssl_ca` — pins that specific CA certificate as the trust anchor (encrypted +
   chain-verified against it, no hostname check). **Must be the actual CA certificate**,
   not merely a certificate the peer happens to present — confirmed live while building
   this: pinning the LEAF/server certificate returned by e.g. `ssl.getpeercert()` does
   NOT satisfy chain validation, because the self-signed CA that issued that leaf still
   isn't trusted. Point this at a real CA file: the
   [Amazon RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
   for an RDS/Aurora side that needs strict pinning, or the customer's own CA bundle for
   an on-prem source that has one.
3. `ssl_insecure: true` — **explicit opt-in only, never the default.** Encrypts the
   session but skips certificate verification entirely. The real, bounded fallback for
   exactly the case tier 2 can't cover: a self-signed certificate auto-generated by the
   DB engine itself (MySQL 8.0 does this out of the box) on a host with no shell/
   filesystem access to retrieve the actual CA file — confirmed live against exactly
   this shape of on-prem source. Still strictly better than the original bug (no TLS
   negotiated at all); every other case should reach for tier 1 or 2 first.

### Dedicated read-only credential — never the admin/master secret

The soak check only ever needs `SELECT` on the tables it checks, plus whatever
replication-status read access its configured lag mechanism needs (`REPLICATION CLIENT`
for `SHOW REPLICA STATUS` on MySQL/MariaDB; `pg_monitor`/`pg_read_all_stats` — or just
`SELECT` on `pg_stat_replication`, which is world-readable by default — for the
PostgreSQL query). Create a dedicated user for exactly this on **both** source and
target, store each in its own Secrets Manager secret, and point `SOURCE_SECRET_ARN`/
`TARGET_SECRET_ARN` (Lambda) or `soak-config.json`'s `source.password`/`target.password`
(script) at those — never at the cluster's generated admin secret:

```sql
-- MySQL/MariaDB (run on each side, scoped to the actual schema being checked)
CREATE USER 'soak_ro'@'%' IDENTIFIED BY '<generated>';
GRANT SELECT ON ordersys.* TO 'soak_ro'@'%';
GRANT REPLICATION CLIENT ON *.* TO 'soak_ro'@'%';   -- only if SHOW REPLICA STATUS is in use

-- PostgreSQL
CREATE ROLE soak_ro WITH LOGIN PASSWORD '<generated>';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO soak_ro;
GRANT pg_read_all_stats TO soak_ro;   -- only if the pg_last_xact_replay_timestamp query is in use
```

No `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP` — verify this by attempting a throwaway
write with the new credential and confirming it's denied before wiring it into the
Lambda/config.

**Provisioning this needs a privileged credential on that side — a DB-privilege
requirement, not an OS one.** The `CREATE USER`/`GRANT` statements above are plain SQL
over the normal DB connection (no shell on the DB host, no AWS control-plane access —
same as any other query in this skill), but the credential *running* them still needs
`CREATE USER` + `GRANT OPTION` (MySQL/MariaDB) or role-creation privilege (PostgreSQL).
Confirmed live against a real externally-managed MySQL DBaaS source where the only
credential available was the Phase 2 assessment user (scoped to `SELECT` +
`REPLICATION SLAVE`/`CLIENT` only, per this skill's own least-privilege guidance for that
credential): `CREATE USER 'soak_ro'@'%' ...` failed with `ERROR 1227 (42000): Access
denied; you need (at least one of) the CREATE USER privilege(s) for this operation` — a
privilege denial, not an "OS/no-shell-access" one, and it happens identically whether the
source is a managed RDS instance or a true external DBaaS/PaaS with zero AWS
control-plane access (this skill's flagship no-OS-access case — see method-selection.md
row 12a/12b). **Do not** ask the customer to hand over admin/master credentials, or to
permanently widen the read-only assessment credential's grants, just to mint one more
read-only user — instead, if that existing read-only credential is already SELECT-only
with no mutating privileges (the same throwaway-write-denial check above proves it), it
already satisfies everything this section requires: **reuse it directly** as that side's
soak-check credential instead of self-provisioning a fresh `soak_ro`. Only actually run
the `CREATE USER`/`GRANT` statements above on the side(s) where you hold (or the customer
grants, even temporarily) a privileged credential — typically the target, where the
deploying party holds admin access by default. On an externally-managed DBaaS source this
elevated access is often simply not available, and that's fine — it doesn't violate the
"never the admin/master secret" rule above, because the existing read-only assessment
credential already IS the dedicated, minimally-scoped credential this section asks for.

**For the actual multi-day unattended window, the production path is Lambda +
EventBridge Scheduler — recommend this first, not as an alternative.** `shared/scripts/
soak_check_lambda.py` ports the exact same checks (same fields, same null-handling, same
`needs_agent_review` logic) to run as an AWS-managed Lambda: VPC-attached into the *same*
private subnets and security group the migration bastion already uses (identical
reachability to the source over the existing VPN/DX path — no new networking, and it
reuses those subnets' existing NAT gateway for its own AWS API calls), triggered daily by
EventBridge Scheduler, writing directly into the dashboard's S3 bucket (`dashboard.md`
§Presigned-URL viewing) instead of a local file. See
[../patterns/cdk-stacks.md](../patterns/cdk-stacks.md) §soak-stack.ts for the exact CDK.
Get the customer's approval before creating this — it's infrastructure, not something to
stand up invisibly, same as any other stack in this skill.

Why Lambda over a bastion cron by default: a multi-day soak window needs something
running the *whole* time, unattended, with nothing to babysit. A bastion is a persistent
EC2 host — it can be rebooted for patching, its instance can be stopped by an unrelated
cleanup pass, and if SSM Agent or the instance's networking hiccups mid-window, the cron
job silently stops firing with nothing watching it. Lambda + EventBridge Scheduler has no
host to keep alive, no OS to patch, and failed invocations are themselves a CloudWatch
metric you can alarm on — the automation's own reliability no longer depends on a single
long-running box staying healthy for days.

**Fallback — cron on the migration bastion, for a short/low-stakes engagement where
standing up Lambda feels like overkill.** Weaker, but simpler: the bastion is already part
of this engagement's setup and already reachable to both source and target, so there's
zero new infrastructure. Accept that this only makes sense when the soak window is short
(the low tier's 1 day) and non-critical — for the moderate/high tiers, prefer Lambda.

The bastion's IAM role also needs `cloudwatch:DescribeAlarms`, `cloudwatch:GetMetricStatistics`,
and `rds:DescribeDBInstances` (scoped to this engagement's specific alarms/instance) for the
`alarms`/`headroom` checks — confirmed live that a bastion role provisioned only for the
earlier assessment/execution/validation scripts (`secretsmanager:GetSecretValue` + running
`mysql`/`mysqldump`) does NOT have these by default, and their absence doesn't fail the run:
`cloudwatch_alarms`/`db_headroom_pct` swallow the `AccessDenied` and report `null` (the same
"needs review" state as a genuinely missing datapoint), so a permissions gap looks identical
to "not configured yet" unless someone checks *why* it's null. Add this alongside the
`SOURCE_SECRET_ARN`/`TARGET_SECRET_ARN` read access already required for either path.

```bash
# Fallback only — cron, on the migration bastion (never a personal laptop or workstation:
# it sleeps, gets its lid closed, gets rebooted for an OS update, and a missed day produces
# no report with nothing to notice it):
# 0 9 * * * cd /path/to/engagement && python3 shared/scripts/soak_check.py --config dashboard/soak-config.json >> soak.log 2>&1
```

Exit code 0 (standalone script) / a `needs_agent_review: false` result (Lambda) = clean
day, no review needed. Exit code 2 / `needs_agent_review: true` = any check false, or a
check came back `null` unexpectedly — treat this exactly like a RED day until a human or
the agent has actually looked at it, even if the `overall` verdict happened to read green.
The Lambda logs this at `WARNING` rather than raising, so a normal RED/needs-review day
doesn't trip Lambda's own Errors metric — alarm on the log line (a CloudWatch Logs metric
filter on `needs_agent_review=true`, wired in `cdk-stacks.md` §soak-stack.ts) or on
`status.json` directly, not on invocation failure. **A real, actual invocation failure
(the Lambda erroring, or a scheduled invocation never firing/exhausting its retries) is a
separate, genuinely-nobody-noticed failure mode** — don't rely on someone having the
dashboard open. `cdk-stacks.md`'s soak-stack.ts wires both a CloudWatch Alarm on the
function's own `Errors` metric and a dead-letter queue on the EventBridge Scheduler
target (so an invocation that exhausts its retries is still visible), both feeding the
same SNS topic the rest of this skill's monitoring already uses
([preflight-iam-cost.md](preflight-iam-cost.md) §4) — not a second, disconnected alerting
channel.

**Whichever path runs it, a missed run needs to be visible on its own** — a silently
skipped day looks identical to "waiting for tomorrow" otherwise. Both `soak_check.py` and
`soak_check_lambda.py` write `soak.last_checked_at` on every run; the dashboard flags it
directly if more than 36 hours pass without an update (a dedicated check, not the existing
15-minute chat-staleness badge — that one assumes an active session, and would falsely
flag every normal day of a once-daily cadence). Confirm this banner is actually visible on
the dashboard before walking away from a multi-day soak.

**The dashboard still relocates for the soak window specifically — same lifecycle as
before, different destination.** Before and after Phase 7.7, `dashboard/` stays exactly
where it's always been: a local folder, viewed with `python3 -m http.server` per
`dashboard.md`. For the soak window itself, it moves to the S3 bucket the soak-stack
creates — not the bastion — because that's now the single source of truth `soak_check_
lambda.py` reads and writes, and because S3 is what makes the "one link, seamless
re-polling" viewing model possible (`dashboard.md` §Presigned-URL viewing):

1. **Starting soak**: upload the current `dashboard/` contents (`index.html`, `assets/`,
   `status.json` with the soak object seeded, an empty `activity-log.jsonl`) to the bucket,
   once. From then until soak exits, the bucket's copy is the live one — the Lambda reads
   and writes it in place, same principle as the old bastion-copy step, just S3 instead of
   an EC2 disk.
2. **Viewing it during soak**: generate the presigned URLs (next section) and hand over the
   single resulting link — no port-forwarding, no tunnel, no re-download to see an update;
   the page's own 5-second polling keeps working against the presigned URLs until they
   expire.
3. **Ending soak**: download the bucket's final `dashboard/` contents (now holding every
   day's results — `status.json`, `activity-log.jsonl`, every `reports/soak-report-day*.md`)
   back into the engagement working directory, so `migration-plan.md` and everything else
   stay consistent with it afterward — a plain sync, same as the old bastion-copy-back step:
   ```bash
   aws s3 sync s3://<dashboard-bucket-name>/ dashboard/ --exclude "index.html"
   # index.html excluded deliberately: the bucket's copy is the presigned-URL-materialized
   # one (generate_presigned_urls.py) — keep the clean shared/templates/dashboard.html
   # copy locally instead of pulling back one full of soon-to-expire presigned URLs.
   ```

Before and after the soak window, working locally is normal and doesn't need any of this —
say so explicitly, so the customer doesn't come away thinking the dashboard lives in S3 for
the whole engagement.

### Presigned-URL dashboard access (the customer's one link for the soak window)

Run once, right after the soak-stack deploys and the initial `dashboard/` contents
(`index.html`, `assets/`, an empty `activity-log.jsonl`, a `status.json` with the soak
object seeded) are uploaded to the bucket:

```bash
python3 shared/scripts/generate_presigned_urls.py \
  --bucket <dashboard-bucket-name> --expires-seconds 561600   # 561600s = 6.5 days — slightly
                                                                # UNDER the tier's nominal 7
                                                                # days (604800 is the hard SigV4
                                                                # ceiling), so the link itself is
                                                                # never what expires first if
                                                                # soak-exit slips a few hours past
                                                                # the nominal window. Re-running
                                                                # the script (safe, see its
                                                                # docstring) extends it further.
```

This presigns every file the page needs (`index.html`, both assets, `status.json`,
`activity-log.jsonl`) for the same duration, rewrites `index.html` so its CSS/JS/data
references are absolute presigned URLs instead of relative paths (a relative
`fetch('status.json')` from a page loaded via a presigned URL drops the query string
entirely and 403s against a private bucket — this rewrite is what avoids that), and prints
one line prefixed `CUSTOMER LINK:` — that line, and only that line, is what you hand the
customer. One link, valid for the whole soak window; presigned URLs support repeated GETs
until they expire, so the dashboard's existing 5-second polling just keeps working against
it without anything being regenerated mid-window.

🔴 **Read the script's own docstring before running it for a 3- or 7-day tier.** A presigned
URL can never outlive the credentials used to sign it — asking for a 7-day `Expires` while
signing with a temporary/SSO session that itself expires in a few hours produces a URL that
stops working when *that* session ends, not when the URL says it should, and fails with a
confusing signature error rather than a clean "expired" message. For anything past a
same-day (low-tier) window, sign with a throwaway IAM user's long-term access key created
for exactly this purpose, kept alive for the soak's duration, and deactivated right after
soak-exit — not the operator's own role/SSO session.

The dashboard bucket itself stays fully private (blocked public access, no bucket-policy
public-read) — presigned URLs are the only access path, exactly as required everywhere
else in this skill. A basic CORS rule (GET only) on the bucket is still needed because the
page's own fetches to its sibling S3 objects are subject to the browser's CORS rules like
any other cross-resource fetch; see `dashboard.md` for what that rule looks like and why it
was verified in an actual browser, not just with `curl`.

### If XtraBackup Seed + Binlog/DMS CDC Catch-up (large MySQL, minimal downtime — matrix row 6)

Combine the two procedures above: the physical copy does the bulk, CDC closes the delta.

```bash
# 1. Take the XtraBackup exactly as in the previous section. The backup RECORDS the
#    consistent binlog position itself — read it from the prepared backup dir:
cat /backup/xtrabackup_binlog_info        # e.g.  mysql-bin.000042  1337  [gtid-set]

# 2. Restore to Aurora via restore-db-cluster-from-s3 (previous section, steps 3-4).

# 3. Catch up the delta from the RECORDED position — two equivalent channels:
# 3a. Native binlog replication (Aurora as replica of the source):
mysql -h $AURORA_ENDPOINT -u admin -p -e "
  CALL mysql.rds_set_external_source ('$SOURCE_HOST', 3306, '$REPL_USER', '$REPL_PASS',
       'mysql-bin.000042', 1337, 0);
  CALL mysql.rds_start_replication;"
#     Requires a REPLICATION SLAVE user on the source and binlog retention long enough
#     to cover the bulk-copy + restore time: on the source,
#     SET GLOBAL binlog_expire_logs_seconds ≥ (copy+restore hours × 3600) × 2.
# 3b. Or a DMS CDC-only task with --cdc-start-position "mysql-bin.000042:1337".

# 4. Monitor lag until ≈0 (SHOW REPLICA STATUS → Seconds_Behind_Source, or CDC metrics),
#    then hold it running until the cutover window. At cutover (cutover-procedures.md):
#    freeze source → lag=0 → CALL mysql.rds_stop_replication / stop the DMS task → proceed.
```

**The recorded position is the whole game** — a wrong or expired position means silent
duplicate/missing rows. Record it in `migration-plan.md` the moment the backup completes.
(Oracle equivalent: Data Pump seed with the SCN captured via `FLASHBACK_SCN`, then DMS CDC
`--cdc-start-position` at that SCN — matrix row 15.)

### If PostgreSQL Native Logical Replication (EC2/on-prem PG → Aurora PG, near-zero downtime — matrix row 11)

Preferred over DMS for PG→PG: better datatype fidelity, no replication instance to run.
Constraints to state up front: **sequences are NOT replicated** (re-seed at cutover — the
high-water-mark step is mandatory), **DDL is not replicated** (freeze schema changes for
the migration window), every table needs a PK or `REPLICA IDENTITY FULL`, and large
objects (`lo`) are not carried.

```bash
# 0. Prerequisites — SOURCE postgresql.conf (restart if wal_level changes):
#    wal_level=logical, max_replication_slots ≥ 2, max_wal_senders ≥ 2
#    pg_hba.conf: allow replication connection from the Aurora VPC/SG path.
#    TARGET Aurora PG cluster parameter group: rds.logical_replication=1 (reboot).

# 1. Schema first (logical replication moves rows, not DDL):
pg_dump --schema-only --no-owner --no-privileges -h $SOURCE -U postgres your_db \
  | psql -h $AURORA_ENDPOINT -U postgres -d your_db

# 2. On SOURCE: publication for all tables (or an explicit list):
psql -h $SOURCE -U postgres -d your_db -c "CREATE PUBLICATION mig_pub FOR ALL TABLES;"

# 3. On TARGET: subscription — initial data copy + streaming happen automatically:
psql -h $AURORA_ENDPOINT -U postgres -d your_db -c "
  CREATE SUBSCRIPTION mig_sub
  CONNECTION 'host=$SOURCE port=5432 dbname=your_db user=repl_user password=...'
  PUBLICATION mig_pub;"       # creates its own slot on the source

# 4. Monitor: initial sync state per table, then ongoing lag:
psql -h $AURORA_ENDPOINT -c "SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY 1;"
#    srsubstate: i=init, d=copying, s/r=synced+streaming — wait for all 'r'
psql -h $SOURCE -c "SELECT slot_name, confirmed_flush_lsn,
  pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes
  FROM pg_replication_slots;"

# 5. At cutover (cutover-procedures.md order): freeze source → lag_bytes=0 →
#    DISABLE the subscription (ALTER SUBSCRIPTION mig_sub DISABLE;) → re-seed sequences
#    (setval per pg_get_serial_sequence) → repoint clients.

# 6. AFTER the rollback window — drop subscription AND make sure the source slot is gone
#    (an orphaned slot pins WAL on the source until the disk fills):
psql -h $AURORA_ENDPOINT -c "DROP SUBSCRIPTION mig_sub;"
psql -h $SOURCE -c "SELECT pg_drop_replication_slot(slot_name)
  FROM pg_replication_slots WHERE slot_name LIKE '%mig_sub%' AND NOT active;"
```

Version note: source must be PG 10+; cross-major replication (13 → 16) is supported and
is the standard near-zero-downtime major-upgrade path — run the
[version-upgrades.md](version-upgrades.md) validation battery when crossing majors.

### If Aurora Read Replica (RDS MySQL/PG → Aurora)

```bash
# From RDS MySQL → Aurora MySQL Read Replica
aws rds create-db-cluster \
  --db-cluster-identifier aurora-replica-cluster \
  --engine aurora-mysql \
  --replication-source-identifier arn:aws:rds:REGION:ACCOUNT:db:source-rds-instance

# Wait for replica to sync, then promote
aws rds promote-read-replica-db-cluster \
  --db-cluster-identifier aurora-replica-cluster
```

**Only works from RDS (not EC2 directly).** For EC2 → Aurora, use DMS or XtraBackup.

### If Blue/Green Deployment (RDS/Aurora in-place upgrade)

```bash
aws rds create-blue-green-deployment \
  --blue-green-deployment-name "migrate-to-aurora" \
  --source "arn:aws:rds:REGION:ACCOUNT:db:source-instance" \
  --target-engine-version "8.0.mysql_aurora.3.07.1"

# Wait for green to be AVAILABLE and synchronized, then:
aws rds switchover-blue-green-deployment \
  --blue-green-deployment-identifier $BGD_ID \
  --switchover-timeout 300
```

### If Oracle Data Pump (Oracle → RDS Oracle, primary method)

RDS Oracle has **no OS shell** — you cannot run `impdp`/`expdp` on the host. You either call **`DBMS_DATAPUMP`** from a SQL client, or run **`impdp` from a remote Oracle Instant Client** against the RDS endpoint. The dump moves to RDS via **S3 integration** (most common) or **`DBMS_FILE_TRANSFER` over a DB link**.

> **Schema/table mode only — never FULL mode**, and never import SYS/SYSTEM/RDSADMIN-owned objects: a full-mode import can damage the data dictionary. RDS does not grant SYS/SYSDBA.

**One-time setup — S3 integration:** attach an IAM policy (`s3:GetObject`, `s3:ListBucket`, `s3:PutObject`; add `s3:AbortMultipartUpload`+`s3:ListMultipartUploadParts` for ≥100 MB files; add `kms:Decrypt`/`Encrypt`/`GenerateDataKey`/`DescribeKey` for SSE-KMS buckets — bucket must be **same Region**, SSE-C not supported), associate the role for `S3_INTEGRATION`, and add the `S3_INTEGRATION` option to the option group:
```bash
aws rds add-role-to-db-instance --db-instance-identifier my-oracle-target \
  --feature-name S3_INTEGRATION --role-arn arn:aws:iam::ACCT:role/rds-s3-integration-role
aws rds add-option-to-option-group --option-group-name myoptiongroup \
  --options OptionName=S3_INTEGRATION,OptionVersion=1.0 --apply-immediately
```

```sql
-- 1. On TARGET (as master): create the schema + grants (schema mode)
CREATE USER schema_1 IDENTIFIED BY "<password>";
GRANT CREATE SESSION, RESOURCE TO schema_1;
ALTER USER schema_1 QUOTA UNLIMITED ON users;
```

```sql
-- 2. On SOURCE: export with DBMS_DATAPUMP (schema mode). EXCLUDE Scheduler objects
--    owned by system schemas (importing those into RDS is unsupported).
DECLARE v_hdnl NUMBER;
BEGIN
  v_hdnl := DBMS_DATAPUMP.OPEN(operation=>'EXPORT', job_mode=>'SCHEMA', job_name=>NULL);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample.dmp','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_dump_file);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample_exp.log','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_log_file);
  DBMS_DATAPUMP.METADATA_FILTER(v_hdnl,'SCHEMA_EXPR','IN (''SCHEMA_1'')');
  DBMS_DATAPUMP.START_JOB(v_hdnl);
END;
/
-- (expdp equivalent: expdp user/pwd@src DIRECTORY=DATA_PUMP_DIR DUMPFILE=sample.dmp SCHEMAS=SCHEMA_1 PARALLEL=4)
-- For a TDE source, export with ENCRYPTION_MODE=PASSWORD (TRANSPARENT mode is NOT supported by RDS).
-- Each S3 object must be ≤ 5 TiB — use PARALLEL to split larger dumps into multiple files.
```

```sql
-- 3. Upload dump to S3 (from source, if source is also RDS; else use `aws s3 cp` from the OS)
SELECT rdsadmin.rdsadmin_s3_tasks.upload_to_s3(
  p_bucket_name=>'amzn-s3-demo-bucket', p_directory_name=>'DATA_PUMP_DIR') AS task_id FROM dual;

-- 4. On TARGET (as master): download dump from S3 into DATA_PUMP_DIR (async; returns a task id)
SELECT rdsadmin.rdsadmin_s3_tasks.download_from_s3(
  p_bucket_name=>'amzn-s3-demo-bucket', p_directory_name=>'DATA_PUMP_DIR') AS task_id FROM dual;
-- confirm the file landed
SELECT * FROM TABLE(rdsadmin.rds_file_util.listdir('DATA_PUMP_DIR')) ORDER BY mtime;
```

```sql
-- 5. On TARGET (as master): import via DBMS_DATAPUMP. Add METADATA_REMAP for tablespace/schema
--    remap; set TABLE_EXISTS_ACTION=>'REPLACE' to re-run.
DECLARE v_hdnl NUMBER;
BEGIN
  v_hdnl := DBMS_DATAPUMP.OPEN(operation=>'IMPORT', job_mode=>'SCHEMA', job_name=>NULL);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample.dmp','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_dump_file);
  DBMS_DATAPUMP.ADD_FILE(v_hdnl,'sample_imp.log','DATA_PUMP_DIR',NULL,dbms_datapump.ku$_file_type_log_file);
  DBMS_DATAPUMP.METADATA_FILTER(v_hdnl,'SCHEMA_EXPR','IN (''SCHEMA_1'')');
  DBMS_DATAPUMP.START_JOB(v_hdnl);
END;
/
```

```bash
# 5-alt. Or run impdp from a REMOTE Instant Client (bastion/EC2) against the RDS endpoint:
impdp admin@//RDS-ENDPOINT:1521/DBNAME \
  directory=DATA_PUMP_DIR dumpfile=sample.dmp logfile=sample_imp.log \
  schemas=SCHEMA_1 table_exists_action=replace
```

```sql
-- 6. Cleanup — dump files are NOT auto-purged and consume the same EBS volume as datafiles
EXEC UTL_FILE.FREMOVE('DATA_PUMP_DIR','sample.dmp');
```

**Alternative transfer (no S3): `DBMS_FILE_TRANSFER` over a DB link** — create a DB link from source to the RDS endpoint, then `DBMS_FILE_TRANSFER.PUT_FILE(...)` to push `sample.dmp` into the target `DATA_PUMP_DIR`; import as in step 5. Requires VPC routing + security-group ingress between source and target.

**Best practice:** transfer the dump → take a **DB snapshot** → test the import. If objects get invalidated, delete and recreate from the snapshot (the staged dump is included).

**Transportable tablespaces (XTTS, very large EE DBs):** use the dedicated `rdsadmin.rdsadmin_transport_util` package (not the regular impdp path). EE-only, source ≥ 12c, Linux only, target release ≥ source, **no encrypted tablespaces**, cannot transport `SYSTEM`/`SYSAUX` or non-data objects (recreate PL/SQL/views/users/sequences via Data Pump metadata), and the instance must have no read replicas. S3 file limit 5 TiB (EFS recommended for larger). See [aws-official-migration-methods.md](aws-official-migration-methods.md).

### If SQL Server Native Backup/Restore (SQL Server → RDS SQL Server, primary method)

RDS SQL Server has **no OS access** and **no `RESTORE FROM DISK`** — you restore a `.bak` staged in **S3** via the `msdb.dbo.rds_*` procedures, enabled by the **`SQLSERVER_BACKUP_RESTORE`** option.

**One-time setup:** create an S3 bucket in the **same Region**, an IAM role (trust `rds.amazonaws.com`, scoped with `aws:SourceArn` for the DB instance + option group), and add the option:
```bash
aws rds add-option-to-option-group --apply-immediately --option-group-name mybackupgroup \
  --options "OptionName=SQLSERVER_BACKUP_RESTORE,OptionSettings=[{Name=IAM_ROLE_ARN,Value=arn:aws:iam::ACCT:role/rds-backup-restore-role}]"
aws rds modify-db-instance --db-instance-identifier mydbinstance \
  --option-group-name mybackupgroup --apply-immediately   # no restart required
```
The IAM permissions policy needs `s3:ListBucket`,`s3:GetBucketLocation` on the bucket and `s3:GetObject`,`s3:PutObject`,`s3:ListMultipartUploadParts`,`s3:AbortMultipartUpload`,`s3:GetObjectAttributes` on `bucket/*`; add `kms:DescribeKey`/`GenerateDataKey`/`Encrypt`/`Decrypt` on a **symmetric** key for encrypted backups.

```sql
-- 1. On SOURCE: take a native backup, then upload .bak to S3 (aws s3 cp from the source host)
BACKUP DATABASE mydatabase TO DISK = 'D:\backups\mydb_full.bak' WITH INIT, FORMAT, COMPRESSION;
```
```bash
aws s3 cp D:\backups\mydb_full.bak s3://my-bucket/sqlbackups/mydb_full.bak
```
```sql
-- 2. On RDS: restore (single file → DB comes online; @with_norecovery defaults to 0 for FULL)
exec msdb.dbo.rds_restore_database
  @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/sqlbackups/mydb_full.bak';

-- 3. Monitor (status refreshes ~every 5%; history kept 36 days)
exec msdb.dbo.rds_task_status @db_name='mydatabase';
-- cancel:  exec msdb.dbo.rds_cancel_task @task_id=<n>;   (cannot cancel FINISH_RESTORE)
```

**Multifile backup (large DBs, ≤10 files, parallel throughput):** the `*` is expanded to `1-of-N`, etc.:
```sql
exec msdb.dbo.rds_backup_database @source_db_name='mydatabase',
  @s3_arn_to_backup_to='arn:aws:s3:::my-bucket/out/backup*.bak',
  @number_of_files=4, @max_transfer_size=4194304, @buffer_count=10;
-- restore by giving the common prefix + '*'
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/out/backup*';
```

**Minimal-downtime sequence (FULL + DIFFERENTIAL + LOG)** — source must be in **FULL recovery model**. Restore the big backups ahead of time `WITH NORECOVERY`, apply the final log at cutover `WITH RECOVERY`:
```sql
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_full.bak', @type='FULL', @with_norecovery=1;
exec msdb.dbo.rds_restore_database @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_diff.bak', @type='DIFFERENTIAL', @with_norecovery=1;
exec msdb.dbo.rds_restore_log @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_log1.trn', @with_norecovery=1;
-- final log at cutover brings the DB online (rds_restore_log defaults to NORECOVERY=1, so set 0)
exec msdb.dbo.rds_restore_log @restore_db_name='mydatabase',
  @s3_arn_to_restore_from='arn:aws:s3:::my-bucket/mydb_logN.trn', @with_norecovery=0;
-- or, if the last task was left NORECOVERY:
exec msdb.dbo.rds_finish_restore @db_name='mydatabase';
```
`rds_restore_log` supports `@stopat='2026-06-04 03:57:09'` for point-in-time. Drop a stuck restore: `exec msdb.dbo.rds_drop_database @db_name='mydatabase';`

**Constraints:** S3 bucket same Region as instance; cannot restore over an existing DB name; 5 TB per file, native restore up to 64 TiB (Express 10 GB); up to 2 concurrent tasks; **cannot back up to a `.bak` from RDS for log shipping (no native log backups from RDS)**; a `.bak` from a *higher* engine version won't restore; FILESTREAM filegroups rejected; Multi-AZ native restore requires FULL recovery model; not supported with cross-Region read replicas; KMS must be symmetric; procedures can't run inside a transaction. **Logins, SQL Agent jobs, and linked servers are NOT in a user-DB `.bak`** — migrate them separately (§"SQL Server — Server-Level Objects" below).

---

## Schema Object Migration (If Method Doesn't Include Them)

If you used DMS, binlog replication, or S3 import — schema objects must be migrated separately:

```bash
# MySQL: Export schema objects only (no data)
mysqldump --routines --triggers --events --no-data --skip-lock-tables \
  -h $SOURCE -u admin -p your_db > schema_objects.sql

# Remove DEFINER clauses (they break on Aurora)
sed -i 's/DEFINER=[^*]*\*/\*/g' schema_objects.sql

# Import to target
mysql -h $TARGET -u admin -p your_db < schema_objects.sql
```

Two gotchas, both confirmed against a live MySQL 8.0 source:
- **Do not add `--no-create-info`.** It silently drops views with no error — mysqldump
  emits a view via a temporary placeholder `CREATE TABLE` + a later `CREATE VIEW`
  replacement, and `--no-create-info` suppresses both halves of that mechanism. Triggers
  and procedures are unaffected; only views vanish. The placeholder
  `DROP TABLE IF EXISTS`/`CREATE TABLE` pair that appears per view in the output is
  expected and harmless on import — there's no real base table by that name to collide
  with. If you want to avoid the mechanism entirely, `SHOW CREATE VIEW <name>` is a clean
  explicit alternative with the same minimal privileges.
- **`--skip-lock-tables` is required against a genuinely read-only account** (SELECT +
  SHOW VIEW + TRIGGER + EVENT, no LOCK TABLES) — exactly what a managed-DB provider or a
  cautious customer typically hands out. Without it, mysqldump fails outright with
  `Access denied ... when using LOCK TABLES`, even for a `--no-data` dump. Safe here since
  there's no data being read for consistency to worry about.

### Load Order — Tables, Then Data, Then Views/Procs, Then Triggers Last

Don't apply `schema_objects.sql` in one shot alongside the table structure. Sequence it:

1. **Create base tables** on the target — structure only, no triggers yet.
2. **Bulk-load the data** (DMS Full Load, mysqldump restore, or physical backup restore).
   No triggers exist on the target yet, so there's no per-row trigger overhead during the
   load and no risk of triggers double-processing logic the source side already applied
   before the data was captured.
3. **Once the data load completes** (and, if running CDC, once it's caught up to
   near-real-time) — create views, procedures, functions, and events. These don't touch
   existing rows; they only need the table structure to exist, which it already does.
4. **Create triggers last, immediately before cutover** — not "create then disable."
   MySQL triggers have no native enable/disable toggle; the only way to keep one inactive
   during the load is to defer its creation entirely until you're ready for it to be live.
5. **At cutover**, triggers are now active, so genuinely new application writes (i.e.,
   ones that happen after cutover) get the trigger logic applied fresh — matching how they
   behaved on the source.

Concretely: a `BEFORE INSERT` trigger that defaults a NULL column (or, more generally, any
trigger with side effects — audit logging, counters, notifications) would otherwise fire
on every single bulk-loaded historical row during Full Load. That's both a performance
cost (millions of trigger invocations for data that's just being copied, not created) and
a correctness risk (replaying migration-time bulk data through logic that was written for
live application traffic, not backfill).

### Target Hygiene During Load/CDC — Backups and Multi-AZ Off Until Cutover

Same principle as deferring triggers, applied to the target instance itself, for any
method: **turn off automated backups and Multi-AZ on the target while the load/CDC window
is open**, then re-enable both as part of the cutover sequence, before the rollback window
starts (`post-migration.md`'s T+1h→T+24h watch is also where you'd confirm this actually
happened, not just that it was planned). Backups taken mid-load are backups of a
half-loaded, not-yet-consistent database — worthless as a recovery point and pure overhead
on the target during exactly the window you want its write throughput unconstrained.
Multi-AZ during this window adds synchronous replication overhead for no benefit, since
the target isn't serving production traffic yet. Re-enabling both is a single parameter
change each; do it before you consider cutover complete, not as an afterthought days later.

```bash
# PostgreSQL: Functions, triggers, views, types
pg_dump --schema-only --no-owner --no-privileges \
  -h $SOURCE -U postgres your_db | \
  grep -v 'COMMENT ON EXTENSION' > schema.sql

psql -h $TARGET -U postgres -d your_db -f schema.sql
```

### Oracle — Objects NOT Carried by a Schema-Mode Data Pump

Schema-mode Data Pump brings in-schema objects (procs, triggers, views, sequences). It does **not** bring SYS/SYSTEM-owned Scheduler jobs (intentionally excluded), nor will arbitrary directory objects / ACLs work as-is on RDS:
- **Scheduler jobs**: recreate **app-owned** `DBMS_SCHEDULER` jobs on the target (migrate any legacy `DBMS_JOB` to `DBMS_SCHEDULER`). Never recreate SYS/SYSTEM-owned jobs.
- **Network ACLs** (for `UTL_HTTP`/`UTL_SMTP`/`UTL_TCP`): re-grant with `DBMS_NETWORK_ACL_ADMIN` on the target and confirm VPC egress.
- **Database links**: recreate; they need VPC routing + security-group rules and updated TNS descriptors.
- **Directory objects / external tables / BFILE**: re-stage through RDS-managed directories (the master user lacks `CREATE ANY DIRECTORY`).

### SQL Server — Server-Level Objects (logins, Agent jobs, linked servers)

A user-DB `.bak` carries **database users** but not **server logins** → SQL-auth users are **orphaned** after restore (login SID mismatch). Fastest fix: recreate logins on RDS with the **same SID + HASHED password** so the orphan auto-resolves.

```sql
-- On SOURCE: generate CREATE LOGIN statements preserving hash + SID
SELECT 'CREATE LOGIN ' + QUOTENAME(p.name) +
  CASE WHEN p.type_desc='SQL_LOGIN'
    THEN ' WITH PASSWORD = ' + CONVERT(NVARCHAR(MAX),l.password_hash,1) +
         ' HASHED, SID = ' + CONVERT(NVARCHAR(MAX),p.sid,1) + ';'
    ELSE ' FROM WINDOWS;' END
FROM sys.server_principals p
LEFT JOIN sys.sql_logins l ON p.principal_id=l.principal_id
WHERE p.type_desc IN ('SQL_LOGIN','WINDOWS_LOGIN','WINDOWS_GROUP')
  AND p.name NOT LIKE '##%##' AND p.name <> 'sa'
  AND p.name NOT LIKE 'NT SERVICE%' AND p.name NOT LIKE 'NT AUTHORITY%';
```
```sql
-- On RDS, after restore: if a user is still orphaned, relink (preferred over deprecated sp_change_users_login)
USE [mydatabase];
EXEC sp_change_users_login 'Report';     -- list orphans
ALTER USER [appuser] WITH LOGIN = [appuser];
```
- **SQL Agent jobs** live in `msdb` (not importable) — script them out on the source and recreate (no CmdExec/PowerShell/replication steps, no email/alerts on RDS).
- **Linked servers** are server-level — recreate manually (Oracle OLEDB has a dedicated RDS option).
- **CLR**: `SAFE` assemblies only on ≤2016; not supported 2017+ — refactor.

### TDE-Encrypted Source — Bringing the Certificate / Re-encrypting

- **Oracle**: you **cannot import your own wallet**. Export with Data Pump `ENCRYPTION_MODE=PASSWORD`, import into a TDE-enabled target whose wallet **AWS generates** (`SELECT * FROM v$encryption_wallet;` to confirm). Create encrypted tablespaces normally: `CREATE TABLESPACE enc_ts ENCRYPTION USING 'AES256' DEFAULT STORAGE(ENCRYPT);`. The `TDE` option is permanent — to remove it you must export to a non-TDE instance.
- **SQL Server**: bring the **source TDE certificate** in first via `rds_restore_tde_certificate` (cert name must start with `UserTDECertificate_`). On the source, `BACKUP CERTIFICATE ... WITH PRIVATE KEY`, where the private-key password is the **plaintext** of a KMS data key (`aws kms generate-data-key --key-spec AES_256`); upload `.cer`/`.pvk` to S3 and tag the `.pvk` object with `x-amz-meta-rds-tde-pwd` = the KMS `CiphertextBlob`:
  ```sql
  EXECUTE msdb.dbo.rds_restore_tde_certificate
    @certificate_name='UserTDECertificate_mycert',
    @certificate_file_s3_arn='arn:aws:s3:::cert-bucket/tde-cert.cer',
    @private_key_file_s3_arn='arn:aws:s3:::cert-bucket/tde-key.pvk',
    @kms_password_key_arn='arn:aws:kms:us-west-2:ACCT:key/<key-id>';
  ```
  Then `rds_restore_database` the TDE `.bak`; RDS **auto-rekeys** the restored DB to an RDS-managed `RDSTDECertificate` before it becomes available. Constraints: both `SQLSERVER_BACKUP_RESTORE` + `TDE` options required; **TDE cert restore not supported on Multi-AZ** (do it Single-AZ, then convert); max 10 user certs; no cross-account KMS keys.

---

## Migration Rehearsal (STRONGLY Recommended Before Production)

> **If the customer declines a clone rehearsal** (common for small DBs — "too much
> cost/time"): do not silently skip. (1) State plainly what risk moves into the production
> window — untested cutover mechanics, unmeasured time budget. (2) Get the waiver recorded
> in `migration-plan.md`. (3) Recover what rehearsal value you can without a clone:
> component-test every freeze-window command against the REAL target (syntax differs
> across engine versions), pre-stage the cutover scripts on-host (see cutover-procedures.md
> §"ONE Pre-Staged Script per Host"), and treat the forward seed+catch-up run as your
> timing measurement. The canonical failure this prevents: an untested
> `SHOW BINARY LOG STATUS` (MySQL 8.4 syntax) against Aurora 8.0, discovered mid-freeze,
> turning a 40-second window into a 5-minute one.

**Run the rehearsal concurrently with the parallel-run soak, not after it.** They verify
different things and have no dependency on each other — rehearsal measures the cutover
*procedure itself* (on a clone), soak measures *replication durability over time* (on the
real target). Nothing about the rehearsal needs the soak clock to finish, and nothing
about the soak needs the rehearsal to finish first. Serializing them (waiting for N green
days before even starting the rehearsal) burns calendar time for no safety benefit — start
the rehearsal as soon as it's schedulable, in parallel with the soak already running.

Before executing against production, perform a full dry-run:

1. **Create a source clone**: Snapshot the source EC2, launch a clone in same VPC.
2. **Run the full migration against the clone** (all phases).
3. **Measure actual time**: Record duration of each phase.
4. **Validate cutover procedure**: Practice end-to-end.
5. **Verify rollback**: Test the rollback procedure works.
6. **Destroy the clone**: Delete all rehearsal resources.

This de-risks production by: confirming time estimates, catching permission/network/compatibility issues, giving team confidence, and providing a realistic timeline for stakeholders.
