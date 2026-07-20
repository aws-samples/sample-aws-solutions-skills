# CDK Stacks — Reproduction Guide for the 10-Stack Governance Gateway

> This document transcribes the CDK (TypeScript) stacks of the `llm-gateway-multi-agent` reference solution **verbatim**, with English explanations + WHY comments + cross-layer mapping added.
> So that an AI agent can read this document and regenerate the same gateway, the code is not summarized — it carries the actual source.
>
> **Current architecture (v1.1) — the 3 new/changed stacks have their full source in separate pattern documents:**
> - `AgentCoreGatewayStack` (us-east-1, web search) → `shared/patterns/agentcore-websearch.md`
> - `MantleNetworkStack` (us-east-1) + `MantlePeeringRoutesStack` (default region, Mantle peering) → `shared/patterns/mantle-peering.md`
> In addition, the `bin/app.ts`/`schema.ts`/`litellm-stack.ts` in the body of this document must reflect the following:
> ① add a top-level `config.awsRegion` (platform region, authoritative) + `config.sso`/`config.agentcore`/`config.mantle`,
> ② add `bedrock-agentcore:InvokeGateway`, `aws-marketplace:Subscribe` (+`ViewSubscriptions`), and the Mantle actions (`bedrock-mantle:CreateInference`/`GetInference`/`GetProject`/`ListProjects` on `project/*` — NOT `foundation-model` — plus `bedrock-mantle:CallWithBearerToken` on `*`) to the LiteLLM Task Role, and add `WEBSEARCH_GATEWAY_URL`/`WEBSEARCH_GATEWAY_REGION`/`BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE` to its env (Mantle region pinning — `MANTLE_REGION` is not read by the provider),
> ③ AuthStack consumes `config.sso` (org-sso) **or** creates a Cognito User Pool from `config.cognitoNative` (cognito-native), and emits the matching outputs,
> ④ The ALB is the edge (CloudFront removed): `config.litellm.certMode` = `acm` | `http` selects TLS; the ALB is **always internet-facing with SG ingress restricted to `litellm.albIngressCidrs`** (no AWS WAF, no self-signed cert, no SSM tunnel); the ALB `idleTimeout` (default 900s) absorbs long completions + the Mantle cold-start (no more 120s CloudFront VPC-Origin ceiling).
>
> **Fixed deployment order (zero circular dependencies):**
> `Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM → Langfuse(conditional) → Auth → Observability → MantleNetwork(us-east-1) → MantlePeeringRoutes`
>
> Core design principles:
> - **Cross-stack coupling only via the `*Exports` interfaces (append-only)** — validated at compile time. Cross-region wiring uses `crossRegionReferences: true`.
> - **Runtime-only wiring (LiteLLM internal URL → Token Service) references the SSM Parameter Store by "name"** (avoids deploy-time cross-refs).
> - **Claude auth is tokenless (SigV4 Task Role); Mantle (GPT-5.x) is Bearer-token** — its Responses route has no SigV4 path, so a short-term key is minted at runtime from the Task Role into `BEDROCK_MANTLE_API_KEY` (never `AWS_BEARER_TOKEN_BEDROCK`) by a LiteLLM callback (see `litellm-gateway.md`). No long-term secret, no external scheduler.
> - **The region is authoritative via `config.awsRegion`** (`bin/app.ts`: `config.awsRegion ?? CDK_DEFAULT_REGION ?? AWS_REGION`). AgentCoreGateway and MantleNetwork are pinned to us-east-1.
> - **The ALB is the public edge** (always internet-facing: acm = HTTPS:443, http = HTTP:80; SG ingress restricted to `albIngressCidrs`). A separate **internal ALB (:4000)** is kept for the Token Service. CloudFront is removed.

---

## 0. App wiring — `bin/app.ts`

CDK app entry point. Instantiates the 6 stacks (+Guardrail) in a **fixed order** and wires them with explicit props. Langfuse is created conditionally based on `config.enableLangfuse` (overridable via the context `-c enableLangfuse=false`).

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

// config.awsRegion is authoritative (Hard Constraint #10) — it must win over the
// CLI-injected CDK_DEFAULT_REGION so a sandbox/CI profile cannot misdirect the stacks.
const primaryRegion = config.awsRegion ?? process.env.CDK_DEFAULT_REGION ?? AWS_REGION;
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: primaryRegion,
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

