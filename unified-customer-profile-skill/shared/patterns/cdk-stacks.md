# CDK Stack Patterns

Reusable CDK stack patterns. When generating, reference these blocks to produce tailored code.

## Foundation Stack

```typescript
import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

export interface FoundationStackProps {
  projectName: string;
}

export class FoundationStack extends Construct {
  public readonly kmsKey: kms.Key;
  public readonly dlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: FoundationStackProps) {
    super(scope, id);

    this.kmsKey = new kms.Key(this, 'DataKey', {
      alias: `${props.projectName}-data-key`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.dlq = new sqs.Queue(this, 'DLQ', {
      queueName: `${props.projectName}-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: this.kmsKey,
    });
  }
}
```

## Storage Stack

```typescript
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface StorageStackProps {
  projectName: string;
}

export class StorageStack extends Construct {
  public readonly dataBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id);

    this.dataBucket = new s3.Bucket(this, 'DataBucket', {
      bucketName: `${props.projectName}-data-${cdk.Aws.ACCOUNT_ID}-${cdk.Aws.REGION}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      versioned: false,
      lifecycleRules: [{
        id: 'cleanup-old-data',
        expiration: cdk.Duration.days(90),
        prefix: 'er-output/',
      }],
    });
  }
}
```

## Ingestion Stack (Multi-Mode)

```typescript
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import type { SchemaConfig } from '../shared/schema-config';

export interface IngestionStackProps {
  projectName: string;
  dlq: sqs.Queue;
  dataBucket: s3.Bucket;
  schemaConfig: SchemaConfig;
}

export class IngestionStack extends Construct {
  public readonly bookingStream?: kinesis.Stream;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id);
    const mode = props.schemaConfig.features.ingestion.mode;

    // ─── CSV Mode ─────────────────────────────────────────
    // S3 upload → Lambda parsing → Glue Table (simplest)
    if (mode === 'csv' || mode === 'parquet' || mode === 'hybrid') {
      // S3 notification → Lambda to validate & register in Glue
      // (for CSV, parse+transform; for Parquet, reference the Glue Table directly)
    }

    // ─── Parquet Mode ─────────────────────────────────────
    // Upload Parquet directly to S3 → Glue Table references it directly
    // No separate transformation needed (schema is embedded in the file)
    if (mode === 'parquet') {
      // Set the Glue Table SerDe to Parquet
      // InputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat'
      // SerdeInfo: { serializationLibrary: 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe' }
    }

    // ─── Glue Connection Mode ─────────────────────────────
    // Crawl directly from an existing DB (RDS, Aurora, DynamoDB, etc.)
    if (mode === 'glue_connection') {
      // 1. Store DB credentials in Secrets Manager
      const dbSecret = new secretsmanager.Secret(this, 'DbSecret', {
        secretName: `${props.projectName}/db-credentials`,
        description: 'Database credentials for Glue Connection',
        generateSecretString: {
          secretStringTemplate: JSON.stringify({ username: 'admin' }),
          generateStringKey: 'password',
        },
      });

      // 2. Glue Connection (JDBC)
      const connection = new glue.CfnConnection(this, 'DbConnection', {
        catalogId: cdk.Aws.ACCOUNT_ID,
        connectionInput: {
          name: `${props.projectName}-db-connection`,
          connectionType: 'JDBC',
          connectionProperties: {
            JDBC_CONNECTION_URL: 'jdbc:mysql://host:3306/dbname', // → replace with user input
            SECRET_ID: dbSecret.secretName,
          },
          physicalConnectionRequirements: {
            // VPC, Subnet, Security Group (when the DB is inside a VPC)
            availabilityZone: 'ap-northeast-2a',
            // subnetId: ...,
            // securityGroupIdList: [...],
          },
        },
      });

      // 3. Glue Crawler (auto-detect DB schema → create Glue Table)
      const crawlerRole = new iam.Role(this, 'CrawlerRole', {
        assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole'),
        ],
      });
      dbSecret.grantRead(crawlerRole);
      props.dataBucket.grantReadWrite(crawlerRole);

      new glue.CfnCrawler(this, 'DbCrawler', {
        name: `${props.projectName}-db-crawler`,
        role: crawlerRole.roleArn,
        databaseName: `${props.projectName}_db`,
        targets: {
          jdbcTargets: [{
            connectionName: `${props.projectName}-db-connection`,
            path: 'dbname/customers_table', // → replace with user input
          }],
        },
        schedule: { scheduleExpression: 'cron(0 0 * * ? *)' }, // daily at midnight
      });
    }

    // ─── Kinesis Mode ─────────────────────────────────────
    if (mode === 'kinesis' || mode === 'hybrid') {
      this.bookingStream = new kinesis.Stream(this, 'IngestionStream', {
        streamName: `${props.projectName}-ingestion-stream`,
        streamMode: kinesis.StreamMode.ON_DEMAND,
      });
      // + EventBridge Pipe → CP integration
    }
  }
}
```

## Profiles Stack (Schema-Driven)

```typescript
import * as cdk from 'aws-cdk-lib';
import * as connect from 'aws-cdk-lib/aws-connect';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import type { SchemaConfig } from '../shared/schema-config';

export interface ProfilesStackProps {
  projectName: string;
  kmsKey: kms.Key;
  dlq: sqs.Queue;
  schemaConfig: SchemaConfig;
}

export class ProfilesStack extends Construct {
  public readonly connectInstance: connect.CfnInstance;
  public readonly customerProfilesDomain: /* CP Domain reference */;

  constructor(scope: Construct, id: string, props: ProfilesStackProps) {
    super(scope, id);

    // Connect Instance
    this.connectInstance = new connect.CfnInstance(this, 'Instance', {
      instanceAlias: `${props.projectName}-instance`,
      identityManagementType: 'CONNECT_MANAGED',
      attributes: {
        contactflowLogs: false,
        contactLens: false,
        inboundCalls: true,
        outboundCalls: false,
        autoResolveBestVoices: true,
      },
    });

    // CP Domain (via Custom Resource — no L2 construct yet)
    const cpDomain = new cr.AwsCustomResource(this, 'CpDomain', {
      onCreate: {
        service: 'CustomerProfiles',
        action: 'createDomain',
        parameters: {
          DomainName: `${props.projectName}-domain`,
          DefaultExpirationDays: 365,
          DefaultEncryptionKey: props.kmsKey.keyArn,
          DeadLetterQueueUrl: props.dlq.queueUrl,
        },
        physicalResourceId: cr.PhysicalResourceId.of(`${props.projectName}-domain`),
      },
      onDelete: {
        service: 'CustomerProfiles',
        action: 'deleteDomain',
        parameters: { DomainName: `${props.projectName}-domain` },
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
    });

    // Object Types (from schema)
    // → use ObjectTypesConstruct (see object-types-construct pattern below)
  }
}
```

## Matching Stack

```typescript
import * as glue from 'aws-cdk-lib/aws-glue';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';

export class MatchingStack extends Construct {
  public readonly matchingResultsTable: dynamodb.Table;
  public readonly accuracyMetricsTable: dynamodb.Table;
  public readonly suggestionsTable: dynamodb.Table;
  public readonly ruleChangeHistoryTable: dynamodb.Table;
  public readonly erRoleArn: string;
  public readonly glueDbName: string;
  public readonly glueTableName: string;

  constructor(scope: Construct, id: string, props: MatchingStackProps) {
    super(scope, id);

    // Glue Database + Table (columns from schema PII fields)
    const glueDb = new glue.CfnDatabase(this, 'GlueDb', {
      catalogId: cdk.Aws.ACCOUNT_ID,
      databaseInput: { name: `${props.projectName}_db` },
    });

    // Table columns derived from schema.pii_fields
    const columns = props.schemaConfig.pii_fields.map(f => ({
      name: f.name,
      type: 'string', // ER uses string for all PII
    }));

    const glueTable = new glue.CfnTable(this, 'GlueTable', {
      catalogId: cdk.Aws.ACCOUNT_ID,
      databaseName: glueDb.ref,
      tableInput: {
        name: `${props.projectName}_variants`,
        storageDescriptor: {
          columns,
          location: `s3://${props.dataBucket.bucketName}/er-input/`,
          inputFormat: 'org.apache.hadoop.mapred.TextInputFormat',
          outputFormat: 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
          serdeInfo: { serializationLibrary: 'org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe' },
        },
      },
    });

    // DynamoDB Tables
    this.matchingResultsTable = new dynamodb.Table(this, 'MatchingResults', {
      tableName: `${props.projectName}-matching-results`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ... accuracyMetricsTable, suggestionsTable, ruleChangeHistoryTable similarly

    // ER IAM Role
    const erRole = new iam.Role(this, 'ErRole', {
      assumedBy: new iam.ServicePrincipal('entityresolution.amazonaws.com'),
      // ... S3 + Glue permissions
    });
    this.erRoleArn = erRole.roleArn;
  }
}
```

## Auth Stack (Cognito)

```typescript
import * as cognito from 'aws-cdk-lib/aws-cognito';

