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
if (constants.NATIVE_REPLICATION) {
  dbSg.addEgressRule(ec2.Peer.ipv4(constants.SOURCE_CIDR), ec2.Port.tcp(constants.SOURCE_DB_PORT),
    'native replication to source');
}

new rds.SubnetGroup(this, 'DbSubnets', { vpc,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  description: 'DB subnets (private)' });
```

Pitfalls: `Vpc.fromLookup` needs `env: { account, region }` set on the stack (no
env-agnostic synth); DMS needs its own SG allowed **into both** the source SG and `dbSg`.

🔴 **Pitfall — EC2 security-group descriptions are a restricted charset, checked only at
DEPLOY time (hit by both live runners).** A security group's `GroupDescription` and every
ingress/egress **rule** `Description` accept only `a-z A-Z 0-9`, space and
`. _ - : / ( ) # , @ [ ] + = & ; { } ! $ *`, up to 255 characters (EC2 API
`CreateSecurityGroup` / `IpRange`). `cdk synth` passes; `cdk deploy` then fails with
`Invalid rule description ... a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*` and rolls the stack back.
Live offenders: an ASCII arrow `'DMS -> target DB'` (the `>`), `<`, `→`, em dashes `—`,
quotes, and Korean text. **This applies in a Korean-language session too**: chat, plan and
dashboard prose follow the user's language; SG/rule descriptions stay plain ASCII from that
set (`'DMS to target DB'`, `'app client sg-0abc'`). The L2 default `GroupDescription` is
the construct path, so keep construct ids/stack names ASCII as well. Before every
`cdk deploy`, after `cdk synth`, run the check in §scripts/ contract (`02-deploy.sh`).

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
// TWO cluster parameter groups — swap before validation/soak, never after go-live.
const migrationParams = new rds.ParameterGroup(this, 'MigrationParams', { engine,
  description: 'import-optimized', parameters: {
    // MySQL-family examples; engine-specific values live in constants.ts
    max_allowed_packet: '1073741824',
    innodb_flush_log_at_trx_commit: '2',          // relax durability DURING IMPORT ONLY
    foreign_key_checks: '0', unique_checks: '0',   // if the method needs them
    binlog_format: 'ROW',                          // needed for REVERSE replication later
    require_secure_transport: constants.ENFORCE_TLS ? 'ON' : 'OFF',
    time_zone: constants.SOURCE_TIME_ZONE,
  }});
const productionParams = new rds.ParameterGroup(this, 'ProductionParams', { engine,
  description: 'steady-state', parameters: {
    binlog_format: 'ROW',
    innodb_flush_log_at_trx_commit: '1',
    foreign_key_checks: '1', unique_checks: '1',
    require_secure_transport: constants.ENFORCE_TLS ? 'ON' : 'OFF',
    time_zone: constants.SOURCE_TIME_ZONE,         // match source — Phase 1 adjustment
  }});
// Materialize BOTH groups now and retain them when the association swaps in Phase 7.
// new ParameterGroup alone is lazy — see the pitfall below.
migrationParams.bindToCluster({});
const productionParamsConfig = productionParams.bindToCluster({});
new CfnOutput(this, 'ProductionParameterGroupName', {
  value: productionParamsConfig.parameterGroupName,
});

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

**Pitfall — an unbound `rds.ParameterGroup` can disappear from synth:** the L2 represents
either `AWS::RDS::DBClusterParameterGroup` or `AWS::RDS::DBParameterGroup`; it creates
the concrete resource when bound to that kind of DB. In this pattern only
`migrationParams` is associated at provisioning time. Merely constructing
`productionParams` can leave it absent even though both `cdk synth` and `cdk deploy`
succeed — Phase 7 then fails when applying a group that never existed.

The explicit `productionParams.bindToCluster({})` above materializes the cluster group
and supplies its generated name for an output **without switching the running cluster
to it**. Binding `migrationParams` explicitly also keeps it present after the Phase 7
association swap. For an RDS `DatabaseInstance`, use `bindToInstance({})` instead. Do not bind the
same group as both kinds. On CDK versions providing standalone factories,
`ParameterGroup.forCluster()` / `forInstance()` are alternatives; an explicit
`CfnDBClusterParameterGroup` / `CfnDBParameterGroup` with the verified engine family is
also suitable. `fromParameterGroupName()` alone imports a reference; it creates nothing.

**Verify existence at Phase 5:** inspect the synthesized database-stack template for
**both** groups of the correct resource type, then describe both by their deployed
names (`describe-db-cluster-parameter-groups` for Aurora, `describe-db-parameter-groups`
for an instance) and record name/family in the plan. A successful stack deployment alone
is insufficient. At Phase 7, change the CDK association to `productionParams`, deploy,
complete any required reboot/session recycling, and verify effective production values
before validation/soak. Keep the established logical IDs; do not create a replacement
group merely to change which one is associated.

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

// verify-ca / verify-full need the CA imported into DMS (Ref = the certificate ARN). import * as fs from 'fs';
// Source (self-managed MySQL): the server's CA file — SHOW GLOBAL VARIABLES LIKE 'ssl_ca'
// (auto-generated installs: ca.pem in the datadir). Public cert only; never ca-key.pem.
// Target (RDS/Aurora): the RDS CA bundle for the region (shared/assets/rds-global-bundle.pem
// covers every region; the regional bundle is truststore.pki.rds.amazonaws.com/<region>/<region>-bundle.pem).
const sourceCa = new dms.CfnCertificate(this, 'SourceCa', {
  certificatePem: fs.readFileSync(constants.SOURCE_CA_PEM_PATH, 'utf8') });
const targetCa = new dms.CfnCertificate(this, 'TargetCa', {
  certificatePem: fs.readFileSync(constants.RDS_CA_BUNDLE_PATH, 'utf8') });

const sourceEp = new dms.CfnEndpoint(this, 'SourceEp', { endpointType: 'source',
  engineName: constants.SOURCE_ENGINE, serverName: constants.SOURCE_HOST,
  port: constants.DB_PORT, databaseName: constants.DB_NAME,
  // MySQL-family endpoints: 'none' | 'verify-ca' | 'verify-full' ONLY — 'require' is
  // rejected at deploy ("The require SSL mode is not supported by the 'mysql' engine").
  sslMode: 'verify-ca', certificateArn: sourceCa.ref,
  mySqlSettings: {
    secretsManagerAccessRoleArn: constants.DMS_SECRET_ACCESS_ROLE_ARN,
    secretsManagerSecretId: constants.SOURCE_DMS_SECRET_ARN,
  } });
