# AWS Services — Constraints & Quotas

## Amazon Connect Customer Profiles

| Item | Value | Notes |
|------|------|------|
| Instance quota (default) | 2 per account | Quota increase can be requested (4-5) |
| Domain names | Unique within Account+Region | |
| Object Types per domain | 100 | |
| Keys per Object Type | 10 | |
| Fields per Object Type | 200 | |
| Calculated Attributes per domain | 20 | |
| Profile size max | 50KB | |
| PutProfileObject rate | 500 TPS | Not an issue in the demo |

## AWS Entity Resolution

| Item | Value | Notes |
|------|------|------|
| ML Matching regions | us-east-1, us-west-2, eu-west-1, ap-southeast-1, etc. | **Must verify!** |
| Rule-based matching | Available in most regions | |
| Max match keys per rule | 15 | |
| Max rules per workflow | 25 | |
| Input: Glue Table required | | S3 direct not allowed |
| Processing time | ~5 min (10K records) | Batch job, not real-time |

### Cost (ER)
- Rule-based: $0.25 per 1,000 records processed
- ML-based: $1.00 per 1,000 records processed

## Amazon Neptune

| Item | Value | Notes |
|------|------|------|
| Minimum instance | db.r5.large | ~$0.58/hr = ~$420/month |
| Serverless option | Neptune Serverless | Minimum 2.5 NCU = ~$200/month |
| VPC required | YES | NAT Gateway adds cost |
| Query languages | openCypher, Gremlin, SPARQL | |

## Amazon Bedrock — Claude model catalog (latest, as of 2026-Q2)

> 📌 **ID format**: single-region calls use `<vendor>.<model>`; cross-region inference profiles use the `us./eu./apac.` prefix. e.g. `us.anthropic.claude-opus-4-7`. When calling from an AP region, use the `apac.` prefix. The inference profile matching the user's region must be explicitly allowed in IAM.

| Model | Bedrock model ID | Context | Max output | Strengths | Notes |
|---|---|---|---|---|---|
| **Claude Opus 4.7** | `us.anthropic.claude-opus-4-7` (cross-region) / `anthropic.claude-opus-4-7` | **1M** tokens | 128K | Best reasoning/coding, knowledge cutoff 2026-01 | Only `thinking.type: "adaptive"` supported (no enabled mode). Most expensive but highest precision. Recommended for ER rule generation/complex analysis |
| **Claude Sonnet 4** | `anthropic.claude-sonnet-4-20250514-v1:0` | 200K | 64K | Balanced model, strong coding+reasoning | Supports thinking + tool use. Default for most workloads |
| **Claude Opus 4.6** | `us.anthropic.claude-opus-4-6` | **1M** tokens | 128K | Generation just before flagship, large-volume RAG | knowledge cutoff 2025-05. Supports `thinking.type: "enabled"` |
| **Claude Opus 4.1** | `anthropic.claude-opus-4-1-20250805-v1:0` | 200K | 32K | Stable reasoning | knowledge bases / agents not supported |
| **Claude Haiku 4.5** | `anthropic.claude-haiku-4-5-20251001` | 200K | 8K | Cheapest/fastest | Suitable for high-volume simple classification/summarization workloads, first-pass filtering |

### Pricing (approximate, per 1M tokens)

| Model | Input | Output | Recommended use |
|---|---|---|---|
| Opus 4.7 | $15 | $75 | ER rule auto-improvement (low Bedrock call frequency, accuracy matters) |
| Sonnet 4 | $3 | $15 | General chat/summarization/analysis — UCP default |
| Haiku 4.5 | $1 | $5 | Marketing message generation, first-pass classification, high-volume personalization |

### Model selection guide (Skill proposes to the user)

```
Q: "What trade-off do you want for the AI features?"
├─ Accuracy is top priority (e.g. ER rule generation, complex reasoning)
│   → Opus 4.7   (`us.anthropic.claude-opus-4-7`)
├─ Balance (most cases)
│   → Sonnet 4   (`anthropic.claude-sonnet-4-20250514-v1:0`)
├─ Minimize cost (high-volume personalization, simple classification)
│   → Haiku 4.5  (`anthropic.claude-haiku-4-5-20251001`)
└─ Need 1M context (large profile + transaction history all at once)
    → Opus 4.7 or Opus 4.6
```

### IAM policy — both ARNs needed

```json
{
  "actions": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
  "resources": [
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
    "arn:aws:bedrock:us-east-1:<acct>:inference-profile/us.anthropic.claude-opus-4-7"
  ]
}
```

### Cautions

- **`apac.` prefix**: required when calling a cross-region inference profile from an AP region such as ap-northeast-2
- **Opus 4.7 thinking**: using `thinking.type: "enabled"` causes a BadRequest. Only `"adaptive"` is allowed
- **Model ID evolution**: a new generation is released every 6-12 months. This catalog is based on the 2026-01 cutoff; when building anew, re-confirm the latest ID once more via the AWS Knowledge MCP's `aws___search_documentation`

## Amazon Kinesis Data Streams

| Item | Value | Notes |
|------|------|------|
| Per-shard cost | ~$0.015/hr/shard | On-demand: auto-scaling |
| PutRecord rate | 1,000 records/sec/shard | |
| Data retention | 24h (default) - 365 days | |

## Amazon Cognito

| Item | Value | Notes |
|------|------|------|
| Free MAU | 50,000 | $0 cost in the demo |
| Hosted UI | Included | Custom domain possible |

## AWS Glue

| Item | Value | Notes |
|------|------|------|
| Database/Table creation | Free | Metadata only (with static definitions) |
| Crawler run | $0.44/DPU-hour | Min 10 min billed, usually 1-2 DPU used |
| Connection (JDBC) | Free | Credentials in Secrets Manager ($0.40/secret/month) |
| ETL Job | $0.44/DPU-hour | When data transformation is needed |

### Glue Connection supported sources
| Source | Connection Type | Notes |
|------|----------------|------|
| Amazon RDS (MySQL, PostgreSQL) | JDBC | Connection within VPC |
| Amazon Aurora | JDBC | Connection within VPC |
| Amazon Redshift | JDBC/Redshift | Native support |
| Amazon DynamoDB | DynamoDB | Direct support |
| On-premise DB | JDBC + VPN/DX | Site-to-Site VPN or Direct Connect required |
| S3 (CSV/Parquet/JSON) | S3 | Crawler or direct Table definition |

### Parquet vs CSV (S3 input)
| Item | CSV | Parquet |
|------|-----|---------|
| Compression | Low (original size) | High (50-80% savings) |
| Schema | External definition needed (Glue Table) | Built into the file |
| Query performance (Athena) | Full scan | Column pushdown (10x+ faster) |
| ER compatibility | ✅ Glue Table definition needed | ✅ Glue Table auto-recognized |
| Ease of generation | Excel/script immediately possible | Requires pandas/Spark, etc. |

## Aggregate cost examples

### Minimal (demo/PoC)
```
Connect CP Domain: ~$0 (when profile count is low)
Entity Resolution: ~$5/month (20K records × 1/week)
S3: ~$1
DynamoDB: ~$5 (on-demand)
Lambda: ~$0 (free tier)
Cognito: ~$0 (within 50K MAU)
API Gateway: ~$5
─────────────────
Total: ~$15-50/month
```

### Full (Neptune + Cross-Domain)
```
Above + Neptune: ~$300-420
Above + additional Connect Instances: ~$0 (the instances themselves are free)
Above + Kinesis (optional): ~$30
─────────────────
Total: ~$350-500/month
```
