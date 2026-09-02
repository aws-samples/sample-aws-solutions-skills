# CDK Patterns — Target Infrastructure for the Migration

> Generate the CDK project during **Phase 5 (Provision)** from these patterns. TypeScript,
> aws-cdk-lib v2, Constructs v10, strict mode. One stack per concern; all magic values in
> `lib/config/constants.ts`. Stacks marked *(conditional)* are generated only when the
> approved plan needs them.

## Project layout (the deliverable)

```
{prefix}-migration/
├── bin/app.ts
├── lib/
│   ├── config/constants.ts        ← ALL tunables: ids, sizes, CIDRs, retention, tags
│   └── stacks/
│       ├── network-stack.ts       ← VPC lookup + subnet group + security groups
│       ├── security-stack.ts      ← KMS CMK + Secrets Manager + IAM service roles
│       ├── database-stack.ts      ← Aurora/RDS cluster + BOTH parameter groups
│       ├── proxy-stack.ts         ← RDS Proxy (conditional: minimal-downtime plans)
│       ├── migration-stack.ts     ← DMS instance/endpoints/tasks (conditional: DMS paths)
│       └── monitoring-stack.ts    ← Alarms + dashboard + SNS
├── scripts/{01-precondition-check,02-deploy,03-execute-migration,
│           04-validate,05-cutover,06-rollback}.sh
├── cdk.json  package.json  tsconfig.json  README.md
```

`bin/app.ts` wires `addDependency()` in order: network → security → database → proxy →
migration → monitoring. Tag everything via `Tags.of(app).add(...)` from constants
(`Project`, `Owner`, `Environment`, `CostCenter`, `CreatedBy: cdk`).

## network-stack.ts — import, don't create

The source's VPC already exists. Look it up; never create a new VPC for a migration.
This applies to the **target's** VPC too, whether or not the source is EC2-hosted: when
the source is in AWS (same account/VPC as the target), `VPC_ID` is naturally that VPC.
When the source is external (on-prem/another cloud — nothing called "the source's VPC"
exists in AWS at all), `VPC_ID` still comes from discovery, not from the agent deciding
to provision one — it's whatever existing target VPC the customer confirmed in Phase 1
discovery input #2 (`target-provisioning.md` §Network Placement). Only synthesize a new
VPC/subnet group when the customer has explicitly confirmed there's no existing target
infrastructure to reuse (greenfield/PoC).

```typescript
const vpc = ec2.Vpc.fromLookup(this, 'Vpc', { vpcId: constants.VPC_ID });

const dbSg = new ec2.SecurityGroup(this, 'DbSg', { vpc, allowAllOutbound: false,
  description: `${constants.PREFIX} target DB` });
// Ingress ONLY from the app tier SGs discovered in Phase 2 + the DMS SG — never 0.0.0.0/0
for (const sgId of constants.APP_CLIENT_SG_IDS) {
  dbSg.addIngressRule(ec2.Peer.securityGroupId(sgId), ec2.Port.tcp(constants.DB_PORT),
    `app client ${sgId}`);
}

new rds.SubnetGroup(this, 'DbSubnets', { vpc,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  description: 'DB subnets (private)' });
```

Pitfalls: `Vpc.fromLookup` needs `env: { account, region }` set on the stack (no
env-agnostic synth); DMS needs its own SG allowed **into both** the source SG and `dbSg`.

## security-stack.ts

```typescript
const key = new kms.Key(this, 'DbKey', { enableKeyRotation: true,
  alias: `${constants.PREFIX}-db`, removalPolicy: RemovalPolicy.RETAIN });
```

