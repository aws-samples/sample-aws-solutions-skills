# Preflight — Environment Preconditions, IAM, and Cost Estimation

> Run this during **Phase 0 (Preflight)**, before any assessment query. Every check must
> pass (or be explicitly waived by the user) before proceeding. On failure: **STOP and
> report** — do not improvise around a missing permission.

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

## 2. IAM — what the migration executor needs

Do **not** run a production migration on `AdministratorAccess` out of habit; propose this
split. Verify effective permissions up front with `aws iam simulate-principal-policy`
rather than failing at step 14 of the cutover. Additionally, apply the **engagement
guardrail policy** ([engagement-safety.md](engagement-safety.md) §IAM guardrails):
read-only session for assessment-only mode, and explicit Denies protecting the source
(no terminate/stop/delete) until the decommission authorization is signed.

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
| Data transfer | one-time | Same-region private = free; cross-region/DX/internet ≠ free; Snow Family per-device |
| Source EC2 kept 7 days post-cutover | one-time | The rollback window is a real cost line |
| Performance Insights, Enhanced Monitoring, Database Activity Streams | monthly | PI free tier = 7 days retention; DAS has Kinesis costs |

Typical shape of the sanity check to state aloud: *"steady-state moves you from an EC2
instance you patch yourself to ~$X/mo managed; the migration itself costs ~$Y one-time,
dominated by the DMS instance and double-running the source for the rollback window."*

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
- **Log exports** to CloudWatch (error/slowquery/audit as the engine provides).

First-24-hours watchlist after cutover: [validation-patterns.md](validation-patterns.md)
§Monitoring Checklist.
