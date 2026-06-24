---
name: llm-gateway-governance
description: |
  Build a governed LLM gateway that lets internal developers use code agents (Claude Code,
  Codex) against Amazon Bedrock through a single control point — enforcing SSO identity,
  per-user virtual keys, model/cost tiering, Bedrock Guardrails, managed web search, and tracing.
  Generates AWS CDK (TypeScript): VPC + Aurora Serverless v2 + Bedrock Guardrail +
  AgentCore Web Search gateway + LiteLLM on ECS Fargate + optional Langfuse + an IAM/SSO token
  service + CloudFront + cross-region Mantle (GPT-5.x) VPC peering. Use when the user asks to
  "build an LLM gateway", "govern Bedrock access for developers", "central proxy for Claude
  Code / Codex", "LiteLLM on AWS", or "SSO virtual keys for LLMs".
license: MIT
metadata:
  version: "1.1"
  author: aws-solution-skills
  reference-implementation: llm-gateway-multi-agent
---

# LLM Gateway Governance

## Purpose

Generate a production-shaped **code-agent governance gateway** on AWS: a single LiteLLM proxy
(on ECS Fargate) that internal developers reach with **SSO-minted per-user virtual keys**, with
**Bedrock Guardrails**, **model/cost tiering**, **managed web search (AgentCore)**, **network
isolation**, and **observability**. The solution lets an org give developers Claude Code / Codex
access to Amazon Bedrock while centrally controlling *who*, *which models*, *how much*, *what
content*, and *with what audit trail* — without requiring LiteLLM Enterprise.

This skill produces AWS CDK (TypeScript), Lambda (Python), the LiteLLM container config, and
developer-onboarding scripts, customized to the user's domain (custom domain or not, Langfuse or
not, which models, which tiers, **which region**, web search on/off).

## Knowledge sources

Read these before generating. All real knowledge lives in `shared/`:

- `shared/reference/architecture.md` — the **11-stack** architecture, request lifecycle, and the "why"
- `shared/reference/decision-tree.md` — map Discovery answers → `config/dev.json` + stack choices (region, web search, Mantle)
- `shared/reference/aws-services.md` — service/model catalog (verify volatile IDs via MCP)
- `shared/reference/constraints.md` — failure modes & gotchas (bootstrap, CFR4, Mantle guardrail, secrets, AgentCore web search, Mantle peering, Marketplace, region)
- `shared/reference/sso-setup.md` — IAM Identity Center Discovery + provisioning + the generated `config.sso` block & AuthStack outputs
- `shared/patterns/cdk-stacks.md` — full CDK source for the platform stacks + interfaces + config validation
- `shared/patterns/agentcore-websearch.md` — **AgentCore Web Search gateway** stack (Gateway + built-in `web-search` connector) + LiteLLM wiring (replaces Tavily)
- `shared/patterns/mantle-peering.md` — **Bedrock Mantle in us-east-1 via cross-region VPC peering** (MantleNetworkStack + MantlePeeringRoutesStack)
- `shared/patterns/lambda-handlers.md` — SSO Token Service + db-init Custom Resource (Python)
- `shared/patterns/litellm-gateway.md` — LiteLLM `config.yaml`, Dockerfile, entrypoint, Mantle SigV4 overlay
- `shared/patterns/developer-onboarding.md` — token helper, setup script, Claude Code / Codex client config
- `shared/examples/` — domain instantiations (enterprise SSO, domain-less PoC, economy tiering)

## Workflow

