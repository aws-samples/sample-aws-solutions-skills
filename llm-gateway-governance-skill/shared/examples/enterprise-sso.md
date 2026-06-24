# Example — Enterprise SSO (custom domain + Langfuse + multi-tier)

Hypothetical customer **"Helios Bank"** — a full enterprise rollout that provides Claude Code/Codex to ~300 internal
developers on a corporate-standard domain, with both prompt/trace auditing (Langfuse) and cost tiering (standard vs economy) enabled.

> This example is the "everything on" instantiation of the branches in `shared/reference/decision-tree.md`. For the
> domain-less minimal deploy, see `domainless-poc.md`; for a deep dive on the tiering mechanism, see `economy-tiering.md`.

---

## 1. Requirements (Discovery answers)

| Question | Helios Bank answer |
|---|---|
| Domain? | Yes. Owns the Route53 hosted zone `helios.example.com` → wants the brand URL `llmlite.helios.example.com` |
| Models? | Claude Sonnet/Haiku/Opus + GPT-5.5/GPT-5.4 (Codex, Mantle). Standard teams get all; the economy team (interns/experimental org) gets low-cost models + a budget cap |
| Observability? | Regulated industry — prompt/trace auditing is mandatory → **Langfuse ON** |
| Region/account? | Gateway `config.awsRegion = us-east-2`. AgentCore Web Search, CDN, and Mantle pinned to `us-east-1`. Bedrock access confirmed (Claude us-east-2 / GPT-5.x us-east-1) |
| MCP tools? | Web search — **AgentCore Web Search Tool** (built-in connector, us-east-1 gateway). No separate deployment/API key needed |
| SSO? | IAM Identity Center permission sets: standard `ClaudeCodeUser`, economy `ClaudeCodeEconomy` (reflected in `config.sso`) |

---

## 2. `config/dev.json` values

```json
{
  "awsRegion": "us-east-2",
  "enableLangfuse": true,
  "network": { "vpcCidr": "10.0.0.0/16", "maxAzs": 2, "natGateways": 2 },
  "data": { "minCapacityAcu": 1, "maxCapacityAcu": 8, "engineVersion": "15.15" },
  "litellm": {
    "certMode": "acm-dns",
    "domainName": "llmlite.helios.example.com",
    "hostedZoneId": "Z0123456789ABCDEFGHIJ",
    "hostedZoneName": "helios.example.com",
    "certificateArn": "",
    "masterKey": "<strong-random-secret-from-secrets-manager-or-CI>",
    "desiredCount": 2,
    "cpu": 2048,
    "memoryLimitMiB": 4096
  },
  "auth": { "keyCacheTtlSeconds": 2592000 },
  "sso": {
    "startUrl": "https://d-9067890abc.awsapps.com/start",
    "region": "us-east-1",
    "accountId": "111122223333",
    "roleName": "ClaudeCodeUser"
  },
  "agentcore": {
    "webSearchRegion": "us-east-1",
    "gatewayName": "helios-prod-websearch",
    "domainDenyList": []
  },
  "mantle": {
    "region": "us-east-1",
    "peerVpcCidr": "10.1.0.0/16",
    "enablePrivateEndpoint": true
  },
  "langfuse": { "desiredCount": 1, "cpu": 1024, "memoryLimitMiB": 2048 },
  "guardrail": { "enabled": true },
  "observability": { "dashboardEnabled": true }
}
```

