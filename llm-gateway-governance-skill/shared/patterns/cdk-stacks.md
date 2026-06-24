# CDK Stacks — Reproduction Guide for the 11-Stack Governance Gateway

> This document transcribes the CDK (TypeScript) stacks of the `llm-gateway-multi-agent` reference solution **verbatim**, with English explanations + WHY comments + cross-layer mapping added.
> So that an AI agent can read this document and regenerate the same gateway, the code is not summarized — it carries the actual source.
>
> **Current architecture (v1.1) — the 3 new/changed stacks have their full source in separate pattern documents:**
> - `AgentCoreGatewayStack` (us-east-1, web search) → `shared/patterns/agentcore-websearch.md`
> - `MantleNetworkStack` (us-east-1) + `MantlePeeringRoutesStack` (default region, Mantle peering) → `shared/patterns/mantle-peering.md`
> In addition, the `bin/app.ts`/`schema.ts`/`litellm-stack.ts`/`cdn-stack.ts` in the body of this document must reflect the following:
> ① add a top-level `config.awsRegion` (platform region, authoritative) + `config.sso`/`config.agentcore`/`config.mantle`,
> ② add `bedrock-agentcore:InvokeGateway` + `aws-marketplace:Subscribe` (+`ViewSubscriptions`/`Unsubscribe`) to the LiteLLM Task Role, and add `WEBSEARCH_GATEWAY_URL`/`WEBSEARCH_GATEWAY_REGION`/`BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE` to its env (Mantle region pinning — `MANTLE_REGION` is not read by the provider),
> ③ AuthStack consumes `config.sso` and outputs `SsoStartUrl/Region/AccountId/RoleName`,
> ④ CdnStack LiteLLM origin `readTimeout`/`keepaliveTimeout = 60s` (mitigates the Mantle first-subscription cold-start 504).
>
> **Fixed deployment order (zero circular dependencies):**
> `Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM → Langfuse(conditional) → Auth → Observability → CDN(us-east-1) → MantleNetwork(us-east-1) → MantlePeeringRoutes`
>
> Core design principles:
> - **Cross-stack coupling only via the `*Exports` interfaces (append-only)** — validated at compile time. Cross-region wiring uses `crossRegionReferences: true`.
> - **Runtime-only wiring (LiteLLM internal URL → Token Service) references the SSM Parameter Store by "name"** (avoids deploy-time cross-refs).
> - **Model authentication is tokenless (SigV4 Task Role)** — there is no key to rotate and no token-refresh scheduler.
> - **The region is authoritative via `config.awsRegion`** (`bin/app.ts`: `config.awsRegion ?? CDK_DEFAULT_REGION ?? AWS_REGION`). AgentCoreGateway, CDN, and MantleNetwork are pinned to us-east-1.
> - **The ALB is always internal**; the CloudFront VPC Origin is the only external entry point.

---

## 0. App wiring — `bin/app.ts`

CDK app entry point. Instantiates the 6 stacks (+Guardrail/CDN) in a **fixed order** and wires them with explicit props. Langfuse is created conditionally based on `config.enableLangfuse` (overridable via the context `-c enableLangfuse=false`).

```typescript
#!/usr/bin/env node
/**
 * CDK app entry. Instantiates 6 stacks in fixed order with explicit props wiring.
 * Langfuse is conditional on config.enableLangfuse (overridable via
 * `-c enableLangfuse=false`).
 *
 * Fixed order (zero circular deps):
 *   Network -> Data -> LiteLLM -> Langfuse(conditional) -> Auth -> Observability
 *
 * Tavily MCP runtime is deployed separately via Marketplace CLI (us-east-1).
 * LiteLLM calls it cross-region via SigV4 (no Gateway, no CDK stack needed).
 *
 * Owned by: Architect.
 */
import 'source-map-support/register';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { AWS_REGION, ENV_NAME, PROJECT_PREFIX, ns } from '../lib/config/constants';
import { validateConfig } from '../lib/config/schema';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { LiteLLMStack } from '../lib/litellm-stack';
import { LangfuseStack } from '../lib/langfuse-stack';
import { AuthStack } from '../lib/auth-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { CdnStack } from '../lib/cdn-stack';
import { GuardrailStack } from '../lib/guardrail-stack';
import { applyDevSuppressions, applyResourceSuppressions } from '../lib/nag-suppressions';

const app = new cdk.App();

// ---- Load + validate config -------------------------------------------------
const configPath = path.join(__dirname, '..', 'config', `${ENV_NAME}.json`);
const rawConfig = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
const config = validateConfig(rawConfig);

// Context override: -c enableLangfuse=false
const ctxLangfuse = app.node.tryGetContext('enableLangfuse');
const enableLangfuse =
  ctxLangfuse === undefined ? config.enableLangfuse : ctxLangfuse !== 'false' && ctxLangfuse !== false;

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? AWS_REGION,
};
const tags = { Project: PROJECT_PREFIX, Environment: ENV_NAME };
const stackProps = (id: string): cdk.StackProps => ({ env, stackName: ns(id), tags });

// ---- 1. Network -------------------------------------------------------------
const network = new NetworkStack(app, 'NetworkStack', {
  ...stackProps('network'),
  config: config.network,
});

// ---- 2. Data ----------------------------------------------------------------
const data = new DataStack(app, 'DataStack', {
  ...stackProps('data'),
  config: config.data,
  network,
});

// ---- 2.5 Guardrail ----------------------------------------------------------
const guardrail = new GuardrailStack(app, 'GuardrailStack', {
  ...stackProps('guardrail'),
  enabled: config.guardrail.enabled,
});

// ---- 3. LiteLLM -------------------------------------------------------------
const litellm = new LiteLLMStack(app, 'LiteLLMStack', {
  ...stackProps('litellm'),
  crossRegionReferences: true,
  config: config.litellm,
  agentcore: config.agentcore,
  guardrailId: guardrail.guardrailId,
  guardrailVersion: guardrail.guardrailVersion,
  network,
  data,
});

// ---- 4. Langfuse (conditional) ----------------------------------------------
let langfuse: LangfuseStack | undefined;
if (enableLangfuse) {
  langfuse = new LangfuseStack(app, 'LangfuseStack', {
    ...stackProps('langfuse'),
    crossRegionReferences: true,
    config: config.langfuse,
    network,
    data,
  });
}

// ---- 5. Auth ----------------------------------------------------------------
const auth = new AuthStack(app, 'AuthStack', {
  ...stackProps('auth'),
  config: config.auth,
  network,
  litellm,
});

// ---- 6. Observability -------------------------------------------------------
new ObservabilityStack(app, 'ObservabilityStack', {
  ...stackProps('observability'),
  config: config.observability,
  litellm,
  auth,
  langfuse,
});

// ---- 7. CDN (CloudFront → ALBs) — deployed in us-east-1 (CF cert requirement)
// useCustomDomain: only when certMode='acm-dns' (a real Route53 hosted zone is
// available). Otherwise CloudFront uses its default *.cloudfront.net domain —
// no ACM cert, no Route53, no hosted zone required.
const cdn = new CdnStack(app, 'CdnStack', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' },
  stackName: ns('cdn'),
  tags,
  crossRegionReferences: true,
  useCustomDomain: config.litellm.certMode === 'acm-dns',
  litellmDomain: config.litellm.domainName,
  langfuseDomain: 'langfuse.example.com',
  hostedZoneId: config.litellm.hostedZoneId,
  hostedZoneName: config.litellm.hostedZoneName,
  litellm,
  langfuse,
  litellmAlb: litellm.loadBalancer,
  langfuseAlb: langfuse?.loadBalancer,
});

// ---- Security checks --------------------------------------------------------
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

const allStacks: cdk.Stack[] = [network, data, litellm, auth, cdn];
if (langfuse) allStacks.push(langfuse);
applyDevSuppressions(allStacks);
applyResourceSuppressions({
  network, data, litellm, auth,
  ...(langfuse ? { langfuse } : {}),
});

app.synth();
```

**WHY — wiring essentials:**
- **Fixed order = circular dependencies eliminated.** Each stack directly receives, as props, the instances of stacks created before it (`network`, `data`, `litellm`, etc.). CDK automatically builds the dependency graph and deploys in topological order.
- **`useCustomDomain` is a derived value.** It is true when `config.litellm.certMode === 'acm-dns'`. That is, it operates in custom-domain mode only when a real Route53 hosted zone exists; otherwise it uses the default CloudFront domain. Every domain/ACM/Route53 branch in the CDN stack diverges from this single line.
- **Only the CDN deploys to `us-east-1`.** The ACM certificate for CloudFront must be in us-east-1, so its region differs from the other stacks (us-east-2). That is why `crossRegionReferences: true` is set on LiteLLM/Langfuse/CDN to allow cross-region export/import.
- **Guardrail is created "before" LiteLLM.** This is because LiteLLM receives `guardrail.guardrailId`/`guardrailVersion` as environment variables and applies them on Bedrock calls.
- **config is fail-fast validated by `validateConfig` immediately on entry** — synthesis/deployment does not proceed with an invalid config.

---

## 0-1. Cross-stack contract — `lib/interfaces.ts` (append-only `*Exports`)

Each Stack class implements its corresponding `*Exports` interface as `public readonly` fields. Downstream stacks receive these exports via props. **Signature changes require team sync** (the append-only convention).