### Phase 1: Discovery (ask only what you don't know)
1. **Domain?** Custom domain + Route53 hosted zone, or domain-less (default `*.cloudfront.net`)?
2. **Models?** Which Claude / GPT(Mantle) models? **Per-org governance (optional)**: which SSO groups/teams (typically named by org/team) should get their own budget cap + model allowlist? Map each org SSO group → its own LiteLLM team. ("economy/standard" is just one worked example of this pattern — not a required split.)
3. **Observability?** Langfuse (prompt/trace level) on, or CloudWatch only?
4. **Region & account?** Target gateway region (`config.awsRegion`, **authoritative**). AgentCore Web Search, CDN, and Mantle are pinned to **us-east-1** — so confirm Claude access in the gateway region and GPT-5.x (Mantle) + Web Search access in us-east-1.
5. **Web search?** Use the managed **AgentCore Web Search Tool** (built-in `web-search` connector on an AgentCore Gateway, us-east-1)? Or no web search? (Tavily/3rd-party API keys are no longer used.)
6. **SSO (IAM Identity Center)?** Is IdC enabled + in which region? Identity source (IdC directory vs external IdP)? **Permission set: create a NEW one for this gateway or reuse an existing one — and what name?** (Default to creating a new, uniquely-named one; a name match like `ClaudeCodeUser` is NOT proof of ownership — never silently reuse/edit a pre-existing permission set, as it may belong to other groups/another gateway.) **Which group(s) or users to assign?** Optional tier mapping. These populate `config.sso`. See `shared/reference/sso-setup.md`. If IdC isn't ready, flag it as a prerequisite at GATE 1 (the gateway rejects non-SSO callers by design).

⛔ **GATE 1**: summarize requirements + the resulting `config/dev.json` (incl. `awsRegion`, `sso`, `agentcore`, `mantle`); await confirmation.

### Phase 2: Architecture Design
- Apply `shared/reference/decision-tree.md` to choose `certMode`/`useCustomDomain`, `enableLangfuse`, tiers, capacity, region, web search, Mantle peering.
- **Verify model IDs + regional availability via AWS Knowledge MCP** (`aws___search_documentation`, `aws___get_regional_availability`) — never hard-code stale IDs. Confirm Web Search + Mantle PrivateLink in us-east-1.
- Produce the stack list (Network → Data → Guardrail → **AgentCoreGateway(us-east-1)** → LiteLLM → Langfuse? → Auth → Observability → CDN(us-east-1) → **MantleNetwork(us-east-1)** → **MantlePeeringRoutes**) and a cost estimate.

⛔ **GATE 2**: present architecture + cost; await confirmation.

