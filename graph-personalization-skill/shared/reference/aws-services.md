# AWS Services Catalog

> This skill's catalog of services/models/regions/quotas/costs. **Always re-verify via AWS Knowledge MCP** (`aws___search_documentation`, `aws___get_regional_availability`).

## Amazon Neptune

### Mode comparison

| Mode | NCU/Instance | Unit price (us-east-1) | Best for |
|---|---|---|---|
| **Serverless v2** | 0.5 – 128 NCU auto-scale | $0.1608 / NCU-hour | dev/PoC, variable-load production (default) |
| **Provisioned db.r6g.large** | 16 GiB | ~$0.348/h ≈ $254/mo | large steady load + RI possible |
| **Provisioned db.r6g.xlarge** | 32 GiB | ~$0.696/h ≈ $508/mo | high volume |
| **Provisioned db.r6g.4xlarge** | 128 GiB | ~$2.784/h ≈ $2,032/mo | TB+ dataset |

### Storage

| Item | Unit price |
|---|---|
| Storage | $0.10 / GB / month |
| I/O | $0.20 / 1M I/O |
| Backup | free up to DB size during the retention period |
| Cross-region replica (Global DB) | additional cost |

### Engine versions

```
PostgreSQL compatible: N/A — Neptune is a graph DB, not an RDBMS
Engine: 1.3.x or later recommended (stable openCypher + ML integration)
2.x is also available — check the latest via MCP
```

### IAM database authentication

```python
# Connecting Lambda → Neptune (Python)
import boto3
from neo4j import GraphDatabase
# Use neo4j-driver with custom auth provider that signs SigV4

# Or gremlin-python + tornado SigV4 plugin
```

→ Password-based auth is also possible, but IAM is recommended (audit, no secret-rotation burden).

### Available regions

| Region | Neptune standard | Neptune Serverless v2 | Neptune ML |
|---|---|---|---|
| us-east-1 | ✅ | ✅ | ✅ |
| us-west-2 | ✅ | ✅ | ✅ |
| ap-northeast-2 (Seoul) | ✅ | ✅ | ⚠️ verify via MCP |
| ap-northeast-1 (Tokyo) | ✅ | ✅ | ✅ |
| eu-west-1 | ✅ | ✅ | ✅ |

→ Always re-confirm with `aws___get_regional_availability(filters=["Amazon Neptune"])`.

## Bedrock (for explanation)

### Claude models (cross-region inference profile)

