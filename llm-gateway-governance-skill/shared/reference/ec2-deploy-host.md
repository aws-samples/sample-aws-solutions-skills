# EC2 deploy host — run this skill from an EC2 instance instead of a laptop

This guide sets up an **EC2 instance as the machine that runs the skill and `cdk deploy`**, replacing
the "Local tooling" host in `prerequisites.md` §1. Everything else in `prerequisites.md` (§2 account
access, §3 auth mode, §4 certMode) is unchanged — only *where the operator works* moves.

## TL;DR — CloudShell to a running skill in four commands

Open **AWS CloudShell** in the target account (CLI v2 + SSM plugin + your console credentials are
preinstalled) and run:

```bash
git clone https://github.com/aws-samples/sample-aws-solutions-skills.git
cd sample-aws-solutions-skills/llm-gateway-governance-skill/scripts/ec2-deploy-host
./launch-ec2-host.sh launch --region <gateway-region>       # ① create the host (~1 min + 3-5 min bootstrap)
aws ssm start-session --region <gateway-region> --target <instance-id-from-output>   # ② connect
```

then, inside the session:

```bash
sudo su - ec2-user
./start-llmgw.sh          # ③④ clone skill → link → preflight → Claude Code in tmux, Bedrock-authenticated
```

Claude Code runs against **Bedrock through the instance role** — no API key, no login. The one manual
step the scripts cannot do: enable **Anthropic model access** in the Bedrock console for the region
(Bedrock → Model access), which `start-llmgw.sh`'s preflight reminds you about. Details below.

## When to use an EC2 host (and when not to)

Use it when any of these apply:

| Situation | Why EC2 helps |
|---|---|
| Laptop can't run Docker (corporate policy, licensing, low disk/RAM) | Docker Engine runs on the instance |
| Deploying from Windows/Intel Mac/x86 CI | A **Graviton (ARM64) instance builds the LiteLLM image natively** — the entire QEMU cross-build failure class in `constraints.md` → "Docker build architecture mismatch on x86 hosts" disappears |
| Long `cdk deploy` (30-60 min first run) from an unstable network / laptop that sleeps | Run inside `tmux` on the instance; disconnect freely |
| No long-lived AWS keys allowed on laptops | The **instance profile role is the deploy credential** — no `aws configure`, no key files, no SSO-session expiry mid-deploy |
| Several operators share one deploy environment | One instance, one toolchain, SSM-audited access |

**Not a fit**: Kiro (an IDE, needs a desktop) — on a headless EC2 host use **Claude Code** or **Codex CLI**
as the skill host. Also remember the EC2 host is the *operator/deploy* machine only — developer onboarding
(`llmgw-login`, Claude Code/Codex client config) still happens on developer laptops as documented in
`shared/patterns/developer-onboarding.md` (see §7 below for the one verification-flow exception).

## What changes vs the local-machine flow

| `prerequisites.md` item | On the EC2 host |
|---|---|
| Docker Desktop + QEMU note | Docker Engine, native ARM64 on Graviton (bootstrap script handles both) |
| `aws configure sso` / access keys | **None** — instance profile provides credentials (`aws sts get-caller-identity` shows the role) |
| Per-OS install tables (§1.1) | One script: `scripts/ec2-deploy-host/bootstrap.sh` |
| AI tool login via local browser | **None by default** — bootstrap pre-wires Claude Code to Bedrock through the instance role (no API key, nothing expires); alternatives in §6 |
| Browser loopback flows (`llmgw-login` on `127.0.0.1:8400`) | SSM port forwarding when you must run one *from the host* — see §7 |

Everything in `prerequisites.md` §2-§5 and the SKILL.md phases applies verbatim once you're on the instance.

## 1. Instance spec

| Item | Recommendation | Why |
|---|---|---|
| **AMI** | Amazon Linux 2023 (SSM agent preinstalled) | `bootstrap.sh` supports AL2023 and Ubuntu; AL2023 is the default in the launch script |
| **Architecture** | **ARM64 / Graviton** — `t4g.xlarge` (default) or `m7g.large`+; `t4g.large` (8 GiB) is the floor | Native ARM64 Docker build matches the Fargate tasks; no QEMU. Docker build + `npm ci` + CDK synth want ≥8 GiB RAM |
| **Root volume** | 50 GB gp3 (default AMI root is 8 GB — too small) | Docker layers + node_modules + CDK assets |
| **Network** | Any subnet with **outbound** internet (default-VPC public subnet, or private + NAT) | npm/Docker Hub/AWS API egress; the gateway's own VPC is created by CDK later and is unrelated |
| **Security group** | **No inbound rules at all** | Access is SSM Session Manager only — no SSH port, no key pair |
| **IMDS** | IMDSv2 required (`HttpTokens=required`) | Credential-hardening baseline |
| **IAM instance profile** | `AmazonSSMManagedInstanceCore` + deploy permissions per `prerequisites.md` §2 (sandbox: `AdministratorAccess` is simplest; shared account: a scoped deploy role) + a `bedrock-invoke` inline policy (the launch script always attaches it) | The role **is** the deploy credential *and* Claude Code's Bedrock credential (§6, option A) — no API key on the host |

