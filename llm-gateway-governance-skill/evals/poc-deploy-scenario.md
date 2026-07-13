# Eval — PoC Deploy Scenario (Domain-less PoC Deployment)

A black-box checklist that verifies the `llm-gateway-governance` skill correctly generates/deploys
a **minimal domain-less PoC**. The evaluator does not look at the skill's internal implementation,
and judges solely on **user input → expected outputs**.

Related example: `shared/examples/domainless-poc.md`. Decision rationale: `shared/reference/decision-tree.md` §1.

---

## User input (simulated prompt)

```
I don't have a domain or a Route53 hosted zone. For now, just stand up an LLM gateway for evaluation.
Use only Claude Sonnet/Haiku, turn off observability tools like Langfuse to save cost,
and I don't need tiering. Region is us-east-2. Our office egress range is 203.0.113.0/24.
```

Expected: in Discovery the skill derives "no domain → certMode http (public ALB, HTTP:80, SG-restricted)",
"Langfuse off", "single tier", asks for/captures the allowed source CIDRs (`albIngressCidrs`), surfaces the
**plaintext acknowledgement** at GATE 1, presents a `config/dev.json` summary, then proceeds to code
generation/synthesis.

---

## Expected output checklist

### A. config/dev.json derivation (Discovery → configuration)
- [ ] `enableLangfuse: false` (Langfuse would require `certMode='acm'`, so it cannot be on here)
- [ ] `litellm.certMode` is `http` — **never** `self-signed` (removed), never an `acm-dns`/`*.cloudfront.net` default-domain value
- [ ] `litellm.domainName` / `hostedZoneId` / `hostedZoneName` / `certificateArn` pass schema even when empty strings (http needs none)
- [ ] `litellm.albIngressCidrs: ["203.0.113.0/24"]` — captured in Discovery (a required answer); GATE 1 carries the **plaintext acknowledgement** (and a separate explicit acknowledgement if the user insisted on `0.0.0.0/0`)
- [ ] `litellm.albIdleTimeoutSeconds` present or defaulted (900s) — governs long completions (no 120s CloudFront ceiling)
- [ ] `litellm.masterKey` is not a hard-coded plaintext (Secrets/CI injection assumed), and `config/dev.json` is gitignored
- [ ] Cost-minimizing knobs: `network.natGateways: 1`, `data.minCapacityAcu: 0.5`, `litellm.desiredCount: 1`

### B. Domain-less edge = certMode http (most important)
- [ ] No `useCustomDomain`/`acm-dns` derivation and **no CdnStack** — `LiteLLMStack` reads `certMode` directly
- [ ] `http` → an **internet-facing public ALB** with a plain **HTTP:80** listener (no cert), whose SG allows ingress **only from `albIngressCidrs`** — no AWS WAF, no `SelfSignedCert` Custom Resource, no SSM tunnel
- [ ] A separate **internal ALB (HTTP:4000)** carries the Token Service path (SSM URL unchanged), never internet-exposed
- [ ] `GatewayUrl` output is `http://<alb-dns>` — not a `*.cloudfront.net` domain, not `localhost`
- [ ] Does **not** reject the deploy or force a domain on the grounds that no domain exists

### C. Stack combination (excluding Langfuse and CloudFront)
- [ ] Synthesized stacks: `Network → Data → Guardrail → LiteLLM → Auth → Observability`
- [ ] **No CdnStack** — CloudFront is removed; the ALB is the edge
- [ ] **LangfuseStack not created** (since `enableLangfuse: false`, not instantiated in `bin/app.ts`; it would also require `certMode='acm'`)
- [ ] **AgentCoreGatewayStack / MantleNetworkStack / MantlePeeringRoutesStack not created** (conditional): since this is a Claude-only PoC without web search, the web-search gateway and Mantle peering stacks are omitted. (Enabling GPT-5.x or web search adds them → `decision-tree.md` §6–§7)
- [ ] LiteLLM has an **internet-facing public ALB (HTTP:80, SG = `albIngressCidrs`)** + a separate internal ALB(4000). The ECS tasks stay private (`PRIVATE_WITH_EGRESS`)
- [ ] GuardrailStack stays `guardrail.enabled: true` (a content/PII Guardrail exists even in a minimal deploy)

