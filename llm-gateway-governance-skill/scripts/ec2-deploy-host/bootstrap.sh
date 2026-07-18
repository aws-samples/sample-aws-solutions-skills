#!/usr/bin/env bash
#
# bootstrap.sh — Install the llm-gateway-governance deploy toolchain on an EC2 host.
#
# Covers everything `shared/reference/prerequisites.md` §1 requires, on a fresh
# Amazon Linux 2023 or Ubuntu instance:
#   Docker Engine (enabled + started), Node.js 20, AWS CDK CLI, AWS CLI v2,
#   jq, git, tmux, Claude Code CLI (and optionally Codex CLI).
#
# Usage:
#   - As EC2 user-data (launch-ec2-host.sh passes it automatically), or
#   - Manually on an existing instance:  sudo bash bootstrap.sh
#
# Also wires Claude Code to Bedrock via the instance role (no API key):
#   writes ~/.claude/settings.json (CLAUDE_CODE_USE_BEDROCK=1 + region) and a
#   ~/start-llmgw.sh helper that clones the skill repo, links the skill, preflights
#   Bedrock access, and starts Claude Code inside tmux — one command after login.
#
# Env toggles (set before running / prepend in user-data; use `sudo -E` to pass them):
#   INSTALL_CODEX=1        also install @openai/codex (default: off)
#   SKIP_AI_TOOLS=1        skip Claude Code/Codex install + Bedrock wiring (default: off)
#   CLAUDE_MODEL=...       pin ANTHROPIC_MODEL (Bedrock inference-profile ID); default:
#                          unset — Claude Code picks its own Bedrock default
#
# Idempotent: safe to re-run.
set -euo pipefail

log() { echo "[bootstrap] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo bash bootstrap.sh)" >&2
  exit 1
fi

# ---- detect distro + default login user -------------------------------------
if command -v dnf >/dev/null 2>&1; then
  PKG=dnf
elif command -v apt-get >/dev/null 2>&1; then
  PKG=apt
else
  echo "Unsupported distro: need dnf (Amazon Linux 2023) or apt (Ubuntu)" >&2
  exit 1
fi

TARGET_USER=""
for u in ec2-user ubuntu; do
  if id "$u" >/dev/null 2>&1; then TARGET_USER="$u"; break; fi
done
[ -n "$TARGET_USER" ] || TARGET_USER="$(logname 2>/dev/null || echo root)"
log "distro=$PKG target_user=$TARGET_USER arch=$(uname -m)"

# ---- base packages -----------------------------------------------------------
if [ "$PKG" = dnf ]; then
  dnf install -y git jq tmux unzip tar gzip which >/dev/null
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null
  apt-get install -y git jq tmux unzip curl ca-certificates >/dev/null
fi
log "base packages installed (git, jq, tmux, unzip)"

# ---- Docker Engine -----------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  if [ "$PKG" = dnf ]; then
    dnf install -y docker >/dev/null
  else
    # Docker's own install script adds the official apt repo (distro docker.io is often stale)
    curl -fsSL https://get.docker.com | sh >/dev/null
  fi
fi
systemctl enable --now docker
usermod -aG docker "$TARGET_USER"
log "Docker installed and running; $TARGET_USER added to docker group (re-login required for non-sudo docker)"

# ---- QEMU/binfmt for x86_64 hosts (ARM64 cross-build) ------------------------
# The Fargate tasks are ARM64/Graviton. On a Graviton (aarch64) host the build is
# native and nothing is needed. On x86_64, install binfmt emulation once so
# `docker buildx build --platform linux/arm64` works (prerequisites.md §1 warning).
if [ "$(uname -m)" = "x86_64" ]; then
  log "x86_64 host detected — installing ARM64 binfmt emulation (prefer a Graviton instance to skip this)"
  docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
fi

# ---- Node.js 20 (NodeSource — same repo works for AL2023 rpm / Ubuntu deb) ----
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/^v\([0-9]*\).*/\1/')" -lt 18 ]; then
  if [ "$PKG" = dnf ]; then
    curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - >/dev/null
    dnf install -y nodejs >/dev/null
  else
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
    apt-get install -y nodejs >/dev/null
  fi
fi
log "Node: $(node -v)"

# ---- AWS CLI v2 (preinstalled on AL2023; install on Ubuntu) -------------------
if ! command -v aws >/dev/null 2>&1; then
  case "$(uname -m)" in
    aarch64) AWSCLI_ARCH=aarch64 ;;
    *)       AWSCLI_ARCH=x86_64 ;;
  esac
  tmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip" -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp"
  "$tmp/aws/install" >/dev/null
  rm -rf "$tmp"
