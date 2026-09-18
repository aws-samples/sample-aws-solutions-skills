# Preflight — Environment Preconditions, IAM, and Cost Estimation

> Run this during **Phase 0 (Preflight)**, before any assessment query. Every check must
> pass (or be explicitly waived by the user) before proceeding. On failure: **STOP and
> report** — do not improvise around a missing permission.

## 0. Local tooling (check before §1 — a missing binary makes every later check fail with a raw shell error, not a clean ❌)

```bash
# Required in every mode — everything else in this skill assumes these already work
aws --version
python3 --version

# Required only if the engagement will provision infrastructure — Mode 1 (analysis-only)
# never does (engagement-safety.md's Mode 1 row: read-only assessment, no Create*), so
# skip these entirely for Mode 1. Mode is already known by this point (Phase 0 step 2
# runs before this check).
node --version && npm --version   # needed for cdk (target provisioning is CDK-based)
cdk --version

# Only affects generate_presigned_urls.py (soak dashboard presigned links) — also Mode
# 2/3 only, same reasoning as above
python3 -c "import boto3" 2>&1

# Optional — only affects which source-access path is available; Send-Command
# (running the client that already exists ON the source/target host via SSM) always
# works without these, so their absence is never a blocker, just a narrower menu
mysql --version 2>&1
psql --version 2>&1
```

Report missing **required** items (for the mode in play) as a table with the exact install
command for the detected OS (`uname -s`; ask rather than guess for anything unclear), then
ask once — *"Want me to install these now?"* — same lightweight courtesy check-in as Phase
0 step 1, not silent, since this touches the machine, not just the AWS account. Only run
the install commands after a clear yes. If declined, stop here and let the user install
manually, then re-run this check on the next turn — don't try to work around a missing
binary. **This is about the local machine only — it is not authorization to deploy
anything into AWS itself; `cdk bootstrap` specifically is covered under §1 below, under
the normal A3 infrastructure-deploy rule, not this courtesy check.**

| Missing | Install (Linux) | Install (macOS) |
|---|---|---|
| AWS CLI v2 | `curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip && unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install` | `brew install awscli` (or the official `.pkg`) |
| Node.js + npm | [nvm](https://github.com/nvm-sh/nvm) is the safest default — no `sudo`, no fighting the OS package manager's often-stale version, AND its Node install is user-owned so a later global npm install won't hit `EACCES`. ⚠️ Piping a downloaded script to `bash` executes it with your privileges — this is nvm's own documented install method, but say so plainly rather than calling it "safest" without the caveat; offer to download and show the script first if the user wants to review it. `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh \| bash`, then **source it in the current shell** (`\. "$HOME/.nvm/nvm.sh"` — the installer edits shell profile files but does not affect the shell you're already in) before `nvm install --lts`. Any later non-interactive shell you run commands in (e.g. via SSM) needs that same `source` line first. | `brew install node` (or nvm, same as Linux) |
| AWS CDK CLI | Check `npm config get prefix` first — if it's root-owned (common with an OS-package-manager Node install, not with nvm), `npm install -g aws-cdk` fails with `EACCES`; don't blindly prepend `sudo` to a global npm install. Prefer nvm's Node (avoids this entirely) or `npm config set prefix ~/.npm-global` + add it to `PATH`, then `npm install -g aws-cdk`. | same |
| Python 3 | OS package manager (`dnf install python3` / `apt install python3`) — usually already present | usually already present; `brew install python3` if not |
| `boto3` | `shared/scripts/requirements.txt` is for the soak **Lambda's** runtime only (`pymysql`/`pg8000` — no boto3 in it; don't point here for this). Many distros' Python is `EXTERNALLY-MANAGED` and reject a bare `pip3 install`: create a venv first — `python3 -m venv ~/.venvs/db-migration-agent && ~/.venvs/db-migration-agent/bin/pip install boto3`, then use that venv's `python3` (not the system one) for `generate_presigned_urls.py`. | same (`python3 -m venv` + venv pip, same reasoning) |

Windows: point at the official installer/MSI for whichever is missing rather than trying
to script it — package-manager conventions differ too much to guess safely.