// ---- 4. Langfuse (conditional — only with certMode='acm') -------------------
// Langfuse UI needs a real domain/cert (public ALB + ACM). http deploys use
// CloudWatch-only observability, so Langfuse is not deployed there (enforced in schema too).
let langfuse: LangfuseStack | undefined;
if (enableLangfuse && config.litellm.certMode === 'acm') {
  langfuse = new LangfuseStack(app, 'LangfuseStack', {
    ...stackProps('langfuse'),
    config: config.langfuse,                         // incl. langfuse.domainName (e.g. langfuse.<domain>)
    hostedZoneId: config.litellm.hostedZoneId,        // reuse the LiteLLM Route53 hosted zone
    hostedZoneName: config.litellm.hostedZoneName,
    albIngressCidrs: config.litellm.albIngressCidrs,
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
// Assigned to a variable (NOT bare `new`) so it can join allStacks below — a bare
// `new` here left ObservabilityStack out of the suppression pass (real-deploy lesson).
const observability = new ObservabilityStack(app, 'ObservabilityStack', {
  ...stackProps('observability'),
  config: config.observability,
  litellm,
  auth,
  langfuse,
});

// ---- 7. Edge = ALB (CloudFront/CdnStack removed) ----------------------------
// TLS is chosen by config.litellm.certMode inside LiteLLMStack. The ALB is always
// internet-facing, SG ingress restricted to config.litellm.albIngressCidrs:
//   acm  → HTTPS:443, public ACM cert (+Route53 alias, HTTP→443 redirect)
//   http → HTTP:80, no cert (plaintext — PoC only, GATE-1 acknowledgement)
// The old 120s CloudFront VPC-Origin ceiling is gone; ALB idleTimeout governs long completions.

// ---- Security checks --------------------------------------------------------
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

// ⚠️ EVERY instantiated stack must be in this array (real-deploy lesson): a stack left
// out receives no dev suppressions at all and synth fails with unsuppressed cdk-nag
// ERRORs — guardrail and observability were the ones actually missed.
const allStacks: cdk.Stack[] = [network, data, guardrail, litellm, auth, observability];
if (langfuse) allStacks.push(langfuse);
// Conditional us-east-1 stacks (only when web search / GPT-Mantle are enabled — see
// agentcore-websearch.md / mantle-peering.md). When generated, push them too:
//   allStacks.push(agentcoreGateway);
//   allStacks.push(mantleNetwork, mantleRoutes);
applyDevSuppressions(allStacks);
applyResourceSuppressions({
  network, data, guardrail, litellm, auth, observability,
  ...(langfuse ? { langfuse } : {}),
});

app.synth();
```

**WHY — wiring essentials:**
- **Fixed order = circular dependencies eliminated.** Each stack directly receives, as props, the instances of stacks created before it (`network`, `data`, `litellm`, etc.). CDK automatically builds the dependency graph and deploys in topological order.
- **Edge TLS is chosen by `config.litellm.certMode`** (`acm` / `http`) inside `LiteLLMStack` — CloudFront/`CdnStack` is removed, so there is no separate us-east-1 edge stack and no `useCustomDomain` derivation. An `acm` cert lives in `config.awsRegion` (a regional ALB cert, not a us-east-1 CloudFront cert); `http` uses no cert.
- **`crossRegionReferences: true`** remains only for the genuinely cross-region stacks (AgentCore Web Search + Mantle, pinned to us-east-1), not for any edge stack.
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
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets'; // Platform.LINUX_ARM64 pin for ContainerImage.fromAsset() — see constraints.md "Docker build architecture mismatch on x86 hosts"

/**
 * NetworkStack — VPC + shared-infra security groups + endpoints.
 *
 * ⚠️ SG OWNERSHIP RULE (cyclic-reference bug observed in a real deploy):
 * NetworkStack owns ONLY the SGs of resources it/DataStack/AuthStack place on
 * shared infra (Aurora, VPC interface endpoints, the Token Lambda). App-facing
 * SGs — the ECS service SGs and EVERY ALB SG — are owned by the stack that
 * creates the resource (LiteLLMStack / LangfuseStack). Never call
 * `networkOwnedSg.addIngressRule(appStackSg, ...)`: the rule is created inside
 * NetworkStack and forces Network → app-stack dependency, while the app stack
 * already depends on Network (VPC) → cyclic reference at synth. The same trap
 * hides inside `addTargets()` (ELBv2 auto-wires ALB SG → target SG). Shared-infra
 * SGs therefore use CIDR-based ingress (private-with-egress subnet CIDRs), not
 * SG-to-SG references to app stacks.
 */
export interface NetworkExports {
  readonly vpc: ec2.IVpc;
  readonly auroraSecurityGroup: ec2.ISecurityGroup;
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  readonly agentcoreEndpointSecurityGroup: ec2.ISecurityGroup;
  readonly interfaceVpcEndpointSecurityGroup: ec2.ISecurityGroup;
  /** ipv4 CIDRs of the private-with-egress subnets (basis of CIDR-based ingress). */
  readonly privateSubnetCidrs: readonly string[];
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
  /**
   * Public gateway base, e.g. https://{domain}/v1 (acm) or http://{albDns}/v1 (http mode —
   * the field NAME is historical/append-only; the scheme follows certMode).
   */
  readonly publicHttpsUrl: string;
  readonly taskRole: iam.IRole;
  /** Master key created/owned here; consumed by AuthStack (grantRead). */
  readonly masterKeySecret: secretsmanager.ISecret;
  /** SSM param NAME (not value) carrying the internal service URL. */
  readonly internalUrlSsmParameterName: string;
  /**
   * LiteLLM container log group. Carries the cloudwatch_usage EMF records —
   * ObservabilityStack points its per-user Logs Insights widgets here.
   */
  readonly logGroup: logs.ILogGroup;
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
   * Edge TLS strategy for the ALB (CloudFront is removed — the ALB is the edge, ALWAYS
   * internet-facing, ALWAYS SG-restricted to `albIngressCidrs`):
   *  - 'acm'  : HTTPS:443 with a PUBLIC ACM cert. Provide EITHER domainName + hostedZoneId +
   *             hostedZoneName (CDK issues a DNS-validated cert IN `config.awsRegion` — not
   *             us-east-1 — and a Route53 A-record alias to the ALB) OR an existing
   *             `certificateArn`. ✅ recommended / PROD.
   *  - 'http' : HTTP:80, no cert, no domain. ⛔ PoC only — the virtual key AND prompt/response
   *             bodies are PLAINTEXT on the wire; the SG allowlist is the only access control
   *             (a GATE-1 acknowledgement item).
   */
  readonly certMode: 'acm' | 'http';
  /** Custom domain for the ALB (acm mode, when CDK issues the cert via Route53). */
  readonly domainName: string;
  /** Existing ACM cert ARN — optional in acm to skip cert issuance. */
  readonly certificateArn: string;
  /** Route53 hosted zone (acm mode with a CDK-issued cert). */
  readonly hostedZoneId: string;
  readonly hostedZoneName: string;
  /**
   * ALB idle timeout in seconds. This governs long completions now that CloudFront's hard 120s
   * VPC-Origin ceiling is gone (e.g. Opus/Fable extended thinking). Default 900; max 4000.
   */
  readonly albIdleTimeoutSeconds?: number;
  /**
   * CIDRs allowed to reach the public ALB — the PRIMARY access control in both modes (there is
   * no AWS WAF). A required Discovery answer (office/NAT egress CIDRs). '0.0.0.0/0' with
   * certMode='http' means the plaintext endpoint is open to the whole internet — its own
   * explicit GATE-1 acknowledgement.
   */
  readonly albIngressCidrs: readonly string[];
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
    litellm.certMode === 'acm' || litellm.certMode === 'http',
    "litellm.certMode must be 'acm' or 'http'",
  );
  req(Array.isArray(litellm.albIngressCidrs) && (litellm.albIngressCidrs as unknown[]).length > 0,
    'litellm.albIngressCidrs must be a non-empty array (the SG allowlist is the primary access control — captured in Discovery)');
  str(litellm, 'certificateArn', 'litellm');
  str(litellm, 'domainName', 'litellm');
  str(litellm, 'hostedZoneId', 'litellm');
  str(litellm, 'hostedZoneName', 'litellm');
  str(litellm, 'masterKey', 'litellm');
  req((litellm.masterKey as string).length > 0, 'litellm.masterKey must be set');
  num(litellm, 'desiredCount', 'litellm');
  num(litellm, 'cpu', 'litellm');
  num(litellm, 'memoryLimitMiB', 'litellm');
  if (litellm.albIdleTimeoutSeconds !== undefined) {
    const t = litellm.albIdleTimeoutSeconds;
    req(typeof t === 'number' && t >= 1 && t <= 4000, 'litellm.albIdleTimeoutSeconds must be 1-4000 (ALB limit)');
  }
  if (litellm.certMode === 'acm') {
    const hasArn = typeof litellm.certificateArn === 'string' && (litellm.certificateArn as string).length > 0;
    const hasZone =
      (litellm.domainName as string).length > 0 &&
      (litellm.hostedZoneId as string).length > 0 &&
      (litellm.hostedZoneName as string).length > 0;
    req(hasArn || hasZone,
      "certMode='acm' requires either certificateArn OR domainName+hostedZoneId+hostedZoneName");
  }
  // certMode='http': no cert/domain fields required (HTTP:80, plaintext — PoC only). The SG
  // allowlist (albIngressCidrs) is the only access control; '0.0.0.0/0' is legal but is an
  // explicit GATE-1 acknowledgement, not a schema error.
  // Langfuse UI needs a real domain/cert → only allowed with certMode='acm'. http deploys use
  // CloudWatch-only observability (Langfuse is not deployed).
  req(!c.enableLangfuse || litellm.certMode === 'acm',
    "enableLangfuse=true requires litellm.certMode='acm' (Langfuse UI needs a real domain/cert; http → CloudWatch only)");

  const agentcore = obj('agentcore');
  str(agentcore, 'webSearchRegion', 'agentcore');           // us-east-1
  str(agentcore, 'gatewayName', 'agentcore');               // ^([0-9a-zA-Z][-]?){1,100}$
  req((agentcore.webSearchRegion as string).length > 0, 'agentcore.webSearchRegion must be set');
  // domainDenyList is optional (string[])

  // Top-level awsRegion (authoritative platform region) + authMode + sso/cognitoNative + mantle blocks:
  str(c, 'awsRegion', 'root');
  req((c.awsRegion as string).length > 0, 'awsRegion must be set');
  const authMode = (c.authMode ?? 'org-sso') as string;
  // 'account-sso' is accepted as a deprecated alias for schema compatibility, but do NOT
  // generate it: an IdC account instance cannot host the SAML app it would require. Use
  // 'cognito-native' for account instances / no-IdC.
  req(
    authMode === 'org-sso' || authMode === 'cognito-native' || authMode === 'account-sso',
    "authMode must be 'org-sso' or 'cognito-native'",
  );
  if (authMode === 'org-sso') {
    const sso = obj('sso');
    str(sso, 'startUrl', 'sso'); str(sso, 'region', 'sso'); str(sso, 'accountId', 'sso'); str(sso, 'roleName', 'sso');
  } else {
    // cognito-native: cognitoNative is optional (all fields have defaults applied in
    // AuthStack). Validate only the constrained fields when present.
    const cognitoNative = (c.cognitoNative ?? {}) as Record<string, unknown>;
    if (cognitoNative.multiGroupStrategy !== undefined) {
      req(cognitoNative.multiGroupStrategy === 'require-single-team-group', "cognitoNative.multiGroupStrategy must be 'require-single-team-group'");
    }
    if (cognitoNative.refreshTokenValidityDays !== undefined) {
      const d = cognitoNative.refreshTokenValidityDays;
      req(typeof d === 'number' && d >= 1 && d <= 3650, 'cognitoNative.refreshTokenValidityDays must be 1-3650 (Cognito App Client limit)');
    }
    if (cognitoNative.passwordMinLength !== undefined) {
      const l = cognitoNative.passwordMinLength;
      req(typeof l === 'number' && l >= 6 && l <= 99, 'cognitoNative.passwordMinLength must be 6-99 (Cognito User Pool limit)');
    }
  }
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
- **Conditional required-value validation is the key.** `certMode='acm'` requires either `certificateArn` OR `domainName`+`hostedZoneId`+`hostedZoneName` (CDK issues a DNS-validated cert in `config.awsRegion`); `http` needs no cert/domain. `albIngressCidrs` is structurally required in both modes (a Discovery answer — the SG allowlist is the primary access control; the `0.0.0.0/0` policy decision is a GATE-1 acknowledgement, never a schema error). This branch validation pairs with the ALB listener selection in `LiteLLMStack`.
- **A validator with no external dependencies** — structural validation using only the `req/obj/num/str` helpers. By not adding zod or similar, it reduces the dependency surface. It throws immediately on the first violation (fail-fast).

---

## 0-3. Single source of constants — `lib/config/constants.ts`

The single source of truth for cross-stack constants. Literals of these values must not appear elsewhere; they are always imported from here.

```typescript
// Region is config-driven (config.awsRegion is authoritative — see bin/app.ts).
// AWS_REGION here is ONLY a last-resort fallback when config + CDK_DEFAULT_REGION
// are both absent; never rely on editing it to change the deploy region.
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
 *
 * ⚠️ Backend IDs are `global.` inference profiles, NOT `us.`. Verified with
 * `aws bedrock list-inference-profiles`: recent (2026) Claude models are published
 * only as GLOBAL-type profiles — a `bedrock/us.anthropic.<id>` call fails with
 * "The provided model identifier is invalid." Always re-verify the exact profile id
 * per model in the target account/region before editing (never assume `us.`).
 * GPT-5.x (bedrock_mantle) uses Bearer-token auth (BEDROCK_MANTLE_API_KEY), not SigV4.
 */
export const MODELS = {
  CLAUDE_OPUS:   { litellmName: 'claude-opus-4-8',   backend: 'bedrock/global.anthropic.claude-opus-4-8' },
  CLAUDE_SONNET: { litellmName: 'claude-sonnet-5',   backend: 'bedrock/global.anthropic.claude-sonnet-5' },
  CLAUDE_HAIKU:  { litellmName: 'claude-haiku-4-5',  backend: 'bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0' },
  CLAUDE_FABLE:  { litellmName: 'claude-fable-5',    backend: 'bedrock/global.anthropic.claude-fable-5' }, // Mythos-class: requires provider_data_share opt-in (per region) — see constraints.md
  GPT55: { litellmName: 'gpt-5.5', backend: 'bedrock_mantle/openai.gpt-5.5' }, // responses API, Bearer token auth (not SigV4)
  GPT54: { litellmName: 'gpt-5.4', backend: 'bedrock_mantle/openai.gpt-5.4' }, // economy tier (~2x cheaper)
} as const;

/** Only assumed-role principals from IAM Identity Center are accepted (org-sso mode). */
export const SSO_ARN_PREFIX = 'AWSReservedSSO_';

/**
 * Teams for this deployment. Each name is the authorization unit AND the LiteLLM
 * team_alias, 1:1: a permission set name (org-sso) or a Cognito User Pool Group
 * name (cognito-native). Seed with the team(s) from Discovery; onboarding more
 * later is console-only. (Illustrative names — rename to real orgs/teams.)
 */
export const TEAMS = {
  DEV1: 'llmgw-dev1',
  DEV2: 'llmgw-dev2',
} as const;

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

/**
 * CloudWatch usage metrics (EMF). The LiteLLM container's cloudwatch_usage
 * callback (litellm-gateway.md §4) emits EMF records into this namespace —
 * injected as env LLMGW_METRICS_NAMESPACE — and the ObservabilityStack
 * dashboard reads the same namespace. Env-scoped via ns() so two deployments
 * in one account never mix metrics.
 */
export const METRICS = {
  NAMESPACE: ns('usage'),
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
- **`PORTS`**: the SG chain (Network) and container ports (LiteLLM/Langfuse) all share these constants.

---

## 1. NetworkStack — VPC + SG chain + VPC endpoints

VPC (2 AZ, 1 NAT), the full security-group chain, and VPC endpoints that keep Bedrock/AgentCore/AWS API traffic inside the AWS network. The root stack — it depends on nothing.

```typescript
export class NetworkStack extends cdk.Stack implements NetworkExports {
  public readonly vpc: ec2.IVpc;
  public readonly auroraSecurityGroup: ec2.ISecurityGroup;
  public readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  public readonly agentcoreEndpointSecurityGroup: ec2.ISecurityGroup;
  public readonly interfaceVpcEndpointSecurityGroup: ec2.ISecurityGroup;
  public readonly privateSubnetCidrs: readonly string[];

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

    // The private-with-egress subnet CIDRs — the basis of CIDR-based ingress on
    // the shared-infra SGs below (and exported for app stacks that need them).
    const privateSubnetCidrs = vpc
      .selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS })
      .subnets.map((s) => s.ipv4CidrBlock);
    this.privateSubnetCidrs = privateSubnetCidrs;

    // ---- Shared-infra security groups ---------------------------------------
    // ⚠️ SG OWNERSHIP RULE (real-deploy cyclic-reference lesson — see interfaces.ts):
    // NetworkStack owns ONLY shared-infra SGs (Aurora / VPC endpoints / Token Lambda).
    // ECS service SGs and ALL ALB SGs live in LiteLLMStack / LangfuseStack. A rule on a
    // Network-owned SG must never reference an app-stack SG (`sg.addIngressRule(appSg)`
    // creates the rule HERE and imports the app SG's GroupId → Network depends on the
    // app stack → cycle, because the app stack already depends on Network for the VPC).
    // Hence: ingress from app tasks is granted by SUBNET CIDR, not by SG reference.
    // Trade-off: subnet-CIDR ingress is broader than SG-to-SG, but the peers are our
    // own private subnets — acceptable, and it removes the cross-stack edge entirely.
    const sg = (logical: string, description: string, allowOutbound = true): ec2.SecurityGroup =>
      new ec2.SecurityGroup(this, logical, {
        vpc,
        securityGroupName: ns(logical.toLowerCase()),
        description, // ASCII only (constraints.md)
        allowAllOutbound: allowOutbound,
      });

    const lambdaSg = sg('LambdaSg', 'Token Service Lambda (VPC-placed)');
    const auroraSg = sg('AuroraSg', 'Aurora Serverless v2', false);
    const vpceSg = sg('VpceSg', 'Interface VPC Endpoints', false);
    const agentcoreSg = sg('AgentCoreEndpointSg', 'bedrock-agentcore interface endpoint', false);

    // CIDR-based ingress: anything in the private-with-egress subnets (LiteLLM tasks,
    // Langfuse tasks, the Token Lambda) may reach Aurora:5432 and the endpoints:443.
    for (const cidr of privateSubnetCidrs) {
      auroraSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(PORTS.AURORA), 'Private subnets to Aurora');
      vpceSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(PORTS.HTTPS), 'Private subnets to interface endpoints');
      agentcoreSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(PORTS.HTTPS), 'Private subnets to bedrock-agentcore');
    }

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
- **SG ownership rule (real-deploy cyclic-reference lesson).** NetworkStack owns only **shared-infra** SGs (Aurora / interface endpoints / Token Lambda). ECS service SGs and every ALB SG are created in LiteLLMStack / LangfuseStack. Why: LiteLLM/Langfuse already depend on Network (VPC). If a Network-owned SG holds a rule referencing an app-stack SG (`auroraSg.addIngressRule(serviceSg, ...)`), the `CfnSecurityGroupIngress` is created in NetworkStack and must import the app SG's GroupId → Network → app dependency → **cyclic reference at synth**. This bit three times in a real deploy (public ALB SG → service SG, internal ALB SG, then Aurora ← serviceSg) before the general rule was applied.
- **CIDR-based ingress replaces cross-stack SG-to-SG.** Aurora:5432 and the endpoints:443 accept from the **private-with-egress subnet CIDRs** — every legitimate caller (LiteLLM tasks, Langfuse tasks, Token Lambda) lives there. Trade-off: broader than SG-to-SG, but the peers are our own private subnets and the cross-stack edge disappears entirely.
- **Beware auto-wiring**: `addTargets()` / `grantConnect()` silently create SG rules "from the ALB SG to the target SG". Keeping ALB SG + service SG in the **same stack** (LiteLLM/Langfuse) keeps that auto-wiring stack-local.
- **SGs with `allowAllOutbound=false`**: aurora/vpce/agentcore block outbound to narrow the data plane. Lambda is true (outbound required).
- **Gateway endpoints (S3/DynamoDB) are free**, while Interface endpoints (bedrock-runtime/secrets/ssm/ecr/ecr-docker/logs) are paid but reach the AWS APIs **without traversing the NAT** → a win on both cost and security.
- **The AgentCore endpoint may not be in the enum**, so the service name is specified directly via `InterfaceVpcEndpointService(com.amazonaws.{region}.bedrock-agentcore)`. A pattern resilient to CDK version differences.
- **Cross-layer mapping**: `NetworkExports` exposes the shared-infra SGs + `privateSubnetCidrs`. Data (aurora SG) and Auth (lambda SG) consume them; LiteLLM/Langfuse consume only the VPC + CIDRs and own their SGs.

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

## 4. LiteLLMStack — the governance gateway core (ECS Fargate + ALB edge)

ECS Fargate runs the LiteLLM proxy. The **public, internet-facing ALB** (SG-restricted to `albIngressCidrs`) is the developer edge — HTTPS:443 (`acm`) or HTTP:80 (`http`); a separate **internal ALB (:4000)** serves the Token Service. It owns the master key secret and publishes the internal URL to SSM for the Auth plane to consume at runtime.

```typescript
export class LiteLLMStack extends cdk.Stack implements LiteLLMExports {
  public readonly loadBalancer: elbv2.IApplicationLoadBalancer;
  public readonly publicHttpsUrl: string;
  public readonly taskRole: iam.IRole;
  public readonly masterKeySecret: secretsmanager.ISecret;
  public readonly internalUrlSsmParameterName: string;
  public readonly logGroup: logs.ILogGroup;

  constructor(scope: Construct, id: string, props: LiteLLMStackProps) {
    super(scope, id, props);
    const { config, agentcore, guardrailId, guardrailVersion, network, data, mantleRegion } = props;

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
    // Claude (bedrock-runtime) invocation — tokenless SigV4.
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
        'bedrock:ApplyGuardrail',
      ],
      resources: ['*'], // dev sample; prod TODO: scope to the global. inference-profile ARNs + their unqualified foundation-model fan-out (see below / constants.ts)
    }));
    // Bedrock Mantle (GPT-5.x) — Bearer-token route. IMPORTANT: the inference actions
    // are grantable only on the `project` resource type, NOT `foundation-model` (AWS's
    // managed policy AmazonBedrockMantleInferenceAccess; a foundation-model ARN is
    // rejected with AccessDenied on CreateInference). CallWithBearerToken has no
    // resource scoping (it authenticates the Bearer token before project attribution).
    // Do NOT use a `bedrock-mantle:*` wildcard.
    taskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockMantleInvoke',
      actions: ['bedrock-mantle:CreateInference', 'bedrock-mantle:GetInference', 'bedrock-mantle:GetProject', 'bedrock-mantle:ListProjects'],
      resources: [`arn:aws:bedrock-mantle:${mantleRegion}:${this.account}:project/*`],
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockMantleCallWithBearerToken',
      actions: ['bedrock-mantle:CallWithBearerToken'],
      resources: ['*'],
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
    // Exported: ObservabilityStack queries the cloudwatch_usage EMF records in
    // this group for its per-user / hourly Logs Insights widgets.
    this.logGroup = logGroup;

    taskDef.addContainer('litellm', {
      // WHY `platform: Platform.LINUX_ARM64`: ecs.ContainerImage.fromAsset() runs a local `docker
      // build` on whatever machine executes `cdk deploy`, and Docker defaults to building for the
      // HOST's own architecture. runtimePlatform above hard-codes ARM64 (Graviton) for the Fargate
      // task, so on an x86_64 deploy host (Windows/Intel-Mac/x86 CI) an unpinned build silently
      // produces an amd64 image. The stack still deploys successfully (ECR does not validate image
      // architecture against the task definition) and the mismatch only surfaces at task launch as
      // `exec format error` in CloudWatch Logs — see constraints.md "Docker build architecture
      // mismatch on x86 hosts". Pin `platform` explicitly so the build always matches
      // `runtimePlatform.cpuArchitecture`, regardless of the deploy host's own architecture.
      // (No local Docker AT ALL — daemon can't even be installed? See §4-1: imageBuild.mode
      // = 'codebuild' swaps this fromAsset() for fromEcrRepository() + a native-ARM CodeBuild.)
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, '..', 'services', 'litellm'), {
        platform: ecrAssets.Platform.LINUX_ARM64,
      }),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'litellm', logGroup }),
      portMappings: [{ containerPort: PORTS.LITELLM }],
      environment: {
        LITELLM_MODE: 'PRODUCTION',
        // PROXY_BASE_URL is the public URL the LiteLLM Admin UI (SPA) builds redirects from.
        // acm: config.domainName. http (no domain): empty — the /ui -> /ui/ 307 scheme quirk
        // is cosmetic only. ⚠️ Do NOT compensate with `--forwarded-allow-ips` in the
        // entrypoint: the pinned image's CLI lacks that option and dies at boot
        // (exitCode 2) — see Hard Constraint #8 / constraints.md.
        PROXY_BASE_URL: config.domainName ? `https://${config.domainName}` : '',
        AWS_REGION: this.region,
        STORE_MODEL_IN_DB: 'True',
        // EMF namespace for the cloudwatch_usage callback — MUST match what the
        // ObservabilityStack dashboard reads (constants METRICS.NAMESPACE).
        LLMGW_METRICS_NAMESPACE: METRICS.NAMESPACE,
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
        // Pin via the vars the provider actually reads. MANTLE_REGION is NOT read by
        // the provider (documentation alias only). NOTE: Mantle uses a Bearer token
        // (BEDROCK_MANTLE_API_KEY), minted at runtime by the mantle_token_refresh
        // callback — do NOT set AWS_BEARER_TOKEN_BEDROCK here (boto3-reserved; it would
        // break Claude's SigV4). The callback sets BEDROCK_MANTLE_API_KEY in-process.
        BEDROCK_MANTLE_REGION: mantleRegion,
        BEDROCK_MANTLE_API_BASE: `https://bedrock-mantle.${mantleRegion}.api.aws`,
        MANTLE_REGION: mantleRegion, // human-readable alias only (not consumed by litellm)
        // AgentCore Web Search gateway (MCP, SigV4). Cross-region exports from
        // AgentCoreGatewayStack (us-east-1); requires crossRegionReferences: true.
        WEBSEARCH_GATEWAY_URL: agentcoreGateway.gatewayUrl,
        WEBSEARCH_GATEWAY_REGION: agentcoreGateway.webSearchRegion,
        BEDROCK_GUARDRAIL_ID: guardrailId,
        BEDROCK_GUARDRAIL_VERSION: guardrailVersion,
        LANGFUSE_HOST: config.domainName ? `https://langfuse.${config.domainName}` : '',
      },
      secrets: {
        LITELLM_MASTER_KEY: ecs.Secret.fromSecretsManager(masterKey),
        // Langfuse trace keys — injected from the shared secret (created in DataStack,
        // BEFORE LiteLLM), never plaintext in `environment` (Hard Constraint #4).
        LANGFUSE_PUBLIC_KEY: ecs.Secret.fromSecretsManager(data.langfuseSharedSecret, 'publicKey'),
        LANGFUSE_SECRET_KEY: ecs.Secret.fromSecretsManager(data.langfuseSharedSecret, 'secretKey'),
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

    // ---- Security groups owned by THIS stack (SG ownership rule) ------------
    // The ECS service SG and both ALB SGs live HERE, not in NetworkStack — a rule
    // on a Network-owned SG referencing a LiteLLM-owned SG would create a
    // Network → LiteLLM dependency and a cyclic reference (real-deploy lesson).
    // Aurora/endpoint access needs no SG edit: NetworkStack already allows the
    // private-with-egress subnet CIDRs (where these tasks run).
    const serviceSg = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc: network.vpc, allowAllOutbound: true,
      description: 'ECS LiteLLM tasks', // ASCII only (constraints.md)
    });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: config.desiredCount,
      securityGroups: [serviceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      healthCheckGracePeriod: cdk.Duration.seconds(60),
    });

    // Auth model: Claude (bedrock/) is tokenless SigV4 via the Task Role. Bedrock
    // Mantle (GPT-5.5/5.4) has NO SigV4 path on its Responses route — it uses a
    // Bearer token minted at runtime into BEDROCK_MANTLE_API_KEY by the
    // mantle_token_refresh callback (services/litellm/callbacks/), refreshed
    // in-process, no long-term secret and no external scheduler. See
    // constraints.md "LiteLLM image + Mantle Bearer-token auth".

    // ---- ALB(s): the ALB is the edge now (CloudFront/CdnStack removed) --------
    // An INTERNAL ALB (HTTP:4000) always exists so the Token Service reaches LiteLLM
    // privately — the SSM URL below is UNCHANGED, so the auth plane needs no edit.
    // The developer edge is a PUBLIC, internet-facing ALB in BOTH modes, with SG ingress
    // restricted to config.albIngressCidrs (the primary access control — no AWS WAF):
    //   acm  → HTTPS:443, regional ACM cert (+ HTTP→443 redirect)
    //   http → HTTP:80, no cert (⛔ plaintext — PoC only, GATE-1 acknowledgement)
    // idleTimeout (default 900s) governs long completions — the old CloudFront 120s
    // VPC-Origin ceiling is gone.
    const certMode = config.certMode ?? 'acm';
    const idleTimeout = cdk.Duration.seconds(config.albIdleTimeoutSeconds ?? 900);
    const targetProps = {
      port: PORTS.LITELLM,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        path: '/health/liveliness', healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(15), timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2, unhealthyThresholdCount: 3,
      },
    };

    // Internal ALB — Token Service path, never internet-facing. Its SG is owned
    // HERE (see the SG ownership rule above): ingress 4000 from the VPC CIDR
    // (the Token Lambda calls it from the private subnets).
    const internalAlbSg = new ec2.SecurityGroup(this, 'InternalAlbSg', {
      vpc: network.vpc, allowAllOutbound: true,
      description: 'Internal ALB - Token Service path', // ASCII only
    });
    internalAlbSg.addIngressRule(
      ec2.Peer.ipv4(network.vpc.vpcCidrBlock), ec2.Port.tcp(PORTS.LITELLM), 'VPC to internal ALB',
    );
    serviceSg.addIngressRule(internalAlbSg, ec2.Port.tcp(PORTS.LITELLM), 'Internal ALB to LiteLLM'); // same-stack SG ref: safe
    const internalAlb = new elbv2.ApplicationLoadBalancer(this, 'InternalAlb', {
      vpc: network.vpc,
      internetFacing: false,
      securityGroup: internalAlbSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      idleTimeout,
    });
    // ⚠️ open:false on EVERY addListener() — CDK defaults open:true, which silently appends
    // a 0.0.0.0/0 ingress rule for the listener port to the ALB SG, defeating albIngressCidrs
    // (real-deploy incident; see constraints.md). Verify the deployed SG after any listener change.
    internalAlb
      .addListener('Http', { port: PORTS.LITELLM, protocol: elbv2.ApplicationProtocol.HTTP, open: false })
      .addTargets('LiteLlmInternal', targetProps);

    // Public ALB — the developer edge in BOTH modes. Its SG allows ingress ONLY from the
    // albIngressCidrs allowlist (a required Discovery answer). NOTE: SG descriptions must
    // be ASCII (a Unicode em-dash fails the create — see constraints.md).
    const publicAlbSg = new ec2.SecurityGroup(this, 'PublicAlbSg', {
      vpc: network.vpc, allowAllOutbound: true,
      description: 'Public ALB edge - ingress from albIngressCidrs only',
    });
    for (const cidr of config.albIngressCidrs) {
      if (certMode === 'acm') {
        publicAlbSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(443), 'HTTPS from allowlist');
        publicAlbSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(80), 'HTTP redirect from allowlist');
      } else {
        publicAlbSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(80), 'HTTP from allowlist (plaintext PoC)');
      }
    }
    // Let the ECS service accept traffic from the public ALB too (same-stack SG ref: safe).
    serviceSg.addIngressRule(publicAlbSg, ec2.Port.tcp(PORTS.LITELLM), 'Public ALB to LiteLLM');
    const publicAlb = new elbv2.ApplicationLoadBalancer(this, 'PublicAlb', {
      vpc: network.vpc,
      internetFacing: true,
      securityGroup: publicAlbSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      idleTimeout,
    });

    let gatewayUrl: string;
    if (certMode === 'acm') {
      // Resolve the TLS cert — REGIONAL (config.awsRegion), not a us-east-1 CloudFront cert.
      const hasArn = config.certificateArn.length > 0;
      let certificate: acm.ICertificate;
      if (hasArn) {
        certificate = acm.Certificate.fromCertificateArn(this, 'EdgeCert', config.certificateArn);
      } else {
        const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
          hostedZoneId: config.hostedZoneId, zoneName: config.hostedZoneName,
        });
        certificate = new acm.Certificate(this, 'EdgeCert', {
          domainName: config.domainName,
          validation: acm.CertificateValidation.fromDns(zone),
        });
        new route53.ARecord(this, 'EdgeAlias', {
          zone, recordName: config.domainName,
          target: route53.RecordTarget.fromAlias(new route53targets.LoadBalancerTarget(publicAlb)),
        });
      }
      publicAlb
        .addListener('Https', {
          port: 443,
          protocol: elbv2.ApplicationProtocol.HTTPS,
          sslPolicy: elbv2.SslPolicy.TLS13_RES,
          certificates: [certificate],
          open: false,
        })
        .addTargets('LiteLlmEdge', targetProps);
      // HTTP:80 → HTTPS:443 (virtual keys always over TLS in acm mode).
      publicAlb.addListener('HttpRedirect', {
        port: 80, protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({ protocol: 'HTTPS', port: '443', permanent: true }),
        open: false,
      });
      gatewayUrl = config.domainName.length > 0
        ? `https://${config.domainName}`
        : `https://${publicAlb.loadBalancerDnsName}`;
    } else {
      // http mode: a plain HTTP:80 listener. ⛔ The virtual key AND prompt/response bodies
      // are plaintext on the wire — PoC only; the SG allowlist above is the only access
      // control (GATE-1 acknowledgement, incl. an explicit one for 0.0.0.0/0).
      publicAlb
        .addListener('Http80', { port: 80, protocol: elbv2.ApplicationProtocol.HTTP, open: false })
        .addTargets('LiteLlmEdge', targetProps);
      gatewayUrl = `http://${publicAlb.loadBalancerDnsName}`;
    }
    this.loadBalancer = publicAlb;
    this.publicHttpsUrl = `${gatewayUrl}/v1`;

    // ---- Publish INTERNAL URL to SSM (Token Service wiring — UNCHANGED) -------
    this.internalUrlSsmParameterName = SSM.LITELLM_INTERNAL_URL;
    new ssm.StringParameter(this, 'InternalUrlParam', {
      parameterName: SSM.LITELLM_INTERNAL_URL,
      stringValue: `http://${internalAlb.loadBalancerDnsName}:${PORTS.LITELLM}`,
      description: 'LiteLLM internal base URL for the Token Service',
    });

    new cdk.CfnOutput(this, 'AlbDns', { value: publicAlb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'GatewayUrl', { value: gatewayUrl });
    new cdk.CfnOutput(this, 'AdminUiUrl', { value: `${gatewayUrl}/ui/` });
  }
}
```

**WHY — gateway essentials:**
- **Two auth models, and the distinction is the heart of the design.** Claude (`bedrock:` Converse + ApplyGuardrail) is **tokenless SigV4** via the Task Role — no key to store/rotate. Mantle (the `bedrock-mantle` actions scoped to `project/*` + `CallWithBearerToken`) has **no SigV4 path**, so a short-term Bearer key is minted at runtime from the same Task Role into `BEDROCK_MANTLE_API_KEY` by the `mantle_token_refresh` callback — still no long-term secret and no external scheduler, but it is a Bearer token, not SigV4. `bedrock-agentcore:InvokeGateway` (Web Search) is SigV4. (Mantle auto-subscribes on first call via `aws-marketplace:Subscribe`.) ⚠️ Never set `AWS_BEARER_TOKEN_BEDROCK` — boto3 would apply it to Claude too and 403 all Claude models.
- **The `secrets` vs `environment` distinction matters (security):**
  - `secrets` (Secrets Manager injection): `LITELLM_MASTER_KEY`, `DATABASE_PASSWORD/HOST/USER`, **and the Langfuse trace keys (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` from the shared `data.langfuseSharedSecret`)** — all sensitive values go through `ecs.Secret.fromSecretsManager`. Plaintext is not exposed in the task definition.
  - `environment` (plaintext): only non-sensitive values like model aliases/region/SSM names/guardrail ID/`LANGFUSE_HOST`.
  - The Langfuse trace keys are shared with Langfuse's `LANGFUSE_INIT_PROJECT_*` and must match; because LiteLLM is created **before** Langfuse, the shared secret is created in an **earlier** stack (DataStack) and both consume it — **never** hard-code the literal on either side (Hard Constraint #4).
- **Two ALBs, two audiences — and every SG here is stack-local.** The **public, internet-facing ALB** (own SG, ingress only from `albIngressCidrs`) is the developer edge — HTTPS:443 with a regional ACM cert (`acm`) or plain HTTP:80 (`http`, plaintext PoC). The **internal ALB (:4000)** (own SG, VPC-CIDR ingress) serves only the Token Service and is never internet-exposed. The ECS `serviceSg` is also created here, so `addTargets()`'s auto-wiring (ALB SG → target SG) stays inside this stack — referencing a Network-owned service SG instead caused a **cyclic reference** in a real deploy (see NetworkStack). No AWS WAF — the SG allowlist is the access control.
- **SSM publishing (runtime wiring):** writes `http://{albDns}:4000` to `SSM.LITELLM_INTERNAL_URL`. The Auth Lambda looks it up at runtime by this name → avoids a LiteLLM↔Auth deploy-time cross-ref (connected to the `internalUrlSsmParameterName` design in interface §0-1).
- **ARM64 (Graviton) + circuitBreaker (rollback) + health-check grace 90s** — cost/stability. LiteLLM boots slowly, hence `startPeriod: 90s`.
- **Cross-layer mapping**: `masterKeySecret` (→Auth grantRead), `loadBalancer` (= the public edge ALB), `publicHttpsUrl`/`internalUrlSsmParameterName` (→Auth/Observability) flow as `LiteLLMExports`.

---

## 4-1. ImageBuildStack (conditional) — build the LiteLLM image in CodeBuild when local Docker is unavailable

**Decision rule (Discovery / prerequisites): if `docker info` succeeds on the deploy machine, use the
default `fromAsset()` local build (§4) — do NOT deploy this stack. Only when Docker cannot run locally
at all** (real case: a corporate Windows laptop where Docker Desktop needs WSL2/Hyper-V, both of which
need an admin install **and a reboot** that policy forbade) **and the operator still wants to keep
CDK + the AI tool local** (i.e. the full `ec2-deploy-host.md` move is heavier than the problem),
delegate ONLY the image build to CodeBuild: a conditional `ImageBuildStack` (ECR repository + CodeBuild
project on **native ARM** — no QEMU, no cross-build) builds and pushes the image, and `LiteLLMStack`
consumes it via `fromEcrRepository()` instead of `fromAsset()`. Everything else about the deploy is
unchanged. (If more than Docker is unsuitable on the machine, prefer the EC2 deploy host instead.)

Config: `config.litellm.imageBuild?.mode: 'local-docker' | 'codebuild'` (absent = `'local-docker'`,
the §4 default). Add to `LiteLLMConfig` in `lib/config/schema.ts`:

```typescript
  /**
   * Where the LiteLLM proxy image is built:
   *  - 'local-docker' (default; field may be omitted): CDK fromAsset() runs `docker build`
   *    on the deploy machine — daemon required, QEMU on x86 hosts (§4).
   *  - 'codebuild'   : no local Docker anywhere in the deploy — the conditional
   *    ImageBuildStack (ECR + CodeBuild, native ARM) builds/pushes the image and
   *    LiteLLMStack consumes it via fromEcrRepository(). Requires the 3-step deploy
   *    order documented in cdk-stacks.md §4-1.
   */
  readonly imageBuild?: { readonly mode: 'local-docker' | 'codebuild' };