fi
log "AWS CLI: $(aws --version 2>&1)"

# ---- AWS CDK CLI + AI coding tools -------------------------------------------
npm install -g aws-cdk >/dev/null 2>&1
log "CDK: $(cdk --version)"

if [ "${SKIP_AI_TOOLS:-0}" != "1" ]; then
  npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
  log "Claude Code: $(claude --version 2>/dev/null || echo installed)"
  if [ "${INSTALL_CODEX:-0}" = "1" ]; then
    npm install -g @openai/codex >/dev/null 2>&1
    log "Codex: $(codex --version 2>/dev/null || echo installed)"
  fi
fi

# ---- Claude Code ↔ Bedrock wiring (instance role is the credential — no API key) ----
if [ "${SKIP_AI_TOOLS:-0}" != "1" ]; then
  # Instance region via IMDSv2 (fall back to AWS_REGION for manual runs off-instance)
  REGION="${AWS_REGION:-}"
  if [ -z "$REGION" ]; then
    IMDS_TOKEN="$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' 2>/dev/null || true)"
    [ -n "$IMDS_TOKEN" ] && REGION="$(curl -sf -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
      http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)"
  fi

  USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  if [ -n "$REGION" ] && [ -n "$USER_HOME" ]; then
    mkdir -p "$USER_HOME/.claude"
    if [ ! -f "$USER_HOME/.claude/settings.json" ]; then
      if [ -n "${CLAUDE_MODEL:-}" ]; then
        jq -n --arg r "$REGION" --arg m "$CLAUDE_MODEL" \
          '{env: {CLAUDE_CODE_USE_BEDROCK: "1", AWS_REGION: $r, ANTHROPIC_MODEL: $m}}' \
          > "$USER_HOME/.claude/settings.json"
      else
        jq -n --arg r "$REGION" \
          '{env: {CLAUDE_CODE_USE_BEDROCK: "1", AWS_REGION: $r}}' \
          > "$USER_HOME/.claude/settings.json"
      fi
      log "Claude Code → Bedrock configured ($USER_HOME/.claude/settings.json, region $REGION)"
    else
      log "existing $USER_HOME/.claude/settings.json found — left untouched (set CLAUDE_CODE_USE_BEDROCK=1 there yourself)"
    fi

    # One-command entry point: clone skill repo → link skill → preflight → Claude Code in tmux
    cat > "$USER_HOME/start-llmgw.sh" <<'HELPER'
#!/usr/bin/env bash
# start-llmgw.sh — from a fresh login to the llm-gateway-governance skill running in Claude Code.
# Idempotent; re-run any time. Override the repo with LLMGW_REPO_URL=<git-url>.
set -euo pipefail

REPO_URL="${LLMGW_REPO_URL:-https://github.com/aws-samples/sample-aws-solutions-skills.git}"
REPO_DIR="$HOME/$(basename "$REPO_URL" .git)"
SKILL_SRC="$REPO_DIR/llm-gateway-governance-skill"

[ -d "$REPO_DIR/.git" ] || git clone "$REPO_URL" "$REPO_DIR"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$SKILL_SRC/claude-code/skills/llm-gateway-governance" "$HOME/.claude/skills/llm-gateway-governance"
ln -sfn "$SKILL_SRC/shared" "$SKILL_SRC/claude-code/skills/llm-gateway-governance/shared"