### Phase 3: Code Generation
- Emit from `shared/patterns/`: `bin/app.ts`, `lib/*-stack.ts` (incl. `agentcore-gateway-stack.ts`, `mantle-network-stack.ts`, `mantle-peering-routes-stack.ts`), `lib/interfaces.ts`, `lib/config/{constants,schema}.ts` (incl. `awsRegion`, `sso`, `agentcore`, `mantle`), `lib/nag-suppressions.ts`, `lambda/token-service/handler.py`, `lambda/db-init/handler.py`, `services/litellm/{config.yaml,Dockerfile,entrypoint.sh}`, `scripts/*`, `templates/*`.
- Make CdnStack **domain-optional** (Hard Constraints #1) and raise its LiteLLM origin timeout to 60s (Hard Constraints #10).
- Wire web search via the AgentCore Gateway (Hard Constraints #11) and Mantle via cross-region peering (Hard Constraints #12).
- Wire secrets through Secrets Manager — **never hard-code credentials** (Hard Constraints #4).

### Phase 4: Validate
- `npm install && npm run typecheck && npx cdk synth --all`.
- Resolve cdk-nag findings: suppress with written justification (`PROD TODO`) where they are intentional dev tradeoffs (e.g., `CFR4`, `CFR2`, `IAM5`), fix genuine issues.
- Verify `data.engineVersion` exists in the target region; confirm `mantle.peerVpcCidr` does not overlap `network.vpcCidr`.

### Phase 5: Deploy
- Ensure Docker is running (LiteLLM image builds via `fromAsset`).
- **Bootstrap us-east-1 AND the gateway region**: `cdk bootstrap aws://<acct>/<awsRegion> aws://<acct>/us-east-1` — if leftovers block it, use a **custom qualifier** (Hard Constraints #2).
- `cdk deploy --all --require-approval never --outputs-file outputs.json` (or a subset by stack name).
- Run developer onboarding (`scripts/setup-developer.sh`) using the CloudFront domain + Token Service URL + SSO outputs.
- **SSO provisioning (if SSO path)**: per the Phase 1 decision, **create** the permission set(s) — name with **no underscore**, prefer a new uniquely-named one (do NOT reuse a pre-existing permission set just because the name matches) — with an `execute-api:Invoke`-only inline policy whose `Resource` is `arn:aws:execute-api:<config.awsRegion>:<account>:<tokenServiceApiId>/*` (region + API id MUST match the deployed Token Service, else every SSO call 403s), assign to the account (the user-specified **group**(s) or users), `provision-permission-set`, and hand off password activation (IdC console only). Follow `shared/reference/sso-setup.md`.
- **Mantle warm-up**: after a fresh-account deploy, make one call per GPT-5.x model to trigger the Marketplace auto-subscribe (first call may transiently 5xx for ~1 min).

### Phase 6: Developer Onboarding — ALWAYS present this as the final output
After a successful deploy you **MUST** end by presenting a ready-to-paste setup guide for **both Claude Code and Codex**, filled with the actual deployed values (CloudFront LiteLLM domain, Token Service URL, deploy region, model aliases, SSO outputs). Source the exact content from `shared/patterns/developer-onboarding.md`. The guide must include:
- **Prerequisite**: `aws sso login --profile <sso-profile>`. The Token Service only accepts IAM Identity Center principals (`AWSReservedSSO_` ARN); a non-SSO caller is rejected with 403 **by design** — state this.
- **Claude Code** (`~/.claude/settings.json`): `ANTHROPIC_BASE_URL=https://<cloudfront-domain>`, `AWS_REGION=<deploy-region>`, model aliases, `apiKeyHelper` → the token helper, and `permissions.deny: ["WebSearch"]` so Claude uses the AgentCore Web Search MCP instead of its (unsupported) built-in WebSearch.
- **Codex** (`~/.codex/config.toml`): `base_url=https://<cloudfront-domain>/v1`, `wire_api=responses`, `model=<gpt-alias>`, `web_search="disabled"`, and `[model_providers.*.auth].command` → the token helper.
- **Web search**: register the MCP server `websearch` (`https://<cloudfront-domain>/mcp/`, Bearer virtual key) once; the tool is `websearch-web-search-tool___WebSearch`.
- **Region (no hardcode)**: the token helper derives the SigV4 signing region from the Token Service URL host, so it works in **any** deploy region with no edit.
- **Quick admin test (no SSO)**: for a single operator, use the LiteLLM master key directly as the bearer against `https://<cloudfront-domain>/v1` (skips the Token Service).
- **Verify**: `scripts/healthcheck.sh`, `GET /v1/models` (expect the configured aliases incl. GPT-5.x), and `GET /v1/mcp/tools` (expect the websearch tool).

## Hard Constraints

1. **CloudFront works WITHOUT a domain.** If no hosted zone, set `useCustomDomain=false`: omit `domainNames`/`certificate`/Route53/rewrite-Function and serve on default `*.cloudfront.net`. (See `constraints.md`.)
2. **Bootstrap collisions** → bootstrap with a custom `--qualifier` + `@aws-cdk/core:bootstrapQualifier` in `cdk.json`; delete any empty `REVIEW_IN_PROGRESS` `CDKToolkit` stack. Bootstrap **both** us-east-1 and the gateway region. Never delete other apps' bootstrap resources.
3. **Bedrock Guardrails are bedrock-runtime only** — never attach them to `bedrock_mantle/` (GPT) models; cover Mantle with LiteLLM `hide-secrets` and document the gap.
4. **Never hard-code secrets.** Master key, DB creds, Langfuse admin password + project keys all go through Secrets Manager. Shared LiteLLM↔Langfuse trace keys must live in a stack created *before* LiteLLM.
5. **Internal ALBs only** (`internetFacing: false`); CloudFront VPC Origin is the sole public surface.
6. **Tokenless model auth** — Claude **and** Mantle authenticate via the ECS Task Role (SigV4); there are **no bearer tokens/API keys and no token-refresh scheduler** to maintain.
7. **Production posture** — `removalPolicy: RETAIN` + backups, per-AZ NAT, scoped IAM (no `*`/`bedrock-mantle:*`), access/flow logs. Dev sample uses the opposite; tag each with `PROD TODO`.
8. **UIs must not redirect to a dead host** (esp. domain-less), for **both LiteLLM and Langfuse**: (a) a viewer-response CloudFront Function that rewrites the `Location` header to `https://<viewer Host>`; (b) the SPA public-base env must be the real public URL (`PROXY_BASE_URL` / `NEXTAUTH_URL`). Domain-less = two-phase deploy. See `constraints.md`.
9. **SSO (IdC) is a prerequisite for the SSO path.** The Token Service accepts only `AWSReservedSSO_` principals. Provision a permission set whose **name has no underscore** with an `execute-api:Invoke`-only inline policy on the Token Service API, an account assignment (prefer **group**), and console-only password activation. The skill writes `config.sso` and AuthStack emits SSO onboarding outputs. See `shared/reference/sso-setup.md`.
10. **Region is config-driven** — `config.awsRegion` is authoritative (`bin/app.ts`: `config.awsRegion ?? CDK_DEFAULT_REGION ?? AWS_REGION`); never require editing `constants.ts`. AgentCoreGateway, CdnStack, MantleNetwork are pinned to us-east-1. CloudFront LiteLLM origin `readTimeout`/`keepaliveTimeout` = 60s (Mantle cold-start subscribe).
11. **Web search = AgentCore Web Search Tool** — provision `AWS::BedrockAgentCore::Gateway` (MCP, `AWS_IAM` inbound) + `GatewayTarget` (`connectorId: web-search`, `GATEWAY_IAM_ROLE`) in us-east-1; LiteLLM calls it with SigV4 (`bedrock-agentcore:InvokeGateway`). No Tavily, no 3rd-party API key. See `shared/patterns/agentcore-websearch.md`.
12. **Mantle (GPT-5.x) = us-east-1 via cross-region VPC peering** — `MantleNetworkStack` (peer VPC + `bedrock-mantle` endpoint + peering + acceptance custom resource + cross-region PHZ) + `MantlePeeringRoutesStack` (primary-side routes); `MANTLE_REGION=us-east-1`; Task Role needs `aws-marketplace:Subscribe` (first-call auto-subscribe). `mantle.peerVpcCidr` must not overlap `network.vpcCidr`. See `shared/patterns/mantle-peering.md`.

## Generation rules

- Single source of truth in `lib/config/constants.ts`; runtime-validate `config/dev.json` (`lib/config/schema.ts`, incl. `awsRegion`/`sso`/`agentcore`/`mantle`) so deploy fails fast.
- Append-only cross-stack `*Exports` interfaces; runtime-only wiring via SSM by name; cross-region wiring via `crossRegionReferences: true`.
- ARM64/Graviton for all Fargate/Lambda.
- Every cdk-nag suppression carries a written reason.

## When to call MCP

| Trigger | MCP call |
|---|---|
| Choosing/confirming model IDs (Claude + GPT-5.x) | `aws___search_documentation`, `aws___get_regional_availability` |
| Confirming AgentCore Web Search is in us-east-1 / CFN resources exist | `aws___get_regional_availability` (`AWS::BedrockAgentCore::Gateway`, `...::GatewayTarget`) |
| Confirming `bedrock-mantle` PrivateLink + GPT-5.x in us-east-1 | `aws___get_regional_availability`; CLI `ec2 describe-vpc-endpoint-services` |
| Confirming a service/feature is in the target region | `aws___get_regional_availability` |
| Aurora engine version validity | (CLI) `rds describe-db-engine-versions` in the target region |
