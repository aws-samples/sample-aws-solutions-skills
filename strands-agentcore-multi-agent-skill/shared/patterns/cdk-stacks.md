# CDK Stack Patterns

> **Python CDK** stack patterns that wire together AgentCore Runtime / Gateway / Memory / KB / other services. Every stack is based on `aws-cdk-lib==2.231.0` + `aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0`.

## File layout (CDK)

```
cdk-infra/
├── app.py                              ← Stack instances + dependency
├── cdk.json                            ← context (tool-name, model-id, etc.)
├── requirements.txt
├── requirements-dev.txt
├── .env.example                        ← external API token placeholders
└── src/
    ├── __init__.py
    └── stacks/
        ├── __init__.py
        ├── orchestrator_agent_stack.py
        ├── <domain>_mcp_stack.py
        ├── <domain>_agent_stack.py     (optional)
        ├── knowledge_base_stack.py
        └── agentcore_gateway_stack.py
```

## `app.py`

```python
#!/usr/bin/env python3
import os
from pathlib import Path

import aws_cdk as cdk
from dotenv import load_dotenv

from src.stacks.agentcore_gateway_stack import AgentCoreGatewayStack
from src.stacks.knowledge_base_stack import KnowledgeBaseStack
from src.stacks.orchestrator_agent_stack import OrchestratorAgentCoreStack

# ── Domain-specific stacks (enable when generated)
from src.stacks.jira_mcp_stack import JiraMcpAgentCoreStack
from src.stacks.github_mcp_stack import GitHubMcpAgentCoreStack
from src.stacks.text2sql_agent_stack import Text2SqlAgentStack

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# 1) Orchestrator (always first)
orchestrator = OrchestratorAgentCoreStack(
    app, "MultiAgentOrchestrator",
    description="Orchestrator Agent (Strands + AgentCore Runtime)",
    env=env,
)

# 2) MCP servers
jira = JiraMcpAgentCoreStack(app, "JiraMcp", description="Jira MCP Server", env=env)
github = GitHubMcpAgentCoreStack(app, "GitHubMcp", description="GitHub MCP Server", env=env)

# 3) Specialized agents (direct invoke)
text2sql = Text2SqlAgentStack(app, "Text2SqlAgent", description="Text2SQL Strands Agent", env=env)

# 4) Knowledge Base (independent)
kb = KnowledgeBaseStack(app, "WorkshopKnowledgeBase", description="Bedrock KB with web crawler", env=env)

# 5) Gateway (last — depends on all MCP stacks)
gateway = AgentCoreGatewayStack(
    app, "AgentCoreGateway",
    description="AgentCore Gateway (semantic search MCP fan-out)",
    mcp_stacks={"jira": jira, "github": github},
    env=env,
)

# ── Dependencies
jira.add_dependency(orchestrator)
github.add_dependency(orchestrator)
text2sql.add_dependency(orchestrator)
gateway.add_dependency(jira)
gateway.add_dependency(github)

app.synth()
```

## `cdk.json` (context)

```json
{
  "app": "python3 app.py",
  "watch": {
    "include": ["**"],
    "exclude": ["README.md", "cdk*.json", "requirements*.txt", "tests"]
  },
  "context": {
    "webCrawlerUrl": "https://docs.example.com",
    "orchestrator-agent-agentcore": {
      "tool-name": "orchestrator_agent",
      "model-id": "us.anthropic.claude-sonnet-4-20250514-v1:0"
    },
    "jira-mcp-agentcore":   { "tool-name": "jira_mcp" },
    "github-mcp-agentcore": { "tool-name": "github_mcp" },
    "text2sql-agent":       { "tool-name": "text2sql_agent",
                              "model-id":  "us.anthropic.claude-sonnet-4-20250514-v1:0" },

    "@aws-cdk/aws-iam:minimizePolicies": true,
    "@aws-cdk/core:checkSecretUsage": true,
    "@aws-cdk/customresources:installLatestAwsSdkDefault": false
  }
}
```

