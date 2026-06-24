# AgentCore Primitives — Deep Dive

> A code-generation and operations-oriented summary of the 4 primitives of Amazon Bedrock AgentCore (Runtime / Gateway / Memory / Identity). **You cannot write the CDK stacks without reading this document first.**

## 1. AgentCore Runtime

### What it is

- A container (Docker) based **fully-managed agent runtime**.
- Different from ECS: you do not create a cluster/task definition. You push an image to ECR and declare a `Runtime` resource, and AgentCore handles hosting, scaling, and cold-start avoidance.
- The endpoint is an HTTPS POST of the form `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<URL-encoded ARN>/invocations?qualifier=DEFAULT`.

### Container contract

Conditions the image must satisfy:
1. **Linux ARM64** (`platform=ecr_assets.Platform.LINUX_ARM64`). This matches an Apple Silicon build, but the build will fail if you use a base image that supports only x86.
2. It must export a `bedrock_agentcore.BedrockAgentCoreApp` instance and have an async generator or callable decorated with `@app.entrypoint`.
3. Port exposure:
   - `8080` — invocations endpoint (required)
   - `8000` — MCP `streamable-http` transport (only for MCP server Runtimes)
4. Non-root user recommended: `useradd -m -u 1000 bedrock_agentcore && USER bedrock_agentcore`.
5. OpenTelemetry instrumentation recommended: `RUN uv pip install aws-opentelemetry-distro>=0.10.1` + `CMD ["opentelemetry-instrument", "python", "-m", "<module>"]`.

### Protocol type

| `protocol_configuration` | Use |
|---|---|
| `ProtocolType.HTTP` | Regular Strands agent (the entry handler yields an SSE stream) |
| `ProtocolType.MCP` | FastMCP server (`mcp.run(transport="streamable-http")`) |

> An MCP server must be registered with the `MCP` protocol for the Gateway target to work correctly.

### Authorizer

- `using_jwt(discovery_url, allowed_audiences)` — the Cognito User Pool's OIDC discovery URL + a list of client IDs.
- Other IdPs are supported via the same interface (Auth0, Okta, etc.). Only the discovery URL differs.
- No additional options — fine-grained ACL is handled within the agent code.

### Memory primitive (a separate object)

```python
agentcore.Memory(
    self, "Memory",
    memory_name=f"{self.tool_name}_memory",
    expiration_duration=Duration.days(90),
    memory_strategies=[
        agentcore.MemoryStrategy.using_built_in_semantic(),     # semantic search
        agentcore.MemoryStrategy.using_built_in_user_preference(), # preferences
    ],
)
```

Strategy comparison:

| Strategy | Behavior | When to use |
|---|---|---|
| (none, raw) | Stores events as-is. Only `get_last_k_turns` is possible | Short conversational context — short-term |
| `using_built_in_semantic` | Embeds events for semantic recall | Long-term user history, RAG-like |
| `using_built_in_user_preference` | Extracts/accumulates "preferred items" | Personalized responses |
| `using_built_in_summarization` | Auto-summarizes events | Long-term retention while reducing context-window burden |

Using `MemoryClient`:
```python
from bedrock_agentcore.memory import MemoryClient
client = MemoryClient(region_name=AWS_REGION)
client.create_memory_and_wait(name=..., strategies=[], event_expiry_days=7)
client.create_event(memory_id, actor_id, session_id, messages=[(text, role.upper())])
turns = client.get_last_k_turns(memory_id, actor_id, session_id, k=10)
```

### Workload identity (key points)

- The Runtime IAM role's inline policy must allow the following:
  - `bedrock-agentcore:GetWorkloadAccessToken*`
  - resources: `arn:aws:bedrock-agentcore:<region>:<account>:workload-identity-directory/default*`
- If omitted, the entry handler fails with `AccessDeniedException` — a pattern that is hard to spot in the logs.

## 2. AgentCore Gateway

### What it is

- An **MCP fan-out** that bundles multiple MCP server (or Lambda, OpenAPI) targets into a single streamable-http endpoint.
- Core value: **semantic search** — exposes to the LLM only the tools with high embedding similarity to the user's query.

### CDK definition

```python
gateway = agentcore.Gateway(
    self, "Gateway",
    gateway_name="workshop-gateway",
    protocol_configuration=agentcore.McpProtocolConfiguration(
        search_type=agentcore.McpGatewaySearchType.SEMANTIC,
    ),
    authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
)
```

- `search_type=SEMANTIC` — automatically selects the top-k tools. With `LITERAL`, every tool is always exposed, causing a token explosion.
- `using_aws_iam()` — the Orchestrator calls via SigV4. The simplest option.
- `using_jwt(...)` — for external IdP integration. Usually unnecessary.

### Adding a target (per-MCP)

```python
gateway.add_mcp_server_target(
    "JiraMcpTarget",
    gateway_target_name="jira-target",
    endpoint=jira_mcp_stack.runtime_endpoint_url,
    credential_provider_configurations=[
        agentcore.GatewayCredentialProvider.from_oauth_identity_arn(
            provider_arn=jira_mcp_stack.oauth_provider_arn,
            secret_arn=jira_mcp_stack.oauth_secret_arn,
            scopes=[f"{jira_mcp_stack.tool_name}-api/invoke"],
        )
    ],
)
```

