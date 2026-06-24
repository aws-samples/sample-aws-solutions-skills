# Pattern — Developer Onboarding (SSO → key helper → virtual key)

A pattern for regenerating the **client-side onboarding** that connects a developer's Claude Code / Codex to the internal
LLM gateway. What this pattern covers is **3 shell scripts + 2 client templates**, all real code taken verbatim from the
`scripts/`·`templates/` of the `llm-gateway-multi-agent` reference.

Core flow:

```
aws sso login --profile llm-gateway
   → the client (Claude Code / Codex) runs the key helper on every request
   → get-gateway-token.sh : calls API Gateway (/auth/token) with boto3 SigV4
   → Token Lambda verifies the SSO identity and returns a LiteLLM virtual key (sk-...)
   → the client uses that virtual key as a Bearer token against the CloudFront domain
```

> **WHY (overall design intent)**: developers are never issued/distributed/revoked long-lived API keys. Only while the SSO
> session is alive does the key helper dynamically fetch a virtual key, and the key is cached behind the Token Service (DynamoDB).
> The client config files contain **no secrets at all** — only the script path that `apiKeyHelper` (Claude) / `auth.command` (Codex)
> points to.

---

## Cross-layer mapping (the stacks this pattern touches)

| Client-side element | Call target | Server-side stack |
|---|---|---|
| `get-gateway-token.sh` (SigV4) | API Gateway `/auth/token` (AWS_IAM auth) | **AuthStack** |
| `claude-settings.json` → `ANTHROPIC_BASE_URL` | CloudFront domain → internal ALB | **CdnStack → LiteLLMStack** |
| `codex-config.toml` → `base_url` `/v1` `wire_api=responses` | same (CloudFront) | **CdnStack → LiteLLMStack** |
| returned virtual key (`sk-...`) | LiteLLM `/v1/messages`·`/v1/responses` | **LiteLLMStack** |
| key issuance/cache | DynamoDB key cache | **AuthStack (Token Lambda + DDB)** |

The two values passed to `setup-developer.sh` come from the deployment outputs (`cdk deploy --outputs-file`):
- `ALB_DNS` = the **CloudFront public domain** (e.g. `llmlite.example.com` or `dxxxx.cloudfront.net`) — not the internal ALB DNS.
- `TOKEN_SERVICE_URL` = AuthStack's API Gateway invoke URL.

---

## 1. `scripts/get-gateway-token.sh` — shared key helper (SigV4 point)

The script that Claude Code's `apiKeyHelper` and Codex's `auth.command` run **identically**.
The contract is strict: **stdout is exactly one line with the token**, diagnostics go to stderr, non-zero exit on failure.

> **WHY boto3 + Python here-doc?** Building a SigV4 signature in pure shell is fragile (canonical request,
> header ordering, payload hash). Delegating to `botocore.auth.SigV4Auth` makes API Gateway IAM auth reliable.
>
> **The region is never hard-coded.** The SigV4 signing region must always match the region of the API Gateway being called,
> and that region is already embedded in the Token Service URL host:
> `{api-id}.execute-api.{region}.amazonaws.com`. So we **parse the region from the URL** — it works regardless of which region
> the gateway is deployed to, with no script edit. (Force it with `AWS_SIGV4_REGION` only when unavoidable.)
> Pinning `AWS_REGION=us-east-2` as in the past causes a `Credential should be scoped to a valid region` 403 on deployments in
> other regions — this pattern structurally eliminates that bug.

