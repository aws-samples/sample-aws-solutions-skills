# Pattern — Developer Onboarding (authMode → key helper → virtual key)

A pattern for regenerating the **client-side onboarding** that connects a developer's Claude Code / Codex to the internal
LLM gateway. What this pattern covers is **3 shell scripts + 2 client templates**, all real code taken verbatim from the
`scripts/`·`templates/` of the `llm-gateway-multi-agent` reference.

Core flow (`org-sso`):

```
aws sso login --profile llm-gateway
   → the client (Claude Code / Codex) runs the key helper on every request
   → get-gateway-token.sh : calls API Gateway (/auth/token) with boto3 SigV4
   → Token Lambda verifies the SSO identity and returns a LiteLLM virtual key (sk-...)
   → the client uses that virtual key as a Bearer token against the gateway URL (the public ALB)
```



Core flow (`cognito-native`):

```
llmgw-login
   → browser opens the Cognito Hosted UI (Cognito's OWN email/password form;
     no external IdP, no IdC — Cognito is the sole identity source)
   → gateway_auth.py completes the PKCE code exchange and caches the tokens
   → the client runs get-gateway-token helper on every request
   → helper sends the Cognito ACCESS token (NOT id_token — the API Gateway
     Cognito authorizer 401s on an id_token) to Token Service (/auth/token)
   → API Gateway Cognito authorizer verifies the JWT; Token Lambda reads the
     verified cognito:groups claim (no Identity Store call) and returns sk-...
   → the client uses that virtual key as a Bearer token against the gateway URL (the public ALB)
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
| `claude-settings.json` → `ANTHROPIC_BASE_URL` | gateway URL (public ALB) | **LiteLLMStack** |
| `codex-config.toml` → `base_url` `/v1` `wire_api=responses` | same (gateway URL) | **LiteLLMStack** |
| returned virtual key (`sk-...`) | LiteLLM `/v1/messages`·`/v1/responses` | **LiteLLMStack** |
| key issuance/cache | DynamoDB key cache | **AuthStack (Token Lambda + DDB)** |

`setup-developer.sh` is **zero-touch**: it reads `outputs.json` (`cdk deploy --outputs-file outputs.json`) and derives everything itself — no env vars to assemble by hand. Env vars exist only as overrides:
- `ALB_DNS` + `GATEWAY_SCHEME` — auto-derived by splitting the `GatewayUrl` output (`https://<domain>` for acm, `http://<alb-dns>` for http). Override only when running without `outputs.json`.
- `TOKEN_SERVICE_URL` — auto-derived from the `TokenServiceUrl` output.
- `SSO_START_URL` / `SSO_ACCOUNT_ID` / `SSO_ROLE_NAME` — auto-derived from the AuthStack SSO outputs (org-sso).

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

# Resolve this script's real path (works when symlinked into ~/.local/bin).
SRC="$0"; while [ -L "$SRC" ]; do SRC="$(readlink "$SRC")"; done
REPO="$(cd "$(dirname "$SRC")/.." && pwd)"

