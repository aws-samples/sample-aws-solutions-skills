# Decision Tree

> Conditional logic for deciding component selection from the user's responses. **Discovery-phase answers → map via these tables → Architecture Design output**.

## 1. Domain tool integration approach — MCP server vs specialized agent vs direct call

Key question: **"Is this integration a simple function call, or does it need its own reasoning?"**

| Integration pattern | When to choose? | Examples |
|---|---|---|
| **MCP Server (Gateway target)** | A stateless function interface to the external system is sufficient | Jira REST API, GitHub REST API, Notion API, Slack Webhook |
| **Specialized Strands Agent** | The LLM must call tools in multiple steps and synthesize the results to get an answer | Text2SQL (schema lookup → SQL generation → execution → retry), code-generation agent, browser agent |
| **Local Tool (a function inside the Orchestrator)** | A single AWS API call, or a thin wrapper to invoke a sub-agent | `bedrock:Retrieve` (KB), Bedrock Guardrail check |
| **Direct boto3 (no agent)** | A simple statistics/monitoring endpoint that needs no LLM reasoning | CloudWatch alarm status, S3 file listing |

Decision flow:

```
Need to integrate an external system?
├─ Yes
│  ├─ Multi-step reasoning required? (e.g., user intent → appropriate search → result synthesis)
│  │  └─ Yes → Specialized Strands Agent (Runtime + direct invoke)
│  │  └─ No → MCP Server (Gateway target)
│  └─ Just an API call without reasoning → MCP Server
└─ No (a direct AWS service call is sufficient) → Local Tool (inside the Orchestrator)
```

## 2. Gateway search type — SEMANTIC vs LITERAL

| Number of tools | Choice |
|---|---|
| ≤ 5 tools | `LITERAL` (all tools always exposed) — saves the semantic-search overhead |
| 6–20 tools | `SEMANTIC` recommended |
| > 20 tools | `SEMANTIC` required — with LITERAL, prompt tokens explode and intent confusion increases |

> Number of tools = (the sum of all MCP target tool counts).

## 3. Memory strategy selection

| Scenario | Recommended strategy |
|---|---|
| Follow-up questions only within the same session | (none, raw) — `event_expiry_days=7` |
| Learn user preferences across sessions (e.g., remember "reply in Korean") | `using_built_in_user_preference` |
| Want to automatically pull in semantically relevant parts of past conversations | `using_built_in_semantic` |
| The context grows too large in long conversations | `using_built_in_summarization` |
| Document search is sufficient (no user data) | Don't create Memory; use only the KB |

> Multiple strategies can be combined in one Memory. But since indexing costs add up, add only what you need.

## 4. Whether to add a Knowledge Base

| Answer | KB needed? |
|---|---|
| "Use static documents like workshop docs / FAQs / policy docs in answers" | ✅ Needed |
| "Just give me AWS public docs links" | ❌ A general LLM is sufficient (or the AWS Knowledge MCP) |
| "An internal wiki" | ✅ Web Crawler or S3 data source |
| "DB/CSV data analysis" | ❌ Use a Text2SQL agent, not a KB |
| "Find patterns in customer chat logs" | ✅ S3 data source + KB |

## 5. Model selection (Bedrock Claude)

The `shared/reference/aws-services.md` catalog + the following heuristics:

| Trade-off | Recommendation |
|---|---|
| Accuracy first (high cost of intent-classification failure, complex reasoning) | `us.anthropic.claude-opus-4-7` (1M ctx, adaptive thinking) |
| Balanced (the majority of scenarios, Orchestrator default) | `us.anthropic.claude-sonnet-4-20250514-v1:0` |
| Minimize cost (high call volume, simple intent classification) | `anthropic.claude-haiku-4-5-20251001` |
| Same model for sub-agents too? | Usually standardize on Sonnet 4. However, consider Opus where SQL-generation accuracy matters, as in Text2SQL |

