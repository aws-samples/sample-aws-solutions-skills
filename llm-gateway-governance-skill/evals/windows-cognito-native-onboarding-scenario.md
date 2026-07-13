# Eval — Windows PowerShell onboarding for `cognito-native`

## Scenario

A developer on **Windows** needs to use Claude Code and Codex against a `cognito-native` LLM gateway (the org only has an IdC account instance, so Cognito is the identity source). The organization blocks WSL for this developer, so **native PowerShell** instructions are required. The gateway is already deployed; this eval covers the client onboarding output.

## Expected skill behavior

### A. Prerequisite framing
- [ ] The onboarding prerequisite is **`llmgw-login.ps1`** (opens the Cognito Hosted UI, PKCE, caches tokens) — **not** `aws sso login`. The output must not tell a `cognito-native` user to run `aws sso login`.
- [ ] The output explains access-token vs refresh-token lifetimes: the access token (~1h) is auto-refreshed by the helper; the refresh token (default 30 days, `cognitoNative.refreshTokenValidityDays`) determines when the developer must re-run `llmgw-login`.

### B. Cross-platform helpers (no Unix-only assumptions)
- [ ] PowerShell launchers are provided: `llmgw-login.ps1`, `get-gateway-token.ps1`, `setup-developer.ps1`, `healthcheck.ps1` — each a thin wrapper over `python gateway_auth.py <cmd>`.
- [ ] No required `sed`, `chmod`, bash here-docs, or POSIX-only paths appear in the Windows path. `gateway_auth.py` uses only `pathlib`/`webbrowser`/`http.server`/`urllib`/`json`.
- [ ] Launchers resolve their own real path (`$MyInvocation.MyCommand.Path`) so they run from any cwd; an explicit `python C:\Users\<user>\.llm-gateway\gateway_auth.py token --config ...` form is offered when PowerShell execution policy is restrictive.

### C. Client config
- [ ] Claude Code `~/.claude/settings.json`: `ANTHROPIC_BASE_URL`, `AWS_REGION`, **all four** `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`, `apiKeyHelper` → `get-gateway-token.ps1` (or `python ...gateway_auth.py token`), `permissions.deny: ["WebSearch"]`.
- [ ] Codex `~/.codex/config.toml`: `base_url=.../v1`, `wire_api=responses`, `web_search="disabled"`, `auth.command` → the PowerShell/python token helper.
- [ ] The token helper sends the Cognito **access token** (id_token → 401).
- [ ] AgentCore Web Search MCP is registered via `claude mcp add-json` + `headersHelper` → `gateway_auth.py mcp-headers`.
- [ ] `ANTHROPIC_BASE_URL` / Codex `base_url` = the **`GatewayUrl` output** (the ALB domain — CloudFront is removed; `certMode` is orthogonal to `cognito-native`): `https://<custom-domain>` for `acm`, or `http://<alb-dns>` for `http` (plaintext, reachable only from the SG `albIngressCidrs` allowlist — no cert trust step, no tunnel).
- [ ] Ends by generating the **two HTML onboarding docs** (`developer-setup.html` + `admin-onboarding.html`) via `scripts/gen-onboarding.py`; the developer doc carries the PowerShell launchers + the `certMode`-specific base URL.

### D. Verification
- [ ] `healthcheck.ps1` obtains a key and probes `/health/liveliness`.
- [ ] `GET /v1/models` returns the configured aliases (incl. GPT-5.x); `/v1/mcp/tools` lists `websearch-web-search-tool___WebSearch` once registered.

## Failure checks (must NOT happen)
- [ ] Must not present bash-only scripts as the Windows path, or require WSL.
- [ ] Must not omit `ANTHROPIC_DEFAULT_FABLE_MODEL` (its absence hides the Fable tier from the `/model` picker).
- [ ] Must not tell the user to run `aws sso login`.
- [ ] Must not instruct sending the id_token to the Token Service.

## Pass criteria
All of A–D satisfied and no failure check triggered. A Windows developer can log in, fetch a rotating virtual key, and use Claude Code + Codex + web search entirely through native PowerShell/Python, with no WSL and no `aws sso login`.