```typescript
/**
 * Append-only cross-stack export contract.
 *
 * Each Stack class implements its corresponding `*Exports` interface as public
 * readonly fields. Downstream stacks receive these via props. Signature changes
 * require team sync (per build contract).
 *
 * Owned by: Architect.
 */
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

/** NetworkStack — VPC + the full security-group chain + endpoints. */
export interface NetworkExports {
  readonly vpc: ec2.IVpc;
  readonly litellmServiceSecurityGroup: ec2.ISecurityGroup;
  readonly langfuseServiceSecurityGroup: ec2.ISecurityGroup;
  readonly auroraSecurityGroup: ec2.ISecurityGroup;
  readonly albSecurityGroup: ec2.ISecurityGroup;
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  readonly agentcoreEndpointSecurityGroup: ec2.ISecurityGroup;
  readonly interfaceVpcEndpointSecurityGroup: ec2.ISecurityGroup;
}

/** DataStack — Aurora Serverless v2 cluster + per-app DB secrets. */
export interface DataExports {
  readonly cluster: rds.IDatabaseCluster;
  readonly litellmDbSecret: secretsmanager.ISecret;
  readonly langfuseDbSecret: secretsmanager.ISecret;
  readonly clusterEndpointHostname: string;
  readonly clusterPort: number;
}

/** LiteLLMStack — ECS/ALB gateway. Owns the LiteLLM master key secret. */
export interface LiteLLMExports {
  readonly loadBalancer: elbv2.IApplicationLoadBalancer;
  /** Public HTTPS base, e.g. https://{albDns}/v1 (internet-facing). */
  readonly publicHttpsUrl: string;
  readonly taskRole: iam.IRole;
  /** Master key created/owned here; consumed by AuthStack (grantRead). */
  readonly masterKeySecret: secretsmanager.ISecret;
  /** SSM param NAME (not value) carrying the internal service URL. */
  readonly internalUrlSsmParameterName: string;
}


/** LangfuseStack — optional (present only when enableLangfuse=true). */
export interface LangfuseExports {
  readonly langfuseUrl: string;
  readonly loadBalancer: elbv2.IApplicationLoadBalancer;
}

/** AuthStack — SSO Token Service. */
export interface AuthExports {
  readonly tokenServiceApiUrl: string;
  /** Full invoke URL; response body carries key `api_key`. */
  readonly tokenServiceInvokeUrl: string;
  readonly keyCacheTable: dynamodb.ITable;
  readonly keyCacheTableName: string;
}

/** ObservabilityStack. */
export interface ObservabilityExports {
  readonly dashboardName: string;
}
```

**WHY — cross-layer mapping:**
- **Exposed as `I*` types (`IVpc`, `ISecret`)**: exposing interfaces rather than concrete types keeps downstream consumers from coupling to construction details (the construct).
- **`masterKeySecret` is owned by LiteLLM → consumed by Auth via `grantRead`**: single ownership of the master key stays with LiteLLM, and the Token Service receives read permission only. (See the Auth stack.)
- **`internalUrlSsmParameterName` is the "name", not the "value"**: the LiteLLM ALB's internal URL is not passed as a deploy-time cross-stack ref; only the SSM parameter "name" is exported. The Token Service Lambda looks up SSM by this name **at runtime** → this breaks the deploy-time tight coupling between LiteLLM and Auth.
- **append-only**: as long as fields are only added and never removed/changed, downstream consumers do not break.

---

## 0-2. Config schema + runtime validation — `lib/config/schema.ts`

The type definitions for `config/dev.json`, plus a lightweight runtime validator with no external dependencies. It lets `bin/app.ts` fail-fast on an invalid config before synthesis.

```typescript
export interface LiteLLMConfig {
  /**
   * Custom domain for the LiteLLM ALB (e.g. llmlite.example.com).
   * Required when certMode='acm-dns'. If empty and certMode='acm-arn', the ALB
   * DNS is used for the URL.
   */
  readonly domainName: string;
  /**
   * Certificate source:
   *  - 'acm-dns': CDK issues a Public ACM cert for `domainName`, DNS-validated
   *    via the Route53 hosted zone (`hostedZoneId` + `hostedZoneName`), and
   *    creates an A-record alias to the ALB. No manual ARN needed.
   *  - 'acm-arn': use an existing certificate (`certificateArn`).
   */
  readonly certMode: 'acm-dns' | 'acm-arn';
  /** Existing ACM cert ARN (required when certMode='acm-arn'). */
  readonly certificateArn: string;
  /** Route53 hosted zone (required when certMode='acm-dns'). */
  readonly hostedZoneId: string;
  readonly hostedZoneName: string;
  /** LiteLLM master key (admin login for UI + API). */
  readonly masterKey: string;
  readonly desiredCount: number;
  readonly cpu: number;
  readonly memoryLimitMiB: number;
}

/** Minimal structural validator. Throws on the first violation. */
export function validateConfig(raw: unknown): AppConfig {
  const c = raw as Record<string, unknown>;
  const req = (cond: boolean, msg: string): void => {
    if (!cond) throw new Error(`config/dev.json invalid: ${msg}`);
  };
  req(typeof c === 'object' && c !== null, 'root must be an object');
  req(typeof c.enableLangfuse === 'boolean', 'enableLangfuse must be boolean');

  const obj = (k: string): Record<string, unknown> => {
    req(typeof c[k] === 'object' && c[k] !== null, `${k} must be an object`);
    return c[k] as Record<string, unknown>;
  };
  const num = (o: Record<string, unknown>, k: string, path: string): void =>
    req(typeof o[k] === 'number', `${path}.${k} must be a number`);
  const str = (o: Record<string, unknown>, k: string, path: string): void =>
    req(typeof o[k] === 'string', `${path}.${k} must be a string`);

  // ... network / data validation omitted (same pattern) ...

  const litellm = obj('litellm');
  str(litellm, 'certMode', 'litellm');
  req(
    litellm.certMode === 'acm-dns' || litellm.certMode === 'acm-arn',
    "litellm.certMode must be 'acm-dns' or 'acm-arn'",
  );
  str(litellm, 'certificateArn', 'litellm');
  str(litellm, 'domainName', 'litellm');
  str(litellm, 'hostedZoneId', 'litellm');
  str(litellm, 'hostedZoneName', 'litellm');
  str(litellm, 'masterKey', 'litellm');
  req((litellm.masterKey as string).length > 0, 'litellm.masterKey must be set');
  num(litellm, 'desiredCount', 'litellm');
  num(litellm, 'cpu', 'litellm');
  num(litellm, 'memoryLimitMiB', 'litellm');
  if (litellm.certMode === 'acm-dns') {
    req((litellm.domainName as string).length > 0, "litellm.domainName required when certMode='acm-dns'");
    req((litellm.hostedZoneId as string).length > 0, "litellm.hostedZoneId required when certMode='acm-dns'");
    req((litellm.hostedZoneName as string).length > 0, "litellm.hostedZoneName required when certMode='acm-dns'");
  } else {
    req((litellm.certificateArn as string).length > 0, "litellm.certificateArn required when certMode='acm-arn'");
  }

  const agentcore = obj('agentcore');
  str(agentcore, 'webSearchRegion', 'agentcore');           // us-east-1
  str(agentcore, 'gatewayName', 'agentcore');               // ^([0-9a-zA-Z][-]?){1,100}$
  req((agentcore.webSearchRegion as string).length > 0, 'agentcore.webSearchRegion must be set');
  // domainDenyList is optional (string[])

  // Top-level awsRegion (authoritative platform region) + sso + mantle blocks:
  str(c, 'awsRegion', 'root');
  const sso = obj('sso');
  str(sso, 'startUrl', 'sso'); str(sso, 'region', 'sso'); str(sso, 'accountId', 'sso'); str(sso, 'roleName', 'sso');
  const mantle = obj('mantle');
  str(mantle, 'region', 'mantle'); str(mantle, 'peerVpcCidr', 'mantle');
  req((mantle.peerVpcCidr as string) !== (network.vpcCidr as string), 'mantle.peerVpcCidr must not overlap network.vpcCidr');
  req(typeof mantle.enablePrivateEndpoint === 'boolean', 'mantle.enablePrivateEndpoint must be boolean');

  const guardrail = obj('guardrail');
  req(typeof guardrail.enabled === 'boolean', 'guardrail.enabled must be boolean');

  return raw as AppConfig;
}
```

**WHY:**
- **Conditional required-value validation is the key.** If `certMode='acm-dns'`, then `domainName`/`hostedZoneId`/`hostedZoneName` are required; if `acm-arn`, then `certificateArn` is required. This branch validation pairs with the `useCustomDomain` derived value in `bin/app.ts`.
- **A validator with no external dependencies** — structural validation using only the `req/obj/num/str` helpers. By not adding zod or similar, it reduces the dependency surface. It throws immediately on the first violation (fail-fast).

---

## 0-3. Single source of constants — `lib/config/constants.ts`

The single source of truth for cross-stack constants. Literals of these values must not appear elsewhere; they are always imported from here.

```typescript
export const AWS_REGION = 'us-east-2';
export const PROJECT_PREFIX = 'codeagent-gov';
export const ENV_NAME = 'dev';

/** Helper to namespace resource ids / param names consistently. */
export const ns = (suffix: string): string => `${PROJECT_PREFIX}-${ENV_NAME}-${suffix}`;

/** SSM Parameter Store paths used for runtime cross-stack wiring (not deploy-time refs). */
export const SSM = {
  LITELLM_INTERNAL_URL: `/${PROJECT_PREFIX}/${ENV_NAME}/litellm/internal-url`,
} as const;

/**
 * Model aliases (what clients request) and backend routing (LiteLLM litellm_params.model).
 */
export const MODELS = {
  CLAUDE_OPUS:   { litellmName: 'claude-opus-4-8',   backend: 'bedrock/us.anthropic.claude-opus-4-8' },
  CLAUDE_SONNET: { litellmName: 'claude-sonnet-4-6', backend: 'bedrock/us.anthropic.claude-sonnet-4-6' },
  CLAUDE_HAIKU:  { litellmName: 'claude-haiku-4-5',  backend: 'bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0' },
  CLAUDE_FABLE:  { litellmName: 'claude-fable-5',    backend: 'bedrock/us.anthropic.claude-fable-5' },
  GPT55: { litellmName: 'gpt-5.5', backend: 'bedrock_mantle/openai.gpt-5.5' }, // responses API, Bearer token auth
  GPT54: { litellmName: 'gpt-5.4', backend: 'bedrock_mantle/openai.gpt-5.4' }, // economy tier (~2x cheaper)
} as const;

/** Only assumed-role principals from IAM Identity Center are accepted. */
export const SSO_ARN_PREFIX = 'AWSReservedSSO_';

/** Network. */
export const VPC_CIDR = '10.0.0.0/16';
export const VPC_MAX_AZS = 2;
export const VPC_NAT_GATEWAYS = 1;

/** Container ports. */
export const PORTS = {
  LITELLM: 4000,
  LANGFUSE: 3000,
  MCP: 8000,
  AURORA: 5432,
  HTTPS: 443,
} as const;

/** DynamoDB key-cache table conventions (Auth plane). */
export const DYNAMO = {
  PK_PATTERN: 'USER#{user_id}',
  SK_VIRTUAL_KEY: 'VIRTUAL_KEY',
  TTL_ATTRIBUTE: 'ttl',
} as const;

/** Token Service response contract. */
export const TOKEN_SERVICE = { RESPONSE_KEY: 'api_key' } as const;
```