## `requirements.txt`

```
aws-cdk-lib==2.231.0
constructs>=10.0.0,<11.0.0
aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0
cdk_nag
aws_cdk.aws_lambda_python_alpha
bedrock-agentcore-starter-toolkit
cdklabs.generative-ai-cdk-constructs
strands-agents
strands-agents-tools
python-dotenv
```

---

## Pattern 1: Orchestrator stack — `OrchestratorAgentCoreStack`

Core responsibilities:
- AgentCore Runtime + Memory + Cognito User Pool (user authentication)
- Auto-register a Cognito test user + secret (Custom Resource)
- Expose the Runtime ARN / Memory ID via SSM parameters

```python
from pathlib import Path
import os

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_cognito as cognito,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_ssm as ssm,
    custom_resources as cr,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class OrchestratorAgentCoreStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context("orchestrator-agent-agentcore") or {}
        self.tool_name = ctx.get("tool-name", "orchestrator_agent")
        self.model_id = ctx.get("model-id", "us.anthropic.claude-sonnet-4-20250514-v1:0")

        test_username = os.getenv("COGNITO_TEST_USERNAME", "testuser")
        test_password = os.getenv("COGNITO_TEST_PASSWORD", "MyPassword123!")

        self.role = self._create_role()
        self.log_group = self._create_log_group()
        self.memory = self._create_memory()
        self.user_pool, self.client, self.user = self._create_cognito(test_username)
        self._create_secret_update_resource(test_username, test_password)
        self.runtime = self._create_runtime()
        self._create_ssm_parameters()
        self._apply_cdk_nag_suppressions()
        self._create_outputs()

    # ── IAM role
    def _create_role(self) -> iam.Role:
        role = iam.Role(
            self, "Role",
            role_name=f"{self.region}-agentcore-{self.tool_name}-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        self.policy = iam.Policy(self, "Policy", policy_name="AgentCorePolicy", statements=[
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*"],
            ),
            iam.PolicyStatement(
                sid="MarketplaceModelAccess",
                actions=["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="ECRPull",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="LogsCreate",
                actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"],
            ),
            iam.PolicyStatement(
                sid="LogsWrite",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"],
            ),
            iam.PolicyStatement(
                sid="XRay",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
                resources=["*"],
            ),
            iam.PolicyStatement(
                sid="CWMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            ),
            iam.PolicyStatement(
                sid="WorkloadAccessToken",
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/{self.tool_name}-*",
                ],
            ),
            iam.PolicyStatement(sid="SSMRead", actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:DescribeParameters"], resources=["*"]),
            iam.PolicyStatement(sid="SecretsRead", actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], resources=["*"]),
            iam.PolicyStatement(
                sid="MemoryAccess",
                actions=[
                    "bedrock-agentcore:CreateMemory", "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:UpdateMemory", "bedrock-agentcore:DeleteMemory",
                    "bedrock-agentcore:ListMemories", "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent", "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(sid="KBRetrieve", actions=["bedrock:Retrieve"], resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"]),
            iam.PolicyStatement(sid="GatewayInvoke", actions=["bedrock-agentcore:InvokeGateway"], resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"]),
        ])
        role.attach_inline_policy(self.policy)
        return role

    def _create_log_group(self) -> logs.LogGroup:
        return logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/aws/bedrock-agentcore/runtimes/{self.tool_name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

    # ── AgentCore Memory
    def _create_memory(self) -> agentcore.Memory:
        return agentcore.Memory(
            self, "Memory",
            memory_name=f"{self.tool_name}_memory",
            description=f"Conversation memory for {self.tool_name}",
            expiration_duration=Duration.days(90),
            memory_strategies=[
                agentcore.MemoryStrategy.using_built_in_semantic(),
                agentcore.MemoryStrategy.using_built_in_user_preference(),
            ],
        )

    # ── Cognito (user_password_auth)
    def _create_cognito(self, username: str):
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
            generate_secret=False,
        )
        user = cognito.CfnUserPoolUser(
            self, "TestUser",
            user_pool_id=user_pool.user_pool_id,
            username=username,
            user_attributes=[{"name": "email", "value": "test@example.com"}],
        )
        return user_pool, client, user

    def _create_secret_update_resource(self, username: str, password: str):
        """Set permanent password + store credentials in Secrets Manager."""
        role = iam.Role(
            self, "UpdateSecretRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
            inline_policies={"Access": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["secretsmanager:UpdateSecret", "secretsmanager:CreateSecret", "secretsmanager:DescribeSecret"],
                    resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{self.tool_name}/cognito/credentials-*"],
                ),
                iam.PolicyStatement(actions=["cognito-idp:AdminSetUserPassword"], resources=[self.user_pool.user_pool_arn]),
            ])},
        )
        fn = lambda_.Function(
            self, "UpdateSecretFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            role=role,
            code=lambda_.Code.from_inline("""
import boto3, json
cognito = boto3.client("cognito-idp")
secrets = boto3.client("secretsmanager")
def handler(event, context):
    if event["RequestType"] in ["Create", "Update"]:
        p = event["ResourceProperties"]
        cognito.admin_set_user_password(UserPoolId=p["UserPoolId"], Username=p["Username"], Password=p["Password"], Permanent=True)
        sv = json.dumps({
            "user_pool_id": p["UserPoolId"], "client_id": p["ClientId"],
            "username": p["Username"], "password": p["Password"], "discovery_url": p["DiscoveryUrl"],
        })
        try: secrets.update_secret(SecretId=p["SecretName"], SecretString=sv)
        except secrets.exceptions.ResourceNotFoundException: secrets.create_secret(Name=p["SecretName"], SecretString=sv)
        return {"PhysicalResourceId": f"{p['UserPoolId']}-secret"}
    return {"PhysicalResourceId": event.get("PhysicalResourceId", "default")}
"""),
        )
        provider = cr.Provider(self, "UpdateSecretProvider", on_event_handler=fn)
        resource = CustomResource(
            self, "UpdateSecret",
            service_token=provider.service_token,
            properties={
                "UserPoolId": self.user_pool.user_pool_id,
                "Username": username,
                "Password": password,
                "ClientId": self.client.user_pool_client_id,
                "DiscoveryUrl": f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
                "SecretName": f"{self.tool_name}/cognito/credentials",
            },
        )
        resource.node.add_dependency(self.user)
        resource.node.add_dependency(self.client)
        self.update_secret_role = role
        self.update_secret_fn = fn

    # ── AgentCore Runtime
    def _create_runtime(self) -> agentcore.Runtime:
        agent_path = Path(__file__).parent.parent.parent.parent / "agents" / "orchestrator-agent"
        artifact = agentcore.AgentRuntimeArtifact.from_asset(str(agent_path), platform=ecr_assets.Platform.LINUX_ARM64)
        runtime = agentcore.Runtime(
            self, "Runtime",
            runtime_name=self.tool_name,
            agent_runtime_artifact=artifact,
            execution_role=self.role,
            protocol_configuration=agentcore.ProtocolType.HTTP,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
                [self.client.user_pool_client_id],
            ),
            environment_variables={"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region},
        )
        runtime.node.add_dependency(self.policy)
        return runtime

    def _create_ssm_parameters(self):
        for name, value in [
            (f"/{self.tool_name}/runtime/agent_name", self.tool_name),
            (f"/{self.tool_name}/runtime/agent_role_name", self.role.role_name),
            (f"/{self.tool_name}/runtime/agent_arn", self.runtime.agent_runtime_arn),
            (f"/{self.tool_name}/runtime/agent_id", self.runtime.agent_runtime_id),
            (f"/{self.tool_name}/memory/memory_id", self.memory.memory_id),
        ]:
            ssm.StringParameter(self, name.replace("/", "_"), parameter_name=name, string_value=value)

    def _create_outputs(self):
        CfnOutput(self, "RuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "RuntimeId", value=self.runtime.agent_runtime_id)
        CfnOutput(self, "MemoryId", value=self.memory.memory_id)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "ClientId", value=self.client.user_pool_client_id)

    def _apply_cdk_nag_suppressions(self):
        NagSuppressions.add_resource_suppressions(self.policy, [{"id": "AwsSolutions-IAM5", "reason": "Wildcards required for X-Ray, Bedrock model access, ECR auth."}])
        NagSuppressions.add_resource_suppressions(self.role, [{"id": "AwsSolutions-IAM4", "reason": "AgentCore service role."}])
```

