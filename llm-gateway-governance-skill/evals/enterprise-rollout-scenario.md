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
Full-time engineers (permission set ClaudeCodeUser) get all models; the intern org (ClaudeCodeEconomy) is limited to low-cost models + a $50 per-person cap.
Route web search through the gateway's AgentCore Web Search (managed). Gateway region us-east-2 (GPT-5.x/web search in us-east-1).
```

Expected: in Discovery, derive "custom domain → acm-dns", "Langfuse on", "2 tiers (permission set → team)",
present `config/dev.json` + architecture/cost summary at the GATE, then proceed.

---

## Expected output checklist

### A. config/dev.json derivation
- [ ] `enableLangfuse: true`
- [ ] `litellm.certMode: "acm-dns"`
- [ ] `litellm.domainName: "llmlite.helios.example.com"`, `hostedZoneId`/`hostedZoneName` populated (not empty strings)
- [ ] `guardrail.enabled: true`
- [ ] HA knobs: `network.natGateways: 2` (NAT per AZ), `litellm.desiredCount >= 2`, `data.minCapacityAcu >= 1`
- [ ] `awsRegion: "us-east-2"` (gateway platform region, authoritative)
- [ ] `agentcore.webSearchRegion: "us-east-1"` + `agentcore.gatewayName` (no underscore) populated
- [ ] `mantle.region: "us-east-1"` + `mantle.peerVpcCidr` (non-overlapping with network.vpcCidr) + `enablePrivateEndpoint`
- [ ] `sso.startUrl`/`region`/`accountId`/`roleName` populated
- [ ] `masterKey` not hard-coded as plaintext

### B. useCustomDomain derivation
- [ ] `bin/app.ts` evaluates `useCustomDomain = (certMode === 'acm-dns')` → **true**
- [ ] CdnStack issues a DNS-validated ACM certificate (`CertificateValidation.fromDns(zone)`), attaches `domainNames`
- [ ] Creates a Route53 `ARecord` alias (CloudFront target)
- [ ] Creates/attaches the 3xx `Location` header rewrite CloudFront Function (`VIEWER_RESPONSE`)
- [ ] CdnStack output `LiteLlmCfDomain` is `https://llmlite.helios.example.com`

### C. Stack combination (all 11)
- [ ] `Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM → Langfuse(ON) → Auth → Observability → CDN(us-east-1) → MantleNetwork(us-east-1) → MantlePeeringRoutes`
- [ ] **AgentCoreGatewayStack created** (us-east-1): `AWS::BedrockAgentCore::Gateway` (MCP, AWS_IAM) + GatewayTarget (`connectorId=web-search`, GATEWAY_IAM_ROLE) + service role (InvokeGateway+InvokeWebSearch)
- [ ] **MantleNetworkStack + MantlePeeringRoutesStack created**: cross-region VPC peering + bedrock-mantle endpoint + acceptance custom resource + PHZ + routes on both sides
- [ ] **LangfuseStack created** (`enableLangfuse: true`) + Langfuse DB secret in DataStack + db-init custom resource
- [ ] CdnStack creates **2 CloudFront distributions** for LiteLLM and Langfuse (each VPC Origin → internal ALB), LiteLLM origin 60s timeout
- [ ] All ALBs internal (`internetFacing: false`)
- [ ] GuardrailStack's Guardrail ID/version passed to LiteLLMStack as props

### D. Multi-tier (permission set → LiteLLM team)
- [ ] Token Lambda `ECONOMY_PERMISSION_SETS` includes `"ClaudeCodeEconomy"`
- [ ] `ECONOMY_MODELS` only low-cost models (excludes Opus/GPT-5.5/Fable), `ECONOMY_MAX_BUDGET_USD = 50.0`
- [ ] `_resolve_team_id` maps the economy permission set → `sso-economy` team (allowlist+cap), others → `sso-users` (unlimited)
- [ ] Both teams inherit `MCP_ACCESS_GROUPS = ["default_tools"]` (AgentCore Web Search access)
- [ ] `ECONOMY_MODELS` model names exactly match the LiteLLM `model_list` names
- [ ] Key issuance proceeds even when team resolution fails (graceful degradation)

### E. Auth/identity invariants
- [ ] Token Service API Gateway uses `AWS_IAM` (SigV4) auth — not Cognito (`COG4` intentionally suppressed + justification)
- [ ] Token Lambda rejects ARNs without the `AWSReservedSSO_` prefix with 403
- [ ] LiteLLM↔Bedrock/Mantle/AgentCore Gateway use Task Role SigV4 (no bearer tokens/keys to rotate, no scheduler); web search uses `bedrock-agentcore:InvokeGateway`, Mantle uses `aws-marketplace:Subscribe` auto-subscribe

### F. Guardrail / Mantle limitations
- [ ] Bedrock Guardrail applies only to Claude (`bedrock/`), **not** to GPT (`bedrock_mantle/`)
- [ ] Content/PII protection on the GPT path is supplemented by LiteLLM `hide-secrets`, and the limitation is documented

### G. cdk-nag / build gate
- [ ] Custom domain + ACM enforce TLSv1.2_2021 → **CFR4 does not occur** (decisive difference from domain-less)
- [ ] All intended dev suppressions (CFR2/CFR3/CFR5/IAM5/IAM4, etc.) have written justifications
- [ ] `npm install && npm run typecheck && npx cdk synth --all` passes (including LangfuseStack)
- [ ] Model ID/regional availability verified via AWS Knowledge MCP, `data.engineVersion` verified to exist in region

### H. Onboarding
- [ ] `setup-developer.sh` `ALB_DNS = llmlite.helios.example.com` (CloudFront public domain), not the internal ALB DNS
- [ ] Standard/economy developers log in with their respective SSO permission sets → obtain virtual keys via the same helper
- [ ] Prompt/trace auditing available via the Langfuse UI (`https://langfuse.helios.example.com`)

---

## Pass criteria (PASS conditions)

All of A–H satisfied + decisive items:

1. **useCustomDomain=true** path creates ACM (DNS validation) + Route53 alias + Location-rewrite Function (B).
2. **All 11 stacks** synthesized, including LangfuseStack + AgentCoreGatewayStack + MantleNetworkStack + MantlePeeringRoutesStack (C).
3. **Permission set → team tiering** maps exactly to allowlist+$50 cap (D).
4. **IAM/SSO identity invariants** maintained (not Cognito, non-SSO 403, Task Role SigV4) (E).
5. **`cdk synth --all` passes**, CFR4 does not occur (G).

## Failure signals (FAIL)

- `certMode='acm-dns'` but deriving `useCustomDomain=false`, omitting the domain/Route53.
- Dropping LangfuseStack, or failing synth/deploy due to a missing db-init/Langfuse DB secret.
- High-cost models leaking into the economy tier (missing allowlist), or the $50 cap not applied.
- Incorrectly attaching the Guardrail to the GPT (Mantle) path.
- Switching the Token Service to Cognito or letting a non-SSO ARN pass.
- Hard-coding secrets as plaintext, or exposing an ALB that should be internal as internet-facing.