## 2. Launch — scripted (run from CloudShell)

**AWS CloudShell is the recommended place to run this** — it already has AWS CLI v2, the Session
Manager plugin, and the credentials of whoever is signed in to the console; nothing to install.
Any machine with equivalent CLI credentials (able to create EC2 + IAM resources) works the same way.

```bash
git clone https://github.com/aws-samples/sample-aws-solutions-skills.git
cd sample-aws-solutions-skills/llm-gateway-governance-skill/scripts/ec2-deploy-host
./launch-ec2-host.sh launch --region ap-northeast-2            # defaults: t4g.xlarge, 50GB, default VPC
# options: --instance-type m7g.large  --subnet-id subnet-xxx  --volume-gb 80
#          --policy-arn <scoped-deploy-policy>   (instead of the AdministratorAccess default)
#          --instance-profile <existing-name>    (reuse a profile your team already manages)
./launch-ec2-host.sh status    --region ap-northeast-2
./launch-ec2-host.sh terminate --region ap-northeast-2 --purge-iam   # full cleanup
```

The script creates the IAM role/instance profile (deploy policy + `AmazonSSMManagedInstanceCore` +
a `bedrock-invoke` inline policy for Claude Code) + no-inbound SG + instance, and passes
`bootstrap.sh` as user-data, so the toolchain installs itself on first boot (~3-5 min).

**Console/manual alternative**: launch AL2023 ARM64 with the §1 spec, then run the bootstrap by hand
(§3). Nothing in the flow depends on the launch script.

> The launch region only determines where the *host* lives. `cdk deploy` targets whatever
> `config.awsRegion` says — same-region is marginally faster for asset uploads but not required.

## 3. Bootstrap the toolchain

Skip if the instance was launched with the script (user-data already ran it — check
`sudo tail /var/log/cloud-init-output.log` for the `================ verify ================` block).
On an existing instance:

```bash
# copy bootstrap.sh to the host (see §5 for repo transfer), then:
sudo bash bootstrap.sh                 # INSTALL_CODEX=1 sudo -E bash bootstrap.sh  → also installs Codex CLI
newgrp docker                          # or log out/in, so docker works without sudo
```

It installs Docker Engine (enabled+started), Node 20, AWS CDK CLI, AWS CLI v2, jq/git/tmux, and Claude
Code, then prints the same verify block as `prerequisites.md` §1.2. On an x86_64 host it also installs
ARM64 binfmt emulation automatically. It additionally:

- writes `~/.claude/settings.json` with `CLAUDE_CODE_USE_BEDROCK=1` + the instance's region — Claude
  Code authenticates to **Bedrock via the instance role**, no API key, no login — and
  `permissions.defaultMode: "bypassPermissions"` (an existing `settings.json` is never overwritten).
  Pin a model with `CLAUDE_MODEL=<bedrock-inference-profile-id> sudo -E bash bootstrap.sh`; by
  default Claude Code picks its own Bedrock default model.
- writes `~/start-llmgw.sh` (see §5) and a login hint in the message-of-the-day.

## 4. Connect — SSM only

```bash
aws ssm start-session --region <region> --target <instance-id>
sudo su - ec2-user        # work as ec2-user, not ssm-user (docker group, home dir)
```

Requires the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
on your laptop. No SSH key pair or open port exists on this host by design.

**Optional — VS Code / SSH over SSM**: if you want an editor attached to the generated project, add an
SSH key and a `ProxyCommand`-based `~/.ssh/config` entry (`aws ssm start-session --document-name
AWS-StartSSHSession`), or use CloudShell/plain terminal editing. This is comfort, not a requirement —
the skill host (Claude Code/Codex) does the editing.

## 5. Start everything with one command — `start-llmgw.sh`

After connecting (§4) and switching to `ec2-user`:

```bash
./start-llmgw.sh
# private fork instead of the public sample repo:
#   LLMGW_REPO_URL=<git-url> ./start-llmgw.sh
```

Idempotent — it (1) clones the skill repo if absent, (2) symlinks the skill into
`~/.claude/skills/`, (3) preflights Docker / instance-role credentials / Bedrock API reachability,
then (4) starts **Claude Code inside tmux session `llmgw`** with `~/work` as the working directory
(the generated CDK project lands there). If the session already exists it re-attaches, so re-running
after an SSM disconnect drops you back into the live deploy. Detach with `Ctrl-b d`.

Claude Code starts in **bypass-permissions mode**, set declaratively in `~/.claude/settings.json`
(`permissions.defaultMode: "bypassPermissions"`, written by bootstrap — no CLI flag): this host is a
disposable, single-purpose, SSM-only deploy box, and skipping per-command approval lets the long
multi-phase deploy (npm/cdk/docker/aws calls) run unattended. On the very first interactive session
Claude Code shows a one-time acknowledgement dialog for this mode — accept it once. If you'd rather
approve each command — e.g. on a shared or long-lived host — start with
`LLMGW_SAFE_MODE=1 ./start-llmgw.sh` (passes `--permission-mode default`, which overrides the
settings file for that run). Note bypass mode applies to *every* `claude` session on this host, not
just ones started by the helper.