**WHY — cross-layer mapping:**
- **`ns()` namespaces every resource name as `codeagent-gov-dev-*`** — for environment isolation/collision avoidance.
- **`SSM.LITELLM_INTERNAL_URL`**: LiteLLM writes its internal URL to this path (`ssm.StringParameter`), and the Auth Lambda reads it from this path. A single constant guarantees both sides see the same path.
- **`MODELS`' two-tier structure of `litellmName` (the alias clients request) vs `backend` (LiteLLM `litellm_params.model` routing)** — clients use a stable alias, the backend uses the Bedrock provider notation. `bedrock/` is SigV4 Converse; `bedrock_mantle/` is the responses API route.
- **`SSO_ARN_PREFIX = 'AWSReservedSSO_'`**: the Auth Lambda checks whether the caller ARN has this prefix to allow only SSO identities (non-SSO → 403).
- **`PORTS`**: the SG chain (Network), container ports (LiteLLM/Langfuse), and VPC Origin (CDN) all share these constants.

---

## 1. NetworkStack — VPC + SG chain + VPC endpoints

VPC (2 AZ, 1 NAT), the full security-group chain, and VPC endpoints that keep Bedrock/AgentCore/AWS API traffic inside the AWS network. The root stack — it depends on nothing.

```typescript
export class NetworkStack extends cdk.Stack implements NetworkExports {
  public readonly vpc: ec2.IVpc;
  public readonly litellmServiceSecurityGroup: ec2.ISecurityGroup;
  public readonly langfuseServiceSecurityGroup: ec2.ISecurityGroup;
  public readonly auroraSecurityGroup: ec2.ISecurityGroup;
  public readonly albSecurityGroup: ec2.ISecurityGroup;
  public readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  public readonly agentcoreEndpointSecurityGroup: ec2.ISecurityGroup;
  public readonly interfaceVpcEndpointSecurityGroup: ec2.ISecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);
    const { config } = props;

    // ---- VPC: public / private-with-egress / isolated ----------------------
    const vpc = new ec2.Vpc(this, 'Vpc', {
      ipAddresses: ec2.IpAddresses.cidr(config.vpcCidr),
      maxAzs: config.maxAzs,
      natGateways: config.natGateways,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 22 },
        { name: 'isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });
    this.vpc = vpc;

    // ---- Security group chain ----------------------------------------------
    const sg = (logical: string, description: string, allowOutbound = true): ec2.SecurityGroup =>
      new ec2.SecurityGroup(this, logical, {
        vpc,
        securityGroupName: ns(logical.toLowerCase()),
        description,
        allowAllOutbound: allowOutbound,
      });

    const albSg = sg('AlbSg', 'ALB: internet-facing HTTPS');
    const litellmSg = sg('LiteLlmSg', 'ECS: LiteLLM tasks');
    const langfuseSg = sg('LangfuseSg', 'ECS: Langfuse tasks');
    const lambdaSg = sg('LambdaSg', 'Token Service Lambda (VPC-placed)');
    const auroraSg = sg('AuroraSg', 'Aurora Serverless v2', false);
    const vpceSg = sg('VpceSg', 'Interface VPC Endpoints', false);
    const agentcoreSg = sg('AgentCoreEndpointSg', 'bedrock-agentcore interface endpoint', false);

    // ALB is internal — CloudFront VPC Origin connects directly inside VPC.
    // No internet ingress needed. CloudFront VPC Origin traffic uses the VPC
    // network, so we allow from the VPC CIDR on the service ports.
    albSg.addIngressRule(ec2.Peer.ipv4(config.vpcCidr), ec2.Port.tcp(PORTS.LITELLM), 'CloudFront VPC Origin to LiteLLM');
    albSg.addIngressRule(ec2.Peer.ipv4(config.vpcCidr), ec2.Port.tcp(PORTS.LANGFUSE), 'CloudFront VPC Origin to Langfuse');

    // LiteLLM <- ALB on 4000.
    litellmSg.addIngressRule(albSg, ec2.Port.tcp(PORTS.LITELLM), 'ALB to LiteLLM');
    // Langfuse <- ALB on 3000 (only relevant when Langfuse enabled).
    langfuseSg.addIngressRule(albSg, ec2.Port.tcp(PORTS.LANGFUSE), 'ALB to Langfuse');

    // Aurora <- LiteLLM, Langfuse, Lambda on 5432.
    auroraSg.addIngressRule(litellmSg, ec2.Port.tcp(PORTS.AURORA), 'LiteLLM to Aurora');
    auroraSg.addIngressRule(langfuseSg, ec2.Port.tcp(PORTS.AURORA), 'Langfuse to Aurora');
    auroraSg.addIngressRule(lambdaSg, ec2.Port.tcp(PORTS.AURORA), 'Lambda to Aurora');

    // Interface endpoints <- LiteLLM + Lambda on 443.
    vpceSg.addIngressRule(litellmSg, ec2.Port.tcp(PORTS.HTTPS), 'LiteLLM to interface endpoints');
    vpceSg.addIngressRule(lambdaSg, ec2.Port.tcp(PORTS.HTTPS), 'Lambda to interface endpoints');
    // AgentCore endpoint <- LiteLLM on 443.
    agentcoreSg.addIngressRule(litellmSg, ec2.Port.tcp(PORTS.HTTPS), 'LiteLLM to bedrock-agentcore');

    this.albSecurityGroup = albSg;
    this.litellmServiceSecurityGroup = litellmSg;
    this.langfuseServiceSecurityGroup = langfuseSg;
    this.lambdaSecurityGroup = lambdaSg;
    this.auroraSecurityGroup = auroraSg;
    this.interfaceVpcEndpointSecurityGroup = vpceSg;
    this.agentcoreEndpointSecurityGroup = agentcoreSg;

    // ---- Gateway endpoints (free) ------------------------------------------
    vpc.addGatewayEndpoint('S3Endpoint', { service: ec2.GatewayVpcEndpointAwsService.S3 });
    vpc.addGatewayEndpoint('DynamoDbEndpoint', { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB });

    // ---- Interface endpoints -----------------------------------------------
    const privateSubnets = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };
    const addIf = (logical: string, service: ec2.InterfaceVpcEndpointAwsService, group: ec2.ISecurityGroup): void => {
      vpc.addInterfaceEndpoint(logical, {
        service,
        securityGroups: [group as ec2.SecurityGroup],
        subnets: privateSubnets,
        privateDnsEnabled: true,
      });
    };
    addIf('BedrockRuntimeEndpoint', ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME, vpceSg);
    addIf('SecretsManagerEndpoint', ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER, vpceSg);
    addIf('SsmEndpoint', ec2.InterfaceVpcEndpointAwsService.SSM, vpceSg);
    addIf('EcrApiEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR, vpceSg);
    addIf('EcrDockerEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER, vpceSg);
    addIf('LogsEndpoint', ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS, vpceSg);

    // bedrock-agentcore endpoint (service name may not be in enum across versions).
    new ec2.InterfaceVpcEndpoint(this, 'BedrockAgentCoreEndpoint', {
      vpc,
      service: new ec2.InterfaceVpcEndpointService(
        `com.amazonaws.${this.region}.bedrock-agentcore`,
        PORTS.HTTPS,
      ),
      securityGroups: [agentcoreSg],
      subnets: privateSubnets,
      privateDnsEnabled: true,
    });

    new cdk.CfnOutput(this, 'VpcId', { value: vpc.vpcId });
  }
}
```

**WHY — network design:**
- **3-tier subnets**: `public` (NAT/none), `private-with-egress` (ECS/Lambda/endpoints — outbound via NAT), `isolated` (Aurora — internet-disconnected). The DB never touches the internet.
- **SG chain = least privilege.** Each tier receives ingress only "where needed":
  - `albSg` ← VPC CIDR (4000/3000) — the CloudFront VPC Origin enters over the VPC network, so internet ingress is unnecessary. **Note: the comment says "internet-facing" but it is actually an internal ALB** (a leftover code comment).
  - `litellmSg`/`langfuseSg` ← only `albSg`
  - `auroraSg` ← only 5432 from the litellm/langfuse/lambda SGs
  - `vpceSg` ← 443 from litellm/lambda
  - `agentcoreSg` ← 443 from litellm
- **SGs with `allowAllOutbound=false`**: aurora/vpce/agentcore block outbound to narrow the data plane. ALB/ECS/Lambda are true (outbound required).
- **Gateway endpoints (S3/DynamoDB) are free**, while Interface endpoints (bedrock-runtime/secrets/ssm/ecr/ecr-docker/logs) are paid but reach the AWS APIs **without traversing the NAT** → a win on both cost and security.
- **The AgentCore endpoint may not be in the enum**, so the service name is specified directly via `InterfaceVpcEndpointService(com.amazonaws.{region}.bedrock-agentcore)`. A pattern resilient to CDK version differences.
- **Cross-layer mapping**: the SGs created here are exposed as `NetworkExports`, and the Data (aurora SG)/LiteLLM (litellm+alb SG)/Langfuse/Auth (lambda SG) stacks receive them as props and reuse them. The reason SGs are not created separately in each stack = ingress rules are tied together by SG object references, so the whole chain must be defined in one place (Network) to keep consistency.

---

## 2. DataStack — Aurora Serverless v2 + per-app secrets + db-init

Places Aurora Serverless v2 (PostgreSQL) in the isolated subnet and creates separate secrets for the LiteLLM/Langfuse DBs. The Langfuse user/DB is created by a Custom Resource (`db-init`) using master credentials.

