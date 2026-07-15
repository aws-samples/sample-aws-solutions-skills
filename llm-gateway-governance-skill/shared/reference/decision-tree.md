# Decision Tree — mapping requirements to configuration

Use this to turn the Discovery answers into concrete `config/dev.json` + stack choices.

## 0. Can org-sso be used? → `authMode`

Detect the IAM Identity Center instance type before asking permission-set questions:

```text
aws sso-admin list-instances --region <idc-region>   # empty ⇒ no IdC in this region
aws organizations describe-organization              # if permitted / account is in an org

- OwnerAccountId == organization management account (organization instance): authMode = "org-sso" is available
- OwnerAccountId != management account, OR a standalone account instance, OR no usable IdC at all: authMode = "cognito-native"
```

| `authMode` | Use when | Generated auth stack | Client helper |
|---|---|---|---|
| `org-sso` | IdC **organization** instance with permission sets | API Gateway `AWS_IAM`; Token Lambda parses `AWSReservedSSO_...` ARN | `aws sso login` + SigV4 `get-gateway-token.sh` |
| `cognito-native` | IdC **account** instance, or no usable IdC (partner is payer/owns the org IdC, etc.) | Cognito User Pool (sole identity source) + API Gateway `COGNITO_USER_POOLS` authorizer; Token Lambda reads the `cognito:groups` claim (no Identity Store) | `llmgw-login` + `gateway_auth.py` token helper |

`org-sso` is the default only when an **organization** instance is present. When detection shows an account instance (or no IdC), use `cognito-native`. ⚠️ Do **not** choose `account-sso`/IdC-federated: an account instance cannot host a SAML 2.0 customer-managed application (AWS-confirmed), so Cognito↔IdC SAML federation is impossible — `cognito-native` uses Cognito as the sole identity store precisely to sidestep that. (`account-sso` remains in the schema only as a deprecated no-op.) Account instances also cannot produce the permission-set AWS credentials the org-sso SigV4 helper needs.

## 1. TLS / edge exposure → `litellm.certMode` (CloudFront removed — the ALB is the edge)

The ALB is **always the edge, always internet-facing, always SG CIDR-restricted**. The user picks a TLS strategy via `litellm.certMode` — this is **orthogonal to `authMode`** (any certMode pairs with either `org-sso` or `cognito-native`):

| User answer | `litellm.certMode` | Result |
|---|---|---|
| "I have a Route53 hosted zone / want a branded URL" | `acm` | Internet-facing ALB, HTTPS:443 with a **public ACM cert issued REGIONALLY (`config.awsRegion`, not us-east-1)** — via an existing `certificateArn`, or CDK DNS-issues it from `domainName`+`hostedZoneId`+`hostedZoneName` and adds a Route53 alias + HTTP→443 redirect. ✅ recommended / PROD. |
| "No domain" | `http` | Internet-facing ALB, **HTTP:80, no cert, no domain**. ⛔ the virtual key **and prompt/response bodies** are **plaintext on the wire** → PoC-only, and the SG allowlist (`albIngressCidrs`) is the only access control (a GATE-1 acknowledgement item; `0.0.0.0/0` = plaintext open to the internet → its own explicit acknowledgement). |

**Both modes require `litellm.albIngressCidrs`** — ask in Discovery which source CIDRs (office/NAT egress IPs) may reach the ALB. The generated SG allows ingress only from those CIDRs. There is no AWS WAF, no self-signed mode, no internal/VPN exposure variant, and no SSM tunnel.

> There is no `useCustomDomain` derivation and no `acm-dns`/`acm-arn` split anymore — `bin/app.ts` reads `config.litellm.certMode` directly and `LiteLLMStack` selects the ALB listener from it. Domain-less = `http`, **never** CloudFront and never a self-signed cert.
>
> ⚠️ `acm` fail-fasts at synth if it has neither `certificateArn` nor `domainName`+`hostedZoneId`+`hostedZoneName`. `http` needs no domain fields. Long completions are governed by `litellm.albIdleTimeoutSeconds` (default 900s, max 4000s) — the old CloudFront 120s ceiling is gone.

