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
| SSO? | Helios Bank has an IdC **organization** instance → `authMode="org-sso"`. Permission sets named exactly like LiteLLM teams: `LlmGatewayUser`, `LlmGatewayEconomy` (reflected in `config.sso`). *(If an org only had an IdC **account** instance or no usable org SSO, it would instead use `authMode="cognito-native"` — Cognito User Pool Groups as teams; see `account-instance-setup.md`.)* |

---

## 2. `config/dev.json` values

```json
{
  "awsRegion": "us-east-2",
  "authMode": "org-sso",
  "enableLangfuse": true,
  "network": { "vpcCidr": "10.0.0.0/16", "maxAzs": 2, "natGateways": 2 },
  "data": { "minCapacityAcu": 1, "maxCapacityAcu": 8, "engineVersion": "15.15" },
  "litellm": {
    "certMode": "acm",
    "domainName": "llmlite.helios.example.com",
    "hostedZoneId": "Z0123456789ABCDEFGHIJ",
    "hostedZoneName": "helios.example.com",
    "certificateArn": "",
    "albIngressCidrs": ["198.51.100.0/24", "203.0.113.0/24"],
    "masterKey": "<strong-random-secret-from-secrets-manager-or-CI>",
    "desiredCount": 2,
    "cpu": 2048,
    "memoryLimitMiB": 4096
  },
  "auth": { "keyCacheTtlSeconds": 2592000, "keyDurationSeconds": 86400 },
  "sso": {
    "startUrl": "https://d-9067890abc.awsapps.com/start",
    "region": "us-east-1",
    "accountId": "111122223333",
    "roleName": "LlmGatewayUser"
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
> - `certMode: "acm"` → `LiteLLMStack` provisions an **internet-facing ALB** on HTTPS:443 with a **regional** public ACM cert issued in `config.awsRegion` from the 3 Route53 zone fields (DNS-validated) + a Route53 A-record alias + an HTTP→443 redirect. CloudFront is removed — the ALB is the edge.
> - `albIngressCidrs` → the public ALB SG allows ingress only from the corporate NAT egress ranges (a required Discovery answer; the SG allowlist is the access control — there is no AWS WAF).
> - `natGateways: 2` → enterprise uses NAT per AZ for HA (in contrast to the PoC's 1).
> - `data.minCapacityAcu: 1` → with steady traffic, raise the floor instead of near-zero.
> - `desiredCount: 2` → 2 LiteLLM Fargate tasks for availability.
> - `enableLangfuse: true` → deploy LangfuseStack (Fargate service + db-init custom resource).
> - `guardrail.enabled: true` → GuardrailStack creates the content/PII/denied-topics Guardrail, and LiteLLM references its ID/version.

> **Pitfall**: never commit `masterKey` as plaintext. `config/dev.json` is gitignored and injected from CI/Secrets Manager.
> When `certMode='acm'`, `certificateArn` may be an empty string and still pass schema validation as long as the 3 domain fields (`domainName`+`hostedZoneId`+`hostedZoneName`) are set (else synth fail-fast). Optionally set `litellm.albIdleTimeoutSeconds` (default 900s, max 4000s) to raise the long-completion ceiling.

---

## 3. Org/team governance — permission set name → same LiteLLM team

Governance is implemented by the Token Lambda's unbranched IdC mapping plus LiteLLM teams. In this `org-sso` example, the **permission-set name is the LiteLLM `team_alias`**; per-team budget caps and model allowlists are then managed in LiteLLM. The below is just an example with 2 teams (standard/economy); "economy" is not a fixed classification.

```python
TIER_CONFIG = {
    "LlmGatewayEconomy": {
        "models": ["gpt-5.4", "claude-sonnet-5", "claude-haiku-4-5"],
        "max_budget": 50.0,
    }
}  # Optional first-create seed. Permission set name == team_alias; no branching.
```

Behavior:
- A developer logging in with the `LlmGatewayUser` permission set → Token Lambda resolves/creates the same-named `LlmGatewayUser` LiteLLM team.
- A developer logging in with the `LlmGatewayEconomy` permission set → Token Lambda resolves/creates the same-named `LlmGatewayEconomy` LiteLLM team. The optional `TIER_CONFIG` seed applies only if that team does not already exist.
- Both teams inherit `MCP_ACCESS_GROUPS = ["default_tools"]` → permission to use the AgentCore Web Search MCP.

> **WHY permission-set-name mapping?** IAM Identity Center is the single source of truth for identity. To move a developer to a different tier, an admin changes the user's group/permission-set assignment in IdC; budgets/model allowlists stay in the same-named LiteLLM team.

---

## 4. Resulting stack combination (all 10 — CloudFront removed)

Because `enableLangfuse: true` + `certMode: acm`, **all stacks** are deployed:

```
Network → Data → Guardrail → AgentCoreGateway(us-east-1) → LiteLLM(acm public ALB) → Langfuse(ON, acm public ALB) → Auth → Observability → MantleNetwork(us-east-1) → MantlePeeringRoutes
```

| Stack | Concrete output in this scenario |
|---|---|
| NetworkStack | VPC `10.0.0.0/16`, 2 AZ, **2 NAT**, full set of VPC endpoints |
| DataStack | Aurora Serverless v2 (1–8 ACU), LiteLLM & Langfuse DB secrets + db-init |
| GuardrailStack | Bedrock Guardrail (HATE/INSULTS/SEXUAL/VIOLENCE/MISCONDUCT + denied topics + PII BLOCK) |
| **AgentCoreGatewayStack** | us-east-1 AgentCore Gateway (MCP, AWS_IAM) + built-in Web Search Tool connector + service role |
| LiteLLMStack | Fargate **2 tasks**, **internet-facing ALB (HTTPS:443, regional ACM + Route53 alias, `idleTimeout=900s`)** for developer traffic + a separate **internal ALB (HTTP:4000)** for the Token Service, Task Role: Claude SigV4 + Mantle Bearer (runtime-minted `BEDROCK_MANTLE_API_KEY`) + InvokeGateway + Marketplace, master-key secret, publishes the internal URL to SSM |
| **LangfuseStack** | Fargate Langfuse behind its **own internet-facing ALB + ACM cert** (Route53 alias) — exists because `enableLangfuse: true` **and** `certMode='acm'` |
| AuthStack | API GW(IAM) + Token Lambda (permission set name → same `team_alias`) + DynamoDB key cache + `config.sso` outputs |
| ObservabilityStack | CloudWatch usage dashboard (tokens by model/team, spend, latency, per-user + hourly Logs Insights, ALB requests/5xx, Langfuse link) |
| **MantleNetworkStack** | us-east-1 peer VPC + bedrock-mantle endpoint + cross-region peering + acceptance + PHZ |
| **MantlePeeringRoutesStack** | primary-region (us-east-2) routes to the peer CIDR |

> With `certMode='acm'` the public ALB uses a modern TLS policy (e.g. `TLS13_RES`) on a real regional ACM cert, so there is no default-cert TLS downgrade to justify — the old CloudFront `AwsSolutions-CFR4` finding does not exist (CloudFront is removed). A decisive difference from the plaintext `http` PoC path.

---

## 5. Onboarding output values

```bash
# Zero-touch: setup-developer.sh reads outputs.json (GatewayUrl → https://llmlite.helios.example.com,
# TokenServiceUrl, SSO outputs) — nothing to pass. Env vars remain as overrides only.
cdk deploy --all --outputs-file outputs.json
./scripts/setup-developer.sh
```

- Standard developer: `aws sso login --profile llm-gateway` (permission set `LlmGatewayUser`) → all models.
- Economy org: same flow but permission set/team alias `LlmGatewayEconomy` → optional first-create $50 cap + low-cost model allowlist seed.
- Auditing: per-prompt/trace tracking in the Langfuse UI (`https://langfuse.helios.example.com`).
