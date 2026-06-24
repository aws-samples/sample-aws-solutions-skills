# Architecture

> **A multi-agent system based on Strands Agents + Amazon Bedrock AgentCore.** A single Orchestrator classifies intent and routes the request to the appropriate destination among (1) MCP server tools, (2) specialized Strands agents, and (3) a Knowledge Base.

## High-level diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Frontend (React + Vite)                            │
│                  Amplify Auth (Cognito) → Bearer Token                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼  HTTPS + JWT
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Orchestrator Agent (AgentCore Runtime)                  │
│                                                                             │
│   • Strands Agent (Bedrock Claude Sonnet 4)                                 │
│   • Intent classification + tool selection                                  │
│   • ShortTermMemoryHooks → AgentCore Memory                                 │
│   • System prompt: context resolution + routing rules                       │
└──────────┬───────────────────┬─────────────────────────┬────────────────────┘
           │ MCP (SigV4)       │ HTTPS (JWT)             │ bedrock:Retrieve
           ▼                   ▼                         ▼
┌──────────────────────┐  ┌─────────────────────┐  ┌────────────────────────┐
│ AgentCore Gateway    │  │ Specialized Strands │  │ Bedrock                │
│ (semantic search)    │  │ Agent Runtime       │  │ Knowledge Base         │
│                      │  │ (Text2SQL, etc.)    │  │ (Web Crawler / S3)     │
│ ┌──────┐  ┌────────┐ │  │                     │  │                        │
│ │ Jira │  │ GitHub │ │  │ • Strands Agent     │  │ • Vector store         │
│ │ MCP  │  │ MCP    │ │  │ • Domain tools      │  │ • Titan Embed v1       │
│ │ tgt  │  │ target │ │  │ • Direct Athena/RDS │  │                        │
│ └──┬───┘  └───┬────┘ │  └─────────┬───────────┘  └────────────────────────┘
│    │          │      │            │
│    │ OAuth2   │      │            │ AWS SDK
│    │ M2M      │      │            ▼
│    ▼          ▼      │      ┌──────────────────────┐
│ ┌──────┐  ┌────────┐ │      │ Athena / Glue / S3   │
│ │ Jira │  │ GitHub │ │      │ Aurora / DynamoDB    │
│ │ MCP  │  │ MCP    │ │      └──────────────────────┘
│ │ Run- │  │ Run-   │ │
│ │ time │  │ time   │ │
│ │(Fast │  │(Fast   │ │
│ │ MCP) │  │ MCP)   │ │
│ └──────┘  └────────┘ │
└──────────────────────┘
```

## Component decisions and WHY

### 1. Orchestrator Agent on AgentCore Runtime
- **What**: A single entry-point agent. It receives a user request, classifies intent, and routes to the appropriate tool, sub-agent, or KB.
- **Why AgentCore Runtime?**
  - A container-based, fully-managed runtime — no need to operate ECS/Fargate yourself; cold starts and scaling are managed automatically.
  - A built-in JWT authorizer (Cognito User Pool) outsources user authentication.
  - Built-in X-Ray tracing, CloudWatch logging, and metrics.
- **Why Strands?**
  - Tool calling, hooks, streaming, and multi-agent patterns are built in.
  - The `BedrockModel` adapter connects directly to Bedrock Claude.
  - The `HookProvider` interface cleanly composes Memory, logging, and policy.
- **Streaming**: AgentCore Runtime streams responses in Server-Sent Events (`data: ...`) format. The frontend parses SSE lines and renders incrementally.

### 2. AgentCore Gateway → MCP servers
- **What**: Bundles multiple MCP servers (Jira, GitHub, ...) behind a single endpoint and, via **semantic search**, exposes only the tools most relevant to the user's query.
- **Why Gateway, not a direct MCP connect?**
  - Once tools exceed 50, the LLM context explodes. Gateway semantic search exposes only the top-k tools, saving tokens.
  - Authentication is unified in one place at the Gateway (SigV4 from Orchestrator → Gateway, OAuth2 client_credentials from Gateway → MCP target).
  - MCP servers can be added/removed without changing Orchestrator code.
- **Trade-off**: The Gateway can only be defined as IaC via the alpha CDK module (`aws_cdk.aws_bedrock_agentcore_alpha`) — the API may change.

### 3. Specialized Strands Agent (direct invoke)
- **What**: Some intent-classification results are delegated to a separate Strands agent (e.g., Text2SQL — which needs its own LLM reasoning + multi-step tool calls).
- **Why direct invoke, not via Gateway?**
  - A sub-agent is not a simple tool but needs its own reasoning loop. The Gateway tool interface (a single function call) is unsuitable.
  - The Orchestrator calls it with `requests.post(agent_url, headers={authorization: Bearer ...})` → parses the SSE stream to extract text.
  - It uses an access token issued via Cognito USER_PASSWORD_AUTH as the Bearer.
- **Pattern**: An `@tool`-decorated Python function in the Orchestrator makes the HTTP call to the sub-agent. From the LLM's perspective, it is just another tool.

### 4. AgentCore Memory (short-term)
- **What**: The Strands `HookProvider` saves each message via `create_event`, and at agent init injects K turns into the system prompt via `get_last_k_turns`.
- **Why short-term?**
  - In the vast majority of scenarios, solving only conversational continuity (pronoun resolution, follow-up context) is enough.
  - Long-term/semantic memory (`MemoryStrategy.using_built_in_semantic`) overlaps in role with RAG, so add it carefully.
- **Trade-off**: `event_expiry_days=7` cleans up automatically. If longer retention is needed, combine with a KB.

### 5. Bedrock Knowledge Base
- **What**: Vectorizes static documents (workshop docs, FAQs, policies) and answers via RAG.
- **Why?**
  - To answer general-purpose Q&A like "What is X?" using **domain documents** rather than the LLM's general knowledge.
  - A site can be synced in one pass with a Web Crawler data source (cdklabs/generative-ai-cdk-constructs `WebCrawlerDataSource`).
- **Embedding**: `TITAN_EMBED_TEXT_V1` (Cohere Embed is also possible — see `aws-services.md`).

### 6. Cognito (auth)
- **Two flows side by side**:
  - **JWT (USER_PASSWORD_AUTH)**: user → Frontend → Orchestrator Runtime. The Authenticator component manages the tokens.
  - **OAuth2 client_credentials (M2M)**: Gateway → MCP target. Issued with the resource server `{tool}-api/invoke` scope.
- **Why two?**
  - The Runtime needs per-user workload identity, while Gateway-MCP is a machine-to-machine trust relationship.
  - Rather than placing both in the same User Pool, separate them per stack — to prevent permission explosion.
- **Token rotation**: `CognitoTokenManager` calls `initiate_auth` or `oauth2/token` on every invocation (caching once only). Short expiry.

### 7. SigV4 (Gateway invocation auth)
- **What**: Orchestrator Runtime → Gateway uses IAM auth (`bedrock-agentcore:InvokeGateway`). `httpx.Auth` is implemented as SigV4 and injected into the streamable HTTP MCP client.
- **Why?**
  - It is simplest when the Gateway-side authorizer is `using_aws_iam()`. No need to issue a separate Cognito JWT.
  - Only the Runtime's IAM role is managed as the trust boundary.

## Stack composition

| Stack | Responsibility | Dependencies |
|---|---|---|
| `OrchestratorAgentCoreStack` | Orchestrator Runtime + Cognito User Pool + Memory | (none, first deploy) |
| `<DomainMcp>Stack` (e.g., JiraMcp) | MCP Runtime + Cognito M2M client + OAuth2 provider + endpoint URL | Orchestrator (ordering) |
| `<DomainAgent>Stack` (e.g., Text2SqlAgent) | Specialized Strands Runtime + Cognito + data source | Orchestrator |
| `KnowledgeBaseStack` | Bedrock KB + Web Crawler data source | (independent) |
| `AgentCoreGatewayStack` | Gateway + MCP targets | all MCP stacks |
| `(optional)` Frontend hosting | S3 + CloudFront / Amplify Hosting | Orchestrator (needs the User Pool ID) |

> **Order matters**: The Gateway receives every MCP stack's OAuth provider ARN and endpoint URL as cross-stack references, so it is deployed last.

## Request lifecycle (example: "Show me total sales by customer")

1. The user types a question in the chat UI → the frontend calls the Orchestrator endpoint with the Cognito JWT header (POST `/invocations`).
2. The Orchestrator entry handler fetches the `/agentcore_gateway/gateway_url` SSM parameter + the `text2sql_agent/cognito/credentials` Secret.
3. `ShortTermMemoryHooks` prepends the last N turns to the system prompt, keyed by `actor_id` (=customer_id) + `session_id`.
4. The Strands `Agent` reads the routing table in the system prompt, classifies the intent as `DATA`, and calls the `query_data` local tool.
5. Inside `query_data`, a Bearer is issued via `CognitoTokenManager.get_fresh_token()` → an SSE POST is made to the Text2SQL agent endpoint.
6. The Text2SQL Strands agent: looks up the schema → generates SQL with Bedrock → runs Athena → returns row results.
7. The Orchestrator receives the result, summarizes it in natural language, and streams it back to the frontend over SSE.
8. The `MessageAddedEvent` hook saves the user/assistant turn to Memory.

## Cross-stack communication

| Flow | Auth |
|---|---|
| Frontend → Orchestrator Runtime | Cognito User Pool JWT (USER_PASSWORD_AUTH) |
| Orchestrator → Gateway | AWS SigV4 (`bedrock-agentcore:InvokeGateway`) |
| Gateway → MCP target Runtime | OAuth2 client_credentials (per-MCP Cognito client) |
| Orchestrator → Specialized Agent Runtime | Cognito Bearer token (per-agent Cognito client) |
| Orchestrator → Knowledge Base | IAM (`bedrock:Retrieve`) |
| Orchestrator → Memory | IAM (`bedrock-agentcore:CreateEvent`, etc.) |

## Directory mapping (CDK output)

```
generated-project/
├── cdk-infra/                              ← AWS CDK Python (uses the alpha agentcore module)
│   ├── app.py
│   └── src/stacks/
│       ├── orchestrator_agent_stack.py
│       ├── <domain>_mcp_stack.py
│       ├── <domain>_agent_stack.py         ← (optional) specialized Strands agent
│       ├── knowledge_base_stack.py
│       └── agentcore_gateway_stack.py
├── agents/
│   ├── orchestrator-agent/                 ← Strands + AgentCore SDK
│   │   ├── orchestrator_agent.py
│   │   ├── common/{aws_config,cognito_token_manager,sigv4_auth,prompts}.py
│   │   ├── memory/short_term_memory.py     ← HookProvider
│   │   ├── Dockerfile (LINUX_ARM64)
│   │   └── requirements.txt
│   └── <domain>-agent/                     ← (optional) specialized Strands agent
│       └── ...
├── mcp-servers/
│   ├── <domain>-mcp/                       ← FastMCP (streamable-http)
│   │   ├── <domain>_mcp.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── ...
├── frontend/                               ← React + Vite + Amplify
│   ├── src/{App.tsx, pages/chat.tsx, store/, components/}
│   ├── public/config.json                  ← injects Cognito + endpoint URL
│   └── package.json
└── scripts/
    ├── deploy.sh
    ├── destroy.sh
    └── test_orchestrator.py
```

## Why this composition over alternatives

| Alternative | Why not adopted |
|---|---|
| Implementing directly with Lambda + Step Functions | No automated container SDK packaging, extra work for MCP streamable-http compatibility, and harder to leverage Strands hooks/streaming |
| Bedrock Agents (legacy) | Forces OpenAPI definitions, requires Lambda + Action Group boilerplate to add custom tools, and is incompatible with MCP |
| Operating ECS Fargate directly | You configure auth/scaling/logging yourself. AgentCore Runtime bundles all of the above |
| A single giant agent | Beyond ~30 tools, intent confusion and hallucination increase. Classifying intent first and then delegating to a sub-agent is superior in both accuracy and cost |

## Volatile catalog (always verify via MCP)

`shared/reference/aws-services.md` has a catalog, but **always re-confirm via the AWS Knowledge MCP**:
- AgentCore Runtime/Gateway/Memory region availability
- Bedrock Claude Sonnet 4 / Opus 4 model IDs and cross-region inference profile prefixes (`us.`, `eu.`, `apac.`)
- The `aws_cdk.aws_bedrock_agentcore_alpha` package version (the CDK core and the alpha are in lockstep)
