# CDK stack patterns

Use this pattern after Discovery has written `config/media-archive.yaml`. The chosen Bedrock models are intentionally configuration values, not a fixed catalog: at runtime, list the models available in the selected Region, select one video embedding model and one video-understanding model, and record the verified embedding dimension before generating the index mapping or Lambda guard.

All identifiers below are sanitized and parameterized. Do not put an AWS account ID, OAuth secret, deployed Gateway URL, Cognito ID, or local machine path in generated source or client configuration.

## File layout

```text
<project-root>/
├── config/
│   └── media-archive.yaml
├── infra/
│   ├── bin/app.ts
│   └── lib/
│       ├── config.ts
│       └── media-archive-stack.ts
├── lambdas/
│   ├── catalog/schema.json
│   ├── control/schema.json
│   ├── dispatch/index.py
│   ├── workflow/index.py
│   └── common.py
├── clients/
│   ├── codex-plugin/
│   ├── kiro/
│   ├── claude/
│   └── quick/
├── scripts/
│   ├── check-prerequisites.sh
│   ├── deploy.sh
│   ├── destroy.sh
│   └── verify.py
├── cdk.json
├── package.json
└── tsconfig.json
```

## Configuration and loader

### `config/media-archive.yaml`

The example uses placeholders for model IDs because model availability and capabilities change. Replace them only after Discovery has filtered `aws bedrock list-foundation-models --region <region>` for `VIDEO` input, then verified each selected model's output modality and embedding dimension.

```yaml
# Every Discovery answer belongs here. This file has no credentials or client secrets.
projectName: media-archive
region: <region>
tenantId: pilot

models:
  # Re-verify availability and modality at generation/deploy time; do not copy a stale catalog.
  embeddingModelId: <embedding-model-id>
  understandingModelId: <understanding-model-id>
  # Read this from the chosen embedding model card or a probe. The index and Lambda guard use it.
  embeddingDimension: 512

retention:
  rawIntelligentTieringDays: 30
  noncurrentToGlacierDays: 90
  archiveTaggedToGlacierDays: 1
  analysisExpireDays: 90

proxy:
  profile: analysis-720p-h264-aac
  width: 1280
  height: 720
  videoBitrateMbps: 4
  audioBitrateKbps: 128
  chunkMinutes: 55
  maxChunks: 5
  mapConcurrency: 2

workflow:
  # The state-machine limit bounds the complete workflow. The Lambda enforces the vendor poll bound.
  stateMachineTimeoutHours: 12
  modelPollTimeoutHours: 6

aossNetwork: public # public | vpc
# Required only when aossNetwork is vpc. Create/select these AOSS VPC endpoint IDs in the network account.
aossVpcEndpointIds: []

auth:
  resourceServerIdentifier: media-archive
  scopes:
    - media-archive/read
    - media-archive/write

# Each client receives only its declared scopes. CDK creates client secrets in Cognito but never outputs them.
clients:
  - name: codex-plugin
    scopes: [media-archive/read, media-archive/write]
  - name: kiro
    scopes: [media-archive/read, media-archive/write]
  - name: claude
    scopes: [media-archive/read]
  - name: quick
    scopes: [media-archive/read]
```

### `infra/lib/config.ts`

```ts
import { readFileSync } from 'node:fs';
import * as YAML from 'yaml';

export type AossNetwork = 'public' | 'vpc';

export interface MediaArchiveClientConfig {
  readonly name: string;
  readonly scopes: readonly string[];
}

export interface MediaArchiveConfig {
  readonly projectName: string;
  readonly region: string;
  readonly tenantId: string;
  readonly embeddingModelId: string;
  readonly understandingModelId: string;
  readonly embeddingDimension: number;
  readonly retention: {
    readonly rawIntelligentTieringDays: number;
    readonly noncurrentToGlacierDays: number;
    readonly archiveTaggedToGlacierDays: number;
    readonly analysisExpireDays: number;
  };
  readonly proxy: {
    readonly profile: string;
    readonly width: number;
    readonly height: number;
    readonly videoBitrateMbps: number;
    readonly audioBitrateKbps: number;
    readonly chunkMinutes: number;
    readonly maxChunks: number;
    readonly mapConcurrency: number;
  };
  readonly workflow: {
    readonly stateMachineTimeoutHours: number;
    readonly modelPollTimeoutHours: number;
  };
  readonly aossNetwork: AossNetwork;
  readonly aossVpcEndpointIds: readonly string[];
  readonly auth: {
    readonly resourceServerIdentifier: string;
    readonly scopes: readonly string[];
  };
  readonly clients: readonly MediaArchiveClientConfig[];
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, name: string): UnknownRecord {
  if (!isRecord(value)) throw new Error(`${name} must be a mapping`);
  return value;
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value.trim();
}

function requirePositiveInteger(value: unknown, name: string): number {
  if (!Number.isInteger(value) || (value as number) <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value as number;
}

function requireStringArray(value: unknown, name: string): string[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error(`${name} must be a non-empty list`);
  }
  return value.map((item, index) => requireString(item, `${name}[${index}]`));
}

function optionalStringArray(value: unknown, name: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new Error(`${name} must be a list when supplied`);
  return value.map((item, index) => requireString(item, `${name}[${index}]`));
}

function requireSafeName(value: unknown, name: string): string {
  const result = requireString(value, name);
  if (!/^[a-z][a-z0-9-]{1,62}$/.test(result)) {
    throw new Error(`${name} must use lowercase letters, digits, and hyphens`);
  }
  return result;
}

export function toPascalCase(value: string): string {
  return value
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => `${part[0].toUpperCase()}${part.slice(1)}`)
    .join('');
}

export function loadMediaArchiveConfig(configPath: string): MediaArchiveConfig {
  const raw = requireRecord(YAML.parse(readFileSync(configPath, 'utf8')), 'config');
  const models = requireRecord(raw.models, 'models');
  const retention = requireRecord(raw.retention, 'retention');
  const proxy = requireRecord(raw.proxy, 'proxy');
  const workflow = requireRecord(raw.workflow, 'workflow');
  const auth = requireRecord(raw.auth, 'auth');
  const aossNetwork = requireString(raw.aossNetwork, 'aossNetwork');
  if (aossNetwork !== 'public' && aossNetwork !== 'vpc') {
    throw new Error('aossNetwork must be public or vpc');
  }

  const resourceServerIdentifier = requireString(
    auth.resourceServerIdentifier,
    'auth.resourceServerIdentifier',
  );
  if (resourceServerIdentifier !== 'media-archive') {
    throw new Error('auth.resourceServerIdentifier must be media-archive');
  }

  const scopes = requireStringArray(auth.scopes, 'auth.scopes');
  const expectedScopes = new Set(['media-archive/read', 'media-archive/write']);
  if (scopes.length !== expectedScopes.size || scopes.some((scope) => !expectedScopes.has(scope))) {
    throw new Error('auth.scopes must contain media-archive/read and media-archive/write exactly once');
  }

  if (!Array.isArray(raw.clients) || raw.clients.length === 0) {
    throw new Error('clients must be a non-empty list of mappings');
  }
  const clients = raw.clients.map((client, index): MediaArchiveClientConfig => {
    const item = requireRecord(client, `clients[${index}]`);
    const name = requireSafeName(item.name, `clients[${index}].name`);
    const clientScopes = requireStringArray(item.scopes, `clients[${index}].scopes`);
    if (clientScopes.some((scope) => !expectedScopes.has(scope))) {
      throw new Error(`clients[${index}].scopes contains a scope outside auth.scopes`);
    }
    return { name, scopes: clientScopes };
  });
  if (new Set(clients.map((client) => client.name)).size !== clients.length) {
    throw new Error('clients names must be unique');
  }

  const config: MediaArchiveConfig = {
    projectName: requireSafeName(raw.projectName, 'projectName'),
    region: requireString(raw.region, 'region'),
    tenantId: requireSafeName(raw.tenantId, 'tenantId'),
    embeddingModelId: requireString(models.embeddingModelId, 'models.embeddingModelId'),
    understandingModelId: requireString(models.understandingModelId, 'models.understandingModelId'),
    embeddingDimension: requirePositiveInteger(models.embeddingDimension, 'models.embeddingDimension'),
    retention: {
      rawIntelligentTieringDays: requirePositiveInteger(retention.rawIntelligentTieringDays, 'retention.rawIntelligentTieringDays'),
      noncurrentToGlacierDays: requirePositiveInteger(retention.noncurrentToGlacierDays, 'retention.noncurrentToGlacierDays'),
      archiveTaggedToGlacierDays: requirePositiveInteger(retention.archiveTaggedToGlacierDays, 'retention.archiveTaggedToGlacierDays'),
      analysisExpireDays: requirePositiveInteger(retention.analysisExpireDays, 'retention.analysisExpireDays'),
    },
    proxy: {
      profile: requireSafeName(proxy.profile, 'proxy.profile'),
      width: requirePositiveInteger(proxy.width, 'proxy.width'),
      height: requirePositiveInteger(proxy.height, 'proxy.height'),
      videoBitrateMbps: requirePositiveInteger(proxy.videoBitrateMbps, 'proxy.videoBitrateMbps'),
      audioBitrateKbps: requirePositiveInteger(proxy.audioBitrateKbps, 'proxy.audioBitrateKbps'),
      chunkMinutes: requirePositiveInteger(proxy.chunkMinutes, 'proxy.chunkMinutes'),
      maxChunks: requirePositiveInteger(proxy.maxChunks, 'proxy.maxChunks'),
      mapConcurrency: requirePositiveInteger(proxy.mapConcurrency, 'proxy.mapConcurrency'),
    },
    workflow: {
      stateMachineTimeoutHours: requirePositiveInteger(workflow.stateMachineTimeoutHours, 'workflow.stateMachineTimeoutHours'),
      modelPollTimeoutHours: requirePositiveInteger(workflow.modelPollTimeoutHours, 'workflow.modelPollTimeoutHours'),
    },
    aossNetwork,
    aossVpcEndpointIds: optionalStringArray(raw.aossVpcEndpointIds, 'aossVpcEndpointIds'),
    auth: { resourceServerIdentifier, scopes },
    clients,
  };

  if (config.aossNetwork === 'vpc' && config.aossVpcEndpointIds.length === 0) {
    throw new Error('aossVpcEndpointIds is required when aossNetwork is vpc');
  }
  if (config.workflow.modelPollTimeoutHours > config.workflow.stateMachineTimeoutHours) {
    throw new Error('workflow.modelPollTimeoutHours cannot exceed stateMachineTimeoutHours');
  }
  return config;
}
```