```typescript
export class DataStack extends cdk.Stack implements DataExports {
  public readonly cluster: rds.IDatabaseCluster;
  public readonly litellmDbSecret: secretsmanager.ISecret;
  public readonly langfuseDbSecret: secretsmanager.ISecret;
  public readonly clusterEndpointHostname: string;
  public readonly clusterPort: number;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);
    const { config, network } = props;

    const cluster = new rds.DatabaseCluster(this, 'Aurora', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.of(config.engineVersion, config.engineVersion.split('.')[0]),
      }),
      vpc: network.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [network.auroraSecurityGroup as ec2.SecurityGroup],
      serverlessV2MinCapacity: config.minCapacityAcu,
      serverlessV2MaxCapacity: config.maxCapacityAcu,
      writer: rds.ClusterInstance.serverlessV2('writer'),
      defaultDatabaseName: 'litellm',
      storageEncrypted: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // dev sample
    });
    this.cluster = cluster;
    this.clusterEndpointHostname = cluster.clusterEndpoint.hostname;
    this.clusterPort = PORTS.AURORA;

    // The cluster's master secret backs the LiteLLM DB connection.
    this.litellmDbSecret = cluster.secret!;

    // Separate generated secret for Langfuse (own DB user concept; for the dev
    // sample we generate credentials and reuse the same cluster host/port).
    this.langfuseDbSecret = new secretsmanager.Secret(this, 'LangfuseDbSecret', {
      secretName: ns('langfuse-db'),
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          username: 'langfuse',
          host: cluster.clusterEndpoint.hostname,
          port: PORTS.AURORA,
          dbname: 'langfuse',
        }),
        generateStringKey: 'password',
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    // ---- DB Init Custom Resource: create langfuse user + database -------------
    // Uses master credentials to run CREATE USER / CREATE DATABASE on Aurora.
    const dbInitFn = new lambda.Function(this, 'DbInitFn', {
      functionName: ns('db-init'),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'db-init')),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      vpc: network.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [network.lambdaSecurityGroup as ec2.SecurityGroup],
    });
    cluster.secret!.grantRead(dbInitFn);
    this.langfuseDbSecret.grantRead(dbInitFn);

    const dbInitProvider = new cr.Provider(this, 'DbInitProvider', {
      onEventHandler: dbInitFn,
    });
    const dbInit = new cdk.CustomResource(this, 'DbInit', {
      serviceToken: dbInitProvider.serviceToken,
      properties: {
        MasterSecretArn: cluster.secret!.secretArn,
        DatabaseName: 'langfuse',
        Username: 'langfuse',
        PasswordSecretArn: this.langfuseDbSecret.secretArn,
      },
    });
    dbInit.node.addDependency(cluster);

    new cdk.CfnOutput(this, 'ClusterEndpoint', { value: cluster.clusterEndpoint.hostname });
  }
}
```