// target endpoint analogous, pointing at cluster endpoint, certificateArn: targetCa.ref,
// sslMode 'verify-full' (RDS endpoint names match the RDS CA-issued server certificate)

// CloudWatch: AWS/DMS task metrics use ReplicationTaskIdentifier = the task's RESOURCE ID
// (Fn.select(6, Fn.split(':', task.ref)) — CfnReplicationTask's Ref/attrReplicationTaskArn is
// the ARN; there is no resource-id attribute) plus ReplicationInstanceIdentifier = the friendly
// instance id. Build the CDCLatencySource/Target alarms (preflight-iam-cost.md §4) from those.
// FORWARD task (full-load-and-cdc) AND REVERSE task (cdc, created stopped) — the reverse
// task is part of the plan, not an afterthought. Task settings JSON from
// ../reference/dms-best-practices.md; table mappings from constants.
```

This endpoint example is MySQL-specific: use the corresponding engine settings property
for other engines. **AWS DMS supports only `none`, `verify-ca` and `verify-full` for
MySQL/MariaDB/Aurora MySQL endpoints — `require` is "Not supported"** (DMS User Guide,
"Using SSL with AWS DMS") and fails the stack at deploy, after synth passed. Use
`verify-ca` for a self-managed source whose certificate is the engine's auto-generated one
(its CN does not match the hostname, so `verify-full` fails) and `verify-full` for an
RDS/Aurora target; both need the imported CA (`CfnCertificate` above, or
`aws dms import-certificate`). `none` is not only weaker: a target with
`require_secure_transport=ON` rejects it (MySQL error 3159). Any weaker mode needs explicit
approval, never default to `none`. Use Secrets Manager references,
never plaintext passwords in constants or synthesized templates. Test both endpoints post-deploy in
`scripts/01-precondition-check.sh` via `aws dms test-connection`.

## proxy-stack.ts (conditional) / monitoring-stack.ts

Proxy: `rds.DatabaseProxy` with `requireTLS: true`, the app SGs allowed in — output the
proxy endpoint; Phase 8 points clients at it.

🔴 **Register EVERY account that will log in through the Proxy — not just the admin
secret.** RDS Proxy authenticates clients only against the secrets in its auth list ("a
separate Secrets Manager secret for each database user account that the proxy connects
to"); an application account without its own registered secret gets `ERROR 1045 Access
denied` through the Proxy while a direct login works (live: cutover-blocking, caught only by
the Phase 7 §2.6 check). Each entry's client auth type must match that account's
authentication plugin: `MYSQL_NATIVE_PASSWORD` for `mysql_native_password`,
`MYSQL_CACHING_SHA2_PASSWORD` for `caching_sha2_password` (CDK `rds.ClientPasswordAuthType`;
CloudFormation `AuthFormat.ClientPasswordAuthType`). The CDK L2 prop
`clientPasswordAuthType` applies **one** value to every secret, so either keep all
Proxy-facing accounts on one plugin, or override the per-entry value on the L1:

```typescript
const proxy = new rds.DatabaseProxy(this, 'Proxy', {
  proxyTarget: rds.ProxyTarget.fromInstance(db),           // fromCluster(cluster) for Aurora
  secrets: [adminSecret, ...appAccountSecrets],            // one secret per login account (Phase 7 §2.6 list)
  vpc, securityGroups: [proxySg], requireTLS: true,
  clientPasswordAuthType: rds.ClientPasswordAuthType.MYSQL_NATIVE_PASSWORD,  // = the accounts' plugin
});
// Mixed plugins: Auth entries follow the `secrets` order — e.g. entry 1 is caching_sha2:
// (proxy.node.defaultChild as rds.CfnDBProxy).addPropertyOverride('Auth.1.ClientPasswordAuthType', 'MYSQL_CACHING_SHA2_PASSWORD');
```

An account added later (Phase 7.5) needs its secret added here and a redeploy before
cutover. Phase 7 then authenticates as **each** application account through the Proxy
endpoint (`validation-patterns.md` §2.6), not only directly. Monitoring: the alarm set
from [../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §4 + a
dashboard with source-vs-target panels during the migration window, all → one SNS topic.

## soak-stack.ts (conditional — Phase 7.7 automated soak checks, the recommended path)

Generated when the customer approves automating the Phase 7.7 checklist (see
[../reference/execution-runbooks.md](../reference/execution-runbooks.md) §Soak automation —
get approval before creating this, it's infrastructure like everything else here). Pieces:
an S3 bucket that becomes the soak window's single source of truth for the dashboard, a
VPC-attached Lambda running `shared/scripts/soak_check_lambda.py`, an EventBridge Scheduler
rule that invokes it daily, and CloudWatch alarms (Lambda errors, missed/exhausted
invocation, `needs_agent_review`) feeding the monitoring SNS topic this skill already uses.
`shared/scripts/soak_check.py` stays the reference implementation for running the same
checks by hand — this stack is the unattended production path.

🔴 **Run the soak preflight checks before the first `cdk deploy`** —
[../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §0 "Soak
automation" (Docker or the no-Docker bundling path, PyPI/public.ecr.aws reachability, NAT
or VPC endpoints in the Lambda's subnets). Real engagements lost several redeploys to each
of these; none of them is visible until deploy or first invoke.

### Dedicated read-only DB credentials — never the admin/master secret

Create a SELECT-only user on **both** source and target for this Lambda (exact `GRANT`s:
[../reference/execution-runbooks.md](../reference/execution-runbooks.md) §Dedicated
read-only credential; add `REPLICATION CLIENT` on the side named by
`mysqlReplicaStatusSide`). The cluster's generated **admin** secret must never be handed to
this function. Two options per side:

- **New secret (default):** the stack creates it with a generated password and a
  **generated name** (no `secretName`). After deploy, read the password back and run the
  `CREATE USER`/`GRANT` from the bastion — CDK never touches the database.
- **Reuse an existing read-only secret** (e.g. the credential already used for Phase 2
  assessment): pass `existingSecretArn` (the *complete* ARN incl. the 6-character suffix).
  The stack then adds an explicit `secretsmanager:GetSecretValue` grant on exactly that ARN —
  an imported secret gets **no** permission unless you grant it. If that secret is encrypted
  with a customer-managed KMS key, also pass `secretKmsKeyArn`: imported secrets carry no
  key information, so CDK cannot add `kms:Decrypt` on its own, and the failure surfaces
  only at runtime as `AccessDeniedException: Access to KMS is not allowed`.

### Lambda asset — two bundling paths (pick in preflight)

```bash
mkdir -p lambda/soak-check
cp <skill>/shared/scripts/soak_check_lambda.py lambda/soak-check/
cp <skill>/shared/scripts/requirements.txt     lambda/soak-check/
# Tier-1 TLS default trust anchor — mandatory, not optional pinning: the platform
# trust store does NOT contain the current Amazon RDS root CA (confirmed live).
cp <skill>/shared/assets/rds-global-bundle.pem lambda/soak-check/
# bundling: 'prebuilt' (default — no Docker): install the pure-Python deps in place.
# Needs only PyPI reachability from this machine; pymysql/pg8000 have no compiled parts,
# so a host-arch-independent install is correct for the arm64 Lambda.
python3 -m pip install -r lambda/soak-check/requirements.txt -t lambda/soak-check/
```

`bundling: 'docker'` instead runs that pip install inside
`public.ecr.aws/sam/build-python3.12` at synth — needs a running Docker daemon **and**
network to `public.ecr.aws` + PyPI, and fails `cdk synth` (not deploy) without them.

### The stack — `lib/stacks/soak-stack.ts` (self-contained; synthesizes as-is)

Every external dependency is an explicit prop; no fixed physical names anywhere (bucket,
secrets, log group, topic are all CloudFormation-generated), so a rolled-back first deploy
can never block the retry with "already exists" / "scheduled for deletion"; every optional
environment variable is `?? ''` (the handler treats empty as "not configured", so a
binlog-replication engagement with no DMS props deploys cleanly); the function gets an
explicit `logs.LogGroup` instead of the `LogRetention` custom resource (which collided with
a leftover `/aws/lambda/<name>` group on retry).

```typescript
// lib/stacks/soak-stack.ts — Phase 7.7 automated soak checks. Self-contained: every
// external dependency arrives as an explicit prop (no free variables), no fixed physical
// names (a rolled-back first deploy never blocks the retry), optional env always `?? ''`.
import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sm from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

