# Auth Patterns

> The **3 authentication flows** that appear in this skill:
> 1. **Cognito JWT** (USER_PASSWORD_AUTH) — Frontend → Orchestrator Runtime, Orchestrator → Specialized Strands Agent
> 2. **Cognito OAuth2 client_credentials** (M2M) — Gateway → MCP target Runtime
> 3. **AWS SigV4** — Orchestrator → AgentCore Gateway

This document summarizes the code/CDK patterns that make up each flow.

## 1. Cognito JWT (user → Runtime)

### Where it's used
- Frontend user login → Orchestrator Runtime invocation
- Orchestrator → Specialized Strands Agent invocation (Bearer token)

### CDK (User Pool with USER_PASSWORD_AUTH)

```python
user_pool = cognito.UserPool(
    self, "UserPool",
    user_pool_name=f"{self.tool_name}.Pool",
    self_sign_up_enabled=False,
    password_policy=cognito.PasswordPolicy(min_length=8),
    removal_policy=RemovalPolicy.DESTROY,
)

client = user_pool.add_client(
    "Client",
    auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
    generate_secret=False,                          # ← user-facing clients need no secret
)
```

### Auto-register a test user (Custom Resource)

```python
update_secret_resource = CustomResource(
    self, "UpdateSecret",
    service_token=update_secret_provider.service_token,
    properties={
        "UserPoolId": user_pool.user_pool_id,
        "Username": "testuser",
        "Password": os.getenv("COGNITO_TEST_PASSWORD", "MyPassword123!"),
        "ClientId": client.user_pool_client_id,
        "DiscoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{user_pool.user_pool_id}/.well-known/openid-configuration",
        "SecretName": f"{tool_name}/cognito/credentials",
    },
)
```

→ Make the password permanent with `cognito-idp:AdminSetUserPassword` + store the dict in Secrets Manager.

### Runtime authorizer

```python
authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
    f"https://cognito-idp.{region}.amazonaws.com/{user_pool.user_pool_id}/.well-known/openid-configuration",
    [client.user_pool_client_id],                   # ← allowed audiences
)
```

### Issuing tokens (Python — when calling a sub-agent)

```python
# common/cognito_token_manager.py
import base64, boto3, json, requests
from botocore.exceptions import ClientError

class CognitoTokenManager:
    def __init__(self, secret_name: str = "orchestrator_agent/cognito/credentials"):
        self.secret_name = secret_name
        self.secrets_client = boto3.client("secretsmanager")
        self._cached = None

    def _get_credentials(self) -> dict:
        if not self._cached:
            v = self.secrets_client.get_secret_value(SecretId=self.secret_name)
            self._cached = json.loads(v["SecretString"])
        return self._cached

    def refresh_bearer_token(self) -> str:
        creds = self._get_credentials()
        # Auto-detect flow
        if creds.get("client_secret") and creds.get("token_url"):
            return self._refresh_client_credentials(creds)
        return self._refresh_user_password(creds)

    def _refresh_user_password(self, creds: dict) -> str:
        cognito = boto3.client("cognito-idp")
        resp = cognito.initiate_auth(
            ClientId=creds["client_id"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": creds["username"], "PASSWORD": creds["password"]},
        )
        return resp["AuthenticationResult"]["AccessToken"]

    def _refresh_client_credentials(self, creds: dict) -> str:
        auth = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
        r = requests.post(
            creds["token_url"],
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": creds.get("scope", "")},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def get_fresh_token(self) -> str:
        return self.refresh_bearer_token()
```

### Usage example (calling a sub-agent)

```python
token_manager = CognitoTokenManager(secret_name="text2sql_agent/cognito/credentials")
bearer = token_manager.get_fresh_token()

response = requests.post(
    agent_url,
    headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    json={"prompt": question, "user_id": user_id},
    timeout=180,
)
```

---

## 2. Cognito OAuth2 client_credentials (Gateway → MCP)

### Where it's used
- When the Gateway forwards a request to an MCP target, an OAuth2 token is auto-issued/cached.

### CDK (User Pool with client_credentials)

```python
user_pool = cognito.UserPool(self, "UserPool", ...)

resource_server = user_pool.add_resource_server(
    "ResourceServer",
    identifier=f"{tool_name}-api",                          # ← scope prefix
    scopes=[cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP")],
)

client = user_pool.add_client(
    "Client",
    generate_secret=True,                                   # ★ M2M needs a client_secret
    o_auth=cognito.OAuthSettings(
        flows=cognito.OAuthFlows(client_credentials=True),
        scopes=[cognito.OAuthScope.resource_server(
            resource_server,
            cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP"),
        )],
    ),
)

# An OAuth2 token endpoint is needed → User Pool Domain
domain_prefix = f"{tool_name.replace('_', '-')}-{account}"
user_pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix))
# → token URL: https://<domain_prefix>.auth.<region>.amazoncognito.com/oauth2/token
```