**WHY — data design:**
- **A single Aurora cluster backs both LiteLLM + Langfuse.** `defaultDatabaseName: 'litellm'` is connected via the cluster master secret (`cluster.secret!`), and db-init creates a separate DB/user for Langfuse. Serverless v2 (min/max ACU) scales down to nearly 0 in dev → cost savings.
- **`litellmDbSecret = cluster.secret!`**: the master secret auto-generated by Aurora is used directly for the LiteLLM connection. **The correct pattern** — no plaintext password.
- **`langfuseDbSecret` is created via `generateSecretString`** — CDK generates the password and it is never exposed in plaintext in code/templates. (This pattern is reused in the Langfuse stack's anti-pattern fix — see §5.)
- **db-init Custom Resource**: since Aurora has no DB/user for Langfuse at bootstrap, a Lambda that received the master secret via `grantRead` runs `CREATE USER/CREATE DATABASE`. `dbInit.node.addDependency(cluster)` ensures it runs after the cluster is created.
- **`storageEncrypted: true`** is mandatory. **`removalPolicy: DESTROY` is dev-sample only** — in prod use `RETAIN` + deletion protection.
- **Cross-layer mapping**: `clusterEndpointHostname`/`clusterPort`/the two secrets are exposed as `DataExports` → LiteLLM (litellmDbSecret) and Langfuse (langfuseDbSecret + host/port) are injected as ECS `secrets`.

---

## 3. GuardrailStack — Bedrock Guardrail (content filter + denied topics + PII)

The central content/PII policy that LiteLLM references by ID/version. If `enabled=false`, it emits an empty ID so LiteLLM operates without a guardrail.

```typescript
export class GuardrailStack extends cdk.Stack implements GuardrailExports {
  public readonly guardrailId: string;
  public readonly guardrailVersion: string;

  constructor(scope: Construct, id: string, props: GuardrailStackProps) {
    super(scope, id, props);

    if (!props.enabled) {
      this.guardrailId = '';
      this.guardrailVersion = '';
      return;
    }

    const guardrail = new bedrock.CfnGuardrail(this, 'Guardrail', {
      name: ns('content-guard'),
      description: 'Code agent governance guardrail: content filter + PII + denied topics',
      blockedInputMessaging: 'Your request was blocked by the security policy. Please remove sensitive information or prohibited content and try again.',
      blockedOutputsMessaging: 'The response was blocked by the security policy due to potentially sensitive content.',

      // Content policy: block harmful content
      contentPolicyConfig: {
        filtersConfig: [
          { type: 'HATE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'INSULTS', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'SEXUAL', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'VIOLENCE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'MISCONDUCT', inputStrength: 'HIGH', outputStrength: 'HIGH' },
        ],
      },

      // Denied topics
      topicPolicyConfig: {
        topicsConfig: [
          {
            name: 'HackingTools',
            definition: 'Requests to create hacking tools, exploit code, SQL injection payloads, malware, ransomware, or any code intended to compromise systems without authorization.',
            type: 'DENY',
            examples: [
              'Write a SQL injection attack',
              'Create a keylogger',
              'Generate ransomware code',
              'How to bypass authentication',
            ],
          },
          {
            name: 'DataExfiltration',
            definition: 'Requests to extract, steal, or exfiltrate proprietary data, credentials, or internal system information.',
            type: 'DENY',
            examples: [
              'Extract all customer data from the database',
              'How to steal API keys from environment variables',
              'Write code to scrape internal documents',
            ],
          },
        ],
      },

      // PII / Sensitive information policy
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          { type: 'CREDIT_DEBIT_CARD_NUMBER', action: 'BLOCK' },
          { type: 'US_SOCIAL_SECURITY_NUMBER', action: 'BLOCK' },
          { type: 'AWS_ACCESS_KEY', action: 'BLOCK' },
          { type: 'AWS_SECRET_KEY', action: 'BLOCK' },
        ],
        regexesConfig: [
          {
            name: 'GenericAPIKey',
            description: 'Detect common API key patterns (sk-, key-, token-)',
            pattern: '(?i)(sk-|api[_-]?key|secret[_-]?key|token)[\\s=:]+["\']?[A-Za-z0-9+/=_-]{20,}',
            action: 'BLOCK',
          },
        ],
      },
    });

    this.guardrailId = guardrail.attrGuardrailId;
    this.guardrailVersion = guardrail.attrVersion;

    new cdk.CfnOutput(this, 'GuardrailId', { value: this.guardrailId });
    new cdk.CfnOutput(this, 'GuardrailVersion', { value: this.guardrailVersion });
  }
}
```

**WHY — guardrail design:**
- **If `enabled=false`, it exports an empty-string ID and returns immediately** — the Guardrail resource itself is not created. When LiteLLM receives an empty ID, it skips guardrail application. A toggleable security layer.
- **Defense in depth across three policies:**
  1. `contentPolicyConfig` — blocks HATE/INSULTS/SEXUAL/VIOLENCE/MISCONDUCT at HIGH strength on both input and output.
  2. `topicPolicyConfig` — blocks **code-agent-specific denied topics** like hacking tools/data exfiltration via definitions+examples. (Since this is a code-generation gateway, blocking malicious-code generation requests is essential.)
  3. `sensitiveInformationPolicyConfig` — BLOCKs card numbers/SSNs/AWS keys + uses regexes to also block generic API key patterns (`sk-`, `api_key`, etc.).
- **Exports `attrGuardrailId`/`attrVersion`** → passed to the LiteLLM container env vars `BEDROCK_GUARDRAIL_ID`/`BEDROCK_GUARDRAIL_VERSION`, so LiteLLM applies `bedrock:ApplyGuardrail` on every Claude request.
- **Cross-layer mapping**: the reason this stack is placed "before" LiteLLM = the LiteLLM props require `guardrailId`/`guardrailVersion` (see `bin/app.ts`).

---

## 4. LiteLLMStack — the governance gateway core (ECS Fargate + internal ALB)

ECS Fargate runs the LiteLLM proxy behind an internal ALB. TLS is terminated at CloudFront and the ALB receives HTTP only. It owns the master key secret and publishes the internal URL to SSM for the Auth plane to consume at runtime.

```typescript
export class LiteLLMStack extends cdk.Stack implements LiteLLMExports {
  public readonly loadBalancer: elbv2.IApplicationLoadBalancer;
  public readonly publicHttpsUrl: string;
  public readonly taskRole: iam.IRole;
  public readonly masterKeySecret: secretsmanager.ISecret;
  public readonly internalUrlSsmParameterName: string;

  constructor(scope: Construct, id: string, props: LiteLLMStackProps) {
    super(scope, id, props);
    const { config, agentcore, guardrailId, guardrailVersion, network, data } = props;

    // ---- Master key (LiteLLM admin) ----------------------------------------
    // Master key stored in Secrets Manager (value from config, not auto-generated).
    const masterKey = new secretsmanager.Secret(this, 'MasterKey', {
      secretName: ns('litellm-admin-key'),
      secretStringValue: cdk.SecretValue.unsafePlainText(config.masterKey),
    });
    this.masterKeySecret = masterKey;

    // ---- ECS cluster + task -------------------------------------------------
    const cluster = new ecs.Cluster(this, 'Cluster', { vpc: network.vpc, containerInsights: true });

    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'LiteLLM task role: invoke Bedrock + sign AgentCore MCP (SigV4)',
    });
    // Bedrock model invocation (Claude runtime + mantle GPT-5.5).
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
        'bedrock:ApplyGuardrail',
        'bedrock:CallWithBearerToken',
        'bedrock-mantle:CreateInference',
        'bedrock-mantle:*',
      ],
      resources: ['*'], // dev sample; prod TODO: scope to specific model ARNs
    }));
    // AgentCore Web Search gateway invocation (MCP via SigV4, cross-region us-east-1).
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'],
      resources: ['*'],
    }));
    // Bedrock Mantle (GPT-5.x) models are AWS Marketplace offerings → first-call auto-subscribe.
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['aws-marketplace:Subscribe', 'aws-marketplace:ViewSubscriptions', 'aws-marketplace:Unsubscribe'],
      resources: ['*'],
    }));
    this.taskRole = taskRole;

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: config.cpu,
      memoryLimitMiB: config.memoryLimitMiB,
      taskRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LiteLlmLogs', {
      logGroupName: `/ecs/${ns('litellm')}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    taskDef.addContainer('litellm', {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, '..', 'services', 'litellm')),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'litellm', logGroup }),
      portMappings: [{ containerPort: PORTS.LITELLM }],
      environment: {
        LITELLM_MODE: 'PRODUCTION',
        // PROXY_BASE_URL must be the PUBLIC URL the browser uses — the LiteLLM
        // Admin UI (SPA) builds absolute redirects from it. With a custom domain
        // this is config.domainName. WITHOUT a domain, set config.domainName to
        // the CloudFront distribution domain (dxxxx.cloudfront.net) AFTER the CDN
        // stack is deployed (two-phase), then redeploy LiteLLM — otherwise the UI
        // redirects the browser to the placeholder host and breaks.
        PROXY_BASE_URL: `https://${config.domainName}`,
        AWS_REGION: this.region,
        STORE_MODEL_IN_DB: 'True',
        CLAUDE_OPUS_MODEL: MODELS.CLAUDE_OPUS.litellmName,
        CLAUDE_OPUS_BACKEND: MODELS.CLAUDE_OPUS.backend,
        CLAUDE_SONNET_MODEL: MODELS.CLAUDE_SONNET.litellmName,
        CLAUDE_SONNET_BACKEND: MODELS.CLAUDE_SONNET.backend,
        CLAUDE_HAIKU_MODEL: MODELS.CLAUDE_HAIKU.litellmName,
        CLAUDE_HAIKU_BACKEND: MODELS.CLAUDE_HAIKU.backend,
        CLAUDE_FABLE_MODEL: MODELS.CLAUDE_FABLE.litellmName,
        CLAUDE_FABLE_BACKEND: MODELS.CLAUDE_FABLE.backend,
        GPT55_MODEL: MODELS.GPT55.litellmName,
        GPT55_BACKEND: MODELS.GPT55.backend,
        GPT54_MODEL: MODELS.GPT54.litellmName,
        GPT54_BACKEND: MODELS.GPT54.backend,
        // Bedrock Mantle (GPT-5.x) reached in us-east-1 over cross-region VPC peering.
        // Pin via the vars the provider actually reads (#29788). MANTLE_REGION is NOT read.
        BEDROCK_MANTLE_REGION: mantleRegion,
        BEDROCK_MANTLE_API_BASE: `https://bedrock-mantle.${mantleRegion}.api.aws`,
        MANTLE_REGION: mantleRegion, // human-readable alias only (not consumed by litellm)
        // AgentCore Web Search gateway (MCP, SigV4). Cross-region exports from
        // AgentCoreGatewayStack (us-east-1); requires crossRegionReferences: true.
        WEBSEARCH_GATEWAY_URL: agentcoreGateway.gatewayUrl,
        WEBSEARCH_GATEWAY_REGION: agentcoreGateway.webSearchRegion,
        BEDROCK_GUARDRAIL_ID: guardrailId,
        BEDROCK_GUARDRAIL_VERSION: guardrailVersion,
        // Langfuse tracing (keys match LANGFUSE_INIT_* in Langfuse stack)
        LANGFUSE_PUBLIC_KEY: 'lf_pk_CHANGE_ME',
        LANGFUSE_SECRET_KEY: 'lf_sk_CHANGE_ME',
        LANGFUSE_HOST: 'https://langfuse.example.com',
      },
      secrets: {
        LITELLM_MASTER_KEY: ecs.Secret.fromSecretsManager(masterKey),
        DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'password'),
        DATABASE_HOST: ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'host'),
        DATABASE_USER: ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'username'),
      },
      healthCheck: {
        command: ['CMD-SHELL', `python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORTS.LITELLM}/health/liveliness')" || exit 1`],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 3,
        startPeriod: cdk.Duration.seconds(90),
      },
    });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: config.desiredCount,
      securityGroups: [network.litellmServiceSecurityGroup as ec2.SecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      healthCheckGracePeriod: cdk.Duration.seconds(60),
    });

    // All models (Claude + Bedrock Mantle GPT-5.5/5.4) are config-defined and
    // authenticate via SigV4 using the ECS Task Role — no bearer tokens, nothing to
    // rotate, no EventBridge scheduler. Mantle SigV4 comes from the #29788 overlay in
    // the image (services/litellm/Dockerfile).

    // ---- ALB: internal (CloudFront VPC Origin handles external access) --------
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: network.vpc,
      internetFacing: false,
      securityGroup: network.albSecurityGroup as ec2.SecurityGroup,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
    this.loadBalancer = alb;

    // HTTP listener only — CloudFront terminates TLS, ALB receives HTTP.
    const httpListener = alb.addListener('Http', {
      port: PORTS.LITELLM,
      protocol: elbv2.ApplicationProtocol.HTTP,
    });
    httpListener.addTargets('LiteLlmTarget', {
      port: PORTS.LITELLM,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        path: '/health/liveliness',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    // URL will be via CloudFront (CDN stack manages Route53 + TLS).
    this.publicHttpsUrl = `https://${config.domainName}/v1`;

    // ---- Publish internal URL to SSM (runtime cross-stack wiring) -----------
    this.internalUrlSsmParameterName = SSM.LITELLM_INTERNAL_URL;
    new ssm.StringParameter(this, 'InternalUrlParam', {
      parameterName: SSM.LITELLM_INTERNAL_URL,
      stringValue: `http://${alb.loadBalancerDnsName}:${PORTS.LITELLM}`,
      description: 'LiteLLM internal base URL for the Token Service',
    });

    new cdk.CfnOutput(this, 'AlbDns', { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'AdminUiUrl', { value: `${this.publicHttpsUrl.replace('/v1', '')}/ui/` });
  }
}
```

**WHY — gateway essentials:**
- **Tokenless model authentication is the heart of the design.** The Task Role calls `bedrock:*` (Claude Converse + ApplyGuardrail), `bedrock-mantle:*` (GPT-5.x), and `bedrock-agentcore:InvokeGateway` (AgentCore Web Search) via SigV4. **There is no API key to store/rotate.** No EventBridge token-refresh scheduler is needed either. (Mantle auto-subscribes on first call via `aws-marketplace:Subscribe`.)
- **The `secrets` vs `environment` distinction matters (security):**
  - `secrets` (Secrets Manager injection): `LITELLM_MASTER_KEY`, `DATABASE_PASSWORD/HOST/USER` — all sensitive values go through `ecs.Secret.fromSecretsManager`. Plaintext is not exposed in the task definition.
  - `environment` (plaintext): only non-sensitive values like model aliases/region/SSM names/guardrail ID.
  - **⚠️ However, 3 Langfuse keys such as `LANGFUSE_PUBLIC_KEY: 'lf_pk_CHANGE_ME'` are hardcoded in plaintext in `environment`** → an anti-pattern. (A fix is proposed in §5.)
- **`internalFacing: false` (internal ALB) + HTTP listener only.** Since TLS is terminated at CloudFront, the ALB only receives HTTP on port 4000. The ALB is in a private-with-egress subnet and is not exposed to the internet.
- **SSM publishing (runtime wiring):** writes `http://{albDns}:4000` to `SSM.LITELLM_INTERNAL_URL`. The Auth Lambda looks it up at runtime by this name → avoids a LiteLLM↔Auth deploy-time cross-ref (connected to the `internalUrlSsmParameterName` design in interface §0-1).
- **ARM64 (Graviton) + circuitBreaker (rollback) + health-check grace 90s** — cost/stability. LiteLLM boots slowly, hence `startPeriod: 90s`.
- **Cross-layer mapping**: `masterKeySecret` (→Auth grantRead), `loadBalancer` (→CDN VPC Origin), `publicHttpsUrl`/`internalUrlSsmParameterName` (→Auth/Observability) flow as `LiteLLMExports`.

---

## 5. LangfuseStack (conditional) — self-hosted observability + ⚠️ plaintext-secret anti-pattern

Created only when `enableLangfuse=true`. Runs Postgres-backed Langfuse v2 as Fargate behind an internal ALB. LiteLLM sends traces here.

```typescript
export class LangfuseStack extends cdk.Stack implements LangfuseExports {
  public readonly langfuseUrl: string;
  public readonly loadBalancer: elbv2.IApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: LangfuseStackProps) {
    super(scope, id, props);
    const { config, network, data } = props;

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc: network.vpc });

    // NEXTAUTH_SECRET / SALT for Langfuse session signing.
    const appSecret = new secretsmanager.Secret(this, 'AppSecret', {
      secretName: ns('langfuse-app'),
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ salt: 'langfuse' }),
        generateStringKey: 'nextauthSecret',
        excludePunctuation: true,
        passwordLength: 48,
      },
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: config.cpu,
      memoryLimitMiB: config.memoryLimitMiB,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'LangfuseLogs', {
      logGroupName: `/ecs/${ns('langfuse')}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    taskDef.addContainer('langfuse', {
      image: ecs.ContainerImage.fromRegistry('langfuse/langfuse:2'),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'langfuse', logGroup }),
      portMappings: [{ containerPort: PORTS.LANGFUSE }],
      environment: {
        HOSTNAME: '0.0.0.0',
        PORT: String(PORTS.LANGFUSE),
        TELEMETRY_ENABLED: 'false',
        // Public URL for NextAuth callbacks/redirects. Must be the URL the browser
        // uses; a placeholder makes login bounce to a dead host (same class as
        // LiteLLM PROXY_BASE_URL). Domain-less: set config.langfuse.publicUrl to the
        // Langfuse CloudFront domain (two-phase) then redeploy Langfuse.
        NEXTAUTH_URL: config.publicUrl ?? 'https://langfuse.example.com',
        // Headless initialization — auto-create org, project, API keys, admin user
        LANGFUSE_INIT_ORG_ID: 'codeagent-gov',
        LANGFUSE_INIT_ORG_NAME: 'Code Agent Governance',
        LANGFUSE_INIT_PROJECT_ID: 'llm-gateway',
        LANGFUSE_INIT_PROJECT_NAME: 'LLM Gateway Traces',
        LANGFUSE_INIT_PROJECT_PUBLIC_KEY: 'lf_pk_CHANGE_ME',
        LANGFUSE_INIT_PROJECT_SECRET_KEY: 'lf_sk_CHANGE_ME',
        LANGFUSE_INIT_USER_EMAIL: 'admin@example.com',
        LANGFUSE_INIT_USER_NAME: 'Admin',
        LANGFUSE_INIT_USER_PASSWORD: 'Admin123!',
        DATABASE_HOST: data.clusterEndpointHostname,
        DATABASE_PORT: String(data.clusterPort),
        DATABASE_NAME: 'langfuse',
      },
      secrets: {
        DATABASE_USERNAME: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'username'),
        DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'password'),
        NEXTAUTH_SECRET: ecs.Secret.fromSecretsManager(appSecret, 'nextauthSecret'),
        SALT: ecs.Secret.fromSecretsManager(appSecret, 'salt'),
      },
    });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: config.desiredCount,
      securityGroups: [network.langfuseServiceSecurityGroup as ec2.SecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      healthCheckGracePeriod: cdk.Duration.seconds(60),
    });

    // Internal ALB (ops-only access; not internet-facing).
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: network.vpc,
      internetFacing: false,
      securityGroup: network.langfuseServiceSecurityGroup as ec2.SecurityGroup,
    });
    const listener = alb.addListener('Http', { port: PORTS.LANGFUSE, protocol: elbv2.ApplicationProtocol.HTTP });
    listener.addTargets('LangfuseTarget', {
      port: PORTS.LANGFUSE,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        path: '/api/public/health',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    this.loadBalancer = alb;
    this.langfuseUrl = `http://${alb.loadBalancerDnsName}:${PORTS.LANGFUSE}`;
    new cdk.CfnOutput(this, 'LangfuseUrl', { value: this.langfuseUrl });
  }
}
```

**WHY — what is done correctly:**
- **Conditional stack** — if `enableLangfuse=false` it is not even instantiated (`bin/app.ts`). A PoC that does not need observability reduces surface/cost.
- **`appSecret` (NEXTAUTH_SECRET/SALT) is created via `generateSecretString`** → CDK creates the session-signing keys and injects them via `ecs.Secret`. **This part is the correct pattern.**
- **DB credentials (`DATABASE_USERNAME/PASSWORD`) are also injected via `ecs.Secret` from `data.langfuseDbSecret`** — correct.
- **internal ALB (ops-only)** — not internet-exposed. External access is only through the CDN stack.

### ⚠️ Anti-pattern — hardcoding plaintext secrets

This stack's `environment` block has **plaintext secrets embedded directly. Never do this:**

```typescript
// ❌ ANTI-PATTERN — plaintext secrets are exposed in the CloudFormation template/console/git
LANGFUSE_INIT_PROJECT_PUBLIC_KEY: 'lf_pk_CHANGE_ME',   // trace ingestion public key
LANGFUSE_INIT_PROJECT_SECRET_KEY: 'lf_sk_CHANGE_ME',   // trace ingestion secret key
LANGFUSE_INIT_USER_PASSWORD: 'Admin123!',              // admin password(!)
```

And the LiteLLM stack also has the **matching plaintext keys** embedded:

```typescript
// ❌ litellm-stack.ts environment block — same anti-pattern
LANGFUSE_PUBLIC_KEY: 'lf_pk_CHANGE_ME',
LANGFUSE_SECRET_KEY: 'lf_sk_CHANGE_ME',
LANGFUSE_HOST: 'https://langfuse.example.com',
```

**What is the problem:**
1. `environment` values are exposed as-is in the ECS task definition → **plaintext CloudFormation template → console/`describe-task-definition`/git**.
2. `Admin123!` is the Langfuse UI **admin account password**. Even behind an internal ALB, plaintext exposure is critical.
3. LiteLLM and Langfuse **redundantly hardcode the same plaintext literal (`lf_pk_CHANGE_ME`) on both sides** → on rotation you must fix both stacks at once, with drift risk.

### ✅ Fix — Secrets Manager + ecs.Secret + place the shared key in an earlier-created stack

**Core principle: the keys shared by LiteLLM and Langfuse (`LANGFUSE_*_KEY`) are created once in a stack created before both (e.g. DataStack or a separate SharedSecretsStack), and both consume them via `grantRead`.**

```typescript
// ✅ Created once in DataStack (or a stack created before LiteLLM/Langfuse)
//    — since it is a shared key, create it ahead of both stacks and export it.
export const langfuseSharedSecret = new secretsmanager.Secret(this, 'LangfuseSharedKeys', {
  secretName: ns('langfuse-shared-keys'),
  generateSecretString: {
    // public key template + auto-generated secret key. No plaintext literals.
    secretStringTemplate: JSON.stringify({ publicKey: `lf_pk_${cdk.Names.uniqueId(this).slice(-8)}` }),
    generateStringKey: 'secretKey',
    excludePunctuation: true,
    passwordLength: 40,
  },
});
// Admin password is auto-generated too:
export const langfuseAdminSecret = new secretsmanager.Secret(this, 'LangfuseAdmin', {
  secretName: ns('langfuse-admin'),
  generateSecretString: { generateStringKey: 'password', excludePunctuation: true, passwordLength: 24,
    secretStringTemplate: JSON.stringify({ email: 'admin@example.com' }) },
});
```

```typescript
// ✅ LangfuseStack — remove the 3 plaintext keys from environment and move them to secrets
// environment: { ... remove LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY/USER_PASSWORD ... }
secrets: {
  DATABASE_USERNAME: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'username'),
  DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'password'),
  NEXTAUTH_SECRET:   ecs.Secret.fromSecretsManager(appSecret, 'nextauthSecret'),
  SALT:              ecs.Secret.fromSecretsManager(appSecret, 'salt'),
  // ↓ all previously-plaintext values now injected from Secrets Manager
  LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ecs.Secret.fromSecretsManager(langfuseSharedSecret, 'publicKey'),
  LANGFUSE_INIT_PROJECT_SECRET_KEY: ecs.Secret.fromSecretsManager(langfuseSharedSecret, 'secretKey'),
  LANGFUSE_INIT_USER_PASSWORD:      ecs.Secret.fromSecretsManager(langfuseAdminSecret, 'password'),
},
```

```typescript
// ✅ LiteLLMStack — consume the same shared secret via grantRead (remove redundant literals)
secrets: {
  LITELLM_MASTER_KEY: ecs.Secret.fromSecretsManager(masterKey),
  DATABASE_PASSWORD:  ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'password'),
  DATABASE_HOST:      ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'host'),
  DATABASE_USER:      ecs.Secret.fromSecretsManager(data.litellmDbSecret, 'username'),
  // ↓ inject the Langfuse trace keys from the shared secret instead of plaintext (both sides see the same source)
  LANGFUSE_PUBLIC_KEY: ecs.Secret.fromSecretsManager(langfuseSharedSecret, 'publicKey'),
  LANGFUSE_SECRET_KEY: ecs.Secret.fromSecretsManager(langfuseSharedSecret, 'secretKey'),
},
// LANGFUSE_HOST is non-sensitive → keep it in environment, but instead of hardcoding
// 'https://langfuse.example.com', inject langfuse?.langfuseUrl or a config value.
```

**WHY — the core of the fix:**
- **Why place the shared key in the "earlier-created stack"**: LiteLLM (the producer side that sends traces) and Langfuse (the consumer side that ingests them) must see the **same key value**. Creating it once in a stack ahead of both lets both reference the same secret via `grantRead` → single source, no drift, fix in one place on rotation.
- Like `cluster.secret`, `generateSecretString` means **plaintext never appears in the CFN template** (CloudFormation generates/stores it at deploy time).
- `ecs.Secret.fromSecretsManager` is injected into the container only at runtime, leaving only the ARN+key in the task definition.

---

## 6. AuthStack — SSO Token Service (API Gateway IAM auth + VPC Lambda + DynamoDB)

An IAM-authenticated API Gateway fronts a Lambda placed in the VPC. The Lambda parses the caller's SSO ARN to enforce the `AWSReservedSSO_` prefix, and returns (or issues) a LiteLLM virtual key cached in DynamoDB. The master key is `grantRead`-only from Secrets Manager.

```typescript
export class AuthStack extends cdk.Stack implements AuthExports {
  public readonly tokenServiceApiUrl: string;
  public readonly tokenServiceInvokeUrl: string;
  public readonly keyCacheTable: dynamodb.ITable;
  public readonly keyCacheTableName: string;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);
    const { config, network, litellm } = props;

    // ---- DynamoDB key cache -------------------------------------------------
    const table = new dynamodb.Table(this, 'KeyCache', {
      tableName: ns('key-cache'),
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: DYNAMO.TTL_ATTRIBUTE,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // dev sample
    });
    this.keyCacheTable = table;
    this.keyCacheTableName = table.tableName;

    // ---- Token Service Lambda (VPC-placed) ----------------------------------
    const fn = new lambda.Function(this, 'TokenService', {
      functionName: ns('token-service'),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'token-service')),
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      vpc: network.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [network.lambdaSecurityGroup as ec2.SecurityGroup],
      environment: {
        CONFIG_TABLE_NAME: table.tableName,
        LITELLM_MASTER_KEY_ARN: litellm.masterKeySecret.secretArn,
        LITELLM_ENDPOINT_SSM: SSM.LITELLM_INTERNAL_URL,
        KEY_CACHE_TTL_SECONDS: String(config.keyCacheTtlSeconds),
        RESPONSE_KEY: TOKEN_SERVICE.RESPONSE_KEY,
        SSO_ARN_PREFIX_REQUIRED: 'true',
      },
    });

    // Least-privilege grants.
    table.grantReadWriteData(fn);
    litellm.masterKeySecret.grantRead(fn);
    ssm.StringParameter.fromStringParameterName(this, 'LiteLlmUrlParam', SSM.LITELLM_INTERNAL_URL)
      .grantRead(fn);

    // ---- API Gateway (IAM auth) ---------------------------------------------
    const api = new apigw.RestApi(this, 'Api', {
      restApiName: ns('token-service'),
      description: 'SSO Token Service — IAM-authenticated virtual key issuance',
      deployOptions: { stageName: 'v1' },
      defaultMethodOptions: { authorizationType: apigw.AuthorizationType.IAM },
    });
    const auth = api.root.addResource('auth');
    const token = auth.addResource('token');
    token.addMethod('POST', new apigw.LambdaIntegration(fn), {
      authorizationType: apigw.AuthorizationType.IAM,
    });

    this.tokenServiceApiUrl = api.url;
    this.tokenServiceInvokeUrl = `${api.url}auth/token`;

    new cdk.CfnOutput(this, 'TokenServiceUrl', { value: this.tokenServiceInvokeUrl });
    new cdk.CfnOutput(this, 'KeyCacheTableName', { value: table.tableName });
  }
}
```

**WHY — auth plane:**
- **API Gateway `AuthorizationType.IAM` (SigV4) is the trust anchor of identity.** After `aws sso login`, a developer calls `/auth/token` with SigV4. API GW passes the caller ARN to the Lambda, which checks the `AWSReservedSSO_` prefix to allow only SSO identities → **SSO enforcement without LiteLLM Enterprise.** (That is why nag's COG4 (Cognito) is intentionally suppressed — §8.)
- **3 least-privilege grants:**
  - `table.grantReadWriteData(fn)` — read/write the virtual-key cache.
  - `litellm.masterKeySecret.grantRead(fn)` — **read only.** Calls `/key/generate` with the LiteLLM-owned master key. The master key is passed not as an env var but **only as an ARN** (`LITELLM_MASTER_KEY_ARN`), and the Lambda reads it at runtime.
  - `ssm ...fromStringParameterName(...).grantRead(fn)` — receives the LiteLLM internal URL **by name** and looks it up at runtime. Avoids a deploy-time cross-ref.
- **DynamoDB: PK/SK + TTL + encryption + PITR.** The `ttl` attribute auto-expires virtual keys → cache freshness. `PAY_PER_REQUEST` means 0 idle cost in dev.
- **VPC placement + lambdaSg** — for the Lambda to reach Aurora/internal ALB/endpoints, it must be inside the VPC (connected to the Network SG chain).
- **Cross-layer mapping**: `tokenServiceInvokeUrl`/`keyCacheTableName` flow to Observability as `AuthExports`. The response body key is `api_key` (constants `TOKEN_SERVICE.RESPONSE_KEY`).

---

## 7. ObservabilityStack — CloudWatch dashboard

A minimal dashboard bundling the LiteLLM ALB, the Token Service API, and (optionally) Langfuse. CloudWatch covers infra/cost; Langfuse covers the prompt/trace level.

```typescript
export class ObservabilityStack extends cdk.Stack implements ObservabilityExports {
  public readonly dashboardName: string;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);
    const { config, litellm, auth, langfuse } = props;

    this.dashboardName = ns('dashboard');

    if (!config.dashboardEnabled) {
      new cdk.CfnOutput(this, 'DashboardName', { value: '(disabled)' });
      return;
    }

    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: this.dashboardName,
    });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          `# ${ns('overview')}`,
          '',
          `**LiteLLM**: ${litellm.publicHttpsUrl}`,
          `**Token Service**: ${auth.tokenServiceInvokeUrl}`,
          langfuse ? `**Langfuse**: ${langfuse.langfuseUrl}` : '**Langfuse**: disabled',
        ].join('\n'),
        width: 24,
        height: 4,
      }),
    );

    // LiteLLM ALB request + 5xx (use the ALB's own metric helpers).
    const alb = litellm.loadBalancer;
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'LiteLLM ALB — Requests',
        left: [alb.metrics.requestCount({ statistic: 'Sum' })],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: 'LiteLLM ALB — 5xx',
        left: [
          alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, { statistic: 'Sum' }),
        ],
        width: 12,
      }),
    );

    new cdk.CfnOutput(this, 'DashboardName', { value: this.dashboardName });
  }
}
```

**WHY:**
- **Toggleable** — if `dashboardEnabled=false`, there is no dashboard, only a `(disabled)` output. Cost control.
- **Uses the `alb.metrics.*` helpers** — pulls metrics directly from the ALB object (LiteLLMExports) rather than specifying dimensions by hand. `langfuse` is optional, so it branches with a ternary.
- **Cross-layer mapping**: receives the exports of all three planes (LiteLLM/Auth/Langfuse) as props and gathers them onto one screen. The division of roles between CloudWatch (infra/cost) + Langfuse (prompt/trace).

---

## 8. CdnStack — CloudFront + VPC Origin (domain-optional)

A CloudFront distribution fronts every UI surface. The ALB is internal (private) and CloudFront connects via VPC Origin → users access only through CloudFront, and the ALB is never exposed to the internet. **The `useCustomDomain` branch turns ACM/Route53/Function on and off.** The ACM certificate is in us-east-1 per the CloudFront requirement.

```typescript
export class CdnStack extends cdk.Stack {
  public readonly litellmDistributionDomain: string;
  public readonly langfuseDistributionDomain: string;

