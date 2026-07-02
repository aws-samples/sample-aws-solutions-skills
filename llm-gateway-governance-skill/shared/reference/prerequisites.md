# Prerequisites — before running this skill

> ⚠️ **AI tool requirement (read first)**: this is an **Agent Skill**, not a standalone CLI — it only runs
> inside a supported AI coding tool that can load `SKILL.md` and drive the multi-phase workflow. You need
> **one of the following installed**: **Kiro**, **Claude Code**, or **Amazon Quick Desktop**. Everything
> else in this document (Docker/Node/CDK/AWS CLI/IdC) is verified *from inside* one of these tools, not
> standalone. **Codex support is planned but not yet available** — do not expect this skill to run under
> Codex today.

Verify all of these **before** Phase 1 Discovery. Missing any of them blocks a specific
phase later (noted below) — catching them up front avoids a failed `cdk deploy` mid-way.

## 0. Supported AI tool (required — pick one)

| Tool | Install | Skill install path |
|---|---|---|
| **Kiro** | [kiro.dev](https://kiro.dev/) | `~/.kiro/skills/llm-gateway-governance/` |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` — [docs](https://docs.anthropic.com/en/docs/claude-code) | `~/.claude/skills/llm-gateway-governance/` |
| **Amazon Quick Desktop** | [aws.amazon.com/ko/quick/desktop](https://aws.amazon.com/ko/quick/desktop/) | `~/.quickwork/skills/llm-gateway-governance/` |
| Codex | — | **Not supported yet** — planned for a future update |

See the repo `README.md` → **Install** section for the exact `ln -sf`/`cp -r` commands per tool.

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
| **Target AWS account** | One account for the gateway (all platform stacks). AgentCore Web Search, Mantle, and CDN(with custom domain) pin to **us-east-1** regardless of the gateway region — the same account, a second region. |
| **IAM permissions to deploy** | Broad enough to create the 11-stack app: VPC/networking, Aurora Serverless v2, ECS Fargate + ALB, Secrets Manager, IAM roles/policies, API Gateway, Lambda, CloudFront, Route53 (if custom domain), `BedrockAgentCore::Gateway`/`GatewayTarget`, EC2 VPC peering, Route53 PHZ, `sso-admin`/`identitystore` (if driving SSO provisioning from the CLI). Administrator access on a **sandbox/dev** account is the simplest path; for a shared/prod account, scope a deploy role instead. |
| **CDK bootstrap** | Must be able to run `cdk bootstrap` in **both** the gateway region and us-east-1 (Hard Constraint #2). If either region was bootstrapped by another team/tool with a different qualifier, plan for a custom `--qualifier` — see `constraints.md` → Bootstrap. |
| **Bedrock model access** | Request/enable model access for the Claude models you intend to route (Bedrock console → **Model access**) in the **gateway region**, and for the GPT-5.x (Mantle) models in **us-east-1**. Mantle models are AWS Marketplace offerings — the *account* must be able to subscribe (first call auto-subscribes; see Phase 5 Mantle warm-up), so don't run this in an account with Marketplace purchasing restricted. |
| **Service quotas** | Default quotas are normally sufficient for a first deploy (VPCs, EIPs, Fargate tasks). If the account already runs other workloads, sanity-check VPC/EIP-per-region quotas before adding this stack's Network stack. |

## 3. IAM Identity Center (SSO) — required for the SSO path

The Token Service **only** accepts IAM Identity Center principals; there is no fallback auth mode for
developer traffic. Before Phase 1 SSO discovery questions can be answered, confirm:

- **IdC is enabled** in the account or org (org management account or a delegated admin account). Enabling
  IdC for the first time is an org-level action or a Console permissions.
- **Identity source** is known: built-in IdC directory (default) vs. external IdP (Okta / Entra ID / Google) —
  this determines whether users/passwords are managed in IdC or externally.
- At least one **IdC group or user** exists (or you have permission to create one) to assign the gateway's
  permission set to.

If IdC is not yet enabled, flag it explicitly at **GATE 1** as a blocking prerequisite — do not attempt to
work around it, the gateway design has no non-SSO path by intent (Hard Constraint #9). Full setup detail:
`shared/reference/sso-setup.md`.

## 4. Custom domain (only if `useCustomDomain=true`)

Skip this section entirely for domain-less mode (default `*.cloudfront.net` — no prerequisites beyond §1–3).

| Requirement | Detail |
|---|---|
| **Route53 hosted zone** | A public hosted zone for the domain you'll front with CloudFront, in the **same account** (or delegated so CDK can create validation records). |
| **ACM certificate region** | Must be requested/validated in **us-east-1** — CloudFront only accepts viewer certs from us-east-1, regardless of the gateway region. |

## 5. Optional: Langfuse tracing

Only relevant if you plan to answer "yes" to the Phase 1 Observability question.

- No extra external account/service is required — `LangfuseStack` self-hosts Langfuse on Aurora, wired
  through Secrets Manager. The only prerequisite is the same Docker/CDK toolchain above; there's no separate
  SaaS sign-up.

## Quick checklist (copy into your own notes)

- [ ] One of **Kiro / Claude Code / Amazon Quick Desktop** is installed and this skill is linked into it (Codex: not yet supported)
- [ ] `docker info` succeeds (daemon running)
- [ ] `node -v` → 18.x/20.x, `cdk --version` → v2, `aws --version` → v2
- [ ] `aws sts get-caller-identity` resolves to the intended target account
- [ ] Deploy credentials can create VPC/ECS/RDS/Lambda/APIGW/CloudFront/IAM resources
- [ ] Bedrock model access enabled for target Claude models (gateway region) + GPT-5.x (us-east-1)
- [ ] Account can subscribe to AWS Marketplace (needed for Mantle's first-call auto-subscribe)
- [ ] IdC is enabled; identity source known; at least one group/user available to assign
- [ ] (custom domain only) Route53 hosted zone ready; will request ACM cert in us-east-1
- [ ] Can run `cdk bootstrap` in the gateway region **and** us-east-1
