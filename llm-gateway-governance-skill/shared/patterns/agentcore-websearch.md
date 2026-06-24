# Pattern — AgentCore Web Search (replaces Tavily MCP)

Web search is provided by **Web Search on Amazon Bedrock AgentCore** — a fully-managed,
MCP-compliant **built-in connector** (`connectorId: web-search`) exposed through an **AgentCore
Gateway**. It replaces the former self-hosted **Tavily MCP runtime** entirely:

| Old (Tavily) | New (AgentCore Web Search) |
|---|---|
| Tavily MCP server image on AgentCore **Runtime**, deployed separately via Marketplace | **Built-in connector** on an AgentCore **Gateway** — provisioned in-CDK, no separate deploy |
| Tavily **API key** in Secrets Manager (`TAVILY_SECRET_NAME`) | **No API key** — AWS-owned, queries never leave AWS |
| Task Role `bedrock-agentcore:InvokeAgentRuntime` | Task Role `bedrock-agentcore:InvokeGateway` |
| Tools `tavily-tavily_search`/`-extract`/… | Tool `websearch-web-search-tool___WebSearch` (input `query`, `maxResults` 1–25) |

**Key facts (verify with MCP at design time):**
- GA region: **us-east-1 only** → the gateway stack is pinned to `config.agentcore.webSearchRegion`.
- CloudFormation resources exist: `AWS::BedrockAgentCore::Gateway`, `AWS::BedrockAgentCore::GatewayTarget`.
- Gateway inbound auth = **`AWS_IAM`** (SigV4). LiteLLM signs MCP calls with the ECS Task Role — no OAuth/JWT/Cognito, nothing to rotate. (Other allowed authorizer types: `CUSTOM_JWT`, `NONE`, `AUTHENTICATE_ONLY`.)
- Gateway `Fn::GetAtt`: `GatewayUrl` (MCP endpoint), `GatewayArn`, `GatewayIdentifier`, `Status`.
- LiteLLM calls the gateway MCP endpoint **cross-region** (gateway in us-east-1, gateway platform in `config.awsRegion`) over the public AWS network (SigV4); no VPC endpoint required for the gateway itself.

## Config (`config/dev.json`)

```jsonc
"agentcore": {
  "webSearchRegion": "us-east-1",          // GA region (pinned)
  "gatewayName": "codeagent-gov-dev-websearch", // ^([0-9a-zA-Z][-]?){1,100}$ — no underscores, no trailing hyphen
  "domainDenyList": []                      // optional server-side domain blocklist (hidden from the model)
}
```

Schema (`lib/config/schema.ts`):
```ts
export interface AgentCoreConfig {
  readonly webSearchRegion: string;          // must be us-east-1
  readonly gatewayName: string;              // ^([0-9a-zA-Z][-]?){1,100}$
  readonly domainDenyList?: readonly string[];
}
```

## Stack: `lib/agentcore-gateway-stack.ts`

L1 `CfnResource` for the Gateway + GatewayTarget (the built-in connector shape is new; CFN supports it). The service role grants the two documented actions.