export interface SoakDbSide {
  engine: string;                 // 'mysql' | 'mariadb' | 'aurora-mysql' | 'postgres' | ...
  host: string;                   // target: e.g. databaseStack.cluster.clusterEndpoint.hostname
  port: number;
  dbName: string;
  /** Reuse an EXISTING read-only secret (complete ARN, with the 6-char suffix) instead of
   *  creating a new one. It gets an explicit GetSecretValue grant (below). */
  existingSecretArn?: string;
  /** Customer-managed KMS key encrypting that secret. REQUIRED when the secret is not on
   *  the AWS-managed aws/secretsmanager key — imported secrets carry no key info, so CDK
   *  cannot add kms:Decrypt by itself. */
  secretKmsKeyArn?: string;
  sslCaPath?: string;             // see TLS tiers in soak_check_lambda.py _tls_context
  tlsSkipVerify?: boolean;
}

export interface SoakStackProps extends StackProps {
  prefix: string;                 // constants.PREFIX — used for metric namespace/descriptions only, never physical names
  vpcId: string;                  // the migration bastion's VPC (imported, never created)
  availabilityZones: string[];    // AZs of the subnets below (fromVpcAttributes needs them; no lookup)
  privateSubnetIds: string[];     // the bastion's private subnets — Lambda ENIs go here
  securityGroupId: string;        // the bastion's SG, imported mutable:false
  source: SoakDbSide;
  target: SoakDbSide;
  targetDbInstanceId?: string;    // RDS instance id for headroom; omit/'' for Aurora or n/a
  tables: string[];
  checksumTables?: string[];
  alarmNames?: string[];
  /** taskId = the task's RESOURCE ID (CloudWatch ReplicationTaskIdentifier dimension), NOT the
   *  friendly task name: Fn.select(6, Fn.split(':', task.ref)) — Ref is the task ARN.
   *  replicationInstanceId = the friendly instance identifier. */
  dms?: { taskId?: string; replicationInstanceId?: string; taskArn?: string };
  mysqlReplicaStatusSide?: 'source' | 'target';
  pgReplicationLagSide?: 'source' | 'target';
  customerTestSuiteProvided: boolean;   // Q18
  nTotal: number;                       // soak days (engagement-safety.md tier)
  alertTopicArn?: string;               // monitoring-stack's topic; omitted -> a new topic
  accessLogsBucketName?: string;        // existing centralized access-log bucket (optional)
  dashboardKmsKeyArn?: string;          // CMK for the dashboard bucket (omit -> SSE-S3)
  existingDashboardBucketName?: string; // reuse a bucket instead of creating one
  /** 'prebuilt' (default): deps pip-installed into lambda/soak-check/ beforehand, no Docker.
   *  'docker': CDK bundles in public.ecr.aws/sam/build-python3.12 (needs Docker + network). */
  bundling?: 'prebuilt' | 'docker';
  assetPath?: string;                   // default 'lambda/soak-check'
  /** Default false: the schedule deploys DISABLED — enabled, its next 23:30 UTC run would
   *  fire before DB users, dashboard files, or a passing preflight exist. Flip to true only
   *  after preflight is ok, then redeploy (template stays the truth). */
  scheduleEnabled?: boolean;
  /** Lambda timeout. Default 900 (the Lambda maximum; you pay only for actual duration).
   *  Size from a measured COUNT/checksum of the largest table — see "Sizing" below. */
  functionTimeoutSeconds?: number;
  /** Per-statement DB read timeout inside the function. Default 300. */
  dbQueryTimeoutSeconds?: number;
  /** Watermark-bounded comparison under live writes (soak_check_lambda.py WATERMARK_DEFAULTS):
   *  pkMargin default 10000 keys; timestampColumns {table: column} bound those tables by
   *  `column <= source NOW() - ageMinutes` (default 15) instead of the PK. */
  watermark?: { enabled?: boolean; pkMargin?: number; timestampColumns?: Record<string, string>; ageMinutes?: number };
}

// Same normalization as soak_check_lambda.py's _engine_family() — keep in sync.
export function engineFamily(engine: string): 'mysql' | 'postgres' {
  const e = engine.trim().toLowerCase();
  if (['mysql', 'mariadb', 'aurora-mysql'].includes(e)) return 'mysql';
  if (['postgres', 'postgresql', 'aurora-postgresql'].includes(e)) return 'postgres';
  throw new Error(`SoakStack: unsupported engine '${engine}'.`);
}

