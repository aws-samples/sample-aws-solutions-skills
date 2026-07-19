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

`setup-developer.sh` / `setup-developer.ps1` are **zero-touch** thin launchers over `gateway_auth.py setup` (§1A — ONE cross-platform implementation of all derivation/merge logic): they read `outputs.json` (`cdk deploy --outputs-file outputs.json`) and derive everything — no env vars to assemble by hand. Env vars exist only as overrides:
- `ALB_DNS` + `GATEWAY_SCHEME` — auto-derived by splitting the `GatewayUrl` output (`https://<domain>` for acm, `http://<alb-dns>` for http). Override only when running without `outputs.json`.
- `TOKEN_SERVICE_URL` — auto-derived from the `TokenServiceUrl` output.
- `SSO_START_URL` / `SSO_ACCOUNT_ID` / `SSO_ROLE_NAME` — auto-derived from the AuthStack SSO outputs (org-sso).

---

## 1. `scripts/get-gateway-token.sh` — shared key helper (SigV4 point)

The script that Claude Code's `apiKeyHelper` and Codex's `auth.command` run **identically**.
The contract is strict: **stdout is exactly one line with the token**, diagnostics go to stderr, non-zero exit on failure (launchers must **propagate** that exit code — Claude Code/Codex read it).

> ⚠️ **POSIX-only legacy form.** This standalone bash+here-doc script remains valid for
> macOS/Linux-only `org-sso` deployments. The **canonical cross-platform token path is
> `gateway_auth.py token` (§1A)**, which implements the SAME SigV4 logic in Python for
> `org-sso` **and** the Cognito flow for `cognito-native`. **Windows has no bash — a Windows
> developer in an `org-sso` deployment MUST use `get-gateway-token.ps1` → `gateway_auth.py
> token` (org-sso SigV4 is built in);** do not present this `.sh` as the Windows path.

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



## 1A. Cross-platform client core (`gateway_auth.py` + thin launchers) — BOTH auth modes

Generate a **single Python core plus thin OS-specific launchers** for every deployment. `gateway_auth.py` handles **both** auth modes (`authMode` is stamped into `~/.llm-gateway/config.json` by `setup`): the `cognito-native` PKCE/Hosted-UI flow **and** the `org-sso` SigV4 token fetch — so a Windows developer is supported in either mode without bash (the §1 `.sh` is the POSIX-only legacy form).

```text
scripts/gateway_auth.py          # shared Python implementation (setup/login/token/healthcheck/mcp-headers, BOTH auth modes)
scripts/llmgw-login.sh           # macOS/Linux: login wrapper (cognito-native)
scripts/llmgw-login.ps1          # Windows PowerShell: login wrapper (cognito-native)
scripts/get-gateway-token.sh     # macOS/Linux: prints sk-... only
scripts/get-gateway-token.ps1    # Windows PowerShell: prints sk-... only
scripts/setup-developer.sh       # macOS/Linux setup (thin wrapper -> gateway_auth.py setup)
scripts/setup-developer.ps1      # Windows setup (thin wrapper -> gateway_auth.py setup)
scripts/healthcheck.sh           # macOS/Linux verification (thin wrapper)
scripts/healthcheck.ps1          # Windows verification (thin wrapper)
```

`gateway_auth.py` subcommands:
- `setup`: **all onboarding derivation/merge logic lives here, once, cross-platform** (the `.sh`/`.ps1` setup scripts are thin wrappers, so the merge rules can never drift between OSes). Reads `outputs.json` (env vars remain overrides), derives the gateway/token URLs and `authMode` (Cognito outputs present → `cognito-native`, else `org-sso`), writes `~/.llm-gateway/config.json` (+ the legacy `env` file the POSIX `.sh` helpers read), **copies `gateway_auth.py` itself to `~/.llm-gateway/gateway_auth.py`** (a stable, repo-independent helper path — the path the docs reference), **merges** `~/.claude/settings.json` and `~/.codex/config.toml` (backup first — only our keys/block), and (org-sso) idempotently appends the AWS SSO profile. On Windows it writes the helper commands using `sys.executable` (never bare `python`, which may resolve to the Microsoft Store alias stub).
- `login`: `cognito-native` only — PKCE verifier/challenge, loopback HTTP listener (**loops until `/callback`** — a single `handle_request()` is a bug: any stray hit like favicon/preconnect would consume it), `webbrowser` to the Cognito Hosted UI `/oauth2/authorize`, code exchange at `/oauth2/token`, tokens cached under `~/.llm-gateway/`. In `org-sso` mode it exits with the correct `aws sso login` command instead.
- `token`: **`org-sso`** → boto3 SigV4 POST to the Token Service (region parsed from the `execute-api` URL host, empty body byte-identical — same rules as §1; boto3 is imported lazily so `cognito-native` stays stdlib-only). **`cognito-native`** → refresh the Cognito tokens when needed and send `Authorization: Bearer <ACCESS token>` (⚠️ **the access token, never the id_token** — the API Gateway Cognito authorizer 401s on an id_token even though it also carries `cognito:groups`). Either way: parse `api_key`, print only the key to stdout, non-zero exit on failure.
- `mcp-headers`: print `{"Authorization": "Bearer <key>"}` as JSON — the entry point for Claude Code's MCP `headersHelper` so the AgentCore Web Search MCP always gets a fresh virtual key with no static token in `.mcp.json`.
- `healthcheck`: call `token`, then probe `<gatewayUrl>/health/liveliness` — the **full URL including scheme** from config, so it works in `certMode=http` too (never hardcode `https://`).

