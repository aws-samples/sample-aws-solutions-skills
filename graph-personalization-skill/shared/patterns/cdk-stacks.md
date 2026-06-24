# CDK Stack Patterns

> CDK TypeScript (aws-cdk-lib v2). 7 stacks for production: Network, Graph, Ingestion, (optional) ML, Auth, API, Frontend.

## File layout

```
cdk/
├── bin/app.ts
├── cdk.json
├── package.json
└── lib/
    ├── network-stack.ts          ← VPC for Neptune
    ├── graph-stack.ts            ← Neptune cluster + IAM auth
    ├── ingestion-stack.ts        ← Kinesis + Lambda + DLQ + Bulk loader bucket
    ├── ml-stack.ts               ← (optional) Neptune ML + SageMaker
    ├── auth-stack.ts             ← Cognito User Pool
    ├── api-stack.ts              ← API Gateway + Lambdas (recommend, explore, admin)
    └── frontend-stack.ts         ← S3 + CloudFront + WAF + Cognito Authenticator
```

## bin/app.ts

```typescript
#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { NetworkStack } from '../lib/network-stack';
import { GraphStack } from '../lib/graph-stack';
import { IngestionStack } from '../lib/ingestion-stack';
import { MLStack } from '../lib/ml-stack';
import { AuthStack } from '../lib/auth-stack';
import { ApiStack } from '../lib/api-stack';
import { FrontendStack } from '../lib/frontend-stack';

const app = new App();
const projectName = app.node.tryGetContext('projectName') ?? 'graph-personalization';
const environment = app.node.tryGetContext('environment') ?? 'dev';
const industry = app.node.tryGetContext('industry') ?? 'ecommerce';
const enableNeptuneML = app.node.tryGetContext('enableNeptuneML') === 'true';

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'ap-northeast-2',
};

const network = new NetworkStack(app, `${projectName}-Network`, { env, projectName, environment });

const graph = new GraphStack(app, `${projectName}-Graph`, {
  env, projectName, environment,
  vpc: network.vpc,
  mode: 'serverless-v2',           // or 'provisioned'
  minNcu: 0.5, maxNcu: 16,
});

const ingestion = new IngestionStack(app, `${projectName}-Ingestion`, {
  env, projectName, environment,
  vpc: network.vpc,
  graphCluster: graph.cluster,
  graphSg: graph.sg,
  shards: environment === 'prod' ? 3 : 1,
});

if (enableNeptuneML) {
  new MLStack(app, `${projectName}-ML`, {
    env, projectName, environment,
    vpc: network.vpc,
    graphCluster: graph.cluster,
    trainingSchedule: 'monthly',     // or 'weekly'
  });
}

const auth = new AuthStack(app, `${projectName}-Auth`, { env, projectName, environment });

const api = new ApiStack(app, `${projectName}-Api`, {
  env, projectName, environment, industry,
  vpc: network.vpc,
  graphCluster: graph.cluster,
  graphSg: graph.sg,
  auth,
});

new FrontendStack(app, `${projectName}-Frontend`, {
  env, projectName, environment,
  api, auth,
});

app.synth();
```

## NetworkStack — VPC for Neptune

Neptune requires a VPC private subnet. Multi-AZ (minimum 2 AZ).

```typescript
import { Stack, StackProps, Tags, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface NetworkStackProps extends StackProps {
  projectName: string;
  environment: string;
}

export class NetworkStack extends Stack {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `${props.projectName}-vpc`,
      ipAddresses: ec2.IpAddresses.cidr('10.0.0.0/16'),
      maxAzs: 3,
      natGateways: props.environment === 'prod' ? 3 : 1,
      subnetConfiguration: [
        { name: 'Public',   subnetType: ec2.SubnetType.PUBLIC,              cidrMask: 24 },
        { name: 'Private',  subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 22 },
        { name: 'Isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED,    cidrMask: 24 },
      ],
      flowLogs: {
        rejected: {
          destination: ec2.FlowLogDestination.toCloudWatchLogs(
            new logs.LogGroup(this, 'FlowLogs', {
              retention: logs.RetentionDays.ONE_MONTH,
              removalPolicy: RemovalPolicy.DESTROY,
            }),
          ),
          trafficType: ec2.FlowLogTrafficType.REJECT,
        },
      },
    });

    // VPC Endpoints (Gateway free + Interface for Secrets, etc.)
    this.vpc.addGatewayEndpoint('S3Endpoint', { service: ec2.GatewayVpcEndpointAwsService.S3 });

    Tags.of(this).add('Project', props.projectName);
    Tags.of(this).add('Environment', props.environment);
  }
}
```