```

Append to `lib/interfaces.ts` (the §0-1 append-only contract):

```typescript
/** ImageBuildStack (conditional — litellm.imageBuild.mode === 'codebuild' only). */
export interface ImageBuildExports {
  readonly repository: ecr.IRepository;
  /** Deterministic image tag = content hash of services/litellm/ (NEVER ':latest' — see §4-1 WHY). */
  readonly imageTag: string;
}
```

The full `lib/image-build-stack.ts`:

```typescript
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import { NagSuppressions } from 'cdk-nag';
import { ImageBuildExports } from './interfaces';
import { ns } from './config/constants';

/**
 * Builds the LiteLLM proxy image in CodeBuild on NATIVE ARM when the deploy
 * machine has no usable Docker (imageBuild.mode === 'codebuild').
 *
 * ⚠️ Deploy-order contract: `cdk deploy` of this stack creates the project but
 * does NOT run it (StartBuild is an API call, not a CloudFormation resource).
 * The image must exist in ECR BEFORE LiteLLMStack deploys — see the 3-step
 * procedure below this stack.
 */
export class ImageBuildStack extends cdk.Stack implements ImageBuildExports {
  public readonly repository: ecr.IRepository;
  public readonly imageTag: string;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    // Replaces the CDK-managed asset repository that fromAsset() would have used.
    const repo = new ecr.Repository(this, 'LitellmRepo', {
      repositoryName: ns('litellm'),
      removalPolicy: cdk.RemovalPolicy.DESTROY, // dev sample: tear-downable
      emptyOnDelete: true,
    });