The full `scripts/gateway_auth.py` (cross-platform, no shell-only assumptions):

```python
#!/usr/bin/env python3
"""gateway_auth.py — shared cross-platform client core for Claude Code / Codex.

ONE file, BOTH auth modes (`authMode` in ~/.llm-gateway/config.json, stamped
by `setup`):
  - org-sso        : login = `aws sso login --profile <profile>` (NOT here);
                     `token` SigV4-signs the Token Service call with boto3
                     (imported lazily — cognito-native never needs it).
  - cognito-native : Cognito User Pool is the SOLE identity source. `login` is
                     Cognito's own Hosted UI (Authorization Code + PKCE,
                     loopback listener); team membership arrives in the
                     `cognito:groups` claim of the issued JWT.

Subcommands: setup | login | token | healthcheck | mcp-headers.
Cross-platform rules: pathlib/webbrowser/http.server/urllib only (boto3 lazily
for org-sso `token`), so it runs unmodified from a .sh (macOS/Linux) or .ps1
(Windows) launcher. Windows file protection is icacls best-effort (chmod 0600
is a no-op there). stdout contract for `token`: EXACTLY the key, no newline;
diagnostics to stderr; NON-ZERO EXIT on failure — launchers must propagate it
(Claude Code / Codex read the exit code). Never prints refresh tokens.
"""
import argparse, base64, hashlib, http.server, json, os, re, secrets, shutil, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".llm-gateway"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_TOKEN_CACHE_PATH = DEFAULT_CONFIG_DIR / "tokens.json"
LOOPBACK_HOST, LOOPBACK_PORT = "127.0.0.1", 8400
DEFAULT_ALIASES = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5",
                   "haiku": "claude-haiku-4-5", "fable": "claude-fable-5", "gpt": "gpt-5.5"}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _restrict_perms(p: Path) -> None:
    """Best-effort user-only file permissions. chmod(0o600) is a NO-OP on
    Windows (it only maps to the read-only flag), so on nt ALSO strip ACL
    inheritance and grant only the current user via icacls."""
    try:
        os.chmod(p, 0o600)
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, check=False)
        except Exception:  # noqa: BLE001
            pass


def _load_config(p: Path) -> dict[str, Any]:
    if not p.exists():
        _log(f"ERROR: config not found at {p}. Run setup-developer (gateway_auth.py setup) first.")
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
    _restrict_perms(p)  # 0600 on POSIX; icacls user-only on Windows (chmod alone is a no-op there)


def _pkce() -> tuple[str, str]:
    v = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


class _CB(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            # Stray hit (favicon.ico, a browser preconnect probe, another local
            # process): answer 404 and KEEP LISTENING — _run_cb loops until the
            # real /callback arrives.
            self.send_response(404); self.end_headers(); return
        _CB.result = dict(urllib.parse.parse_qsl(parsed.query))
        _CB.done.set()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body>Login complete. You can close this window.</body></html>")

    def log_message(self, *a):  # noqa: A002
        pass


def _run_cb(timeout: int = 180) -> dict[str, str]:
    # Serve requests in a LOOP until /callback lands or the deadline passes.
    # A single handle_request() is a real-world bug: any stray request (favicon,
    # preconnect) consumes the one slot and the login dies with "state mismatch"
    # even though the user did everything right.
    srv = http.server.HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _CB)
    srv.timeout = 1  # per-accept timeout so the deadline is re-checked every second
    deadline = time.time() + timeout
    while not _CB.done.is_set() and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    return _CB.result


def cmd_login(cfg_path: Path, cache_path: Path) -> None:
    cfg = _load_config(cfg_path)
    if cfg.get("authMode") == "org-sso":
        _log("org-sso mode: run `aws sso login --profile "
             f"{cfg.get('awsProfile', 'llm-gateway')}` instead (this login is cognito-native only)")
        sys.exit(1)
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


