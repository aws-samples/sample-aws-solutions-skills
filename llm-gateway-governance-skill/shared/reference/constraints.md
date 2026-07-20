# Constraints & Gotchas

Hard-won lessons. Each is a real failure mode observed building/deploying this solution.

## Bootstrap

- **Broken/partial prior bootstrap blocks `cdk bootstrap`.** If an account/region has leftover `cdk-hnb659fds-*` resources (e.g., a `cfn-exec-role`, assets bucket, ECR repo) but no `CDKToolkit` stack (or it's stuck in `REVIEW_IN_PROGRESS`), the new CLI tries to **auto-import** them and fails with `AutomaticImportNeedsRetain`.
  - **Fix (non-destructive):** delete the empty `REVIEW_IN_PROGRESS` `CDKToolkit` stack, add `"@aws-cdk/core:bootstrapQualifier": "<qual>"` to `cdk.json` context, then `cdk bootstrap aws://<acct>/<region> --qualifier <qual>`. Fresh `cdk-<qual>-*` resources are created with no collision. Leave the old leftovers untouched.
- **⚠️ `cdk bootstrap` saying "no changes" does NOT mean "correctly bootstrapped" (real-deploy incident).** A shared/sandbox account may already be bootstrapped with a **different, custom qualifier** — the default-qualifier command then reports "no changes" while the default-qualifier IAM roles (`cdk-hnb659fds-*`) don't exist, and `cdk deploy` fails with `could not be used to assume 'arn:aws:iam::...:role/cdk-hnb659fds-file-publishing-role-...'` / `SSM parameter /cdk-bootstrap/hnb659fds/version not found`. **Before deploying, check the actual qualifier**: `aws cloudformation describe-stacks --stack-name CDKToolkit` → the `Qualifier` parameter — and set `"@aws-cdk/core:bootstrapQualifier"` in `cdk.json` to match. Verify with `cdk synth` that `BootstrapVersion` references `/cdk-bootstrap/<qual>/version`.
- A `REVIEW_IN_PROGRESS` CloudFormation stack has **no real resources** — safe to delete.

## npm / CDK toolchain version alignment (real-deploy incident)

- **`aws-cdk-lib` and `cdk-nag` must be co-resolved.** A caret range like `cdk-nag@^2.28.x` resolves to the latest 2.x at install time, whose peer dependency may demand a newer `aws-cdk-lib` than your pin → `npm error ERESOLVE`. Pin recent, compatible versions together (e.g. `aws-cdk-lib@2.213.0` + `cdk-nag@^2.38.2`) rather than an old lib pin + open nag range.
- **The `aws-cdk` CLI must be ≥ the library's cloud-assembly schema.** A new `aws-cdk-lib` with an old CLI fails at synth with `Cloud assembly schema version mismatch: Maximum schema version supported is 43.x, but found 48.0.0`. Treat `aws-cdk-lib` + `aws-cdk` as a **set**: upgrade the CLI at least to the version the error names (e.g. `aws-cdk@^2.1033.0`).

## CDK cross-stack SG ownership (cyclic-reference — recurred 3x in a real deploy)

- **Rule**: `sgA.addIngressRule(sgB, ...)` creates the ingress resource **in sgA's stack** and, if `sgB` belongs to another stack, imports its GroupId → **sgA's stack now depends on sgB's stack**. If sgB's stack already depends on sgA's (e.g. for the VPC), that is a **cyclic reference at synth**.
- This includes **hidden auto-wiring**: `addTargets()` auto-creates "ALB SG → target SG" rules, and `grantConnect()`-style helpers do the same. Keeping the ALB SG and the ECS service SG in the **same app stack** keeps auto-wiring stack-local.
- **Design applied here** (see `cdk-stacks.md`): NetworkStack owns only shared-infra SGs (Aurora / interface endpoints / Token Lambda) and grants ingress by **private-subnet CIDR**, never by reference to an app-stack SG; LiteLLMStack/LangfuseStack own their service + ALB SGs. CIDR ingress is broader than SG-to-SG but the peers are our own private subnets, and the cross-stack edge disappears.
- **Meta-lesson**: this error recurred three times in one deploy because each occurrence was patched individually. On the **second** occurrence of the same error shape, stop, derive the general rule, and apply it everywhere at once.

## Pinned-image CLI flags — verify before emitting (real-deploy incident)

- A CLI flag documented in a skill/reference doc may **not exist in the pinned image tag** you deploy. The `--forwarded-allow-ips` flag killed the LiteLLM container at boot (`Error: No such option`, exitCode 2 → circuit breaker retried 5x → automatic rollback) because the pinned tag's `litellm` CLI never had it (see "LiteLLM Admin UI + Langfuse redirects").
- **Pre-deploy check (seconds, saves redeploy cycles)**: `docker run --rm --entrypoint litellm <pinned-image> --help` for every flag the entrypoint passes; for env-var claims, inspect the actual installed source (`docker run --rm --entrypoint cat <image> <path>`), the same discipline as the Mantle SigV4 correction above.
- **Diagnosis pattern**: a container dying within seconds (exitCode N, "Essential container exited") tells you nothing from ECS events alone — **CloudWatch Logs first**; the real error is the last lines of the container log.

## Deploy-process lifecycle — don't edit code under a live deploy (real-deploy incident)

- `cdk deploy` builds the Docker image **from the source at the moment it started**. Editing the entrypoint/config while a deploy is still running (or retrying under the ECS circuit breaker) does **nothing** for that deploy — it keeps failing with the old code, costing a full retries+rollback cycle (~10-15 min).
- Before re-deploying after a fix: check for a still-running deploy (the CLI process, or CloudFormation stack status `*_IN_PROGRESS`), let it finish or roll back (or cancel it), **then** start a fresh `cdk deploy` from the fixed source.

## Edge TLS via `certMode` (CloudFront removed — the ALB is the edge)

CloudFront is gone; the ALB is the public edge and `config.litellm.certMode` chooses the TLS strategy. There is no `*.cloudfront.net` default domain, no `useCustomDomain` derivation, and no `acm-dns`/`acm-arn` split. The ALB is **always internet-facing and always SG CIDR-restricted** (`litellm.albIngressCidrs`) — there is no self-signed mode, no internal/VPN exposure variant, no SSM tunnel, and no AWS WAF.

- **`acm` (✅ recommended / PROD)** — internet-facing ALB, HTTPS:443 with a **public ACM cert issued in `config.awsRegion`** (a regional ALB cert, **not** a us-east-1 CloudFront cert). Provide either an existing `certificateArn`, or `domainName`+`hostedZoneId`+`hostedZoneName` (CDK DNS-issues the cert + a Route53 A-record alias + an HTTP→443 redirect). Auto-renews, publicly trusted → clients need no config. **Fail-fast at synth** if neither the ARN nor the 3 zone fields are set.
- **`http` (⛔ PoC only)** — internet-facing ALB, **HTTP:80, no cert, no domain**. The virtual key **and prompt/response bodies** travel **plaintext** on the wire; the SG allowlist is the only access control. This is a **GATE-1 acknowledgement item**, and `albIngressCidrs = 0.0.0.0/0` (plaintext open to the whole internet) requires its own explicit acknowledgement. Prefer `acm` whenever a domain is available or the traffic is sensitive.
- **`albIngressCidrs` (both modes)** — a **required Discovery answer** (which office/NAT egress CIDRs may reach the ALB). The skill generates the SG from this answer, so validation lives in Discovery + GATE-1, not in a synth-time guard.
- **AWS WAF is not deployed.** SG CIDR allowlisting is the access control; LiteLLM virtual-key auth + budget caps handle abuse. For a deliberately open (`0.0.0.0/0`) `acm` production deployment, note WAF as an optional hardening step in docs only.
- **Langfuse UI is `acm`-only** — it needs a real domain + ACM cert (its own internet-facing ALB). `enableLangfuse=true` with `certMode='http'` is a **schema fail-fast**; those deploys are CloudWatch-only.

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

## ⛔ GATE: GPT-5.6 is not a valid model choice — offer only GPT-5.5 / GPT-5.4 (real-deploy incident)

**Do not let Discovery/GATE-1 accept a GPT-5.6-family model (`gpt-5.6-sol`/`-terra`/`-luna` or any other `gpt-5.6-*`) for a Codex-facing deployment.** When a user asks for "the newest/fastest GPT" or names a `5.6` variant, the agent must redirect to `gpt-5.5` (flagship) / `gpt-5.4` (economy) and explain why, rather than deploying what was asked.

- **OpenAI's own "Configure Codex with Amazon Bedrock" support article lists exactly four Bedrock model IDs**: `openai.gpt-5.5`, `openai.gpt-5.4`, `openai.gpt-oss-120b`, `openai.gpt-oss-20b` (recommended default: `gpt-5.5`). `gpt-5.6-*` is **not on that list** — it may exist as a Bedrock Mantle model, but it is not a model OpenAI has certified for the Codex↔Bedrock integration.
- **Root-caused failure mode (do not re-diagnose this from scratch if seen again):** Codex CLI sets `namespace_tools: true` in its `ProviderCapabilities` for any provider using `wire_api = "responses"` (confirmed: this is not `amazon-bedrock`-provider-specific — the same wrapping fires for custom `model_providers.<id>` entries with `wire_api="responses"`, which is how this gateway's Codex client config works). Once **any real config with tools** is in play (built-in `shell`/`apply_patch`, or any registered MCP server — this repo's default Codex config registers `mcp_servers.node_repl`/`computer-use`), Codex serializes tool definitions using an OpenAI-proprietary `{"type": "namespace", ...}` wrapper. **Bedrock Mantle's Responses API schema validator only accepts `"function"` and `"mcp"` tool types** and rejects `"namespace"` with `400 validation_error: Invalid 'input'/'tools': value did not match any expected variant` / `unknown variant 'namespace'`. Symptom in the client: Codex reports this as "Reconnecting... N/5 — We're currently experiencing high demand" (misleading — it is a hard 400, not load), then gives up. See upstream: `openai/codex#25034` (closed) and OpenAI's own compatibility note: "Because MCP namespace tools and tool search are not currently available, MCP and tool discovery functionality may be limited in this configuration."
- **This was reproduced end-to-end on `gpt-5.6-sol`** (CloudWatch: 138/149 `/v1/responses` calls in a live Codex session returned 500 with the `namespace` validation error; the only 200s were single-turn/no-tool requests) and **did not reproduce after switching the deployment to `gpt-5.5`/`gpt-5.4`** in the same Codex session with the same MCP servers registered — but treat that as **not yet a proven root-cause fix**; it may be coincidental (e.g., timing, a different Mantle backend build). Re-verify with a fresh multi-turn, tool-using Codex session before assuming GPT-5.6 vs GPT-5.5/5.4 is *the* deciding factor, independent of the `namespace_tools` mechanism above.
- **Action for the skill**: `lib/config/constants.ts` `MODELS` should define `GPT55`/`GPT54` (`bedrock_mantle/openai.gpt-5.5` / `openai.gpt-5.4`) and never a `GPT56_*` entry; `config.yaml`'s `model_list` should route only those two. If a future user insists on `gpt-5.6-*` anyway, warn explicitly about the `namespace_tools`/`400 validation_error` failure mode above before deploying it, and confirm they understand Codex tool-use may break mid-session even though a plain first-turn call succeeds.
- **Status update (2026-07-19, re-verified against a live deployment)** — the landscape moved, but the gate stays until the unblock conditions below are met:
  - The `namespace` tool type is now **accepted by Mantle for `gpt-5.4`/`gpt-5.5`** (AWS fixed it server-side; `openai/codex#25034` closed with no Codex-side change — direct `/v1/responses` test with a namespace-wrapped tool returned 200 on both models). The original root cause is therefore resolved for 5.4/5.5; **not yet re-verified on a 5.6 model**.
  - OpenAI's own Bedrock guide (feature table dated 2026-07-13) now lists GPT-5.6 on Bedrock, and LiteLLM ships `gpt-5.6-sol/terra/luna` cost-map/routing entries — but only in **v1.94 pre-releases** (PR #33412; no stable release yet). This skill pins `v1.89.0-rc.1`, where a 5.6 route **falls through to chat-completions emulation and Mantle rejects it** — so 5.6 cannot work on the pinned image regardless of the namespace fix.
  - **Unblock conditions**: ① bump the LiteLLM image pin to a tag ≥ v1.94 containing #33412 (re-verify `transformation.py` per `litellm-gateway.md`), ② add the 5.6 route to `config.yaml`/constants, ③ pass a fresh **multi-turn, tool-using Codex session** on 5.6 (with `web_search = "disabled"` — a `web_search` tool produces the *same* misleading "high demand" symptom and must not contaminate the re-test).


## Mantle Marketplace auto-subscribe + ALB idle timeout for long completions

- Mantle models are **AWS Marketplace** offerings. The LiteLLM Task Role needs `aws-marketplace:Subscribe` (+ `ViewSubscriptions`/`Unsubscribe`) — without it the first GPT-5.x call returns `access_denied ... aws-marketplace:Subscribe`.
- The **first** call auto-subscribes (~1 min). Steady-state is sub-second. Recommend a one-time per-model warm-up after a fresh-account deploy.
- **Warm-up calls must use `max_tokens` ≥ 16** — the Mantle (OpenAI Responses) route rejects smaller values with `integer_below_min_value ... Expected a value >= 16` (a tiny probe like `max_tokens: 10` fails; use e.g. `max_tokens: 32`). Claude routes don't have this floor, so a warm-up loop that reuses one payload across all models must satisfy the strictest one.
- **Long completions are governed by the ALB `idleTimeout`, not any CloudFront ceiling.** CloudFront is removed, so the old **hard 120s VPC-Origin read-timeout ceiling is gone** — it used to 504 Opus/Fable extended-thinking responses **with no matching LiteLLM access-log line** (CloudFront severed the origin connection before uvicorn logged). Now set `config.litellm.albIdleTimeoutSeconds` (default 900s, max 4000s) high enough for your longest completion (measured: a Fable 5 extended-thinking 500-word essay took ~24s — there is now ample headroom above that). The same idle timeout absorbs the Marketplace cold-start on the first Mantle call. Diagnostic hint: if a client still sees a 504/timeout, check the ALB `idleTimeout` and the ECS target health first, then query CloudWatch Logs Insights at the failure timestamp to confirm whether the request reached the origin.

## Region selection (config.awsRegion is authoritative)

- The platform region is `config.awsRegion`. `bin/app.ts` resolves `config.awsRegion ?? process.env.CDK_DEFAULT_REGION ?? AWS_REGION` — **config wins**, so a sandbox/CI profile with no region (which makes the CLI inject an arbitrary region) cannot misdirect the platform stacks.
- AgentCoreGateway and MantleNetwork are **always pinned to us-east-1** (Web Search GA / Mantle home region). There is **no CdnStack** (CloudFront removed) — the ALB is the edge, and its ACM cert (acm mode) is **regional** (`config.awsRegion`). There is no AWS WAF. Everything else follows `config.awsRegion`.
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

- The ALB is the edge (CloudFront removed). A **public, internet-facing ALB** fronts LiteLLM in both modes (`acm` HTTPS:443, `http` HTTP:80), with SG ingress restricted to the `albIngressCidrs` allowlist (no AWS WAF); the ECS tasks stay in `PRIVATE_WITH_EGRESS`. A **separate internal ALB (HTTP:4000)** always exists for the Token Service — its SSM URL `LITELLM_INTERNAL_URL` is **unchanged**, so the auth plane needs no edit and there is no NAT hairpin. Never expose the internal ALB or the `:4000` listener to the internet.
- Single NAT gateway is a cost/HA tradeoff (dev). Production: one NAT per AZ.

## CDK ELBv2 `addListener()` silently opens `0.0.0.0/0` (real-deploy incident — security)

- **`elbv2.ApplicationLoadBalancer.addListener()` defaults `open: true`.** When the ALB is `internetFacing: true`, CDK auto-adds an ingress rule for **`0.0.0.0/0`** on the listener's port to the ALB's security group — *in addition to* whatever CIDR allowlist you already added via `securityGroup.addIngressRule(...)`. This silently defeats `config.litellm.albIngressCidrs`: the SG allowlist stays correct-looking in your own code, but the listener creation appends a second, world-open rule that CDK doesn't warn about.
- **Fix: pass `open: false` on every `addListener()` call** on an internet-facing ALB (HTTP listener, HTTPS listener, and the HTTP→HTTPS redirect listener alike — see `litellm-stack.ts:252,301,309` and `langfuse-stack.ts:151,171`). This tells CDK not to auto-manage listener-level ingress; your explicit `albIngressCidrs` rule remains the only one.
- **Verify after every deploy touching the ALB/listeners** — do not assume the flag alone proves it: `aws ec2 describe-security-groups --group-ids <PublicAlbSg-id>` and confirm the ingress rule list contains **only** the intended CIDR(s), with no stray `0.0.0.0/0` entry. This is a synth-time-invisible bug (no warning, no error) that only shows up by inspecting the deployed SG.


## Docker build architecture mismatch on x86 hosts (real-deploy incident — ECS tasks crash-loop)

`ecs.ContainerImage.fromAsset(...)` (LiteLLM, and Langfuse if it also builds from source) runs a
**local `docker build`/`buildx` on the machine executing `cdk deploy`**. CDK does not pin the build
platform by default — Docker builds for the **host's own architecture**. The Fargate task definitions
in this skill hard-code `runtimePlatform.cpuArchitecture: ecs.CpuArchitecture.ARM64` (Graviton, for
cost). On an **x86_64 Windows/Linux/Intel-Mac deploy host**, this mismatch is silent at build and synth
time — `docker build` succeeds, `cdk deploy` succeeds, the CloudFormation stack reaches
`UPDATE_COMPLETE` — and only then does ECS fail to start the task:

- **Symptom**: the LiteLLM (or Langfuse) service never reaches steady state. `aws ecs describe-services`
  shows the deployment stuck with `runningCount: 0` and repeated task stops; the circuit breaker
  eventually triggers an automatic **rollback** (Hard Constraint — see "Pinned-image CLI flags" above for
  the general diagnosis pattern). `aws ecs describe-tasks` on a stopped task shows
  `stoppedReason: "CannotPullContainerError"` or, if the pull succeeds, the container **exits immediately**
  with `exec /usr/local/bin/entrypoint.sh: exec format error` (or `standard_init_linux.go:...: exec user
  process caused: exec format error`) in the very first CloudWatch log line — because the kernel is being
  asked to run an amd64 binary on an ARM64 (Graviton) Fargate instance.
- **This is easy to misdiagnose as an application bug** because the error string mentions the entrypoint,
  not "architecture" or "platform" — check the **image manifest architecture** before debugging application
  logic: `docker inspect --format '{{.Architecture}}' <local-image>` (or, post-push,
  `aws ecr describe-images` / `docker buildx imagetools inspect <ecr-uri>:<tag>` shows per-platform
  manifests). If it says `amd64` while the task definition says `ARM64`, this is the root cause.
- **Fix — force the build platform to match `runtimePlatform` regardless of the deploy host's own
  architecture.** `ecs.ContainerImage.fromAsset()` accepts a `platform` option
  (`ecrAssets.Platform.LINUX_ARM64` / `ecrAssets.Platform.LINUX_AMD64`) that is passed through to Docker as
  `--platform`; set it explicitly instead of relying on the host default:
  ```typescript
  import { Platform } from 'aws-cdk-lib/aws-ecr-assets';

  taskDef.addContainer('litellm', {
    image: ecs.ContainerImage.fromAsset(
      path.join(__dirname, '..', 'services', 'litellm'),
      { platform: Platform.LINUX_ARM64 }, // MUST match runtimePlatform.cpuArchitecture above
    ),
    // ...
  });
  ```
  Apply the same `platform` option to every `ContainerImage.fromAsset()` call in the app (LiteLLM, and
  Langfuse's Dockerfile build if `enableLangfuse=true`) — a mismatch on either stack reproduces the same
  crash-loop independently.
- **Why this bites Windows x86 hosts specifically**: Docker Desktop on Windows/Intel builds natively for
  `linux/amd64` and does not cross-build to `arm64` unless QEMU emulation is available and the `platform`
  flag is passed. Without `platform` set in CDK, `docker build` silently produces an amd64 image, `docker
  push`/CDK's asset publishing silently accepts it (ECR does not validate architecture against the task
  definition), and the mismatch surfaces only at ECS task launch — many minutes and a full deploy cycle
  later. Apple Silicon (arm64) hosts do NOT hit this bug (host arch already matches Graviton), which is why
  it is easy to miss in local testing on an M-series Mac and only appears when a teammate deploys from an
  Intel/Windows machine or from x86_64 CI.
- **Cross-building ARM64 images from an x86 host requires QEMU emulation** (`docker buildx` sets this up
  automatically on Docker Desktop; on Linux, install `binfmt` support: `docker run --privileged --rm
  tonistiigi/binfmt --install arm64`). Expect the build step to be noticeably slower under emulation — this
  is expected, not a new failure.
- **Verify before considering the deploy done**, not just after `cdk deploy` returns 0:
  ```bash
  aws ecs describe-services --cluster <cluster> --services <service> \
    --query 'services[0].deployments[0].{running:runningCount,desired:desiredCount,rolloutState:rolloutState}'
  # rolloutState should reach COMPLETED with running == desired; if it is stuck at IN_PROGRESS
  # or the service rolled back, check CloudWatch Logs for exec format error before anything else.
  ```
- If switching `platform` requires a same-architecture alternative instead (e.g., a locked-down
  environment where ARM64 emulation is unavailable or too slow), the other valid fix is to set
  `runtimePlatform.cpuArchitecture: ecs.CpuArchitecture.X86_64` on the Fargate task definition to match the
  deploy host — but this gives up the Graviton cost/perf advantage documented elsewhere in this skill, so
  prefer fixing the build `platform` instead unless there is a specific reason to standardize on x86_64.

## CodeBuild image-build path (no local Docker) — API traps (real-deploy incidents)

When Docker can't run on the deploy machine at all (managed Windows laptop, WSL2/Hyper-V install forbidden),
the image builds in CodeBuild instead — `litellm.imageBuild.mode='codebuild'`, full pattern in
`cdk-stacks.md` §4-1. Three traps, all hit on the real deploy that produced the pattern:

- **`codebuild.ComputeType.ARM_CONTAINER` does not exist in aws-cdk-lib** — doc snippets/autocomplete
  suggest it, but `tsc` fails (property does not exist). ARM is selected by the **build image**
  (`codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0`) with `ComputeType.SMALL` +
  `privileged: true` (docker-in-docker). Do not emit `ARM_CONTAINER`.
- **`StartBuild` is not a CloudFormation resource** — `cdk deploy` of the build stack creates a project
  that has never run, and `cdk deploy --all` will NOT wait for (or trigger) an image build. The deploy is
  therefore an explicit **3-step order**: deploy `ImageBuildStack` → `aws codebuild start-build` + poll to
  `SUCCEEDED` + `aws ecr describe-images` to confirm the tag exists → deploy the remaining stacks.
  Skipping step 2 fails **late**: synth and deploy succeed, then ECS tasks die with
  `CannotPullContainerError` (same late-failure shape as the x86 architecture mismatch above).
- **Never tag the image `:latest` on this path** — `fromEcrRepository(repo, tag)` pins the tag into the
  task definition at synth; a rebuilt `:latest` produces no CloudFormation diff, so the service silently
  keeps running the stale image. Tag with the **content hash of `services/litellm/`** (the CDK source
  asset's `assetHash`) so a source change rolls the service by itself.
- cdk-nag fires **CB3** (privileged mode — required for `docker build`) and **CB4** (no customer-managed
  KMS key) on the project; both are suppressed inline in the stack with written reasons (§4-1).

## Data

- `removalPolicy: DESTROY` + deletion protection off is intentional for a tear-downable dev sample. **Production: `RETAIN` + backups + deletion protection.**
- Validate `data.engineVersion` against `aws rds describe-db-engine-versions --engine aurora-postgresql` in the **target region** before deploy — an invalid version fails DataStack create after a long wait.

## LiteLLM image + Mantle (GPT-5.x) Bearer-token auth

> ⚠️ **Correction (verified against the actual installed source of the pinned tag).** An earlier version of this skill claimed the `bedrock_mantle` Responses route supports SigV4/IAM auth (allegedly shipped in LiteLLM v1.87.2, PR #29788). That is **false**. Extracting `litellm/llms/bedrock_mantle/responses/transformation.py` from the pinned image (`docker run --entrypoint cat`) shows its `validate_environment()` has **no SigV4 code path at all** — it reads `BEDROCK_MANTLE_API_KEY` or `AWS_BEARER_TOKEN_BEDROCK`, sets `Authorization: Bearer <key>`, and raises `ValueError` if neither is set (regardless of model name). Calling GPT-5.x with SigV4 only reproduces 100%: `litellm.APIConnectionError: Bedrock Mantle API key is required.` The `get_provider_responses_api_config` "gate condition" the old text cited is not present in the function that actually authenticates. Do not reintroduce the SigV4 claim; if the pinned tag changes, re-verify by extracting the real source, not from release notes or issue numbers.

- **Mantle auth is a runtime-minted short-term Bedrock API key (Bearer), not a stored secret.** The image installs `aws-bedrock-token-generator`, and a LiteLLM callback (`callbacks/mantle_token_refresh.py`, a `CustomLogger` whose `async_pre_call_hook` runs before each request) **signs a fresh key on every request** from the ECS Task Role's own SigV4 credentials — pure local HMAC via a once-initialized botocore `RefreshableCredentials` handle + `BedrockTokenGenerator.get_token()`. No long-term IAM user, no static secret, no scheduler, **no token caching**. Claude (`bedrock/`) stays pure tokenless SigV4 — only Mantle needs this.
- **⚠️ Never cache the minted Mantle key on a timer (production incident).** The minted key is a SigV4-presigned artifact: its real lifetime is `min(requested expiry, remaining lifetime of the Task Role session that signed it)`, and Fargate Task Role sessions last ≤~6h. A version that cached the key against its requested 10h TTL broke in production at ~6h40m (`401 "The security token included in the request is expired"`). botocore rotates the session automatically, but a cached key signed with the old session does not follow — and **no fixed refresh interval can be correct**, because the callback cannot observe when the session rotates. Per-request signing closes this structurally (the key is consumed ms after signing). See `shared/patterns/litellm-gateway.md` §3.
- **⚠️ The env var MUST be `BEDROCK_MANTLE_API_KEY`, never `AWS_BEARER_TOKEN_BEDROCK`.** `validate_environment()` accepts either, but `AWS_BEARER_TOKEN_BEDROCK` is a **boto3-reserved name**: the moment it exists in the process env, *every* `bedrock-runtime` boto3 client in the same process — including Claude's SigV4 route — switches to Bearer auth and Claude breaks with `403 Authentication failed`. This exact mistake caused a **4-Claude-model production outage** (verified by reproduction: same value in `AWS_BEARER_TOKEN_BEDROCK` breaks Claude; in `BEDROCK_MANTLE_API_KEY` Claude is fine). `BEDROCK_MANTLE_API_KEY` is not a name boto3 recognizes, so it only satisfies Mantle's own fallback chain.
- **`get_secret_str()`/`os.getenv()` are re-read per request (no caching for plain env vars in this build)**, confirmed in `litellm/secret_managers/main.py` — so the callback updating `os.environ` takes effect immediately, no LiteLLM restart.
- **Dockerfile: the base image has no `pip`** (uv-managed venv, pip stripped). To add `aws-bedrock-token-generator`, copy the `uv` binary from `ghcr.io/astral-sh/uv:latest` in a separate stage and run `uv pip install --python /app/.venv/bin/python3 aws-bedrock-token-generator==1.1.0`. `pip install` in the base image fails with `No module named pip`.
- **Mantle IAM uses the `project` resource type, not `foundation-model`.** `bedrock-mantle:CreateInference`/`GetInference`/`GetProject`/`ListProjects` are grantable only on `arn:aws:bedrock-mantle:<region>:<account>:project/*` (per AWS's managed policy `AmazonBedrockMantleInferenceAccess`); a `foundation-model` ARN is rejected with AccessDenied on `CreateInference`. `bedrock-mantle:CallWithBearerToken` has no resource scoping (grant on `*`; it authenticates the Bearer token before project attribution).
- **New-principle — reserved env-var names.** Before setting any process-global env var that an SDK might special-case, confirm it is not reserved. When two names alias the same feature (e.g. an auth token), do not assume both are equally safe — one may be intercepted at the SDK layer. In a shared-process gateway (multiple providers, one boto3 session family), a var set for one provider can affect another. Regression-test the providers you did **not** change before shipping (Claude call → GPT call → Claude call again).
- **General model-ID principle — never assume a `us.` prefix.** Resolve each Claude model's actual inference-profile ID with `aws bedrock list-inference-profiles` in the target region before writing `constants.ts`. Recent (2026) models (Opus 4.8, Sonnet 5, Haiku 4.5, Fable 5) exist only as `global.` GLOBAL profiles — a `bedrock/us.anthropic.<id>` call returns `The provided model identifier is invalid.` A GLOBAL profile's IAM fan-out targets are the unqualified `arn:aws:bedrock:::foundation-model/<model>` (no region segment) **plus** the deploy-region foundation-model ARN — not `us-east-1`/`us-west-2`, which is where a `us.` cross-region profile would fan out.
- Building the image requires a **running Docker daemon** at `cdk deploy` time.

## Fable/Mythos-class models — `provider_data_share` data-retention opt-in

- Claude Fable 5 and Claude Mythos 5 are restricted to `allowed_modes: ["provider_data_share"]` (per their model cards + `bedrock/latest/userguide/data-retention.html`). If the account (or project) data-retention mode is `default` or `none`, the call is **blocked outright**.
- `provider_data_share` permits prompts/responses to be **retained by Anthropic for 30 days and subject to human safety review** — a policy decision that **must be surfaced at GATE 1 and explicitly approved by the account owner**, never assumed.
- No console UI — read/set it via the Bedrock control plane, **in this order** (do NOT jump straight to the SigV4 bypass below — on a later real deploy the plain CLI just worked, and the account was already opted in, so steps 1–2 were the whole job):
  1. **Read current mode first**: `aws bedrock get-account-data-retention --region <region>`. If it already reports `provider_data_share`, there is nothing to set in that region — stop here.
  2. **Set via the plain CLI**: `aws bedrock put-account-data-retention --data-retention-config '{"mode":"provider_data_share"}' --region <region>` (only after the GATE-1 approval above; verify the exact parameter shape with `aws bedrock put-account-data-retention help` — don't guess).
  3. **Only if the CLI lacks the subcommand** (`Invalid choice` — seen on older toolchains: CLI 2.27.x / boto3 1.42.x; newer CLIs have it) fall back to a raw SigV4-signed request — and note the path is **`/data-retention`**, NOT `/account-data-retention` (guessing the path from the API name `PutAccountDataRetention` returns `404 UnknownOperationException`; confirm the path in the official docs first):
  ```python
  # botocore SigV4-signed PUT (works even when the CLI/boto3 service model lacks the API)
  from botocore.auth import SigV4Auth
  from botocore.awsrequest import AWSRequest
  import boto3, json, urllib.request
  region = "<region>"
  creds = boto3.Session().get_credentials().get_frozen_credentials()
  req = AWSRequest(method="PUT", url=f"https://bedrock.{region}.amazonaws.com/data-retention",
                   data=json.dumps({"mode": "provider_data_share"}),
                   headers={"Content-Type": "application/json"})
  SigV4Auth(creds, "bedrock", region).add_auth(req)
  print(urllib.request.urlopen(urllib.request.Request(
      req.url, data=req.body.encode(), headers=dict(req.headers), method="PUT")).read())
  ```
- **Trap: this is per-region.** Setting it in `us-east-1` but invoking from `ap-northeast-2` still fails. Set it in **every region the model is invoked from** (the gateway region, and any other invocation region).

## Deploy targeting

- To deploy a subset (e.g., skip a stack), pass explicit stack names: `cdk deploy NetworkStack DataStack ...`. CDK respects dependency order with `--all`.
- IAM/security changes prompt approval; `--require-approval never` is acceptable for an explicitly requested deploy.

## Client onboarding (token helper)

- **Never hardcode the SigV4 region in `get-gateway-token.sh`.** The signing region must equal the Token Service API Gateway's region, which is already in the URL host (`{id}.execute-api.{region}.amazonaws.com`). Parse it from `TOKEN_SERVICE_URL` so the helper is deploy-region-agnostic. A hardcoded region (the original bug) breaks every deploy in a different region with `Credential should be scoped to a valid region` (HTTP 403 at API Gateway, before the Lambda runs).
- The empty POST body (`{}`) must be **byte-identical** between the signed payload and the sent payload — identity comes from the signed caller ARN, not the body.
- `claude-settings.json` / `codex-config.toml` carry **no secret** — only the helper path (`apiKeyHelper` / `auth.command`). `ANTHROPIC_BASE_URL` / `base_url` must be the **gateway URL** (the `GatewayUrl` output = the ALB domain: `https://<custom-domain>` for `acm`, `http://<alb-dns>` for `http` — reachable only from the `albIngressCidrs` allowlist) — never the raw internal ALB DNS for a public client.
- Quick single-operator test without SSO: use the LiteLLM master key directly as the Bearer against `<gateway-url>/v1` (the Token Service rejects non-`AWSReservedSSO_` callers by design).
- **macOS default `/bin/bash` is bash 3.2 (2007) — keep apostrophes out of `${VAR:?message}` strings (real-deploy incident).** An error message like `...outputs's host...` inside `: "${VAR:?...}"` makes bash 3.2 miscount quotes and die with `unexpected EOF while looking for matching quote` — even though bash 4/5 parse it fine and `#!/usr/bin/env bash` may still resolve to the system bash. Keep `:?` messages ASCII-plain with no apostrophes/quotes. Diagnosis tip: bisect the file with `head -N | bash -n` to find the offending line fast.
- **Stale local client config silently hijacks a new deploy (real-deploy incident).** `~/.llm-gateway/*` (env/config.json/token caches) may hold values from a **previous or different** deployment — e.g. an old Cognito `appClientId` makes the Hosted UI render a **blank page** (an invalid `client_id` shows no form and no useful error). After any redeploy: re-run `setup-developer.sh`/`llmgw-login` setup so the local files are rewritten from the new `outputs.json`, and delete stale token caches. Server-side check that beats browser debugging: `aws cognito-idp describe-user-pool-client --client-id <id>` (a stale id returns `ResourceNotFoundException`). DNS triage tip: if `curl` fails but `dig` resolves, suspect the calling process's resolver path, and isolate the server with `curl --resolve <host>:443:<ip>`.

## LiteLLM Admin UI + Langfuse redirects (PROXY_BASE_URL / NEXTAUTH_URL)

The UIs must not redirect the browser to an unreachable host. With CloudFront removed there is **no Location-rewrite CloudFront Function** — the ALB is the edge and the apps build their own absolute URLs:

1. **⚠️ Do NOT use `--forwarded-allow-ips` (real-deploy incident).** Earlier revisions of this skill told the entrypoint to run `litellm ... --forwarded-allow-ips '*'` so uvicorn would trust `X-Forwarded-Proto`/`X-Forwarded-Host`. **The pinned image's `litellm` CLI does not have that option** — the container exits instantly (`Error: No such option: --forwarded-allow-ips`, exitCode 2), the ECS circuit breaker retries 5x and rolls the deploy back. Verified against the actual image: `proxy_cli.py` constructs the uvicorn args explicitly and reads neither the flag nor a `FORWARDED_ALLOW_IPS` env var, so there is no uvicorn-level workaround. Redirect correctness comes from `PROXY_BASE_URL` instead.

2. **`PROXY_BASE_URL` (the SPA absolute base).** The LiteLLM UI is a SPA that builds absolute URLs/redirects from `PROXY_BASE_URL`. For `acm`, set it to the **gateway URL** the browser actually uses (`https://<custom-domain>` — known at synth, injected directly; no two-phase deploy). For `http` (no domain), the ALB DNS is **not** known when the container definition is synthesized (the task definition is created before the ALB in the same stack), so it stays **empty** — the `/ui` → `/ui/` 307 may then come back on the request host over `http://`, which is **cosmetic only** (the API and the UI both keep working on the http gateway URL). A wrong placeholder is worse than empty: the browser bounces to a dead host even though `curl /ui/` returns 200.

> Symptom signature: `curl` of `/ui/` returns 200, but a real browser still redirects to a weird host. That points at `PROXY_BASE_URL`, not any CloudFront layer. (And if the container never even starts — exitCode 2 within seconds — check the entrypoint for the nonexistent `--forwarded-allow-ips` flag first.)

3. **Langfuse uses `NEXTAUTH_URL` the same way.** Langfuse (NextAuth) builds absolute redirects from `NEXTAUTH_URL` — set it to the **Langfuse acm domain** (its own public ALB + ACM). Langfuse is deployed **only** when `certMode='acm'`, so a real domain always exists (no placeholder, no two-phase); `http` deploys don't run Langfuse at all.

## Onboarding writes to SHARED user config — merge, never overwrite (real-deploy incident)

`~/.claude/settings.json` and `~/.codex/config.toml` are the user's **personal, shared** config files — hooks, plugins, project-trust settings from other tools already live there. An earlier `setup-developer.sh` did `sed template > target` and **wiped all of it in one run** (recovered only via another tool's incidental backups). Generated onboarding scripts MUST:
1. **Back up first** — copy the current file to `*.llmgw-backup-<timestamp>` on every run.
2. **JSON: load → update only our keys → save** (`env.*`, `apiKeyHelper`, `permissions.deny` append) — never rewrite the document.
3. **TOML: replace only our `[model_providers.llm-gateway]`(+`.auth`) block**; upsert top-level keys only **in the top-level region** (before the first table — appending a bare key after a table silently re-scopes it) and keep a user's existing `model =` value.
4. Treat "the target file does not exist yet" as the special case, not the default assumption.

## Token Service first-issuance race (recovery bug)

On the very first key issuance, two near-simultaneous client calls (Claude Code/Codex fire the key helper more than once) can race: call A creates the virtual key (`/key/generate` 200) and caches it; call B then hits `/key/generate` 400 (`Key with alias 'sso-<user>' already exists`) and the reference recovery path queries `/user/info?user_id=<user>` which returns **404** (the user was never registered as a LiteLLM user, only as key metadata) → the Lambda returns 500. It **self-heals** once the cache is populated (subsequent calls hit DynamoDB), so it's a transient on first use. **Robust fix for generated code:** recover the existing key by **alias lookup** (`/key/info` / `/key/list` filtered by `key_alias`) instead of `/user/info`, and/or re-check the DynamoDB cache immediately before calling `/key/generate` to close the race window.


## Virtual-key lifetime ≠ SSO session (governance — keys must expire, offboarding must revoke)

- **SSO/Cognito expiry gates only key *issuance*, never key *use*.** An already-issued LiteLLM virtual key keeps authenticating requests after the SSO session expires — and even after the user is removed from IdC/Cognito entirely (real finding: a client with a cached key kept working across an expired SSO session; only *re-minting* failed).
- Therefore the Token Service **must pass `duration` on `/key/generate`** (`KEY_DURATION_SECONDS`, default 3600 = 1h, from `auth.keyDurationSeconds`). This bounds residual access after any revocation to at most the duration window; re-minting requires a live SSO/Cognito login. Never generate a token service without it — a duration-less key is **non-expiring**.
- **Duration sizing (real-deploy validated)**: short durations cost nothing UX-wise — helpers (`apiKeyHelper`/`auth.command`) re-mint silently while the session lives, so **login cadence is set by the IdP session (org-sso: IdC session, default 8h; cognito-native: refresh token, default 30d), not by key duration**. 1h is the recommended default. ⚠️ cognito-native: do NOT "align" duration to the 30-day refresh token — that widens the residual window; the tight coupling target is the 1h access token. Rotation UX observed live: first request after expiry may take ~1-2 min to converge (client-side helper caching/backoff); steady-state is a single 401 → instant recovery.
- **Three traps that come with key expiry** (all handled in `lambda-handlers.md` — do not regress; each caused or nearly caused a real outage):
  - *Cache outliving the key*: the DynamoDB cache TTL must stay below the key duration (`min(KEY_CACHE_TTL_SECONDS, KEY_DURATION_SECONDS - 300)`), or the Token Service serves an expired key from cache → 401 loop.
  - *Alias-collision recovery resurrecting a dead key*: an expired (or **near-expiry — 120s margin**) key row still occupies its `key_alias`, so `/key/generate` 400s; `_recover_existing_key` must check `expires`, delete the stale key, and let the caller re-create — recovering it verbatim hands the client a dead key.
  - *Recovery itself crashing (real ~30 min outage)*: the `user_id` contains `:` and `+` (`org-sso:<acct>:user+tag`) and **must be percent-encoded** in the `user/info` query — a raw `+` arrives as a space, LiteLLM 404s "user not found", and an unhandled HTTPError turns every issuance into a 500 until LiteLLM's periodic expired-key cleanup happens to free the alias. Also: **`/key/generate` does not create a user record**, so `user/info` can legitimately 404 even when the alias row exists — recovery must fall back to deleting the alias and re-creating (in practice this delete+recreate path IS the normal rotation on such deployments).
- **Offboarding is a two-step procedure, in this order**: ① revoke the key in LiteLLM (`/key/delete` by `key_aliases`, or `/key/block` for reversible suspension) — immediate cutoff; ② remove IdC assignment / disable the Cognito user — blocks re-issuance. IdP removal alone leaves access alive for up to the duration window (or forever on pre-duration deployments). Full procedure: `litellm-admin-guide.md` → "Offboarding".

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

**Endpoint-prefix trap (real verification stumble)**: LiteLLM **management** endpoints live at the
**root** — `POST <gateway-url>/key/generate`, `/team/new`, `/user/info`, `/key/delete` — with **no
`/v1`**. `POST /v1/key/generate` returns **404**. Only the OpenAI-compatible **inference** surface
(`/v1/chat/completions`, `/v1/responses`, `/v1/models`) is under `/v1`. When a verification step
mixes both (mint a key, then call the model with it), the prefix changes between the two calls.

A common failure: paths 1 and 2 pass but path 3 fails (clients silently get nothing) because of an
**SSO inline-policy region mismatch** (next gotcha). Always test path 3 with a real SSO user — do not
declare success from a master-key test alone.

## SSO permission set — decide in Discovery, then create (don't assume)

- **The permission set + group are a Discovery decision, not a default to silently reuse.** During
  Phase 1 the agent MUST ask: *create a new permission set for this gateway or reuse an existing one?
  what name? which group(s) or users to assign?* — and then create/assign per that answer. Do **not**
  pick a pre-existing permission set just because its name matches the config default (`LlmGatewayUser`);
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


## IAM Identity Center account instances → use `cognito-native` (NOT SAML federation)

- Account instances do **not** provide permission sets, account assignments, or `AWSReservedSSO_...` IAM roles. The org-sso SigV4 helper depends on those roles and therefore cannot work for account instances.
- **An account instance cannot host a SAML 2.0 customer-managed application** (AWS-confirmed: SAML customer-managed apps are an *organization-instance* capability; the account-instance "add application" flow offers only OAuth 2.0). Its OAuth 2.0 support is for **trusted identity propagation** — the inverse direction (an already-authenticated external app propagates identity *to* IdC), which cannot serve as a login/IdP. Therefore **Cognito↔IdC SAML federation is impossible on an account instance** — the earlier `account-sso` design that assumed it does not work and must not be generated.
- **Use `authMode="cognito-native"`**: an Amazon Cognito User Pool is the **sole** identity source — no external IdP, no IdC federation, **no Identity Store lookup** (`identitystore:*` is not granted). Teams are native Cognito **User Pool Groups**; the Token Lambda reads the `cognito:groups` claim from the API-Gateway-verified JWT. See `account-instance-setup.md`.
- `aws sso login` is not used in `cognito-native` at all; login is the Cognito Hosted UI via `llmgw-login`.
- **id_token vs access_token trap**: the API Gateway `COGNITO_USER_POOLS` authorizer accepts only `token_use=access`. Sending the id_token → 401, even though it also carries `cognito:groups`. The client helper must send the access token.
- Group name is a routing API. Use a prefix such as `llmgw-` and `multiGroupStrategy=require-single-team-group` to avoid ambiguous team assignment.

## Cognito Hosted UI domain prefix is GLOBALLY unique (real-deploy incident, misleading error)

- The Hosted UI `domainPrefix` is unique across **all AWS accounts worldwide**, not per account/region. A generic prefix like `llmgw-dev-auth` may already be taken by another AWS customer — AuthStack then fails to deploy.
- **The error message lies**: CloudFormation surfaces the collision as "domain ... **does not exist**" (it actually means AlreadyExists). Don't chase a missing-resource bug — check for a prefix collision first.
- Fix (generated by `cdk-stacks.md` AuthStack): suffix the account id — `` `${ns('auth')}-${this.account}` `` → `llmgw-dev-auth-<accountId>` — which restores global uniqueness deterministically. Changing the prefix after users exist is disruptive (login URL changes), so get it right at first deploy.

## Cross-platform client onboarding and Windows gotchas

- Generate a shared Python core (`gateway_auth.py`, subcommands `setup`/`login`/`token`/`healthcheck`/`mcp-headers`, **covering BOTH auth modes** — org-sso `token` is boto3 SigV4, imported lazily) and thin launchers (`llmgw-login.sh` / `.ps1`, `get-gateway-token.sh` / `.ps1`, `setup-developer.sh` / `.ps1`, `healthcheck.sh` / `.ps1`). **All onboarding merge/derivation logic lives in `gateway_auth.py setup` once** — never re-implement the settings.json/config.toml merge in shell or PowerShell; a second implementation is how the merge rules drift and the overwrite incident recurs on the OS nobody tested.
- **`org-sso` on Windows has no bash**: the §1 bash+here-doc helper is POSIX-only legacy. A Windows developer in an org-sso deployment uses `get-gateway-token.ps1` → `gateway_auth.py token` (built-in SigV4). Never present a `.sh` as the Windows path in either auth mode.
- **PowerShell launchers MUST end with `exit $LASTEXITCODE` (exit-code contract).** In Windows PowerShell 5.1, `$ErrorActionPreference='Stop'` does **not** propagate a native command's non-zero exit — without the explicit `exit`, a failed `python` still exits 0, Claude Code/Codex treat empty stdout as the key, and the developer sees unexplained 401s instead of the helper's stderr diagnostic.
- **Bare `python` is unreliable on Windows**: on a stock machine it resolves to the Microsoft Store *app-execution alias* stub (prints nothing, exit 9009). Launchers prefer the `py -3` launcher when present (`Get-Command py`); config values written by `setup` use the absolute `sys.executable` path instead of `python`. **Preferring `py -3` is necessary but NOT sufficient (real onboarding incident)**: on a clean developer box `py` can be entirely absent AND `python` the stub — a plain `if (Get-Command py) {...} else { & python ... }` fallback then runs the stub and the launcher "does nothing" (blank output, exit 9009), which reads as a broken script. Every `.ps1` launcher therefore runs a **Python-3 preflight**: probe `py -3` then `python` with `--version`, accept only exit 0 + a `Python 3.x` banner, else print an actionable install hint (`winget install Python.Python.3.12`) to **stderr** (stdout stays reserved for the token) and `exit 1`. Golden block: `developer-onboarding.md` §1A.
- **Launchers must resolve their own real path** so they run from any cwd (including a `~/.local/bin` symlink): bash uses a `readlink` loop over `$BASH_SOURCE` (`dirname "$0"` alone returns the symlink's dir, not the target); PowerShell uses `$PSScriptRoot` (more robust than `$MyInvocation.MyCommand.Path`; PS3+). Do not write launchers that assume the repo cwd (`REPO="$(cd "$(dirname "$0")/.." && pwd)"` breaks once symlinked).
- Avoid bash-only behavior: no required `sed`, `chmod`, POSIX paths, or here-docs in the Windows path. Prefer `pathlib`, `webbrowser`, `http.server`, and `urllib` in Python.
- **`os.chmod(0o600)` is a NO-OP on Windows** (it only maps to the read-only flag). Any "written 0600" guarantee (token caches, `admin-onboarding.html`) must be backed on Windows by `icacls <file> /inheritance:r /grant:r <user>:F` — `gateway_auth.py` does this best-effort in `_restrict_perms`, and `gen-onboarding.py` must do the same for the admin doc.
- **The PKCE loopback listener must LOOP until `/callback`** — a single `handle_request()` is a real bug: any stray hit (favicon.ico, a browser preconnect probe, another local process) consumes the one slot and the login fails with "state mismatch" even though the user did everything right. Answer 404 to non-`/callback` paths and keep listening until the deadline.
- Cognito loopback callback URIs must be explicitly allow-listed. Use `127.0.0.1` and `localhost` variants when supporting Windows developer desktops.
- Token cache files contain bearer/refresh material. Store them under the OS user config directory (`~/.llm-gateway` = `%USERPROFILE%\.llm-gateway` on Windows) and restrict file permissions (POSIX 0600 / Windows icacls); never print refresh tokens in diagnostics.
- **`healthcheck` must probe the full `gatewayUrl` (scheme included) from config** — hardcoding `https://` makes the probe permanently fail on a `certMode=http` PoC deploy.
- **AgentCore Web Search MCP is not auto-available to the client** just because LiteLLM registers it. The developer must `claude mcp add-json` pointing at `https://<gateway-url>/mcp/` (the `GatewayUrl` output = the ALB domain); use Claude Code's **`headersHelper`** → `gateway_auth.py mcp-headers` so the rotating virtual key is injected dynamically (a static `Authorization: Bearer sk-...` needs re-registration on every rotation). On Windows the `headersHelper` is the `sys.executable` + `%USERPROFILE%\.llm-gateway\gateway_auth.py mcp-headers` command (`setup` copies the core there).
- **Access vs refresh token lifetimes** confuse users: the Cognito access token expires in ~1h (auto-refreshed by the helper via the refresh token), while the refresh token (default 30 days, `cognitoNative.refreshTokenValidityDays`) is what determines when the developer must re-run `llmgw-login`. Spell both out in the onboarding guide.
- Claude Code / Codex Windows helper commands should use PowerShell launchers or the explicit `"<sys.executable>" "C:\Users\<user>\.llm-gateway\gateway_auth.py" token` command (written automatically by `setup`), not `.sh` scripts. The `~/.llm-gateway/gateway_auth.py` copy is **made by `setup`** — do not reference that path in docs unless setup ran.
- **Codex `auth.command` is spawned WITHOUT a shell (real Windows incident — 401 "No api key passed in").** Codex CreateProcess/exec's the value directly, so a joined `command = "\"python.exe\" \"gateway_auth.py\" token"` string is treated as ONE executable path → process creation fails (os error 123), no `Authorization` header is attached, every request 401s. `_merge_codex_config` must write **`command` (executable only) + `args` (array)** as separate TOML fields via `_helper_program_args()`. Claude Code's `apiKeyHelper` runs through a shell — the joined string stays correct there and must NOT be split.
- **Merging must also REMOVE direct-Bedrock remnants from `~/.claude/settings.json` (real Windows incident — 403).** Add-only merging leaves a prior direct-Bedrock setup fighting the gateway: `CLAUDE_CODE_USE_BEDROCK=1` bypasses `ANTHROPIC_BASE_URL` entirely, and a stale `AWS_BEARER_TOKEN_BEDROCK` yields 403 "API key is not valid". `_merge_claude_settings` pops both env keys before writing gateway values.
- **…and the TOP-LEVEL `"model"` field (real Windows incident — "team not allowed to access model").** A leftover top-level `model` (a raw Bedrock model ID, e.g. `us.anthropic.claude-sonnet-5...`) outranks the `ANTHROPIC_DEFAULT_*_MODEL` aliases and is sent verbatim to the gateway, which doesn't serve that ID. `_merge_claude_settings` pops top-level `model`; the gateway's model aliases are the contract.
- **Codex interactive sessions MUST have top-level `web_search = "disabled"` (real incident — misleading "high demand" loop).** Custom providers default the `web_search` capability ON; the interactive TUI then attaches a `web_search` tool that Bedrock Mantle rejects (`validation_error: "Live web access is not yet available"`) → LiteLLM 500 → Codex shows "Reconnecting... We're currently experiencing high demand" and gives up. ⚠️ Deceptive to debug: `codex exec` (non-interactive) does NOT attach the tool and works fine, and the same "high demand" symptom previously had a different cause (the GPT-5.6 `namespace` incident — since fixed server-side, verified 2026-07-19 on gpt-5.4/5.5). `gateway_auth.py setup` upserts the key; search flows through the AgentCore Web Search MCP instead.
- **Codex `/v1/models` parse noise is cosmetic (known, non-blocking).** Codex's model-catalog auto-refresh expects `{"models": [...]}`, LiteLLM answers the OpenAI-standard `{"data": [...]}` → a parsing-error log line on refresh. Chat/code requests (`/v1/responses`) are unaffected. Don't burn time on it; mention it in onboarding docs so developers don't misread the log.
- **Windows operator (deploy host)**: Phase 5's automatic onboarding run is `scripts\setup-developer.ps1` (or `python scripts\gateway_auth.py setup`) on a native-PowerShell operator machine — `setup-developer.sh` needs bash (WSL/Git Bash). The Docker ARM64 cross-build gotcha for x86 Windows hosts is covered above ("Docker build architecture mismatch").