Manual fallback (no helper): the repo `README.md` → Install has the `git clone` + `ln -sf` commands;
run `claude` inside your own `tmux` session. (Repo with no reachable remote? `aws s3 cp` a tarball
through a bucket, or `rsync` over the SSH-over-SSM setup from §4.)

## 6. AI-tool authentication — Bedrock by default, alternatives if needed

| Option | How | Notes |
|---|---|---|
| **A. Claude Code via Bedrock** — **pre-configured; do nothing** | `bootstrap.sh` already wrote `~/.claude/settings.json` (`CLAUDE_CODE_USE_BEDROCK=1` + region); credentials come from the instance profile's `bedrock-invoke` policy | No secret on the host, nothing expires. Two account-side prerequisites: **Anthropic model access enabled** in the Bedrock console for the region (`prerequisites.md` §2 — the preflight reminds you), and, deliberately, **no Bedrock API key**: a short-term key expires in ≤12 h and a long-term key requires creating an IAM user — both strictly worse than the role on EC2 |
| **B. API key** (only if A is blocked, e.g. no Bedrock in region) | `export ANTHROPIC_API_KEY=...` (Claude Code) / `codex login --api-key` (Codex) — remove the `env` block from `~/.claude/settings.json` first | Treat the key like any secret on a shared host |
| **C. Browser-handoff OAuth** (subscription accounts) | Remove the `env` block from `~/.claude/settings.json`, run `claude` → it prints a login URL → open it in your **laptop** browser → paste the code back into the SSM session | No key on the host; Pro/Max accounts work |

## 7. Run the skill

`start-llmgw.sh` leaves you inside Claude Code in tmux — start with a trigger phrase from the repo
README (e.g. *"Build an LLM gateway on AWS ..."*). Phases 1-5 then proceed exactly as SKILL.md
documents; tmux keeps the 30-60 min `cdk deploy` alive across SSM disconnects
(`tmux attach -t llmgw`, or just re-run `./start-llmgw.sh`). EC2-specific notes:

- **Docker check (Phase 0/5)**: `docker info` must pass *without sudo* — if it doesn't, you skipped
  the `newgrp docker`/re-login step in §3.
- **`org-sso` CLI verification steps**: any step the docs show as `aws sso login --profile ...` can run
  headless with `aws sso login --no-browser` (device-code flow — open the printed URL on your laptop).
  The *deploy* itself never needs this; the instance profile covers it.
- **`cognito-native` full-path verification (Phase 5)**: `llmgw-login` opens a browser and listens on
  `127.0.0.1:8400`. Two ways:
  - *Preferred*: run the full-path verification from a **developer laptop** (it exercises the real
    onboarding path anyway).
  - *From the host*: SSM port-forward first, then run `llmgw-login` on the host and open the printed
    URL in the laptop browser — the loopback redirect lands on the laptop's `127.0.0.1:8400` and is
    forwarded to the host:
    ```bash
    aws ssm start-session --region <region> --target <instance-id> \
      --document-name AWS-StartPortForwardingSession \
      --parameters '{"portNumber":["8400"],"localPortNumber":["8400"]}'
    ```
- **ALB reachability tests**: the host's egress IP is the instance's public IP (or the subnet's NAT IP)
  — if you `curl` the gateway from the host, that IP must be in `litellm.albIngressCidrs`, or the test
  fails by design. Query it with `curl -s https://checkip.amazonaws.com`.

## 8. Cost & lifecycle

- `t4g.xlarge` is ≈ $0.13-0.17/hr on-demand (region-dependent) + 50 GB gp3 ≈ $4/mo. **Stop the
  instance between work sessions** (`aws ec2 stop-instances`) — toolchain and repo survive stop/start;
  only the deploy session state (tmux) does not.
- When the gateway is deployed and stable, the host is disposable: `./launch-ec2-host.sh terminate
  --purge-iam`. Re-running the launch script later recreates an identical host in minutes. The deployed
  gateway stacks are entirely independent of this instance.

## Quick checklist (EC2 delta only — then continue with `prerequisites.md`)

- [ ] Graviton instance, ≥8 GiB RAM, 50 GB gp3, IMDSv2, **no inbound SG rules**
- [ ] Instance profile: `AmazonSSMManagedInstanceCore` + deploy permissions + `bedrock-invoke` (launch script attaches all three)
- [ ] `bootstrap.sh` verify block all ✅ (`/var/log/cloud-init-output.log`)
- [ ] **Anthropic model access enabled** in the Bedrock console for the gateway region (the one manual step)
- [ ] `./start-llmgw.sh` preflight all ✅ — Docker without sudo, instance-role ARN in the intended account, Bedrock reachable
- [ ] You are inside tmux session `llmgw` before starting the Phase 5 deploy (automatic via `start-llmgw.sh`)