def _region_from_url(u: str):
    """{api-id}.execute-api.{region}.amazonaws.com -> {region}. NEVER hardcode
    the SigV4 region — it must match the Token Service API's own region."""
    parts = (urllib.parse.urlparse(u).hostname or "").split(".")
    if "execute-api" in parts:
        i = parts.index("execute-api")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _fetch_key_org_sso(cfg: dict[str, Any]) -> str:
    """org-sso: SigV4-sign the Token Service call from the SSO profile creds.
    Same rules as the legacy bash helper (§1): region parsed from the
    execute-api host, empty body byte-identical between signing and sending.
    boto3 is imported lazily — cognito-native never needs it. This is the ONLY
    supported org-sso token path on Windows (no bash there)."""
    try:
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except ImportError:
        _log("ERROR: boto3 required for org-sso. pip install boto3"); sys.exit(1)
    url = cfg["tokenServiceUrl"]
    region = (os.environ.get("AWS_SIGV4_REGION") or _region_from_url(url)
              or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    if not region:
        _log(f"ERROR: could not determine region from tokenServiceUrl={url}"); sys.exit(1)
    profile = os.environ.get("AWS_PROFILE") or cfg.get("awsProfile", "llm-gateway")
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        creds = session.get_credentials()
    except Exception:  # noqa: BLE001 - unknown profile, expired sso cache, ...
        creds = None
    if not creds:
        _log(f"ERROR: no credentials. Run: aws sso login --profile {profile}"); sys.exit(1)
    frozen = creds.get_frozen_credentials()
    body = json.dumps({})  # signed and sent payloads MUST be byte-identical
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(frozen, "execute-api", region).add_auth(req)
    try:
        http_req = urllib.request.Request(url, data=body.encode(), headers=dict(req.headers), method="POST")
        with urllib.request.urlopen(http_req, timeout=10) as r:
            result = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        _log(f"ERROR: HTTP {exc.code} (region={region}): {exc.read().decode()[:200]}"); sys.exit(1)
    key = result.get("api_key", "")
    if not key:
        _log(f"ERROR: no api_key in response: {json.dumps(result)}"); sys.exit(1)
    return key


def _fetch_key_cognito(cfg: dict[str, Any], cache_path: Path) -> str:
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


def _fetch_key(cfg_path: Path, cache_path: Path) -> str:
    cfg = _load_config(cfg_path)
    if cfg.get("authMode") == "org-sso":
        return _fetch_key_org_sso(cfg)
    return _fetch_key_cognito(cfg, cache_path)


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
    # Full URL incl. scheme from config — works for certMode=http too. Never
    # hardcode https:// here (an http PoC deploy would always fail the probe).
    base = cfg.get("gatewayUrl") or (f"https://{cfg['gatewayDomain']}" if cfg.get("gatewayDomain") else "")
    if base:
        _log("==> 2/2 Probing LiteLLM health endpoint")
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{base}/health/liveliness"), timeout=10) as r:
                _log("    ok: LiteLLM is live" if r.status == 200 else f"    WARN: status {r.status}")
        except Exception as exc:  # noqa: BLE001
            _log(f"    WARN: health endpoint not reachable ({exc})")
    _log("Healthcheck complete.")


# --------------------------------------------------------------------------- #
# setup — one-shot onboarding (ALL merge/derivation logic lives here, once)
# --------------------------------------------------------------------------- #
def _flatten_outputs(path: Path) -> dict[str, str]:
    flat: dict[str, str] = {}
    for stack in json.loads(path.read_text()).values():
        if isinstance(stack, dict):
            flat.update(stack)
    return flat


def _helper_cmd(installed: Path, sub: str) -> str:
    """The command string written into client configs (apiKeyHelper /
    auth.command / headersHelper). Windows: absolute interpreter via
    sys.executable + the ~/.llm-gateway copy — NEVER bare `python`, which on a
    stock Windows box resolves to the Microsoft Store alias stub (prints
    nothing, exit 9009). POSIX: the proven launcher scripts."""
    if os.name == "nt":
        return f'"{sys.executable}" "{installed}" {sub}'
    if sub == "token":
        return str(Path(__file__).resolve().parent / "get-gateway-token.sh")  # repo launcher (setup runs from scripts/)
    return str(DEFAULT_CONFIG_DIR / "get-mcp-headers.sh")  # written by setup below


def _helper_program_args(installed: Path, sub: str) -> "tuple[str, list[str]]":
    """Codex spawns auth.command directly (CreateProcess/exec — NO shell), so the
    executable and its arguments must be written as separate TOML fields
    (command + args). A joined '"prog" "arg" sub' string is treated as ONE
    program path: process creation fails (Windows: os error 123), no
    Authorization header is ever attached, and every request 401s with
    "No api key passed in" (real Windows incident). Claude Code's apiKeyHelper
    runs through a shell, so _helper_cmd's joined string stays correct there."""
    if os.name == "nt":
        return sys.executable, [str(installed), sub]
    if sub == "token":
        return str(Path(__file__).resolve().parent / "get-gateway-token.sh"), []
    return str(DEFAULT_CONFIG_DIR / "get-mcp-headers.sh"), []


def _merge_claude_settings(gw: str, region: str, auth_mode: str, aliases: dict, helper: str, stamp: str) -> None:
    # MERGE, don't overwrite (real-deploy incident): ~/.claude/settings.json is
    # the user's own shared config — hooks/plugins/statusLine from other tools
    # live here. Back up first, then update ONLY our keys.
    p = Path.home() / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict[str, Any] = {}
    if p.exists():
        shutil.copyfile(p, p.with_name(f"settings.json.llmgw-backup-{stamp}"))
        try:
            cfg = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            _log(f"ERROR: {p} exists but is not valid JSON - fix or move it first (backup was taken)")
            sys.exit(1)
    env = cfg.setdefault("env", {})
    # Remove direct-Bedrock remnants BEFORE merging (real Windows incident): leaving them
    # makes two auth paths fight — CLAUDE_CODE_USE_BEDROCK=1 bypasses ANTHROPIC_BASE_URL
    # entirely, and a stale AWS_BEARER_TOKEN_BEDROCK produces 403 "API key is not valid".
    for k in ("CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK"):
        env.pop(k, None)
    # A leftover TOP-LEVEL "model" (a raw Bedrock model ID from a direct-Bedrock setup)
    # outranks the ANTHROPIC_DEFAULT_*_MODEL aliases, so the gateway receives a model it
    # doesn't serve → "team not allowed to access model". The alias env vars are the contract.
    cfg.pop("model", None)
    env.update({
        "ANTHROPIC_BASE_URL": gw,
        "AWS_REGION": region,
        # ALL FOUR aliases — omitting Fable hides that tier from /model.
        "ANTHROPIC_DEFAULT_OPUS_MODEL": aliases["opus"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": aliases["sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": aliases["haiku"],
        "ANTHROPIC_DEFAULT_FABLE_MODEL": aliases["fable"],
    })
    if auth_mode == "org-sso":
        env["AWS_PROFILE"] = os.environ.get("AWS_PROFILE_NAME", "llm-gateway")
    cfg["apiKeyHelper"] = helper
    deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
    if "WebSearch" not in deny:
        deny.append("WebSearch")
    # hooks / enabledPlugins / statusLine / anything else: untouched.
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    _log(f"    merged: {p}")


def _merge_codex_config(gw: str, aliases: dict, prog_args: "tuple[str, list[str]]", stamp: str) -> None:
    # TOML: replace ONLY our [model_providers.llm-gateway](+.auth) block; upsert
    # top-level keys ONLY in the top-level region (before the first table header
    # — appending after a table would silently re-scope them); keep a user's
    # existing model= value and say so.
    p = Path.home() / ".codex" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if p.exists():
        shutil.copyfile(p, p.with_name(f"config.toml.llmgw-backup-{stamp}"))
        text = p.read_text()

    def esc(s: str) -> str:  # TOML basic-string escaping (Windows paths!)
        return s.replace("\\", "\\\\").replace('"', '\\"')

    # command = executable ONLY, args = list — Codex spawns this without a shell
    # (see _helper_program_args; a joined string here caused the 401 incident).
    prog, args = prog_args
    args_toml = "[" + ", ".join(f'"{esc(a)}"' for a in args) + "]"
    block = ("[model_providers.llm-gateway]\n"
             'name = "Company LLM Gateway (LiteLLM)"\n'
             f'base_url = "{gw}/v1"\n'
             'wire_api = "responses"\n\n'
             "[model_providers.llm-gateway.auth]\n"
             f'command = "{esc(prog)}"\n'
             f"args = {args_toml}\n"
             "refresh_interval_ms = 300000\n"
             "timeout_ms = 5000\n")
    text = re.sub(r"^\[model_providers\.llm-gateway(?:\.auth)?\][^\[]*(?=^\[|\Z)", "", text, flags=re.M | re.S)
    m = re.search(r"^\[", text, flags=re.M)
    top, rest = (text[: m.start()], text[m.start():]) if m else (text, "")

    def upsert(region_text: str, key: str, line: str) -> str:
        hit = re.search(rf"^{key}\s*=\s*(.+)$", region_text, flags=re.M)
        if hit is None:
            return line + "\n" + region_text
        if hit.group(0).strip() != line:
            _log(f"    note: keeping existing top-level `{hit.group(0).strip()}` (wanted `{line}`)")
        return region_text

    top = upsert(top, "model_provider", 'model_provider = "llm-gateway"')
    top = upsert(top, "model", f'model = "{aliases["gpt"]}"')
    # web_search MUST be disabled (real incident): custom providers default the
    # web_search capability ON, Codex's interactive TUI then attaches a web_search
    # tool, and Mantle rejects it ("Live web access is not yet available") -> LiteLLM
    # 500 -> Codex shows a misleading "Reconnecting... high demand" loop. `codex exec`
    # doesn't attach the tool, so the failure only appears in interactive sessions.
    top = upsert(top, "web_search", 'web_search = "disabled"')
    parts = [top.rstrip("\n"), rest.strip("\n"), block.rstrip("\n")]
    p.write_text("\n\n".join(s for s in parts if s) + "\n")
    _log(f"    merged: {p}")


def _write_sso_profile(flat: dict[str, str], region: str) -> None:
    # Additive only — never clobbers existing profiles (same shape the legacy
    # bash setup wrote: [sso-session] + [profile], both named llm-gateway).
    start_url = os.environ.get("SSO_START_URL") or flat.get("SsoStartUrl", "")
    account_id = os.environ.get("SSO_ACCOUNT_ID") or flat.get("SsoAccountId", "")
    role_name = os.environ.get("SSO_ROLE_NAME") or flat.get("SsoRoleName", "")
    if not (start_url and account_id and role_name):
        _log("==> Skipping ~/.aws/config profile creation (SSO outputs not present)")
        return
    name = os.environ.get("AWS_PROFILE_NAME", "llm-gateway")
    sso_region = os.environ.get("SSO_REGION") or flat.get("SsoRegion", "us-east-1")
    aws_cfg = Path.home() / ".aws" / "config"
    aws_cfg.parent.mkdir(parents=True, exist_ok=True)
    existing = aws_cfg.read_text() if aws_cfg.exists() else ""
    if re.search(rf"^\[profile {re.escape(name)}\]", existing, flags=re.M):
        _log(f"==> ~/.aws/config already has [profile {name}] - unchanged")
        return
    with aws_cfg.open("a") as f:
        f.write(f"\n[sso-session {name}]\nsso_start_url = {start_url}\nsso_region = {sso_region}\n"
                "sso_registration_scopes = sso:account:access\n"
                f"\n[profile {name}]\nsso_session = {name}\nsso_account_id = {account_id}\n"
                f"sso_role_name = {role_name}\nregion = {region}\n")
    _log(f"==> Added SSO profile [profile {name}] to ~/.aws/config")


def cmd_setup(cfg_path: Path, _cache_path: Path, outputs_arg) -> None:
    repo = Path(__file__).resolve().parent.parent  # scripts/ -> repo root
    outputs_path = Path(outputs_arg or os.environ.get("OUTPUTS_FILE") or repo / "outputs.json")
    flat = _flatten_outputs(outputs_path) if outputs_path.exists() else {}
    # ---- derive endpoints (env vars are OVERRIDES — the zero-touch contract) --
    gw = os.environ.get("GATEWAY_URL") or flat.get("GatewayUrl", "")
    if not gw:
        alb = os.environ.get("ALB_DNS") or flat.get("AlbDns", "")
        if alb:
            gw = f"{os.environ.get('GATEWAY_SCHEME', 'https')}://{alb}"
    token_url = os.environ.get("TOKEN_SERVICE_URL") or flat.get("TokenServiceUrl", "")
    if not gw or not token_url:
        _log(f"ERROR: GatewayUrl/TokenServiceUrl not found (looked in {outputs_path} and env) - "
             "deploy with: cdk deploy --all --outputs-file outputs.json")
        sys.exit(1)
    region = _region_from_url(token_url) or os.environ.get("AWS_REGION", "us-east-1")
    # authMode: the presence of the Cognito outputs is authoritative.
    auth_mode = "cognito-native" if (flat.get("CognitoAppClientId") or os.environ.get("COGNITO_APP_CLIENT_ID")) else "org-sso"

    cfg: dict[str, Any] = {"authMode": auth_mode, "gatewayUrl": gw,
                           "tokenServiceUrl": token_url, "awsRegion": region}
    if auth_mode == "cognito-native":
        hosted = os.environ.get("COGNITO_HOSTED_UI") or flat.get("CognitoHostedUiDomain", "")
        cfg.update({"appClientId": os.environ.get("COGNITO_APP_CLIENT_ID") or flat["CognitoAppClientId"],
                    "authorizationEndpoint": f"https://{hosted}/oauth2/authorize",
                    "tokenEndpoint": f"https://{hosted}/oauth2/token"})
    else:
        cfg["awsProfile"] = os.environ.get("AWS_PROFILE_NAME", "llm-gateway")

    # ---- install: config + a stable copy of this file (repo-independent path) -
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2))
    _restrict_perms(cfg_path)
    installed = DEFAULT_CONFIG_DIR / "gateway_auth.py"
    if Path(__file__).resolve() != installed:
        shutil.copyfile(Path(__file__).resolve(), installed)
    # Legacy env file — still read by the POSIX .sh helpers (URLs only, no secrets).
    (DEFAULT_CONFIG_DIR / "env").write_text(f"TOKEN_SERVICE_URL={token_url}\nGATEWAY_URL={gw}\n")
    _restrict_perms(DEFAULT_CONFIG_DIR / "env")
    if os.name != "nt":
        mcp_sh = DEFAULT_CONFIG_DIR / "get-mcp-headers.sh"
        mcp_sh.write_text(f'#!/bin/sh\nexec python3 "{installed}" mcp-headers "$@"\n')
        mcp_sh.chmod(0o755)
    _log(f"==> Wrote {cfg_path} and installed helper copy at {installed}")

    aliases = dict(DEFAULT_ALIASES)
    dev_json = repo / "config" / "dev.json"
    if dev_json.exists():
        try:
            aliases.update(json.loads(dev_json.read_text()).get("litellm", {}).get("modelAliases", {}) or {})
        except Exception:  # noqa: BLE001
            pass

    stamp = time.strftime("%Y%m%d-%H%M%S")
    _log("==> Merging Claude Code settings (~/.claude/settings.json)")
    _merge_claude_settings(gw, region, auth_mode, aliases, _helper_cmd(installed, "token"), stamp)
    _log("==> Merging Codex config (~/.codex/config.toml)")
    _merge_codex_config(gw, aliases, _helper_program_args(installed, "token"), stamp)
    if auth_mode == "org-sso":
        _write_sso_profile(flat, region)

    if auth_mode == "org-sso":
        login = f"aws sso login --profile {cfg['awsProfile']}"
    else:
        login = "scripts\\llmgw-login.ps1" if os.name == "nt" else "scripts/llmgw-login.sh"
    hc = "scripts\\healthcheck.ps1" if os.name == "nt" else "scripts/healthcheck.sh"
    _log(f"\nDone. Next steps:\n  1. {login}\n"
         "  2. Run claude or codex - the key helper fetches your virtual key automatically.\n"
         f"  3. Verify with {hc}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LLM Gateway cross-platform client helper (org-sso + cognito-native)")
    ap.add_argument("command", choices=["setup", "login", "token", "healthcheck", "mcp-headers"])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH)
    ap.add_argument("--outputs", default=None,
                    help="setup: path to cdk outputs.json (default: <repo>/outputs.json)")
    args = ap.parse_args()
    if args.command == "setup":
        cmd_setup(args.config, args.token_cache, args.outputs)
        return
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
exec python3 "$SCRIPT_DIR/gateway_auth.py" token "$@"
```

```powershell
# scripts/get-gateway-token.ps1 (Windows). Contract: stdout = token only,
# NON-ZERO EXIT on failure — Claude Code/Codex read the exit code.
# ⚠️ Two Windows-specific rules (both real gotchas):
#   1. $ErrorActionPreference='Stop' does NOT propagate a native command's
#      non-zero exit in Windows PowerShell 5.1 — without `exit $LASTEXITCODE`
#      a failed python still exits 0 and the client uses empty stdout as the
#      key (unexplained 401s). ALWAYS end with `exit $LASTEXITCODE`.
#   2. Bare `python` may resolve to the Microsoft Store alias stub (prints
#      nothing, exit 9009) — prefer the `py -3` launcher when present.
$gw = Join-Path $PSScriptRoot "gateway_auth.py"   # $PSScriptRoot: robust from any cwd (PS3+)
if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 $gw token @args }
else { & python $gw token @args }
exit $LASTEXITCODE
```

Generate `llmgw-login.ps1`, `setup-developer.ps1`, and `healthcheck.ps1` **identically**, replacing the `token` subcommand with `login` / `setup` / `healthcheck` — every `.ps1` launcher is this same 4-line pattern (py-launcher preference + `exit $LASTEXITCODE`), never more.

Claude Code / Codex Windows helper when PowerShell execution policy is restrictive — `setup` writes exactly this form automatically (using `sys.executable` + the `~/.llm-gateway` copy it installs, so the path really exists and survives repo moves):

```jsonc
// written by `gateway_auth.py setup` on Windows — sys.executable avoids the
// Microsoft Store `python` alias stub; the ~/.llm-gateway copy is made by setup.
"apiKeyHelper": "\"C:\\Program Files\\Python312\\python.exe\" \"C:\\Users\\<user>\\.llm-gateway\\gateway_auth.py\" token"
```

**Register the AgentCore Web Search MCP on the client** (registering it server-side in LiteLLM does not auto-enable it):

```bash
claude mcp add-json websearch '{
  "type": "http",
  "url": "<gateway-url>/mcp/",
  "headersHelper": "'"$HOME"'/.llm-gateway/get-mcp-headers.sh"
}'
# get-mcp-headers.sh is WRITTEN BY `gateway_auth.py setup` (POSIX): a one-line
# launcher for `gateway_auth.py mcp-headers`. On Windows, register instead with:
#   "headersHelper": "\"<sys.executable>\" \"C:\\Users\\<user>\\.llm-gateway\\gateway_auth.py\" mcp-headers"
```

Avoid `sed`, `chmod`, bash here-docs, and Unix-only paths anywhere in the client path. The §1 standalone bash helper remains acceptable **only** for POSIX-only `org-sso` deployments; **on Windows both auth modes go through `gateway_auth.py`** (`token` includes the org-sso SigV4 path).

## 2. `scripts/setup-developer.sh` / `.ps1` — one-shot onboarding (thin wrappers)

**All derivation/merge logic lives in `gateway_auth.py setup` (§1A) — one cross-platform implementation.** The setup scripts are thin launchers, so the merge rules can never drift between the macOS/Linux and Windows paths (drift is exactly what would re-create the overwrite incident below on the OS nobody tested). The zero-touch contract is unchanged: read `outputs.json` (`cdk deploy --outputs-file outputs.json`) and derive everything; env vars are **overrides only** (`GATEWAY_URL` or `ALB_DNS`+`GATEWAY_SCHEME`, `TOKEN_SERVICE_URL`, `SSO_START_URL`/`SSO_ACCOUNT_ID`/`SSO_ROLE_NAME`, `AWS_PROFILE_NAME`). The skill agent runs it automatically right after deploy (Phase 5) — **on a Windows operator machine run the `.ps1`** (or `python scripts\gateway_auth.py setup`); the `.sh` requires bash (WSL/Git Bash).

> ⚠️ **Merge, don't overwrite (real-deploy incident).** An earlier revision rendered the templates with `sed` and wrote them with `>` — one run wiped the user's existing `~/.claude/settings.json` hooks/plugins and `~/.codex/config.toml` project-trust sections (recovered only thanks to another tool's own backups). These are **shared personal config files**: JSON is handled load → update only our keys (`env`, `apiKeyHelper`, `permissions.deny`) → save; TOML replaces only the `[model_providers.llm-gateway]` block; both are backed up to `*.llmgw-backup-<timestamp>` on every run. The rules are enforced in `_merge_claude_settings` / `_merge_codex_config` (§1A) — the **only** place they exist. Never regress to template-overwrite, and never re-implement the merge in shell (that is how the Windows copy drifts).

> **WHY derive from `GatewayUrl`?** CloudFront is removed — the public ALB is the edge, and the `GatewayUrl` output already carries both the scheme and the host (`https://<acm-domain>` or `http://<alb-dns>`). Splitting it yields the scheme + host, so neither the operator nor a developer needs to know `certMode` or look up the ALB DNS (and can never mistakenly use the internal Token-Service ALB DNS `:4000`). Env vars remain as overrides for running without `outputs.json`.