export class AuthStack extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id);

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${props.projectName}-users`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      standardAttributes: { email: { required: true } },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.userPoolClient = this.userPool.addClient('WebClient', {
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        callbackUrls: ['http://localhost:3000/callback', `https://main.*.amplifyapp.com/callback`],
        logoutUrls: ['http://localhost:3000', `https://main.*.amplifyapp.com`],
      },
    });

    // Hosted UI domain
    this.userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: props.projectName },
    });

    // Create admin user
    // → Custom Resource to create user with admin email from schema
  }
}
```

## Graph Stack (Optional)

```typescript
// Only created when schemaConfig.features.graph.enabled === true
import * as neptune from 'aws-cdk-lib/aws-neptune';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

export class GraphStack extends Construct {
  constructor(scope: Construct, id: string, props: GraphStackProps) {
    super(scope, id);

    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1, // ⚠️ cost: ~$45/month
    });

    const cluster = new neptune.CfnDBCluster(this, 'Cluster', {
      dbClusterIdentifier: `${props.projectName}-graph`,
      engineVersion: '1.3.2.1',
      iamAuthEnabled: true,
      storageEncrypted: true,
      // ...
    });

    new neptune.CfnDBInstance(this, 'Instance', {
      dbClusterIdentifier: cluster.ref,
      dbInstanceClass: 'db.r5.large', // ⚠️ minimum ~$300/month
      dbInstanceIdentifier: `${props.projectName}-graph-1`,
    });
  }
}
```

## Main Stack (Orchestrator)

```typescript
// main-stack.ts — wires all layers together
export class MainStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MainStackProps) {
    super(scope, id, props);
    const { schemaConfig } = props;
    const projectName = schemaConfig.project.name;

    const foundation = new FoundationStack(this, 'Foundation', { projectName });
    const storage = new StorageStack(this, 'Storage', { projectName });
    const profiles = new ProfilesStack(this, 'Profiles', { projectName, kmsKey: foundation.kmsKey, dlq: foundation.dlq, schemaConfig });
    const matching = new MatchingStack(this, 'Matching', { projectName, dataBucket: storage.dataBucket, schemaConfig });
    const ingestion = new IngestionStack(this, 'Ingestion', { projectName, dlq: foundation.dlq, dataBucket: storage.dataBucket, schemaConfig });
    const auth = new AuthStack(this, 'Auth', { projectName, schemaConfig });

    // Optional: Graph
    let graph: GraphStack | undefined;
    if (schemaConfig.features.graph.enabled) {
      graph = new GraphStack(this, 'Graph', { projectName });
    }

    // API (depends on everything)
    const api = new ApiStack(this, 'Api', {
      projectName, schemaConfig,
      dataBucket: storage.dataBucket,
      domainName: profiles.domainName,
      matchingResultsTable: matching.matchingResultsTable,
      // ... all references
    });
  }
}
```