echo "── preflight ─────────────────────────────────────"
docker info >/dev/null 2>&1 && echo "✅ Docker" || { echo "❌ Docker not usable — run 'newgrp docker' or re-login"; exit 1; }
ARN="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)"
[ -n "$ARN" ] && echo "✅ AWS credentials: $ARN" || { echo "❌ no AWS credentials — instance profile missing?"; exit 1; }
REGION="$(jq -r '.env.AWS_REGION // empty' "$HOME/.claude/settings.json" 2>/dev/null || true)"
REGION="${REGION:-${AWS_REGION:-}}"
if [ -n "$REGION" ] && aws bedrock list-foundation-models --region "$REGION" --by-provider anthropic \
     --query 'modelSummaries[0].modelId' --output text >/dev/null 2>&1; then
  echo "✅ Bedrock API reachable in $REGION"
  echo "   (model ACCESS is a separate console toggle: Bedrock → Model access — enable Anthropic models)"
else
  echo "⚠️  cannot list Bedrock models in ${REGION:-<no region>} — check instance-role Bedrock policy / region"
fi
echo "──────────────────────────────────────────────────"

mkdir -p "$HOME/work"   # generated CDK project lands here
cd "$HOME/work"
# --dangerously-skip-permissions: this host is a disposable, SSM-only deploy box created
# for exactly this workload — skipping per-command prompts lets the multi-phase deploy
# (npm/cdk/docker/aws) run unattended. Set LLMGW_SAFE_MODE=1 to keep permission prompts.
if [ "${LLMGW_SAFE_MODE:-0}" = "1" ]; then
  exec tmux new-session -A -s llmgw claude
fi
exec tmux new-session -A -s llmgw "claude --dangerously-skip-permissions"
HELPER
    chmod +x "$USER_HOME/start-llmgw.sh"
    chown -R "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$USER_HOME/.claude" "$USER_HOME/start-llmgw.sh"
    log "wrote $USER_HOME/start-llmgw.sh"

    # Login hint
    if [ -d /etc/motd.d ]; then
      printf 'llm-gateway deploy host — run: sudo su - %s && ./start-llmgw.sh\n' "$TARGET_USER" > /etc/motd.d/30-llmgw
    elif ! grep -q start-llmgw /etc/motd 2>/dev/null; then
      printf 'llm-gateway deploy host — run: sudo su - %s && ./start-llmgw.sh\n' "$TARGET_USER" >> /etc/motd
    fi
  else
    log "skipping Bedrock wiring (no region detected and AWS_REGION unset)"
  fi
fi

# ---- final verify (mirrors prerequisites.md §1.2) -----------------------------
echo
echo "================ verify ================"
docker info    >/dev/null 2>&1 && echo "✅ Docker daemon running"      || echo "❌ Docker not running"
node -v        >/dev/null 2>&1 && echo "✅ Node: $(node -v)"           || echo "❌ Node.js missing"
cdk --version  >/dev/null 2>&1 && echo "✅ CDK: $(cdk --version)"      || echo "❌ AWS CDK CLI missing"
aws --version  >/dev/null 2>&1 && echo "✅ AWS CLI: $(aws --version)"  || echo "❌ AWS CLI missing"
jq --version   >/dev/null 2>&1 && echo "✅ jq: $(jq --version)"        || echo "⚠️  jq missing (optional)"
aws sts get-caller-identity --output text --query Arn 2>/dev/null \
  && echo "✅ instance credentials resolve (see ARN above)" \
  || echo "⚠️  no AWS credentials yet — attach an instance profile"
echo "========================================"
echo
log "done. Log out/in (or run 'newgrp docker') so '$TARGET_USER' can use docker without sudo."
log "Next: log in as $TARGET_USER and run ./start-llmgw.sh (clones the skill, preflights Bedrock, starts Claude Code in tmux)."
log "Reminder: enable Anthropic model access once in the Bedrock console (Model access) for this region."