Also here: the service roles from
[../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §2 that the plan
needs (aurora-s3-import-role for XtraBackup, `dms-vpc-role` — exact name, only if absent
in the account — rds-proxy-secrets-role, rds-monitoring-role).

**Do NOT create the DB credentials secret here.** Only the KMS key belongs in
security-stack. Generate the secret *inside* database-stack via
`Credentials.fromGeneratedSecret()` (see below) — creating it in security-stack and
attaching it to the instance from database-stack via `Credentials.fromSecret()` is a
confirmed cyclic-dependency trap (next section).

## database-stack.ts — the immutability trap lives here

Everything flagged "fixed at creation" in
[../reference/target-provisioning.md](../reference/target-provisioning.md) must come from
constants and be user-confirmed at GATE 2: engine version, KMS key, Oracle charset /
DB_BLOCK_SIZE, SQL Server collation, license model, port.

```typescript
// TWO cluster parameter groups — deploy with migration PG, swap to production PG later.
const migrationParams = new rds.ParameterGroup(this, 'MigrationParams', { engine,
  description: 'import-optimized', parameters: {
    // MySQL-family examples; engine-specific values live in constants.ts
    max_allowed_packet: '1073741824',
    innodb_flush_log_at_trx_commit: '2',          // relax durability DURING IMPORT ONLY
    foreign_key_checks: '0', unique_checks: '0',   // if the method needs them
    binlog_format: 'ROW',                          // needed for REVERSE replication later
  }});
const productionParams = new rds.ParameterGroup(this, 'ProductionParams', { engine,
  description: 'steady-state', parameters: {
    binlog_format: 'ROW',
    require_secure_transport: constants.ENFORCE_TLS ? 'ON' : 'OFF',
    time_zone: constants.SOURCE_TIME_ZONE,         // match source — Phase 1 adjustment
  }});

const cluster = new rds.DatabaseCluster(this, 'Cluster', {
  // Generate the secret HERE, not in security-stack — see pitfall below.
  // Full connection contract (host/port/dbname/engine) comes from secretStringTemplate;
  // Phase 7.5 discovers whether the app's existing secret has `host`; this one always does.
  engine, credentials: rds.Credentials.fromGeneratedSecret('admin', {
    secretName: `${constants.PREFIX}/db-credentials`,
    encryptionKey: key,   // security-stack's KMS key — one-way reference, no cycle
  }),
  writer: rds.ClusterInstance.provisioned('writer', {
    instanceType: constants.MIGRATION_INSTANCE_TYPE,   // sized UP for import
    enablePerformanceInsights: true }),
  readers: constants.READER_COUNT > 0
    ? [rds.ClusterInstance.provisioned('reader1', { promotionTier: 1 })] : [],
  vpc, securityGroups: [dbSg], subnetGroup,
  storageEncryptionKey: key, parameterGroup: migrationParams,
  backup: { retention: Duration.days(constants.BACKUP_RETENTION_DAYS) },
  deletionProtection: true, removalPolicy: RemovalPolicy.RETAIN,
  cloudwatchLogsExports: constants.LOG_EXPORTS, monitoringInterval: Duration.seconds(60),
});
new CfnOutput(this, 'WriterEndpoint', { value: cluster.clusterEndpoint.hostname });
```

Notes: `deletionProtection: true` + `RETAIN` always — a migration target holds production
data the moment CDC starts. XtraBackup path uses `restore-db-cluster-from-s3` (no CDK L2)
— run it from `scripts/03-execute-migration.sh`, then adopt monitoring around it; don't
fight CDK into importing it mid-migration. RDS (non-Aurora) targets: `rds.DatabaseInstance`
with `multiAz: true` — same parameter-group pair pattern.

**Pitfall — cyclic cross-stack dependency (confirmed live, not theoretical):** never create
the Secret in security-stack and hand it to database-stack via
`credentials: rds.Credentials.fromSecret(dbSecret)`. RDS's L2 construct attaches the
secret to the instance/cluster ARN, which forces security-stack (owner of the Secret) to
depend on database-stack (for the instance ref) — while database-stack already depends on
security-stack for the KMS key. CDK throws `Adding this dependency ... would create a
cyclic reference` on synth. Fix: generate the secret *inside* database-stack with
`Credentials.fromGeneratedSecret('admin', { secretName, encryptionKey: key })` as shown
above — the KMS key reference still flows one-way (security → database), so there's no
cycle. `key` is the only thing security-stack should export.

## migration-stack.ts (conditional — DMS paths)

CDK has only L1s (`CfnReplication*`) for DMS. Keep it thin and readable:

```typescript
const dmsSg = new ec2.SecurityGroup(this, 'DmsSg', { vpc });
const subnetGrp = new dms.CfnReplicationSubnetGroup(this, 'DmsSubnets', {
  replicationSubnetGroupDescription: 'dms', subnetIds });
const instance = new dms.CfnReplicationInstance(this, 'DmsInstance', {
  replicationInstanceClass: constants.DMS_INSTANCE_CLASS,   // never t-family in prod
  allocatedStorage: 100, multiAz: constants.PROD,
  replicationSubnetGroupIdentifier: subnetGrp.ref,
  vpcSecurityGroupIds: [dmsSg.securityGroupId], publiclyAccessible: false });

const sourceEp = new dms.CfnEndpoint(this, 'SourceEp', { endpointType: 'source',
  engineName: constants.SOURCE_ENGINE, serverName: constants.SOURCE_HOST,
  port: constants.DB_PORT, databaseName: constants.DB_NAME,
  username: constants.DMS_USER, password: constants.DMS_PASSWORD_FROM_SECRET });
// target endpoint analogous, pointing at cluster endpoint

// FORWARD task (full-load-and-cdc) AND REVERSE task (cdc, created stopped) — the reverse
// task is part of the plan, not an afterthought. Task settings JSON from
// ../reference/dms-best-practices.md; table mappings from constants.
```

Secrets-in-CFN caveat: prefer `SecretsManagerAccessRoleArn`/`SecretsManagerSecretId` on
endpoints over inline passwords. Test both endpoints post-deploy in
`scripts/01-precondition-check.sh` via `aws dms test-connection`.

## proxy-stack.ts (conditional) / monitoring-stack.ts

Proxy: `rds.DatabaseProxy` with `requireTLS: true`, secret-based auth, the app SGs allowed
in — output the proxy endpoint; Phase 8 points clients at it. Monitoring: the alarm set
from [../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §4 + a
dashboard with source-vs-target panels during the migration window, all → one SNS topic.

## soak-stack.ts (conditional — Phase 7.7 automated soak checks, the recommended path)

Generated when the customer approves automating the Phase 7.7 checklist (see
[../reference/execution-runbooks.md](../reference/execution-runbooks.md) §Soak automation —
get approval before creating this, it's infrastructure like everything else here). Pieces:
an S3 bucket that becomes the soak window's single source of truth for the dashboard, a
VPC-attached Lambda that ports `shared/scripts/soak_check_lambda.py`'s logic, an
EventBridge Scheduler rule that invokes it daily, and CloudWatch alarms (on Lambda errors,
on a missed/exhausted invocation, and on `needs_agent_review`) feeding the monitoring
SNS topic this skill already uses elsewhere. `shared/scripts/soak_check.py` stays the
reference implementation for running the same checks by hand from any machine that can
reach the databases directly — this stack is the unattended production path.

🔴 **Verify VPC/NAT reachability before deploying, not after the first missed run.** The
Lambda has no reachability of its own beyond what it inherits from the imported bastion
subnets/SG — run `scripts/01-precondition-check.sh`'s connectivity checks (or a one-off
`aws lambda invoke` against a throwaway test function in the same subnets) against BOTH
the source and target endpoints before wiring the real schedule; a VPC/NAT/SG mistake here
fails silently as a Lambda timeout, indistinguishable at a glance from a slow query.

### Dedicated read-only DB credentials — never the admin/master secret

Create a SELECT-only user on **both** source and target for this Lambda specifically (see
[../reference/execution-runbooks.md](../reference/execution-runbooks.md) §Dedicated
read-only credential for the exact `GRANT` statements) — `cluster.secret!` is the
cluster's generated **admin** secret and must never be handed to this function. Run the
`CREATE USER`/`GRANT` once (via the bastion, same as any other one-off SQL setup step in
this skill — not a CDK resource, CDK cannot run SQL), then store each credential in its
own secret:

```typescript
const sourceReadOnlySecret = new sm.Secret(this, 'SourceSoakReadOnlySecret', {
  secretName: `${constants.PREFIX}/soak/source-readonly`,
  generateSecretString: { secretStringTemplate: JSON.stringify({ username: 'soak_ro' }),
                           generateStringKey: 'password', excludePunctuation: true },
});
const targetReadOnlySecret = new sm.Secret(this, 'TargetSoakReadOnlySecret', {
  secretName: `${constants.PREFIX}/soak/target-readonly`,
  generateSecretString: { secretStringTemplate: JSON.stringify({ username: 'soak_ro' }),
                           generateStringKey: 'password', excludePunctuation: true },
});
// After deploy: read each generated password back out and run the CREATE USER/GRANT
// above with it, from the bastion — the secret exists first so the password is never
// typed by a human, but CDK itself never touches the database.
```

```typescript
// ── S3 bucket: single source of truth for dashboard/ during the soak window ──
const dashboardBucket = new s3.Bucket(this, 'DashboardBucket', {
  bucketName: `${constants.PREFIX}-soak-dashboard`,
  encryption: s3.BucketEncryption.S3_MANAGED,           // SSE-S3; use KMS if the plan needs a CMK
  enforceSSL: true,                                     // reject any non-TLS S3 API call outright
  blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,    // no bucket policy public-read, ever —
  publicReadAccess: false,                              // presigned URLs are the ONLY access path
  versioned: true,
  serverAccessLogsBucket: accessLogsBucket,             // this account's existing centralized
  serverAccessLogsPrefix: `${constants.PREFIX}-soak-dashboard/`,  // access-log bucket — every
                                                         // GET against a presigned URL is logged
  removalPolicy: RemovalPolicy.RETAIN, autoDeleteObjects: false,
  cors: [{
    allowedMethods: [s3.HttpMethods.GET],
    allowedOrigins: ['*'],       // the page's own origin IS the bucket's origin (same-origin
    allowedHeaders: ['*'],       // fetches to sibling keys) in the common case; kept permissive
    maxAge: 3000,                // (GET-only, no credentials) so a differently-hosted dashboard
  }],                            // shell or a future CDN in front doesn't need a stack change.
});
```

Scaffold the asset directory once, before first synth — the bundling command below
depends on `requirements.txt` actually being present next to the handler, which is the
gap that used to make this bundling command reference a file that didn't exist anywhere
in the generated project:

```bash
mkdir -p lambda/soak-check
cp <skill>/shared/scripts/soak_check_lambda.py lambda/soak-check/
cp <skill>/shared/scripts/requirements.txt     lambda/soak-check/
```

```typescript
// ── Lambda: VPC-attached into the SAME private subnets + SG the migration bastion already
// uses — identical reachability to the source over the existing VPN/DX path, no new
// networking, no new NAT (reuses the subnets' existing NAT gateway for AWS API calls). ──
const soakFn = new lambda.Function(this, 'SoakCheckFunction', {
  runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
  handler: 'soak_check_lambda.handler',
  code: lambda.Code.fromAsset('lambda/soak-check', {   // shared/scripts/soak_check_lambda.py +
    bundling: {                                         // shared/scripts/requirements.txt copied
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,  // in here (pins pymysql + pg8000)
      command: ['bash', '-c',
        'pip install -r requirements.txt -t /asset-output && cp -au . /asset-output'],
    },
  }),
  timeout: Duration.seconds(90), memorySize: 256,
  vpc: bastionVpc,                                       // ec2.Vpc.fromLookup — the migration
  vpcSubnets: { subnets: bastionPrivateSubnets },         // bastion's own target VPC, not a new one
  securityGroups: [ec2.SecurityGroup.fromSecurityGroupId(   // import the bastion's SG read-only
    this, 'ImportedBastionSg', constants.BASTION_SG_ID, { mutable: false })],  // (mutable:false —
  environment: {                                          // never add ingress from here; the
    // Engine, port, and db name are INDEPENDENT per side — never point both SOURCE_* and
    // TARGET_* at one shared constant (the confirmed bug: reusing constants.DB_PORT/
    // DB_NAME/SOURCE_ENGINE for both sides silently breaks a cross-version or
    // differently-named-database engagement). Both engines must still normalize to the
    // same MySQL-family-or-Postgres-family — heterogeneous soak-checking across families
    // is not yet supported (soak_check_lambda.py raises clearly if they don't match).
    SOURCE_ENGINE: constants.SOURCE_ENGINE, TARGET_ENGINE: constants.TARGET_ENGINE,
    SOURCE_HOST: constants.SOURCE_HOST, SOURCE_PORT: `${constants.SOURCE_DB_PORT}`,
    SOURCE_DB: constants.SOURCE_DB_NAME, SOURCE_SECRET_ARN: sourceReadOnlySecret.secretArn,
    TARGET_HOST: cluster.clusterEndpoint.hostname, TARGET_PORT: `${constants.TARGET_DB_PORT}`,
    TARGET_DB: constants.TARGET_DB_NAME, TARGET_SECRET_ARN: targetReadOnlySecret.secretArn,
    TABLES: JSON.stringify(constants.SOAK_TABLES),
    CHECKSUM_TABLES: JSON.stringify(constants.SOAK_CHECKSUM_TABLES),  // explicit, not left to
                                                                        // the tables[:2] default
    ALARM_NAMES: JSON.stringify(constants.SOAK_ALARM_NAMES),
    TARGET_DB_INSTANCE_ID: constants.TARGET_DB_INSTANCE_ID,
    // Set the ones that apply to THIS engagement's replication mechanism, leave the rest
    // unset — see execution-runbooks.md §Soak automation for the full soak-config.json/
    // env-var schema and the "not_applicable" semantics of leaving all of them unset.
    DMS_TASK_ID: constants.SOAK_DMS_TASK_ID, DMS_REPLICATION_INSTANCE_ID: constants.SOAK_DMS_REPLICATION_INSTANCE_ID,
    DMS_TASK_ARN: constants.SOAK_DMS_TASK_ARN,
    CUSTOMER_TEST_SUITE_PROVIDED: `${constants.CUSTOMER_TEST_SUITE_PROVIDED}`,  // Q18 answer
    // Independent per side — an on-prem/legacy source and an RDS/Aurora target almost
    // always have DIFFERENT trust anchors. Leave either *_SSL_CA_PATH unset to fall
    // back to the platform default trust store for that side (still full TLS, just not
    // CA-pinned). *_TLS_SKIP_VERIFY is a THIRD, explicit-opt-in-only tier (encrypts but
    // skips verification) for a self-signed source cert whose actual CA file can't be
    // retrieved at all — see execution-runbooks.md §Soak automation for when this is
    // actually the right call vs. just being lazy about CA pinning.
    TARGET_SSL_CA_PATH: '/var/task/rds-ca-bundle.pem',   // bundled into the asset; see bundling note below
    // SOURCE_SSL_CA_PATH: only if the source needs a pinned self-signed/private-CA cert
    // AND that cert (not just any certificate the peer presents) is actually available.
    // SOURCE_TLS_SKIP_VERIFY: 'true' — only if it genuinely isn't.
    N_TOTAL: `${constants.SOAK_N_TOTAL}`,          // the risk-tiered default from engagement-safety.md
    DASHBOARD_BUCKET: dashboardBucket.bucketName, DASHBOARD_PREFIX: '',
  },
});
dashboardBucket.grantRead(soakFn);
dashboardBucket.grantPut(soakFn);   // explicit GetObject+PutObject — NOT grantReadWrite(), which
                                     // also hands out DeleteObject/multipart-abort this function
                                     // never needs (least-privilege, not "works either way").
sourceReadOnlySecret.grantRead(soakFn);
targetReadOnlySecret.grantRead(soakFn);
soakFn.addToRolePolicy(new iam.PolicyStatement({
  actions: ['cloudwatch:DescribeAlarms', 'cloudwatch:GetMetricStatistics', 'rds:DescribeDBInstances',
            'dms:DescribeReplicationTasks'],
  resources: ['*'],   // these four are describe/read-only and don't support resource-level scoping
}));
```

Pitfalls: `lambda.Function`'s default execution role does **not** include ENI
create/describe/delete permissions — attaching `vpc`/`vpcSubnets` without also having
`AWSLambdaVPCAccessExecutionRole` on the role (CDK adds this automatically the moment you
pass `vpc`, but confirm it if you construct the role yourself) means the function creates
but every invocation fails at ENI attach. Import the bastion's security group **by ID**
(`ec2.SecurityGroup.fromSecurityGroupId(this, 'ImportedBastionSg', bastionSgId, { mutable:
false })`) — never create a new SG and add it as an extra ingress rule on the
source/target DB security groups; that's new networking, which the whole point of this
design is to avoid. `pymysql`/`pg8000` are both pure-Python (no compiled extension), so the
Docker bundling step above works unmodified across host architectures — no manylinux wheel
concerns. If the account needs strict CA pinning rather than the platform default trust
store (RDS/Aurora endpoints already verify fine against the default trust store without
this — it's for pinning a specific bundle anyway), bundle the
[Amazon RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
into `lambda/soak-check/rds-ca-bundle.pem` before the `cp -au` step and leave
`TARGET_SSL_CA_PATH` set as above; omit both the file and the env var to fall back to the
platform default trust store for that side (still full TLS, just not CA-pinned). If the
SOURCE is an on-prem/legacy host with a self-signed certificate, bundle THAT certificate
instead and point `SOURCE_SSL_CA_PATH` at it — `soak_check_lambda.py`'s `_tls_context`
treats a configured CA path as a pinned trust anchor (still fully encrypted and verified
against it) and skips hostname verification in that case specifically, since on-prem
certs frequently carry no SAN matching the IP/hostname actually used to reach them.

```typescript
// ── EventBridge Scheduler: daily invocation, pure AWS-managed, nothing to keep alive.
// A DLQ on the target means an invocation that exhausts BOTH retries is still visible
// (an alarm on the DLQ's depth below), not just silently dropped. ──
const schedulerRole = new iam.Role(this, 'SoakSchedulerRole', {
  assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
});
soakFn.grantInvoke(schedulerRole);

const soakDlq = new sqs.Queue(this, 'SoakScheduleDlq', {
  retentionPeriod: Duration.days(14), enforceSSL: true,
});
soakDlq.grantSendMessages(schedulerRole);

new scheduler.CfnSchedule(this, 'SoakDailySchedule', {
  flexibleTimeWindow: { mode: 'OFF' },
  scheduleExpression: 'rate(1 day)',   // or a cron() at a fixed off-peak hour; match the
  target: {                            // soak cadence chosen at GATE 1 (engagement-safety.md)
    arn: soakFn.functionArn,
    roleArn: schedulerRole.roleArn,
    retryPolicy: { maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600 },
    deadLetterConfig: { arn: soakDlq.queueArn },
  },
});
```

### Alerting — real, not just a dashboard banner nobody may be looking at

Three alarms, all feeding the **same SNS topic this skill's monitoring baseline already
uses** ([../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §4) — not
a second, disconnected channel:

```typescript
// 1. The Lambda itself erroring (bad config, connection refused, an unhandled exception —
//    NOT a normal RED/needs-review day, which is logged at WARNING and returned
//    normally specifically so it does NOT trip this metric).
new cloudwatch.Alarm(this, 'SoakFnErrorsAlarm', {
  metric: soakFn.metricErrors({ period: Duration.days(1) }),
  threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
}).addAlarmAction(new cw_actions.SnsAction(alertTopic));

// 2. The scheduled invocation never happening at all, or exhausting both retries — the
//    DLQ above catches the second case; this alarm covers both by watching the DLQ depth.
new cloudwatch.Alarm(this, 'SoakScheduleDlqAlarm', {
  metric: soakDlq.metricApproximateNumberOfMessagesVisible(),
  threshold: 1, evaluationPeriods: 1,
}).addAlarmAction(new cw_actions.SnsAction(alertTopic));

// 3. A day the Lambda ran but flagged needs_agent_review=true (any RED, or a check that
//    came back null) — a metric filter on the literal log line, not a re-parse of S3.
const needsReviewFilter = new logs.MetricFilter(this, 'SoakNeedsReviewFilter', {
  logGroup: soakFn.logGroup, metricNamespace: `${constants.PREFIX}/Soak`,
  metricName: 'NeedsAgentReview', metricValue: '1',
  filterPattern: logs.FilterPattern.literal('"needs_agent_review=true"'),
});
new cloudwatch.Alarm(this, 'SoakNeedsReviewAlarm', {
  metric: needsReviewFilter.metric({ statistic: 'Sum', period: Duration.days(1) }),
  threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
}).addAlarmAction(new cw_actions.SnsAction(alertTopic));
```

`alertTopic` is the same `sns.ITopic` monitoring-stack.ts already creates for the
migration-window alarm set — pass it into `SoakStack`'s constructor rather than creating a
second topic; an engagement that deploys soak-stack without a monitoring-stack (unusual,
but not impossible for a very light Mode-1-adjacent check) creates its own topic instead,
with a subscription confirmed at the same time the customer's monitoring contacts are set up.

**Presigned URLs — a one-time step, not part of this stack.** Run
`shared/scripts/generate_presigned_urls.py` once, right after this stack deploys and the
initial `dashboard/` contents are uploaded to `dashboardBucket` (index.html, assets/, empty
status.json + activity-log.jsonl) — it presigns every file the page needs for the full soak
duration and rewrites `index.html` so its CSS/JS/data references are absolute presigned
URLs rather than relative paths (see that script's own docstring for exactly why the naive
relative-path version silently 403s). It is deliberately not a CDK resource or a
Lambda-invoked-at-deploy custom resource — read that script's credential-longevity caveat
before running it: for a 3- or 7-day soak, sign with a throwaway IAM user's long-term
access key, not the deploying operator's own temporary/SSO session, or the URLs stop
working when that session expires, long before the `Expires` value they carry claims. Sign
for slightly UNDER the tier's nominal length, not exactly the SigV4 ceiling — e.g.
`--expires-seconds 561600` (6.5 days) for the 7-day tier, not `604800` — so the link is
never the thing that expires first if soak-exit slips by a few hours past the nominal
window; re-running the script (safe — see its docstring) extends it if the soak genuinely
runs long.

**Ending soak — pull the reports back, don't leave them only in S3.** Before tearing down
or letting the bucket's presigned URLs lapse, sync the bucket's final contents (now
holding every day's `soak-report-day*.md`, the final `status.json`/`activity-log.jsonl`)
back into the engagement working directory so `migration-plan.md` and the rest of the
engagement stay consistent with what actually happened:

```bash
aws s3 sync s3://<dashboard-bucket-name>/ dashboard/ --exclude "index.html"
# index.html excluded deliberately — the bucket's copy is the presigned-URL-materialized
# one (see generate_presigned_urls.py); keep the clean shared/templates/dashboard.html
# copy locally instead of pulling back a copy full of soon-to-expire presigned URLs.
```

**IAM summary for this stack:** `soakFn`'s role = `secretsmanager:GetSecretValue` on
exactly the two dedicated read-only secrets (never the admin/master secret, never `*`),
`cloudwatch:DescribeAlarms`/`GetMetricStatistics` + `rds:DescribeDBInstances` +
`dms:DescribeReplicationTasks` (read-only, no resource-level scoping available),
`s3:GetObject`/`PutObject` (explicitly, not `grantReadWrite`) scoped to `dashboardBucket`
only, plus the VPC ENI permissions from `AWSLambdaVPCAccessExecutionRole`. No
`Create*`/`Modify*`/`Delete*` anywhere — this function only ever reads the databases and
writes to one bucket.

**Fallback — bastion cron, for someone who really doesn't want to stand up Lambda.** Simpler
to set up, but weaker: the bastion has to stay running and reachable for the entire soak
window, a missed run is invisible unless something else is watching, and the dashboard
files end up on the bastion's local disk instead of a durable, shareable location. See
execution-runbooks.md §Soak automation for the one-line cron form if this is the deliberate
choice for a short, low-stakes engagement.

## scripts/ contract

Each script is idempotent, `set -euo pipefail`, reads identifiers from `cdk` outputs
(`aws cloudformation describe-stacks --query ...Outputs`), and refuses to run if the
previous stage's completion marker is absent in `migration-plan.md`. `05-cutover.sh` and
`06-rollback.sh` are generated from the runbook templates
([../templates/cutover-runbook.md](../templates/cutover-runbook.md),
[../templates/rollback-runbook.md](../templates/rollback-runbook.md)) with real values —
no placeholders left at generation time.

## Post-stabilization changes (the CDK project owns day-2)

- Swap `parameterGroup` migration → production, deploy (reboot-scoped params noted in README).
- Scale writer down to steady-state instance type.
- Remove migration-stack entirely (after the rollback window closes).
- Hand the project to the customer: README documents every constant and the change log.