    // The build context (services/litellm/) travels as a CDK S3 asset:
    // `cdk deploy` re-uploads it whenever its content changes, and the asset's
    // content hash doubles as the image tag — one hash names both the source
    // zip and the image built from it.
    const source = new s3assets.Asset(this, 'LitellmBuildContext', {
      path: path.join(__dirname, '..', 'services', 'litellm'),
    });
    this.imageTag = source.assetHash;

    const project = new codebuild.Project(this, 'LitellmImageBuild', {
      projectName: ns('litellm-image-build'),
      description: 'Builds the LiteLLM proxy image natively on ARM (no local Docker, no QEMU)',
      source: codebuild.Source.s3({ bucket: source.bucket, path: source.s3ObjectKey }),
      environment: {
        // ⚠️ API trap (cost a real deploy an edit-fail-retry loop): there is NO
        // `codebuild.ComputeType.ARM_CONTAINER` in aws-cdk-lib, even though docs
        // snippets/autocomplete suggest it. ARM is selected by the BUILD IMAGE
        // (LinuxArmBuildImage.*); computeType stays SMALL/MEDIUM/LARGE.
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
        privileged: true, // `docker build` inside CodeBuild needs privileged mode (nag CB3 below)
      },
      environmentVariables: {
        REPO_URI: { value: repo.repositoryUri },
        IMAGE_TAG: { value: this.imageTag },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              // ${REPO_URI%%/*} = the registry host (docker login wants the host, not the repo path).
              'aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin "${REPO_URI%%/*}"',
            ],
          },
          build: {
            commands: [
              // Native arm64 host: no --platform flag, no binfmt/QEMU setup. The x86
              // cross-build guidance in prerequisites.md does not apply on this path.
              'docker build -t "$REPO_URI:$IMAGE_TAG" .',
            ],
          },
          post_build: { commands: ['docker push "$REPO_URI:$IMAGE_TAG"'] },
        },
      }),
    });
    repo.grantPullPush(project);
    // Explicit, version-independent grant: `docker login` needs GetAuthorizationToken,
    // which is account-scoped ('*' is the only valid resource for this action).
    project.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['ecr:GetAuthorizationToken'], resources: ['*'] }),
    );

    this.repository = repo;

    new cdk.CfnOutput(this, 'ImageBuildProjectName', { value: project.projectName });
    new cdk.CfnOutput(this, 'LitellmImageUri', { value: `${repo.repositoryUri}:${this.imageTag}` });

    // cdk-nag — both fired unsuppressed in the real deploy that produced this pattern
    // (suppressed inline so the conditional stack stays self-contained; see §9 note):
    NagSuppressions.addResourceSuppressions(project, [
      {
        id: 'AwsSolutions-CB3',
        reason:
          'Privileged mode is required to run `docker build` inside CodeBuild; the project builds exactly one container image and runs no other workload.',
      },
      {
        id: 'AwsSolutions-CB4',
        reason:
          'Build artifacts/cache use AWS-managed encryption; a customer-managed KMS key is out of scope for the dev sample. PROD TODO: pass encryptionKey.',
      },
    ]);
  }
}
```

Wiring in `bin/app.ts` (between Guardrail and LiteLLM; the fixed stack order gains one conditional member):

```typescript
// ---- 2.7 ImageBuild (conditional: only when litellm.imageBuild.mode === 'codebuild') --
const imageBuild =
  config.litellm.imageBuild?.mode === 'codebuild'
    ? new ImageBuildStack(app, 'ImageBuildStack', stackProps('image-build'))
    : undefined;