---

## Pattern 2: MCP server stack — `<Domain>McpAgentCoreStack`

Core responsibilities:
- MCP Runtime + Cognito (M2M, client_credentials)
- Resource server with the `<tool>-api/invoke` scope
- Create the OAuth2 credential provider (Custom Resource)
- Compose the endpoint URL (Custom Resource — URL encode)
- Inject the external API config via SSM Parameter Store

> The full stack code is ~400 lines and is nearly identical to [the original reference project's `jira_mcp_stack.py`](https://...). Only the essentials are summarized:

### Cognito + OAuth2 client_credentials

```python
def _create_cognito(self):
    user_pool = cognito.UserPool(
        self, "UserPool",
        user_pool_name=f"{self.tool_name}-user-pool",
        self_sign_up_enabled=False,
        removal_policy=RemovalPolicy.DESTROY,
    )

    resource_server = user_pool.add_resource_server(
        "ResourceServer",
        identifier=f"{self.tool_name}-api",
        scopes=[cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP server")],
    )

    client = user_pool.add_client(
        "Client",
        generate_secret=True,                   # ← M2M requires a secret
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.resource_server(
                resource_server,
                cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke MCP server"),
            )],
        ),
    )

    domain_prefix = f"{self.tool_name.replace('_', '-')}-{self.account}"
    user_pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix))

    return user_pool, client, domain_prefix
```

### Endpoint URL Custom Resource

```python
def _create_endpoint_url(self) -> str:
    role = iam.Role(self, "EndpointUrlRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")])
    fn = lambda_.Function(self, "EndpointUrlFn",
        runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler", timeout=Duration.seconds(30), role=role,
        code=lambda_.Code.from_inline('''
import urllib.parse
def handler(event, context):
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "endpoint-url")}
    p = event["ResourceProperties"]
    encoded = urllib.parse.quote(p["RuntimeArn"], safe="")
    url = f"https://bedrock-agentcore.{p['Region']}.amazonaws.com/runtimes/{encoded}/invocations"
    return {"PhysicalResourceId": "endpoint-url", "Data": {"EndpointUrl": url}}
'''))
    provider = cr.Provider(self, "EndpointUrlProvider", on_event_handler=fn)
    resource = CustomResource(self, "EndpointUrl",
        service_token=provider.service_token,
        properties={"RuntimeArn": self.runtime.agent_runtime_arn, "Region": self.region})
    resource.node.add_dependency(self.runtime)
    return resource.get_att_string("EndpointUrl")
```

### OAuth2 credential provider Custom Resource

```python
def _create_oauth_provider(self):
    name = f"gateway-{self.tool_name.replace('_', '-')}-oauth"
    role = iam.Role(self, "OAuthProviderRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        inline_policies={"Access": iam.PolicyDocument(statements=[
            iam.PolicyStatement(
                actions=["bedrock-agentcore:CreateOauth2CredentialProvider", "bedrock-agentcore:DeleteOauth2CredentialProvider",
                         "bedrock-agentcore:GetOauth2CredentialProvider", "bedrock-agentcore:CreateTokenVault"],
                resources=["*"],
            ),
            iam.PolicyStatement(actions=["cognito-idp:DescribeUserPoolClient"], resources=[self.user_pool.user_pool_arn]),
            iam.PolicyStatement(actions=["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret"],
                                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity*"]),
        ])})
    fn = lambda_.Function(self, "OAuthProviderFn",
        runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler", timeout=Duration.minutes(2), role=role,
        code=lambda_.Code.from_inline('''
import boto3, time
def handler(event, context):
    p = event["ResourceProperties"]
    control = boto3.client("bedrock-agentcore-control")
    cognito = boto3.client("cognito-idp")
    name = p["ProviderName"]
    if event["RequestType"] == "Delete":
        try: control.delete_oauth2_credential_provider(name=name)
        except: pass
        return {"PhysicalResourceId": name}
    resp = cognito.describe_user_pool_client(UserPoolId=p["UserPoolId"], ClientId=p["ClientId"])
    secret = resp["UserPoolClient"].get("ClientSecret", "")
    if event["RequestType"] == "Update":
        try: control.delete_oauth2_credential_provider(name=name); time.sleep(10)
        except: pass
    try:
        out = control.create_oauth2_credential_provider(
            name=name, credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={"customOauth2ProviderConfig": {
                "oauthDiscovery": {"discoveryUrl": p["DiscoveryUrl"]},
                "clientId": p["ClientId"], "clientSecret": secret,
            }})
    except control.exceptions.ConflictException:
        out = control.get_oauth2_credential_provider(name=name)
    parn = out.get("credentialProviderArn") or out.get("oauth2CredentialProvider", {}).get("credentialProviderArn")
    sarn = out.get("clientSecretArn", {}).get("secretArn") or out.get("oauth2CredentialProvider", {}).get("clientSecretArn", {}).get("secretArn")
    return {"PhysicalResourceId": name, "Data": {"ProviderArn": parn, "SecretArn": sarn}}
'''))
    provider = cr.Provider(self, "OAuthProviderProvider", on_event_handler=fn)
    resource = CustomResource(self, "OAuthProvider",
        service_token=provider.service_token,
        properties={
            "ProviderName": name,
            "UserPoolId": self.user_pool.user_pool_id,
            "ClientId": self.client.user_pool_client_id,
            "DiscoveryUrl": f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
        })
    return resource.get_att_string("ProviderArn"), resource.get_att_string("SecretArn")
```

### MCP Runtime

```python
def _create_runtime(self):
    mcp_path = Path(__file__).parent.parent.parent.parent / "mcp-servers" / f"{self.tool_name.replace('_', '-')}"
    runtime = agentcore.Runtime(
        self, "Runtime",
        runtime_name=self.tool_name,
        agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(str(mcp_path), platform=ecr_assets.Platform.LINUX_ARM64),
        execution_role=self.role,
        protocol_configuration=agentcore.ProtocolType.MCP,           # ← only the MCP server uses the MCP type
        authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
            f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
            [self.client.user_pool_client_id],
        ),
        environment_variables={"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region},
    )
    runtime.node.add_dependency(self.policy)
    return runtime
```

### Injecting the external API config (e.g., Jira)

```python
def _create_ssm_parameters(self, config: dict):
    # public name/arn parameters
    ssm.StringParameter(self, "AgentArn", parameter_name=f"/{self.tool_name}/runtime/agent_arn", string_value=self.runtime.agent_runtime_arn)
    ssm.StringParameter(self, "EndpointUrl", parameter_name=f"/{self.tool_name}/runtime/endpoint_url", string_value=self.endpoint_url)

    # external API config (from .env via os.getenv)
    if config.get("JIRA_BASE_URL"):
        ssm.StringParameter(self, "BaseUrl", parameter_name=f"/{self.tool_name}/config/jira_base_url", string_value=config["JIRA_BASE_URL"])
    if config.get("JIRA_EMAIL"):
        ssm.StringParameter(self, "Email", parameter_name=f"/{self.tool_name}/config/jira_email", string_value=config["JIRA_EMAIL"])
    if config.get("JIRA_API_TOKEN"):
        ssm.StringParameter(self, "Token", parameter_name=f"/{self.tool_name}/config/jira_api_token", string_value=config["JIRA_API_TOKEN"])
```

---

## Pattern 3: Specialized agent stack — `<Domain>AgentStack`

A specialized Strands agent is almost identical to the MCP server stack, except:
- `protocol_configuration=agentcore.ProtocolType.HTTP` (not MCP)
- Does not create an OAuth2 credential provider (it is not a Gateway target)
- Adds a data source configuration (Athena, Glue, S3, etc.)

```python
def _create_runtime(self):
    agent_path = Path(__file__).parent.parent.parent.parent / "agents" / "text2sql-agent"
    runtime = agentcore.Runtime(
        self, "Runtime",
        runtime_name=self.tool_name,
        agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(str(agent_path), platform=ecr_assets.Platform.LINUX_ARM64),
        execution_role=self.role,
        protocol_configuration=agentcore.ProtocolType.HTTP,    # ← HTTP
        authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
            f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
            [self.client.user_pool_client_id],
        ),
        environment_variables={
            "AWS_REGION": self.region,
            "AWS_DEFAULT_REGION": self.region,
            "BEDROCK_MODEL_ID": self.model_id,
            "TOOL_NAME": self.tool_name,
        },
    )
    runtime.node.add_dependency(self.policy)
    return runtime
```

Data source (Athena + Glue + S3) example:

```python
def _create_data_resources(self):
    table_bucket = s3.Bucket(self, "TableBucket",
        bucket_name=f"{self.tool_name.replace('_', '-')}-tables-{self.account}-{self.region}",
        removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)
    results_bucket = s3.Bucket(self, "ResultsBucket",
        bucket_name=f"{self.tool_name.replace('_', '-')}-results-{self.account}-{self.region}",
        removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

    glue.CfnDatabase(self, "GlueDatabase",
        catalog_id=self.account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(name="workshop_db", description="Sample DB"))

    for tname, cols in [
        ("customers", [("customer_id", "int"), ("name", "string"), ("email", "string"), ("country", "string")]),
        # ... etc
    ]:
        glue.CfnTable(self, f"{tname.title()}Table", ... )

    return table_bucket, results_bucket
```

---

## Pattern 4: Knowledge Base stack — `KnowledgeBaseStack`

```python
import aws_cdk as cdk
from aws_cdk import Stack, aws_ssm as ssm, RemovalPolicy
from constructs import Construct
from cdklabs.generative_ai_cdk_constructs import bedrock


class KnowledgeBaseStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        crawler_url = self.node.try_get_context("webCrawlerUrl") or "https://docs.example.com"

        kb = bedrock.VectorKnowledgeBase(
            self, "KnowledgeBase",
            embeddings_model=bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V1,
            description="Knowledge Base for multi-agent system",
        )

        bedrock.WebCrawlerDataSource(
            self, "WebCrawlerDataSource",
            knowledge_base=kb,
            data_source_name="workshop-web-crawler",
            source_urls=[crawler_url],
            chunking_strategy=bedrock.ChunkingStrategy.fixed_size(max_tokens=500, overlap_percentage=20),
        )

        ssm.StringParameter(self, "KbId",
            parameter_name="/workshop/knowledge_base/kb_id",
            string_value=kb.knowledge_base_id,
            description="Workshop Knowledge Base ID")

        cdk.CfnOutput(self, "KnowledgeBaseId", value=kb.knowledge_base_id)
        cdk.CfnOutput(self, "KnowledgeBaseArn", value=kb.knowledge_base_arn)
```

---

## Pattern 5: Gateway stack — `AgentCoreGatewayStack`

```python
from aws_cdk import (
    CfnOutput, Stack,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_iam as iam, aws_ssm as ssm,
)
from constructs import Construct


class AgentCoreGatewayStack(Stack):
    """Wires multiple MCP server stacks behind a single Gateway with semantic search."""

    def __init__(self, scope: Construct, construct_id: str, mcp_stacks: dict | None = None, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        mcp_stacks = mcp_stacks or {}

        self.gateway = agentcore.Gateway(
            self, "Gateway",
            gateway_name="multi-agent-gateway",
            description="Multi-agent Gateway for MCP servers",
            protocol_configuration=agentcore.McpProtocolConfiguration(
                search_type=agentcore.McpGatewaySearchType.SEMANTIC,    # ★ semantic search
            ),
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
        )

        # ★ Required IAM additions (CDK L2 doesn't auto-attach these)
        self.gateway.role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock-agentcore:*"], resources=["*"])
        )
        self.gateway.role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity*"],
            )
        )

        for name, stack in mcp_stacks.items():
            self._add_mcp_target(name, stack)

        ssm.StringParameter(self, "GatewayId", parameter_name="/agentcore_gateway/gateway_id", string_value=self.gateway.gateway_id)
        ssm.StringParameter(self, "GatewayUrl", parameter_name="/agentcore_gateway/gateway_url", string_value=self.gateway.gateway_url)

        CfnOutput(self, "GatewayId_Output", value=self.gateway.gateway_id)
        CfnOutput(self, "GatewayUrl_Output", value=self.gateway.gateway_url)

    def _add_mcp_target(self, name: str, mcp_stack):
        self.gateway.add_mcp_server_target(
            f"{name.capitalize()}McpTarget",
            gateway_target_name=f"{name}-target",
            description=f"{name.capitalize()} MCP Server",
            endpoint=mcp_stack.runtime_endpoint_url,
            credential_provider_configurations=[
                agentcore.GatewayCredentialProvider.from_oauth_identity_arn(
                    provider_arn=mcp_stack.oauth_provider_arn,
                    secret_arn=mcp_stack.oauth_secret_arn,
                    scopes=[f"{mcp_stack.tool_name}-api/invoke"],
                )
            ],
        )
```

---

## Pattern 6: Deployment script (`scripts/deploy.sh`)

```bash
#!/usr/bin/env bash
set -e

REGION="${AWS_REGION:-us-east-1}"
echo "→ Deploying to region: $REGION"

# 1) venv + deps
python3.13 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install bedrock-agentcore strands-agents bedrock-agentcore-starter-toolkit
pip install aws-cdk.aws-bedrock-agentcore-alpha==2.231.0a0

# 2) CDK install + bootstrap
cd cdk-infra
pip install -r requirements.txt
cdk bootstrap "aws://$(aws sts get-caller-identity --query Account --output text)/$REGION"

# 3) Deploy in order
cdk deploy MultiAgentOrchestrator --require-approval never
cdk deploy JiraMcp GitHubMcp Text2SqlAgent --require-approval never
cdk deploy WorkshopKnowledgeBase --require-approval never
cdk deploy AgentCoreGateway --require-approval never

# 4) Print key outputs
aws cloudformation describe-stacks --stack-name MultiAgentOrchestrator --query 'Stacks[0].Outputs'
aws cloudformation describe-stacks --stack-name AgentCoreGateway --query 'Stacks[0].Outputs'

echo "✓ Deployment complete. Update frontend/public/config.json with the user pool ID and runtime endpoint."
```

## Pattern 7: Verification script (`scripts/check-prerequisites.sh`)

```bash
#!/usr/bin/env bash
set -e
echo "→ Checking prerequisites..."
python3.13 --version || { echo "✗ Python 3.13 required"; exit 1; }
node --version | grep -E "v(20|2[1-9])" || { echo "✗ Node 20+ required"; exit 1; }
docker info >/dev/null 2>&1 || { echo "✗ Docker daemon not running"; exit 1; }
cdk --version | grep -E "2\.(23[1-9]|2[4-9][0-9]|[3-9][0-9]{2})" || { echo "✗ CDK 2.231+ required"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "✗ AWS credentials not configured"; exit 1; }
echo "✓ All prerequisites OK"
```