```bash
#!/usr/bin/env bash
# setup-developer.sh — one-shot developer onboarding (thin launcher).
# Usage (zero-touch):
#   cdk deploy --all --outputs-file outputs.json
#   ./scripts/setup-developer.sh
# ALL logic lives in gateway_auth.py `setup` (env overrides documented there).
set -euo pipefail
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
exec python3 "$SCRIPT_DIR/gateway_auth.py" setup "$@"
```

```powershell
# setup-developer.ps1 — one-shot developer onboarding (Windows thin launcher).
$gw = Join-Path $PSScriptRoot "gateway_auth.py"
if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 $gw setup @args }
else { & python $gw setup @args }
exit $LASTEXITCODE
```

What `setup` performs (implemented once, in Python — §1A):

| Target | Action |
|---|---|
| `~/.llm-gateway/config.json` | `authMode` (auto-detected from the outputs) + gateway/token URLs (+ Cognito endpoints or `awsProfile`) — user-only perms (POSIX `0600` / Windows `icacls`) |
| `~/.llm-gateway/gateway_auth.py` | a **copy of the core itself** — the stable, repo-independent helper path the Windows client config points at |
| `~/.llm-gateway/env` | URLs only, no secrets — still read by the legacy POSIX `.sh` helpers |
| `~/.llm-gateway/get-mcp-headers.sh` | (POSIX only) one-line `mcp-headers` launcher for the MCP `headersHelper` |
| `~/.claude/settings.json` | **merge**: gateway URL · region · **all four model aliases** (omitting Fable hides that tier from `/model`) · `apiKeyHelper` (OS-appropriate command) · `permissions.deny: ["WebSearch"]` — backup first |
| `~/.codex/config.toml` | **merge**: only the `[model_providers.llm-gateway]`(+`.auth`) block, plus top-level `model`/`model_provider` upsert in the top-level region — backup first |
| `~/.aws/config` | (org-sso only) idempotently append `[sso-session llm-gateway]` + `[profile llm-gateway]` — never clobbers existing profiles |

