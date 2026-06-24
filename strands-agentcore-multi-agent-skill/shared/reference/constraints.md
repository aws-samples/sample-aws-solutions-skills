# Constraints & Gotchas

> The **pitfalls you must avoid** when applying this skill. Focused on items where the CDK build/deploy failure or runtime-error pattern is hard to spot.

## 1. AgentCore Runtime — Linux ARM64 is mandatory

```python
agentcore.AgentRuntimeArtifact.from_asset(
    str(agent_path),
    platform=ecr_assets.Platform.LINUX_ARM64,  # ← x86 is absolutely not allowed
)
```

- Building on Apple Silicon naturally yields ARM64. On an Intel Mac/CI, docker buildx handles ARM64 cross-compilation automatically, but it fails if the Dockerfile's base image does not support ARM64.
- Prefer a multi-arch image such as `FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim`.
- Even for an Apple Silicon native build, it is recommended to specify the `--platform=linux/arm64/v8` flag — to avoid the pitfall of buildx following the host platform.

## 2. Reserved names similar to service-reserved keys like `_profileId`

AgentCore itself has no reserved keys, but **never override these in the container env**:

| Env var | Reason |
|---|---|
| `AWS_REGION`, `AWS_DEFAULT_REGION` | AgentCore Runtime injects these automatically. Setting them via CDK `environment_variables` may conflict |
| `BEDROCK_AGENTCORE_*` | Runtime-internal variables. Used by the SDK |

## 3. The Cognito client_secret cannot be exported via CloudFormation

- `UserPoolClient.user_pool_client_secret` is not an attribute in CDK (CloudFormation export is not allowed).
- **Call `cognito-idp:DescribeUserPoolClient` at deploy time via a Custom Resource** and store it in Secrets Manager.
- To pass the created secret's ARN to another stack, use SSM or CfnOutput.

## 4. The AgentCore Runtime endpoint URL can only be created at deploy time

- The ARN is known only after `agentcore.Runtime` is created (`runtime.agent_runtime_arn`).
- It cannot be placed directly in a URL path (`:` and `/` need encoding).
- Use the **Custom Resource pattern (Lambda)** to `urllib.parse.quote(arn, safe="")` and then compose `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<encoded>/invocations`.
- Export the composed URL via SSM and use it for the Gateway target endpoint.

## 5. `aws_cdk.aws_bedrock_agentcore_alpha` is in lockstep with the CDK core

```bash
pip install aws-cdk-lib==2.231.0
pip install aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0  # ← same prefix
```

- A major/minor mismatch causes a `Construct property type mismatch` error.
- When you upgrade the core, upgrade the alpha together. Specify both in `requirements.txt`.

## 6. IAM policies the Gateway L2 does not add automatically

```python
gateway = agentcore.Gateway(self, "Gateway", ...)

# ★ Add the following two statements directly — if omitted, invocation returns 401
gateway.role.add_to_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:*"], resources=["*"],
))
gateway.role.add_to_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity*"],
))
```

## 7. The OAuth2 credential provider can only be created via a Custom Resource

- CDK L2 has no `Oauth2CredentialProvider` class (not included in the alpha).
- Create it with `boto3.client("bedrock-agentcore-control").create_oauth2_credential_provider(...)`.
- If the same name already exists, you get a `ConflictException` → on Update, delete then re-create + `time.sleep(10)` (eventual consistency).
- The provider's IAM role needs the following actions:
  - `bedrock-agentcore:CreateOauth2CredentialProvider / DeleteOauth2CredentialProvider / GetOauth2CredentialProvider / CreateTokenVault`
  - `cognito-idp:DescribeUserPoolClient`
  - `secretsmanager:CreateSecret / DeleteSecret` on `bedrock-agentcore-identity*`

## 8. Gateway semantic search depends absolutely on tool description quality

- If a tool description is poor, semantic matching fails → the wrong tool is called or no tool is found.
- Write the docstring right after the `@mcp.tool()` decorator so that it includes **user query keywords**:
  ```python
  @mcp.tool()
  def search_issues(jql: str = "", limit: int = 50) -> dict:
      """
      Search Jira issues using JQL (Jira Query Language).
      Use this tool for queries like: "show me bugs", "list open tickets",
      "find issues assigned to John", "what tasks are in sprint 5".
      """
  ```

## 9. The Strands `MCPClient` is alive only inside a `with` block

```python
gateway_mcp_client = MCPClient(lambda: streamablehttp_client(gateway_url, auth=sigv4))

with gateway_mcp_client:                    # ← outside this block the connection is closed
    tools = gateway_mcp_client.list_tools_sync()
    agent = Agent(..., tools=tools)
    async for event in agent.stream_async(prompt):
        yield event
```

