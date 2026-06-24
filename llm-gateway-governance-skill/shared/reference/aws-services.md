# AWS Services & Model Catalog

> ⚠️ **Model IDs, regional availability, and prices are volatile.** Always verify with **AWS Knowledge MCP** (`aws___search_documentation`, `aws___get_regional_availability`) at Discovery/Design time before emitting `lib/config/constants.ts`. Treat the values below as the *shape* of the catalog, not a frozen source of truth.

## Core services used

| Service | Role in this solution | Notes |
|---|---|---|
| **Amazon Bedrock** | Claude model invocation (bedrock-runtime) | SigV4 via ECS Task Role; Guardrail-compatible. |
| **Amazon Bedrock (Mantle)** | OpenAI-family (GPT-5.x) via the Responses route (`bedrock_mantle/`) | SigV4; **not** Guardrail-compatible. **us-east-1 only** → reached over cross-region VPC peering. Pin via `aws_region_name=us-east-1` + env `BEDROCK_MANTLE_REGION` + `BEDROCK_MANTLE_API_BASE` (NOT `MANTLE_REGION`). AWS Marketplace offering (auto-subscribe). |
| **Amazon Bedrock Guardrails** | Content filter + denied topics + PII | Referenced by LiteLLM by `guardrailIdentifier` + `guardrailVersion`. Claude only. |
| **Amazon Bedrock AgentCore — Gateway + Web Search Tool** | Managed web search for agents | A `AWS::BedrockAgentCore::Gateway` (MCP, **AWS_IAM** inbound) + `GatewayTarget` with the built-in `web-search` connector. LiteLLM calls it cross-region via SigV4 (`aws_service_name: bedrock-agentcore`, `InvokeGateway`). **us-east-1 GA.** Replaces self-hosted Tavily. See `shared/patterns/agentcore-websearch.md`. |
| **AWS Marketplace** | Mantle (GPT-5.x) model subscription | Task Role `aws-marketplace:Subscribe` → first call auto-subscribes (transient 5xx during ~1-min setup). |
| **ECS Fargate (ARM64/Graviton)** | Runs LiteLLM proxy (and Langfuse) | `runtimePlatform.cpuArchitecture = ARM64`; circuit breaker + health checks. |
| **Application Load Balancer (internal)** | Fronts LiteLLM / Langfuse inside the VPC | `internetFacing: false`; HTTP listener (TLS at CloudFront). |
| **Amazon CloudFront** | Public entry; VPC Origin → internal ALB | Custom domain (ACM+Route53) **or** default `*.cloudfront.net`. `CACHING_DISABLED`, `ALL_VIEWER`. LiteLLM origin read/keepalive timeout = **60s** (Mantle cold-start subscribe). |
| **Amazon Aurora Serverless v2 (PostgreSQL)** | LiteLLM state + Langfuse traces | Isolated subnets, `storageEncrypted`; verify `engineVersion` per region. |
| **Amazon API Gateway (REST)** | SSO Token Service edge | `AuthorizationType.IAM` (SigV4). |
| **AWS Lambda (Python 3.12, ARM64)** | Token Service + db-init Custom Resource | VPC-placed; least-privilege grants. |
| **Amazon DynamoDB** | Virtual-key cache | `PAY_PER_REQUEST`, TTL, PITR. |
| **AWS Secrets Manager** | Master key, DB secrets, Langfuse app secrets | `ecs.Secret.fromSecretsManager`; `grantRead` for consumers. |
| **AWS Systems Manager Parameter Store** | Runtime cross-stack wiring (LiteLLM internal URL) | Read by the Token Lambda by name. |
| **Amazon Route 53 (private hosted zone)** | Cross-region DNS for `bedrock-mantle.us-east-1.api.aws` | PHZ associated with both the peer VPC and the gateway VPC; aliased to the bedrock-mantle endpoint. |
| **VPC Peering (cross-region)** | Gateway VPC ↔ us-east-1 Mantle peer VPC | Not auto-accepted cross-region → acceptance custom resource. See `shared/patterns/mantle-peering.md`. |
| **Amazon CloudWatch** | Dashboard (ALB requests/5xx, token service) | Infra/cost observability. |
| **VPC Endpoints** | Gateway (S3, DynamoDB) + Interface (bedrock-runtime, secrets, ssm, ecr, ecr-docker, logs, bedrock-agentcore) in the gateway VPC; `bedrock-mantle` interface endpoint in the us-east-1 peer VPC | Keep traffic off the public internet. |
| **IAM Identity Center (SSO)** | Identity source | The `AWSReservedSSO_` ARN prefix is the trust anchor; settings come from `config.sso`. |

## Model routing shape (verify IDs via MCP)

LiteLLM `model_list` entries map a client-facing alias → a backend route:

| Alias (client requests) | Backend route prefix | Auth | Guardrail |
|---|---|---|---|
| Claude (opus/sonnet/haiku/fable…) | `bedrock/us.anthropic.<model-id>` | SigV4 (Task Role) | ✅ attach Bedrock Guardrail |
| GPT (5.x family, e.g. `gpt-5.5`, `gpt-5.4`) | `bedrock_mantle/openai.<model-id>` | SigV4 (Task Role, #29788 overlay; `aws_region_name`+`BEDROCK_MANTLE_REGION`+`BEDROCK_MANTLE_API_BASE`=us-east-1) | ❌ not compatible |

Routing lives in `lib/config/constants.ts` (`MODELS`) and `services/litellm/config.yaml` (env-driven). Aliases are what tools like Claude Code / Codex send; keep them stable. `gpt-5.4` is the typical **economy-tier** Mantle model (≈2x cheaper than `gpt-5.5`).

## Third-party

| Dependency | Role | Secret |
|---|---|---|
| **Langfuse v2** (self-hosted) | Trace/prompt observability | Postgres-only deployment; app secrets in Secrets Manager. |

> Web search no longer uses a third-party (Tavily) API key — it is the AWS-managed **AgentCore Web Search Tool** (see core services). Nothing to procure or store.

## Cost levers (order of impact)

1. NAT gateways (1 vs per-AZ).
2. Aurora ACU min (scales to 0.5 in dev).
3. Fargate task size + `desiredCount`.
4. Langfuse on/off.
5. CloudFront `CACHING_DISABLED` is correct for a gateway (don't "optimize" it on).