| Alias | Inference profile ID (us) | Context | Recommended use |
|---|---|---|---|
| Claude Sonnet 4 | `us.anthropic.claude-sonnet-4-20250514-v1:0` | 200K | **Default for explanation generation** — balanced |
| Claude Sonnet 4.5 | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | 200K | faster Sonnet, similar cost |
| Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001` | 200K | lower cost — when a short explanation is enough |
| Claude Opus 4.7 | `us.anthropic.claude-opus-4-7` | 1M | complex GraphRAG reasoning |

### Token cost (us-east-1)

| Model | Input $/M tokens | Output $/M tokens |
|---|---|---|
| Sonnet 4 | $3 | $15 |
| Haiku 4.5 | $1 | $5 |
| Opus 4.7 | $15 | $75 |

### Prompt caching (lower cost)

When the system prompt + few-shot examples exceed 100K, enable **`promptCache: { type: "ephemeral" }`**:
- On a cache hit, cost drops by 90%
- 5-minute ephemeral cache (resets on continued calls)

→ Per-industry schema descriptions + examples rarely change, so caching is a good fit.

### Region/inference profile

```python
# Always specify the prefix (us./eu./apac./global.)
# Model IDs are updated monthly — confirm via MCP
```

## Kinesis Data Streams

| Item | Unit price |
|---|---|
| Shard hour | $0.015 / shard / hour ≈ $11/mo/shard |
| PUT payload | $0.014 / 1M req |
| Throughput | 1 MB/s in, 2 MB/s out per shard |
| Records | 1000 records/sec per shard |
| Retention | 24h default, max 365 days |

→ 1 shard = 1M events/day ≈ 12 events/sec is enough. For production with many shards, use auto-sharding (on-demand mode is also available).

## Neptune ML (optional)

### Components

```
1. Data Export Job (Neptune → S3 graph format)
2. Data Processing (S3 graph → SageMaker processing)
3. Model Training (SageMaker training job)
4. Model Transform (predictions back to Neptune as vertex properties)
```

### Cost

| Stage | Unit price |
|---|---|
| Export | Neptune I/O + Lambda |
| Processing (ml.m5.xlarge) | ~$0.27/h |
| Training (ml.g4dn.xlarge GPU) | ~$0.94/h |
| Transform (ml.m5.xlarge) | ~$0.27/h |

Training once per month: ~$30-50.
Four times per month (weekly): ~$120-200.

### Available regions

⚠️ Neptune ML is region-limited — verification with `aws___get_regional_availability` is mandatory.
Typically: us-east-1, us-west-2, eu-west-1, ap-northeast-1.

## API Gateway + Lambda

### API Gateway

| Item | Unit price |
|---|---|
| REST API | $3.50 / 1M req |
| HTTP API | $1.00 / 1M req (optional, cheaper) |
| Cognito authorizer | free |

### Lambda

| Item | Unit price |
|---|---|
| Request | $0.20 / 1M |
| Compute | $0.0000166667 / GB-second (1 GB) |
| 1024 MB Lambda for 1 second = $0.0000166667 |

→ Average Lambda 1 sec / 1024 MB / 10K req/day = ~$5/mo.

## S3 + CloudFront (Frontend)

| Item | Unit price |
|---|---|
| S3 Standard | $0.023 / GB / month |
| CloudFront data | $0.085 / GB |
| CloudFront requests | $0.0075 / 10K HTTPS |

## Cognito User Pool

| Item | Unit price |
|---|---|
| MAU | 50K free, then $0.0055/MAU |
| Hosted UI | free |

## DynamoDB (optional — recommendation cache)

| Item | Unit price |
|---|---|
| On-demand | $0.25 / 1M write, $0.0625 / 1M read |
| Storage | $0.25 / GB / month |
| TTL | free (cache auto-expire) |

→ Cache recommendation results (user_id key, 1-hour TTL) — reduces Neptune load.

## KMS

| Item | Unit price |
|---|---|
| CMK | $1/mo/key + $0.03/10K req |
| Best practice | 1 CMK + alias `alias/{projectName}-cmk`. Neptune, S3, and Kinesis all use this key |

## Cost scenarios (us-east-1)

### Dev / PoC

```
Component                              Monthly
──────────────────────────────────────────────
Neptune Serverless v2 (min 0.5 NCU)    $44
Lambda (~100 invocations/day)          $1
Bedrock Sonnet 4 (caching applied)     $5
Kinesis OR batch (skip)                $0 (batch)
S3 + CloudFront                        $5
Cognito (low MAU)                      $0
KMS                                    $1
──────────────────────────────────────────────
Total                                  ~$56/mo
```

### Production (medium, 1M events/d, 10K rec/d)

```
Component                              Monthly
──────────────────────────────────────────────
Neptune Serverless v2 (avg 4 NCU)      $464
Lambda                                 $30
Bedrock Sonnet 4 (with caching)        $150
Kinesis (1 shard)                      $11
S3 + CloudFront                        $30
Cognito                                $10
DynamoDB cache                         $20
KMS                                    $2
─────────────────────────────────────────────
Total                                  ~$717/mo
```

### + Neptune ML (optional)

```
+ SageMaker training (once/month, 4h)  $30
+ Data processing                      $10
+ (if hosting an inference endpoint)   $200+ (24/7 endpoint)
                                       (or $0 with batch transform)
```

### Production large (10M events/d, 100K rec/d)

```
Neptune Serverless v2 (avg 16 NCU)     $1,853
or Provisioned db.r6g.4xlarge + RI     $1,200
Lambda                                 $200
Bedrock Haiku 4.5 (cost-conscious)     $300
Kinesis (10 shards)                    $110
DynamoDB cache (high read)             $200
─────────────────────────────────────────────
Total                                  ~$2,500-3,000/mo
```

## CDK package versions

```
aws-cdk-lib                 ^2.150.0
constructs                  ^10.0.0
```

Additional (Neptune-related):
```python
# Neptune CDK Construct (optional — L2 provides some features)
from aws_cdk import aws_neptune_alpha as neptune_alpha   # some features
from aws_cdk import aws_neptune as neptune_cfn           # CFN-level
```

## Service Quotas — items recommended for increase

| Service | Quota | Default | Recommended increase |
|---|---|---|---|
| Neptune | DB clusters | 40 | usually sufficient |
| Bedrock | Sonnet 4 RPM | 50 | 200+ (production) |
| Bedrock | Sonnet 4 TPM | 200K | 2M+ |
| Kinesis | shards per region | 500 | usually sufficient |
| Lambda | concurrent executions | 1000 | for large production |

## Foundation model access procedure

1. AWS Console → Bedrock → Model access
2. Anthropic Claude Sonnet 4 / Haiku 4.5 → Request
3. Fill out the use-case form (organization root account; child accounts inherit automatically)
4. Approved immediately
