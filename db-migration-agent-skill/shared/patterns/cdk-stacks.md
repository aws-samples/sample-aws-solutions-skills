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

// Secret holds the FULL connection contract — host/port/dbname/engine, not just creds.
// Phase 7.5 discovers whether the app's existing secret has `host`; this one always does.
const dbSecret = new secretsmanager.Secret(this, 'DbSecret', {
  secretName: `${constants.PREFIX}/db-credentials`,
  generateSecretString: {
    secretStringTemplate: JSON.stringify({ username: 'admin', dbname: constants.DB_NAME,
      engine: constants.ENGINE_FAMILY, port: constants.DB_PORT }),
    generateStringKey: 'password', excludeCharacters: '"@/\\\'',
  }, encryptionKey: key,
});
```

Also here: the service roles from
[../reference/preflight-iam-cost.md](../reference/preflight-iam-cost.md) §2 that the plan
needs (aurora-s3-import-role for XtraBackup, `dms-vpc-role` — exact name, only if absent
in the account — rds-proxy-secrets-role, rds-monitoring-role).

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
  engine, credentials: rds.Credentials.fromSecret(dbSecret),
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