## GraphStack — Neptune cluster

```typescript
import { Stack, StackProps, Duration, Tags, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as neptune from 'aws-cdk-lib/aws-neptune';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface GraphStackProps extends StackProps {
  projectName: string;
  environment: string;
  vpc: ec2.IVpc;
  mode: 'serverless-v2' | 'provisioned';
  minNcu?: number;
  maxNcu?: number;
  instanceType?: string;          // for provisioned
}

export class GraphStack extends Stack {
  public readonly cluster: neptune.CfnDBCluster;
  public readonly clusterEndpoint: string;
  public readonly readerEndpoint: string;
  public readonly sg: ec2.SecurityGroup;
  public readonly cmk: kms.Key;

  constructor(scope: Construct, id: string, props: GraphStackProps) {
    super(scope, id, props);

    // KMS CMK (1 per project, used by Neptune + Kinesis + Lambda env vars)
    this.cmk = new kms.Key(this, 'Cmk', {
      alias: `alias/${props.projectName}-cmk`,
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,                 // mistake prevention
    });

    // Security Group
    this.sg = new ec2.SecurityGroup(this, 'NeptuneSg', {
      vpc: props.vpc,
      allowAllOutbound: false,
      description: 'Neptune cluster — accept from Lambda SG only',
    });

    // Subnet Group (Multi-AZ private isolated)
    const subnetGroup = new neptune.CfnDBSubnetGroup(this, 'SubnetGroup', {
      dbSubnetGroupDescription: `${props.projectName} subnet group`,
      subnetIds: props.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnetIds,
    });

    // Cluster
    this.cluster = new neptune.CfnDBCluster(this, 'Cluster', {
      dbClusterIdentifier: `${props.projectName}-${props.environment}`,
      engineVersion: '1.3.2.0',
      vpcSecurityGroupIds: [this.sg.securityGroupId],
      dbSubnetGroupName: subnetGroup.ref,
      iamAuthEnabled: true,                                // ★ IAM database auth
      storageEncrypted: true,
      kmsKeyId: this.cmk.keyArn,
      backupRetentionPeriod: props.environment === 'prod' ? 30 : 7,
      preferredBackupWindow: '17:00-18:00',                // UTC = KST 02:00-03:00
      preferredMaintenanceWindow: 'sun:18:00-sun:19:00',
      deletionProtection: props.environment === 'prod',
      // Serverless v2
      ...(props.mode === 'serverless-v2' && {
        serverlessScalingConfiguration: {
          minCapacity: props.minNcu ?? 0.5,
          maxCapacity: props.maxNcu ?? 16,
        },
      }),
    });
    this.cluster.applyRemovalPolicy(props.environment === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY);

    // Writer instance
    new neptune.CfnDBInstance(this, 'Writer', {
      dbClusterIdentifier: this.cluster.ref,
      dbInstanceClass: props.mode === 'serverless-v2' ? 'db.serverless' : (props.instanceType ?? 'db.r6g.large'),
      dbInstanceIdentifier: `${props.projectName}-${props.environment}-writer`,
    });

    // Reader instance (production only)
    if (props.environment === 'prod') {
      new neptune.CfnDBInstance(this, 'Reader1', {
        dbClusterIdentifier: this.cluster.ref,
        dbInstanceClass: props.mode === 'serverless-v2' ? 'db.serverless' : (props.instanceType ?? 'db.r6g.large'),
        dbInstanceIdentifier: `${props.projectName}-${props.environment}-reader1`,
      });
    }

    this.clusterEndpoint = this.cluster.attrEndpoint;
    this.readerEndpoint = this.cluster.attrReadEndpoint;

    Tags.of(this).add('Project', props.projectName);
    Tags.of(this).add('Component', 'graph');
    Tags.of(this).add('DataClassification', 'confidential');
  }
}
```

