# Pattern: LiteLLM Governance Gateway (config / image / MCP)

> **Reflects current architecture (v1.1)**: web search uses the **AgentCore Web Search Tool** (built-in connector) and Tavily MCP has been removed → `shared/patterns/agentcore-websearch.md`. GPT-5.x (Mantle) is reached in **us-east-1** over cross-region VPC peering and routed via the `BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE`=us-east-1 env → `shared/patterns/mantle-peering.md`. Claude and GPT-5.x are **both SigV4 (Task Role)** — there is **no** bearer token / 12h expiry / EventBridge token-refresh scheduler (if any old bearer/scheduler description remains below, ignore it and follow the single SigV4 model). The `mcp_servers` in the `config.yaml` below is `websearch` (see §MCP below).

This pattern teaches how to configure the **LiteLLM Proxy** as a single governance gateway.
It reproduces `services/litellm/` of the reference solution (`llm-gateway-multi-agent`) verbatim. The gateway handles all of the following in one place:

- **Model routing** — Claude (`bedrock/`) + GPT-5.x (`bedrock_mantle/`, `BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE`=us-east-1) behind a single OpenAI/Anthropic-compatible endpoint
- **Authentication** — all SigV4 based on the ECS Task Role (no api_key → no token to rotate, no scheduler)
- **Guardrails** — 3-layer defense per request (except for GPT/mantle)
- **MCP (WebSearch)** — calls the AgentCore Gateway's built-in Web Search Tool via cross-region SigV4 (`bedrock-agentcore`, `InvokeGateway`)

Core design principle: **inject all dynamic values via environment variables.** So that the same image works
across all environments (dev/prod), config.yaml never hardcodes secrets or environment-specific values. The values
are injected by the ECS Task Definition (Secrets Manager + plaintext env).

Cross-layer mapping:

| Layer | What it provides | Where |
|--------|----------------|--------|
| CDK `LiteLLMStack` | the ECS Fargate Task Def injects env/secret, and grants the Task Role Bedrock/`InvokeGateway`/`aws-marketplace:Subscribe` permissions | `lib/litellm-stack.ts` |
| this pattern (image/config) | defines the routing/auth/Guardrail/MCP rules | `services/litellm/` |
| CDK `AgentCoreGatewayStack` (us-east-1) | hosts the built-in Web Search Tool connector (MCP, AWS_IAM) | `shared/patterns/agentcore-websearch.md` |
| SSO Token Service | issues virtual keys via `/key/generate` + grants `mcp_access_groups` | `lambda/token-service/` |

---

## Section 1: config.yaml — routing · auth · Guardrail · MCP

The full `services/litellm/config.yaml`:

```yaml
# LiteLLM gateway config. All dynamic values come from environment variables
# (injected by the ECS task definition) so the same image works across envs.
#
# Routing:
#   - Claude via bedrock/ (Anthropic Messages / Converse) -> bedrock-runtime (us-east-2)
#   - GPT-5.5 via bedrock_mantle/ (OpenAI Responses) -> bedrock-mantle (us-east-2)
#
# MCP (Tavily WebSearch):
#   - LiteLLM calls Tavily Runtime (us-east-1) directly via SigV4 (cross-region).
#   - No AgentCore Gateway in between (same-region constraint prevents it).

model_list:
  # Anthropic Claude Opus 4.8 (top performance, deep reasoning)
  - model_name: os.environ/CLAUDE_OPUS_MODEL
    litellm_params:
      model: os.environ/CLAUDE_OPUS_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Anthropic Claude Sonnet 4.6 (balanced, default coding model)
  - model_name: os.environ/CLAUDE_SONNET_MODEL
    litellm_params:
      model: os.environ/CLAUDE_SONNET_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Anthropic Claude Haiku 4.5 (fastest and cheapest)
  - model_name: os.environ/CLAUDE_HAIKU_MODEL
    litellm_params:
      model: os.environ/CLAUDE_HAIKU_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Anthropic Claude Fable 5 (Mythos-class, 1M context, autonomous coding)
  - model_name: os.environ/CLAUDE_FABLE_MODEL
    litellm_params:
      model: os.environ/CLAUDE_FABLE_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Bedrock Mantle models (GPT-5.5 / GPT-5.4) — SigV4 via the ECS Task Role, exactly
  # like Claude above (NO api_key → no bearer token, nothing to rotate). SigV4 on the
  # bedrock_mantle responses route is provided by the #29788 overlay in the Dockerfile.
  # No guardrails: Bedrock Guardrails are bedrock-runtime only and are not compatible
  # with bedrock_mantle.
  #
  # ⚠️ REGION PINNING (do NOT use MANTLE_REGION — it is NOT read by LiteLLM):
  #   The bedrock_mantle provider (#29788 `_resolve_region`) reads region from, in order:
  #     1) litellm_params.aws_region_name  2) BEDROCK_MANTLE_API_BASE host  3) BEDROCK_MANTLE_REGION
  #     4) AWS_REGION_NAME  5) AWS_REGION  6) default.
  #   So GPT models MUST set aws_region_name to the Mantle region (us-east-1), and the CDK
  #   MUST inject env BEDROCK_MANTLE_REGION=us-east-1 AND
  #   BEDROCK_MANTLE_API_BASE=https://bedrock-mantle.us-east-1.api.aws so both the endpoint
  #   host and the SigV4 scope are pinned to us-east-1. If you only set aws_region_name and it
  #   does not propagate on the chat→responses bridge, the provider falls back to AWS_REGION
  #   (the gateway region) → "Cannot connect to host bedrock-mantle.<gw-region>.api.aws".
  #   `MANTLE_REGION` is a documentation alias only; it is NOT consumed by the provider.
  # (gpt-oss-120b is intentionally NOT offered: it uses the mantle chat/completions
  # route, which the #29788 overlay does not SigV4-sign, and Codex dropped wire_api=chat.)

  # OpenAI GPT-5.5 (Bedrock Mantle, OpenAI Responses API)
  - model_name: os.environ/GPT55_MODEL
    litellm_params:
      model: os.environ/GPT55_BACKEND
      aws_region_name: os.environ/BEDROCK_MANTLE_REGION   # us-east-1 (NOT the gateway region)

  # OpenAI GPT-5.4 (Bedrock Mantle, OpenAI Responses API)
  - model_name: os.environ/GPT54_MODEL
    litellm_params:
      model: os.environ/GPT54_BACKEND
      aws_region_name: os.environ/BEDROCK_MANTLE_REGION   # us-east-1 (NOT the gateway region)

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  proxy_base_url: os.environ/PROXY_BASE_URL

litellm_settings:
  drop_params: true
  drop_params_if_unset: true
  modify_params: true
  # ⚠️ `callbacks` value MUST be a litellm CustomLogger SUBCLASS INSTANCE, not a bare
  #    function. A plain function fails at request time with
  #    "'function' object has no attribute 'async_post_call_success_hook'".
  #    callbacks/user_trace.py must define e.g.:
  #        from litellm.integrations.custom_logger import CustomLogger
  #        class UserTrace(CustomLogger): ...
  #        user_trace_callback = UserTrace()
  #    When enableLangfuse=false, OMIT the callbacks / success_callback / failure_callback /
  #    langfuse_default_tags lines entirely (the user_trace callback + langfuse sinks are only
  #    meaningful with Langfuse on; LiteLLM still logs user_api_key_* natively).
  callbacks: user_trace.user_trace_callback
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  langfuse_default_tags: ["user_api_key_user_id", "user_api_key_alias"]

# AgentCore Web Search Tool — built-in connector via AgentCore Gateway (us-east-1).
# LiteLLM signs MCP calls with SigV4 (service bedrock-agentcore, InvokeGateway).
# WEBSEARCH_GATEWAY_URL / WEBSEARCH_GATEWAY_REGION are injected as env vars.
mcp_servers:
  websearch:
    url: os.environ/WEBSEARCH_GATEWAY_URL
    transport: "http"
    auth_type: "aws_sigv4"
    aws_region_name: os.environ/WEBSEARCH_GATEWAY_REGION   # us-east-1
    aws_service_name: "bedrock-agentcore"
    # Scoped access (not public): tag this server into the "default_tools" access
    # group. The SSO Token Service grants each virtual key this group at /key/generate
    # (object_permission.mcp_access_groups), so access is per-key/auditable. To expose
    # a new MCP server to all users, just add it to this access group — no token
    # service change needed.
    access_groups: ["default_tools"]

mcp_settings:
  require_approval: "never"

# Guardrails — 3-layer defense (all default_on for every request)
guardrails:
  # Layer 1: Secret/API key detection (LiteLLM built-in)
  - guardrail_name: "secret-detection"
    litellm_params:
      guardrail: "hide-secrets"
      mode: "pre_call"
      default_on: true

  # Layer 2: Bedrock Guardrails (content filter + PII + denied topics)
  # Note: only compatible with bedrock-runtime models (Claude). Not mantle (GPT-5.5).
  - guardrail_name: "bedrock-content-filter"
    litellm_params:
      guardrail: bedrock
      mode: "during_call"
      default_on: false
      guardrailIdentifier: os.environ/BEDROCK_GUARDRAIL_ID
      guardrailVersion: os.environ/BEDROCK_GUARDRAIL_VERSION
```