  constructor(scope: Construct, id: string, props: CdnStackProps) {
    super(scope, id, props);

    const useCustomDomain = props.useCustomDomain;

    // Custom-domain mode: issue a DNS-validated ACM cert + Route53 aliases.
    // No-domain mode: CloudFront serves on its default *.cloudfront.net domain
    // with the default CloudFront certificate (no ACM / Route53 / hosted zone).
    let zone: route53.IHostedZone | undefined;
    let cfCert: acm.ICertificate | undefined;

    if (useCustomDomain) {
      zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
        hostedZoneId: props.hostedZoneId,
        zoneName: props.hostedZoneName,
      });

      // ACM cert (this stack is in us-east-1 so cert is in correct region for CF).
      cfCert = new acm.Certificate(this, 'CfCert', {
        domainName: props.litellmDomain,
        subjectAlternativeNames: [props.langfuseDomain],
        validation: acm.CertificateValidation.fromDns(zone),
      });
    }

    // CloudFront Function (viewer-response): origin redirects (e.g. uvicorn's
    // 307 on /ui -> /ui/) emit a Location built with the origin's own scheme/port
    // (http://<host>:4000/ui/), which is unreachable through CloudFront. Rewrite
    // scheme+host to https + the *viewer* Host header so it works for BOTH the
    // custom domain and the default *.cloudfront.net domain (no hardcoded host).
    // MUST be applied in both modes — domain-less is exactly when it's needed.
    const rewriteLocationFn = new cloudfront.Function(this, 'RewriteLocation', {
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  var res = event.response;
  var headers = res.headers;
  if (headers.location && headers.location.value && req.headers.host && req.headers.host.value) {
    headers.location.value = headers.location.value
      .replace(/^https?:\\/\\/[^/]+/, 'https://' + req.headers.host.value);
  }
  return res;
}
`),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });

    // ---- LiteLLM CloudFront Distribution (VPC Origin → internal ALB) ---------
    const litellmVpcOrigin = origins.VpcOrigin.withApplicationLoadBalancer(props.litellmAlb, {
      httpPort: 4000,
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
    });

    const litellmDist = new cloudfront.Distribution(this, 'LiteLlmDist', {
      ...(useCustomDomain ? { domainNames: [props.litellmDomain], certificate: cfCert } : {}),
      defaultBehavior: {
        origin: litellmVpcOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        functionAssociations: [{ function: rewriteLocationFn, eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE }],
      },
    });
    this.litellmDistributionDomain = litellmDist.distributionDomainName;

    if (useCustomDomain && zone) {
      new route53.ARecord(this, 'LiteLlmAlias', {
        zone,
        recordName: props.litellmDomain,
        target: route53.RecordTarget.fromAlias(
          new route53targets.CloudFrontTarget(litellmDist),
        ),
      });
    }

    new cdk.CfnOutput(this, 'LiteLlmCfDomain', {
      value: useCustomDomain
        ? `https://${props.litellmDomain}`
        : `https://${litellmDist.distributionDomainName}`,
    });

    // ---- Langfuse CloudFront Distribution (VPC Origin → internal ALB) --------
    if (props.langfuseAlb) {
      const langfuseVpcOrigin = origins.VpcOrigin.withApplicationLoadBalancer(props.langfuseAlb, {
        httpPort: 3000,
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      });

      const langfuseDist = new cloudfront.Distribution(this, 'LangfuseDist', {
        ...(useCustomDomain ? { domainNames: [props.langfuseDomain], certificate: cfCert } : {}),
        defaultBehavior: {
          origin: langfuseVpcOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
          functionAssociations: [{ function: rewriteLocationFn, eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE }],
        },
      });
      this.langfuseDistributionDomain = langfuseDist.distributionDomainName;

      if (useCustomDomain && zone) {
        new route53.ARecord(this, 'LangfuseAlias', {
          zone,
          recordName: props.langfuseDomain,
          target: route53.RecordTarget.fromAlias(
            new route53targets.CloudFrontTarget(langfuseDist),
          ),
        });
      }

      new cdk.CfnOutput(this, 'LangfuseCfDomain', {
        value: useCustomDomain
          ? `https://${props.langfuseDomain}`
          : `https://${langfuseDist.distributionDomainName}`,
      });
    } else {
      this.langfuseDistributionDomain = '';
    }
  }
}
```

**WHY — domain-optional CDN:**
- **This is the "current domain-optional version".** When `useCustomDomain` (= derived from `certMode==='acm-dns'` in `bin/app.ts`) is:
  - **true**: HostedZone lookup → us-east-1 ACM cert (`fromDns` validation) → 3xx Location-rewrite CloudFront Function → attach `domainNames`+`certificate` to the distribution → create a Route53 A-record alias.
  - **false**: **omit all of the above.** `zone`/`cfCert`/`rewriteLocationFn` stay `undefined`, and the spread `...(useCustomDomain ? {...} : {})` leaves out the domain/certificate props → CloudFront serves on the **default `*.cloudfront.net` + default certificate**. No ACM/Route53/hosted zone needed → works immediately without a domain.
- **VPC Origin is the key.** `origins.VpcOrigin.withApplicationLoadBalancer(internalAlb, { httpPort, HTTP_ONLY })` — CloudFront connects directly to the **internal ALB inside the VPC**. The ALB is not exposed to the internet, and the only external entry is CloudFront (TLS termination). LiteLLM=4000, Langfuse=3000.
- **3xx Location-rewrite Function**: when the LiteLLM UI redirects, it exposes the internal `host:port` (`http://...:4000/ui/`); this rewrites it to the public domain (`https://{litellmDomain}`). Needed only with a custom domain (the default domain is a single host, so it is unnecessary).
- **`CACHING_DISABLED` + `ALL_VIEWER` + `ALLOW_ALL`**: the LLM API/UI is dynamic, so caching is disabled and all headers/methods are forwarded.
- **`langfuseAlb` is optional** — when Langfuse is disabled, the second distribution is not created and `langfuseDistributionDomain=''`.
- **Cross-layer mapping**: `litellmAlb`/`langfuseAlb` are the `loadBalancer` of LiteLLM/Langfuse Exports. The CDN is in us-east-1 and the rest are in us-east-2, so `crossRegionReferences: true` is mandatory.