> **WHY these values**:
> - `certMode: "acm-dns"` → `bin/app.ts` derives **true** from `useCustomDomain = (certMode === 'acm-dns')`.
>   CdnStack issues a DNS-validated ACM certificate + attaches `domainNames` + Route53 alias + the 3xx Location rewrite Function.
> - `natGateways: 2` → enterprise uses NAT per AZ for HA (in contrast to the PoC's 1).
> - `data.minCapacityAcu: 1` → with steady traffic, raise the floor instead of near-zero.
> - `desiredCount: 2` → 2 LiteLLM Fargate tasks for availability.
> - `enableLangfuse: true` → deploy LangfuseStack (Fargate service + db-init custom resource).
> - `guardrail.enabled: true` → GuardrailStack creates the content/PII/denied-topics Guardrail, and LiteLLM references its ID/version.

> **Pitfall**: never commit `masterKey` as plaintext. `config/dev.json` is gitignored and injected from CI/Secrets Manager.
> When `certMode='acm-dns'`, `certificateArn` may be an empty string and still pass schema validation (the 3 domain fields are required).

---

## 3. Org/team governance — SSO group/permission set → LiteLLM team

Governance is implemented **not in infrastructure but in Token Lambda constants + LiteLLM teams** (`lambda/token-service/handler.py`). The general form is **SSO group (typically the org name) → team → per-team budget cap + model allowlist**, and you create as many teams as there are orgs. The below is just an **example** showing that pattern with 2 teams (standard/economy); "economy" is not a fixed classification.

```python
STANDARD_TEAM_ALIAS = "sso-users"
ECONOMY_TEAM_ALIAS = "sso-economy"
ECONOMY_PERMISSION_SETS = {"ClaudeCodeEconomy"}                       # ← Helios's economy permission set name
ECONOMY_MODELS = ["gpt-5.4", "claude-sonnet-4-6", "claude-haiku-4-5"] # excludes Opus/5.5/Fable
ECONOMY_MAX_BUDGET_USD = 50.0
```

Behavior:
- A developer logging in with the `ClaudeCodeUser` permission set → Token Lambda, in `_resolve_team_id`, attributes the
  virtual key to the **STANDARD** team (no model restriction, no budget cap).
- The `ClaudeCodeEconomy` permission set → attributed to the **ECONOMY** team (model allowlist + $50 cap).
- Both teams inherit `MCP_ACCESS_GROUPS = ["default_tools"]` → permission to use the AgentCore Web Search MCP.

> **WHY permission-set-based?** IAM Identity Center is the single source of truth for identity. To move a developer to the
> economy tier, an admin just changes the permission set in IdC — no need to manually edit keys in the LiteLLM UI.

---

## 4. Resulting stack combination (all 11)

Because `enableLangfuse: true` + `certMode: acm-dns`, **all stacks** are deployed:

```
Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM → Langfuse(ON) → Auth → Observability → CDN(us-east-1, custom domain) → MantleNetwork(us-east-1) → MantlePeeringRoutes
```

| Stack | Concrete output in this scenario |
|---|---|
| NetworkStack | VPC `10.0.0.0/16`, 2 AZ, **2 NAT**, full set of VPC endpoints |
| DataStack | Aurora Serverless v2 (1–8 ACU), LiteLLM & Langfuse DB secrets + db-init |
| GuardrailStack | Bedrock Guardrail (HATE/INSULTS/SEXUAL/VIOLENCE/MISCONDUCT + denied topics + PII BLOCK) |
| **AgentCoreGatewayStack** | us-east-1 AgentCore Gateway (MCP, AWS_IAM) + built-in Web Search Tool connector + service role |
| LiteLLMStack | Fargate **2 tasks**, internal ALB(4000), Task Role SigV4 (+InvokeGateway+Marketplace), master-key secret, publishes the internal URL to SSM |
| **LangfuseStack** | Fargate Langfuse, internal ALB(3000) — exists because `enableLangfuse: true` |
| AuthStack | API GW(IAM) + Token Lambda (STANDARD/ECONOMY mapping) + DynamoDB key cache + `config.sso` outputs |
| ObservabilityStack | CloudWatch dashboard (ALB requests/5xx, Token Service, Langfuse link) |
| **CdnStack** | 2 CloudFront distributions (LiteLLM & Langfuse) **+ ACM(us-east-1) + Route53 alias + Location rewrite Function**, LiteLLM origin 60s timeout |
| **MantleNetworkStack** | us-east-1 peer VPC + bedrock-mantle endpoint + cross-region peering + acceptance + PHZ |
| **MantlePeeringRoutesStack** | primary-region (us-east-2) routes to the peer CIDR |

> Because CdnStack has `useCustomDomain=true`, the cdk-nag **CFR4 suppression in `cdk.json` need not apply**
> (the custom domain + ACM enforce TLSv1.2_2021, so CFR4 does not fire). A decisive difference from the domain-less PoC.

---

## 5. Onboarding output values

```bash
ALB_DNS=llmlite.helios.example.com \
TOKEN_SERVICE_URL=https://abc123.execute-api.us-east-2.amazonaws.com/v1/auth/token \
./scripts/setup-developer.sh
```

- Standard developer: `aws sso login --profile llm-gateway` (permission set `ClaudeCodeUser`) → all models.
- Economy org: same flow but permission set `ClaudeCodeEconomy` → $50 cap + low-cost model allowlist.
- Auditing: per-prompt/trace tracking in the Langfuse UI (`https://langfuse.helios.example.com`).