## IngestionStack — Kinesis + Lambda + DLQ + Bulk Loader bucket

```typescript
import { Stack, StackProps, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface IngestionStackProps extends StackProps {
  projectName: string;
  environment: string;
  vpc: ec2.IVpc;
  graphCluster: any;          // GraphStack's cluster
  graphSg: ec2.ISecurityGroup;
  shards: number;
}

export class IngestionStack extends Stack {
  public readonly stream: kinesis.IStream;
  public readonly bulkLoadBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, props);

    // Kinesis Data Stream (real-time edge updates)
    this.stream = new kinesis.Stream(this, 'EventStream', {
      streamName: `${props.projectName}-events`,
      shardCount: props.shards,
      retentionPeriod: Duration.hours(24),
      encryption: kinesis.StreamEncryption.MANAGED,
    });

    // DLQ (Dead-Letter Queue)
    const dlq = new sqs.Queue(this, 'IngestDlq', {
      queueName: `${props.projectName}-ingest-dlq`,
      retentionPeriod: Duration.days(14),
    });

    // Bulk loader S3 bucket
    this.bulkLoadBucket = new s3.Bucket(this, 'BulkLoadBucket', {
      bucketName: `${props.projectName}-bulk-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: Duration.days(30), prefix: 'archive/' }],
    });

    // Lambda Security Group
    const lambdaSg = new ec2.SecurityGroup(this, 'IngestLambdaSg', {
      vpc: props.vpc,
      allowAllOutbound: false,
      description: 'Ingestion Lambda',
    });
    lambdaSg.addEgressRule(props.graphSg, ec2.Port.tcp(8182), 'to Neptune');
    props.graphSg.addIngressRule(lambdaSg, ec2.Port.tcp(8182), 'from Ingest Lambda');

    // Bulk loader IAM role (Neptune assumes this)
    const bulkLoaderRole = new iam.Role(this, 'BulkLoaderRole', {
      roleName: `${props.projectName}-bulk-loader-role`,
      assumedBy: new iam.ServicePrincipal('rds.amazonaws.com'),
    });
    this.bulkLoadBucket.grantRead(bulkLoaderRole);

    // Ingestion Lambda
    const ingestFn = new lambda.Function(this, 'IngestFn', {
      functionName: `${props.projectName}-ingest`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambdas/ingest'),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSg],
      timeout: Duration.minutes(5),
      memorySize: 1024,
      reservedConcurrentExecutions: 50,         // prevent Neptune connection spikes
      environment: {
        NEPTUNE_ENDPOINT: props.graphCluster.attrEndpoint,
        NEPTUNE_PORT: '8182',
        BATCH_SIZE: '100',
      },
      deadLetterQueue: dlq,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // IAM auth — Neptune connect permission
    ingestFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['neptune-db:*'],
      resources: [`arn:aws:neptune-db:${this.region}:${this.account}:${props.graphCluster.ref}/*`],
    }));

    // Kinesis → Lambda event source mapping
    ingestFn.addEventSource(new (require('aws-cdk-lib/aws-lambda-event-sources').KinesisEventSource)(
      this.stream, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 100,
        maxBatchingWindow: Duration.seconds(5),
        retryAttempts: 3,
        bisectBatchOnError: true,
      },
    ));
  }
}
```

## MLStack (optional) — Neptune ML

```typescript
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as sagemaker from 'aws-cdk-lib/aws-sagemaker';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';