export class SoakStack extends Stack {
  constructor(scope: Construct, id: string, props: SoakStackProps) {
    super(scope, id, props);
    const { prefix, source, target, dms = {} } = props;

    // Fail at synth, not at the first scheduled invocation.
    if (engineFamily(source.engine) !== engineFamily(target.engine)) {
      throw new Error(`SoakStack: ${source.engine} and ${target.engine} normalize to different SQL ` +
        'families — heterogeneous soak-checking is not supported (execution-runbooks.md §Soak automation).');
    }
    if (!props.env?.region || !props.env?.account) {
      throw new Error('SoakStack: pass env: { account, region } explicitly — the region is baked into ' +
        'every ARN/endpoint this stack and the presigned dashboard links use.');
    }

    // ── Network: import only ──
    const vpc = ec2.Vpc.fromVpcAttributes(this, 'BastionVpc', {
      vpcId: props.vpcId, availabilityZones: props.availabilityZones,
    });
    const subnets = props.privateSubnetIds.map((sid, i) => {
      const sub = ec2.Subnet.fromSubnetId(this, `BastionSubnet${i}`, sid);
      Annotations.of(sub).acknowledgeWarning('@aws-cdk/aws-ec2:noSubnetRouteTableId', 'route table not needed for Lambda ENIs');
      return sub;
    });
    const sg = ec2.SecurityGroup.fromSecurityGroupId(this, 'ImportedBastionSg', props.securityGroupId, { mutable: false });

    // ── Read-only DB secrets: generated names (no secretName) so a rolled-back deploy's
    // "scheduled for deletion" secret can never block the retry. ──
    const secretFor = (side: SoakDbSide, label: string): sm.ISecret =>
      side.existingSecretArn
        ? sm.Secret.fromSecretCompleteArn(this, `${label}ReadOnlySecret`, side.existingSecretArn)
        : new sm.Secret(this, `${label}SoakReadOnlySecret`, {
            description: `${prefix} soak read-only credential (${label.toLowerCase()})`,
            generateSecretString: { secretStringTemplate: JSON.stringify({ username: 'soak_ro' }),
                                    generateStringKey: 'password', excludePunctuation: true },
          });
    const sourceSecret = secretFor(source, 'Source');
    const targetSecret = secretFor(target, 'Target');

    // ── Dashboard bucket: generated name. RETAIN keeps soak evidence; a rolled-back first
    // deploy leaves an orphaned EMPTY bucket with a random name — it never collides with
    // the retry (delete it afterwards, see "Retry after a failed deploy"). ──
    // Imported CMK: CDK cannot edit its key policy — the key policy must allow this account's
    // IAM policies (the default key policy does) or name the Lambda role explicitly.
    const dashboardKey = props.dashboardKmsKeyArn;
    const dashboardBucket: s3.IBucket = props.existingDashboardBucketName
      ? s3.Bucket.fromBucketName(this, 'DashboardBucket', props.existingDashboardBucketName)
      : new s3.Bucket(this, 'DashboardBucket', {
          encryption: dashboardKey ? s3.BucketEncryption.KMS : s3.BucketEncryption.S3_MANAGED,
          encryptionKey: dashboardKey ? kms.Key.fromKeyArn(this, 'DashboardKey', dashboardKey) : undefined,
          bucketKeyEnabled: !!dashboardKey,
          enforceSSL: true,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,   // presigned URLs are the ONLY access path
          publicReadAccess: false,
          versioned: true,
          serverAccessLogsBucket: props.accessLogsBucketName
            ? s3.Bucket.fromBucketName(this, 'AccessLogsBucket', props.accessLogsBucketName) : undefined,
          serverAccessLogsPrefix: props.accessLogsBucketName ? `${prefix}-soak-dashboard/` : undefined,
          removalPolicy: RemovalPolicy.RETAIN, autoDeleteObjects: false,
          cors: [{ allowedMethods: [s3.HttpMethods.GET], allowedOrigins: ['*'],
                   allowedHeaders: ['*'], maxAge: 3000 }],
        });

    // ── Explicit log group (generated name) instead of the LogRetention custom resource,
    // which collides with a leftover /aws/lambda/<fn> group on retry. ──
    const logGroup = new logs.LogGroup(this, 'SoakFnLogs', {
      retention: logs.RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.DESTROY,
    });

    const assetPath = props.assetPath ?? 'lambda/soak-check';
    const code = (props.bundling ?? 'prebuilt') === 'docker'
      ? lambda.Code.fromAsset(assetPath, { bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: ['bash', '-c', 'pip install -r requirements.txt -t /asset-output && cp -au . /asset-output'],
        } })
      : lambda.Code.fromAsset(assetPath);   // deps already pip-installed into assetPath (no Docker)

    const soakFn = new lambda.Function(this, 'SoakCheckFunction', {
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: 'soak_check_lambda.handler', code,
      // 900s default: full scans of ~100M-row tables (source and target run concurrently,
      // tables sequentially) needed ~900s live. Preflight budgets itself against the remaining time.
      timeout: Duration.seconds(props.functionTimeoutSeconds ?? 900), memorySize: 256,
      logGroup,
      vpc, vpcSubnets: { subnets }, securityGroups: [sg],
      environment: {
        SOURCE_ENGINE: source.engine, TARGET_ENGINE: target.engine,
        SOURCE_HOST: source.host, SOURCE_PORT: `${source.port}`, SOURCE_DB: source.dbName,
        SOURCE_SECRET_ARN: sourceSecret.secretArn,
        TARGET_HOST: target.host, TARGET_PORT: `${target.port}`, TARGET_DB: target.dbName,
        TARGET_SECRET_ARN: targetSecret.secretArn,
        TABLES: JSON.stringify(props.tables),
        CHECKSUM_TABLES: props.checksumTables ? JSON.stringify(props.checksumTables) : '',
        ALARM_NAMES: JSON.stringify(props.alarmNames ?? []),
        // Optional: empty string == not configured (the handler treats '' as unset).
        TARGET_DB_INSTANCE_ID: props.targetDbInstanceId ?? '',
        DMS_TASK_ID: dms.taskId ?? '',
        DMS_REPLICATION_INSTANCE_ID: dms.replicationInstanceId ?? '',
        DMS_TASK_ARN: dms.taskArn ?? '',
        MYSQL_REPLICA_STATUS_SIDE: props.mysqlReplicaStatusSide ?? '',
        PG_REPLICATION_LAG_SIDE: props.pgReplicationLagSide ?? '',
        CUSTOMER_TEST_SUITE_PROVIDED: `${props.customerTestSuiteProvided}`,
        SOURCE_SSL_CA_PATH: source.sslCaPath ?? '', TARGET_SSL_CA_PATH: target.sslCaPath ?? '',
        SOURCE_TLS_SKIP_VERIFY: `${source.tlsSkipVerify ?? false}`,
        TARGET_TLS_SKIP_VERIFY: `${target.tlsSkipVerify ?? false}`,
        N_TOTAL: `${props.nTotal}`,
        DB_QUERY_TIMEOUT_SECONDS: `${props.dbQueryTimeoutSeconds ?? 300}`,
        WATERMARK_ENABLED: `${props.watermark?.enabled ?? true}`,
        WATERMARK_PK_MARGIN: props.watermark?.pkMargin !== undefined ? `${props.watermark.pkMargin}` : '',
        WATERMARK_TIMESTAMP_COLUMNS: props.watermark?.timestampColumns ? JSON.stringify(props.watermark.timestampColumns) : '',
        WATERMARK_AGE_MINUTES: props.watermark?.ageMinutes !== undefined ? `${props.watermark.ageMinutes}` : '',
        DASHBOARD_BUCKET: dashboardBucket.bucketName, DASHBOARD_PREFIX: '',
      },
    });