# TOKEN_SERVICE_URL resolution chain (zero-touch — the developer never assembles it):
#   1) env var (override)
#   2) ~/.llm-gateway/env — persisted by setup-developer.sh (URLs only, no secrets)
#   3) $REPO/outputs.json — the TokenServiceUrl output, when running from the deploy repo
if [ -z "${TOKEN_SERVICE_URL:-}" ] && [ -f "$HOME/.llm-gateway/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.llm-gateway/env"
fi
if [ -z "${TOKEN_SERVICE_URL:-}" ] && [ -f "$REPO/outputs.json" ]; then
  TOKEN_SERVICE_URL="$(python3 -c "import json;flat={};[flat.update(s) for s in json.load(open('$REPO/outputs.json')).values()];print(flat.get('TokenServiceUrl',''),end='')")"
fi
: "${TOKEN_SERVICE_URL:?not set - run scripts/setup-developer.sh once (it persists ~/.llm-gateway/env), or export TOKEN_SERVICE_URL}"
export TOKEN_SERVICE_URL
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



## 1A. `cognito-native` cross-platform client pattern (`gateway_auth.py` + launchers)

For `cognito-native` (IdC account instances / no usable org-sso), do not generate an AWS SSO profile or require `aws sso login`. Generate a common Python core plus thin OS-specific launchers:

```text
scripts/gateway_auth.py          # shared Python implementation (login/token/healthcheck/mcp-headers)
scripts/llmgw-login.sh           # macOS/Linux: login wrapper
scripts/llmgw-login.ps1          # Windows PowerShell: login wrapper
scripts/get-gateway-token.sh     # macOS/Linux: prints sk-... only
scripts/get-gateway-token.ps1    # Windows PowerShell: prints sk-... only
scripts/setup-developer.sh       # macOS/Linux setup
scripts/setup-developer.ps1      # Windows setup
scripts/healthcheck.sh           # macOS/Linux verification
scripts/healthcheck.ps1          # Windows verification
```

`gateway_auth.py` subcommands:
- `login`: generate PKCE verifier/challenge, start a loopback HTTP listener with `http.server`, open `webbrowser` to the Cognito Hosted UI `/oauth2/authorize`, exchange the auth code at `/oauth2/token`, and cache tokens with `pathlib` under `~/.llm-gateway/`.
- `token`: refresh the Cognito tokens when needed, call Token Service `/auth/token` with `Authorization: Bearer <ACCESS token>` (⚠️ **the access token, never the id_token** — the API Gateway Cognito authorizer 401s on an id_token even though it also carries `cognito:groups`), parse `api_key`, and print only the key to stdout.
- `mcp-headers`: print `{"Authorization": "Bearer <key>"}` as JSON — the entry point for Claude Code's MCP `headersHelper` so the AgentCore Web Search MCP always gets a fresh virtual key with no static token in `.mcp.json`.
- `healthcheck`: call `token`, then verify `<gateway-url>/v1/models` without logging secrets.

The full `services`/`scripts/gateway_auth.py` (cross-platform, no shell-only assumptions):

```python
#!/usr/bin/env python3
"""gateway_auth.py — shared cognito-native client core for Claude Code / Codex.

Cognito User Pool is the SOLE identity source (no external IdP, no IdC
federation — the account instance backing this deployment cannot host a SAML
customer-managed application). Login is Cognito's own Hosted UI; team
membership is a native Cognito User Pool Group, delivered in the
`cognito:groups` claim of the issued JWT.

Subcommands: login | token | healthcheck | mcp-headers.
Cross-platform: pathlib/webbrowser/http.server/urllib only, so it runs
unmodified from a .sh (macOS/Linux) or .ps1 (Windows) launcher. Never prints
refresh tokens; only the derived LiteLLM virtual key goes to stdout.
"""
import argparse, base64, hashlib, http.server, json, os, secrets, sys, threading, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".llm-gateway"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_TOKEN_CACHE_PATH = DEFAULT_CONFIG_DIR / "tokens.json"
LOOPBACK_HOST, LOOPBACK_PORT = "127.0.0.1", 8400


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_config(p: Path) -> dict[str, Any]:
    if not p.exists():
        _log(f"ERROR: config not found at {p}. Run setup-developer first.")
        sys.exit(1)
    return json.loads(p.read_text())


def _load_cache(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    try:
        os.chmod(p, 0o600)  # best-effort; no-op where POSIX perms are absent
    except Exception:  # noqa: BLE001
        pass


def _pkce() -> tuple[str, str]:
    v = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


class _CB(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404); self.end_headers(); return
        _CB.result = dict(urllib.parse.parse_qsl(parsed.query))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body>Login complete. You can close this window.</body></html>")

    def log_message(self, *a):  # noqa: A002
        pass


def _run_cb(timeout: int = 180) -> dict[str, str]:
    srv = http.server.HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _CB)
    srv.timeout = timeout
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start(); t.join(timeout); srv.server_close()
    return _CB.result


def cmd_login(cfg_path: Path, cache_path: Path) -> None:
    cfg = _load_config(cfg_path)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    redirect = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}/callback"
    params = {"client_id": cfg["appClientId"], "response_type": "code",
              "scope": "openid email profile", "redirect_uri": redirect, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    url = f"{cfg['authorizationEndpoint']}?{urllib.parse.urlencode(params)}"
    _log(f"==> Opening browser for login: {url}")
    webbrowser.open(url)
    res = _run_cb()
    if not res or res.get("state") != state or "code" not in res:
        _log("ERROR: login callback did not complete (timeout or state mismatch)"); sys.exit(1)
    body = {"grant_type": "authorization_code", "client_id": cfg["appClientId"],
            "code": res["code"], "redirect_uri": redirect, "code_verifier": verifier}
    req = urllib.request.Request(cfg["tokenEndpoint"], data=urllib.parse.urlencode(body).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read().decode())
    _save_cache(cache_path, {"access_token": tok["access_token"], "id_token": tok.get("id_token"),
                             "refresh_token": tok.get("refresh_token"), "obtained_at": int(time.time()),
                             "expires_in": tok.get("expires_in", 3600)})
    _log("==> Login complete. Tokens cached.")


def _refresh_if_needed(cfg: dict, cache: dict) -> dict:
    if time.time() < cache.get("obtained_at", 0) + cache.get("expires_in", 3600) - 60:
        return cache
    rt = cache.get("refresh_token")
    if not rt:
        _log("ERROR: token expired and no refresh_token cached. Run login again."); sys.exit(1)
    body = {"grant_type": "refresh_token", "client_id": cfg["appClientId"], "refresh_token": rt}
    req = urllib.request.Request(cfg["tokenEndpoint"], data=urllib.parse.urlencode(body).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError:
        _log("ERROR: token refresh failed. Run login again."); sys.exit(1)
    cache["access_token"] = tok["access_token"]
    cache["id_token"] = tok.get("id_token", cache.get("id_token"))
    cache["obtained_at"] = int(time.time()); cache["expires_in"] = tok.get("expires_in", 3600)
    return cache


def _fetch_key(cfg_path: Path, cache_path: Path) -> str:
    cfg = _load_config(cfg_path)
    cache = _load_cache(cache_path)
    if not cache:
        _log("ERROR: not logged in. Run: llmgw-login"); sys.exit(1)
    cache = _refresh_if_needed(cfg, cache)
    _save_cache(cache_path, cache)
    # IMPORTANT: send the ACCESS token. The API Gateway COGNITO_USER_POOLS
    # authorizer only accepts token_use=access; an id_token returns 401.
    bearer = cache["access_token"]
    req = urllib.request.Request(cfg["tokenServiceUrl"], data=b"{}",
                                 headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        _log(f"ERROR: HTTP {exc.code}: {exc.read().decode()[:300]}"); sys.exit(1)
    key = result.get("api_key", "")
    if not key:
        _log(f"ERROR: no api_key in response: {json.dumps(result)}"); sys.exit(1)
    return key


def cmd_token(cfg_path: Path, cache_path: Path) -> None:
    print(_fetch_key(cfg_path, cache_path), end="")  # stdout: ONLY the token, no trailing newline


def cmd_mcp_headers(cfg_path: Path, cache_path: Path) -> None:
    # headersHelper entry point: Claude Code runs this per MCP connection and merges
    # the printed JSON into the request headers, so the rotating virtual key also
    # authenticates the AgentCore Web Search MCP with no static token in .mcp.json.
    print(json.dumps({"Authorization": f"Bearer {_fetch_key(cfg_path, cache_path)}"}))


def cmd_healthcheck(cfg_path: Path, cache_path: Path) -> None:
    cfg = _load_config(cfg_path)
    _log("==> 1/2 Fetching virtual key via Token Service")
    key = _fetch_key(cfg_path, cache_path)
    _log(f"    ok: got key ({len(key)} chars)")
    dom = cfg.get("gatewayDomain")
    if dom:
        _log("==> 2/2 Probing LiteLLM health endpoint")
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://{dom}/health/liveliness"), timeout=10) as r:
                _log("    ok: LiteLLM is live" if r.status == 200 else f"    WARN: status {r.status}")
        except Exception as exc:  # noqa: BLE001
            _log(f"    WARN: health endpoint not reachable ({exc})")
    _log("Healthcheck complete.")


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM Gateway cognito-native client helper")
    ap.add_argument("command", choices=["login", "token", "healthcheck", "mcp-headers"])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH)
    args = ap.parse_args()
    {"login": cmd_login, "token": cmd_token, "healthcheck": cmd_healthcheck,
     "mcp-headers": cmd_mcp_headers}[args.command](args.config, args.token_cache)


if __name__ == "__main__":
    main()
```

**Launchers must resolve their own real path** so they run from any cwd (including a `~/.local/bin` symlink) — do **not** hardcode the repo directory:

```bash
# scripts/get-gateway-token.sh (macOS/Linux). Contract: stdout = token only.
set -euo pipefail
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -L "$SOURCE" ]; do                       # follow symlinks to the real file
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
python3 "$SCRIPT_DIR/gateway_auth.py" token "$@"
```

```powershell
# scripts/get-gateway-token.ps1 (Windows)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
python "$ScriptDir\gateway_auth.py" token @args
```

Claude Code / Codex Windows helper when PowerShell execution policy is restrictive:

```jsonc
"apiKeyHelper": "python C:\\Users\\<user>\\.llm-gateway\\gateway_auth.py token --config C:\\Users\\<user>\\.llm-gateway\\config.json"
```

**Register the AgentCore Web Search MCP on the client** (registering it server-side in LiteLLM does not auto-enable it):

```bash
claude mcp add-json websearch '{
  "type": "http",
  "url": "<gateway-url>/mcp/",
  "headersHelper": "'"$HOME"'/.llm-gateway/get-mcp-headers.sh"
}'
# get-mcp-headers.sh is a readlink-resolving launcher that runs: gateway_auth.py mcp-headers
```

Avoid `sed`, `chmod`, bash here-docs, and Unix-only paths in the `cognito-native` path. Those remain acceptable only for the legacy `org-sso` `.sh` examples.

## 2. `scripts/setup-developer.sh` — one-shot onboarding (zero-touch)

Reads `outputs.json` and **merges** the gateway settings into `~/.claude/settings.json` · `~/.codex/config.toml` — **run with no arguments; nothing to assemble by hand**, and **existing user config is preserved** (backed up to `*.llmgw-backup-<timestamp>` on every run). The skill agent runs it automatically right after `cdk deploy --outputs-file outputs.json` (Phase 5).

> ⚠️ **Merge, don't overwrite (real-deploy incident).** An earlier revision rendered the templates with `sed` and wrote them with `>` — one run wiped the user's existing `~/.claude/settings.json` hooks/plugins and `~/.codex/config.toml` project-trust sections (recovered only thanks to another tool's own backups). These are **shared personal config files**: JSON is handled load → update only our keys (`env`, `apiKeyHelper`, `permissions.deny`) → save; TOML replaces only the `[model_providers.llm-gateway]` block. Never regress to template-overwrite.

> **WHY derive from `GatewayUrl`?** CloudFront is removed — the public ALB is the edge, and the `GatewayUrl` output already carries both the scheme and the host (`https://<acm-domain>` or `http://<alb-dns>`). Splitting it yields `GATEWAY_SCHEME` + `ALB_DNS`, so neither the operator nor a developer needs to know `certMode` or look up the ALB DNS (and can never mistakenly use the internal Token-Service ALB DNS `:4000`). Env vars remain as overrides for running without `outputs.json`.

```bash
#!/usr/bin/env bash
# setup-developer.sh — one-shot developer onboarding.
# Merges the gateway settings into the users existing client configs (backup first).
#
# NOTE: values come from outputs.json automatically — GatewayUrl (scheme + host of
# the PUBLIC gateway ALB; never the internal Token-Service ALB), TokenServiceUrl,
# and the SSO outputs. CloudFront is removed; the ALB is the edge.
#
# Usage (zero-touch — reads outputs.json, nothing to pass):
#   cdk deploy --all --outputs-file outputs.json
#   ./scripts/setup-developer.sh
#
# Env vars are OVERRIDES only (for running without outputs.json):
#   ALB_DNS=llmlite.example.com GATEWAY_SCHEME=https \
#   TOKEN_SERVICE_URL=https://abc.execute-api.us-east-2.amazonaws.com/v1/auth/token \
#   ./scripts/setup-developer.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ---- Auto-derive from cdk outputs (outputs.json) -----------------------------
# GatewayUrl carries BOTH the scheme and the host (https://<domain> for acm,
# http://<alb-dns> for http), so no one has to know certMode to onboard.
OUTPUTS_FILE="${OUTPUTS_FILE:-$REPO/outputs.json}"
if [ -f "$OUTPUTS_FILE" ]; then
  _out() {
    python3 - "$1" <<PYEOF
import json, sys
flat = {}
for stack in json.load(open("$OUTPUTS_FILE")).values():
    flat.update(stack)
print(flat.get(sys.argv[1], ""), end="")
PYEOF
  }
  GW_URL="$(_out GatewayUrl)"
  if [ -n "$GW_URL" ]; then
    GATEWAY_SCHEME="${GATEWAY_SCHEME:-${GW_URL%%://*}}"
    ALB_DNS="${ALB_DNS:-${GW_URL#*://}}"
  fi
  TOKEN_SERVICE_URL="${TOKEN_SERVICE_URL:-$(_out TokenServiceUrl)}"
  SSO_START_URL="${SSO_START_URL:-$(_out SsoStartUrl)}"
  SSO_ACCOUNT_ID="${SSO_ACCOUNT_ID:-$(_out SsoAccountId)}"
  SSO_ROLE_NAME="${SSO_ROLE_NAME:-$(_out SsoRoleName)}"
fi

: "${ALB_DNS:?no outputs.json found and ALB_DNS not set - deploy with --outputs-file outputs.json (preferred) or export ALB_DNS/GATEWAY_SCHEME/TOKEN_SERVICE_URL manually}"
: "${TOKEN_SERVICE_URL:?TOKEN_SERVICE_URL missing - deploy with --outputs-file outputs.json or export it}"
AWS_REGION="${AWS_REGION:-us-east-2}"
# https for acm (default); http for certMode=http (plaintext PoC, SG-allowlisted).
GATEWAY_SCHEME="${GATEWAY_SCHEME:-https}"
GATEWAY_URL="${GATEWAY_SCHEME}://${ALB_DNS}"

# ---- MERGE, don't overwrite (real-deploy incident) ---------------------------
# ~/.claude/settings.json and ~/.codex/config.toml are the users own shared config
# files — other tools (hooks, plugins, project trust settings) already live there.
# An earlier revision did `sed ... template > target`, which WIPED all of that in
# one command. Rules:
#   * back up the current file to *.llmgw-backup-<timestamp> on every run
#   * JSON: load -> update ONLY our keys (env, apiKeyHelper, permissions.deny) -> save
#   * TOML: replace ONLY the [model_providers.llm-gateway] block; never touch other sections
# NOTE (bash 3.2 / macOS): keep apostrophes and other quote characters OUT of the
# ${VAR:?message} strings and comments-in-expansions — the 2007-era parser miscounts them.
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "==> Merging Claude Code settings (~/.claude/settings.json)"
mkdir -p "$HOME/.claude"
[ -f "$HOME/.claude/settings.json" ] && cp "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.llmgw-backup-${STAMP}"
GATEWAY_URL="$GATEWAY_URL" AWS_REGION="$AWS_REGION" REPO="$REPO" python3 - <<'PYEOF'
import json, os, pathlib
p = pathlib.Path.home() / ".claude" / "settings.json"
cfg = {}
if p.exists():
    try:
        cfg = json.loads(p.read_text())
    except Exception:
        raise SystemExit(f"ERROR: {p} exists but is not valid JSON - fix or move it first (backup was taken)")
env = cfg.setdefault("env", {})                      # update ONLY our keys; keep everything else
env.update({
    "ANTHROPIC_BASE_URL": os.environ["GATEWAY_URL"],
    "AWS_REGION": os.environ["AWS_REGION"],
    "AWS_PROFILE": "llm-gateway",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5",
})
cfg["apiKeyHelper"] = os.environ["REPO"] + "/scripts/get-gateway-token.sh"
deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
if "WebSearch" not in deny:
    deny.append("WebSearch")
# hooks / enabledPlugins / extraKnownMarketplaces / statusLine / anything else: untouched.
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
print(f"    merged: {p}")
PYEOF

echo "==> Merging Codex config (~/.codex/config.toml)"
mkdir -p "$HOME/.codex"
[ -f "$HOME/.codex/config.toml" ] && cp "$HOME/.codex/config.toml" "$HOME/.codex/config.toml.llmgw-backup-${STAMP}"
GATEWAY_URL="$GATEWAY_URL" REPO="$REPO" python3 - <<'PYEOF'
import os, pathlib, re
p = pathlib.Path.home() / ".codex" / "config.toml"
block = (
    "[model_providers.llm-gateway]\n"
    'name = "Company LLM Gateway (LiteLLM)"\n'
    f'base_url = "{os.environ["GATEWAY_URL"]}/v1"\n'
    'wire_api = "responses"\n\n'
    "[model_providers.llm-gateway.auth]\n"
    f'command = "{os.environ["REPO"]}/scripts/get-gateway-token.sh"\n'
    "refresh_interval_ms = 300000\n"
    "timeout_ms = 5000\n"
)
text = p.read_text() if p.exists() else ""
# 1) Strip ONLY our provider block(s); [projects.*], [hooks.*], [plugins.*] etc. untouched.
text = re.sub(
    r"^\[model_providers\.llm-gateway(?:\.auth)?\][^\[]*(?=^\[|\Z)",
    "", text, flags=re.M | re.S,
)
# 2) Top-level keys MUST stay in the top-level region (before the first table header) —
#    appending them after a table would silently re-scope them. Upsert only if absent;
#    if the user already set a different value, keep it and tell them.
m = re.search(r"^\[", text, flags=re.M)
top, rest = (text[: m.start()], text[m.start():]) if m else (text, "")
def upsert(region: str, key: str, line: str) -> str:
    hit = re.search(rf"^{key}\s*=\s*(.+)$", region, flags=re.M)
    if hit is None:
        return line + "\n" + region
    if hit.group(0).strip() != line:
        print(f"    note: keeping existing top-level `{hit.group(0).strip()}` (wanted `{line}`) - change manually if desired")
    return region
top = upsert(top, "model_provider", 'model_provider = "llm-gateway"')
top = upsert(top, "model", 'model = "gpt-5.5"')
# 3) Our tables go at the END — always-valid TOML placement, idempotent across runs.
parts = [top.rstrip("\n"), rest.strip("\n"), block.rstrip("\n")]
p.write_text("\n\n".join(s for s in parts if s) + "\n")
print(f"    merged: {p}")
PYEOF

echo "==> Persisting gateway endpoints to ~/.llm-gateway/env (consumed by the key helper — URLs only, no secrets)"
mkdir -p "$HOME/.llm-gateway"
cat > "$HOME/.llm-gateway/env" <<EOF
TOKEN_SERVICE_URL=${TOKEN_SERVICE_URL}
GATEWAY_URL=${GATEWAY_URL}
EOF
chmod 600 "$HOME/.llm-gateway/env"
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
  3. Verify with: $REPO/scripts/healthcheck.sh   (endpoints auto-resolved from ~/.llm-gateway/env)
EOF
```

> **The AWS profile name is `llm-gateway` (named after the gateway, not a specific client).** Claude Code and Codex
> **share a single profile** — the key helper (`get-gateway-token.sh`) defaults `AWS_PROFILE` to this value, and the Codex
> `config.toml` does not specify a separate profile, so it uses the same one as-is.
> Passing `SSO_START_URL`/`SSO_ACCOUNT_ID`/`SSO_ROLE_NAME` (AuthStack's SSO outputs) makes this script idempotently create
> `[sso-session llm-gateway]` + `[profile llm-gateway]` in `~/.aws/config`.
> If omitted, a developer can create it directly with `aws configure sso --profile llm-gateway`.

> **WHY are #4·#5 "reference shapes", not files to copy?** They document exactly what the merge in `setup-developer.sh` produces (our keys/block only, no secrets). The script never renders them with `sed` and never overwrites the target files — see the merge-don't-overwrite warning in §2.
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

# Endpoint resolution: env overrides → ~/.llm-gateway/env (persisted by setup-developer.sh).
if [ -z "${GATEWAY_URL:-}" ] && [ -f "$HOME/.llm-gateway/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.llm-gateway/env"
fi
GATEWAY_URL="${GATEWAY_URL:-${ALB_DNS:+${GATEWAY_SCHEME:-https}://${ALB_DNS}}}"

if [ -n "${GATEWAY_URL:-}" ]; then
  echo "==> 2/2 Probing LiteLLM health endpoint"
  if curl -fsS "${GATEWAY_URL}/health/liveliness" >/dev/null; then
    echo "    ok: LiteLLM is live"
  else
    echo "    WARN: health endpoint not reachable (check network / SG allowlist)"
  fi
else
  echo "==> 2/2 skipped (run setup-developer.sh first, or set GATEWAY_URL / ALB_DNS)"
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
    "ANTHROPIC_BASE_URL": "{GATEWAY_URL}",
    "AWS_REGION": "us-east-2",
    "AWS_PROFILE": "llm-gateway",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5"
  },
  "apiKeyHelper": "{REPO}/scripts/get-gateway-token.sh",
  "permissions": {
    "deny": ["WebSearch"]
  }
}
```

> **WHY each field**:
> - `ANTHROPIC_BASE_URL = {GATEWAY_URL}` (`https://<domain>` for acm, `http://<alb-dns>` for http) — routes to the gateway (public ALB) instead of the Anthropic API.
>   LiteLLM converts `/v1/messages` to Bedrock Claude.
> - `apiKeyHelper` — Claude Code runs this script just before each request to obtain the Bearer key. **No static API key.**
> - `ANTHROPIC_DEFAULT_*_MODEL` — match the model aliases to the names in the LiteLLM `model_list` (LiteLLMStack). **Emit all four**, including `ANTHROPIC_DEFAULT_FABLE_MODEL`: omitting the Fable var hides the Fable tier from Claude Code's `/model` picker entirely.
> - `permissions.deny: ["WebSearch"]` — blocks Claude's built-in WebSearch so that search flows only through the gateway's
>   AgentCore Web Search MCP (`websearch` server, us-east-1 gateway). The intent is to enforce governance (search traffic also passes through the gateway).

---

## 5. `templates/codex-config.toml` — Codex client

```toml
# Codex CLI config — route through the company LLM gateway (LiteLLM) to Bedrock.
# Place at ~/.codex/config.toml. After editing, restart Codex / IDE extension.
#
# Auth (`org-sso`): run `aws sso login` once, then auth.command fetches a virtual key.
# Auth (`cognito-native`): run `llmgw-login` once, then auth.command calls gateway_auth.py token.
# Codex caches the key and refreshes every refresh_interval_ms.

model = "gpt-5.5"
model_provider = "llm-gateway"

[model_providers.llm-gateway]
name = "Company LLM Gateway (LiteLLM)"
base_url = "{GATEWAY_URL}/v1"
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

1. `org-sso`: after `aws sso login --profile llm-gateway`, `./scripts/get-gateway-token.sh` → one line of output starting with `sk-`.
2. `cognito-native`: after `llmgw-login` (or `llmgw-login.ps1`), `get-gateway-token` → one line of output starting with `sk-`.
3. Calling org-sso with a direct IAM role (not SSO) → Token Lambda returns 403 (`caller is not an IAM Identity Center (SSO) principal`).
4. `cognito-native`: a caller in no matching `teamGroupPrefix` group (or in two) → 403 with a clear diagnostic; sending the **id_token** instead of the access token → 401 at the API Gateway Cognito authorizer.
5. `healthcheck.sh` / `healthcheck.ps1` → key issuance OK + `/health/liveliness` 200.
6. When `claude` runs, model calls go out to the gateway URL and usage shows up in the LiteLLM Admin UI (`/ui/`). Registering the `websearch` MCP (`claude mcp add-json` + `headersHelper`) makes `websearch-web-search-tool___WebSearch` available.

---

## 6. Post-deploy onboarding HTML (`scripts/gen-onboarding.py`)

After a successful deploy, produce **two self-contained HTML docs** from the cdk outputs — this is the Phase 6 final deliverable and **replaces the old inline markdown guide**. Golden sources live in `shared/patterns/onboarding/`; emit them into the generated app as `templates/onboarding/*.html.tmpl` + `scripts/gen-onboarding.py`.

| File | Audience | Contents | Secrets |
|------|----------|----------|---------|
| `developer-setup.html` | every developer (shareable) | Claude Code + Codex setup only, login, (http) plaintext/SG-allowlist notice, web search MCP, verify | **none** |
| `admin-onboarding.html` | the deploying operator ONLY | endpoints, identity, real secret values, password-change, developer on/offboarding | master key, Langfuse pw |

```bash
cdk deploy --all --outputs-file outputs.json
python scripts/gen-onboarding.py --outputs outputs.json --config config/dev.json \
    --templates templates/onboarding --out-dir onboarding [--fetch-secrets]
```

- `developer-setup.html` → hand to developers (no admin secrets; virtual keys come from login).
- `admin-onboarding.html` → **operator only**; written `0600`, embeds the master key. Add `onboarding/admin-onboarding.html` to `.gitignore` — never commit/share.

**How it works**: the generator reads `outputs.json` + `config/dev.json`, **strips** `<!--IF authMode=…-->` / `<!--IF certMode=…-->` / `<!--IF langfuse=on-->` blocks that do not match the deploy, then replaces `{{TOKEN}}`s. A `cognito-native` + `acm` deploy yields a doc with only the cognito-native + acm instructions (org-sso / http blocks removed).

**Token → source (abridged):**

| Token | Source |
|-------|--------|
| `{{GATEWAY_URL}}` | outputs `GatewayUrl`/`LiteLlmUrl` (fallback `https://<AlbDns>`, or `http://<AlbDns>` when `certMode=http`) |
| `{{TOKEN_SERVICE_URL}}`, `{{ADMIN_UI_URL}}` | outputs `TokenServiceUrl`, `AdminUiUrl` |
| `{{COGNITO_*}}` | AuthStack cognito-native outputs |
| `{{SSO_*}}` | AuthStack org-sso outputs |
| `{{MASTER_KEY}}` / `{{MASTER_KEY_SECRET}}` | `config.litellm.masterKey` / outputs `MasterKeySecretArn` (admin doc only) |
| `{{LANGFUSE_ADMIN_PW}}` | `config.langfuse.adminPassword` or `--fetch-secrets` (Secrets Manager) |
| `{{OPUS}}/{{SONNET}}/{{HAIKU}}/{{FABLE}}/{{GPT}}` | `config.litellm.modelAliases` (fallback to current defaults) |

> **Acceptance**: `developer-setup.html` MUST NOT contain the master key; `admin-onboarding.html` MUST be `0600`; no `{{…}}` tokens or `<!--IF…-->` markers may remain in either output. (base URL is the ALB/gateway URL — CloudFront is removed; see `shared/reference/constraints.md`.)