// ---- 3. LiteLLM (unchanged except the extra prop) ----------------------------
const litellm = new LiteLLMStack(app, 'LiteLLMStack', {
  /* ...exactly the §0 props... */
  imageBuild, // undefined → default local fromAsset() build
});
```

Image selection inside `LiteLLMStack` (replaces the bare `fromAsset()` call in §4's `addContainer`;
`imageBuild?: ImageBuildExports` is added to `LiteLLMStackProps`):

```typescript
    // Local Docker (default): build at deploy time, ARM64-pinned (§4 WHY comment).
    // CodeBuild path: the image was ALREADY built+pushed by ImageBuildStack —
    // fromEcrRepository() has no build step, so nothing here touches Docker.
    const containerImage = props.imageBuild
      ? ecs.ContainerImage.fromEcrRepository(props.imageBuild.repository, props.imageBuild.imageTag)
      : ecs.ContainerImage.fromAsset(path.join(__dirname, '..', 'services', 'litellm'), {
          platform: ecrAssets.Platform.LINUX_ARM64,
        });

    taskDef.addContainer('litellm', {
      image: containerImage,
      /* ...rest identical to §4... */
    });
```

**The 3-step deploy order (MANDATORY on this path — `cdk deploy --all` alone ships no image):**

```bash
# 1) Deploy ONLY the image-build stack (creates ECR + the CodeBuild project, and
#    uploads services/litellm/ as the S3 source asset):
cdk deploy <prefix>-image-build --outputs-file image-build-outputs.json

# 2) Run the build and WAIT for it — cdk deploy does NOT do this (StartBuild is an
#    API call, not a CloudFormation resource):
BUILD_ID=$(aws codebuild start-build --project-name <prefix>-litellm-image-build \
  --query 'build.id' --output text)
aws codebuild batch-get-builds --ids "$BUILD_ID" \
  --query 'builds[0].{status:buildStatus,phase:currentPhase}'   # poll until SUCCEEDED (~3–6 min)
# Then verify the image actually landed — LiteLLMStack task launch would otherwise
# fail with CannotPullContainerError:
aws ecr describe-images --repository-name <prefix>-litellm \
  --image-ids imageTag=<IMAGE_TAG from LitellmImageUri output>