    // ── IAM: explicit statements, exactly the calls soak_check_lambda.py makes (no grant*()
    // helpers — on a KMS bucket those add unconditional kms:Encrypt/ReEncrypt*/GenerateDataKey*/
    // Decrypt, which would make the conditional KMS statements below meaningless). ──
    const allow = (actions: string[], resources: string[], conditions?: Record<string, unknown>) =>
      soakFn.addToRolePolicy(new iam.PolicyStatement({ actions, resources, conditions }));
    allow(['s3:GetObject', 's3:PutObject'], [dashboardBucket.arnForObjects('*')]);
    allow(['s3:ListBucket'], [dashboardBucket.bucketArn]);   // missing key -> NoSuchKey, not AccessDenied
    allow(['secretsmanager:GetSecretValue'], [sourceSecret.secretArn, targetSecret.secretArn]);
    const viaService = (service: string) => ({ StringEquals: { 'kms:ViaService': `${service}.${this.region}.amazonaws.com` } });
    const secretKeys = [source.secretKmsKeyArn, target.secretKmsKeyArn].filter((k): k is string => !!k);
    if (secretKeys.length) allow(['kms:Decrypt'], [...new Set(secretKeys)], viaService('secretsmanager'));
    if (dashboardKey) allow(['kms:Decrypt', 'kms:GenerateDataKey'], [dashboardKey], viaService('s3'));
    allow(['cloudwatch:DescribeAlarms', 'cloudwatch:GetMetricStatistics',
           'rds:DescribeDBInstances', 'dms:DescribeReplicationTasks'],
          ['*']);   // read-only describes; '*' deliberately (a mis-scoped ARN is the classic redeploy loop)