```bash
#!/usr/bin/env bash
# get-gateway-token.sh — shared key helper for Claude Code and Codex.
# Uses Python + boto3 SigV4 for reliable API Gateway IAM auth.
#
# Region handling (IMPORTANT): the SigV4 signing region MUST match the region of
# the API Gateway being called. That region is already embedded in the Token
# Service URL host: `{api-id}.execute-api.{region}.amazonaws.com`. We therefore
# DERIVE the region from TOKEN_SERVICE_URL — no hardcoded region, works in any
# region the gateway is deployed to. (Override with AWS_SIGV4_REGION only if you
# really must.)
#
# Contract: stdout = token (1 line only), stderr = diagnostics, non-zero exit on failure.
set -euo pipefail

export TOKEN_SERVICE_URL="${TOKEN_SERVICE_URL:-https://your-api-id.execute-api.us-east-1.amazonaws.com/v1/auth/token}"
export AWS_PROFILE="${AWS_PROFILE:-llm-gateway}"
export AWS_SIGV4_REGION="${AWS_SIGV4_REGION:-}"

python3 << 'PYTHON'
import json, sys, os, urllib.request, urllib.error
from urllib.parse import urlparse

try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError:
    print('ERROR: boto3 required. pip install boto3', file=sys.stderr)
    sys.exit(1)

url = os.environ['TOKEN_SERVICE_URL']

# Derive the signing region from the execute-api host:
#   {api-id}.execute-api.{region}.amazonaws.com  ->  {region}
def region_from_url(u):
    parts = (urlparse(u).hostname or '').split('.')
    if 'execute-api' in parts:
        i = parts.index('execute-api')
        if i + 1 < len(parts):
            return parts[i + 1]
    return None

region = (
    os.environ.get('AWS_SIGV4_REGION')        # explicit override (rare)
    or region_from_url(url)                    # authoritative: must match the API's region
    or os.environ.get('AWS_REGION')
    or os.environ.get('AWS_DEFAULT_REGION')
)
if not region:
    print('ERROR: could not determine region from TOKEN_SERVICE_URL=' + url, file=sys.stderr)
    sys.exit(1)

session = boto3.Session(region_name=region)
creds = session.get_credentials()
if not creds:
    print('ERROR: no credentials. Run: aws sso login --profile ' + os.environ.get('AWS_PROFILE','llm-gateway'), file=sys.stderr)
    sys.exit(1)

frozen = creds.get_frozen_credentials()
body = json.dumps({})
req = AWSRequest(method='POST', url=url, data=body, headers={'Content-Type':'application/json'})
SigV4Auth(frozen, 'execute-api', region).add_auth(req)   # ← SigV4 core: service='execute-api', region=parsed from URL

try:
    http_req = urllib.request.Request(url, data=body.encode(), headers=dict(req.headers), method='POST')
    resp = urllib.request.urlopen(http_req, timeout=10)
    result = json.loads(resp.read().decode())
    key = result.get('api_key', '')
    if not key:
        print('ERROR: no api_key in response: ' + json.dumps(result), file=sys.stderr)
        sys.exit(1)
    print(key, end='')   # ← only the token on stdout (no newline). The client uses this value as the Bearer
except urllib.error.HTTPError as e:
    print('ERROR: HTTP ' + str(e.code) + ' (region=' + region + '): ' + e.read().decode()[:200], file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print('ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
PYTHON
```

**SigV4 call points, summarized**:
- `SigV4Auth(frozen, 'execute-api', region).add_auth(req)` — `execute-api` is API Gateway's service namespace. This one line
  fills the `Authorization`/`X-Amz-Date`/`X-Amz-Security-Token` headers.
- `creds.get_frozen_credentials()` — snapshots the SSO temporary credentials (AccessKey + SecretKey + **SessionToken**).
  If the SessionToken is missing, API Gateway rejects the request.
- POSTs an empty body (`{}`) — **identity comes from the signed caller ARN, not from the body**. (Consistent with the
  server-side AuthStack `APIG2` suppression rationale: there is no body schema to validate.)

> **Pitfall**: the payload signed via `body = json.dumps({})` and the payload actually sent via `body.encode()` must be
> **byte-for-byte identical**. Changing either one causes a payload-hash mismatch and a 403.

---

## 2. `scripts/setup-developer.sh` — one-shot onboarding

Substitutes the deployment outputs into the templates and installs `~/.claude/settings.json`·`~/.codex/config.toml`.

> **WHY is `ALB_DNS` actually the CloudFront domain?** The variable name is historical, but the LiteLLM ALB is **internal**,
> so it cannot be reached directly from the internet. Clients access it only through CloudFront (the TLS termination point).
> So the value put here must be the **public CloudFront domain**. The comment warns about this strongly.