# 3) Deploy everything else as usual:
cdk deploy --all --outputs-file outputs.json
```

**Rebuild on change**: any edit under `services/litellm/` changes the asset hash → repeat 1→2→3.
Step 1 re-uploads the context and stamps the new `IMAGE_TAG`; step 3 rolls the ECS service *because
the task definition's image tag changed* — which is exactly WHY the tag is the content hash.

**WHY, item by item**:

- **Native ARM build image, not QEMU** — `LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0` runs on
  Graviton, so the ARM64 image that `runtimePlatform.cpuArchitecture: ARM64` expects is built natively.
  The entire "x86 host cross-build / binfmt / exec format error" constraint class (constraints.md)
  does not exist on this path.
- **`ComputeType.ARM_CONTAINER` does not exist** — the single most likely generation error here.
  The CDK API selects ARM via the build image; if you emit `ComputeType.ARM_CONTAINER`, `tsc` fails
  (property does not exist). Real-deploy verified: `LinuxArmBuildImage` + `ComputeType.SMALL` +
  `privileged: true` is the working combination.
- **`StartBuild` is not a CloudFormation resource** — deploying the stack creates a project that has
  *never run*. `cdk deploy` returns long before any image exists, so the deploy is split into the
  explicit 3 steps above. A Custom Resource (Lambda that runs `StartBuild` + polls, so `cdk deploy
  --all` becomes one-shot) is a legitimate *enhancement*, but the explicit 3-step is this skill's
  default: fewer generated moving parts, and the failure surface (a red build) is directly visible
  in the CodeBuild console instead of buried in a CR Lambda's logs.
- **Tag = asset hash, never `:latest`** — with a static tag, a rebuilt image changes nothing in the
  task definition → CloudFormation sees no diff → ECS keeps running the stale image (and
  `fromEcrRepository` pins by tag at synth). The content hash makes "source changed" and "service
  rolls" the same event, and step 2 provably built the exact context step 1 uploaded.
- **`fromEcrRepository()` has no build step** — it only *references* `repo:tag`. If step 2 is skipped
  (or the tag doesn't exist), synth and `cdk deploy` still succeed and the failure appears only at
  task launch as `CannotPullContainerError` — same late-failure shape as the §4 architecture-mismatch
  trap, hence the explicit `describe-images` verification in step 2.
- **cdk-nag CB3/CB4** — privileged mode (required for docker-in-docker builds) and no customer-managed
  KMS key on the build artifacts both fire on every deploy of this stack; the suppressions ship inline
  with written reasons per the skill-wide rule.
- **Scope guard** — this stack exists for "local Docker impossible, everything else fine". If Node/CDK/
  credentials/network are *also* unsuitable, use the EC2 deploy host (`ec2-deploy-host.md`) instead of
  stretching this pattern.

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
        // Public URL for NextAuth callbacks/redirects = the Langfuse domain. Langfuse is only
        // deployed when certMode='acm', so a real domain + ACM cert always exists (no placeholder).
        NEXTAUTH_URL: `https://${config.domainName}`,
        // Headless initialization — auto-create org, project, API keys, admin user
        LANGFUSE_INIT_ORG_ID: 'codeagent-gov',
        LANGFUSE_INIT_ORG_NAME: 'Code Agent Governance',
        LANGFUSE_INIT_PROJECT_ID: 'llm-gateway',
        LANGFUSE_INIT_PROJECT_NAME: 'LLM Gateway Traces',
        LANGFUSE_INIT_USER_EMAIL: 'admin@example.com',
        LANGFUSE_INIT_USER_NAME: 'Admin',
        DATABASE_HOST: data.clusterEndpointHostname,
        DATABASE_PORT: String(data.clusterPort),
        DATABASE_NAME: 'langfuse',
      },
      secrets: {
        DATABASE_USERNAME: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'username'),
        DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(data.langfuseDbSecret, 'password'),
        NEXTAUTH_SECRET: ecs.Secret.fromSecretsManager(appSecret, 'nextauthSecret'),
        SALT: ecs.Secret.fromSecretsManager(appSecret, 'salt'),
        // The project keys are the SAME shared secret LiteLLM consumes (created in
        // DataStack, before both stacks). Injected via Secrets Manager, never plaintext.
        LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ecs.Secret.fromSecretsManager(data.langfuseSharedSecret, 'publicKey'),
        LANGFUSE_INIT_PROJECT_SECRET_KEY: ecs.Secret.fromSecretsManager(data.langfuseSharedSecret, 'secretKey'),
        // Admin bootstrap password — generated secret, never a hard-coded literal.
        LANGFUSE_INIT_USER_PASSWORD: ecs.Secret.fromSecretsManager(appSecret, 'adminPassword'),
      },
    });

    // Service SG owned by THIS stack (SG ownership rule — see NetworkStack/interfaces.ts).
    // Aurora already allows the private-with-egress subnet CIDRs, so no Network SG edit.
    const serviceSg = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc: network.vpc, allowAllOutbound: true,
      description: 'ECS Langfuse tasks', // ASCII only (constraints.md)
    });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: config.desiredCount,
      securityGroups: [serviceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      healthCheckGracePeriod: cdk.Duration.seconds(60),
    });

    // Public ALB — LangfuseStack is instantiated ONLY when certMode='acm' (bin/app.ts), so the
    // Langfuse UI always gets a real domain + ACM cert. http deploys don't deploy
    // Langfuse (CloudWatch-only observability). NEXTAUTH_URL = https://config.domainName.
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId: props.hostedZoneId, zoneName: props.hostedZoneName,
    });
    const certificate = new acm.Certificate(this, 'Cert', {
      domainName: config.domainName,
      validation: acm.CertificateValidation.fromDns(zone),
    });
    const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc: network.vpc, allowAllOutbound: true,
      description: 'Langfuse public ALB (acm) - 443/80 from albIngressCidrs', // ASCII only (constraints.md)
    });
    for (const cidr of props.albIngressCidrs) {
      albSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(443), 'HTTPS from allowlist');
      albSg.addIngressRule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(80), 'HTTP redirect from allowlist');
    }
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(PORTS.LANGFUSE), 'Public ALB to Langfuse'); // same-stack SG ref: safe
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: network.vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      idleTimeout: cdk.Duration.seconds(120),
    });
    alb
      .addListener('Https', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        certificates: [certificate],
        open: false, // CDK default open:true silently adds 0.0.0.0/0 — see constraints.md
      })
      .addTargets('LangfuseTarget', {
        port: PORTS.LANGFUSE,
        protocol: elbv2.ApplicationProtocol.HTTP,
        targets: [service],
        healthCheck: {
          path: '/api/public/health', healthyHttpCodes: '200',
          interval: cdk.Duration.seconds(15), timeout: cdk.Duration.seconds(5),
          healthyThresholdCount: 2, unhealthyThresholdCount: 3,
        },
      });
    alb.addListener('HttpRedirect', {
      port: 80, protocol: elbv2.ApplicationProtocol.HTTP,
      defaultAction: elbv2.ListenerAction.redirect({ protocol: 'HTTPS', port: '443', permanent: true }),
      open: false,
    });
    new route53.ARecord(this, 'Alias', {
      zone, recordName: config.domainName,
      target: route53.RecordTarget.fromAlias(new route53targets.LoadBalancerTarget(alb)),
    });
    // PROD TODO: tighten albIngressCidrs to a corp/NAT egress range — this is an
    // operator/admin trace UI, so restrict who can reach it.

    this.loadBalancer = alb;
    this.langfuseUrl = `https://${config.domainName}`;
    new cdk.CfnOutput(this, 'LangfuseUrl', { value: this.langfuseUrl });
  }
}
```

**WHY — what the golden code above does (all correct — do not regress):**
- **Conditional stack** — if `enableLangfuse=false` it is not even instantiated (`bin/app.ts`). A PoC that does not need observability reduces surface/cost.
- **`appSecret` (NEXTAUTH_SECRET/SALT/adminPassword) is created via `generateSecretString`** → CDK creates the session-signing keys + admin password and injects them via `ecs.Secret`. No plaintext literals.
- **DB credentials (`DATABASE_USERNAME/PASSWORD`) are injected via `ecs.Secret` from `data.langfuseDbSecret`** — correct.
- **The shared trace keys (`LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`SECRET_KEY`) are injected from `data.langfuseSharedSecret`** — the SAME secret LiteLLM consumes, created in DataStack (before both) so there is a single source and no drift.
- **public ALB (acm only)** — Langfuse is deployed only when `certMode='acm'`, as an internet-facing ALB + ACM cert on `config.domainName` (+ Route53 alias, HTTP→443 redirect). `http` deploys skip Langfuse (CloudWatch-only observability). PROD: restrict `albIngressCidrs` (this is an admin trace UI).

### ⚠️ Do NOT regress to plaintext secrets (Hard Constraint #4)

An earlier revision of this pattern hard-coded the trace keys and admin password as plaintext `environment` values. That is a defect — never do this:

```typescript
// ❌ ANTI-PATTERN — plaintext secrets exposed in the CloudFormation template/console/git
LANGFUSE_INIT_PROJECT_PUBLIC_KEY: 'lf_pk_CHANGE_ME',   // trace ingestion public key
LANGFUSE_INIT_PROJECT_SECRET_KEY: 'lf_sk_CHANGE_ME',   // trace ingestion secret key
LANGFUSE_INIT_USER_PASSWORD: 'Admin123!',              // admin password(!)
// ❌ and the matching literals duplicated in litellm-stack.ts environment{}
```

**Why it's a defect:**
1. `environment` values are exposed as-is in the ECS task definition → **plaintext CloudFormation template → console/`describe-task-definition`/git**.
2. An admin password literal is critical exposure even behind an internal ALB.
3. LiteLLM and Langfuse **redundantly hardcode the same literal on both sides** → on rotation you must fix both stacks at once, with drift risk.

### ✅ The pattern (already applied in the golden code above) — Secrets Manager + ecs.Secret + shared key in an earlier stack

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

## 6. AuthStack — auth-mode Token Service (org-sso IAM/SigV4 or cognito-native Cognito authorizer)

AuthStack fronts a VPC Lambda that returns or issues a LiteLLM virtual key cached in DynamoDB. The generated stack must branch concretely on `config.authMode`:

- `org-sso` (default): API Gateway REST method uses `AuthorizationType.IAM`; Token Lambda trusts `requestContext.identity.userArn`, parses `AWSReservedSSO_...`, and maps permission set name to `team_alias`.
- `cognito-native`: AuthStack **creates** an Amazon Cognito User Pool (the sole identity source — no external IdP, no IdC), a Hosted UI domain, an app client (Authorization Code + PKCE, loopback redirect), and one **User Pool Group per team**. The API Gateway REST method uses a **Cognito User Pools authorizer**, which validates the JWT before the Lambda runs; the Token Lambda reads the verified `cognito:groups` claim from `requestContext.authorizer.claims` and maps the single matching group name to `team_alias`. **No Identity Store, no `identitystore:*` IAM.**

> ⚠️ Do NOT generate an IdC-federated `account-sso` variant. An IdC account instance cannot host a SAML 2.0 customer-managed application (AWS-confirmed), so Cognito↔IdC federation is impossible; `cognito-native` uses Cognito as the sole store precisely to sidestep that. The API Gateway Cognito authorizer accepts only the **access token** (`token_use=access`); an id_token returns 401.

