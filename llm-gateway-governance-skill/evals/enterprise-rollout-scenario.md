# Eval — Enterprise Rollout Scenario (Custom Domain + Multi-tier)

A black-box checklist that verifies the `llm-gateway-governance` skill correctly generates/deploys a full
enterprise rollout with **custom domain + Langfuse + cost tiering** all enabled.

Related examples: `shared/examples/enterprise-sso.md`, `shared/examples/economy-tiering.md`.
Decision rationale: `shared/reference/decision-tree.md` §1–§3.

---

## User input (simulated prompt)

```
I'll provide Claude Code/Codex to 300 internal developers on the standard domain llmlite.helios.example.com.
I have a Route53 hosted zone (helios.example.com). We're a regulated industry, so we need prompt/trace auditing — turn Langfuse on.
Full-time engineers (permission set/team alias `LlmGatewayUser`) get all models; the intern org (permission set/team alias `LlmGatewayEconomy`) is limited to low-cost models + a $50 team cap seed.
Route web search through the gateway's AgentCore Web Search (managed). Gateway region us-east-2 (GPT-5.x/web search in us-east-1).
```

Expected: in Discovery, derive "has a domain → certMode acm", "Langfuse on (requires certMode=acm)", "2 tiers (permission set → team)",
present `config/dev.json` + architecture/cost summary at the GATE, then proceed.

---

## Expected output checklist

### A. config/dev.json derivation
- [ ] `enableLangfuse: true`
- [ ] `litellm.certMode: "acm"` (domain present → internet-facing ALB + regional ACM)
- [ ] `litellm.domainName: "llmlite.helios.example.com"`, `hostedZoneId`/`hostedZoneName` populated (not empty strings)
- [ ] `certMode='acm'` with the 3 zone fields populated passes schema; **acm with neither `certificateArn` nor `domainName`+`hostedZoneId`+`hostedZoneName` is a synth fail-fast**
- [ ] `litellm.albIngressCidrs` populated with the corporate egress CIDRs (a required Discovery answer — the public ALB SG allowlist is the access control; no AWS WAF)
- [ ] `litellm.albIdleTimeoutSeconds` present or defaulted (default 900s, max 4000s) — governs long completions (no CloudFront 120s ceiling)
- [ ] `guardrail.enabled: true`
- [ ] HA knobs: `network.natGateways: 2` (NAT per AZ), `litellm.desiredCount >= 2`, `data.minCapacityAcu >= 1`
- [ ] `awsRegion: "us-east-2"` (gateway platform region, authoritative)
- [ ] `authMode: "org-sso"` (Helios Bank has an IdC organization instance) with a populated `sso` block; **no** `cognitoNative` block
- [ ] `agentcore.webSearchRegion: "us-east-1"` + `agentcore.gatewayName` (no underscore) populated
- [ ] `mantle.region: "us-east-1"` + `mantle.peerVpcCidr` (non-overlapping with network.vpcCidr) + `enablePrivateEndpoint`
- [ ] `sso.startUrl`/`region`/`accountId`/`roleName` populated
- [ ] `masterKey` not hard-coded as plaintext
- [ ] Claude model backends use verified `global.` inference-profile IDs (`bedrock/global.anthropic.<id>`), not `us.` — confirmed via `aws bedrock list-inference-profiles`
- [ ] If Fable/Mythos-class models are offered, the `provider_data_share` account data-retention opt-in (per region) is surfaced and approved at GATE 1

