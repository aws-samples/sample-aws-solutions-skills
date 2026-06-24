# AWS Services Catalog

> A catalog of the **models/regions/quotas/cost** of the AWS services this skill uses. **Always re-verify via the AWS Knowledge MCP (`aws___search_documentation`, `aws___get_regional_availability`)**. This table is only a hint.

## Bedrock Foundation Models

### Claude (Anthropic) — recommended for Orchestrator/sub-agent

> When using a cross-region inference profile prefix, the exact ID differs per region. When calling Bedrock via an inference profile, quota is aggregated not per region but at the USA / EU / APAC level.

| Alias | Inference profile ID (us-based) | Context | Strength | Recommended use |
|---|---|---|---|---|
| Claude Opus 4.7 | `us.anthropic.claude-opus-4-7` | 1M | Top accuracy, adaptive thinking | Complex intent classification, multi-step reasoning sub-agent |
| Claude Sonnet 4 | `us.anthropic.claude-sonnet-4-20250514-v1:0` | 200K | Balanced | **Orchestrator default**, most sub-agents |
| Claude Sonnet 4.5 | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | 200K | Faster Sonnet | One step above on the cost/speed vs accuracy balance |
| Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001` | 200K | Cost/speed first | Simple intent classification, lightweight sub-agent |

**Region prefix mapping**:
- `us.` — use for N. Virginia / Ohio / Oregon
- `eu.` — EU regions
- `apac.` — APJ regions
- `global.` — cross-continent inference (Sonnet 4.5+)

> Always re-confirm the model ID via `aws___search_documentation`. Since Anthropic requires first-time customers to submit a use-case form, you need to enable Anthropic model access once in the Bedrock console.

### Embedding models (for the Knowledge Base)

| Model | ID | Dimensions | Recommended |
|---|---|---|---|
| Titan Embed Text v1 | `amazon.titan-embed-text-v1` | 1536 | Default (cdklabs `BedrockFoundationModel.TITAN_EMBED_TEXT_V1`) |
| Titan Embed Text v2 | `amazon.titan-embed-text-v2:0` | 256/512/1024 | When saving on dimensions |
| Cohere Embed English | `cohere.embed-english-v3` | 1024 | Better English accuracy |
| Cohere Embed Multilingual | `cohere.embed-multilingual-v3` | 1024 | Mixed Korean/English corpus |

## AgentCore (Bedrock AgentCore Alpha)

### Available regions (as of 2026-05, re-verify via MCP)

| Region | Runtime | Gateway | Memory |
|---|---|---|---|
| us-east-1 | ✅ | ✅ | ✅ |
| us-west-2 | ✅ | ✅ | ✅ |
| eu-west-1 | ✅ | ⚠️ check | ⚠️ check |
| ap-northeast-1 | ⚠️ check | ⚠️ check | ⚠️ check |
| ap-northeast-2 | ⚠️ check | ⚠️ check | ⚠️ check |

> ⚠️ check = confirm with `aws___get_regional_availability(resource_type="product", filters=["Amazon Bedrock AgentCore"])`.

### Quotas (default)

| Resource | Default | Increasable |
|---|---|---|
| Runtime per account | 100 | service quota increase possible |
| Gateway per account | 25 | service quota increase possible |
| Memory per account | 50 | service quota increase possible |
| Concurrent invocations / Runtime | 200 | yes |
| Runtime container image size | 10 GB | fixed |

### Cost (us-east-1, 2026-05, estimated)

| Item | Unit price | Note |
|---|---|---|
| Runtime invocation (compute) | per-second per-vCPU | 0 when the container is idle (keep-warm ~5 min after a cold start) |
| Gateway request | $0.001 / 1K requests | the semantic-search embedding call is separate |
| Memory storage | per-event | cheaper the shorter event_expiry_days is |
| Memory semantic strategy | 1 embedding call/event | cost increases per added strategy |

> For exact pricing, see the AWS Pricing docs (`aws___search_documentation` topic=`current_awareness`).

## Bedrock Knowledge Base

| Item | Note |
|---|---|
| Vector store options | Aurora PostgreSQL Serverless, OpenSearch Serverless, Pinecone, Redis |
| Default (CDK construct) | OpenSearch Serverless (cdklabs `VectorKnowledgeBase`) |
| Web Crawler data source | URL-list based, respects robots.txt, depth-limit possible |
| S3 data source | Files (.md, .txt, .pdf, .csv, etc.) auto-chunking |
| Chunking strategy | `fixed_size(max_tokens=500, overlap_percentage=20)` by default |
| Cost | OpenSearch Serverless OCU ~$0.24/h (minimum 2 OCU = $345/mo); a Pinecone free tier is possible |

## Cognito

| Component | Use |
|---|---|
| User Pool (Orchestrator) | User authentication (USER_PASSWORD_AUTH) |
| User Pool (per-MCP) | M2M (client_credentials), resource server `<tool>-api/invoke` scope |
| User Pool Domain | OAuth2 token endpoint (`<prefix>.auth.<region>.amazoncognito.com/oauth2/token`) |
| Resource Server | Defines the scope (`scope_name="invoke"`) |
| Cost | MAU-based, $0.0055/MAU after 50K free |

## Athena / Glue / S3 (when using the Text2SQL sub-agent)

| Item | Note |
|---|---|
| Athena | $5 / TB scanned (us-east-1) |
| Glue Data Catalog | 1M objects / 1M requests free, then $1/100K requests |
| S3 (table data) | $0.023/GB/mo (Standard) |

## ECR (container images)

| Item | Note |
|---|---|
| Storage | $0.10/GB/mo (a regular ECR repo is auto-created on CDK auto-deploy) |
| Data transfer | standard rates on egress |

## CloudWatch / X-Ray

| Item | Note |
|---|---|
| Logs ingestion | $0.50/GB |
| Logs storage | $0.03/GB/mo (retention=ONE_WEEK recommended for dev) |
| X-Ray traces | 100K free/mo, then $5 / 1M traces |

## Cost estimate scenarios

### Dev / PoC (single user, 100 queries/day)
```
- Runtime invocation:       ~$5/mo  (container idle, occasional invocation)
- Gateway:                  ~$1/mo
- Memory:                   ~$2/mo
- Bedrock Sonnet 4:         ~$10/mo (2K tokens avg, 100 query/d)
- KB (OpenSearch Serverless 2 OCU): ~$345/mo  ★ dominant
- Cognito (1 MAU):          $0
- Athena/Glue (low volume): ~$1/mo
─────────────────────────────────────────
Total: ~$364/mo  (KB dominates — $20/mo if using Pinecone or skipping the KB)
```

### Prod (1000 users, 10K queries/day)
```
- Runtime invocation:       ~$200/mo
- Gateway:                  ~$30/mo
- Memory (long-term sem):   ~$80/mo
- Bedrock Sonnet 4:         ~$1000/mo (intent classification + response synthesis)
- Sub-agent invocations:    ~$300/mo (Text2SQL Sonnet 4)
- KB:                       ~$345/mo (KB OCU fixed)
- Cognito (1000 MAU):       ~$5/mo
- Athena scan:              variable, ~$50/mo
─────────────────────────────────────────
Total: ~$2,010/mo
```

## CDK package version lockstep

| Library | Version (lockstep) |
|---|---|
| `aws-cdk-lib` | `2.231.0` |
| `aws-cdk.aws-bedrock-agentcore-alpha` | `2.231.0a0` |
| `cdklabs.generative-ai-cdk-constructs` | latest |
| `bedrock-agentcore` (runtime SDK) | latest |
| `bedrock-agentcore-starter-toolkit` | latest |
| `strands-agents`, `strands-agents-tools` | latest |

> When upgrading the `aws-cdk-lib` major version, the `*-alpha` must also match the same version prefix. A mismatch causes a `Construct property type mismatch` error.

## Foundation model access procedure

1. Go to AWS Console → Bedrock → Model access
2. Anthropic Claude (Sonnet 4, etc.) "Request" → submit the use-case form
3. Submitting once in the root account of the same AWS Organization makes it inherited by child accounts
4. Can be automated via the `PutUseCaseForModelAccess` API
5. Approval is instant — confirm "Available" in the Bedrock console

## Service quotas — items recommended for increase

| Service | Quota | Default | Recommended increase |
|---|---|---|---|
| Bedrock | Sonnet 4 RPM (per region) | 50 | 200+ (production) |
| Bedrock | Sonnet 4 TPM | 200K | 2M+ (production) |
| AgentCore | Runtime per account | 100 | usually no increase needed |
| ECR | Repos per region | 10000 | auto, sufficient |
