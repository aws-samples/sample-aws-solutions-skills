# AWS Services & Model Catalog

> ⚠️ **Model IDs, regional availability, and prices are volatile.** Always verify with **AWS Knowledge MCP** (`aws___search_documentation`, `aws___get_regional_availability`) at Discovery/Design time before emitting `lib/config/constants.ts`. Treat the values below as the *shape* of the catalog, not a frozen source of truth.

## Core services used

| Service | Role in this solution | Notes |
|---|---|---|
| **Amazon Bedrock** | Claude model invocation (bedrock-runtime) | SigV4 via ECS Task Role; Guardrail-compatible. Backend IDs are `global.`-prefixed inference profiles (verify via `aws bedrock list-inference-profiles` — **not** `us.`). |
| **Amazon Bedrock (Mantle)** | OpenAI-family (GPT-5.x) via the Responses route (`bedrock_mantle/`) | **Bearer-token auth, NOT SigV4** — the Responses route has no SigV4 path (verified against installed source); a short-term Bedrock API key is minted at runtime from the Task Role via `aws-bedrock-token-generator` into env **`BEDROCK_MANTLE_API_KEY`** (never `AWS_BEARER_TOKEN_BEDROCK` — boto3-reserved, breaks Claude). **Not** Guardrail-compatible. **us-east-1 only** → reached over cross-region VPC peering. Pin via `aws_region_name=us-east-1` + env `BEDROCK_MANTLE_REGION` + `BEDROCK_MANTLE_API_BASE` (NOT `MANTLE_REGION`). AWS Marketplace offering (auto-subscribe). IAM on `project/*`, not `foundation-model`. |
| **Amazon Bedrock Guardrails** | Content filter + denied topics + PII | Referenced by LiteLLM by `guardrailIdentifier` + `guardrailVersion`. Claude only. |
| **Amazon Bedrock AgentCore — Gateway + Web Search Tool** | Managed web search for agents | A `AWS::BedrockAgentCore::Gateway` (MCP, **AWS_IAM** inbound) + `GatewayTarget` with the built-in `web-search` connector. LiteLLM calls it cross-region via SigV4 (`aws_service_name: bedrock-agentcore`, `InvokeGateway`). **us-east-1 GA.** Replaces self-hosted Tavily. See `shared/patterns/agentcore-websearch.md`. |
| **AWS Marketplace** | Mantle (GPT-5.x) model subscription | Task Role `aws-marketplace:Subscribe` → first call auto-subscribes (transient 5xx during ~1-min setup). |
| **ECS Fargate (ARM64/Graviton)** | Runs LiteLLM proxy (and Langfuse) | `runtimePlatform.cpuArchitecture = ARM64`; circuit breaker + health checks. |
| **Application Load Balancer (edge + internal)** | The **edge** for developer traffic + an internal path for the Token Service | TLS per `config.litellm.certMode`: `acm` = a **public, internet-facing ALB** on HTTPS:443; `http` = a **public, internet-facing ALB** on HTTP:80 (plaintext, PoC-only). In both modes SG ingress is restricted to `litellm.albIngressCidrs`. A separate **internal ALB (HTTP:4000)** always exists for the Token Service (SSM URL unchanged). `idleTimeout` = `config.litellm.albIdleTimeoutSeconds` (default 900s, **max 4000s**) governs long completions — no more 120s CloudFront ceiling. Langfuse (acm only) gets its own public ALB. |
| **AWS Certificate Manager (regional)** | TLS cert for the public ALB | `acm`: a **regional** public cert in `config.awsRegion` (existing ARN or Route53 DNS-issued) — **not** a us-east-1 CloudFront cert. Auto-renews. `http` uses no cert at all. |
| **Amazon Aurora Serverless v2 (PostgreSQL)** | LiteLLM state + Langfuse traces | Isolated subnets, `storageEncrypted`; verify `engineVersion` per region. |
| **Amazon API Gateway (REST)** | Token Service edge | `org-sso`: `AuthorizationType.IAM` (SigV4). `cognito-native`: `AuthorizationType.COGNITO` (`CognitoUserPoolsAuthorizer`, access token only). |
| **Amazon Cognito User Pool** | `cognito-native` identity source | Sole identity store when org-sso is unavailable (account instance / no IdC). Hosted UI login (PKCE), native User Pool Groups = teams via the `cognito:groups` claim. No external IdP, no IdC federation. From `config.cognitoNative`. |
| **AWS Lambda (Python 3.12, ARM64)** | Token Service + db-init Custom Resource | VPC-placed; least-privilege grants. |
| **Amazon DynamoDB** | Virtual-key cache | `PAY_PER_REQUEST`, TTL, PITR. |
| **AWS Secrets Manager** | Master key, DB secrets, Langfuse app secrets | `ecs.Secret.fromSecretsManager`; `grantRead` for consumers. |
| **AWS Systems Manager Parameter Store** | Runtime cross-stack wiring (LiteLLM internal URL) | Read by the Token Lambda by name. |
| **Amazon Route 53 (private hosted zone)** | Cross-region DNS for `bedrock-mantle.us-east-1.api.aws` | PHZ associated with both the peer VPC and the gateway VPC; aliased to the bedrock-mantle endpoint. |
| **VPC Peering (cross-region)** | Gateway VPC ↔ us-east-1 Mantle peer VPC | Not auto-accepted cross-region → acceptance custom resource. See `shared/patterns/mantle-peering.md`. |
| **Amazon CloudWatch** | Usage dashboard: token usage by model/team, spend, latency, failures (EMF from the `cloudwatch_usage` callback), per-user + hourly Logs Insights, ALB requests/5xx | Usage + infra/cost observability. |
| **VPC Endpoints** | Gateway (S3, DynamoDB) + Interface (bedrock-runtime, secrets, ssm, ecr, ecr-docker, logs, bedrock-agentcore) in the gateway VPC; `bedrock-mantle` interface endpoint in the us-east-1 peer VPC | Keep traffic off the public internet. |
| **IAM Identity Center (SSO)** | Identity source (`org-sso` only) | Organization-instance only: the `AWSReservedSSO_` ARN prefix is the trust anchor; settings come from `config.sso`. Account instances cannot host the required SAML app — use `cognito-native` (Cognito row above) instead. |

## Model routing shape (verify IDs via MCP)

LiteLLM `model_list` entries map a client-facing alias → a backend route:

| Alias (client requests) | Backend route prefix | Auth | Guardrail |
|---|---|---|---|
| Claude (opus/sonnet/haiku/fable…) | `bedrock/global.anthropic.<model-id>` (verify with `aws bedrock list-inference-profiles`; recent models are `global.`-only, **not** `us.`) | SigV4 (Task Role), tokenless | ✅ attach Bedrock Guardrail |
| GPT (5.x family, e.g. `gpt-5.5`, `gpt-5.4`) | `bedrock_mantle/openai.<model-id>` | **Bearer token** (short-term Bedrock API key minted at runtime from the Task Role → env `BEDROCK_MANTLE_API_KEY`; NOT SigV4, NOT `AWS_BEARER_TOKEN_BEDROCK`); `aws_region_name`+`BEDROCK_MANTLE_REGION`+`BEDROCK_MANTLE_API_BASE`=us-east-1 | ❌ not compatible |

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
5. `certMode` — both modes run the public ALB (~$16–22/mo) + the internal Token Service ALB; `acm` adds only the (free) ACM cert. No WAF cost.