## 2. Observability depth? → Langfuse toggle

| User answer | `enableLangfuse` | Effect |
|---|---|---|
| "I want prompt/trace-level observability" | `true` | LangfuseStack deploys (extra Fargate service + db-init). |
| "CloudWatch is enough / minimize cost & surface" | `false` (or `-c enableLangfuse=false`) | LangfuseStack skipped; LiteLLM tracing env still present but inert. |

> ⚠️ **Langfuse UI requires `certMode='acm'`** — it needs a real domain + ACM cert (its own internet-facing ALB). `enableLangfuse=true` with `certMode='http'` is a **schema fail-fast**; those deploys are CloudWatch-only. When `certMode='acm'`, whether to deploy Langfuse is the same free `enableLangfuse` choice as before.

## 3. Tiering / per-org governance → IdC authorization unit → LiteLLM team

The general mechanism: **map each team's authorization unit → its own LiteLLM team**. In `org-sso`, that unit is the permission set name. In `cognito-native`, that unit is the Cognito User Pool Group name, and give that team its **own budget cap + model allowlist + MCP access**. Teams are arbitrary and usually named by **organization/team** (e.g. `team-frontend`, `org-research`, `org-marketing`) — not a fixed "standard/economy" split.

| Need | Mechanism |
|---|---|
| Per-org budget + model control | Map the org's IdC authorization-unit name to the same LiteLLM `team_alias`; set that team's `max_budget` + model allowlist. One team per org/team as needed. |
| A team with no limits | A team (or the default) with no budget cap and all models. |
| Scoped tool (MCP) access | Team carries `MCP_ACCESS_GROUPS` (e.g., `default_tools`); LiteLLM `mcp_servers.<name>.access_groups` must match. |

> The reference Token Lambda must use **unbranched** mapping: the IdC authorization-unit name is the LiteLLM `team_alias`. Optional `TIER_CONFIG` entries are keyed by that same alias and only seed a team's first creation with starter `models`/`max_budget` (for example, `LlmGatewayEconomy` → low-cost model allowlist + $50 cap). Treat "economy/standard" as **just one instance** of the per-org pattern, not a required taxonomy. For real orgs, replicate the pattern per organization (IdC auth unit → same-named team → budget/allowlist).
>
> Prerequisite: for `org-sso`, IdC must be provisioned and permission-set names map to teams and **must contain no underscore**. For `cognito-native`, no IdC is needed at all — Cognito User Pool Group names map to teams and should match `teamGroupPrefix` (for example `llmgw-`). Full setup: `reference/sso-setup.md` and `reference/account-instance-setup.md`.

## 4. Which models? → LiteLLM model_list + constants

- Claude family via `bedrock/` (Anthropic Messages/Converse) — Guardrail-compatible, tokenless SigV4.
- GPT family via `bedrock_mantle/` (OpenAI Responses route) — **NOT** Guardrail-compatible (Bedrock Guardrails are bedrock-runtime only), and **NOT SigV4**: needs a runtime-minted Bearer token in `BEDROCK_MANTLE_API_KEY` (see `constraints.md` → "LiteLLM image + Mantle Bearer-token auth").
- **GPT tier = `gpt-5.5` / `gpt-5.4` only — ⛔ never offer `gpt-5.6-*`**, even if asked for "the newest GPT": not on OpenAI's certified Codex↔Bedrock model list, and it breaks Codex tool-use (Codex `namespace` tool type → Mantle `400 validation_error`, shown misleadingly as "high demand" reconnects). Redirect to `gpt-5.5`/`gpt-5.4`; see `constraints.md` → "GPT-5.6 is not a valid model choice".
- Always verify the exact model IDs / regional availability with **AWS Knowledge MCP** and `aws bedrock list-inference-profiles` before emitting `lib/config/constants.ts` (model IDs are volatile — never hard-code blindly). **Do not assume a `us.` prefix** — recent (2026) Claude models (Opus 4.8, Sonnet 5, Haiku 4.5, Fable 5) are `global.`-only; a `us.` id returns `The provided model identifier is invalid.`
- **Fable/Mythos-class models** (e.g. `claude-fable-5`) require the account data-retention mode `provider_data_share` set **per region** (Bedrock control-plane REST API) — a GATE-1 approval item (30-day Anthropic retention + human review). See `constraints.md`.
- **Claude Code client**: emit **all four** `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` vars — omitting the Fable var hides the Fable tier from the `/model` picker.