> **The AWS profile name is `llm-gateway` (named after the gateway, not a specific client).** Claude Code and Codex
> **share a single profile** — the token helper defaults `AWS_PROFILE` to this value, and the Codex
> `config.toml` does not specify a separate profile, so it uses the same one as-is.
> If the SSO outputs are absent, a developer can create it directly with `aws configure sso --profile llm-gateway`.

> **WHY are #4·#5 "reference shapes", not files to copy?** They document exactly what the `setup` merge produces (our keys/block only, no secrets). The setup path never renders them with `sed` and never overwrites the target files — see the merge-don't-overwrite warning above.
> `{REPO}` must be replaced with an absolute path so the client reliably executes the key helper's **absolute path**
> (Claude Code/Codex invoke the helper from an arbitrary cwd, so a relative path breaks). On Windows the helper is the
> absolute `"<sys.executable>" "<home>\.llm-gateway\gateway_auth.py" token` command written by `setup`.

---

## 3. `scripts/healthcheck.sh` / `.ps1` — onboarding verification (thin wrappers)

Verifies key issuance (1/2) and the LiteLLM health endpoint (2/2) — both implemented in `gateway_auth.py healthcheck`, which probes `<gatewayUrl>/health/liveliness` using the **full URL incl. scheme** from config (so it works in `certMode=http` too — never hardcode `https://`).

