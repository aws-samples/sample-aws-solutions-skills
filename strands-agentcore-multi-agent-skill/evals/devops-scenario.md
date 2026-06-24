# Eval: DevOps Assistant Scenario

## Input Prompt
```
Build me a multi-agent system integrating Jira and GitHub.
- Domain: software development team
- External: Atlassian Jira (project DEMO), GitHub (org awslabs)
- KB: internal dev playbook site https://playbook.example.com
- Memory: follow-up within the same session only (short-term)
- Region: us-east-1
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Skip already-answered items among the 13 questions and ask only the missing ones (e.g., number of concurrent users, model-selection trade-off)
- [ ] Present LLM model options: Sonnet 4 by default, Opus 4.7 for accuracy, Haiku 4.5 for cost
- [ ] Re-verify the model ID with AWS Knowledge MCP `aws___search_documentation`

### Phase 2 (Design — GATE 2)
- [ ] Stack composition table:
  ```
  | Stack                        | Selected? |
  | OrchestratorAgentCoreStack   | ✅        |
  | JiraMcpAgentCoreStack        | ✅        |
  | GitHubMcpAgentCoreStack      | ✅        |
  | KnowledgeBaseStack (Web Crawler https://playbook.example.com) | ✅ |
  | AgentCoreGatewayStack (semantic, 12 tools) | ✅ |
  ```
- [ ] Memory strategy = (raw, 7d expiry)
- [ ] Cost estimate ~$370/mo (KB is dominant)
- [ ] Confirm us-east-1 availability with AWS Knowledge MCP `aws___get_regional_availability(filters=["Amazon Bedrock AgentCore"])`

### Phase 3 (Generated Files)

**CDK Stacks**:
- [ ] `cdk-infra/app.py` — 5 stack instances + dependencies, exactly
- [ ] `cdk-infra/src/stacks/orchestrator_agent_stack.py` — all of the `_create_role`, `_create_memory`, `_create_cognito`, `_create_secret_update_resource`, `_create_runtime`, `_create_ssm_parameters` methods present
- [ ] `jira_mcp_stack.py` — includes `_create_oauth_provider`, `_create_endpoint_url` Custom Resource
- [ ] `github_mcp_stack.py` — same structure as Jira MCP, only the GITHUB_TOKEN env differs
- [ ] `knowledge_base_stack.py` — `cdklabs.generative_ai_cdk_constructs.bedrock.VectorKnowledgeBase` + `WebCrawlerDataSource`
- [ ] `agentcore_gateway_stack.py` — `McpProtocolConfiguration(search_type=SEMANTIC)` + `using_aws_iam` + `bedrock-agentcore:*` IAM added
- [ ] `context.orchestrator-agent-agentcore.tool-name = "orchestrator_agent"` in `cdk.json`

**Agent code**:
- [ ] `agents/orchestrator-agent/orchestrator_agent.py` — all of `BedrockAgentCoreApp`, Strands `Agent`, `ShortTermMemoryHooks`, `MCPClient(SigV4)`
- [ ] `agents/orchestrator-agent/Dockerfile` — `LINUX_ARM64`-compatible base + uv + `aws-opentelemetry-distro`
- [ ] `agents/orchestrator-agent/memory/short_term_memory.py` — `HookProvider`, reuse pattern after `list_memories`
- [ ] `agents/orchestrator-agent/common/sigv4_auth.py` — `httpx.Auth` SigV4 (connection header removed)
- [ ] `agents/orchestrator-agent/common/cognito_token_manager.py` — auto-detect M2M / user_password
- [ ] `agents/orchestrator-agent/common/prompts.py` — Jira/GitHub/Knowledge/Multi routing table

**MCP servers**:
- [ ] `mcp-servers/jira-mcp/jira_mcp.py` — `FastMCP(host="0.0.0.0", stateless_http=True)`, 6+ `@mcp.tool` (list_projects, search_issues, get_issue, create_issue, transition_issue, add_comment)
- [ ] Each tool docstring includes "Use for queries like:" (semantic-search friendly)
- [ ] `mcp-servers/github-mcp/github_mcp.py` — same structure, calls the GitHub REST API
- [ ] `mcp.run(transport="streamable-http")` (stdio NOT)

**Frontend**:
- [ ] `frontend/package.json` — `aws-amplify`, `@aws-amplify/ui-react`, `react-markdown`, `tailwindcss@3`, `shadcn` deps
- [ ] `frontend/src/App.tsx` — Amplify Authenticator + config.json fetch
- [ ] `frontend/src/pages/chat.tsx` — SSE streaming parsing (contentBlockDelta)
- [ ] `frontend/src/api/orchestrator.ts` — Bearer token + SSE chunk reader
- [ ] `frontend/public/config.json` — placeholder (replaced by generate-frontend-config.sh after deploy)
- [ ] Cloudscape ❌ NOT used

**Scripts**:
- [ ] `scripts/deploy.sh` — deploy stacks in order (Orchestrator → MCPs → KB → Gateway)
- [ ] `scripts/destroy.sh` — reverse-order destroy
- [ ] `scripts/check-prerequisites.sh` — Python 3.13, Node 20+, Docker, CDK 2.231+
- [ ] `scripts/generate-frontend-config.sh` — CFN outputs → `frontend/public/config.json`

**Configuration**:
- [ ] `cdk-infra/.env.example` — JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, GITHUB_TOKEN, GITHUB_USERNAME, COGNITO_TEST_USERNAME, COGNITO_TEST_PASSWORD
- [ ] `cdk-infra/requirements.txt` — `aws-cdk-lib==2.231.0` + `aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0` (lockstep)

### Phase 4 (Validate — GATE 3)
- [ ] `cdk synth` passes
- [ ] CDK Nag suppressions specified (IAM5/IAM4/COG1, etc.)
- [ ] Use AWS Knowledge MCP to confirm the IAM actions used (`bedrock-agentcore:*`) are actually valid actions

### Phase 5 (Deploy)
- [ ] Clear deploy-order guidance
- [ ] Guidance on the procedure to enable Anthropic Claude model access
- [ ] Guidance on creating a Cognito user and updating the frontend config.json
- [ ] Smoke test command:
  ```bash
  aws bedrock-agent-runtime invoke-agent ...
  ```

## Code Quality Checks
- [ ] LINUX_ARM64 platform specified (all Runtimes)
- [ ] `us.` prefix on the Bedrock model ID
- [ ] `bedrock-agentcore:*` + `secretsmanager:GetSecretValue on bedrock-agentcore-identity*` on the Gateway role
- [ ] OAuth credential provider Custom Resource (Jira + GitHub respectively)
- [ ] Endpoint URL Custom Resource (URL encode)
- [ ] Reuse pattern after `list_memories`
- [ ] Strands `MCPClient` used only inside a `with` block
- [ ] `.dockerignore` present in every Docker context
- [ ] No Cloudscape — shadcn/ui only

## Tool Selection Verification (Gateway semantic search)
- [ ] "Show me bugs" → matches search_issues (jql auto-generated)
- [ ] "List PRs" → matches get_pull_requests
- [ ] "Recent commits" → matches get_recent_commits
- [ ] "How is the team doing this sprint?" → multi-tool sequence (search_issues + get_repository_statistics)

## Memory Continuity Verification
- [ ] `orchestrator_agent.py` uses the `resolve_customer_id(payload, context, logger)` helper — order `payload["customer_id"]` → JWT sub → loud-warn UUID
- [ ] `resolve_session_id(payload)` returns only **`uuid.uuid4()`** when `payload["session_id"]` is absent (timestamp-based fallback ❌)
- [ ] Frontend `api/orchestrator.ts` extracts `idToken.payload.sub` and explicitly sends `customer_id: \`cognito_${userSub}\``
- [ ] `sessionId` in frontend `app-store.ts` is based on `crypto.randomUUID()` (Date.now ❌)
- [ ] First response: "John Doe is the top PR contributor"
- [ ] Second query: "What about his commit count?" → auto-resolves to John Doe's commits (history is visible only with the same actor + same session)
- [ ] Third query (new session_id issued): even with the same customer_id, a different session_id means a separate context
- [ ] **Smoke test**: two invocations as the same user — 1st "내 이름은 김도현" / 2nd "내 이름이 뭐야?" → confirm the 2nd responds with "김도현"