### 1.1 `model_list` — two kinds of backends, one auth model

The `os.environ/XXX` syntax makes LiteLLM pull the value from an environment variable at startup.
**WHY**: by externalizing both the model name (`model_name`, the alias clients call) and the actual backend
(`model`, e.g. `bedrock/anthropic.claude-...`) into env, you can do a model version upgrade by just changing
the Task Definition env, without rebuilding the image.

Two routes coexist:

- **Claude** → `bedrock/` prefix → `bedrock-runtime` (Anthropic Messages/Converse API). Each entry explicitly sets
  `guardrails: ["bedrock-content-filter"]`.
- **GPT-5.5 / GPT-5.4** → `bedrock_mantle/` prefix → `bedrock-mantle` (OpenAI Responses API).
  **No guardrails key at all.**

**WHY — neither has an api_key**: neither Claude nor GPT places `api_key` in `litellm_params`.
When there is no api_key, LiteLLM **signs the request with the ECS Task Role credentials via SigV4**. That is, a bearer
token does not exist, so **there is no secret to rotate**. This is the heart of the gateway's auth model.
The SigV4 signing for the mantle route (`/openai/v1/responses`) is not in vanilla LiteLLM; it is provided by the
PR #29788 overlay applied in the Dockerfile (see Section 2).

**Pitfall — do not add gpt-oss-120b**: as the comment notes, `gpt-oss-120b` is intentionally excluded.
This model uses mantle's `chat/completions` route, which the #29788 overlay does not SigV4-sign,
and Codex removed `wire_api=chat`. When adding a mantle model, **only models using the responses route**
are safe.

### 1.2 `general_settings` / `litellm_settings`

- `master_key` / `database_url` / `proxy_base_url` — all env. `master_key` comes from Secrets Manager, and
  `database_url` is assembled by the entrypoint (Section 2).
- `drop_params` / `drop_params_if_unset` / `modify_params: true` — **WHY**: Claude and GPT accept different
  parameter specs. By simply dropping incompatible parameters, the two models can be handled by the same client call.
- `callbacks: user_trace.user_trace_callback` — a custom callback (`callbacks/user_trace.py`, bundled in the image)
  injects the SSO user identity into traces.
- `success_callback` / `failure_callback: ["langfuse"]` + `langfuse_default_tags` — observe every request via
  Langfuse. **WHY**: thanks to the `user_api_key_user_id`·`user_api_key_alias` tags, each LLM call can be traced
  back from the issued virtual key → the SSO user (the core of governance/auditing).

### 1.3 `mcp_servers.tavily` — scoped MCP access

- `auth_type: "aws_sigv4"` + `aws_service_name: "bedrock-agentcore"` — **WHY**: the Tavily MCP is
  hosted on AgentCore Runtime, and LiteLLM calls it directly via **cross-region SigV4**. The URL is the Runtime's
  invoke endpoint (env-injected). There is no AgentCore Gateway in between, because the Gateway has a same-region
  constraint and cannot bridge the Runtime (us-east-1) and LiteLLM (us-east-2) (see the top comment).
- `access_groups: ["default_tools"]` — **WHY**: this MCP is not public. It is tagged into the `default_tools`
  access group, and the SSO Token Service grants this group to each virtual key at `/key/generate`
  (`object_permission.mcp_access_groups`). Therefore access is **auditable per key**. To expose a new MCP
  server to all users, just add it to this access group, and **there is no need to change the token service**.