> **Pitfall:** when `useCustomDomain=false`, `viewerProtocolPolicy: REDIRECT_TO_HTTPS` still forces viewers onto HTTPS, but **the default CloudFront certificate drops the minimum TLS policy to TLSv1** → nag CFR4 fires (intentionally suppressed, §9). In prod, always enforce TLSv1.2_2021 with a custom domain + ACM.

---

## 9. nag-suppressions — `lib/nag-suppressions.ts` (latest, including CFR4/CFR2)

cdk-nag suppressions. **Policy: suppress only what is (a) inherent to the dev sample or (b) an intentional architecture decision.** **Security essentials** like TLS termination, Secrets Manager secrets, and Token Service IAM auth are never suppressed and are **enforced by jest assertions**.

```typescript
export function applyDevSuppressions(stacks: cdk.Stack[]): void {
  for (const stack of stacks) {
    NagSuppressions.addStackSuppressions(stack, [
      {
        id: 'AwsSolutions-IAM5',
        reason:
          'Dev sample. Bedrock InvokeModel and bedrock-agentcore actions use "*" because model/inference-profile ARNs are account/region specific and chosen at runtime. PROD TODO: scope to specific model and gateway ARNs.',
      },
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'AWS managed policies (e.g. Lambda basic execution, ECS task execution) are acceptable for this dev sample. PROD TODO: replace with scoped customer-managed policies.',
      },
      {
        id: 'AwsSolutions-L1',
        reason: 'Lambda runtime is pinned to PYTHON_3_12 (current). Finding is a false positive against the pinned latest runtime.',
      },
      {
        id: 'AwsSolutions-SMG4',
        reason: 'Automatic secret rotation is out of scope for the dev sample (no rotation Lambda). PROD TODO: enable rotation for the master key and DB secrets.',
      },
      {
        id: 'AwsSolutions-ECS2',
        reason: 'Non-secret ECS env vars (model aliases, region, SSM param names) are intentionally passed as environment; all secrets use ecs.Secret from Secrets Manager.',
      },
      {
        id: 'AwsSolutions-VPC7',
        reason: 'VPC flow logs omitted for the dev sample to limit cost. PROD TODO: enable flow logs to CloudWatch/S3.',
      },
      {
        id: 'AwsSolutions-EC23',
        reason: 'Dev sample security groups; the only internet-facing 0.0.0.0/0 ingress is the ALB on 443 (documented). PROD TODO: restrict ingress CIDRs.',
      },
      {
        id: 'AwsSolutions-CFR3',
        reason: 'CloudFront access logging omitted for dev sample (no log bucket provisioned). PROD TODO: enable CF access logs to S3.',
      },
      {
        id: 'AwsSolutions-CFR5',
        reason: 'Origin protocol set to HTTP_ONLY for internal Langfuse ALB (no cert on internal ALB). LiteLLM origin uses HTTPS. Both are behind CloudFront TLS termination.',
      },
      {
        id: 'AwsSolutions-CFR4',
        reason: 'Domain-less dev mode serves CloudFront on its default *.cloudfront.net domain with the default CloudFront viewer certificate, which forces a minimum security policy of TLSv1. PROD TODO: attach a custom domain + ACM cert (certMode=acm-dns) to enforce TLSv1.2_2021.',
      },
      {
        id: 'AwsSolutions-CFR2',
        reason: 'AWS WAF integration omitted for the dev sample. PROD TODO: associate a WAF WebACL with the CloudFront distributions.',
      },
    ]);
  }
}

/** Per-resource suppressions that need a path. */
export function applyResourceSuppressions(
  stacksByName: Record<string, cdk.Stack>,
): void {
  const suppress = (stackKey: string, path: string, items: { id: string; reason: string }[]): void => {
    const stack = stacksByName[stackKey];
    if (!stack) return;
    NagSuppressions.addResourceSuppressionsByPath(stack, path, items, true);
  };

  // ---- DataStack: Aurora -----------------------------------------------------
  suppress('data', '/DataStack/Aurora/Resource', [
    { id: 'AwsSolutions-RDS6', reason: 'Dev sample uses password auth via Secrets Manager. PROD TODO: enable IAM DB auth.' },
    { id: 'AwsSolutions-RDS10', reason: 'Deletion protection disabled intentionally so the dev sample can be torn down cleanly (RemovalPolicy.DESTROY).' },
  ]);

  // ---- LiteLLMStack: ALB ----------------------------------------------------
  suppress('litellm', '/LiteLLMStack/Alb/Resource', [
    { id: 'AwsSolutions-ELB2', reason: 'Access logs omitted for the dev sample (no log bucket provisioned). PROD TODO: enable ALB access logs to S3.' },
  ]);
  // The ALB security group lives in NetworkStack; EC23 (0.0.0.0/0 ingress) is intentional for dev.
  suppress('network', '/NetworkStack/AlbSg/Resource', [
    { id: 'AwsSolutions-EC23', reason: 'Internet-facing ALB intentionally accepts 0.0.0.0/0 on 443 for the dev sample. PROD TODO: restrict to corporate CIDR.' },
  ]);

  // ---- LangfuseStack ---------------------------------------------------------
  suppress('langfuse', '/LangfuseStack/Cluster/Resource', [
    { id: 'AwsSolutions-ECS4', reason: 'Container Insights left default for the optional Langfuse dev stack to limit cost.' },
  ]);
  suppress('langfuse', '/LangfuseStack/Alb/Resource', [
    { id: 'AwsSolutions-ELB2', reason: 'Internal ops-only ALB; access logs omitted for dev sample.' },
  ]);

  // ---- AuthStack: API Gateway ------------------------------------------------
  // Note: COG4 (Cognito authorizer) is suppressed because we DELIBERATELY use
  // AWS_IAM auth (SigV4) instead of Cognito — this is the core auth decision.
  suppress('auth', '/AuthStack/Api/Resource', [
    { id: 'AwsSolutions-APIG2', reason: 'Request body is empty ({}); identity comes from the SigV4-signed caller ARN, validated in the Lambda. No body schema to validate.' },
  ]);
  suppress('auth', '/AuthStack/Api/DeploymentStage.v1/Resource', [
    { id: 'AwsSolutions-APIG1', reason: 'Access logging omitted for dev sample. PROD TODO: enable API GW access logs.' },
    { id: 'AwsSolutions-APIG6', reason: 'CloudWatch execution logging omitted for dev sample.' },
    { id: 'AwsSolutions-APIG3', reason: 'WAF not attached for dev sample; API is IAM-authenticated and not public.' },
  ]);
  suppress('auth', '/AuthStack/Api/Default/auth/token/POST/Resource', [
    { id: 'AwsSolutions-COG4', reason: 'INTENTIONAL: the Token Service uses AWS_IAM (SigV4) authorization, not Cognito. SSO identity is carried by the signed caller ARN. A Cognito authorizer would defeat the design.' },
  ]);
}
```