### D. cdk-nag (no CloudFront findings)
- [ ] **No `AwsSolutions-CFR4`/`CFR2`/`CFR3`/`CFR5`** findings — CloudFront is removed
- [ ] ALB findings (`ELB2` access logs; `EC23` only if the user answered `0.0.0.0/0`) carry written `PROD TODO` justifications; the real mitigation is the tight `albIngressCidrs` range (no WAF is deployed)
- [ ] The plaintext-key risk is documented as a GATE-1 acknowledgement (Hard Constraint #17)
- [ ] Security essentials (Secrets Manager secrets, Token Service IAM auth) are **not** suppressed

### E. Build/synth gate
- [ ] `npm install && npm run typecheck` passes
- [ ] `npx cdk synth --all` passes (LangfuseStack is not in the synthesis list)
- [ ] `data.engineVersion` (e.g. `15.15`) is verified to exist in `us-east-2` (`rds describe-db-engine-versions`)
- [ ] Model IDs/aliases are verified via AWS Knowledge MCP (no stale hard-coding)

### F. Onboarding outputs
- [ ] Ends by generating the **two HTML onboarding docs** (`developer-setup.html` + `admin-onboarding.html`) via `scripts/gen-onboarding.py`; `get-gateway-token.sh`/`setup-developer.sh`/`healthcheck.sh` are still generated
- [ ] **Onboarding is zero-touch**: the agent runs `setup-developer.sh` itself after `cdk deploy --outputs-file outputs.json`; the script derives the gateway scheme+host from the `GatewayUrl` output and the Token Service URL from `TokenServiceUrl` (no operator env-var assembly), and persists them to `~/.llm-gateway/env` for the key helper/healthcheck
- [ ] Base URL = the **`GatewayUrl` output** `http://<alb-dns>` — never a `*.cloudfront.net` domain, never `localhost:4000`, no tunnel command, no `ca.pem` step
- [ ] The developer doc states the **plaintext / SG-allowlist** nature of the endpoint (troubleshooting: check the caller's egress IP against `albIngressCidrs`)
- [ ] `claude-settings.json` `ANTHROPIC_BASE_URL = http://<alb-dns>`
- [ ] The key helper calls `execute-api` (API Gateway `/auth/token`) with boto3 SigV4
- [ ] A 120s+ completion (Opus/Fable) does **not** 504 — the ALB `idleTimeout` (900s) governs it (no CloudFront ceiling)

---

## Pass criteria (PASS conditions)

All of A–F satisfied + the following decisive items are true:

1. **The deploy is not blocked even when domain-less** — `certMode='http'` works with no domain, with `albIngressCidrs` captured in Discovery and the plaintext acknowledgement at GATE 1 (A/B).
2. **LangfuseStack is not synthesized, and there is no CdnStack** (C).
3. **No CloudFront (CFR*) findings**; nag findings carry justifications; no WAF resources are synthesized (D).
4. **`cdk synth --all` passes**, and a 120s+ completion does not 504 (E/F).

## Failure signals (FAIL)

- Rejecting the deploy for lack of a hosted zone/domain, or forcing a domain on the user.
- Re-introducing CloudFront/CdnStack, `acm-dns`/`useCustomDomain`, `*.cloudfront.net`, a `self-signed` certMode / `SelfSignedCert` Custom Resource / `ca.pem` distribution, an SSM port-forward tunnel, or an AWS WAF WebACL.
- Synthesizing LangfuseStack when `enableLangfuse: false` (or accepting `enableLangfuse: true` with `certMode≠acm`).
- Deploying the `http` public ALB **without** the `albIngressCidrs` SG allowlist, or skipping the GATE-1 plaintext acknowledgement (incl. the extra one for `0.0.0.0/0`).
- Hard-coding `masterKey`/secrets as plaintext in code.