### OAuth2 credential provider

CDK L2 has no direct API for this, so a **Custom Resource calling Boto3** is required:

```python
control = boto3.client("bedrock-agentcore-control")
control.create_oauth2_credential_provider(
    name=name,
    credentialProviderVendor="CustomOauth2",
    oauth2ProviderConfigInput={
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": ...},
            "clientId": ..., "clientSecret": ...,
        }
    },
)
```

This Custom Resource's IAM role needs the following actions:
- `bedrock-agentcore:CreateOauth2CredentialProvider`
- `bedrock-agentcore:DeleteOauth2CredentialProvider`
- `bedrock-agentcore:GetOauth2CredentialProvider`
- `bedrock-agentcore:CreateTokenVault`
- `cognito-idp:DescribeUserPoolClient` (to read the client_secret)
- `secretsmanager:CreateSecret`, `DeleteSecret` on `bedrock-agentcore-identity*`

### Permission pitfall

Policies the CDK L2 `Gateway` does **not** add automatically:

```python
gateway.role.add_to_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:*"], resources=["*"],
))
gateway.role.add_to_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity*"],
))
```

If omitted, a gateway tool call returns 401 at the invocation step.

### Endpoint URL encoding

A Runtime ARN contains `:` and `/`, so it cannot be placed directly in a URL path. **Since the ARN is unknown at CDK synth time, URL-encode it at deploy time with a Custom Resource**:

```python
encoded_arn = urllib.parse.quote(runtime_arn, safe="")
endpoint_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations"
```

This endpoint URL goes into the Gateway target's `endpoint`.

## 3. AgentCore Memory

(See the Memory primitive in the Runtime section above.)

Additional operational tips:
- `actor_id` is per-user (e.g., Cognito sub, customer_id). `session_id` is per-conversation.
- The shorter `event_expiry_days` is, the lower the Memory storage cost.
- If a Memory with the same name already exists, `create_memory_and_wait` causes a conflict → in code, prefer the pattern of first looking it up with `list_memories()` and creating only if it does not exist (see `shared/patterns/memory-hooks.md` for an example).

## 4. AgentCore Identity (workload identity directory)

- AgentCore's internal token-issuing system. The default directory is used automatically even if you do not create one yourself.
- The IAM policy must allow the resource `arn:aws:bedrock-agentcore:<region>:<account>:workload-identity-directory/default*`.
- `GetWorkloadAccessTokenForJWT` — the workload token issued after passing the JWT authorizer.
- `GetWorkloadAccessTokenForUserId` — a token for a specific user context.

## CDK alpha module

```text
pip install aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0
```

- In lockstep with this CDK version `2.231.0`. When you upgrade the core, bump the alpha together.
- Package import: `from aws_cdk import aws_bedrock_agentcore_alpha as agentcore`
- Provided classes (current stable surface):
  - `Runtime`, `AgentRuntimeArtifact`, `ProtocolType`, `RuntimeAuthorizerConfiguration`
  - `Gateway`, `GatewayAuthorizer`, `McpProtocolConfiguration`, `McpGatewaySearchType`, `GatewayCredentialProvider`
  - `Memory`, `MemoryStrategy`
- Meaning of the alpha label: names/props may change. Always check the release notes before a major upgrade.

## Local development tips

| Scenario | Method |
|---|---|
| Run a Strands agent locally | `python orchestrator_agent.py` — `BedrockAgentCoreApp` listens on 8080. AWS creds are inherited from the host. |
| MCP server locally | `python jira_mcp.py` — port 8000 (FastMCP default). Check the tool list with `mcp inspector`. |
| Test a direct MCP connection without the Gateway | `MCPClient(lambda: streamablehttp_client(local_url))` |
| Verify Memory behavior | Instantiate the hook standalone and fire mock events, as in `tests/test_short_term_memory.py` |

## Invocation flow (Orchestrator perspective)

```
@app.entrypoint
async def agent_invocation(payload, context):
    # 1) Look up the Gateway URL (SSM)
    gateway_url = ssm.get_parameter("/agentcore_gateway/gateway_url")

    # 2) Initialize Memory (only once)
    memory_id, memory_client = create_orchestrator_short_term_memory(...)

    # 3) MCPClient(SigV4) → Gateway streamable-http
    sigv4 = get_sigv4_auth(region=AWS_REGION)
    gateway_mcp_client = MCPClient(lambda: streamablehttp_client(gateway_url, auth=sigv4))

    with gateway_mcp_client:
        gateway_tools = gateway_mcp_client.list_tools_sync()
        # 4) Combine local tools (sub-agent invoke / KB retrieve)
        tools = gateway_tools + [query_data, answer_general_questions]

        # 5) Strands Agent + Memory hooks
        agent = Agent(
            model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0"),
            system_prompt=get_orchestrator_system_prompt(tool_descriptions),
            tools=tools,
            hooks=[ShortTermMemoryHooks(...)],
        )

        # 6) Streaming response
        async for event in agent.stream_async(prompt):
            yield event
```

Full code: `shared/patterns/strands-agents.md`.