```typescript
export class AuthStack extends cdk.Stack implements AuthExports {
  public readonly tokenServiceApiUrl: string;
  public readonly tokenServiceInvokeUrl: string;
  public readonly keyCacheTable: dynamodb.ITable;
  public readonly keyCacheTableName: string;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);
    const { config, network, litellm } = props;
    const authMode = config.authMode ?? 'org-sso';

    const table = new dynamodb.Table(this, 'KeyCache', {
      tableName: ns('key-cache'),
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: DYNAMO.TTL_ATTRIBUTE,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY, // dev sample; PROD TODO: RETAIN
    });
    this.keyCacheTable = table;
    this.keyCacheTableName = table.tableName;

    // ---- cognito-native: Cognito User Pool is the SOLE identity source --------
    // Created only in cognito-native mode. No external IdP, no IdC federation.
    let userPool: cognito.UserPool | undefined;
    let userPoolClient: cognito.UserPoolClient | undefined;
    let userPoolDomain: cognito.UserPoolDomain | undefined;
    const cognitoNative = config.cognitoNative ?? {};

    if (authMode === 'cognito-native') {
      userPool = new cognito.UserPool(this, 'UserPool', {
        userPoolName: ns('native-pool'),
        selfSignUpEnabled: false,
        signInAliases: { email: true },
        standardAttributes: { email: { required: true, mutable: true } },
        passwordPolicy: {
          minLength: cognitoNative.passwordMinLength ?? 12,
          requireLowercase: true, requireUppercase: true, requireDigits: true, requireSymbols: true,
        },
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      // ⚠️ Hosted UI domain prefixes are GLOBALLY unique across ALL AWS accounts — a bare
      // ns('auth') ("llmgw-dev-auth") can already be taken by another AWS customer, and the
      // CFN failure then reads, misleadingly, "domain ... does not exist" (it means
      // AlreadyExists — real-deploy incident). The account-id suffix restores uniqueness.
      userPoolDomain = userPool.addDomain('Domain', {
        cognitoDomain: { domainPrefix: `${ns('auth')}-${this.account}`.replace(/[^a-z0-9-]/g, '') },
      });
      // One User Pool Group per team; the group name IS the LiteLLM team_alias, 1:1.
      // Cognito auto-stamps `cognito:groups` into every token based on membership.
      for (const team of Object.values(TEAMS)) {
        new cognito.CfnUserPoolGroup(this, `Group-${team}`, { userPoolId: userPool.userPoolId, groupName: team });
      }
      userPoolClient = userPool.addClient('AppClient', {
        userPoolClientName: ns('llmgw-login-client'),
        generateSecret: false,
        oAuth: {
          flows: { authorizationCodeGrant: true },
          scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
          callbackUrls: ['http://127.0.0.1:8400/callback', 'http://localhost:8400/callback'],
          logoutUrls: ['http://127.0.0.1:8400/logout', 'http://localhost:8400/logout'],
        },
        supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
        preventUserExistenceErrors: true,
        // How long `llmgw-login` sessions last before re-login. Cognito default 30d.
        refreshTokenValidity: cdk.Duration.days(cognitoNative.refreshTokenValidityDays ?? 30),
      });
    }

    const fn = new lambda.Function(this, 'TokenService', {
      functionName: ns('token-service'),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'token-service')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc: network.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [network.lambdaSecurityGroup as ec2.SecurityGroup],
      environment: {
        CONFIG_TABLE_NAME: table.tableName,
        LITELLM_MASTER_KEY_ARN: litellm.masterKeySecret.secretArn,
        LITELLM_ENDPOINT_SSM: SSM.LITELLM_INTERNAL_URL,
        KEY_CACHE_TTL_SECONDS: String(config.keyCacheTtlSeconds),
        // Virtual keys EXPIRE after this window (default 1h) — SSO/Cognito revocation alone
        // never kills an already-issued key, so this bounds residual access; the Lambda also
        // caps the cache TTL below it. Helpers re-mint silently while the session lives, so
        // a short duration does NOT change login cadence (real-deploy validated).
        // See lambda-handlers.md + litellm-admin-guide.md → Offboarding.
        KEY_DURATION_SECONDS: String(config.keyDurationSeconds ?? 3600),
        RESPONSE_KEY: TOKEN_SERVICE.RESPONSE_KEY,
        AUTH_MODE: authMode,
        // org-sso input
        SSO_ARN_PREFIX_REQUIRED: String(authMode === 'org-sso'),
        // cognito-native inputs; empty in org-sso mode
        COGNITO_TEAM_GROUP_PREFIX: cognitoNative.teamGroupPrefix ?? '',
        COGNITO_MULTI_GROUP_STRATEGY: cognitoNative.multiGroupStrategy ?? 'require-single-team-group',
      },
    });

    table.grantReadWriteData(fn);
    litellm.masterKeySecret.grantRead(fn);
    ssm.StringParameter.fromStringParameterName(this, 'LiteLlmUrlParam', SSM.LITELLM_INTERNAL_URL)
      .grantRead(fn);
    // NOTE: cognito-native grants NO identitystore:* — team membership comes from the
    // JWT's cognito:groups claim, verified by the API Gateway authorizer, not Identity Store.

    const api = new apigw.RestApi(this, 'Api', {
      restApiName: ns('token-service'),
      description: `Token Service virtual key issuance (${authMode})`,
      deployOptions: { stageName: 'v1' },
    });
    const token = api.root.addResource('auth').addResource('token');

    if (authMode === 'org-sso') {
      token.addMethod('POST', new apigw.LambdaIntegration(fn), {
        authorizationType: apigw.AuthorizationType.IAM,
      });
    } else {
      // cognito-native: the authorizer validates the Cognito access token
      // (signature/issuer/audience/expiry) before the Lambda runs, exposing the
      // verified claims (incl. cognito:groups) at requestContext.authorizer.claims.
      const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'CognitoNativeAuthorizer', {
        cognitoUserPools: [userPool!],
        identitySource: 'method.request.header.Authorization',
      });
      token.addMethod('POST', new apigw.LambdaIntegration(fn), {
        authorizationType: apigw.AuthorizationType.COGNITO,
        authorizer,
        authorizationScopes: ['openid', 'email', 'profile'],
      });
    }

    this.tokenServiceApiUrl = api.url;
    this.tokenServiceInvokeUrl = `${api.url}auth/token`;

    new cdk.CfnOutput(this, 'TokenServiceUrl', { value: this.tokenServiceInvokeUrl });
    new cdk.CfnOutput(this, 'KeyCacheTableName', { value: table.tableName });

    if (authMode === 'org-sso') {
      new cdk.CfnOutput(this, 'SsoStartUrl', { value: config.sso!.startUrl });
      new cdk.CfnOutput(this, 'SsoRegion', { value: config.sso!.region });
      new cdk.CfnOutput(this, 'SsoAccountId', { value: config.sso!.accountId });
      new cdk.CfnOutput(this, 'SsoRoleName', { value: config.sso!.roleName });
    } else {
      // cognito-native onboarding outputs (feed gateway_auth.py config).
      new cdk.CfnOutput(this, 'CognitoUserPoolId', { value: userPool!.userPoolId });
      new cdk.CfnOutput(this, 'CognitoAppClientId', { value: userPoolClient!.userPoolClientId });
      new cdk.CfnOutput(this, 'CognitoHostedUiDomain', { value: `${userPoolDomain!.domainName}.auth.${this.region}.amazoncognito.com` });
      new cdk.CfnOutput(this, 'CognitoIssuer', { value: `https://cognito-idp.${this.region}.amazonaws.com/${userPool!.userPoolId}` });
      new cdk.CfnOutput(this, 'CognitoTeamGroupPrefix', { value: cognitoNative.teamGroupPrefix ?? '' });
      new cdk.CfnOutput(this, 'LoginCommand', { value: 'llmgw-login' });
    }
  }
}
```

**Config schema for `cognitoNative` (all fields optional — defaults applied in AuthStack):**

```typescript
export interface CognitoNativeConfig {
  readonly teamGroupPrefix?: string;        // scope which groups count as teams (e.g. "llmgw-")
  readonly multiGroupStrategy?: 'require-single-team-group';
  readonly refreshTokenValidityDays?: number; // 1-3650, default 30 (how long llmgw-login lasts)
  readonly passwordMinLength?: number;        // 6-99, default 12
}
```

> `AccountSsoConfig` may remain in the schema as a **deprecated** interface for old config snapshots, but `authMode='account-sso'` must not be generated (IdC account instances cannot host the SAML app it assumed). Use `cognito-native`.

**WHY — auth plane:**
- `org-sso` preserves backward compatibility: API Gateway IAM auth, SigV4 helper, and `AWSReservedSSO_` ARN parsing remain the trust anchor.
- `cognito-native` creates its own Cognito User Pool (sole identity source) + Hosted UI + PKCE app client + one User Pool Group per team. The API Gateway Cognito authorizer validates the access token; the Lambda consumes only the verified `cognito:groups` claim — **no Identity Store call, no `identitystore:*` IAM**.
- Team membership is the `cognito:groups` claim, mapped 1:1 unbranched to `team_alias` (filtered by `teamGroupPrefix`). Onboarding a new team is Cognito console work (create group + add users), never a redeploy.
- The same DynamoDB cache, Secrets Manager master key read, and SSM LiteLLM endpoint lookup are reused across both modes.

---

## 7. ObservabilityStack — CloudWatch usage dashboard (tokens · users · time · health)

The post-deploy dashboard. Data source is the **`cloudwatch_usage` EMF callback**
(litellm-gateway.md §4): every completed request emits one EMF record into the
`METRICS.NAMESPACE` namespace (dimensions `Model` / `Team`, caller identity as a
log **property**). The dashboard shows token usage by model & team, per-user
top-N and hourly activity (Logs Insights over the same records), latency, spend,
failures, and ALB health. CloudWatch covers usage/infra/cost; Langfuse (optional)
covers the prompt/trace level.

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
      defaultInterval: cdk.Duration.hours(24),
    });

    // Usage metrics: models/teams are created at RUNTIME (Token Service creates
    // teams on first login), so their dimension values are unknowable at synth.
    // SEARCH expressions discover every {Model} / {Team} series dynamically.
    // ⚠️ SEARCH is dashboard-only — it is NOT valid in CloudWatch alarms; alarm
    // on explicit per-model metrics if alarms are added later.
    // `label` uses CloudWatch DYNAMIC LABELS (${PROP('Dim.<name>')}) so each series
    // in the legend shows just its dimension value (e.g. "claude-fable-5") instead
    // of the raw SEARCH expression string (the ugly default for SEARCH results).
    const usage = (metricName: string, dim: 'Model' | 'Team', stat: string, label?: string) =>
      new cloudwatch.MathExpression({
        expression: `SEARCH('{${METRICS.NAMESPACE},${dim}} MetricName="${metricName}"', '${stat}')`,
        usingMetrics: {},
        label: label ?? `\${PROP('Dim.${dim}')}`,
        period: cdk.Duration.minutes(5),
      });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          `# ${ns('overview')}`,
          '',
          `**LiteLLM**: ${litellm.publicHttpsUrl}`,
          `**Token Service**: ${auth.tokenServiceInvokeUrl}`,
          langfuse ? `**Langfuse**: ${langfuse.langfuseUrl}` : '**Langfuse**: disabled',
          '',
          `Usage metrics: EMF namespace \`${METRICS.NAMESPACE}\` (cloudwatch_usage callback). Per-user tables query the LiteLLM log group.`,
        ].join('\n'),
        width: 24,
        height: 4,
      }),
    );

    // ---- Row 1: token usage --------------------------------------------------
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Total tokens by model',
        left: [usage('TotalTokens', 'Model', 'Sum')],
        width: 12, height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Total tokens by team',
        left: [usage('TotalTokens', 'Team', 'Sum')],
        width: 12, height: 6,
      }),
    );

    // ---- Row 2: token split + spend -------------------------------------------
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Prompt vs completion tokens (by model)',
        left: [usage('PromptTokens', 'Model', 'Sum', "prompt ${PROP('Dim.Model')}")],
        right: [usage('CompletionTokens', 'Model', 'Sum', "completion ${PROP('Dim.Model')}")],
        width: 12, height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Spend USD by team (LiteLLM cost calc)',
        left: [usage('SpendUSD', 'Team', 'Sum')],
        width: 12, height: 6,
      }),
    );

    // ---- Row 3: request health + latency ---------------------------------------
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Requests / failures by model',
        left: [usage('Requests', 'Model', 'Sum', "requests ${PROP('Dim.Model')}")],
        right: [usage('Failures', 'Model', 'Sum', "failures ${PROP('Dim.Model')}")],
        width: 12, height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Latency by model (avg / p99, ms)',
        left: [usage('LatencyMs', 'Model', 'Average', "avg ${PROP('Dim.Model')}")],
        right: [usage('LatencyMs', 'Model', 'p99', "p99 ${PROP('Dim.Model')}")],
        width: 12, height: 6,
      }),
    );

    // ---- Row 4: per-user + time-of-use (Logs Insights over the EMF records) ----
    // The caller identity is an EMF PROPERTY (unbounded cardinality — never a
    // dimension), so per-user views are Logs Insights queries, not metrics.
    // History is bounded by the log group retention (TWO_WEEKS in LiteLLMStack);
    // long-range per-user reporting lives in the LiteLLM Admin UI / DB.
    const usageLogs = [litellm.logGroup.logGroupName];
    dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Top users by token usage (dashboard time range)',
        logGroupNames: usageLogs,
        view: cloudwatch.LogQueryVisualizationType.TABLE,
        queryLines: [
          'filter llmgw = "usage"',
          'stats sum(TotalTokens) as total_tokens, sum(PromptTokens) as prompt,'
            + ' sum(CompletionTokens) as completion, sum(Requests) as requests,'
            + ' sum(SpendUSD) as spend_usd by User, Team',
          'sort total_tokens desc',
          'limit 20',
        ],
        width: 12, height: 8,
      }),
      new cloudwatch.LogQueryWidget({
        title: 'Usage by hour (tokens · requests · active users)',
        logGroupNames: usageLogs,
        view: cloudwatch.LogQueryVisualizationType.LINE,
        queryLines: [
          'filter llmgw = "usage"',
          'stats sum(TotalTokens) as tokens, sum(Requests) as requests,'
            + ' count_distinct(User) as active_users by bin(1h)',
        ],
        width: 12, height: 8,
      }),
    );

    // ---- Row 5: per-user drill-down + edge health -------------------------------
    const alb = litellm.loadBalancer;
    dashboard.addWidgets(
      new cloudwatch.LogQueryWidget({
        title: 'Per-user tokens by model (drill-down)',
        logGroupNames: usageLogs,
        view: cloudwatch.LogQueryVisualizationType.TABLE,
        queryLines: [
          'filter llmgw = "usage"',
          'stats sum(TotalTokens) as total_tokens, sum(Requests) as requests by User, Model',
          'sort total_tokens desc',
          'limit 50',
        ],
        width: 12, height: 8,
      }),
      new cloudwatch.GraphWidget({
        title: 'LiteLLM ALB — requests / 5xx / target latency',
        left: [
          alb.metrics.requestCount({ statistic: 'Sum' }),
          alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, { statistic: 'Sum' }),
        ],
        right: [alb.metrics.targetResponseTime({ statistic: 'p95' })],
        width: 12, height: 8,
      }),
    );

    new cdk.CfnOutput(this, 'DashboardName', { value: this.dashboardName });
    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${this.dashboardName}`,
    });
  }
}
```