### Store the client_secret in Secrets Manager via a Custom Resource

(the client_secret cannot be exported via CloudFormation)

```python
fn = lambda_.Function(
    self, "StoreCredsFn",
    runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler",
    code=lambda_.Code.from_inline('''
import boto3, json
def handler(event, context):
    p = event["ResourceProperties"]
    cognito = boto3.client("cognito-idp")
    secrets = boto3.client("secretsmanager")
    secret_name = f"{p['ToolName']}/cognito/credentials"
    if event["RequestType"] in ["Create", "Update"]:
        resp = cognito.describe_user_pool_client(UserPoolId=p["UserPoolId"], ClientId=p["ClientId"])
        secret = resp["UserPoolClient"].get("ClientSecret", "")
        sv = json.dumps({
            "client_id": p["ClientId"], "client_secret": secret,
            "token_url": p["TokenUrl"], "scope": p["Scope"],
            "discovery_url": p["DiscoveryUrl"],
        })
        try: secrets.update_secret(SecretId=secret_name, SecretString=sv)
        except secrets.exceptions.ResourceNotFoundException: secrets.create_secret(Name=secret_name, SecretString=sv)
        return {"PhysicalResourceId": secret_name}
    return {"PhysicalResourceId": event.get("PhysicalResourceId", secret_name)}
'''),
    role=role,
)
```

### OAuth2 credential provider (on the AgentCore side)

Register a credential provider the Gateway will use when calling the MCP target (via a Custom Resource calling boto3 — see `shared/patterns/cdk-stacks.md` Pattern 2).

The gist:
```python
control = boto3.client("bedrock-agentcore-control")
control.create_oauth2_credential_provider(
    name=name,
    credentialProviderVendor="CustomOauth2",
    oauth2ProviderConfigInput={
        "customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": discovery_url},
            "clientId": client_id,
            "clientSecret": client_secret,
        }
    },
)
```

### Connect the provider to the Gateway target

```python
gateway.add_mcp_server_target(
    "JiraMcpTarget",
    endpoint=mcp_stack.runtime_endpoint_url,
    credential_provider_configurations=[
        agentcore.GatewayCredentialProvider.from_oauth_identity_arn(
            provider_arn=mcp_stack.oauth_provider_arn,
            secret_arn=mcp_stack.oauth_secret_arn,
            scopes=[f"{mcp_stack.tool_name}-api/invoke"],   # ← resource_server identifier + scope
        )
    ],
)
```

---

## 3. AWS SigV4 (Orchestrator → Gateway)

### Where it's used
- When the Orchestrator Runtime calls the Gateway streamable-http endpoint.
- When the Gateway authorizer is `using_aws_iam()`.

### Gateway side (CDK)

```python
gateway = agentcore.Gateway(
    self, "Gateway",
    authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),    # ← IAM auth
)

# Add InvokeGateway permission to the Orchestrator role (in the Orchestrator stack)
iam.PolicyStatement(
    sid="GatewayInvoke",
    actions=["bedrock-agentcore:InvokeGateway"],
    resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"],
)
```

### httpx Auth class (Orchestrator side)

```python
# common/sigv4_auth.py
import boto3
import httpx
from typing import Generator, Optional
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


class SigV4HTTPXAuth(httpx.Auth):
    """httpx Auth that signs requests with AWS SigV4."""

    def __init__(self, service: str = "bedrock-agentcore", region: Optional[str] = None):
        session = boto3.Session()
        creds = session.get_credentials()
        if not creds:
            raise ValueError("No AWS credentials found")
        if not region:
            region = session.region_name or "us-east-1"
        self.signer = SigV4Auth(creds, service, region)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        headers = dict(request.headers)
        headers.pop("connection", None)         # ★ exclude the 'connection' header from SigV4 signing
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=headers,
        )
        self.signer.add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def get_sigv4_auth(region: str = "us-east-1") -> SigV4HTTPXAuth:
    return SigV4HTTPXAuth(service="bedrock-agentcore", region=region)
```

### Usage (Orchestrator entry handler)

```python
from common.sigv4_auth import get_sigv4_auth
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

sigv4 = get_sigv4_auth(region=AWS_REGION)
gateway_mcp_client = MCPClient(lambda: streamablehttp_client(gateway_url, auth=sigv4))

with gateway_mcp_client:
    tools = gateway_mcp_client.list_tools_sync()
    # ... use tools
```