```ts
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { AgentCoreGatewayExports } from './interfaces';
import { AgentCoreConfig } from './config/schema';
import { ns } from './config/constants';

export interface AgentCoreGatewayStackProps extends cdk.StackProps {
  readonly config: AgentCoreConfig;
}

export class AgentCoreGatewayStack extends cdk.Stack implements AgentCoreGatewayExports {
  public readonly gatewayUrl: string;
  public readonly gatewayArn: string;
  public readonly webSearchRegion: string;

  constructor(scope: Construct, id: string, props: AgentCoreGatewayStackProps) {
    super(scope, id, props);
    const { config } = props;
    this.webSearchRegion = config.webSearchRegion;
    const region = this.region;
    const account = this.account;

    // Gateway service role (assumed by the AgentCore service)
    const serviceRole = new iam.Role(this, 'GatewayServiceRole', {
      roleName: ns('websearch-gateway-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'AgentCore Gateway service role: invoke gateway + Web Search tool',
    });
    serviceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeGateway',
      actions: ['bedrock-agentcore:InvokeGateway'],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:gateway/*`],
    }));
    serviceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeWebSearch',
      actions: ['bedrock-agentcore:InvokeWebSearch'],
      // service-owned tool ARN — note the literal `aws` account segment
      resources: [`arn:aws:bedrock-agentcore:${region}:aws:tool/web-search.v1`],
    }));

    // Gateway (MCP protocol, AWS_IAM inbound auth)
    const gateway = new cdk.CfnResource(this, 'Gateway', {
      type: 'AWS::BedrockAgentCore::Gateway',
      properties: {
        Name: config.gatewayName,
        ProtocolType: 'MCP',
        AuthorizerType: 'AWS_IAM',
        RoleArn: serviceRole.roleArn,
        Description: 'Web Search Tool gateway',
      },
    });
    gateway.node.addDependency(serviceRole);

    // Built-in web-search connector target (+ optional domain denylist)
    const parameterValues: Record<string, unknown> =
      config.domainDenyList && config.domainDenyList.length > 0
        ? { domainFilter: { exclude: [...config.domainDenyList] } }
        : {};

    const target = new cdk.CfnResource(this, 'WebSearchTarget', {
      type: 'AWS::BedrockAgentCore::GatewayTarget',
      properties: {
        GatewayIdentifier: gateway.getAtt('GatewayIdentifier').toString(),
        Name: 'web-search-tool',
        Description: 'Built-in AgentCore Web Search connector',
        TargetConfiguration: {
          Mcp: {
            Connector: {
              Source: { ConnectorId: 'web-search' },
              Configurations: [{ Name: 'WebSearch', ParameterValues: parameterValues }],
            },
          },
        },
        CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
      },
    });
    target.node.addDependency(gateway);

    this.gatewayUrl = gateway.getAtt('GatewayUrl').toString();
    this.gatewayArn = gateway.getAtt('GatewayArn').toString();
    new cdk.CfnOutput(this, 'WebSearchGatewayUrl', { value: this.gatewayUrl });
    new cdk.CfnOutput(this, 'WebSearchGatewayArn', { value: this.gatewayArn });
    new cdk.CfnOutput(this, 'WebSearchRegion', { value: this.webSearchRegion });
  }
}
```

Cross-stack export (`lib/interfaces.ts`):
```ts
export interface AgentCoreGatewayExports {
  readonly gatewayUrl: string;       // MCP endpoint (SigV4 / AWS_IAM inbound)
  readonly gatewayArn: string;
  readonly webSearchRegion: string;  // us-east-1
}
```

## Wiring into LiteLLM

`bin/app.ts` — instantiate the gateway (us-east-1) and pass its exports to LiteLLM (which runs in `config.awsRegion`, so set `crossRegionReferences: true` on the LiteLLM stack):
```ts
const agentcoreGateway = new AgentCoreGatewayStack(app, 'AgentCoreGatewayStack', {
  env: { account, region: config.agentcore.webSearchRegion },  // us-east-1
  stackName: ns('websearch-gateway'), tags,
  config: config.agentcore,
});
const litellm = new LiteLLMStack(app, 'LiteLLMStack', {
  ...stackProps('litellm'),
  crossRegionReferences: true,
  agentcoreGateway,           // exports: gatewayUrl, webSearchRegion
  /* ...other props... */
});
```

LiteLLM Task Role (`lib/litellm-stack.ts`) — replace the old `InvokeAgentRuntime` with `InvokeGateway`:
```ts
taskRole.addToPolicy(new iam.PolicyStatement({
  actions: ['bedrock-agentcore:InvokeGateway'],
  resources: ['*'], // dev sample; PROD TODO: scope to the gateway ARN
}));
```

LiteLLM container env:
```ts
WEBSEARCH_GATEWAY_URL: agentcoreGateway.gatewayUrl,
WEBSEARCH_GATEWAY_REGION: agentcoreGateway.webSearchRegion, // us-east-1
```

`services/litellm/config.yaml` — the MCP server points at the gateway, signed with SigV4 for `bedrock-agentcore`:
```yaml
mcp_servers:
  websearch:
    url: os.environ/WEBSEARCH_GATEWAY_URL
    transport: "http"
    auth_type: "aws_sigv4"
    aws_region_name: os.environ/WEBSEARCH_GATEWAY_REGION   # us-east-1
    aws_service_name: "bedrock-agentcore"
    access_groups: ["default_tools"]   # team-scoped (see lambda-handlers / decision-tree)

mcp_settings:
  require_approval: "never"
```

## Verification

- `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <id> --region us-east-1` → target `web-search-tool` status `READY`.
- Through LiteLLM: `GET /v1/mcp/tools` (Bearer master/virtual key) lists `websearch-web-search-tool___WebSearch`.
- End-to-end: a chat/completions call with `tools:[{type:"mcp",server_label:"websearch",server_url:"litellm_proxy",require_approval:"never"}]` → the model invokes WebSearch and grounds the answer with cited URLs.

## Gotchas

- **Region**: Web Search is **us-east-1 only**. Bootstrap us-east-1 in addition to the gateway region.
- **Gateway name** must match `^([0-9a-zA-Z][-]?){1,100}$` (no underscores, no trailing hyphen).
- **`AWS_IAM` inbound** keeps the design tokenless. If you instead pick `CUSTOM_JWT`, you must run an OIDC IdP and have LiteLLM obtain JWTs — avoid unless required.
- **SLR**: deploy role needs `iam:CreateServiceLinkedRole` for AgentCore.
- The `InvokeWebSearch` resource ARN uses the literal `aws` account segment: `arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1`.
