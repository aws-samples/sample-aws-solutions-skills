# Example — Domainless PoC (default cloudfront.net, Langfuse OFF, minimal deploy)

Hypothetical customer **"Nimbus Labs"** — a startup with no hosted zone and no domain that just wants a
minimal PoC "stood up for evaluation." Exposes the gateway on the default CloudFront `*.cloudfront.net`
without a domain, keeps Langfuse off, and minimizes cost.

> The key point of this example is that **CloudFront works even without a domain**. Do not block the deploy
> just because there is no hosted zone. (`shared/reference/constraints.md` Hard Constraint #1)

---

## 1. Requirements (Discovery answers)

| Question | Nimbus Labs answer |
|---|---|
| Domain? | **None.** No Route53 hosted zone. The default CloudFront domain is sufficient |
| Models? | Claude Sonnet/Haiku only. No tiering needed (everyone identical) |
| Observability? | CloudWatch is enough → **Langfuse OFF** (minimize service surface/cost) |
| Region/account? | `us-east-2`. CdnStack is `us-east-1` |
| MCP tools? | Optional at the evaluation stage — when on, the us-east-1 AgentCore Web Search Tool (built-in connector, no API key needed); when off, omit `mcp_servers.websearch` |
| SSO? | A single default permission set `ClaudeCodeUser`. No economy tier |

---

## 2. `config/dev.json` values

```json
{
  "awsRegion": "us-east-2",
  "enableLangfuse": false,
  "network": { "vpcCidr": "10.0.0.0/16", "maxAzs": 2, "natGateways": 1 },
  "data": { "minCapacityAcu": 0.5, "maxCapacityAcu": 4, "engineVersion": "15.15" },
  "litellm": {
    "certMode": "acm-arn",
    "domainName": "",
    "hostedZoneId": "",
    "hostedZoneName": "",
    "certificateArn": "arn:aws:acm:us-east-2:444455556666:certificate/placeholder-not-used-in-domainless",
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
    "roleName": "ClaudeCodeUser"
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

> **WHY the path to domain-less**:
> - Set `certMode` to a value other than `acm-dns` (`acm-arn`) → in `bin/app.ts`,
>   `useCustomDomain = (config.litellm.certMode === 'acm-dns')` = **false**.
> - When `useCustomDomain=false`, CdnStack **omits all of** `domainNames`/`certificate`/Route53 alias/Location-rewrite
>   Function and serves on the default CloudFront domain (`*.cloudfront.net`) + default CloudFront certificate.
> - Therefore `domainName`/`hostedZoneId`/`hostedZoneName` may be empty strings.
>   The schema passes as long as `certificateArn` is non-empty when `certMode='acm-arn'` → fill in a **placeholder ARN**
>   (it is not actually used on the domain-less path).
> - `enableLangfuse: false` → LangfuseStack skipped. The `langfuse` block is kept but inactive.
> - `natGateways: 1`, `minCapacityAcu: 0.5`, `desiredCount: 1` → lowest cost.

> **Pitfall**: schema validation requires `certificateArn.length > 0` when `certMode='acm-arn'`. Even domain-less,
> leaving the placeholder ARN empty **fail-fasts before deploy**. Put in a dummy ARN that is merely well-formed.

---

## 3. What CdnStack creates in domain-less mode (actual branch code)

The `useCustomDomain=false` path in `lib/cdn-stack.ts`:

```ts
// useCustomDomain=false → skip the zone/cfCert/rewriteLocationFn block entirely
const litellmDist = new cloudfront.Distribution(this, 'LiteLlmDist', {
  ...(useCustomDomain ? { domainNames: [props.litellmDomain], certificate: cfCert } : {}), // ← {} (omitted)
  defaultBehavior: {
    origin: litellmVpcOrigin,                                  // VPC Origin → internal ALB:4000
    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
    cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,      // LLM responses must not be cached
    originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
    // no rewriteLocationFn → functionAssociations omitted
  },
});

new cdk.CfnOutput(this, 'LiteLlmCfDomain', {
  value: useCustomDomain
    ? `https://${props.litellmDomain}`
    : `https://${litellmDist.distributionDomainName}`,        // ← https://dxxxx.cloudfront.net
});
```

> **WHY keep the VPC Origin?** Even without a domain, the ALB is still internal. CloudFront connects via the VPC Origin
> to the ALB (4000) in the private subnet → **the ALB is not exposed to the internet**, regardless of whether a domain exists.

---

## 4. Resulting stack combination (excluding Langfuse = 7)

Because `enableLangfuse: false`, LangfuseStack is dropped:

```
Network → Data → Guardrail → LiteLLM → (Langfuse skipped) → Auth → Observability → CDN(us-east-1, domain-less)
```

| Stack | Domain-less PoC output |
|---|---|
| NetworkStack | VPC, 2 AZ, **1 NAT**, VPC endpoints |
| DataStack | Aurora Serverless v2 (0.5–4 ACU), LiteLLM DB secret (no Langfuse DB/db-init) |
| GuardrailStack | Bedrock Guardrail (kept on — content/PII protection maintained even in a minimal deploy) |
| LiteLLMStack | Fargate 1 task, internal ALB(4000), Task Role SigV4 |
| ~~LangfuseStack~~ | **None** (`enableLangfuse: false`) |
| AuthStack | API GW(IAM) + Token Lambda (STANDARD single tier) + DynamoDB |
| ObservabilityStack | CloudWatch dashboard (Langfuse link widget disabled) |
| CdnStack | CloudFront(LiteLLM) on the **default `*.cloudfront.net`**, **no** ACM/Route53/Function. With no Langfuse distribution, there is no Langfuse CloudFront either |

---

## 5. cdk-nag — CFR4 suppression required

Domain-less mode uses the default CloudFront certificate, so the minimum TLS drops to `TLSv1` → `AwsSolutions-CFR4` fires.
Justify it with a stack-wide suppression in `lib/nag-suppressions.ts`:

```ts
{
  id: 'AwsSolutions-CFR4',
  reason: 'Domain-less dev mode serves CloudFront on its default *.cloudfront.net domain with the default CloudFront viewer certificate, which forces a minimum security policy of TLSv1. PROD TODO: attach a custom domain + ACM cert (certMode=acm-dns) to enforce TLSv1.2_2021.',
},
```

> **WHY is the suppression justified?** This is an intentional PoC tradeoff. When transitioning to production, switching
> to `certMode='acm-dns'` makes CFR4 disappear and enforces TLSv1.2_2021 (→ `enterprise-sso.md`).

---

## 6. Onboarding output values (domain-less)

```bash
# Use the CloudFront default domain directly as ALB_DNS
ALB_DNS=dxxxxxxxxxxxxx.cloudfront.net \
TOKEN_SERVICE_URL=https://poc123.execute-api.us-east-2.amazonaws.com/v1/auth/token \
./scripts/setup-developer.sh
```

`claude-settings.json`'s `ANTHROPIC_BASE_URL` becomes `https://dxxxx.cloudfront.net`, and the key helper flow is
identical regardless of whether a domain exists (SigV4 → Token Service → virtual key).

---

## Verification checkpoints

- `npx cdk synth --all` passes (LangfuseStack is not in the synthesis list).
- CdnStack output `LiteLlmCfDomain` is of the form `https://*.cloudfront.net`.
- The cdk-nag report shows CFR4 as **suppressed**.
- `healthcheck.sh` (ALB_DNS = cloudfront domain) issues a key + `/health/liveliness` 200.