- All of the entry handler's streaming must finish inside the `with` block.
- If you are not familiar with the async generator + `with` combination, a connection error occurs.

## 10. `Agent.stream_async` yields dict events — watch the position of the `text` field

- Strands stream event structure:
  ```python
  {"event": {"contentBlockDelta": {"delta": {"text": "..."}}}}
  ```
- When the frontend parses SSE, it is one line with a `data: ` prefix + one JSON line. `\n\n` is the chunk boundary.
- When synthesizing a sub-agent call result, extract only the text from the structure above:
  ```python
  for line in response.text.split("\n"):
      if line.startswith("data: ") and "contentBlockDelta" in line:
          data = json.loads(line[6:])
          full_text += data["event"]["contentBlockDelta"]["delta"].get("text", "")
  ```

## 11. The `create_event` messages format for Memory

Correct:
```python
client.create_event(
    memory_id=..., actor_id=..., session_id=...,
    messages=[(content_text, role.upper())],   # ← a list of (text, ROLE) tuples
)
```

Common mistakes (fail):
```python
messages=[{"role": "user", "content": ...}]   # ← the dict form is not accepted
messages=[content_text]                        # ← role is missing
```

`role` is uppercase `"USER"` / `"ASSISTANT"` / `"SYSTEM"`.

## 12. The Cognito User Pool Domain prefix is globally unique

- Prefer `domain_prefix=f"{tool_name.replace('_', '-')}-{account}"` — using the account number as a suffix avoids collisions.
- `_` cannot be used in the prefix (a Cognito constraint).
- Changing the domain requires recreating the stack (no in-place update).

## 13. Knowledge Base — the Web Crawler `source_urls` is path-scoped

- If you seed `https://docs.example.com/foo`, it crawls paths within the `docs.example.com` domain.
- It does not follow external domains (safe).
- The first sync is triggered manually in the console, or via the SDK right after the CDK deploy (there is no auto-trigger option).

## 14. Anthropic model access — the first-time use-case form

- Before the first Anthropic model call in a new AWS account, you must fill out the use case via **Bedrock console → Model access → Anthropic Claude → "Request"**.
- Once approved in the root account of the same Organization, it is inherited by all child accounts.
- If not approved, you get `AccessDeniedException: AnthropicModelAccessNotApproved`.

## 15. Container build context — a missing `.dockerignore` bloats the image

- `.git`, `__pycache__`, `tests/`, `*.md`, `.venv/` must all be included in `.dockerignore`.
- If an incorrectly installed venv ends up in the image, the ARM64 cross-compile fails.
- Recommended `.dockerignore`:
  ```
  __pycache__/
  *.pyc
  .git/
  .venv/
  venv/
  tests/
  *.md
  .pytest_cache/
  .mypy_cache/
  ```

## 16. Both `bedrock-agentcore-starter-toolkit` and `bedrock-agentcore` are required

- `bedrock-agentcore` — the Runtime SDK (`BedrockAgentCoreApp`, `MemoryClient`).
- `bedrock-agentcore-starter-toolkit` — IAM/role automation helpers (for CDK environment setup).
- Specify both in `requirements.txt`. If omitted, you get `ModuleNotFoundError`.

## 17. The Strands `BedrockModel` accepts only an inference profile ID

Correct:
```python
BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0", client=bedrock_client)
```

Fails:
```python
BedrockModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0")  # foundation model ID
# → ValidationException: This model requires a cross-region inference profile
```

A model ID without a prefix (`us.`, `eu.`, `apac.`, `global.`) cannot be called directly (models that require cross-region).

## 18. SigV4 + httpx — removing the `Connection` header is required

```python
def auth_flow(self, request):
    headers = dict(request.headers)
    headers.pop("connection", None)   # ← without this, the SigV4 signature mismatches
    aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
    self.signer.add_auth(aws_request)
    request.headers.update(dict(aws_request.headers))
    yield request
```

## 19. CDK Stack dependency order

```
OrchestratorAgentCoreStack          ← first
   ↑ depends_on
JiraMcpAgentCoreStack
GitHubMcpAgentCoreStack
Text2SqlAgentStack
KnowledgeBaseStack                  (independent, OK anywhere)
   ↑ depends_on
AgentCoreGatewayStack               ← last
```

If the order is violated:
- The Gateway cannot find the OAuth provider ARN or endpoint URL and the deploy fails.
- `cdk deploy --all` auto-sorts, but **you must specify the order for individual deploys**.

## 20. Memory `list_memories()` allows duplicates with the same name

- Calling `create_memory_and_wait` with the same `memory_name` creates yet another new instance.
- **Always list first and reuse if the same name exists** (see `shared/patterns/memory-hooks.md` for the code).