## CDK entrypoint and project metadata

### `infra/bin/app.ts`

```ts
import { App, Tags } from 'aws-cdk-lib';
import * as path from 'node:path';
import { loadMediaArchiveConfig } from '../lib/config.js';
import { MediaArchiveStack } from '../lib/media-archive-stack.js';

const app = new App();
const configPath =
  app.node.tryGetContext('config') ??
  process.env.MEDIA_ARCHIVE_CONFIG ??
  path.resolve(process.cwd(), 'config/media-archive.yaml');
const config = loadMediaArchiveConfig(configPath);

const env = {
  account: app.node.tryGetContext('targetAccount') ?? process.env.CDK_DEFAULT_ACCOUNT,
  region: app.node.tryGetContext('targetRegion') ?? config.region,
};

const stack = new MediaArchiveStack(app, 'MediaArchive', { env, config });
Tags.of(stack).add('Application', 'media-archive');
Tags.of(stack).add('ManagedBy', 'aws-cdk');
Tags.of(stack).add('DataClassification', 'customer-media');

app.synth();
```

### `cdk.json`

```json
{
  "app": "npx tsx infra/bin/app.ts",
  "context": {
    "@aws-cdk/aws-lambda:recognizeLayerVersion": true,
    "@aws-cdk/core:checkSecretUsage": true,
    "@aws-cdk/core:newStyleStackSynthesis": true
  }
}
```

### `package.json`

All versions are pinned so agents reproduce the tested CDK/AgentCore API surface.

```json
{
  "name": "media-archive",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "tsc --noEmit",
    "synth": "cdk synth --quiet",
    "verify": "python3 scripts/verify.py"
  },
  "dependencies": {
    "@aws-cdk/aws-bedrock-agentcore-alpha": "2.235.1-alpha.0",
    "aws-cdk-lib": "2.235.1",
    "constructs": "10.4.3",
    "yaml": "2.7.0"
  },
  "devDependencies": {
    "@types/node": "22.19.1",
    "aws-cdk": "2.1102.0",
    "tsx": "4.20.6",
    "typescript": "5.9.3"
  }
}
```

### `tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "forceConsistentCasingInFileNames": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["infra/**/*.ts"]
}
```

## Pattern 1 — single `MediaArchiveStack`

### `infra/lib/media-archive-stack.ts`

```ts
import {
  Aws,
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
} from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as aoss from 'aws-cdk-lib/aws-opensearchserverless';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as mediaconvert from 'aws-cdk-lib/aws-mediaconvert';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as path from 'node:path';
import { MediaArchiveConfig, toPascalCase } from './config.js';

export interface MediaArchiveStackProps extends StackProps {
  readonly config: MediaArchiveConfig;
}

/**
 * Video ingest -> analysis proxy -> model enrichment -> vector evidence -> MCP Gateway.
 * All source media remains immutable; render/edit operations write only derivatives/.
 */