```bash
#!/usr/bin/env bash
# setup-developer.sh — one-shot developer onboarding.
# Substitutes deployment outputs into the templates and installs them.
#
# NOTE: ALB_DNS must be the PUBLIC gateway domain served by CloudFront
# (e.g. llmlite.example.com), NOT the internal ALB DNS. The LiteLLM ALB is
# internal-only; clients reach it via CloudFront (TLS terminated there).
#
# Usage:
#   ALB_DNS=llmlite.example.com \
#   TOKEN_SERVICE_URL=https://abc.execute-api.us-east-2.amazonaws.com/v1/auth/token \
#   ./scripts/setup-developer.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
: "${ALB_DNS:?set ALB_DNS to the PUBLIC gateway domain (CloudFront, e.g. llmlite.example.com) — not the internal ALB DNS}"
: "${TOKEN_SERVICE_URL:?set TOKEN_SERVICE_URL to the Token Service invoke URL}"
AWS_REGION="${AWS_REGION:-us-east-2}"

echo "==> Configuring Claude Code (~/.claude/settings.json)"
mkdir -p "$HOME/.claude"
sed -e "s#{ALB_DNS}#${ALB_DNS}#g" -e "s#{REPO}#${REPO}#g" \
  "$REPO/templates/claude-settings.json" > "$HOME/.claude/settings.json"

echo "==> Configuring Codex (~/.codex/config.toml)"
mkdir -p "$HOME/.codex"
sed -e "s#{ALB_DNS}#${ALB_DNS}#g" -e "s#{REPO}#${REPO}#g" \
  "$REPO/templates/codex-config.toml" > "$HOME/.codex/config.toml"

echo "==> Wiring Token Service URL into the key helper"
chmod +x "$REPO/scripts/get-gateway-token.sh"
export TOKEN_SERVICE_URL AWS_REGION

# The single AWS SSO profile shared by Claude Code AND Codex (the key helper
# defaults AWS_PROFILE to this). Named after the gateway, not a single client.
AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-llm-gateway}"

# Optional: write the AWS SSO profile (additive — never clobbers existing profiles).
# Provide SSO_START_URL / SSO_ACCOUNT_ID / SSO_ROLE_NAME (from AuthStack SSO outputs).
if [ -n "${SSO_START_URL:-}" ] && [ -n "${SSO_ACCOUNT_ID:-}" ] && [ -n "${SSO_ROLE_NAME:-}" ]; then
  SSO_REGION="${SSO_REGION:-us-east-1}"
  mkdir -p "$HOME/.aws"; touch "$HOME/.aws/config"
  if grep -q "^\[profile ${AWS_PROFILE_NAME}\]" "$HOME/.aws/config" 2>/dev/null; then
    echo "==> ~/.aws/config already has [profile ${AWS_PROFILE_NAME}] — unchanged"
  else
    echo "==> Adding SSO profile [profile ${AWS_PROFILE_NAME}] to ~/.aws/config"
    cat >> "$HOME/.aws/config" <<EOF

[sso-session ${AWS_PROFILE_NAME}]
sso_start_url = ${SSO_START_URL}
sso_region = ${SSO_REGION}
sso_registration_scopes = sso:account:access

[profile ${AWS_PROFILE_NAME}]
sso_session = ${AWS_PROFILE_NAME}
sso_account_id = ${SSO_ACCOUNT_ID}
sso_role_name = ${SSO_ROLE_NAME}
region = ${AWS_REGION}
EOF
  fi
else
  echo "==> Skipping ~/.aws/config profile creation (set SSO_START_URL / SSO_ACCOUNT_ID / SSO_ROLE_NAME to enable)"
fi

cat <<EOF

Done. Next steps:
  1. aws sso login --profile ${AWS_PROFILE_NAME}
  2. Run 'claude' or 'codex' — the key helper fetches your virtual key automatically.
     (Claude Code and Codex share the single "${AWS_PROFILE_NAME}" profile.)
  3. Verify with: TOKEN_SERVICE_URL="$TOKEN_SERVICE_URL" $REPO/scripts/healthcheck.sh
EOF
```