## 21. AgentCore runtime container — image optimization for fast cold starts

- Use a multi-stage build or uv's `UV_COMPILE_BYTECODE=1` (pre-compile `.pyc`).
- Prefer `uv pip install` over `pip install` (10× faster).
- When changing the base image, confirm that ARM64 wheels exist for all dependencies.

## 22. Knowledge Base parameter store key convention

- Use a fixed path like `/workshop/knowledge_base/kb_id`. If each sub-stack uses a different prefix, the Orchestrator code needs per-environment branching.
- For multiple KBs, namespace it like `/workshop/knowledge_base/<kb_name>/kb_id`.

## 23. The Frontend `config.json` is a static file after the build

- Do not inject values like the Cognito User Pool ID / endpoint URL at build time. **Swap `public/config.json` per environment at deploy time**.
- After the build, separate dev/staging/prod using the same build artifact + a different config.json in the hosting environment.

## 24. AgentCore Memory is region-local

- The Memory ID is region-bound. A Runtime in another region cannot share the same Memory.
- For a multi-region deployment, you need a separate Memory per region + external sync keyed by user ID (e.g., DynamoDB Global Table).

## 25. Memory `actor_id` / `session_id` must be stable IDs — never use a random or timestamp fallback

**Symptom**: Memory saves fine, but long-term recall / user_preference / `get_last_k_turns` is always empty. CloudWatch cost keeps growing.

**Cause**: If the entry handler follows a pattern like the one below, a new actor / session is created on every invocation:

```python
# ❌ Wrong pattern (a pitfall in the reference workshop code)
customer_id = payload.get("customer_id", f"customer_{uuid.uuid4().hex[:8]}")
session_id  = payload.get("session_id",  f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}")
```

- If the frontend does not send `customer_id`, a new UUID is created on every invocation → if one user calls K times, K actors are created. This neutralizes the semantic recall of long-term memory.
- A timestamp-based `session_id` (second granularity) → if another user comes in during the same second, **there is a risk of sessions mixing** (cross-user data leak).

**Correct pattern**:

```python
# ✅ 1) The frontend explicitly sends the Cognito sub as customer_id
# ✅ 2) The backend falls back to the JWT sub, and loud-warns on a UUID fallback

def resolve_customer_id(payload: dict, context, logger) -> str:
    cid = payload.get("customer_id")
    if cid:
        return cid
    # Extract the sub claim from the JWT that AgentCore Runtime verified
    try:
        import base64, json as _json
        auth = None
        if hasattr(context, "headers"):
            auth = context.headers.get("authorization") or context.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            payload_b64 = auth[7:].split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            if claims.get("sub"):
                return f"cognito_{claims['sub']}"
    except Exception as e:
        logger.debug(f"JWT sub extraction failed: {e}")
    # ⚠️ Loud warning — Memory continuity disabled
    fallback = f"anon_{uuid.uuid4().hex[:8]}"
    logger.warning(
        f"⚠️ No stable customer_id (frontend must pass Cognito sub). Falling back to {fallback}. "
        f"Memory continuity DISABLED for this invocation."
    )
    return fallback


def resolve_session_id(payload: dict) -> str:
    # session_id must never be a timestamp — only a UUID fallback is allowed
    return payload.get("session_id") or f"session_{uuid.uuid4()}"
```

**The frontend must**:
```typescript
const session = await fetchAuthSession();
const userSub = session.tokens?.idToken?.payload.sub as string;
fetch(endpoint, {
  body: JSON.stringify({ prompt, customer_id: `cognito_${userSub}`, session_id: currentSessionId }),
});
```

**How to verify**: a Memory continuity smoke test
1. Call twice as the same user (`prompt: "My name is John Doe"`, `prompt: "What is my name?"`)
2. The second response must contain "John Doe". If not, it is evidence that the actor_id differs per invocation.

## Quick checklist (before code generation)

```
[ ] LINUX_ARM64 platform specified
[ ] GetWorkloadAccessToken* action on the Runtime IAM role
[ ] bedrock-agentcore:* + secretsmanager:GetSecretValue on the Gateway role
[ ] IAM for the OAuth provider Custom Resource
[ ] Endpoint URL Custom Resource (URL encode)
[ ] Memory list-then-create pattern
[ ] **Memory actor_id / session_id are Cognito sub + UUID — no random/timestamp fallback (#25)**
[ ] Strands MCPClient streams inside the `with` block
[ ] BedrockModel inference profile ID (us./eu./apac./global. prefix)
[ ] aws-cdk-lib and *-alpha at the same version
[ ] .dockerignore is sufficient
[ ] Bedrock Anthropic model access enabled
[ ] No _ in the Cognito domain prefix, with an account suffix
```