export interface MLStackProps extends StackProps {
  projectName: string;
  environment: string;
  vpc: ec2.IVpc;
  graphCluster: any;
  trainingSchedule: 'monthly' | 'weekly';
}

export class MLStack extends Stack {
  constructor(scope: Construct, id: string, props: MLStackProps) {
    super(scope, id, props);

    // SageMaker execution role for Neptune ML
    const sagemakerRole = new iam.Role(this, 'NeptuneMLRole', {
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerFullAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('NeptuneFullAccess'),
      ],
    });

    // Training trigger Lambda
    const trainFn = new lambda.Function(this, 'TrainTriggerFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambdas/train-gnn'),
      timeout: Duration.minutes(15),
      environment: {
        NEPTUNE_ENDPOINT: props.graphCluster.attrEndpoint,
        SAGEMAKER_ROLE_ARN: sagemakerRole.roleArn,
        TRAINING_INSTANCE_TYPE: 'ml.g4dn.xlarge',
        TRAINING_INSTANCE_COUNT: '1',
      },
    });
    trainFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['neptune-db:*', 'sagemaker:*', 'iam:PassRole'],
      resources: ['*'],
    }));

    // EventBridge schedule
    const schedule = props.trainingSchedule === 'weekly'
      ? events.Schedule.cron({ weekDay: 'SUN', hour: '20', minute: '0' })  // Sunday 5 AM KST
      : events.Schedule.cron({ day: '1', hour: '20', minute: '0' });        // 1st of every month

    new events.Rule(this, 'TrainingSchedule', {
      schedule,
      targets: [new targets.LambdaFunction(trainFn)],
    });
  }
}
```

## AuthStack — Cognito User Pool

```typescript
import { Stack, StackProps, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

export class AuthStack extends Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: StackProps & { projectName: string; environment: string }) {
    super(scope, id, props);

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${props.projectName}-${props.environment}`,
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: true },
      passwordPolicy: { minLength: 8 },
      removalPolicy: props.environment === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    this.userPoolClient = this.userPool.addClient('Client', {
      authFlows: { userPassword: true, userSrp: true },
      generateSecret: false,
      idTokenValidity: Duration.hours(1),
      accessTokenValidity: Duration.hours(1),
    });
  }
}
```

## ApiStack — API Gateway + Lambdas

```typescript
import { Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

export interface ApiStackProps extends StackProps {
  projectName: string;
  environment: string;
  industry: string;            // 'ecommerce' | 'media' | 'b2b-saas' | ...
  vpc: ec2.IVpc;
  graphCluster: any;
  graphSg: ec2.ISecurityGroup;
  auth: { userPool: cognito.UserPool; userPoolClient: cognito.UserPoolClient };
}

export class ApiStack extends Stack {
  public readonly api: apigw.RestApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // Lambda Security Group
    const lambdaSg = new ec2.SecurityGroup(this, 'ApiLambdaSg', {
      vpc: props.vpc, allowAllOutbound: false,
    });
    lambdaSg.addEgressRule(props.graphSg, ec2.Port.tcp(8182), 'to Neptune');
    lambdaSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'to Bedrock + AWS APIs');
    props.graphSg.addIngressRule(lambdaSg, ec2.Port.tcp(8182), 'from API Lambda');

    // Common Lambda env
    const commonEnv = {
      NEPTUNE_ENDPOINT: props.graphCluster.attrEndpoint,
      NEPTUNE_READER_ENDPOINT: props.graphCluster.attrReadEndpoint,
      NEPTUNE_PORT: '8182',
      INDUSTRY: props.industry,
      BEDROCK_MODEL_ID: 'us.anthropic.claude-sonnet-4-20250514-v1:0',
      AWS_REGION_NAME: this.region,
    };

    // Recommend Lambda
    const recommendFn = new lambda.Function(this, 'RecommendFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambdas/recommend'),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSg],
      timeout: Duration.seconds(30),
      memorySize: 2048,
      reservedConcurrentExecutions: 100,
      environment: commonEnv,
    });
    recommendFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['neptune-db:*', 'bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'],
    }));

    // Explore Lambda (graph viz API)
    const exploreFn = new lambda.Function(this, 'ExploreFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambdas/explore'),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSg],
      timeout: Duration.seconds(15),
      environment: commonEnv,
    });
    exploreFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['neptune-db:*'],
      resources: ['*'],
    }));

    // Admin Lambda (graph stats, schema mgmt)
    const adminFn = new lambda.Function(this, 'AdminFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('../backend/lambdas/admin'),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSg],
      timeout: Duration.seconds(30),
      environment: commonEnv,
    });

    // API Gateway
    this.api = new apigw.RestApi(this, 'Api', {
      restApiName: `${props.projectName}-api`,
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
      },
    });

    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'Authorizer', {
      cognitoUserPools: [props.auth.userPool],
    });
    const authOpts = { authorizer, authorizationType: apigw.AuthorizationType.COGNITO };

    // POST /recommendations/collaborative
    const recommendations = this.api.root.addResource('recommendations');
    recommendations.addResource('collaborative').addMethod('POST', new apigw.LambdaIntegration(recommendFn), authOpts);
    recommendations.addResource('cross-sell').addMethod('POST', new apigw.LambdaIntegration(recommendFn), authOpts);
    recommendations.addResource('popular').addMethod('POST', new apigw.LambdaIntegration(recommendFn), authOpts);

    // GET /graph/explore?user_id=X
    this.api.root.addResource('graph').addResource('explore').addMethod('GET', new apigw.LambdaIntegration(exploreFn), authOpts);

    // GET /admin/stats
    this.api.root.addResource('admin').addResource('stats').addMethod('GET', new apigw.LambdaIntegration(adminFn), authOpts);
  }
}
```

## FrontendStack — S3 + CloudFront + WAF

```typescript
import { Stack, StackProps, RemovalPolicy } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { ApiStack } from './api-stack';
import { AuthStack } from './auth-stack';

export interface FrontendStackProps extends StackProps {
  projectName: string;
  environment: string;
  api: ApiStack;
  auth: AuthStack;
}

export class FrontendStack extends Stack {
  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName: `${props.projectName}-${props.environment}-frontend-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: props.environment === 'prod' ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
      autoDeleteObjects: props.environment !== 'prod',
    });

    const oac = new cloudfront.S3OriginAccessControl(this, 'Oac');
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket, { originAccessControl: oac }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200,
    });

    // expose frontend config via SSM (build-time inject)
    new ssm.StringParameter(this, 'FrontendConfig', {
      parameterName: `/${props.projectName}/${props.environment}/frontend-config`,
      stringValue: JSON.stringify({
        apiUrl: props.api.api.url,
        cognito: {
          userPoolId: props.auth.userPool.userPoolId,
          clientId: props.auth.userPoolClient.userPoolClientId,
          region: this.region,
        },
        cloudfrontUrl: `https://${distribution.distributionDomainName}`,
      }),
    });
  }
}
```

## Pitfall avoidance (full 25 items in `shared/reference/constraints.md`)

| Pitfall # | CDK code location | Handling |
|---|---|---|
| #1 schema hard to change | `industry` context in `bin/app.ts` | finalize schema in Phase 2 |
| #2 Neptune SLv2 cost floor | `GraphStack.minNcu = 0.5` | $44/month even for dev — inform the user |
| #5 Real-time throughput | `IngestionStack.batchSize=100, maxBatchingWindow=5s` | UNWIND batch upsert |
| #6 IAM auth | `iamAuthEnabled: true` + lambda role neptune-db:* | gremlin-python SigV4 |
| #19 KMS RETAIN | `cmk.removalPolicy = RETAIN` | prevent accidental cluster lock |
| #20 deletion protection | production = `deletionProtection: true` + RETAIN | prevent data loss |
| #25 Tags | `Tags.of(stack).add('Component', 'graph')` | Cost Explorer breakdown |