### B. certMode = acm → ALB edge (CloudFront removed)
- [ ] No `useCustomDomain`/`acm-dns` derivation and **no CdnStack** — `LiteLLMStack` reads `certMode` directly
- [ ] `certMode='acm'` → an **internet-facing ALB** on HTTPS:443 with a **regional** ACM cert in `config.awsRegion` (DNS-validated via `CertificateValidation.fromDns(zone)`) — **not** a us-east-1 CloudFront cert
- [ ] Creates a Route53 `ARecord` alias to the **ALB** + an HTTP:80→443 redirect listener
- [ ] A separate **internal ALB (HTTP:4000)** exists for the Token Service (SSM URL `LITELLM_INTERNAL_URL` unchanged)
- [ ] `GatewayUrl` output is `https://llmlite.helios.example.com` (the ALB alias domain)
- [ ] UI redirects rely on `PROXY_BASE_URL` only; the entrypoint does **NOT** pass `--forwarded-allow-ips` (nonexistent in the pinned image's CLI — container dies at boot with exitCode 2) and there is no CloudFront Location-rewrite Function

### C. Stack combination (all 10 — CloudFront removed)
- [ ] `Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM(acm public ALB) → Langfuse(ON, acm public ALB) → Auth → Observability → MantleNetwork(us-east-1) → MantlePeeringRoutes`
- [ ] **No CdnStack** — CloudFront is removed; the ALB is the edge
- [ ] **AgentCoreGatewayStack created** (us-east-1): `AWS::BedrockAgentCore::Gateway` (MCP, AWS_IAM) + GatewayTarget (`connectorId=web-search`, GATEWAY_IAM_ROLE) + service role (InvokeGateway+InvokeWebSearch)
- [ ] **MantleNetworkStack + MantlePeeringRoutesStack created**: cross-region VPC peering + bedrock-mantle endpoint + acceptance custom resource + PHZ + routes on both sides
- [ ] **LangfuseStack created** (`enableLangfuse: true` **and** `certMode='acm'`) behind its **own internet-facing ALB + ACM cert** + Langfuse DB secret in DataStack + db-init custom resource
- [ ] LiteLLM has an **internet-facing public ALB (HTTPS:443, acm)** for developer traffic + a **separate internal ALB (HTTP:4000)** for the Token Service; ALB `idleTimeout=900s` (max 4000s) — a 120s+ completion does not 504 (no CloudFront ceiling)
- [ ] GuardrailStack's Guardrail ID/version passed to LiteLLMStack as props

### D. Multi-tier (permission set → LiteLLM team)
- [ ] Token Lambda uses unbranched mapping: `permission_set` name is passed directly as `team_alias`.
- [ ] No `ECONOMY_PERMISSION_SETS`, `if permission_set in {...}`, `sso-economy`, or default `sso-users` branch appears in generated Lambda code.
- [ ] Optional `TIER_CONFIG["LlmGatewayEconomy"]` seeds first-time team creation with low-cost models and `max_budget = 50.0`; `LlmGatewayUser` can be pre-created or auto-created with broader access.
- [ ] Both teams inherit `MCP_ACCESS_GROUPS = ["default_tools"]` (AgentCore Web Search access).
- [ ] Seeded model names exactly match the LiteLLM `model_list` names.
- [ ] Key issuance proceeds even when team resolution fails (graceful degradation).

### E. Auth/identity invariants
- [ ] Token Service API Gateway uses `AWS_IAM` (SigV4) auth — not Cognito (`COG4` intentionally suppressed + justification)
- [ ] Token Lambda rejects ARNs without the `AWSReservedSSO_` prefix with 403
- [ ] LiteLLM↔Claude and ↔AgentCore Gateway use Task Role SigV4 (tokenless); **Mantle (GPT-5.x) uses a Bearer token** minted at runtime from the Task Role into `BEDROCK_MANTLE_API_KEY` by the `mantle_token_refresh` callback (NOT SigV4, and NOT `AWS_BEARER_TOKEN_BEDROCK` — which would break Claude); web search uses `bedrock-agentcore:InvokeGateway`; Mantle IAM is scoped to `project/*` (+`CallWithBearerToken`) and uses `aws-marketplace:Subscribe` auto-subscribe
- [ ] Mantle image installs `aws-bedrock-token-generator` via `uv` (base image has no pip); no long-term secret, no external token-refresh scheduler

### F. Guardrail / Mantle limitations
- [ ] Bedrock Guardrail applies only to Claude (`bedrock/`), **not** to GPT (`bedrock_mantle/`)
- [ ] Content/PII protection on the GPT path is supplemented by LiteLLM `hide-secrets`, and the limitation is documented

### G. cdk-nag / build gate
- [ ] No CloudFront findings (`CFR4`/`CFR2`/`CFR3`/`CFR5`) — CloudFront is removed; `certMode='acm'` uses a modern ALB TLS policy on a real regional ACM cert
- [ ] Dev suppressions (IAM5/IAM4, ALB ELB2/EC23, etc.) have written justifications; the public ALB SG ingress is restricted to `albIngressCidrs` (no AWS WAF resources are synthesized)
- [ ] `npm install && npm run typecheck && npx cdk synth --all` passes (including LangfuseStack)
- [ ] Model ID/regional availability verified via AWS Knowledge MCP, `data.engineVersion` verified to exist in region

### H. Onboarding
- [ ] The base URL is the **`GatewayUrl` output** = the ALB domain `https://llmlite.helios.example.com` (acm), not the internal ALB DNS
- [ ] Ends by generating the **two HTML onboarding docs** (`developer-setup.html` + `admin-onboarding.html`) via `scripts/gen-onboarding.py`
- [ ] Standard/economy developers log in with their respective SSO permission sets → obtain virtual keys via the same helper
- [ ] Prompt/trace auditing available via the Langfuse UI (`https://langfuse.helios.example.com`, its own acm ALB)

---

## Pass criteria (PASS conditions)

All of A–H satisfied + decisive items:

1. **certMode='acm'** path creates an internet-facing ALB + regional ACM (DNS validation) + Route53 alias + HTTP→443 redirect, plus a separate internal ALB:4000 for the Token Service (B).
2. **All 10 stacks** synthesized (no CdnStack), including LangfuseStack + AgentCoreGatewayStack + MantleNetworkStack + MantlePeeringRoutesStack (C).
3. **Permission set name → same LiteLLM team_alias** is unbranched; optional first-create seed applies allowlist+$50 cap to `LlmGatewayEconomy` (D).
4. **IAM/SSO identity invariants** maintained (not Cognito, non-SSO 403, Task Role SigV4) (E).
5. **`cdk synth --all` passes**, no CloudFront (CFR*) findings, and a 120s+ completion does not 504 (G).

## Failure signals (FAIL)

- Re-introducing CloudFront/CdnStack, `acm-dns`/`useCustomDomain`, `*.cloudfront.net`, a Location-rewrite Function, or the 120s VPC-Origin ceiling.
- `certMode='acm'` with no domain fields (nor `certificateArn`) not failing synth; or enabling Langfuse with `certMode≠acm` (must schema fail-fast).
- Dropping LangfuseStack, or failing synth/deploy due to a missing db-init/Langfuse DB secret.
- Hard-coded tier branching (`ECONOMY_PERMISSION_SETS`, `sso-economy`, default `sso-users`) reappears, or high-cost models leak into the economy team seed.
- Incorrectly attaching the Guardrail to the GPT (Mantle) path.
- Switching the Token Service to Cognito or letting a non-SSO ARN pass.
- Hard-coding secrets as plaintext, or exposing the **internal** ALB / the `:4000` listener to the internet.