    // ── Daily schedule + DLQ ──
    const schedulerRole = new iam.Role(this, 'SoakSchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });
    soakFn.grantInvoke(schedulerRole);
    const soakDlq = new sqs.Queue(this, 'SoakScheduleDlq', { retentionPeriod: Duration.days(14), enforceSSL: true });
    soakDlq.grantSendMessages(schedulerRole);
    const schedule = new scheduler.CfnSchedule(this, 'SoakDailySchedule', {
      state: props.scheduleEnabled ? 'ENABLED' : 'DISABLED',
      flexibleTimeWindow: { mode: 'OFF' },
      // Soak verdicts are per UTC CALENDAR day: run once near the end of each UTC day.
      // (rate(1 day) fires relative to enable time, so its runs straddle two UTC days.)
      scheduleExpression: 'cron(30 23 * * ? *)',
      scheduleExpressionTimezone: 'UTC',
      target: {
        arn: soakFn.functionArn, roleArn: schedulerRole.roleArn,
        // Pins the verdict to the scheduled UTC day even if a retry finishes after 00:00.
        input: JSON.stringify({ scheduled_time: '<aws.scheduler.scheduled-time>' }),
        retryPolicy: { maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600 },
        deadLetterConfig: { arn: soakDlq.queueArn },
      },
    });

    // ── Alerting ──
    const alertTopic: sns.ITopic = props.alertTopicArn
      ? sns.Topic.fromTopicArn(this, 'AlertTopic', props.alertTopicArn)
      : new sns.Topic(this, 'SoakAlertTopic', { enforceSSL: true });
    const alarm = (alarmId: string, a: cloudwatch.AlarmProps) =>
      new cloudwatch.Alarm(this, alarmId, a).addAlarmAction(new cw_actions.SnsAction(alertTopic));
    alarm('SoakFnErrorsAlarm', {
      metric: soakFn.metricErrors({ period: Duration.days(1) }),
      threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm('SoakScheduleDlqAlarm', {
      metric: soakDlq.metricApproximateNumberOfMessagesVisible(), threshold: 1, evaluationPeriods: 1,
    });
    alarm('SoakMissingInvocationAlarm', {
      metric: soakFn.metricInvocations({ statistic: 'Sum', period: Duration.days(1) }),
      threshold: 1, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      actionsEnabled: !!props.scheduleEnabled,   // a DISABLED schedule must not page anyone
    });
    const needsReviewFilter = new logs.MetricFilter(this, 'SoakNeedsReviewFilter', {
      logGroup, metricNamespace: `${prefix}/Soak`, metricName: 'NeedsAgentReview', metricValue: '1',
      filterPattern: logs.FilterPattern.literal('"needs_agent_review=true"'),
    });
    alarm('SoakNeedsReviewAlarm', {
      metric: needsReviewFilter.metric({ statistic: 'Sum', period: Duration.days(1) }),
      threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new CfnOutput(this, 'SoakFunctionName', { value: soakFn.functionName, description: 'Soak Lambda (invoke {"mode":"preflight"} first)' });
    new CfnOutput(this, 'DashboardBucketName', { value: dashboardBucket.bucketName, description: 'Soak dashboard bucket' });
    new CfnOutput(this, 'DashboardBucketRegion', { value: this.region, description: 'Pass to generate_presigned_urls.py --region' });
    new CfnOutput(this, 'SoakScheduleName', { value: schedule.ref, description: `Daily schedule (state: ${props.scheduleEnabled ? 'ENABLED' : 'DISABLED'})` });
    new CfnOutput(this, 'SoakLogGroupName', { value: logGroup.logGroupName, description: 'Soak Lambda log group' });
  }
}
```

Wire it in `bin/app.ts` with an explicit `env` — the region is baked into every ARN,
endpoint, and presigned dashboard link (the constructor throws without it). Values come
from `constants.ts` and the other stacks' outputs:

```typescript
new SoakStack(app, `${constants.PREFIX}-SoakStack`, {
  env: { account: constants.ACCOUNT, region: constants.REGION },      // e.g. ap-northeast-2
  prefix: constants.PREFIX,
  vpcId: constants.BASTION_VPC_ID, availabilityZones: constants.BASTION_AZS,
  privateSubnetIds: constants.BASTION_PRIVATE_SUBNET_IDS, securityGroupId: constants.BASTION_SG_ID,
  source: { engine: constants.SOURCE_ENGINE, host: constants.SOURCE_HOST,
            port: constants.SOURCE_DB_PORT, dbName: constants.SOURCE_DB_NAME },
  target: { engine: constants.TARGET_ENGINE, host: databaseStack.cluster.clusterEndpoint.hostname,
            port: constants.TARGET_DB_PORT, dbName: constants.TARGET_DB_NAME },
  targetDbInstanceId: constants.TARGET_DB_INSTANCE_ID,                  // '' / omit for Aurora
  tables: constants.SOAK_TABLES, checksumTables: constants.SOAK_CHECKSUM_TABLES,
  alarmNames: constants.SOAK_ALARM_NAMES,
  // omit `dms` entirely for binlog/native replication. taskId is the ARN's resource-id suffix
  // (import { Fn } from 'aws-cdk-lib'; forwardTask = the migration-stack CfnReplicationTask):
  dms: { taskArn: migrationStack.forwardTask.ref,                                  // Ref = task ARN
         taskId: Fn.select(6, Fn.split(':', migrationStack.forwardTask.ref)),      // arn:aws:dms:r:a:task:<ID>
         replicationInstanceId: constants.DMS_INSTANCE_ID },                       // friendly instance id
  mysqlReplicaStatusSide: constants.SOAK_MYSQL_REPLICA_STATUS_SIDE,      // omit if n/a
  customerTestSuiteProvided: constants.CUSTOMER_TEST_SUITE_PROVIDED,   // Q18
  nTotal: constants.SOAK_N_TOTAL,
  alertTopicArn: monitoringStack.alertTopic.topicArn,   // same topic as the migration alarms
  accessLogsBucketName: constants.ACCESS_LOGS_BUCKET,   // optional; must already accept S3 server access logs
  scheduleEnabled: app.node.tryGetContext('soakScheduleEnabled') === 'true',  // false until preflight is ok
});
```

Engine, port, and DB name are **independent per side** — never point both sides at one
shared constant (that silently breaks cross-version or differently-named-database
engagements). TLS: leave `sslCaPath` unset for an RDS/Aurora side (bundled RDS CA,
verify-full); set it to the actual CA file for an on-prem self-signed/private-CA source;
`tlsSkipVerify: true` only when that CA file genuinely can't be retrieved (see
execution-runbooks.md §Soak automation). Import the bastion's SG by ID with
`mutable: false` — never add ingress rules on the DB SGs from here.

### Live writes and sizing — watermark comparison, timeouts

Under live application writes a whole-table source-vs-target `COUNT(*)`/checksum is RED
every day from replication lag alone (confirmed live). Both soak scripts therefore compare,
per table with a single-column integer PK, only rows with `pk <= min(source MAX(pk),
target MAX(pk)) - pkMargin` (default 10000 keys — set it above peak inserts/s × the 30 s
lag threshold × 2), or `column <= source NOW() - ageMinutes` for tables listed in
`watermark.timestampColumns` (same date/time type required on both sides; NULL rows of a
nullable column are compared explicitly); the tail beyond is reported as `detail.row_count.<t>.tail_rows`
(informational, never a failure). Tables without such a key fall back to the whole-table
comparison and say so in `detail.*.<t>.note`. A mismatch below the watermark stays RED
(it can be a lost row, or an UPDATE/DELETE of an old row still in flight — review it).
Preflight's `*_db_watermark <table>` rows show which mode each table will use.

Timeouts: source and target queries run concurrently, tables sequentially. Measure the
largest checksum table's bounded COUNT + checksum on the target during Phase 7 and set
`functionTimeoutSeconds` ≥ 1.5 × the sum over all tables (default 900 = the Lambda
maximum; a ~100M-row table needed ~900 s live) and `dbQueryTimeoutSeconds` ≥ 1.5 × the
slowest single statement (default 300). If the total cannot fit in 900 s, reduce the
per-run scope — checksum only the critical tables (`checksumTables`) and row-count the
rest — or, for
very large engagements, run the standalone `shared/scripts/soak_check.py` from the
migration host (no 900 s ceiling; `batch_timeout_seconds`). **Sharding the tables across
several soak stacks/functions is unsupported:** each run replaces the whole day entry in
`status.json`, so shards overwrite each other's results. Never silently drop tables from
the soak.

### Network — the Lambda reaches AWS APIs only through its subnets

VPC-attached Lambdas get **no public IP**. Every AWS call the handler makes (Secrets
Manager, S3, CloudWatch, RDS, DMS) needs, in the Lambda's subnets, either a `0.0.0.0/0`
route to a **NAT gateway** or VPC endpoints: interface endpoints for `secretsmanager`,
`monitoring` (CloudWatch metrics/alarms), `rds`, `dms` (only if DMS is configured), and an
**S3 gateway endpoint** on the subnets' route tables. (`logs` is not needed by the handler —
the Lambda service delivers function logs outside your VPC; add it only if you extend the
handler to call CloudWatch Logs APIs.) Without either, calls
**time out** — and a timeout looks like a hung Lambda, which agents misread as IAM. The
handler now fails fast (5 s connect timeout) and labels these `network: ... NOT an IAM
problem`. Interface endpoints with private DNS change name resolution for the **whole VPC**
and cost per AZ-hour — they are new networking: propose them in the plan and get approval,
don't add them silently (this stack deliberately creates none).

### Deploy-once workflow (preflight mode) — never redeploy on a guess

The schedule deploys **DISABLED** (`scheduleEnabled` defaults to false): enabled, it would
fire at the next 23:30 UTC whether or not the read-only DB users exist, the dashboard files
are uploaded, or preflight has passed.

**Why `cron(30 23 * * ? *)` in `UTC`, not `rate(1 day)`:** each soak verdict is for one
**UTC calendar day** (`soak.days[].date`, the green streak, the 36-hour-overdue banner). A
`rate(1 day)` schedule fires relative to whenever it was enabled (e.g. 18:37Z), so "day 1"
mixes two UTC days and agents try to anchor it with `StartDate` (which must then be
`yyyy-MM-ddTHH:mm:ss.SSSZ`, in UTC — a live deploy failed on the format). The cron form needs
no `StartDate`: it runs near the end of every UTC day (EventBridge Scheduler
`scheduleExpressionTimezone: 'UTC'`), and the target input passes
`<aws.scheduler.scheduled-time>` so a retry or long run finishing after 00:00 UTC is still
recorded against the day it checked. State the resulting timetable in chat (Phase 7.7):
first verdict = the first 23:30 UTC after enabling; a day whose 23:30 run is the first one
only covers the hours since enabling — say so, or count from the next full UTC day.

1. `cdk synth` → `cdk deploy` **once** (schedule DISABLED).
2. Create the read-only DB users (§Dedicated read-only DB credentials) and upload the initial
   `dashboard/` files (index.html, assets/, seeded `status.json`, empty `activity-log.jsonl`).
3. Invoke preflight. It runs the same probes as a normal run — every configured table on
   both sides (SELECT, catalog, checksum-shaped query), replica status, all alarms, both DMS
   metrics, RDS describe, every S3 key — and **writes nothing** (S3 writes are proven with a
   wrong-ETag conditional PUT: 412 = allowed). Rows stream to the log as
   `SOAK_PREFLIGHT_ROW {json}`; probes near the function timeout come back `SKIPPED`:
   ```bash
   aws lambda invoke --region <REGION> --function-name <SoakFunctionName output> \
     --cli-binary-format raw-in-base64-out --payload '{"mode":"preflight"}' /tmp/preflight.json
   python3 -c "import json;[print(r['result'].ljust(13),r['check'].ljust(36),r['iam_action'],r['resource'],'' if r['result']=='PASS' else '-> '+r['message'],r.get('kms_key','')) for r in json.load(open('/tmp/preflight.json'))['checks']]"
   ```
4. Every non-`PASS` row names the exact action and resource (KMS denials name the KMS
   action and, separately, `kms_key`), or says `network: ...` / a DB error. Fix **all** of
   them in one change, redeploy at most once, re-run preflight.
5. When `ok: true` — or the only non-PASS rows are `UNVERIFIED` for an SSE-KMS bucket's
   `kms:GenerateDataKey` (a non-mutating probe can't exercise it; watch the first run) —
   enable the schedule: `cdk deploy -c soakScheduleEnabled=true` (template-only change; the
   template stays the source of truth — don't flip it with `aws scheduler update-schedule`,
   a later deploy would silently revert it). Record the enable time in `migration-plan.md`
   and the activity log.
6. If a normal run later fails, its log has one `SOAK_CHECK_ERROR {json}` line with the same
   fields; non-fatal AWS failures appear in that day's `detail.aws_errors[]` and the check is
   `null` (needs review), never a pass.

### Soak IAM — API call → IAM action → granted by

Matches `soak_check_lambda.py` and the synthesized policy exactly (asserted in a scratch app:
no `grant*()` helpers, so no unconditional `kms:Encrypt`/`ReEncrypt*`/`GenerateDataKey*`):

| API call in the handler | IAM action | Resource | Statement in the stack |
|---|---|---|---|
| `secretsmanager.get_secret_value` (source, target; normal + preflight) | `secretsmanager:GetSecretValue` | the two secret ARNs (created or `existingSecretArn`) | `allow(['secretsmanager:GetSecretValue'], …)` |
| — same call, secret on a customer-managed key | `kms:Decrypt`, condition `kms:ViaService = secretsmanager.<region>.amazonaws.com` | each `secretKmsKeyArn` | `secretKeys` statement (**required** for imported/CMK secrets) |
| `s3.get_object` (status.json, activity-log.jsonl) | `s3:GetObject` | bucket `/*` | `allow(['s3:GetObject','s3:PutObject'], [arnForObjects('*')])` |
| `s3.put_object` (status.json/activity-log.jsonl with `IfMatch`/`IfNoneMatch`, `reports/*.md`; preflight's wrong-ETag probes) | `s3:PutObject` | bucket `/*` | same statement |
| — `NoSuchKey` vs `AccessDenied` for a not-yet-existing key | `s3:ListBucket` | bucket ARN | `allow(['s3:ListBucket'], [bucketArn])` |
| — S3 calls on a CMK bucket | `kms:Decrypt`, `kms:GenerateDataKey`, condition `kms:ViaService = s3.<region>.amazonaws.com` | `dashboardKmsKeyArn` | `dashboardKey` statement |
| `cloudwatch.describe_alarms` (if `alarmNames`) | `cloudwatch:DescribeAlarms` | `*` | shared describe statement |
| `cloudwatch.get_metric_statistics` (RDS `FreeStorageSpace`; DMS `CDCLatencyTarget` + `CDCLatencySource`) | `cloudwatch:GetMetricStatistics` | `*` (no resource-level support) | shared describe statement |
| `rds.describe_db_instances` (if `targetDbInstanceId`) | `rds:DescribeDBInstances` | `*` | shared describe statement |
| `dms.describe_replication_tasks` (if `dms.taskArn`) | `dms:DescribeReplicationTasks` | `*` | shared describe statement |
| ENI create/describe/delete | `ec2:CreateNetworkInterface` etc. | — | `AWSLambdaVPCAccessExecutionRole` (CDK adds it when `vpc` is set) |
| CloudWatch Logs | `logs:CreateLogStream`, `logs:PutLogEvents` | the log group | `AWSLambdaBasicExecutionRole` |

The four describe actions stay on `*` deliberately: they are read-only, and a mis-scoped
ARN (wrong account/region/identifier format) is exactly the AccessDenied → guess →
redeploy loop this table exists to stop. An imported CMK's **key policy** must also allow
the role (the default key policy delegates to IAM; a custom one may not) — preflight shows
that as `AccessDenied` with the KMS action and `kms_key` even when the IAM statement is
present. No `Create*`/`Modify*`/`Delete*` anywhere.

### Retry after a failed first deploy

With generated names there is nothing to rename. A rollback leaves behind only the
**retained, empty dashboard bucket** (random name — it does not block the retry; delete it
once the retry succeeds: `aws s3 rb s3://<orphan> --region <REGION>`) and, if the stack
created them, the two secrets **scheduled for deletion** under their generated names
(harmless; `aws secretsmanager delete-secret --secret-id <arn> --force-delete-without-recovery`
only if you want them gone now). If you ever *must* pin a physical name (a customer naming
policy), the retry needs that exact bucket emptied + deleted (or imported via
`existingDashboardBucketName`) and the secret restored with
`aws secretsmanager restore-secret` — which is why the default is generated names. Read the
first failure's `ResourceStatusReason` (`aws cloudformation describe-stack-events`) before
changing anything.

### Alerting

The four alarms in the stack above (Lambda `Errors`, DLQ depth, missing daily invocation —
whose actions stay off while the schedule is DISABLED —
`needs_agent_review=true` metric filter on the explicit log group) all feed **the same SNS
topic the migration-window alarms use** ([../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md)
§4) — pass `alertTopicArn`. Without it the stack creates its own topic; confirm its
subscription at the same time the customer's monitoring contacts are set up. A normal
RED/needs-review day is logged at WARNING and returned normally, so it trips only the
needs-review alarm, not `Errors`; a fatal AWS failure (secret/S3) raises, so it trips
`Errors` with the `SOAK_CHECK_ERROR` line naming the fix.

**Presigned URLs — initial issuance plus planned renewal, not part of this stack.** Run
`shared/scripts/generate_presigned_urls.py --bucket <DashboardBucketName> --region
<DashboardBucketRegion>` (both are stack outputs) right after this stack deploys and the
initial `dashboard/` contents are uploaded (index.html, assets/, status.json +
activity-log.jsonl) — it presigns every file the page needs and rewrites `index.html` so its
CSS/JS/data references are absolute presigned URLs (see that script's docstring for why
relative paths silently 403). It signs for the bucket's **actual** region (detected from
S3; `--region` must match it), then GETs every URL and refuses to print the customer link
unless all return HTTP 200 — a URL signed for the wrong region fails with
`AuthorizationQueryParametersError` while the script's own upload still succeeds, which is
how this bug shipped once. Signing credentials cap every link: with temporary credentials (a role/SSO/instance session) a link dies when that session does, whatever `--expires-seconds` says — the script detects this and prints the **effective** expiry; tell the customer that time and **re-issue on demand** (re-run the script) by default. A dedicated signing IAM user is the exception, proposed only when a longer link is genuinely needed, behind its own A3 block (script docstring) — never suggest long-term keys casually. With credentials that do last, sign for slightly OVER the tier's nominal length: `129600` seconds (1.5 days) for the 1-day
tier and `302400` (3.5 days) for the 3-day tier. Plan **648000 seconds (7.5 days) of
coverage** for the 7-day tier, but never request a single S3 signature that long: SigV4
rejects expiries over `604800` seconds. Issue with `--expires-seconds 604800`, renew by
day 6 (earlier if signing credentials expire), and deliver the new customer link with
an explicit instruction to reopen it. Re-signing does not update URLs embedded in an
already-open page. Renew again if RED days extend the soak; keep signing credentials
valid through the final half-day buffer.

**Ending soak — pull the reports back, don't leave them only in S3.** Before tearing down
or letting the bucket's presigned URLs lapse, sync the bucket's final contents (now
holding every day's `soak-report-day*.md`, the final `status.json`/`activity-log.jsonl`)
back into the engagement working directory so `migration-plan.md` and the rest of the
engagement stay consistent with what actually happened:

```bash
aws s3 sync s3://<dashboard-bucket-name>/ dashboard/ --region <REGION> --exclude "index.html"
# index.html excluded deliberately — the bucket's copy is the presigned-URL-materialized
# one (see generate_presigned_urls.py); keep the clean shared/templates/dashboard.html
# copy locally instead of pulling back a copy full of soon-to-expire presigned URLs.
```

**Fallback — bastion cron, for someone who really doesn't want to stand up Lambda.** Simpler
to set up, but weaker: the bastion has to stay running and reachable for the entire soak
window, a missed run is invisible unless something else is watching, and the dashboard
files end up on the bastion's local disk instead of a durable, shareable location. See
execution-runbooks.md §Soak automation for the one-line cron form if this is the deliberate
choice for a short, low-stakes engagement.

## scripts/ contract

Each script is idempotent, `set -euo pipefail`, non-interactive (no `read` prompts — a
headless session hangs on them; confirmations happen in chat), reads identifiers from `cdk` outputs
(`aws cloudformation describe-stacks --query ...Outputs`), and refuses to run if the
previous stage's completion marker is absent in `migration-plan.md`. `05-cutover.sh` and
`06-rollback.sh` are generated from the runbook templates
([../templates/cutover-runbook.md](../templates/cutover-runbook.md),
[../templates/rollback-runbook.md](../templates/rollback-runbook.md)) with real values —
no placeholders left at generation time.

`02-deploy.sh` runs `cdk synth` and then this **security-group description check** before
any `cdk deploy` (see the network-stack pitfall — synth does not catch it):

```bash
python3 -c 'import json,glob,re,sys;ok=re.compile(r"[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}");bad=[(f,k,v) for f in glob.glob("cdk.out/*.template.json") for k,r in json.load(open(f)).get("Resources",{}).items() for v in ([r.get("Properties",{}).get("GroupDescription")]+[x.get("Description") for x in r.get("Properties",{}).get("SecurityGroupIngress",[])+r.get("Properties",{}).get("SecurityGroupEgress",[])] if r.get("Type")=="AWS::EC2::SecurityGroup" else [r.get("Properties",{}).get("Description")] if r.get("Type") in ("AWS::EC2::SecurityGroupIngress","AWS::EC2::SecurityGroupEgress") else []) if isinstance(v,str) and not ok.fullmatch(v)];[print("INVALID SG description:",*b) for b in bad];sys.exit(1 if bad else 0)'
```

It exits non-zero and names the template, logical id and offending text; fix the string in
code (ASCII from the allowed set), re-synth, re-check. Token-valued descriptions (e.g.
`Fn::Join`) are not strings in the template and are skipped — keep those to ASCII literals.

## Post-stabilization changes (the CDK project owns day-2)

- **No silent IaC drift.** Every ad-hoc resource or configuration change made outside this
  app during the engagement (a helper EC2, RDS parameter changes, `binlog retention hours`,
  Proxy auth fixes, extra alarms or SG rules) is either back-ported here — then `cdk diff`
  is clean — or recorded in the plan as drift with an owner and a reconcile step before
  handover / Phase 9. Non-CloudFormation settings (`CALL mysql.rds_set_configuration(...)`)
  go in the README's post-deploy steps.

- Verify production parameters remain active; the swap/reboot occurs before validation/soak.
- Scale writer down to steady-state instance type.
- Remove migration-stack entirely (after the rollback window closes).
- Hand the project to the customer: README documents every constant and the change log.
