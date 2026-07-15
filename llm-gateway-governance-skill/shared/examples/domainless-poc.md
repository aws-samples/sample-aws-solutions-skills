# Example — Domainless PoC (certMode http, Langfuse OFF, minimal deploy)

Hypothetical customer **"Nimbus Labs"** — a startup with no hosted zone and no domain that just wants a
minimal PoC "stood up for evaluation." With CloudFront removed, the **ALB is the edge** — always
**internet-facing and SG CIDR-restricted**. A domain-less PoC uses **`certMode: http`**: a public ALB on
**HTTP:80, no cert**, with ingress locked to the `albIngressCidrs` allowlist. ⛔ The virtual key **and
prompt/response bodies** travel plaintext, so this is PoC-only and a GATE-1 acknowledgement item. There is
no `*.cloudfront.net` default domain, no self-signed mode, no SSM tunnel, and no AWS WAF.

> The key point of this example is that **you do not need a domain** to stand up the gateway. Do not block the
> deploy just because there is no hosted zone — use `http` with a tight `albIngressCidrs` allowlist and the
> plaintext acknowledgement. (`shared/reference/constraints.md` → "Edge TLS via `certMode`", Hard Constraints #1/#17)

---

## 1. Requirements (Discovery answers)

| Question | Nimbus Labs answer |
|---|---|
| Domain? | **None.** No Route53 hosted zone → `certMode: http` (public ALB, HTTP:80, SG-restricted) |
| Allowed source CIDRs? | The office NAT egress range `203.0.113.0/24` → `albIngressCidrs` (required — the SG allowlist is the only access control in http mode) |
| Plaintext acknowledgement? | **Yes, acknowledged at GATE 1**: the virtual key + prompts travel unencrypted; acceptable for a short-lived evaluation from the office range only |
| Models? | Claude Sonnet/Haiku only. No tiering needed (everyone identical) |
| Observability? | CloudWatch is enough → **Langfuse OFF** (it would require `certMode='acm'` anyway; minimize service surface/cost) |
| Region/account? | `us-east-2`. No us-east-1 edge stack (CloudFront removed); AgentCore/Mantle stay us-east-1 if enabled |
| MCP tools? | Optional at the evaluation stage — when on, the us-east-1 AgentCore Web Search Tool (built-in connector, no API key needed); when off, omit `mcp_servers.websearch` |
| SSO? | A single default permission set `LlmGatewayUser`. No economy tier |

---

## 2. `config/dev.json` values (http — the domain-less path)

```json
{
  "awsRegion": "us-east-2",
  "authMode": "org-sso",
  "enableLangfuse": false,
  "network": { "vpcCidr": "10.0.0.0/16", "maxAzs": 2, "natGateways": 1 },
  "data": { "minCapacityAcu": 0.5, "maxCapacityAcu": 4, "engineVersion": "15.15" },
  "litellm": {
    "certMode": "http",
    "domainName": "",
    "hostedZoneId": "",
    "hostedZoneName": "",
    "certificateArn": "",
    "albIdleTimeoutSeconds": 900,
    "albIngressCidrs": ["203.0.113.0/24"],
    "masterKey": "<strong-random-secret>",
    "desiredCount": 1,
    "cpu": 2048,
    "memoryLimitMiB": 4096
  },
  "auth": { "keyCacheTtlSeconds": 2592000 },
  "sso": {
    "startUrl": "https://d-1234567890.awsapps.com/start",
    "region": "us-east-1",
    "accountId": "444455556666",
    "roleName": "LlmGatewayUser"
  },
  "agentcore": {
    "webSearchRegion": "us-east-1",
    "gatewayName": "nimbus-poc-websearch",
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

> **WHY the domain-less path**:
> - `certMode: "http"` → `LiteLLMStack` stands up an **internet-facing ALB** with a plain **HTTP:80** listener.
>   No cert, no domain, no Route53 — `domainName`/`hostedZoneId`/`hostedZoneName`/`certificateArn` may all be
>   empty strings.
> - `albIngressCidrs: ["203.0.113.0/24"]` is the **primary (and only) access control**: the public ALB SG allows
>   ingress solely from the office range. It is a required Discovery answer — `0.0.0.0/0` would put the plaintext
>   endpoint on the whole internet and needs its own explicit GATE-1 acknowledgement.
> - ⛔ Plaintext on the wire (virtual key + prompts) is the accepted PoC tradeoff (Hard Constraint #17); moving to
>   production means `certMode='acm'` with a real domain.
> - `enableLangfuse: false` → LangfuseStack skipped. (Langfuse UI needs `certMode='acm'`, so it could not be enabled
>   here anyway — http deploys are CloudWatch-only.)
> - `natGateways: 1`, `minCapacityAcu: 0.5`, `desiredCount: 1` → lowest cost.

---

## 3. What LiteLLMStack creates for a domain-less deploy

From `LiteLLMStack` (§4 of `cdk-stacks.md`):
- A **public, internet-facing ALB** with an **HTTP:80** listener (no cert), whose SG allows ingress **only from
  `albIngressCidrs`** (`203.0.113.0/24`).
- The ECS tasks stay in `PRIVATE_WITH_EGRESS`; a **separate internal ALB (HTTP:4000)** carries the Token Service
  path (its SSM URL is unchanged, never internet-exposed).
- `GatewayUrl` output = `http://<alb-dns>` — reachable only from the allowlisted range.

> **WHY no domain is fine?** The ALB is the edge and serves HTTP directly — no public DNS name or purchased domain
> is needed. The ECS tasks are never directly internet-exposed; the SG allowlist bounds who can reach the ALB.

---

## 4. Resulting stack combination (excluding Langfuse — CloudFront removed)

Because `enableLangfuse: false` (and no CDN stack exists), the deploy is:

```
Network → Data → Guardrail → LiteLLM(public HTTP ALB, SG-restricted) → (Langfuse skipped) → Auth → Observability
```

(Add AgentCoreGateway + MantleNetwork + MantlePeeringRoutes if web search / GPT-5.x are enabled — see `decision-tree.md` §6–§7.)

| Stack | Domain-less PoC output |
|---|---|
| NetworkStack | VPC, 2 AZ, **1 NAT**, VPC endpoints |
| DataStack | Aurora Serverless v2 (0.5–4 ACU), LiteLLM DB secret (no Langfuse DB/db-init) |
| GuardrailStack | Bedrock Guardrail (kept on — content/PII protection maintained even in a minimal deploy) |
| LiteLLMStack | Fargate 1 task, internet-facing ALB(HTTP:80, SG = `albIngressCidrs`, `idleTimeout=900s`) + internal ALB(4000). Task Role: Claude SigV4 + Mantle Bearer (runtime-minted `BEDROCK_MANTLE_API_KEY`) |
| ~~LangfuseStack~~ | **None** (`enableLangfuse: false`; would require `certMode='acm'`) |
| AuthStack | API GW(IAM) + Token Lambda (STANDARD single tier) + DynamoDB |
| ObservabilityStack | CloudWatch usage dashboard (tokens by model/team, spend, latency, per-user + hourly Logs Insights, ALB requests/5xx; Langfuse link disabled) |
| ~~CdnStack~~ | **None** — CloudFront removed; the ALB is the edge |

---

## 5. cdk-nag — http considerations (no CFR4)

CloudFront is gone, so the old `AwsSolutions-CFR4` (default `*.cloudfront.net` cert → min TLSv1) **no longer
applies**. Instead:

- Expect ALB findings — `AwsSolutions-ELB2` (access logs omitted for the dev sample) and, only if the user
  answered `0.0.0.0/0`, `AwsSolutions-EC23` on the public ALB SG. Suppress with a `PROD TODO` justification;
  the real mitigation is the tight `albIngressCidrs` range already in config.
- No TLS at all is the documented, GATE-1-acknowledged PoC tradeoff (Hard Constraint #17) — the ALB stays
  SG-restricted and the config is labeled PoC-only.

> **WHY?** These are intentional PoC tradeoffs. Moving to production means `certMode='acm'` (a real domain + regional
> ACM cert), which encrypts the wire and lets you enable Langfuse (→ `enterprise-sso.md`).

---

## 6. Onboarding output values (domain-less)

Base URL is the ALB DNS over plain HTTP — no cert trust, no tunnel:

```bash
# Zero-touch: setup-developer.sh reads outputs.json and derives the base URL
# (http://<alb-dns> from the GatewayUrl output) + Token Service URL itself.
cdk deploy --all --outputs-file outputs.json
./scripts/setup-developer.sh
```

`claude-settings.json`'s `ANTHROPIC_BASE_URL` becomes `http://<alb-dns>`. The key helper flow is identical to the
acm path (SigV4 → Token Service → virtual key); only the base URL scheme differs. If a developer cannot connect,
first check that their egress IP is inside `albIngressCidrs`.

---

## Verification checkpoints

- `npx cdk synth --all` passes (LangfuseStack is not in the synthesis list; no CdnStack exists).
- The `GatewayUrl` output is `http://<alb-dns>`; from an allowlisted IP,
  `curl http://<alb-dns>/health/liveliness` returns 200 — from any other IP the connection times out (SG).
- A 120s+ completion (Opus/Fable extended thinking) does **not** 504 — the ALB `idleTimeout` (900s) governs it, and
  there is no CloudFront 120s ceiling.
- `enableLangfuse=true` with this `certMode` fails schema validation (Langfuse requires `acm`).
- `healthcheck.sh` (endpoints auto-resolved from `~/.llm-gateway/env`) issues a key + `/health/liveliness` 200.