## 5. Region (config-driven)

- The gateway **platform region is `config.awsRegion`** (top-level config key, authoritative). `bin/app.ts` resolves `config.awsRegion ?? process.env.CDK_DEFAULT_REGION ?? AWS_REGION`. **No need to edit `constants.ts`** to change region.
- **Pinned to us-east-1** regardless of `awsRegion`: `AgentCoreGatewayStack` (Web Search GA region) and `MantleNetworkStack` (Mantle home region). There is **no CdnStack** (CloudFront removed) — the ALB is the edge, and its ACM cert (acm mode) is **regional** (`config.awsRegion`), not a us-east-1 CloudFront viewer cert. There is no AWS WAF.
- **Bootstrap both** the gateway region and us-east-1: `cdk bootstrap aws://<acct>/<awsRegion> aws://<acct>/us-east-1`.
- Confirm Claude model access in `awsRegion` and GPT-5.x (Mantle) + Web Search access in us-east-1 (verify via MCP).

## 6. Web search? → AgentCore Web Search Tool

| User answer | Choice |
|---|---|
| "Agents need web search / current info" | Deploy `AgentCoreGatewayStack` (us-east-1) with the built-in `web-search` connector; wire `mcp_servers.websearch` in LiteLLM. No API key. See `shared/patterns/agentcore-websearch.md`. |
| "No web search needed" | Skip the gateway stack and the `mcp_servers.websearch` block. |

> `config.agentcore`: `webSearchRegion` (us-east-1), `gatewayName` (no underscore), optional `domainDenyList`.

## 7. Mantle (GPT-5.x) reachability → cross-region VPC peering

If any `bedrock_mantle/` model is offered, deploy `MantleNetworkStack` (us-east-1) + `MantlePeeringRoutesStack` (primary region) so mantle is reached privately in Virginia. `config.mantle`: `region` (us-east-1), `peerVpcCidr` (non-overlapping with `network.vpcCidr`), `enablePrivateEndpoint`. Set LiteLLM GPT models' `aws_region_name=us-east-1` and inject env `BEDROCK_MANTLE_REGION=us-east-1` + `BEDROCK_MANTLE_API_BASE=https://bedrock-mantle.us-east-1.api.aws` (the vars `bedrock_mantle` actually reads; `MANTLE_REGION` is NOT consumed). See `shared/patterns/mantle-peering.md`. If no GPT models are offered, omit both stacks.

## 8. Capacity / cost knobs

| Knob | Where | Guidance |
|---|---|---|
| Aurora ACU min/max | `data.minCapacityAcu` / `maxCapacityAcu` | Dev: 0.5 / 4. Prod: raise min for steady traffic. |
| LiteLLM task size | `litellm.cpu` / `memoryLimitMiB` / `desiredCount` | Dev: 2048 / 4096 / 1. |
| NAT gateways | `network.natGateways` | Dev: 1 (cost). Prod: one per AZ (HA). |
| Aurora engine version | `data.engineVersion` | Verify it exists in the target region (`describe-db-engine-versions`). |

## 9. Bootstrap qualifier (operational)

If the target account/region has broken or conflicting CDK bootstrap leftovers, bootstrap with a **custom qualifier** and set `@aws-cdk/core:bootstrapQualifier` in `cdk.json` — this avoids importing/colliding with existing `hnb659fds` resources. See `constraints.md`.