export class MediaArchiveStack extends Stack {
  constructor(scope: Construct, id: string, props: MediaArchiveStackProps) {
    super(scope, id, props);
    const { config } = props;
    const collectionName = 'media-archive';
    const searchIndex = 'media-clips-v1';

    const mediaKey = new kms.Key(this, 'MediaKey', {
      alias: 'alias/media-archive',
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const mediaBucket = new s3.Bucket(this, 'MediaBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: mediaKey,
      enforceSSL: true,
      eventBridgeEnabled: true,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
      lifecycleRules: [
        {
          id: 'raw-intelligent-tiering',
          prefix: 'raw/',
          enabled: true,
          transitions: [{
            storageClass: s3.StorageClass.INTELLIGENT_TIERING,
            transitionAfter: Duration.days(config.retention.rawIntelligentTieringDays),
          }],
          noncurrentVersionTransitions: [{
            storageClass: s3.StorageClass.GLACIER,
            transitionAfter: Duration.days(config.retention.noncurrentToGlacierDays),
          }],
        },
        {
          // WHY: archive is an explicit human decision; lifecycle performs the move after tagging.
          id: 'human-approved-cold-archive',
          prefix: 'raw/',
          tagFilters: { archive: 'true' },
          enabled: true,
          transitions: [{
            storageClass: s3.StorageClass.GLACIER,
            transitionAfter: Duration.days(config.retention.archiveTaggedToGlacierDays),
          }],
        },
        {
          // WHY: model artifacts are reproducible transient data, unlike originals and derivatives.
          id: 'expire-transient-analysis-output',
          prefix: 'analysis/',
          enabled: true,
          expiration: Duration.days(config.retention.analysisExpireDays),
        },
      ],
    });

    const catalogTable = new dynamodb.Table(this, 'CatalogTable', {
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: mediaKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    catalogTable.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const embeddingModelParameter = new ssm.StringParameter(this, 'EmbeddingModelIdParameter', {
      parameterName: `/${config.projectName}/models/embedding-model-id`,
      stringValue: config.embeddingModelId,
    });
    const understandingModelParameter = new ssm.StringParameter(this, 'UnderstandingModelIdParameter', {
      parameterName: `/${config.projectName}/models/understanding-model-id`,
      stringValue: config.understandingModelId,
    });
    new ssm.StringParameter(this, 'EmbeddingDimensionParameter', {
      parameterName: `/${config.projectName}/models/embedding-dimension`,
      stringValue: config.embeddingDimension.toString(),
    });

    const encryptionPolicy = new aoss.CfnSecurityPolicy(this, 'AossEncryptionPolicy', {
      name: 'media-archive-encryption',
      type: 'encryption',
      policy: JSON.stringify({
        Rules: [{ ResourceType: 'collection', Resource: [`collection/${collectionName}`] }],
        AWSOwnedKey: false,
        KmsARN: mediaKey.keyArn,
      }),
    });

    const aossRules = [
      { ResourceType: 'collection', Resource: [`collection/${collectionName}`] },
      { ResourceType: 'dashboard', Resource: [`collection/${collectionName}`] },
    ];
    const networkStatement = config.aossNetwork === 'public'
      ? {
          Description: 'Public endpoint; IAM data policy remains mandatory.',
          Rules: aossRules,
          AllowFromPublic: true,
        }
      : {
          Description: 'Private endpoint access through configured AOSS VPC endpoints.',
          Rules: aossRules,
          AllowFromPublic: false,
          SourceVPCEs: config.aossVpcEndpointIds,
        };
    const networkPolicy = new aoss.CfnSecurityPolicy(this, 'AossNetworkPolicy', {
      name: 'media-archive-network',
      type: 'network',
      // WHY: public and VPC are mutually exclusive posture choices; never silently expose a VPC deployment.
      policy: JSON.stringify([networkStatement]),
    });

    const collection = new aoss.CfnCollection(this, 'MediaVectorCollection', {
      name: collectionName,
      type: 'VECTORSEARCH',
      description: 'Configurable video embedding vectors with source-time evidence',
      standbyReplicas: 'DISABLED',
    });
    collection.addDependency(encryptionPolicy);
    collection.addDependency(networkPolicy);
    collection.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const mediaConvertRole = new iam.Role(this, 'MediaConvertRole', {
      assumedBy: new iam.ServicePrincipal('mediaconvert.amazonaws.com'),
      description: 'Reads sources and writes analysis proxies and non-destructive derivatives only',
    });
    mediaBucket.grantReadWrite(mediaConvertRole);
    mediaKey.grantEncryptDecrypt(mediaConvertRole);
    const mediaConvertQueue = new mediaconvert.CfnQueue(this, 'MediaConvertQueue', {
      name: 'media-archive',
      description: 'Analysis proxy and highlight rendering queue',
      pricingPlan: 'ON_DEMAND',
      status: 'ACTIVE',
    });

    const lambdaCode = lambda.Code.fromAsset(path.resolve(process.cwd(), 'lambdas'));
    const commonEnvironment = {
      CATALOG_TABLE: catalogTable.tableName,
      MEDIA_BUCKET: mediaBucket.bucketName,
      KMS_KEY_ARN: mediaKey.keyArn,
      AOSS_ENDPOINT: collection.attrCollectionEndpoint,
      SEARCH_INDEX: searchIndex,
      TENANT_ID: config.tenantId,
      AWS_ACCOUNT_ID: Aws.ACCOUNT_ID,
      EMBEDDING_MODEL_ID: config.embeddingModelId,
      UNDERSTANDING_MODEL_ID: config.understandingModelId,
      EMBEDDING_MODEL_SSM_PARAMETER: embeddingModelParameter.parameterName,
      UNDERSTANDING_MODEL_SSM_PARAMETER: understandingModelParameter.parameterName,
      // WHY: extraction and index mapping must fail fast rather than mix incompatible vector dimensions.
      EMBEDDING_DIMENSION: config.embeddingDimension.toString(),
      PROXY_PROFILE: config.proxy.profile,
      PROXY_WIDTH: config.proxy.width.toString(),
      PROXY_HEIGHT: config.proxy.height.toString(),
      PROXY_VIDEO_BITRATE_MBPS: config.proxy.videoBitrateMbps.toString(),
      PROXY_AUDIO_BITRATE_KBPS: config.proxy.audioBitrateKbps.toString(),
      CHUNK_MINUTES: config.proxy.chunkMinutes.toString(),
      MAX_CHUNKS: config.proxy.maxChunks.toString(),
      MAP_CONCURRENCY: config.proxy.mapConcurrency.toString(),
      // WHY: a vendor poll deadline catches a stalled async job before the larger state-machine limit.
      MODEL_POLL_TIMEOUT_HOURS: config.workflow.modelPollTimeoutHours.toString(),
    };

    const workflowFunction = new lambda.Function(this, 'WorkflowFunction', {
      functionName: 'media-archive-workflow',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      code: lambdaCode,
      handler: 'workflow.index.handler',
      memorySize: 1024,
      timeout: Duration.minutes(15),
      environment: {
        ...commonEnvironment,
        MEDIACONVERT_ROLE_ARN: mediaConvertRole.roleArn,
        MEDIACONVERT_QUEUE_ARN: mediaConvertQueue.attrArn,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    catalogTable.grantReadWriteData(workflowFunction);
    mediaBucket.grantReadWrite(workflowFunction);
    mediaKey.grantEncryptDecrypt(workflowFunction);
    workflowFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:StartAsyncInvoke', 'bedrock:GetAsyncInvoke'],
      // WHY: Discovery selects deployable model IDs; constrain this to their model or inference-profile ARNs when known.
      resources: ['*'],
    }));
    workflowFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['aoss:APIAccessAll'],
      resources: [collection.attrArn],
    }));
    workflowFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['mediaconvert:CreateJob', 'mediaconvert:GetJob'],
      resources: ['*'],
    }));
    workflowFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [mediaConvertRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'mediaconvert.amazonaws.com' } },
    }));

    const catalogFunction = new lambda.Function(this, 'CatalogFunction', {
      functionName: 'media-archive-catalog-tools',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      code: lambdaCode,
      handler: 'catalog.index.handler',
      memorySize: 512,
      timeout: Duration.minutes(2),
      environment: commonEnvironment,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    catalogTable.grantReadData(catalogFunction);
    catalogFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [`${catalogTable.tableArn}/index/*`],
    }));
    mediaBucket.grantRead(catalogFunction);
    mediaKey.grantDecrypt(catalogFunction);
    catalogFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['*'],
    }));
    catalogFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['aoss:APIAccessAll'],
      resources: [collection.attrArn],
    }));
    catalogFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['mediaconvert:GetJob'],
      resources: ['*'],
    }));

    const dataPolicy = new aoss.CfnAccessPolicy(this, 'AossDataPolicy', {
      name: 'media-archive-data',
      type: 'data',
      policy: JSON.stringify([
        {
          Description: 'Only archive workflow and catalog Lambda roles access vectors.',
          Principal: [workflowFunction.role!.roleArn, catalogFunction.role!.roleArn],
          Rules: [
            { ResourceType: 'collection', Resource: [`collection/${collectionName}`], Permission: ['aoss:*'] },
            { ResourceType: 'index', Resource: [`index/${collectionName}/*`], Permission: ['aoss:*'] },
          ],
        },
      ]),
    });
    collection.addDependency(dataPolicy);

    const workflowPayload = (action: string): sfn.TaskInput => sfn.TaskInput.fromObject({
      action,
      'tenant_id.$': '$.tenant_id',
      'asset_id.$': '$.asset_id',
      'job_id.$': '$.job_id',
    });
    const workflowTask = (logicalId: string, action: string): tasks.LambdaInvoke => {
      const task = new tasks.LambdaInvoke(this, logicalId, {
        lambdaFunction: workflowFunction,
        payload: workflowPayload(action),
        payloadResponseOnly: true,
      });
      task.addRetry({
        errors: ['Lambda.ServiceException', 'Lambda.TooManyRequestsException', 'States.Timeout'],
        interval: Duration.seconds(10),
        backoffRate: 2,
        maxAttempts: 4,
      });
      return task;
    };

    const markFailed = new tasks.LambdaInvoke(this, 'MarkFailed', {
      lambdaFunction: workflowFunction,
      payload: sfn.TaskInput.fromObject({ action: 'mark_failed', 'payload.$': '$' }),
      payloadResponseOnly: true,
    });
    const failed = new sfn.Fail(this, 'WorkflowFailed', {
      cause: 'Media understanding or indexing failed',
    });
    markFailed.next(failed);

    const prepare = workflowTask('PrepareAsset', 'prepare');
    const startProxy = workflowTask('StartAnalysisProxy', 'start_proxy');
    const pollProxy = workflowTask('PollAnalysisProxy', 'poll_proxy');
    const startEmbedding = workflowTask('StartEmbedding', 'start_embedding');
    const pollEmbedding = workflowTask('PollEmbedding', 'poll_embedding');
    const prepareEnrichment = workflowTask('PrepareEnrichment', 'prepare_enrichment');
    const mergeEnrichment = workflowTask('MergeEnrichment', 'merge_enrichment');
    const indexEmbeddings = workflowTask('IndexEmbeddings', 'index_embeddings');
    const finish = workflowTask('FinishAsset', 'finish');

    const enrichSegment = new tasks.LambdaInvoke(this, 'EnrichUnderstandingSegment', {
      lambdaFunction: workflowFunction,
      payload: sfn.TaskInput.fromObject({
        action: 'enrich_segment',
        'tenant_id.$': '$.tenant_id',
        'asset_id.$': '$.asset_id',
        'job_id.$': '$.job_id',
        'segment.$': '$.segment',
      }),
      payloadResponseOnly: true,
    });
    enrichSegment.addRetry({
      errors: [
        'Lambda.ServiceException',
        'Lambda.AWSLambdaException',
        'Lambda.SdkClientException',
        'Lambda.TooManyRequestsException',
        'ThrottlingException',
        'ServiceUnavailableException',
      ],
      interval: Duration.seconds(20),
      backoffRate: 2,
      maxAttempts: 4,
    });

    const enrichmentMap = new sfn.Map(this, 'UnderstandingSegmentMap', {
      itemsPath: sfn.JsonPath.stringAt('$.analysis_segments'),
      itemSelector: {
        'tenant_id.$': '$.tenant_id',
        'asset_id.$': '$.asset_id',
        'job_id.$': '$.job_id',
        'segment.$': '$$.Map.Item.Value',
      },
      maxConcurrency: config.proxy.mapConcurrency,
      resultPath: sfn.JsonPath.DISCARD,
    });
    enrichmentMap.itemProcessor(enrichSegment);

    for (const task of [
      prepare,
      startProxy,
      pollProxy,
      startEmbedding,
      pollEmbedding,
      prepareEnrichment,
      mergeEnrichment,
      indexEmbeddings,
      finish,
    ]) {
      task.addCatch(markFailed, { resultPath: '$.error' });
    }
    enrichmentMap.addCatch(markFailed, { resultPath: '$.error' });

    const waitForProxy = new sfn.Wait(this, 'WaitForAnalysisProxy', {
      time: sfn.WaitTime.duration(Duration.seconds(60)),
    });
    const proxyStatus = new sfn.Choice(this, 'AnalysisProxyStatus');
    waitForProxy.next(pollProxy).next(proxyStatus);
    proxyStatus
      .when(sfn.Condition.stringEquals('$.status', 'IN_PROGRESS'), waitForProxy)
      .when(sfn.Condition.stringEquals('$.status', 'COMPLETED'), startEmbedding)
      .otherwise(markFailed);

    const waitForEmbedding = new sfn.Wait(this, 'WaitForEmbedding', {
      time: sfn.WaitTime.duration(Duration.seconds(30)),
    });
    const embeddingStatus = new sfn.Choice(this, 'EmbeddingStatus');
    startEmbedding.next(waitForEmbedding);
    waitForEmbedding.next(pollEmbedding).next(embeddingStatus);
    embeddingStatus
      .when(sfn.Condition.stringEquals('$.status', 'IN_PROGRESS'), waitForEmbedding)
      .when(sfn.Condition.stringEquals('$.status', 'COMPLETED'), prepareEnrichment)
      .otherwise(markFailed);
    prepareEnrichment
      .next(enrichmentMap)
      .next(mergeEnrichment)
      .next(indexEmbeddings)
      .next(finish);
    prepare.next(startProxy).next(waitForProxy);

    const workflowLogGroup = new logs.LogGroup(this, 'WorkflowLogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const stateMachine = new sfn.StateMachine(this, 'MediaIngestStateMachine', {
      stateMachineName: 'media-archive-ingest',
      definitionBody: sfn.DefinitionBody.fromChainable(prepare),
      timeout: Duration.hours(config.workflow.stateMachineTimeoutHours),
      tracingEnabled: true,
      logs: {
        destination: workflowLogGroup,
        level: sfn.LogLevel.ERROR,
        // WHY: execution payloads can contain object keys and advisory model output; do not log them wholesale.
        includeExecutionData: false,
      },
    });

    const controlFunction = new lambda.Function(this, 'ControlFunction', {
      functionName: 'media-archive-command-tools',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      code: lambdaCode,
      handler: 'control.index.handler',
      memorySize: 512,
      timeout: Duration.minutes(2),
      environment: {
        ...commonEnvironment,
        STATE_MACHINE_ARN: stateMachine.stateMachineArn,
        MEDIACONVERT_ROLE_ARN: mediaConvertRole.roleArn,
        MEDIACONVERT_QUEUE_ARN: mediaConvertQueue.attrArn,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    catalogTable.grantReadWriteData(controlFunction);
    mediaBucket.grantReadWrite(controlFunction);
    mediaKey.grantEncryptDecrypt(controlFunction);
    stateMachine.grantStartExecution(controlFunction);
    controlFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['mediaconvert:CreateJob'],
      resources: ['*'],
    }));
    controlFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [mediaConvertRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'mediaconvert.amazonaws.com' } },
    }));

    const dispatchFunction = new lambda.Function(this, 'DispatchFunction', {
      functionName: 'media-archive-upload-dispatch',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      code: lambdaCode,
      handler: 'dispatch.index.handler',
      memorySize: 256,
      timeout: Duration.seconds(30),
      environment: { ...commonEnvironment, STATE_MACHINE_ARN: stateMachine.stateMachineArn },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    catalogTable.grantReadWriteData(dispatchFunction);
    stateMachine.grantStartExecution(dispatchFunction);

    const dispatchDlq = new sqs.Queue(this, 'DispatchDlq', {
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: mediaKey,
      retentionPeriod: Duration.days(14),
    });
    const uploadRule = new events.Rule(this, 'UploadRule', {
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [mediaBucket.bucketName] },
          object: { key: [{ prefix: `raw/${config.tenantId}/` }] },
        },
      },
    });
    uploadRule.addTarget(new eventTargets.LambdaFunction(dispatchFunction, {
      // WHY: S3/EventBridge delivery is at-least-once; preserve a failed event for triage instead of dropping ingest.
      deadLetterQueue: dispatchDlq,
      retryAttempts: 2,
      maxEventAge: Duration.hours(2),
    }));

    mediaKey.addToResourcePolicy(new iam.PolicyStatement({
      principals: [new iam.ServicePrincipal('events.amazonaws.com')],
      actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'aws:SourceAccount': this.account },
        // WHY: use an account-scoped rule/* ARN, not uploadRule.ruleArn, to avoid a KMS/rule dependency cycle.
        ArnLike: { 'aws:SourceArn': `arn:${Aws.PARTITION}:events:${Aws.REGION}:${Aws.ACCOUNT_ID}:rule/*` },
      },
    }));
    mediaKey.addToResourcePolicy(new iam.PolicyStatement({
      principals: [new iam.ServicePrincipal('sqs.amazonaws.com')],
      actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
      resources: ['*'],
      conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
    }));

    // WHY: a DLQ without alarms silently turns failed uploads into missing archive assets.
    new cloudwatch.Alarm(this, 'DispatchDlqDeliveryFailureAlarm', {
      metric: new cloudwatch.Metric({
        namespace: 'AWS/Events',
        metricName: 'InvocationsFailedToBeSentToDLQ',
        dimensionsMap: { RuleName: uploadRule.ruleName },
        statistic: 'Sum',
        period: Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    new cloudwatch.Alarm(this, 'DispatchDlqBacklogAlarm', {
      metric: dispatchDlq.metricApproximateNumberOfMessagesVisible({
        period: Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    const resourceServerScopes = config.auth.scopes.map((fullScope) => {
      const scopeName = fullScope.slice(`${config.auth.resourceServerIdentifier}/`.length);
      return {
        fullScope,
        scope: new cognito.ResourceServerScope({
          scopeName,
          scopeDescription: `${scopeName} access to the media archive`,
        }),
      };
    });
    const userPool = new cognito.UserPool(this, 'McpUserPool', {
      userPoolName: 'media-archive-auth',
      // WHY: service identities are provisioned by an operator; public self-signup would create unreviewed archive access.
      selfSignUpEnabled: false,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const resourceServer = userPool.addResourceServer('McpResourceServer', {
      identifier: config.auth.resourceServerIdentifier,
      scopes: resourceServerScopes.map(({ scope: resourceServerScope }) => resourceServerScope),
    });
    const oauthScopeByName = new Map(resourceServerScopes.map(({ fullScope, scope: resourceServerScope }) => [
      fullScope,
      cognito.OAuthScope.resourceServer(resourceServer, resourceServerScope),
    ]));
    const oauthClients = config.clients.map((configuredClient) => {
      const client = userPool.addClient(`Client${toPascalCase(configuredClient.name)}`, {
        userPoolClientName: `media-archive-${configuredClient.name}`,
        generateSecret: true,
        oAuth: {
          flows: { clientCredentials: true },
          scopes: configuredClient.scopes.map((scope) => {
            const resolved = oauthScopeByName.get(scope);
            if (!resolved) throw new Error(`Unknown client scope: ${scope}`);
            return resolved;
          }),
        },
      });
      return { configuredClient, client };
    });
    const domainPrefix = `${config.projectName}-${this.account}-${this.region}`.toLowerCase();
    userPool.addDomain('McpDomain', { cognitoDomain: { domainPrefix } });

    const gateway = new agentcore.Gateway(this, 'MediaArchiveGateway', {
      gatewayName: 'media-archive',
      description: 'Video archive retrieval, evidence, derivatives, and rights-checked download tools',
      authorizerConfiguration: agentcore.GatewayAuthorizer.usingCognito({
        userPool,
        allowedClients: oauthClients.map(({ client }) => client),
      }),
      protocolConfiguration: new agentcore.McpProtocolConfiguration({
        // WHY: semantic routing improves tool discovery, while instructions protect non-negotiable media semantics.
        instructions: [
          'Preserve source video and create only non-destructive derivatives.',
          'Never infer or change media rights from model output.',
          'Cite asset_id, source_id, start_sec, and end_sec for every confident search finding.',
          'Report low-confidence hits as no confident match.',
          'Request explicit human approval before archival.',
        ].join(' '),
        searchType: agentcore.McpGatewaySearchType.SEMANTIC,
        supportedVersions: [
          agentcore.MCPProtocolVersion.MCP_2025_03_26,
          agentcore.MCPProtocolVersion.MCP_2025_06_18,
        ],
      }),
    });

    const catalogTarget = gateway.addLambdaTarget('CatalogTarget', {
      gatewayTargetName: 'media-catalog',
      description: 'Read-only media search, analysis, ontology, status, preview, and download tools',
      lambdaFunction: catalogFunction,
      toolSchema: agentcore.ToolSchema.fromLocalAsset(
        path.resolve(process.cwd(), 'lambdas/catalog/schema.json'),
      ),
    });
    catalogFunction.grantInvoke(gateway.role);
    catalogTarget.node.addDependency(gateway.role);

    const controlTarget = gateway.addLambdaTarget('ControlTarget', {
      gatewayTargetName: 'media-commands',
      description: 'Upload, enrichment, non-destructive rendering, and explicitly approved archive tools',
      lambdaFunction: controlFunction,
      toolSchema: agentcore.ToolSchema.fromLocalAsset(
        path.resolve(process.cwd(), 'lambdas/control/schema.json'),
      ),
    });
    controlFunction.grantInvoke(gateway.role);
    controlTarget.node.addDependency(gateway.role);

    new CfnOutput(this, 'GatewayUrl', { value: gateway.gatewayUrl! });
    new CfnOutput(this, 'OAuthTokenUrl', {
      value: `https://${domainPrefix}.auth.${this.region}.amazoncognito.com/oauth2/token`,
    });
    new CfnOutput(this, 'OAuthUserPoolId', { value: userPool.userPoolId });
    new CfnOutput(this, 'OAuthScopes', { value: config.auth.scopes.join(' ') });
    for (const { configuredClient, client } of oauthClients) {
      new CfnOutput(this, `OAuthClientId${toPascalCase(configuredClient.name)}`, {
        value: client.userPoolClientId,
      });
    }
    // WHY: client secrets stay in Cognito/Secrets Manager and are read at runtime by an authorized bridge or client.
    new CfnOutput(this, 'MediaBucketName', { value: mediaBucket.bucketName });
    new CfnOutput(this, 'CatalogTableName', { value: catalogTable.tableName });
    new CfnOutput(this, 'VectorCollectionEndpoint', { value: collection.attrCollectionEndpoint });
    new CfnOutput(this, 'IngestStateMachineArn', { value: stateMachine.stateMachineArn });
  }
}
```

The Lambda implementation must use `EMBEDDING_DIMENSION` both when it creates the AOSS index mapping and when it validates model output. A model migration is a reindex → dual-write → alias-cutover operation; never place vectors from different embedding models in one index.

## Pattern 2 — multi-stack split variant

Use a split only when deployment ownership, blast radius, or network boundaries justify it. Pass resource interfaces/ARNs between stacks rather than re-looking up resources by name.

```diff
--- infra/bin/app.ts
+++ infra/bin/app.ts
@@
-import { MediaArchiveStack } from '../lib/media-archive-stack.js';
+import { StorageStack } from '../lib/storage-stack.js';
+import { EnrichmentStack } from '../lib/enrichment-stack.js';
+import { McpStack } from '../lib/mcp-stack.js';
@@
-const stack = new MediaArchiveStack(app, 'MediaArchive', { env, config });
+const storage = new StorageStack(app, 'MediaArchiveStorage', { env, config });
+const enrichment = new EnrichmentStack(app, 'MediaArchiveEnrichment', {
+  env, config, storage,
+});
+const mcp = new McpStack(app, 'MediaArchiveMcp', {
+  env, config, storage, enrichment,
+});
+enrichment.addDependency(storage);
+mcp.addDependency(enrichment);
```

```diff
--- infra/lib/media-archive-stack.ts
+++ infra/lib/storage-stack.ts
@@
-export class MediaArchiveStack extends Stack {
+export class StorageStack extends Stack {
+  public readonly mediaBucket: s3.IBucket;
+  public readonly mediaKey: kms.IKey;
+  public readonly catalogTable: dynamodb.ITable;
+  public readonly collection: aoss.CfnCollection;
```

```diff
--- infra/lib/media-archive-stack.ts
+++ infra/lib/enrichment-stack.ts
@@
-const workflowFunction = new lambda.Function(...);
-const stateMachine = new sfn.StateMachine(...);
+// Receive bucket, key, table, collection, and MediaConvert role through typed props.
+// Export workflowFunction, dispatchFunction, and stateMachine for McpStack.
```

```diff
--- infra/lib/media-archive-stack.ts
+++ infra/lib/mcp-stack.ts
@@
-const dataPolicy = new aoss.CfnAccessPolicy(...);
-const gateway = new agentcore.Gateway(...);
+// Create the AOSS access policy here because it references both workflow and catalog roles.
+// Create catalog/control targets, Cognito clients, and Gateway after EnrichmentStack exports its functions.
```

**Dependency rule:** `StorageStack` owns encrypted durable resources and the network/encryption policies. `EnrichmentStack` owns workflow roles and Step Functions. `McpStack` owns catalog/control Lambda roles, the AOSS data policy that names both role sets, Cognito, and Gateway. This ordering avoids cross-stack policy cycles.

## Pattern 3 — multi-tenant variant notes

The default is a single-tenant pilot: the server resolves the tenant and does not trust a request-supplied tenant ID. Do not convert it into multi-tenancy by merely adding a `tenant_id` tool parameter.

For a true tenant boundary:

1. Bind an authenticated subject/client to a tenant in a Gateway authorization/interceptor integration, then forward the resolved tenant as a signed trusted context value. Leave the extension point explicit until the selected Gateway auth integration is known:

   ```ts
   export interface TenantContextInterceptor {
     resolveTenant(authenticatedSubject: string): Promise<string>;
   }
   // TODO: bind this adapter to the selected Gateway interceptor/authorizer extension point.
   // Lambda still revalidates the tenant against its own allowlist before every data operation.
   ```

2. Give every tenant a dedicated S3 prefix (`raw/{tenant}/`, `proxies/{tenant}/`, `analysis/{tenant}/`, `derivatives/{tenant}/`) and use IAM `s3:prefix` conditions. Lambda must enforce that the resolved tenant matches every object key and DynamoDB partition key.
3. Use a distinct KMS key and AOSS collection per tenant when the threat model requires cryptographic/search isolation. Do not put different-model or different-tenant vectors into a shared index merely for cost convenience.
4. Issue client scopes and resource policies per tenant, and ensure preview/download checks rights and tenant membership server-side on every call.
5. Treat cross-tenant reports as a separate audited service; no wildcard collection, bucket-prefix, or KMS policy is acceptable by default.

## Pattern 4 — deployment and teardown scripts

### `scripts/check-prerequisites.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${MEDIA_ARCHIVE_CONFIG:-$PROJECT_ROOT/config/media-archive.yaml}"
AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

fail() { echo "ERROR: $*" >&2; exit 1; }
command -v node >/dev/null || fail "Node.js 20+ is required"
command -v aws >/dev/null || fail "AWS CLI is required"
command -v npx >/dev/null || fail "npx is required"
[[ -f "$CONFIG" ]] || fail "Missing config: $CONFIG"
[[ -d "$PROJECT_ROOT/node_modules/yaml" ]] || fail "Run npm ci before this check"

node_major="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$node_major" -ge 20 ]] || fail "Node.js 20+ is required"

read_config() {
  node --input-type=module - "$CONFIG" "$1" <<'NODE'
import fs from 'node:fs';
import * as YAML from 'yaml';
const [configPath, dottedPath] = process.argv.slice(2);
let value = YAML.parse(fs.readFileSync(configPath, 'utf8'));
for (const part of dottedPath.split('.')) value = value?.[part];
if (value === undefined || value === null) process.exit(2);
console.log(String(value));
NODE
}

REGION="$(read_config region)"
EMBEDDING_MODEL_ID="$(read_config models.embeddingModelId)"
UNDERSTANDING_MODEL_ID="$(read_config models.understandingModelId)"
EMBEDDING_DIMENSION="$(read_config models.embeddingDimension)"
[[ "$REGION" != "<region>" ]] || fail "Set region in media-archive.yaml"
[[ "$EMBEDDING_MODEL_ID" != "<"* ]] || fail "Select an embedding model during Discovery"
[[ "$UNDERSTANDING_MODEL_ID" != "<"* ]] || fail "Select an understanding model during Discovery"
[[ "$EMBEDDING_DIMENSION" =~ ^[1-9][0-9]*$ ]] || fail "embeddingDimension must be a positive integer"

# This read-only call confirms credentials before deployment. Prefer a least-privilege profile.
aws sts get-caller-identity "${AWS_ARGS[@]}" >/dev/null || fail "AWS credentials are not usable"

model_exists() {
  local model_id="$1"
  local found
  found="$(aws bedrock list-foundation-models "${AWS_ARGS[@]}" --region "$REGION" \
    --query "modelSummaries[?modelId=='${model_id}'].modelId | [0]" --output text)"
  [[ "$found" == "$model_id" ]]
}

# Discovery must filter VIDEO input and EMBEDDING/TEXT output modalities; deployment confirms the selected IDs remain available.
model_exists "$EMBEDDING_MODEL_ID" || fail "Configured embedding model is unavailable in $REGION"
model_exists "$UNDERSTANDING_MODEL_ID" || fail "Configured understanding model is unavailable in $REGION"

echo "Prerequisites passed for region $REGION."
```

### `scripts/deploy.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${MEDIA_ARCHIVE_CONFIG:-$PROJECT_ROOT/config/media-archive.yaml}"
STACK_NAME="${MEDIA_ARCHIVE_STACK_NAME:-MediaArchive}"
AWS_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS+=(--profile "$AWS_PROFILE")
fi

cd "$PROJECT_ROOT"
npm ci
MEDIA_ARCHIVE_CONFIG="$CONFIG" "$PROJECT_ROOT/scripts/check-prerequisites.sh"

read_config() {
  node --input-type=module - "$CONFIG" "$1" <<'NODE'
import fs from 'node:fs';
import * as YAML from 'yaml';
const [configPath, dottedPath] = process.argv.slice(2);
let value = YAML.parse(fs.readFileSync(configPath, 'utf8'));
for (const part of dottedPath.split('.')) value = value?.[part];
console.log(String(value));
NODE
}

REGION="$(read_config region)"
ACCOUNT_ID="$(aws sts get-caller-identity "${AWS_ARGS[@]}" --query Account --output text)"

# Bootstrap and deploy write cloud resources. Keep CDK approval enabled for IAM/security changes.
npx cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" --context "config=$CONFIG"
npx cdk diff "$STACK_NAME" --context "config=$CONFIG"
npx cdk deploy "$STACK_NAME" --context "config=$CONFIG"

OUTPUTS_FILE="$(mktemp)"
trap 'rm -f "$OUTPUTS_FILE"' EXIT
aws cloudformation describe-stacks "${AWS_ARGS[@]}" --region "$REGION" \
  --stack-name "$STACK_NAME" --query 'Stacks[0].Outputs' --output json > "$OUTPUTS_FILE"

# Generate non-secret connection descriptors for every configured client directory.
node --input-type=module - "$PROJECT_ROOT" "$CONFIG" "$OUTPUTS_FILE" <<'NODE'
import fs from 'node:fs';
import path from 'node:path';
import * as YAML from 'yaml';

const [root, configPath, outputsPath] = process.argv.slice(2);
const config = YAML.parse(fs.readFileSync(configPath, 'utf8'));
const outputs = JSON.parse(fs.readFileSync(outputsPath, 'utf8'));
const outputByKey = new Map(outputs.map((output) => [output.OutputKey, output.OutputValue]));
const requiredOutput = (key) => {
  const value = outputByKey.get(key);
  if (!value) throw new Error(`Missing CloudFormation output: ${key}`);
  return value;
};
const pascal = (value) => value
  .split(/[^a-zA-Z0-9]+/)
  .filter(Boolean)
  .map((part) => `${part[0].toUpperCase()}${part.slice(1)}`)
  .join('');

for (const client of config.clients) {
  const connection = {
    gatewayUrl: requiredOutput('GatewayUrl'),
    oauth: {
      tokenUrl: requiredOutput('OAuthTokenUrl'),
      clientId: requiredOutput(`OAuthClientId${pascal(client.name)}`),
      scopes: client.scopes,
      // WHY: no OAuth secret is written to Git, generated files, or terminal output.
      secretSource: 'Read at runtime through the authorized AWS profile, Cognito API, or Secrets Manager.',
    },
  };
  const destination = path.join(root, 'clients', client.name);
  fs.mkdirSync(destination, { recursive: true });
  fs.writeFileSync(
    path.join(destination, 'connection.json'),
    `${JSON.stringify(connection, null, 2)}\n`,
    { mode: 0o600 },
  );
}
NODE

echo "Deployment completed and non-secret client connection files were written under clients/."
```

### `scripts/destroy.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${MEDIA_ARCHIVE_CONFIG:-$PROJECT_ROOT/config/media-archive.yaml}"
STACK_NAME="${MEDIA_ARCHIVE_STACK_NAME:-MediaArchive}"

cat >&2 <<'WARNING'
WARNING: This removes the CloudFormation stack and its non-retained resources.
The media bucket, DynamoDB catalog, KMS key, and AOSS collection are intentionally RETAINed.
They can still retain customer media, rights metadata, and vectors after stack deletion.
Do not delete retained data without an approved retention and recovery plan.
WARNING

if [[ "${CONFIRM_DESTROY:-}" != "destroy-media-archive" ]]; then
  echo "Set CONFIRM_DESTROY=destroy-media-archive to proceed." >&2
  exit 2
fi

cd "$PROJECT_ROOT"
npx cdk destroy "$STACK_NAME" --context "config=$CONFIG"
```

## Pattern 5 — structural and secret-hygiene verification

### `scripts/verify.py`

```python
#!/usr/bin/env python3
"""Offline structural and credential-hygiene verification for a generated media archive."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "config/media-archive.yaml",
    "infra/bin/app.ts",
    "infra/lib/config.ts",
    "infra/lib/media-archive-stack.ts",
    "lambdas/common.py",
    "lambdas/dispatch/index.py",
    "lambdas/workflow/index.py",
    "lambdas/catalog/index.py",
    "lambdas/catalog/schema.json",
    "lambdas/control/index.py",
    "lambdas/control/schema.json",
    "local_bridge/server.py",
    "local_bridge/remote_client.py",
    "local_bridge/ui/media-results.html",
    "scripts/check-prerequisites.sh",
    "scripts/deploy.sh",
    "scripts/destroy.sh",
    "evaluation/golden.json",
]
CATALOG_TOOLS = {
    "list_assets",
    "get_asset",
    "search_assets",
    "analyze_asset",
    "get_job_status",
    "get_preview_url",
    "get_download_url",
    "get_ontology",
    "list_collections",
    "get_collection",
}
CONTROL_TOOLS = {
    "create_upload_session",
    "complete_upload",
    "request_enrichment",
    "render_highlight",
    "request_archive",
    "create_collection",
}
FORBIDDEN = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?i)aws_secret_access_key\s*[=:]"),
    re.compile(r"(?i)client_secret\s*[=:]\s*['\"][^<{].+['\"]"),
    re.compile(r"(?<![<\d])\d{12}(?![>\d])"),
]
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "cdk.out", "generated", "__pycache__"}


def scan_schema(path: Path, expected_names: set[str], errors: list[str]) -> None:
    try:
        tools = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        errors.append(f"invalid JSON in {path.relative_to(ROOT)}: {error}")
        return
    if not isinstance(tools, list):
        errors.append(f"tool schema must be a list: {path.relative_to(ROOT)}")
        return
    names: set[str] = set()
    for tool in tools:
        name = tool.get("name", "") if isinstance(tool, dict) else ""
        if not name or name in names:
            errors.append(f"missing or duplicate tool name in {path.relative_to(ROOT)}: {name!r}")
            continue
        names.add(name)
        if tool.get("inputSchema", {}).get("type") != "object":
            errors.append(f"tool {name} must use an object input schema")
    if names != expected_names:
        errors.append(
            f"unexpected tools in {path.relative_to(ROOT)}: expected {sorted(expected_names)}, got {sorted(names)}"
        )


def iter_source_files() -> list[Path]:
    suffixes = {".py", ".ts", ".json", ".md", ".yaml", ".yml", ".sh"}
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and not any(part in EXCLUDED_PARTS for part in path.parts)
    ]


def main() -> int:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")

    catalog_schema = ROOT / "lambdas/catalog/schema.json"
    control_schema = ROOT / "lambdas/control/schema.json"
    if catalog_schema.is_file():
        scan_schema(catalog_schema, CATALOG_TOOLS, errors)
    if control_schema.is_file():
        scan_schema(control_schema, CONTROL_TOOLS, errors)

    for path in iter_source_files():
        if path.suffix == ".py":
            try:
                ast.parse(path.read_text(), filename=str(path))
            except SyntaxError as error:
                errors.append(f"python syntax error in {path.relative_to(ROOT)}: {error}")

        text = path.read_text(errors="ignore")
        for pattern in FORBIDDEN:
            if pattern.search(text):
                errors.append(f"possible credential or account ID in {path.relative_to(ROOT)}")

    config_text = (ROOT / "config/media-archive.yaml").read_text(errors="ignore") if (ROOT / "config/media-archive.yaml").is_file() else ""
    for required_key in ("embeddingModelId", "understandingModelId", "embeddingDimension", "aossNetwork"):
        if required_key not in config_text:
            errors.append(f"config/media-archive.yaml missing {required_key}")

    stack_path = ROOT / "infra/lib/media-archive-stack.ts"
    if stack_path.is_file():
        stack = stack_path.read_text()
        for required_text in (
            "EMBEDDING_DIMENSION",
            "media-archive/read",
            "media-archive/write",
            "includeExecutionData: false",
            "media-catalog",
            "media-commands",
        ):
            if required_text not in stack:
                errors.append(f"stack missing required invariant: {required_text}")

    if errors:
        print("Verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Verification passed: {len(REQUIRED)} required files, 16 MCP tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run the structural check with `python3 scripts/verify.py`, then run `npm run build`, `npm run synth -- --context config=config/media-archive.yaml`, and the generated Lambda tests. The verifier is intentionally offline; credential/model availability checks belong in `check-prerequisites.sh`.

## Cross-layer mapping — `search_assets`

`search_assets` must preserve source-time evidence across every layer rather than returning an ungrounded text answer.

| Layer | Required implementation |
|---|---|
| CDK environment and IAM | `CatalogFunction` receives `EMBEDDING_MODEL_ID`, `EMBEDDING_DIMENSION`, `AOSS_ENDPOINT`, and `SEARCH_INDEX`; its role has `bedrock:InvokeModel` plus `aoss:APIAccessAll` for the configured collection. |
| Catalog Lambda | Validate the returned embedding length against `int(os.environ["EMBEDDING_DIMENSION"])`, query the configured index, filter by server-resolved tenant and rights, and return `asset_id`, `source_id`, `start_sec`, `end_sec`, modality, score, and confidence band. Return `no confident match` for a low-confidence result. |
| Schema | The catalog schema declares `search_assets` with an object input containing `query`, optional filters, and a bounded result count. Its output contract documents every evidence field. |
| Gateway target | `gateway.addLambdaTarget(... gatewayTargetName: "media-catalog" ...)` loads `lambdas/catalog/schema.json`, so AgentCore exposes the prefixed tool name `media-catalog___search_assets`. Semantic routing uses the gateway instructions to retain evidence. |
| Local bridge and UI | `local_bridge/remote_client.py` strips `media-catalog___` only when the unprefixed tool name is unique. `render_media_results` consumes the evidence fields; `resolve_media_playback` obtains rights-checked short-lived playback URLs server-side. |

A minimal Lambda guard belongs next to vector extraction:

```python
expected_dimension = int(os.environ["EMBEDDING_DIMENSION"])
vector = extract_embedding(model_response)
if len(vector) != expected_dimension:
    raise ValueError(
        f"Embedding dimension mismatch: expected {expected_dimension}, got {len(vector)}"
    )
```

The corresponding schema entry must remain explicit:

```json
{
  "name": "search_assets",
  "description": "Search approved media and return source-time evidence; report no confident match for low confidence.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" },
      "max_results": { "type": "integer", "minimum": 1, "maximum": 20 }
    },
    "required": ["query"]
  }
}
```

The client-facing result must never replace these evidence fields with model prose. Model-generated descriptions and ontology suggestions remain advisory metadata; rights and tenant checks remain server-side.
