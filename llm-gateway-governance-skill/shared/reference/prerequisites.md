# Prerequisites — before running this skill

> ⚠️ **AI tool requirement (read first)**: this is an **Agent Skill**, not a standalone CLI — it runs
> inside a supported AI coding tool that can load `SKILL.md` and drive the multi-phase workflow. Install
> **one of the following**: **Kiro**, **Claude Code**, or **Codex**. Everything else in this document
> (Docker/Node/CDK/AWS CLI/IdC) is verified *from inside* one of these tools. Codex is supported both as
> a skill host and as a developer client of the deployed gateway (see
> `shared/patterns/developer-onboarding.md`).

Verify all of these **before** Phase 1 Discovery. Missing any of them blocks a specific
phase later (noted below) — catching them up front avoids a failed `cdk deploy` mid-way.

## 0. Supported AI tool (required — pick one)

| Tool | Install | Skill install path |
|---|---|---|
| **Kiro** | [kiro.dev](https://kiro.dev/) | `~/.kiro/skills/llm-gateway-governance/` |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` — [docs](https://docs.anthropic.com/en/docs/claude-code) | `~/.claude/skills/llm-gateway-governance/` |
| **Codex** | `npm install -g @openai/codex` — [docs](https://developers.openai.com/codex/) | `~/.agents/skills/llm-gateway-governance/` |

See the repo `README.md` → **Quickstart** section for the exact `ln -sf`/`cp -r` commands per tool. Codex may request approval for networked package installation and AWS deployment commands under its sandbox policy.

## 1. Local tooling

| Tool | Minimum version | Why | Verify |
|---|---|---|---|
| **Docker** (or Docker Desktop / Podman with Docker socket compat) | Any recent version, **daemon running** | `LiteLLMStack` builds the proxy image via CDK `fromAsset` (`services/litellm/Dockerfile`) at `cdk deploy` time — no daemon, no deploy | `docker info` succeeds |
| **Node.js** | 18.x or 20.x LTS | CDK app + `npm`/`npx` toolchain | `node -v` |
| **AWS CDK CLI** | v2 (`aws-cdk` npm package), matching the `aws-cdk-lib` pinned in `package.json` | `cdk synth`/`cdk deploy`/`cdk bootstrap` | `cdk --version` |
| **AWS CLI** | v2 | SSO login, CLI verification calls (`sso-admin`, `identitystore`, `ec2 describe-vpc-endpoint-services`, `rds describe-db-engine-versions`) used throughout Phase 1–5 | `aws --version` |
| **jq** (optional but recommended) | any | Parsing `outputs.json` / CLI JSON output when wiring onboarding scripts | `jq --version` |

> Docker is the most common first-run blocker: `cdk deploy` fails late (after synth succeeds) with a Docker
> daemon connection error if it isn't running. Start Docker before Phase 5, not after a failed deploy.

### 1.1 Install + verify, per OS

Pick your OS below. Run the **verify** command after install — all five must succeed before Phase 1.

#### macOS

| Tool | Install | Verify |
|---|---|---|
| **Docker Desktop** | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) (download `.dmg`), or `brew install --cask docker` (then launch it once from Applications so the daemon starts) | `docker info` (fails with "Cannot connect to the Docker daemon" if not running — open Docker Desktop and retry) |
| **Node.js** | `brew install node@20` (or `nvm install 20 && nvm use 20` if you use [nvm](https://github.com/nvm-sh/nvm)) | `node -v` → `v20.x.x` |
| **AWS CDK CLI** | `npm install -g aws-cdk` | `cdk --version` |
| **AWS CLI v2** | `curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o AWSCLIV2.pkg && sudo installer -pkg AWSCLIV2.pkg -target /` or `brew install awscli` | `aws --version` → `aws-cli/2.x.x` |
| **jq** | `brew install jq` | `jq --version` |

#### Linux (Ubuntu/Debian; adjust package manager for other distros)

| Tool | Install | Verify |
|---|---|---|
| **Docker Engine** | Follow [docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/) (adds Docker's apt repo — the distro's own `docker.io` package is often outdated). After install: `sudo usermod -aG docker $USER` then log out/in so `docker` works without `sudo`. Enable on boot: `sudo systemctl enable --now docker` | `docker info` |
| **Node.js** | Use [nvm](https://github.com/nvm-sh/nvm) (avoids stale distro packages): `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh \| bash`, restart shell, `nvm install 20` | `node -v` → `v20.x.x` |
| **AWS CDK CLI** | `npm install -g aws-cdk` | `cdk --version` |
| **AWS CLI v2** | `curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip && unzip awscliv2.zip && sudo ./aws/install` (use `-linux-aarch64.zip` on ARM) | `aws --version` |
| **jq** | `sudo apt-get update && sudo apt-get install -y jq` (or `sudo dnf install jq` / `sudo yum install jq`) | `jq --version` |

#### Windows

> Run CDK/AWS CLI from **WSL2** (Ubuntu) for the smoothest experience — the Linux steps above then apply
> as-is inside WSL. Native PowerShell works too; both paths are listed.

| Tool | Install | Verify |
|---|---|---|
| **WSL2** (recommended base) | `wsl --install` in an Administrator PowerShell, reboot, then follow the Linux table above inside the Ubuntu shell | `wsl --status` |
| **Docker Desktop** (native, required either way — provides the daemon WSL2 containers use) | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) → download the Windows installer. In Settings, enable **"Use the WSL 2 based engine"** and, under Resources → WSL Integration, enable your distro | `docker info` (from PowerShell **or** WSL — both should reach the same daemon) |
| **Node.js** (native PowerShell path) | [nodejs.org](https://nodejs.org/) LTS installer, or `winget install OpenJS.NodeJS.LTS` | `node -v` (PowerShell) |
| **AWS CDK CLI** | `npm install -g aws-cdk` (run in whichever shell — WSL or PowerShell — you'll run `cdk deploy` from) | `cdk --version` |
| **AWS CLI v2** (native PowerShell path) | [MSI installer](https://awscli.amazonaws.com/AWSCLIV2.msi), or `winget install Amazon.AWSCLI` | `aws --version` (PowerShell) |
| **jq** | `winget install jqlang.jq` (or use WSL's `apt-get install jq`) | `jq --version` |

> **Don't mix shells for one deploy.** Install Node/CDK/AWS CLI once **per shell you'll actually run `cdk
> deploy` from** (WSL *or* PowerShell) — `npm install -g` in WSL does not make `cdk` visible in PowerShell,
> and vice versa. Docker Desktop is the one exception that's shared across both once WSL integration is
> enabled.

### 1.2 One-shot verify (copy-paste, all OSes)

Run this after installing — any missing/failing line must be fixed before Phase 1 Discovery:

```bash
docker info    >/dev/null 2>&1 && echo "✅ Docker daemon running"      || echo "❌ Docker not running"
node -v        >/dev/null 2>&1 && echo "✅ Node: $(node -v)"          || echo "❌ Node.js missing"
cdk --version  >/dev/null 2>&1 && echo "✅ CDK: $(cdk --version)"     || echo "❌ AWS CDK CLI missing"
aws --version  >/dev/null 2>&1 && echo "✅ AWS CLI: $(aws --version)" || echo "❌ AWS CLI missing"
jq --version   >/dev/null 2>&1 && echo "✅ jq: $(jq --version)"       || echo "⚠️  jq missing (optional)"
```
(PowerShell: same commands work as-is since they're just CLI invocations, but swap `&&`/`||` for
`if ($?) { ... } else { ... }` per line, or simply run each command individually and read its output.)

## 2. AWS account access

| Requirement | Detail |
|---|---|
| **Target AWS account** | One account for the gateway (all platform stacks). AgentCore Web Search and Mantle pin to **us-east-1** regardless of the gateway region — the same account, a second region. (CloudFront/CDN is removed — the ALB is the edge with a regional ACM cert in `config.awsRegion`.) |
| **IAM permissions to deploy** | Broad enough to create the platform stacks: VPC/networking, Aurora Serverless v2, ECS Fargate + ALB, Secrets Manager, IAM roles/policies, API Gateway, Lambda, ACM (regional) (+ Route53 if `certMode='acm'` with a CDK-issued cert), `BedrockAgentCore::Gateway`/`GatewayTarget`, EC2 VPC peering, Route53 PHZ, `sso-admin`/`identitystore` (if driving SSO provisioning from the CLI). Administrator access on a **sandbox/dev** account is the simplest path; for a shared/prod account, scope a deploy role instead. |
| **CDK bootstrap** | Must be able to run `cdk bootstrap` in **both** the gateway region and us-east-1 (Hard Constraint #2). If either region was bootstrapped by another team/tool with a different qualifier, plan for a custom `--qualifier` — see `constraints.md` → Bootstrap. |
| **Bedrock model access** | Request/enable model access for the Claude models you intend to route (Bedrock console → **Model access**) in the **gateway region**, and for the GPT-5.x (Mantle) models in **us-east-1**. Mantle models are AWS Marketplace offerings — the *account* must be able to subscribe (first call auto-subscribes; see Phase 5 Mantle warm-up), so don't run this in an account with Marketplace purchasing restricted. |
| **Service quotas** | Default quotas are normally sufficient for a first deploy (VPCs, EIPs, Fargate tasks). If the account already runs other workloads, sanity-check VPC/EIP-per-region quotas before adding this stack's Network stack. |

## 3. Developer identity — `org-sso` (IdC organization instance) or `cognito-native`

This skill has two supported developer-auth modes. Choose the mode during Phase 1 Discovery based on whether an IdC **organization** instance is available:

| Environment | `authMode` | Developer login | Team mapping |
|---|---|---|---|
| **IdC organization instance** (management account owns IdC) | `org-sso` (default) | `aws sso login --profile <profile>` → permission-set AWS credentials → API Gateway SigV4 | Permission set name == LiteLLM `team_alias` |
| **IdC account instance, or no usable IdC** (e.g. partner is payer/owns the org IdC) | `cognito-native` | `llmgw-login` → **Cognito Hosted UI** (Cognito's own login, no IdP/IdC) → Cognito JWT | Cognito User Pool Group name == LiteLLM `team_alias` |

> ⚠️ There is **no viable IdC-federated account-instance mode**. An IdC account instance cannot host a SAML 2.0 customer-managed application (AWS-confirmed), so Cognito↔IdC SAML federation is impossible. Account instances → `cognito-native`, which uses Cognito as the sole identity source.

Before Phase 1 can finish, determine which mode applies:

- Run `aws sso-admin list-instances` and, when Organizations is available, compare `OwnerAccountId` with the management account. Management account owner ⇒ organization instance (`org-sso` possible). Otherwise (account instance, or empty result / no IdC) ⇒ `cognito-native`.

For `org-sso`, also confirm:

- **IdC is enabled** and the instance ARN is known; **identity source** is known (built-in IdC directory vs external IdP).
- At least one **IdC group or user** exists, or you have permission to create one.
- You can create or reuse a **permission set** and assign it to the target AWS account. Permission-set names must not contain `_`.
- You can attach an inline policy that allows only `execute-api:Invoke` on the deployed Token Service API ARN.

For `cognito-native`, also confirm:

- You can create an **Amazon Cognito User Pool + app client** (the AuthStack does this; the account just needs Cognito available). No IdC, no external IdP, and **no Identity Store** are required — Cognito is the sole identity source.
- You will manage users and **User Pool Groups** in the Cognito console (group name == team). Team groups should share a prefix such as `llmgw-` (the routing contract).
- Developers can complete a browser loopback OAuth flow (`127.0.0.1:8400`) for `llmgw-login`.

If neither path is available, flag it explicitly at **GATE 1** as a blocking prerequisite. Do not silently fall back to master-key-only developer access. Full setup detail: `shared/reference/sso-setup.md` (org-sso) and `shared/reference/account-instance-setup.md` (cognito-native).

## 4. `certMode`-specific prerequisites (edge TLS — CloudFront removed, the ALB is the edge)

The ALB is always the edge — **always internet-facing, always SG CIDR-restricted**. Each `litellm.certMode` has different prerequisites (this is orthogonal to `authMode`):

| `certMode` | Prerequisite |
|---|---|
| **`acm`** (recommended / PROD) | A domain — either an existing **regional** ACM cert ARN in `config.awsRegion`, **or** a Route53 public hosted zone in the **same account** (CDK DNS-issues the cert in `config.awsRegion` — **not** us-east-1 — and creates the A-record alias + HTTP→443 redirect). |
| **`http`** (no domain, PoC only) | No domain/cert, no tunnel, no plugin. Developers reach `http://<alb-dns>` directly. ⛔ the virtual key **and prompt/response bodies** are plaintext on the wire → the SG allowlist is the only access control (GATE-1 acknowledgement). |

**Both modes**: know the **source CIDRs** (office/NAT egress IPs) that should be allowed to reach the ALB — Discovery asks for them and they become the `litellm.albIngressCidrs` SG allowlist.

## 5. Optional: Langfuse tracing

Only relevant if you plan to answer "yes" to the Phase 1 Observability question.

- No extra external account/service is required — `LangfuseStack` self-hosts Langfuse on Aurora, wired
  through Secrets Manager. The only prerequisite is the same Docker/CDK toolchain above; there's no separate
  SaaS sign-up.

## Quick checklist (copy into your own notes)

- [ ] One of **Kiro / Claude Code / Codex** is installed and this skill is linked into its documented skills directory
- [ ] `docker info` succeeds (daemon running)
- [ ] `node -v` → 18.x/20.x, `cdk --version` → v2, `aws --version` → v2
- [ ] `aws sts get-caller-identity` resolves to the intended target account
- [ ] Deploy credentials can create VPC/ECS/RDS/Lambda/APIGW/ALB/ACM/IAM resources
- [ ] Bedrock model access enabled for target Claude models (gateway region) + GPT-5.x (us-east-1)
- [ ] Account can subscribe to AWS Marketplace (needed for Mantle's first-call auto-subscribe)
- [ ] IdC is enabled; identity source known; at least one group/user available to assign
- [ ] (`certMode=acm` only) domain ready — an existing regional ACM ARN, or a Route53 hosted zone (CDK issues the cert in `config.awsRegion`); (both modes) the source CIDRs for `litellm.albIngressCidrs` are known
- [ ] Can run `cdk bootstrap` in the gateway region **and** us-east-1