**Credentials configured is a separate question from the CLI being installed.** If
`aws sts get-caller-identity` (§1 below) fails with a credentials/token error specifically
(not "command not found" — that's the check above), don't just log a generic ❌: walk the
user through `aws configure` (access key/secret) or `aws configure sso`, whichever matches
how they said they access AWS.

## 1. Environment preconditions (agent runs these silently)

```bash
# Identity + account — confirm you are in the INTENDED account (demo vs customer!)
aws sts get-caller-identity
# Region actually configured (must match the user's stated target region)
aws configure get region
# Can we see the source? (EC2 source: instance exists, running, SSM-managed?)
aws ec2 describe-instances --instance-ids $SOURCE_INSTANCE_ID \
  --query 'Reservations[].Instances[].[State.Name,PrivateIpAddress,IamInstanceProfile.Arn]'
aws ssm describe-instance-information \
  --filters Key=InstanceIds,Values=$SOURCE_INSTANCE_ID --query 'InstanceInformationList[].PingStatus'
# Target engine version actually available in this region (versions churn)
aws rds describe-db-engine-versions --engine aurora-mysql \
  --query 'DBEngineVersions[].EngineVersion' --output text | tr '\t' '\n' | tail -5
# Existing name collisions
aws rds describe-db-clusters --query "DBClusters[?DBClusterIdentifier=='$TARGET_ID'].Status"
# Service quotas that block mid-flight: RDS instances (L-7B6409FD), DMS instances
aws service-quotas get-service-quota --service-code rds --quota-code L-7B6409FD \
  --query 'Quota.Value'
# CDK bootstrapped? (only if deploying the CDK project)
aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus' 2>/dev/null
```

Report results as a table: ✅/❌ per check. Any ❌ → present the fix, wait for the user.
**CDK bootstrap specifically** is a fix the agent can offer, not just report — but it is a
real infrastructure deploy into the account (an S3 bucket, IAM roles, an ECR repo), so it
falls under action class **A3** (`engagement-safety.md` §Action classes) like any other
target/production infrastructure deploy — **not** the lightweight courtesy check-in §0
uses for local tooling. If missing: propose `cdk bootstrap aws://$ACCOUNT/$REGION`
(confirm the account/region shown above first), append the A3 context-and-mark block to
`authorizations.md`, and wait for its `**Confirmed:**` line before running it — same as
any other first-time infrastructure deploy.

## 2. IAM — what the migration executor needs

Do **not** run a production migration on `AdministratorAccess` out of habit; propose this
split. Verify effective permissions up front with `aws iam simulate-principal-policy`
rather than failing at step 14 of the cutover. Additionally, apply the **engagement
guardrail policy** ([engagement-safety.md](engagement-safety.md) §IAM guardrails):
a read-only session in **Mode 1**, the cutover-capable actions denied in **Mode 2**
(app-secret writes, the app zone's Route 53 records, SSM on application hosts — the
technical backstop for the Mode 2 boundary), and in every mode explicit Denies protecting
the source (no terminate/stop/delete) until the decommission authorization is signed.

| Role | Used in | Key actions |
|------|---------|-------------|
| **migration-operator** (human/agent running the skill) | All phases | `rds:*` on the new cluster + snapshots, `dms:*` on migration resources, `ec2:Describe*`, `ec2:AuthorizeSecurityGroup*` (scoped), `secretsmanager:GetSecretValue/CreateSecret/UpdateSecret` (scoped to the app's secrets), `ssm:StartSession/SendCommand` (scoped to source instances), `cloudwatch:PutMetricAlarm/GetMetricData`, `route53:ChangeResourceRecordSets` (scoped to the zone, only if DNS cutover), `kms:CreateKey/DescribeKey` or use of an existing CMK, `iam:PassRole` for the service roles below |
| **aurora-s3-import-role** (service role, XtraBackup path) | Phase 6 | `s3:GetObject/ListBucket` on the backup bucket + `kms:Decrypt`; trust `rds.amazonaws.com` |
| **rds-s3-integration-role** (Oracle Data Pump) / **rds-backup-restore-role** (SQL Server) | Phase 6 | See [execution-runbooks.md](execution-runbooks.md) one-time setup blocks; trust `rds.amazonaws.com`, scope with `aws:SourceArn` |
| **dms-vpc-role** + **dms-cloudwatch-logs-role** | DMS paths | Exact-name service roles DMS requires in the account (create once per account) |
| **rds-proxy-secrets-role** | RDS Proxy | `secretsmanager:GetSecretValue` on the DB secret; trust `rds.amazonaws.com` |
| **rds-monitoring-role** | Enhanced Monitoring | Managed policy `AmazonRDSEnhancedMonitoringRole`; trust `monitoring.rds.amazonaws.com` |

```bash
# Verify before starting, e.g.:
aws iam simulate-principal-policy --policy-source-arn $OPERATOR_ARN \
  --action-names rds:CreateDBCluster dms:CreateReplicationTask \
    secretsmanager:UpdateSecret ssm:SendCommand route53:ChangeResourceRecordSets \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
```

Cross-account/cross-region notes (KMS key policy grants, snapshot sharing) are in
[method-selection.md](method-selection.md) §Edge-Case Scenarios.

## 3. Cost estimation (present at GATE 2 with the plan)

Give the user an itemized **monthly steady-state** figure and a **one-time migration**
figure before they approve the plan. Use `awslabs.aws-pricing-mcp-server` when connected
([mcp-and-tooling.md](mcp-and-tooling.md)); otherwise CLI pricing or the calculator.

**Itemize:**

| Item | Type | Notes |
|------|------|-------|
| Aurora/RDS instance(s) — migration size | one-time (days) | Sized up per [target-provisioning.md](target-provisioning.md); scale down after |
| Aurora/RDS instance(s) — steady-state | monthly | Writer + readers; Multi-AZ doubles RDS instance cost, is built into Aurora |
| Storage + I/O | monthly | Aurora Standard (pay-per-I/O) vs I/O-Optimized (~+30% instance, free I/O — cheaper when I/O > ~25% of bill) |
| Backup beyond retention free tier | monthly | |
| DMS replication instance | one-time (days–weeks) | Runs until rollback window closes (reverse replication!) — budget the full window, not just the load |
| RDS Proxy | monthly | Priced per vCPU of the target |
| Data transfer | one-time | Same-region private = free; cross-region/DX/internet ≠ free; DataSync has its own per-GB task cost |
| Source EC2 kept 7 days post-cutover | one-time | The rollback window is a real cost line |
| Performance Insights, Enhanced Monitoring, Database Activity Streams | monthly | PI free tier = 7 days retention; DAS has Kinesis costs |

Typical shape of the sanity check to state aloud: *"steady-state moves you from an EC2
instance you patch yourself to ~$X/mo managed; the migration itself costs ~$Y one-time,
dominated by the DMS instance and double-running the source for the rollback window."*

Record the pricing date, currency, assumptions, unpriced items and exclusions in the
plan. At GATE 2 presentation, mirror the totals/ranges and itemization into
`dashboard/status.json`'s `estimates.cost`, the downtime forecast and dependencies into
`estimates.timeline`, and the chosen architecture/method rationale into `strategy`.
Use the schema in [dashboard.md](dashboard.md) §Optional insight fields. An unpriced line
stays unknown and makes the estimate partial; do not turn it into zero. Mirror GATE 2's
pending acceptance into `customer_actions`, then resolve it after the specific confirmation
is recorded. Refresh these fields when scope or rehearsal timings change.

## 4. Monitoring baseline (set up BEFORE cutover, not after)

Capture a **pre-migration performance baseline on the source** while it still serves
production — post-cutover comparisons are meaningless without it:

- Top-20 statements by total time (`performance_schema.events_statements_summary_by_digest`
  / `pg_stat_statements`) + their `EXPLAIN` plans → `migration-plan.md`.
- Peak/typical connections, QPS, p95 latency from the app's own metrics.

On the target, enable at provisioning time (all are in the CDK stacks —
[../patterns/cdk-stacks.md](../patterns/cdk-stacks.md)):

- **Performance Insights** (retention ≥ 7 days) + **Enhanced Monitoring** (60s).
- **CloudWatch alarms**: `CPUUtilization` > 80%, `FreeableMemory` < 10%, 
  `DatabaseConnections` > 80% of `max_connections`, `ReadLatency`/`WriteLatency` > 20 ms,
  `AuroraReplicaLag` > 1000 ms, and during migration `CDCLatencySource`/`CDCLatencyTarget`
  > 30 s on the DMS task → SNS topic the operator actually watches during cutover.
- **Mandatory replication-stall alarm alongside the positive-lag threshold:** for
  RDS MySQL/MariaDB native replicas, alarm on **`ReplicaLag < 0`**. A `-1` is a real
  datapoint (replication inactive or lag unavailable), not healthy zero and not missing
  data, so `treatMissingData: BREACHING` alone cannot detect it. Use `AWS/RDS`, the
  actual replica's `DBInstanceIdentifier`, `Minimum`, a 60-second period,
  `LessThanThreshold`, threshold `0`, and **1 of 1** breaching datapoints → the same
  SNS topic. Keep missing-data treatment `BREACHING` too.
  For an Aurora MySQL **incoming binlog channel**, use **`AuroraBinlogReplicaLag < 0`**
  on its writer instance and a corresponding positive-lag alarm in **seconds**;
  `AuroraReplicaLag` is the intra-cluster reader metric in **milliseconds**, not a
  substitute for source→target binlog monitoring. Keep the existing reader alarm.
  Verify emitted metric/dimensions for the chosen topology, and record a check that
  `0, 0, -1, 0` breaches the stall rule (do not average away or clamp negative values).
  DMS paths retain their CDC-latency and task/error-state monitoring separately.
- **Log exports** to CloudWatch (error/slowquery/audit as the engine provides).

First-24-hours watchlist after cutover: [validation-patterns.md](validation-patterns.md)
§Monitoring Checklist.