> **The AWS profile name is `llm-gateway` (named after the gateway, not a specific client).** Claude Code and Codex
> **share a single profile** — the key helper (`get-gateway-token.sh`) defaults `AWS_PROFILE` to this value, and the Codex
> `config.toml` does not specify a separate profile, so it uses the same one as-is.
> Passing `SSO_START_URL`/`SSO_ACCOUNT_ID`/`SSO_ROLE_NAME` (AuthStack's SSO outputs) makes this script idempotently create
> `[sso-session llm-gateway]` + `[profile llm-gateway]` in `~/.aws/config`.
> If omitted, a developer can create it directly with `aws configure sso --profile llm-gateway`.

> **WHY substitute `{ALB_DNS}`/`{REPO}` with `sed`?** The templates (#4·#5 below) hold only placeholders and no secrets.
> `{REPO}` must be replaced with an absolute path so the client reliably executes the key helper's **absolute path**
> (Claude Code/Codex invoke the helper from an arbitrary cwd, so a relative path breaks).

---

## 3. `scripts/healthcheck.sh` — onboarding verification

Verifies key issuance (1/2) and the LiteLLM health endpoint (2/2) separately.

```bash
#!/usr/bin/env bash
# healthcheck.sh — verify the developer can obtain a virtual key and reach LiteLLM.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 1/2 Fetching virtual key via Token Service"
KEY="$("$REPO/scripts/get-gateway-token.sh")" || { echo "FAILED: could not obtain key"; exit 1; }
echo "    ok: got key (${#KEY} chars)"

if [ -n "${ALB_DNS:-}" ]; then
  echo "==> 2/2 Probing LiteLLM health endpoint"
  if curl -fsS "https://${ALB_DNS}/health/liveliness" >/dev/null; then
    echo "    ok: LiteLLM is live"
  else
    echo "    WARN: health endpoint not reachable (check network/cert)"
  fi
else
  echo "==> 2/2 skipped (set ALB_DNS to probe LiteLLM health)"
fi
echo "Healthcheck complete."
```

> **WHY print only the key length (`${#KEY} chars`)?** The virtual key (`sk-...`) is a secret, so its value is never written to logs.
> Showing only the length proves "issued successfully." The health probe hits `/health/liveliness` (LiteLLM's default endpoint) —
> `-f` makes it fail on HTTP 4xx/5xx.

---

## 4. `templates/claude-settings.json` — Claude Code client

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://{ALB_DNS}",
    "AWS_REGION": "us-east-2",
    "AWS_PROFILE": "llm-gateway",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"
  },
  "apiKeyHelper": "{REPO}/scripts/get-gateway-token.sh",
  "permissions": {
    "deny": ["WebSearch"]
  }
}
```

> **WHY each field**:
> - `ANTHROPIC_BASE_URL = https://{ALB_DNS}` — routes to the gateway (CloudFront) instead of the Anthropic API.
>   LiteLLM converts `/v1/messages` to Bedrock Claude.
> - `apiKeyHelper` — Claude Code runs this script just before each request to obtain the Bearer key. **No static API key.**
> - `ANTHROPIC_DEFAULT_*_MODEL` — match the model aliases to the names in the LiteLLM `model_list` (LiteLLMStack).
> - `permissions.deny: ["WebSearch"]` — blocks Claude's built-in WebSearch so that search flows only through the gateway's
>   AgentCore Web Search MCP (`websearch` server, us-east-1 gateway). The intent is to enforce governance (search traffic also passes through the gateway).

---

## 5. `templates/codex-config.toml` — Codex client

```toml
# Codex CLI config — route through the company LLM gateway (LiteLLM) to Bedrock.
# Place at ~/.codex/config.toml. After editing, restart Codex / IDE extension.
#
# Auth: `aws sso login` once, then auth.command fetches a virtual key per the
# same script Claude Code uses (apiKeyHelper). Codex caches the key and refreshes
# every refresh_interval_ms.

model = "gpt-5.5"
model_provider = "llm-gateway"

[model_providers.llm-gateway]
name = "Company LLM Gateway (LiteLLM)"
base_url = "https://{ALB_DNS}/v1"
wire_api = "responses"

[model_providers.llm-gateway.auth]
command = "{REPO}/scripts/get-gateway-token.sh"
refresh_interval_ms = 300000
timeout_ms = 5000
```

> **WHY `wire_api = "responses"`?** Codex/GPT-family use the OpenAI **Responses API** wire format.
> LiteLLM routes this to `bedrock_mantle/` (Bedrock's OpenAI-compatible path) → hence `/v1` is appended to `base_url`.
> Claude (`/v1/messages`) and GPT (`/v1/responses`) operate on the same gateway over different wires.
>
> **WHY `auth.command` + `refresh_interval_ms`?** Codex has no `apiKeyHelper`, so it calls the same helper via `auth.command`.
> It refreshes the key every 5 minutes (300000ms) → reuses the cached key before the SSO session expires.

> **Pitfall (Mantle + Guardrail)**: the GPT (Mantle) path is **not covered by the Bedrock Guardrail**
> (Guardrails are bedrock-runtime only). Content/PII protection for GPT traffic relies on the LiteLLM `hide-secrets` callback.
> State this limitation in the onboarding docs.

---

## Verification (acceptance criteria for this pattern's outputs)

1. After `aws sso login --profile llm-gateway`, `./scripts/get-gateway-token.sh` → one line of output starting with `sk-`.
2. Calling with a direct IAM role (not SSO) → Token Lambda returns 403 (`caller is not an IAM Identity Center (SSO) principal`).
3. `healthcheck.sh` → key issuance OK + `/health/liveliness` 200.
4. When `claude` runs, model calls go out to the CloudFront domain and usage shows up in the LiteLLM Admin UI (`/ui/`).