**WHY:**
- **Toggleable** — if `dashboardEnabled=false`, there is no dashboard, only a `(disabled)` output. Cost control.
- **Usage data comes from EMF, not PutMetricData** — the LiteLLM container just prints one JSON line per request to stdout (litellm-gateway.md §4); the existing awslogs pipeline delivers it and CloudWatch extracts the metrics. No extra IAM, no API calls from the hot path, nothing new to deploy at runtime.
- **`Model`/`Team` are dimensions, the user is a property.** Every distinct dimension combination is a billable custom metric; models and teams are small bounded sets, but users are unbounded. Per-user and per-hour views therefore run as **Logs Insights query widgets** over the same EMF records (`filter llmgw = "usage"`) — zero metric cost, still on the one dashboard. Their history equals the log retention (2 weeks); longer-range per-user reporting is the LiteLLM Admin UI / DB.
- **SEARCH expressions, not synth-time dimension lists** — teams (and future models) are created at runtime, so widgets discover series dynamically. SEARCH is not valid in alarms; if alarms are added later, define explicit per-model `cloudwatch.Metric` objects.
- **Always set a dynamic `label` on SEARCH expressions** — without one, every legend entry renders the raw expression string (`SEARCH('{...,Model} MetricName="TotalTokens"', 'Sum') claude-fable-5`). `label: "${PROP('Dim.Model')}"` (CloudWatch dynamic labels) shows just the dimension value; prefix a literal (`"p99 ${PROP('Dim.Model')}"`) when one widget graphs two stats of the same metric.
- **Uses the `alb.metrics.*` helpers** — pulls edge metrics directly from the ALB object (LiteLLMExports). `langfuse` is optional, so it branches with a ternary.
- **Cross-layer mapping**: receives the exports of all three planes (LiteLLM/Auth/Langfuse) as props — including `litellm.logGroup` for the Logs Insights widgets — and gathers usage + infra onto one screen. CloudWatch (usage/infra/cost) + Langfuse (prompt/trace) split roles.

---

## 8. ~~CdnStack~~ — removed (CloudFront eliminated)

CloudFront / `CdnStack` has been **removed**. The ALB is now the edge: TLS is chosen by
`config.litellm.certMode` (`acm` / `http`) in `LiteLLMStack` (§4) — always internet-facing, SG
ingress restricted to `albIngressCidrs` — and Langfuse
(acm only) gets its own public ALB (§5). There is no CloudFront distribution, no us-east-1 edge
certificate, and no Location-rewrite Function — UI redirects rely on
`PROXY_BASE_URL` alone (never `--forwarded-allow-ips` — the pinned image's CLI lacks it; see constraints.md). The old 120s VPC-Origin timeout ceiling is gone; the ALB `idleTimeout`
(default 900s) governs long completions.

---

## 9. nag-suppressions — `lib/nag-suppressions.ts` (latest — no CFR*, CloudFront removed)

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
        reason: 'Public ALB SG ingress comes from the albIngressCidrs Discovery answer; 0.0.0.0/0 appears only if the user chose it (GATE-1 acknowledged). PROD TODO: restrict ingress CIDRs.',
      },
      // Cognito findings (cognito-native only — no-op suppressions in org-sso, where no
      // User Pool exists). Both fired unsuppressed in a real deploy:
      {
        id: 'AwsSolutions-COG2',
        reason: 'cognito-native dev sample: MFA is intentionally not required on the User Pool (email+password with a strong password policy + PKCE loopback flow). PROD TODO: require MFA (TOTP).',
      },
      {
        id: 'AwsSolutions-COG8',
        reason: 'cognito-native dev sample: the Cognito Plus feature plan (threat protection / advanced security) is not enabled to limit cost. PROD TODO: enable the Plus feature plan with ENFORCED threat protection.',
      },
      // CloudFront suppressions (CFR2/CFR3/CFR4/CFR5) removed — CloudFront is gone, so cdk-nag no
      // longer emits CFR* (there is no distribution). PROD TODO instead: tighten albIngressCidrs
      // and enable ALB access logs (see per-resource ELB2/EC23 below). No AWS WAF is deployed.
      // NOTE: the conditional ImageBuildStack (§4-1, imageBuild.mode='codebuild' only) carries its
      // CB3 (privileged docker-in-docker build) / CB4 (AWS-managed encryption) suppressions INLINE
      // in the stack — do not add them here, where they'd apply (as dead weight) to every stack.
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

  // ---- LiteLLMStack: ALBs ----------------------------------------------------
  // NOTE: the construct ids are 'InternalAlb' and 'PublicAlb' (there is no 'Alb') —
  // addResourceSuppressionsByPath THROWS at synth if the path does not exist.
  suppress('litellm', '/LiteLLMStack/InternalAlb/Resource', [
    { id: 'AwsSolutions-ELB2', reason: 'Access logs omitted for the dev sample (no log bucket provisioned). PROD TODO: enable ALB access logs to S3.' },
  ]);
  suppress('litellm', '/LiteLLMStack/PublicAlb/Resource', [
    { id: 'AwsSolutions-ELB2', reason: 'Access logs omitted for the dev sample (no log bucket provisioned). PROD TODO: enable ALB access logs to S3.' },
  ]);
  // The public ALB SG lives in LiteLLMStack (PublicAlbSg). EC23 fires only when the user
  // answered 0.0.0.0/0 for albIngressCidrs (a Discovery answer + GATE-1 acknowledgement).
  suppress('litellm', '/LiteLLMStack/PublicAlbSg/Resource', [
    { id: 'AwsSolutions-EC23', reason: 'Ingress CIDRs come from the albIngressCidrs Discovery answer; 0.0.0.0/0 (if chosen) was explicitly acknowledged at GATE 1. PROD TODO: restrict to a corp/NAT egress CIDR.' },
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
- **TLS terminates at the ALB** (`acm` HTTPS:443; `http` mode serves plain HTTP:80 by design — PoC only); the ECS target is HTTP inside the VPC. With CloudFront removed there is no origin-protocol (CFR5) concern anymore.
- **`ECS2` (env var)** — non-sensitive env (model aliases/region/SSM names) is intentionally in environment. It states that **all secrets use ecs.Secret** (though §5's Langfuse plaintext keys are an exception to this justification — they are actually an anti-pattern).
- **`APIG2` (request body validation)** — the body is `{}` and identity comes from the SigV4 ARN, so there is no schema to validate.

*Inherent to the dev sample (with explicit prod TODOs):*
- **`IAM5`/`IAM4`** — Bedrock/agentcore choose the model ARN at runtime, hence `*`. In prod, scope to model/gateway ARNs.
- **`SMG4`** — secret auto-rotation not implemented. Enable in prod.
- **`VPC7`/`EC23`** — flow logs omitted, ALB 0.0.0.0/0 (443). Enable/restrict CIDR in prod.
- **`RDS6`/`RDS10`** — Secrets Manager password auth (no IAM DB auth), deletion protection off (DESTROY for clean teardown).
- **`ELB2`/`APIG1`/`APIG6`/`APIG3`/`ECS4`** — various access/exec logging and Container Insights omitted (cost). Prod TODO.

*Edge (ALB, CloudFront removed):*
- **`ELB2` (ALB access logs)** — omitted for the dev sample (no log bucket). PROD TODO: enable ALB access logs to S3.
- **`EC23` (public ALB SG 0.0.0.0/0)** — only present if the user answered `0.0.0.0/0` for `albIngressCidrs` (a Discovery answer; for `http` mode it is an explicit GATE-1 acknowledgement). PROD TODO: restrict to a corp/NAT egress CIDR. (No CloudFront CFR* — there is no distribution; no WAF is deployed.)

> **Core principle:** a suppression is not "turning off security" but **"documenting the intent/limitation"**. The trailing `true` in `addResourceSuppressionsByPath(..., true)` means it applies to child resources. Security essentials (IAM auth, TLS termination, Secrets Manager) are not in the suppression list and are enforced by jest.

---

## Appendix — cross-layer mapping summary (cross-layer map)

| Producer | export field | Consumer | how consumed |
|---|---|---|---|
| Network | `vpc`, shared-infra SGs (aurora/lambda/vpce/agentcore), `privateSubnetCidrs` | Data / LiteLLM / Langfuse / Auth | VPC + shared-infra SGs via props; app stacks own their service/ALB SGs (SG ownership rule — no cross-stack SG refs) |
| Data | `litellmDbSecret` | LiteLLM | inject DB credentials via `ecs.Secret` |
| Data | `langfuseDbSecret`, `clusterEndpointHostname/Port` | Langfuse | DB connection via `ecs.Secret` + env |
| Guardrail | `guardrailId`, `guardrailVersion` | LiteLLM | env var → ApplyGuardrail on every Claude request |
| LiteLLM | `masterKeySecret` | Auth | `grantRead` (read only) → `/key/generate` |
| LiteLLM | `internalUrlSsmParameterName` | Auth | runtime lookup **by SSM name** (avoids deploy cross-ref) |
| LiteLLM | `loadBalancer` | Observability | ALB metrics (public ALB = edge; internal ALB = Token Service path) |
| LiteLLM | `logGroup` | Observability | Logs Insights widgets over the cloudwatch_usage EMF records (per-user / hourly usage) |
| Langfuse | `loadBalancer` (acm only) | Observability | ALB metrics |
| LiteLLM/Auth/Langfuse | URLs·surfaces | Observability | dashboard widgets |
| (config) `litellm.certMode` | acm/http | LiteLLMStack | selects the public ALB listener (HTTPS:443 vs HTTP:80; no CloudFront/CDN, no WAF) |

**The whole flow in one line:**
login (`org-sso`: `aws sso login` → SigV4 to API GW `IAM` → Token Lambda validates `AWSReservedSSO_`; `cognito-native`: `llmgw-login` → Cognito access token → API GW Cognito authorizer → Token Lambda reads `cognito:groups`) → issues a virtual key via DynamoDB cache / `/key/generate` → client uses the virtual key as Bearer to the gateway URL (public ALB, TLS per certMode) → LiteLLM (ECS) → Claude via Task Role SigV4 (+Guardrail) / Mantle via runtime-minted `BEDROCK_MANTLE_API_KEY` Bearer / AgentCore via SigV4 → traces go to Langfuse.