**WHY — justification for each suppression (by category):**

*Intentional architecture decisions (kept even in prod):*
- **`COG4` (no Cognito)** — the most important. The Token Service uses **AWS_IAM (SigV4)** auth. SSO identity is carried by the signed caller ARN, so attaching a Cognito authorizer would break the design.
- **`CFR5` (origin HTTP_ONLY)** — the internal Langfuse ALB has no certificate, so the origin is HTTP. The LiteLLM origin is the same. Both are safe behind CloudFront TLS termination.
- **`ECS2` (env var)** — non-sensitive env (model aliases/region/SSM names) is intentionally in environment. It states that **all secrets use ecs.Secret** (though §5's Langfuse plaintext keys are an exception to this justification — they are actually an anti-pattern).
- **`APIG2` (request body validation)** — the body is `{}` and identity comes from the SigV4 ARN, so there is no schema to validate.

*Inherent to the dev sample (with explicit prod TODOs):*
- **`IAM5`/`IAM4`** — Bedrock/agentcore choose the model ARN at runtime, hence `*`. In prod, scope to model/gateway ARNs.
- **`SMG4`** — secret auto-rotation not implemented. Enable in prod.
- **`VPC7`/`EC23`** — flow logs omitted, ALB 0.0.0.0/0 (443). Enable/restrict CIDR in prod.
- **`RDS6`/`RDS10`** — Secrets Manager password auth (no IAM DB auth), deletion protection off (DESTROY for clean teardown).
- **`ELB2`/`APIG1`/`APIG6`/`APIG3`/`CFR3`/`ECS4`** — various access/exec logging, Container Insights, and WAF omitted (cost). Prod TODO.

*Consequences of the domain-optional mode (latest additions):*
- **`CFR4` (minimum TLS policy)** — when **domain-less dev mode** serves on the default `*.cloudfront.net` + default cert, the minimum TLS is forced to TLSv1. In prod, attach a custom domain + ACM with `certMode=acm-dns` to enforce TLSv1.2_2021. (Directly connected to the CDN §8 Pitfall.)
- **`CFR2` (WAF not integrated)** — the dev sample does not attach a WAF WebACL to CloudFront. Integrate in prod.

> **Core principle:** a suppression is not "turning off security" but **"documenting the intent/limitation"**. The trailing `true` in `addResourceSuppressionsByPath(..., true)` means it applies to child resources. Security essentials (IAM auth, TLS termination, Secrets Manager) are not in the suppression list and are enforced by jest.

---

## Appendix — cross-layer mapping summary (cross-layer map)

| Producer | export field | Consumer | how consumed |
|---|---|---|---|
| Network | `vpc`, `*SecurityGroup` | Data / LiteLLM / Langfuse / Auth | reuse VPC·SG via props (SG chain consistency) |
| Data | `litellmDbSecret` | LiteLLM | inject DB credentials via `ecs.Secret` |
| Data | `langfuseDbSecret`, `clusterEndpointHostname/Port` | Langfuse | DB connection via `ecs.Secret` + env |
| Guardrail | `guardrailId`, `guardrailVersion` | LiteLLM | env var → ApplyGuardrail on every Claude request |
| LiteLLM | `masterKeySecret` | Auth | `grantRead` (read only) → `/key/generate` |
| LiteLLM | `internalUrlSsmParameterName` | Auth | runtime lookup **by SSM name** (avoids deploy cross-ref) |
| LiteLLM | `loadBalancer` | CDN | VPC Origin target (internal ALB) |
| Langfuse | `loadBalancer` (optional) | CDN | second VPC Origin |
| LiteLLM/Auth/Langfuse | URLs·surfaces | Observability | dashboard widgets |
| (derived) `certMode==='acm-dns'` | `useCustomDomain` | CDN | branches all of ACM/Route53/Function |

**The whole flow in one line:**
`aws sso login` → SigV4 to API GW (`IAM`) → Token Lambda validates `AWSReservedSSO_` → issues a virtual key via DynamoDB cache / `/key/generate` → client uses the virtual key as Bearer to CloudFront (TLS) → VPC Origin → internal ALB → LiteLLM (ECS) → calls Bedrock (+Guardrail)/Mantle/AgentCore with Task Role SigV4 → traces go to Langfuse.
