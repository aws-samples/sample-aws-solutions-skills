# Decision Tree — mapping requirements to configuration

Use this to turn the Discovery answers into concrete `config/dev.json` + stack choices.

## 1. Custom domain or not? → drives CdnStack mode

| User answer | `litellm.certMode` | `useCustomDomain` | Result |
|---|---|---|---|
| "I have a Route53 hosted zone / want a branded URL" | `acm-dns` | `true` | CdnStack issues a DNS-validated ACM cert, attaches `domainNames`, creates Route53 alias + the 3xx Location rewrite Function. |
| "No domain, just deploy it / PoC" | `acm-arn` (with a placeholder ARN) **or** any non-`acm-dns` | `false` | CloudFront serves on its default `*.cloudfront.net` domain with the default CloudFront certificate. **No ACM, no Route53, no hosted zone.** |

> `bin/app.ts` derives `useCustomDomain = config.litellm.certMode === 'acm-dns'`. The domain-less path is what unblocks a deploy when the user has no hosted zone — do **not** require a domain.

⚠️ Domain-less mode uses the default CloudFront cert → minimum TLS is `TLSv1` → cdk-nag `AwsSolutions-CFR4` fires. Suppress it with a justification (see `constraints.md`). For production, prefer a custom domain to enforce TLSv1.2.

## 2. Observability depth? → Langfuse toggle

| User answer | `enableLangfuse` | Effect |
|---|---|---|
| "I want prompt/trace-level observability" | `true` | LangfuseStack deploys (extra Fargate service + db-init). |
| "CloudWatch is enough / minimize cost & surface" | `false` (or `-c enableLangfuse=false`) | LangfuseStack skipped; LiteLLM tracing env still present but inert. |

## 3. Tiering / per-org governance → SSO group (or permission set) → LiteLLM team

The general mechanism: **map each org/team's SSO group (or permission set) → its own LiteLLM team**, and give that team its **own budget cap + model allowlist + MCP access**. Teams are arbitrary and usually named by **organization/team** (e.g. `team-frontend`, `org-research`, `org-marketing`) — not a fixed "standard/economy" split.

| Need | Mechanism |
|---|---|
| Per-org budget + model control | Map the org's SSO group/permission-set name → a LiteLLM team alias; set that team's `max_budget` + model allowlist. One team per org/team as needed. |
| A team with no limits | A team (or the default) with no budget cap and all models. |
| Scoped tool (MCP) access | Team carries `MCP_ACCESS_GROUPS` (e.g., `default_tools`); LiteLLM `mcp_servers.<name>.access_groups` must match. |

> The reference Token Lambda ships **one worked example** of this — a `STANDARD_TEAM` + an `ECONOMY_TEAM` (`ECONOMY_PERMISSION_SETS` → `ECONOMY_TEAM_ALIAS` with `ECONOMY_MODELS` allowlist + `ECONOMY_MAX_BUDGET_USD`, e.g. capping to `gpt-5.4`). Treat "economy/standard" as **just one instance** of the per-org pattern, not a required taxonomy. For real orgs, replicate the pattern per organization (group → team → budget/allowlist).
>
> Prerequisite: the SSO path needs IdC provisioned. Group/permission-set names map to teams via the Token Lambda and **must contain no underscore**. Full setup: `reference/sso-setup.md`. (Prefer **groups** for orgs — easier to assign/revoke than per-user.)

## 4. Which models? → LiteLLM model_list + constants

- Claude family via `bedrock/` (Anthropic Messages/Converse) — Guardrail-compatible.
- GPT family via `bedrock_mantle/` (OpenAI Responses route) — **NOT** Guardrail-compatible (Bedrock Guardrails are bedrock-runtime only).
- Always verify the exact model IDs / regional availability with **AWS Knowledge MCP** before emitting `lib/config/constants.ts` (model IDs are volatile — never hard-code blindly).

## 5. Region (config-driven)

- The gateway **platform region is `config.awsRegion`** (top-level config key, authoritative). `bin/app.ts` resolves `config.awsRegion ?? process.env.CDK_DEFAULT_REGION ?? AWS_REGION`. **No need to edit `constants.ts`** to change region.
- **Pinned to us-east-1** regardless of `awsRegion`: `AgentCoreGatewayStack` (Web Search GA region), `MantleNetworkStack` (Mantle home region), and `CdnStack`. The `CdnStack` pin is a **hard requirement only with a custom domain** — CloudFront accepts its viewer ACM certificate **only from us-east-1** (and a CloudFront-scoped WAF WebACL is also us-east-1-only). In **domain-less mode** (no ACM cert) the CloudFront distribution could be defined from any region, so the pin is for consistency, not a hard requirement. CloudFront is global and VPC Origin does **not** force same-region with the ALB (cross-region via `crossRegionReferences`).
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
