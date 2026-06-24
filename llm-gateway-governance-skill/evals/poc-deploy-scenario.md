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
and I don't need tiering. Region is us-east-2.
```

Expected: in Discovery the skill derives "no domain → domain-less path", "Langfuse off", "single tier",
presents a `config/dev.json` summary at the GATE, then proceeds to code generation/synthesis.

---

## Expected output checklist

### A. config/dev.json derivation (Discovery → configuration)
- [ ] `enableLangfuse: false`
- [ ] `litellm.certMode` is **not** `acm-dns` (`acm-arn` or a non-acm-dns value)
- [ ] `litellm.domainName` / `hostedZoneId` / `hostedZoneName` pass schema even when empty strings
- [ ] When `litellm.certMode='acm-arn'`, `certificateArn` holds a syntactically valid ARN (even a placeholder) — leaving it empty triggers schema fail-fast
- [ ] `litellm.masterKey` is not a hard-coded plaintext (Secrets/CI injection assumed), and `config/dev.json` is gitignored
- [ ] Cost-minimizing knobs: `network.natGateways: 1`, `data.minCapacityAcu: 0.5`, `litellm.desiredCount: 1`

### B. useCustomDomain derivation (most important)
- [ ] `bin/app.ts` evaluates `useCustomDomain = (config.litellm.certMode === 'acm-dns')` → **false**
- [ ] When `useCustomDomain=false`, CdnStack **omits all of** `domainNames`/`certificate`/Route53 alias/Location-rewrite Function
- [ ] CloudFront serves on the default `*.cloudfront.net` + default CloudFront certificate
- [ ] CdnStack output `LiteLlmCfDomain` is of the form `https://<id>.cloudfront.net` (not a custom domain)
- [ ] Does **not** reject the deploy or force a domain on the grounds that no domain exists

### C. Stack combination (excluding Langfuse = 7, plus CDN = synthesis targets)
- [ ] Synthesized stacks: `Network → Data → Guardrail → LiteLLM → Auth → Observability → CDN(us-east-1)`
- [ ] **LangfuseStack not created** (since `enableLangfuse: false`, not instantiated in `bin/app.ts`)
- [ ] **AgentCoreGatewayStack / MantleNetworkStack / MantlePeeringRoutesStack not created** (conditional): since this is a Claude-only PoC without web search, the web-search gateway and Mantle peering stacks are omitted. (Enabling GPT-5.x or web search adds them → `decision-tree.md` §6–§7)
- [ ] CdnStack is pinned to `env.region = 'us-east-1'` (CloudFront ACM requirement)
- [ ] LiteLLM ALB is internal (`internetFacing: false`) — CloudFront VPC Origin is the sole public surface
- [ ] GuardrailStack stays `guardrail.enabled: true` (a content/PII Guardrail exists even in a minimal deploy)

### D. cdk-nag (CFR4 suppression)
- [ ] `AwsSolutions-CFR4` is **suppressed** + the written justification mentions `*.cloudfront.net`/`TLSv1`/`PROD TODO` (acm-dns migration)
- [ ] All intentional dev tradeoff suppressions — `CFR2` (WAF), `IAM5` (Bedrock `*`), etc. — have written justifications
- [ ] Security essentials (TLS termination, Secrets Manager secrets, Token Service IAM auth) are **not** suppressed

### E. Build/synth gate
- [ ] `npm install && npm run typecheck` passes
- [ ] `npx cdk synth --all` passes (LangfuseStack is not in the synthesis list)
- [ ] `data.engineVersion` (e.g. `15.15`) is verified to exist in `us-east-2` (`rds describe-db-engine-versions`)
- [ ] Model IDs/aliases are verified via AWS Knowledge MCP (no stale hard-coding)

### F. Onboarding outputs
- [ ] `scripts/get-gateway-token.sh`, `setup-developer.sh`, `healthcheck.sh` generated
- [ ] `setup-developer.sh` instructs putting the **CloudFront default domain** (`*.cloudfront.net`) in `ALB_DNS`
- [ ] `claude-settings.json` `ANTHROPIC_BASE_URL = https://<cloudfront-domain>`
- [ ] The key helper calls `execute-api` (API Gateway `/auth/token`) with boto3 SigV4

---

## Pass criteria (PASS conditions)

All of A–F satisfied + the following decisive items are true:

1. **The deploy is not blocked even when domain-less** (B's useCustomDomain=false path works).
2. **LangfuseStack is not synthesized** (C).
3. **CFR4 is suppressed with a written justification** (D).
4. **`cdk synth --all` passes** (E).

## Failure signals (FAIL)

- Rejecting the deploy for lack of a hosted zone/domain, or forcing a domain on the user.
- Incorrectly deriving `useCustomDomain=true` and attempting ACM/Route53 issuance (synthesis fails against a non-existent zone).
- Synthesizing LangfuseStack when `enableLangfuse: false`.
- Suppressing CFR4 without justification, or conversely not suppressing it so nag fails as an error.
- Hard-coding `masterKey`/secrets as plaintext in code.