---

## 4. Frontend Auth (Amplify)

### `frontend/public/config.json` (injected after deploy)

```json
{
  "cognito": {
    "userPoolId": "us-east-1_XXXXXXXXX",
    "clientId": "XXXXXXXXXXXXXXXXXXXXXXXXXX"
  },
  "endpoints": {
    "orchestrator": "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<encoded-arn>/invocations"
  }
}
```

### App.tsx (Amplify Authenticator)

```tsx
import { Amplify } from "aws-amplify";
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";

useEffect(() => {
  fetch("/config.json").then(r => r.json()).then(cfg => {
    Amplify.configure({
      Auth: {
        Cognito: {
          userPoolId: cfg.cognito.userPoolId,
          userPoolClientId: cfg.cognito.clientId,
        },
      },
    });
    setConfig(cfg);
    setLoaded(true);
  });
}, []);

return (
  <Authenticator loginMechanisms={["username"]}>
    <ChatPage />
  </Authenticator>
);
```

### API call (token auto-attached)

```tsx
import { fetchAuthSession } from "aws-amplify/auth";

async function postToOrchestrator(prompt: string, sessionId: string) {
  const session = await fetchAuthSession();
  const token = session.tokens?.accessToken?.toString();
  const config = useAppStore.getState().config;

  const res = await fetch(config.endpoints.orchestrator + "?qualifier=DEFAULT", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ prompt, customer_id: getUserId(), session_id: sessionId }),
  });

  // ── SSE streaming
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      handleSSEChunk(chunk);
    }
  }
}
```

---

## 5. IAM policy summary — what goes where

### Orchestrator Runtime IAM role

| Statement | Reason |
|---|---|
| `bedrock:InvokeModel*` on `foundation-model/*` + `inference-profile/*` | Strands `BedrockModel` calls |
| `bedrock-agentcore:GetWorkloadAccessToken*` | Runtime-internal token |
| `bedrock-agentcore:InvokeGateway` | Gateway calls |
| `bedrock-agentcore:Memory*` (Create/Get/Update/CreateEvent/...) | Memory hooks |
| `bedrock:Retrieve` on `knowledge-base/*` | KB tool |
| `secretsmanager:GetSecretValue` (sub-agent secret) | sub-agent Bearer token |
| `ssm:GetParameter*` | gateway URL, sub-agent ARN |

### MCP Runtime IAM role

| Statement | Reason |
|---|---|
| `bedrock:InvokeModel*` (optional) | When calling the LLM inside an MCP tool |
| `bedrock-agentcore:GetWorkloadAccessToken*` | Runtime-internal |
| `ssm:GetParameter*`, `secretsmanager:GetSecretValue` | External API tokens |
| (per-domain) External API calls like Jira/GitHub — no IAM impact for outbound network access | |

### Gateway IAM role (★ not added automatically)

| Statement | Reason |
|---|---|
| `bedrock-agentcore:*` | tool calls |
| `secretsmanager:GetSecretValue` on `bedrock-agentcore-identity*` | OAuth provider secret |

### Sub-agent Runtime IAM role

| Statement | Reason |
|---|---|
| `bedrock:InvokeModel*` | LLM reasoning |
| `bedrock-agentcore:GetWorkloadAccessToken*` | Runtime-internal |
| (per-domain) Data access like Athena/Glue/S3/RDS |  |

---

## 6. Authentication flow diagram (summary)

```
[Frontend (browser)]
     │  Amplify Authenticator
     │  USER_PASSWORD_AUTH → access_token
     │
     ▼  Bearer
[Orchestrator Runtime] ────── SigV4 ─────► [Gateway]
     │                                          │
     │                                          │ OAuth2 client_credentials (auto by Gateway)
     │                                          │ via OAuth2 credential provider
     │                                          ▼
     │  Bearer token (via                  [MCP target Runtime]
     │   CognitoTokenManager)
     │
     ▼
[Specialized Strands Agent Runtime]
```

Auth comparison for each arrow:

| Arrow | Auth |
|---|---|
| Frontend → Orchestrator | Cognito JWT (USER_PASSWORD_AUTH) |
| Orchestrator → Gateway | AWS SigV4 |
| Gateway → MCP target | OAuth2 client_credentials (the provider handles it automatically) |
| Orchestrator → Specialized agent | Cognito JWT (USER_PASSWORD_AUTH, Bearer) |
| Anyone → KB / Memory | IAM (the Runtime role's inline policy) |