- `mcp_settings.require_approval: "never"` — no human approval step on a tool call (autonomous agent execution).

### 1.4 Guardrails — 3-layer defense and its boundary

The config declares two guardrails, but **combined with the application at the model entries, it becomes 3 layers**:

1. **Layer 1 — `secret-detection` (`hide-secrets`, `pre_call`, `default_on: true`)**: LiteLLM built-in.
   For **every request**, it detects/masks secrets/API keys before the call. **WHY**: it blocks at the gateway
   any credentials a developer accidentally pasted into the code agent from being sent to the model.
2. **Layer 2 — `bedrock-content-filter` (`guardrail: bedrock`, `during_call`)**: Bedrock Guardrails
   (content filter + PII + denied topics). `guardrailIdentifier`/`guardrailVersion` are env-injected.
3. **Layer 3 — per-model application**: Layer 2 is `default_on: false` in the config, but it is **explicitly applied
   via `guardrails: ["bedrock-content-filter"]` on each Claude model entry**. That is, Claude calls always go through
   the content filter.

**Important — no Guardrail for GPT (mantle)**: `bedrock-content-filter` is **only for `bedrock-runtime` models
(Claude)**. Since the Bedrock Guardrails API is not compatible with `bedrock_mantle`, the GPT-5.5/5.4 model entries
do not have a `guardrails` key (also stated in the comment). As a result:

| Model | Layer 1 hide-secrets | Layer 2 bedrock-content-filter |
|------|:--:|:--:|
| Claude (bedrock/) | ✅ (pre_call, global) | ✅ (explicit on the model entry) |
| GPT-5.5/5.4 (bedrock_mantle/) | ✅ (pre_call, global) | ❌ (bedrock-runtime only, not applied) |

**Pitfall**: if you try to force the content filter on the GPT route by adding `guardrails` to the mantle model
entry, the call breaks. If you need content control over mantle, you must use a mechanism other than Bedrock Guardrails.

---

## Section 2: Dockerfile + entrypoint.sh — ARM64 image and the #29788 overlay

### 2.1 Dockerfile

The full `services/litellm/Dockerfile`:

```dockerfile
# LiteLLM proxy image for ECS Fargate (ARM64/Graviton).
# v1.89.0-rc.1: includes bedrock_mantle responses API routing (#29490).
FROM ghcr.io/berriai/litellm:v1.89.0-rc.1

# Overlay PR #29788 (SigV4 for the bedrock_mantle /openai/v1/responses route) onto
# the rc.1 base. Verified to be the ONLY delta vs rc.1 for these 3 files, and
# backward-compatible (Bearer still used when api_key present; SigV4 only when absent).
# TEMPORARY: remove once a LiteLLM release tag containing #29788 (commit 2c95d0b) ships.
COPY litellm_overlay/ /tmp/litellm_overlay/
RUN set -e; \
    for D in $(find /app /usr /root -type d -path '*/litellm/llms' 2>/dev/null); do \
      echo "overlaying #29788 into: $D"; \
      cp -r /tmp/litellm_overlay/llms/* "$D/"; \
      find "$D" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true; \
    done; \
    SP="$(/app/.venv/bin/python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')"; \
    grep -q "Bedrock Mantle auth failed" "$SP/litellm/llms/bedrock_mantle/responses/transformation.py" || { echo "RUNTIME COPY NOT PATCHED: $SP"; exit 1; }; \
    echo "runtime litellm patched (#29788) at $SP"

# Bundle config, callback, and entrypoint.
COPY callbacks/user_trace.py /app/user_trace.py
COPY config.yaml /app/config.yaml
COPY entrypoint.sh /app/entrypoint.sh

EXPOSE 4000

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
```

**WHY, item by item**:

- **`FROM ghcr.io/berriai/litellm:v1.89.0-rc.1`** — the base for ARM64/Graviton. This rc.1 tag already
  includes PR #29490 (bedrock_mantle **responses API routing**). That is, the routing that sends GPT to mantle
  responses is in the base, but the **SigV4 authentication** for that route is not yet there.