> **Always re-confirm the latest model ID via AWS Knowledge MCP `aws___search_documentation`.** The catalog is a hint; MCP is the source of truth.

## 6. Cognito flow selection

| Call path | Flow |
|---|---|
| User → Frontend → Orchestrator Runtime | `USER_PASSWORD_AUTH` (the Amplify Authenticator handles it automatically) |
| Gateway → MCP target Runtime | `client_credentials` (M2M, OAuth2) |
| Orchestrator → Specialized Agent Runtime | `USER_PASSWORD_AUTH` (Bearer token, `CognitoTokenManager`) |
| Orchestrator → Gateway | (does not use Cognito — SigV4) |

> The frontend does **not** call the Gateway directly. All client traffic goes through the Orchestrator.

## 7. Region selection

| Priority | Region | Reason |
|---|---|---|
| 1 | `us-east-1` | AgentCore Runtime/Gateway/Memory reach GA first here. The Bedrock Claude 4 inference profile is stable. |
| 2 | `us-west-2` | Same availability (the USA inference profile applies) |
| 3 | `ap-northeast-2`, `eu-west-1` | Some AgentCore features need GA confirmation — **verify via MCP `aws___get_regional_availability`** |
| Avoid | Others | Higher chance that KB or AgentCore is unavailable |

Region decision table (example):

```
User: "Wants a Korea region, data sovereignty first"
→ 1. Check ap-northeast-2 availability via MCP
→ 2. AgentCore Runtime/Memory unavailable → recommend falling back to us-west-2, encrypt data with KMS
→ 3. User declines → run only part of the service in ap-northeast-2 (KB) + the rest cross-region in us-west-2
```

## 8. Splitting routing responsibility between Orchestrator and Frontend

| Decision | Location |
|---|---|
| Intent classification (Jira / GitHub / Data / KB) | The routing table in the Orchestrator system prompt |
| Tool selection (which of the individual jira tools?) | Gateway semantic search → Strands LLM tool selection |
| User → endpoint routing | Frontend (`fetch /chat` POST → Orchestrator endpoint) |
| Multi-tool synthesis ("sprint progress + commit activity") | Orchestrator (the MULTI item in the system prompt) |

**Principle**: The frontend does not route. All decision-making is delegated to the Orchestrator.

## 9. Deployment form (Hosting)

| Frontend hosting | Suitable scenario |
|---|---|
| AWS Amplify Hosting | The simplest. Cognito integration is automatic |
| S3 + CloudFront | When a custom domain / WAF is needed |
| Local dev only | `pnpm dev` (vite) — calls Cognito directly |

## 10. Decisions to add optional components

| Option | Condition to add |
|---|---|
| Sub-agent Memory (a separate instance) | The sub-agent needs to preserve its own user context (e.g., learning Text2SQL query history) |
| Bedrock Guardrails | Answer review / PII redaction needed |
| OTEL trace export to X-Ray | Production. `aws-opentelemetry-distro` is already included in every Dockerfile |
| Cross-region inference profile | Insufficient single-region quota (Bedrock TPM/RPM) |
| WAF on Frontend | When externally exposed |

## Output (the table produced in the Phase 2 Design stage)

```
| Component               | Selection                                     |
|------------------------|------------------------------------------------|
| Orchestrator agent     | Strands + Sonnet 4 + ShortTermMemory          |
| MCP servers            | jira-mcp (Gateway), github-mcp (Gateway)       |
| Specialized agents     | text2sql-agent (direct invoke)                |
| Gateway search type    | SEMANTIC (12 tools)                           |
| Memory strategy        | (raw) + 7 day expiry                          |
| Knowledge Base         | Yes — Web Crawler https://docs.example.com    |
| Region                 | us-east-1                                     |
| Frontend hosting       | Amplify Hosting                               |
| Estimated cost (mo)    | ~$XX (see aws-services.md)                    |
```