```bash
#!/usr/bin/env bash
# healthcheck.sh — verify the developer can obtain a virtual key and reach LiteLLM.
set -euo pipefail
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
exec python3 "$SCRIPT_DIR/gateway_auth.py" healthcheck "$@"
```

(`healthcheck.ps1` is the standard 4-line PowerShell launcher from §1A with the `healthcheck` subcommand — py-launcher preference + `exit $LASTEXITCODE`.)

> **WHY print only the key length (`got key (N chars)`)?** The virtual key (`sk-...`) is a secret, so its value is never written to logs.
> Showing only the length proves "issued successfully." The health probe hits `/health/liveliness` (LiteLLM's default endpoint).

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
> - **Windows**: `setup` writes `apiKeyHelper` as `"<sys.executable>" "<home>\\.llm-gateway\\gateway_auth.py" token` instead of the `.sh` path (no bash on Windows; `sys.executable` avoids the Microsoft Store `python` alias stub). ⚠️ This joined-string form is correct **only for Claude Code** (apiKeyHelper runs through a shell) — Codex's `auth.command` in §5 must instead be split into `command` (executable only) + `args` (array), because Codex spawns it without a shell.

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
web_search = "disabled"

[model_providers.llm-gateway]
name = "Company LLM Gateway (LiteLLM)"
base_url = "{GATEWAY_URL}/v1"
wire_api = "responses"

[model_providers.llm-gateway.auth]
command = "{REPO}/scripts/get-gateway-token.sh"
args = []
refresh_interval_ms = 300000
timeout_ms = 5000
# Windows (written by `setup` automatically):
#   command = "C:\\Program Files\\Python312\\python.exe"
#   args = ["C:\\Users\\<user>\\.llm-gateway\\gateway_auth.py", "token"]
```

> **WHY `wire_api = "responses"`?** Codex/GPT-family use the OpenAI **Responses API** wire format.
> LiteLLM routes this to `bedrock_mantle/` (Bedrock's OpenAI-compatible path) → hence `/v1` is appended to `base_url`.
> Claude (`/v1/messages`) and GPT (`/v1/responses`) operate on the same gateway over different wires.
>
> **WHY `auth.command` + `refresh_interval_ms`?** Codex has no `apiKeyHelper`, so it calls the same helper via `auth.command`.
> It refreshes the key every 5 minutes (300000ms) → reuses the cached key before the SSO session expires.
>
> ⚠️ **WHY `command` + `args` as SEPARATE fields (real Windows incident)?** Codex executes `auth.command`
> directly (CreateProcess/exec) — **no shell parsing**. A joined string like
> `command = "\"C:\\...\\python.exe\" \"...gateway_auth.py\" token"` is treated as one executable path:
> process creation fails (os error 123), no `Authorization` header is attached, and every request fails
> with 401 "No api key passed in". `gateway_auth.py setup` writes the split form via `_helper_program_args()`.
>
> ⚠️ **WHY `web_search = "disabled"` (real incident)?** Codex custom providers default the `web_search`
> capability ON; the interactive TUI then attaches a `web_search` tool that Bedrock Mantle rejects with
> `validation_error: "Live web access is not yet available"` → LiteLLM 500 → Codex shows a misleading
> **"Reconnecting... We're currently experiencing high demand"** loop. `codex exec` doesn't attach the
> tool, so the failure appears **only in interactive sessions** — deceptive to debug. Search flows
> through the gateway's AgentCore Web Search MCP instead (same governance intent as Claude Code's
> `permissions.deny: ["WebSearch"]`). `setup` upserts this key automatically.

> **Pitfall (Mantle + Guardrail)**: the GPT (Mantle) path is **not covered by the Bedrock Guardrail**
> (Guardrails are bedrock-runtime only). Content/PII protection for GPT traffic relies on the LiteLLM `hide-secrets` callback.
> State this limitation in the onboarding docs.

---

## Verification (acceptance criteria for this pattern's outputs)

1. `org-sso`: after `aws sso login --profile llm-gateway`, `./scripts/get-gateway-token.sh` (macOS/Linux) **and** `.\scripts\get-gateway-token.ps1` (Windows — via `gateway_auth.py token` org-sso SigV4) → one line of output starting with `sk-`.
2. `cognito-native`: after `llmgw-login` (or `llmgw-login.ps1`), `get-gateway-token` → one line of output starting with `sk-`.
3. Calling org-sso with a direct IAM role (not SSO) → Token Lambda returns 403 (`caller is not an IAM Identity Center (SSO) principal`).
4. `cognito-native`: a caller in no matching `teamGroupPrefix` group (or in two) → 403 with a clear diagnostic; sending the **id_token** instead of the access token → 401 at the API Gateway Cognito authorizer.
5. `healthcheck.sh` / `healthcheck.ps1` → key issuance OK + `/health/liveliness` 200 (probe uses the config `gatewayUrl` scheme — passes in `http` certMode too).
6. When `claude` runs, model calls go out to the gateway URL and usage shows up in the LiteLLM Admin UI (`/ui/`). Registering the `websearch` MCP (`claude mcp add-json` + `headersHelper`) makes `websearch-web-search-tool___WebSearch` available.
7. **Windows exit-code contract**: with an invalid/absent config, `.\scripts\get-gateway-token.ps1; $LASTEXITCODE` shows a **non-zero** code (the launcher ends with `exit $LASTEXITCODE` — PS 5.1 does not propagate native exit codes otherwise), and stdout stays empty.
8. **Loopback robustness**: during `llmgw-login`, a stray request to `http://127.0.0.1:8400/anything` gets a 404 and the login still completes when the real `/callback` arrives (the listener loops; it is not consumed by the first hit).

---

## 6. Post-deploy onboarding HTML (`scripts/gen-onboarding.py`)

After a successful deploy, produce **two self-contained HTML docs** from the cdk outputs — this is the Phase 6 final deliverable and **replaces the old inline markdown guide**. Golden sources live in `shared/patterns/onboarding/`; emit them into the generated app as `templates/onboarding/*.html.tmpl` + `scripts/gen-onboarding.py`.

| File | Audience | Contents | Secrets |
|------|----------|----------|---------|
| `developer-setup.html` | every developer (shareable) | **script-first**: run `setup-developer.sh`/`.ps1` (one-shot merge of Claude Code + Codex config, with backups), login, (http) plaintext/SG-allowlist notice, web search MCP register command, verify via `healthcheck.sh`/`.ps1`; the merged JSON/TOML appears only as a reference appendix — never as hand-edit instructions | **none** |
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
| `{{TOKEN_CMD}}` / `{{TOKEN_CMD_WIN}}`, `{{MCP_HEADERS_CMD}}` / `{{MCP_HEADERS_CMD_WIN}}` | POSIX launcher paths / Windows `py -3 "%USERPROFILE%\.llm-gateway\gateway_auth.py" …` forms (the copy `setup` installs) |
| `{{MASTER_KEY}}` / `{{MASTER_KEY_SECRET}}` | `config.litellm.masterKey` / outputs `MasterKeySecretArn` (admin doc only) |
| `{{LANGFUSE_ADMIN_PW}}` | `config.langfuse.adminPassword` or `--fetch-secrets` (Secrets Manager) |
| `{{OPUS}}/{{SONNET}}/{{HAIKU}}/{{FABLE}}/{{GPT}}` | `config.litellm.modelAliases` (fallback to current defaults) |

> **Acceptance**: `developer-setup.html` MUST NOT contain the master key; `admin-onboarding.html` MUST be user-only — `0600` on POSIX, **`icacls /inheritance:r /grant:r <user>:F` on Windows** (chmod is a no-op there; the generator applies both); no `{{…}}` tokens or `<!--IF…-->` markers may remain in either output. (base URL is the ALB/gateway URL — CloudFront is removed; see `shared/reference/constraints.md`.)