- **#29788 overlay** — overlays the one missing piece in the base (SigV4 signing of the
  `bedrock_mantle /openai/v1/responses` route). It copies the files from `litellm_overlay/llms/` into every
  `*/litellm/llms` path found inside the image. **WHY copy to multiple paths**: litellm may be installed in
  multiple locations across the build stage and the runtime venv, so it applies uniformly to every `llms`
  directory found.
- **Abort the build if the grep verification fails** — `grep -q "Bedrock Mantle auth failed" ... || { ...; exit 1; }`.
  **WHY**: it verifies that the patch made it into the `transformation.py` of the venv (`purelib`) that the runtime
  actually imports. If the overlay was applied only to the wrong directories and the runtime venv stays unpatched,
  it fails the build immediately to **completely prevent an unpatched image from being deployed**. (Pitfall: remove
  this grep and GPT calls silently break at runtime with "auth failed".)
- **Backward compatibility** — as the comment says, #29788 uses Bearer if `api_key` is present, SigV4 if absent.
  Our config has no api_key, so it takes the SigV4 path.
- **TEMPORARY** — once an official release tag containing #29788 (commit 2c95d0b) ships, the entire overlay must be
  removed. This is the first thing to check when upgrading the base tag.
- **Bundled files** — includes `user_trace.py` (SSO identity callback), `config.yaml` (Section 1), and
  `entrypoint.sh` in the image.
- **`EXPOSE 4000`** — the LiteLLM Proxy port. Must match the ALB target port.

### 2.2 entrypoint.sh

The full `services/litellm/entrypoint.sh`:

```sh
#!/bin/sh
# LiteLLM container entrypoint.
#
# Auth model (all SigV4 via the ECS Task Role — no tokens, nothing to rotate):
#   - Claude (bedrock/*) and Bedrock Mantle (GPT-5.5 / GPT-5.4) are all
#     defined in config.yaml with NO api_key, so LiteLLM signs requests with the Task
#     Role credentials. SigV4 on the bedrock_mantle responses route comes from the
#     #29788 overlay baked into the image (see Dockerfile).
#
# DATABASE_* and LITELLM_MASTER_KEY are injected by the ECS task definition (Secrets
# Manager); AWS_REGION and the model name/backend vars are plain env.

export DATABASE_URL="postgresql://${DATABASE_USER}:${DATABASE_PASSWORD}@${DATABASE_HOST}:5432/litellm"

exec litellm --config /app/config.yaml --port 4000 --num_workers 2
```

**WHY, item by item**:

- **`DATABASE_URL` assembly** — the Aurora connection info (`DATABASE_USER`/`DATABASE_PASSWORD`/`DATABASE_HOST`)
  is injected by the Task Definition **as individual pieces** from Secrets Manager. The entrypoint combines them into
  a single postgres URL and passes it to `config.yaml`'s `database_url: os.environ/DATABASE_URL`. **WHY assemble**:
  it lets pieces like the password be managed as separate secrets while still satisfying the single DSN format LiteLLM
  expects. The port `5432` and DB name `litellm` are fixed.
- **`exec litellm ... --num_workers 2`** — `exec` hands PID 1 to LiteLLM so that container signals (SIGTERM) are
  delivered straight to the process (graceful shutdown). Two workers provide concurrency.
- **Re-confirming tokenless auth** — as the top comment says, both Claude and mantle have no api_key, so they are
  signed via **Task Role SigV4**. The entrypoint handles no LLM credentials. The only secrets the gateway uses are the
  DB connection info and `LITELLM_MASTER_KEY`.

**Cross-layer**: `DATABASE_*` and `LITELLM_MASTER_KEY` are injected by the Task Definition in `lib/litellm-stack.ts`
from Secrets Manager (the Aurora secret in `lib/data-stack.ts`), and the Task Role's Bedrock permissions enable the
SigV4 calls. That is, this image works only on top of the contract "permissions from the Task Role, values from env".

---

## Section 3: tavily-mcp/server.py — an MCP server on AgentCore Runtime

The full `services/tavily-mcp/server.py`:

```python
"""
Tavily MCP server — exposes web search as MCP tools, hosted on AgentCore Runtime.

AgentCore Runtime expects the MCP server at 0.0.0.0:8000/mcp (stateless HTTP).
The Tavily API key is read from Secrets Manager at startup (secret name in
TAVILY_SECRET_NAME); falls back to TAVILY_API_KEY env for local runs.

Owned by: DevOps.
"""
import json
import os
from typing import Any

import boto3
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

_TAVILY_URL = "https://api.tavily.com/search"
_api_key_cache: str | None = None


def _get_api_key() -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key:
        _api_key_cache = env_key
        return _api_key_cache
    secret_name = os.environ.get("TAVILY_SECRET_NAME")
    if not secret_name:
        raise RuntimeError("TAVILY_SECRET_NAME or TAVILY_API_KEY must be set")
    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=secret_name)["SecretString"]
    try:
        _api_key_cache = json.loads(raw).get("api_key", raw)
    except json.JSONDecodeError:
        _api_key_cache = raw
    return _api_key_cache


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the web with Tavily and return ranked results with snippets.

    Args:
        query: The search query.
        max_results: Number of results to return (1-10).
    """
    payload = {
        "api_key": _get_api_key(),
        "query": query,
        "max_results": max(1, min(max_results, 10)),
        "search_depth": "basic",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.post(_TAVILY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return {
        "query": query,
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
            for r in data.get("results", [])
        ],
        "answer": data.get("answer"),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

**WHY, item by item**:

- **`FastMCP(host="0.0.0.0", stateless_http=True)`** — **WHY 0.0.0.0**: AgentCore Runtime expects the MCP
  server at `0.0.0.0:8000/mcp` over stateless HTTP. Since it must be reachable from outside the container, it binds to
  `0.0.0.0` rather than loopback. **WHY stateless**: the Runtime is serverless and runs each request in isolation, so
  stateless HTTP that keeps no session state on the server matches the hosting model.
- **`_get_api_key()` — Secrets Manager priority and caching** — it first reads the `TAVILY_API_KEY` env (for local
  runs), and if absent reads from the Secrets Manager secret pointed to by `TAVILY_SECRET_NAME`. **WHY**: in production
  the key is not left in code/env as plaintext but stored in Secrets Manager. The secret string can be JSON
  (`{"api_key": ...}`) or plaintext, so after `json.loads` it tries the `api_key` key and on failure
  (`JSONDecodeError`) uses the raw text as the key (accepting both storage formats). The `_api_key_cache` global avoids
  a Secrets Manager round-trip on every call (only one lookup per cold start).
- **The `web_search` tool** — exposed as an MCP tool via `@mcp.tool()`. **WHY `max(1, min(max_results, 10))`**:
  clamps the caller-provided result count to 1–10 to protect the Tavily call from abnormal input. The response returns
  only title/url/content and the summary `answer`, normalized into a form an agent can use directly.
- **`httpx.Client(timeout=20)` + `raise_for_status()`** — sets an explicit timeout to prevent stuck requests, and
  raises HTTP errors as exceptions so failures are not hidden.
- **`mcp.run(transport="streamable-http")`** — starts with the streamable HTTP transport that AgentCore Runtime
  expects.

**Cross-layer — who calls whom**:

1. `services/tavily-mcp/` is hosted serverless on **AgentCore Runtime** (`lib/agentcore-runtime-stack.ts`)
   (us-east-1).
2. **LiteLLM** (us-east-2) calls this Runtime directly via **cross-region SigV4**
   (`aws_service_name: bedrock-agentcore`) through `mcp_servers.tavily` in config.yaml.
3. Which virtual keys may use this tool is controlled by the `default_tools` access group and the SSO Token Service's
   key issuance (Section 1.3).

**Pitfall**: this server does no authentication of its own. Access control happens in two places: (a) the AgentCore
Runtime's IAM inbound and (b) the LiteLLM access group. Do not try to put auth logic into server.py — it breaks the
separation of responsibilities.

---

## Summary — what this pattern guarantees

- **Single entry point**: clients call Claude/GPT at the same LiteLLM endpoint.
- **No token to rotate**: every LLM·MCP call uses ECS Task Role SigV4. The only secrets are the DB connection info and the master key.
- **Governance per request**: hide-secrets (global) + Bedrock content filter (Claude only) + Langfuse tracing.
- **Environment-agnostic image**: all dynamic values are env. A model upgrade is just a Task Definition env change.
- **Explicit boundary**: that GPT (mantle) cannot receive a Bedrock Guardrail is nailed down consistently by the config,
  the code, and this document.
