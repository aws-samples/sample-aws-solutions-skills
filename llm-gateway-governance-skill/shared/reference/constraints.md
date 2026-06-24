# Constraints & Gotchas

Hard-won lessons. Each is a real failure mode observed building/deploying this solution.

## Bootstrap

- **Broken/partial prior bootstrap blocks `cdk bootstrap`.** If an account/region has leftover `cdk-hnb659fds-*` resources (e.g., a `cfn-exec-role`, assets bucket, ECR repo) but no `CDKToolkit` stack (or it's stuck in `REVIEW_IN_PROGRESS`), the new CLI tries to **auto-import** them and fails with `AutomaticImportNeedsRetain`.
  - **Fix (non-destructive):** delete the empty `REVIEW_IN_PROGRESS` `CDKToolkit` stack, add `"@aws-cdk/core:bootstrapQualifier": "<qual>"` to `cdk.json` context, then `cdk bootstrap aws://<acct>/<region> --qualifier <qual>`. Fresh `cdk-<qual>-*` resources are created with no collision. Leave the old leftovers untouched.
- A `REVIEW_IN_PROGRESS` CloudFormation stack has **no real resources** — safe to delete.

## CloudFront without a custom domain

- CloudFront **works without a domain** — it serves on `dxxxx.cloudfront.net` with the default CloudFront viewer certificate. Do NOT force ACM/Route53 on users who have no hosted zone.
- The default viewer cert pins **minimum TLS to `TLSv1`** → cdk-nag `AwsSolutions-CFR4` errors and **fails synth**. Suppress with justification:
  ```ts
  { id: 'AwsSolutions-CFR4', reason: 'Domain-less mode uses the default CloudFront certificate (min TLSv1). PROD TODO: custom domain + ACM for TLSv1.2_2021.' }
  ```
- Also suppress `AwsSolutions-CFR2` (WAF) for dev, or attach a WAF WebACL for prod.
- Make the cert/Route53/rewrite-Function creation **conditional** on `useCustomDomain`; in domain-less mode omit `domainNames`/`certificate` from the Distribution and skip the ARecord. Output `distributionDomainName`.

## Bedrock Guardrails ↔ Mantle incompatibility

- Bedrock Guardrails attach only to **bedrock-runtime** models (Claude). They are **not compatible** with `bedrock_mantle/` (GPT) routes. Do not list a Bedrock guardrail on Mantle models — it will error. Cover Mantle with LiteLLM-level guards (e.g., `hide-secrets`) and document the policy gap.

## AgentCore Web Search gateway (us-east-1)

Web search is the managed **AgentCore Web Search Tool** connector, not Tavily. See `shared/patterns/agentcore-websearch.md`.
- **us-east-1 only (GA).** Pin `AgentCoreGatewayStack` to `config.agentcore.webSearchRegion = us-east-1` and **bootstrap us-east-1** in addition to the gateway region.
- Provision via CFN L1: `AWS::BedrockAgentCore::Gateway` (`ProtocolType: MCP`, `AuthorizerType: AWS_IAM`, `RoleArn`) + `AWS::BedrockAgentCore::GatewayTarget` (`TargetConfiguration.Mcp.Connector.Source.ConnectorId = web-search`, `CredentialProviderConfigurations:[{CredentialProviderType: GATEWAY_IAM_ROLE}]`).
- **`AWS_IAM` inbound** keeps the design tokenless — LiteLLM signs MCP calls with the Task Role (`bedrock-agentcore:InvokeGateway`). Don't pick `CUSTOM_JWT` unless you intend to run an OIDC IdP.
- **Gateway service role** needs `bedrock-agentcore:InvokeGateway` (on `gateway/*`) **and** `bedrock-agentcore:InvokeWebSearch` on the service-owned tool ARN `arn:aws:bedrock-agentcore:us-east-1:aws:tool/web-search.v1` (note the literal `aws` account segment).
- **Gateway `Name`** must match `^([0-9a-zA-Z][-]?){1,100}$` — no underscores, no trailing hyphen.
- Deploy role needs `iam:CreateServiceLinkedRole` (AgentCore SLR).
- The connector tool surfaces in LiteLLM as `websearch-web-search-tool___WebSearch` (input `query`, `maxResults` 1–25). Verify with `GET /v1/mcp/tools`.

## Bedrock Mantle (us-east-1 cross-region VPC peering)

GPT-5.x (Mantle) is **us-east-1 only**; reach it privately via cross-region VPC peering. See `shared/patterns/mantle-peering.md`.
- **Cross-region peering is NOT auto-accepted.** Accept it with an `AwsCustomResource` whose SDK call sets `region` to the **primary** (accepter) region.
- **Cross-region private DNS**: an interface endpoint's private DNS only resolves in its own region. Set `privateDnsEnabled:false` on the `bedrock-mantle` endpoint and publish a `CfnHostedZone` (PHZ) for `bedrock-mantle.us-east-1.api.aws` associated with **both** VPCs (cross-region via the `VpcRegion` field), aliased to the endpoint's regional DNS entry.
- **Routes are regional** → primary-side routes (`peerCidr → pcx`) must live in a primary-region stack (`MantlePeeringRoutesStack`), not in the us-east-1 stack. Keep it acyclic: NetworkStack only exports the VPC.
- `mantle.peerVpcCidr` **must not overlap** `network.vpcCidr` (schema-validate).
- Both LiteLLM and the routes stack need `crossRegionReferences: true`; bootstrap us-east-1 + the gateway region.
- Pin Mantle to us-east-1 via the vars LiteLLM's `bedrock_mantle` provider actually reads: each GPT
  model's `aws_region_name=us-east-1` + env `BEDROCK_MANTLE_REGION=us-east-1` +
  `BEDROCK_MANTLE_API_BASE=https://bedrock-mantle.us-east-1.api.aws`. **`MANTLE_REGION` is NOT read by
  the provider** — using it alone leaves the endpoint at `AWS_REGION` (gateway region) and the call
  fails with "Cannot connect to host bedrock-mantle.<gw-region>.api.aws".

## Mantle Marketplace auto-subscribe + CloudFront timeout

- Mantle models are **AWS Marketplace** offerings. The LiteLLM Task Role needs `aws-marketplace:Subscribe` (+ `ViewSubscriptions`/`Unsubscribe`) — without it the first GPT-5.x call returns `access_denied ... aws-marketplace:Subscribe`.
- The **first** call auto-subscribes (~1 min); during setup the request can exceed CloudFront's default **30s** origin timeout → **504**. Raise the LiteLLM CloudFront origin `readTimeout`/`keepaliveTimeout` to **60s** (max without quota). Steady-state is sub-second. Recommend a one-time per-model warm-up after a fresh-account deploy.

## Region selection (config.awsRegion is authoritative)

- The platform region is `config.awsRegion`. `bin/app.ts` resolves `config.awsRegion ?? process.env.CDK_DEFAULT_REGION ?? AWS_REGION` — **config wins**, so a sandbox/CI profile with no region (which makes the CLI inject an arbitrary region) cannot misdirect the platform stacks.
- AgentCoreGateway and MantleNetwork are **always pinned to us-east-1** (Web Search GA / Mantle home region). `CdnStack` is also pinned to us-east-1, but the **hard requirement applies only with a custom domain**: CloudFront accepts its viewer **ACM cert only from us-east-1** (and a CloudFront-scoped WAF WebACL is us-east-1-only too). In **domain-less mode** no ACM cert is created, so the pin is for consistency rather than a strict requirement — CloudFront is global and VPC Origin does not force same-region with the ALB. Only the platform stacks follow `config.awsRegion`.
- Do **not** require editing `lib/config/constants.ts` to change region — it is config-driven.

## Secrets — do NOT hard-code

- Never hard-code real credentials. The reference Langfuse stack historically embedded a plaintext admin password and `LANGFUSE_INIT_*` keys in the task `environment` block — **this is a defect**, not a pattern to copy.
  - Move admin password + project secret key to **Secrets Manager** (`generateSecretString`) and inject via `ecs.Secret.fromSecretsManager` (the stack already does this for `NEXTAUTH_SECRET`/`SALT`).
  - The LiteLLM trace keys (`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`) are **shared** with Langfuse's `LANGFUSE_INIT_PROJECT_*` keys → they must match. Because LiteLLM is created **before** Langfuse, a shared secret must live in an **earlier** stack (e.g., DataStack) for both to consume at synth time.
  - `LANGFUSE_INIT_*` only seed on the **first** boot with an empty DB; changing them later requires DB reset or manual rotation.
- The LiteLLM master key belongs in Secrets Manager; the Token Service gets `grantRead` only.

## IAM least privilege

- The reference Task Role uses `resources: '*'` and a `bedrock-mantle:*` wildcard — acceptable for a dev sample (tag with `PROD TODO`), but scope to specific model / inference-profile ARNs for production.

## Networking

- ALBs are **internal** (`internetFacing: false`); the only public surface is CloudFront via **VPC Origin**. VPC Origin does **not** require the CdnStack (us-east-1) and the ALB to be in the same region — the distribution references the ALB ARN and CloudFront routes cross-region. Pass the ALB across stacks/regions with `crossRegionReferences: true` (CdnStack us-east-1 ← ALB in `config.awsRegion` is the verified default).
- Single NAT gateway is a cost/HA tradeoff (dev). Production: one NAT per AZ.

## Data

- `removalPolicy: DESTROY` + deletion protection off is intentional for a tear-downable dev sample. **Production: `RETAIN` + backups + deletion protection.**
- Validate `data.engineVersion` against `aws rds describe-db-engine-versions --engine aurora-postgresql` in the **target region** before deploy — an invalid version fails DataStack create after a long wait.

## LiteLLM image

- The reference image pins a specific LiteLLM tag and overlays a SigV4 patch for the `bedrock_mantle` responses route (PR #29788). The Dockerfile `grep`-verifies the patch applied and **fails the build** if not — keep the overlay files in sync with the base tag, and remove the overlay once a release ships the patch.
- Building the image requires a **running Docker daemon** at `cdk deploy` time.

## Deploy targeting

- To deploy a subset (e.g., skip a stack), pass explicit stack names: `cdk deploy NetworkStack DataStack ...`. CDK respects dependency order with `--all`.
- IAM/security changes prompt approval; `--require-approval never` is acceptable for an explicitly requested deploy.

## Client onboarding (token helper)

- **Never hardcode the SigV4 region in `get-gateway-token.sh`.** The signing region must equal the Token Service API Gateway's region, which is already in the URL host (`{id}.execute-api.{region}.amazonaws.com`). Parse it from `TOKEN_SERVICE_URL` so the helper is deploy-region-agnostic. A hardcoded region (the original bug) breaks every deploy in a different region with `Credential should be scoped to a valid region` (HTTP 403 at API Gateway, before the Lambda runs).
- The empty POST body (`{}`) must be **byte-identical** between the signed payload and the sent payload — identity comes from the signed caller ARN, not the body.
- `claude-settings.json` / `codex-config.toml` carry **no secret** — only the helper path (`apiKeyHelper` / `auth.command`). `ANTHROPIC_BASE_URL` / `base_url` must be the **public CloudFront domain**, never the internal ALB DNS.
- Quick single-operator test without SSO: use the LiteLLM master key directly as the Bearer against `<cloudfront>/v1` (the Token Service rejects non-`AWSReservedSSO_` callers by design).

## LiteLLM Admin UI redirects (the #1 domain-less gotcha)

Two independent redirect bugs make the UI bounce the browser to an unreachable host. Both must be handled:

1. **Origin 307 Location (`/ui` → `/ui/`).** uvicorn builds the Location from the forwarded Host but with its own scheme/port → `http://<host>:4000/ui/`, unreachable via CloudFront. **Fix:** a viewer-response CloudFront Function that rewrites `^https?://[^/]+` to `https://<viewer Host header>`. Use the **request Host header** (not a hardcoded domain) and attach it in **both** custom-domain and domain-less modes — domain-less is exactly when it's needed. `curl -I https://<dist>/ui` should then show `location: https://<dist>/ui/`.

2. **`PROXY_BASE_URL` (the SPA absolute base).** The LiteLLM UI is a SPA that builds absolute URLs/redirects from `PROXY_BASE_URL`. If it's a placeholder (e.g. `https://llmlite.example.com`), the browser is redirected there even though `curl /ui/` returns 200. **Fix:** `PROXY_BASE_URL` must be the public URL the browser uses. With a custom domain that's the domain. **Domain-less:** the CloudFront domain isn't known until the CDN stack deploys, so use a **two-phase deploy** — deploy CDN, take `dxxxx.cloudfront.net`, set `config.litellm.domainName` to it, and redeploy LiteLLM. (Alternative: publish the CF domain to SSM and read it in the container entrypoint.)

> Symptom signature: `curl` of `/ui/` returns 200, but a real browser still redirects to a weird host. That points at `PROXY_BASE_URL`, not the Location header.

3. **Langfuse has the identical pair of bugs.** Langfuse (NextAuth) uses `NEXTAUTH_URL` exactly like LiteLLM uses `PROXY_BASE_URL` — a placeholder makes login bounce to the dead host. Fix the same way: add `config.langfuse.publicUrl`, set it to the Langfuse `*.cloudfront.net` domain (two-phase), redeploy Langfuse. Also attach the **same** Location-rewrite Function to the **Langfuse** CloudFront distribution (not just LiteLLM's). Both distributions need it in domain-less mode.

## Token Service first-issuance race (recovery bug)

On the very first key issuance, two near-simultaneous client calls (Claude Code/Codex fire the key helper more than once) can race: call A creates the virtual key (`/key/generate` 200) and caches it; call B then hits `/key/generate` 400 (`Key with alias 'sso-<user>' already exists`) and the reference recovery path queries `/user/info?user_id=<user>` which returns **404** (the user was never registered as a LiteLLM user, only as key metadata) → the Lambda returns 500. It **self-heals** once the cache is populated (subsequent calls hit DynamoDB), so it's a transient on first use. **Robust fix for generated code:** recover the existing key by **alias lookup** (`/key/info` / `/key/list` filtered by `key_alias`) instead of `/user/info`, and/or re-check the DynamoDB cache immediately before calling `/key/generate` to close the race window.


## Security Group descriptions must be ASCII (deploy-time failure)

EC2 `GroupDescription` only accepts the ASCII set `[a-zA-Z0-9 ._\-:/()#,@\[\]+=&;{}!$*]`.
A non-ASCII character (an **em-dash `—`**, smart quotes, etc.) in any `SecurityGroup` `description`
fails create with `Resource handler returned message: "Value (...) for parameter GroupDescription
... InvalidRequest"`, which **rolls back the whole NetworkStack**. Use a plain hyphen `-`, not `—`.
This applies to every SG description string in NetworkStack and MantleNetworkStack.

## "Master key works" ≠ "SSO path works" (verification trap)

Three distinct request paths must each be verified — passing one does NOT prove the others:
1. **Gateway → Bedrock** (admin): master key Bearer → `/v1/chat/completions`. Proves model access only.
2. **Virtual-key leg**: mint a key via master-key `/key/generate` (assign the tier team), then call
   `/v1` with **that virtual key**. Proves team/model scoping + the key issuance LiteLLM does.
3. **Full SSO path**: `aws sso login` → key helper → API Gateway (IAM) → Token Lambda → virtual key.
   Proves the SSO permission set + inline policy + assignment.

A common failure: paths 1 and 2 pass but path 3 fails (clients silently get nothing) because of an
**SSO inline-policy region mismatch** (next gotcha). Always test path 3 with a real SSO user — do not
declare success from a master-key test alone.

## SSO permission set — decide in Discovery, then create (don't assume)

- **The permission set + group are a Discovery decision, not a default to silently reuse.** During
  Phase 1 the agent MUST ask: *create a new permission set for this gateway or reuse an existing one?
  what name? which group(s) or users to assign?* — and then create/assign per that answer. Do **not**
  pick a pre-existing permission set just because its name matches the config default (`ClaudeCodeUser`);
  a name match is not ownership, and editing a shared permission set can change access for unrelated
  groups/another gateway. When in doubt, create a dedicated, uniquely-named permission set scoped to the
  users/groups the user specifies.
- **Inline policy `Resource` MUST match the deployed Token Service region + API id:**
  `arn:aws:execute-api:<config.awsRegion>:<account>:<tokenServiceApiId>/*`.
  A stale region (e.g. `us-east-2` while the gateway is `ap-northeast-2`) makes API Gateway **deny every
  SSO token request with 403** — the Token Lambda never runs, no virtual key is issued, and clients fail
  with no useful error (invisible to master-key/virtual-key tests above).
- After any inline-policy change, **`provision-permission-set`** or it does not take effect; users may
  also need to `aws sso login` again.
- `GetRoleCredentials ... ForbiddenException: No access` on the client means the SSO user is **not in a
  group assigned** to the permission set — an assignment problem, not a gateway problem.
