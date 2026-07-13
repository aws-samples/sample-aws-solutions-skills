# Pattern: LiteLLM Governance Gateway (config / image / MCP)

> **Reflects current architecture (v1.2)**: web search uses the **AgentCore Web Search Tool** (built-in connector) — Tavily has been removed entirely → `shared/patterns/agentcore-websearch.md`. GPT-5.x (Mantle) is reached in **us-east-1** over cross-region VPC peering, routed via `BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE`=us-east-1 → `shared/patterns/mantle-peering.md`.
>
> ⚠️ **Auth is NOT uniform.** Claude (`bedrock/`) is tokenless SigV4 via the ECS Task Role. **Mantle (`bedrock_mantle/`, GPT-5.x) is Bearer-token — it has no SigV4 path** on the Responses route (verified by extracting the actual installed source from the pinned image; the earlier "SigV4 shipped in v1.87.2 / #29788" claim was **false** and is retracted). A short-term Bedrock API key is minted at runtime from the Task Role's own credentials by a LiteLLM callback and kept fresh in-process — no long-term secret, no scheduler. It **must** go in `BEDROCK_MANTLE_API_KEY`, never `AWS_BEARER_TOKEN_BEDROCK` (boto3-reserved; setting it breaks Claude's SigV4). See `shared/reference/constraints.md` → "LiteLLM image + Mantle Bearer-token auth".

This pattern teaches how to configure the **LiteLLM Proxy** as a single governance gateway.
It reproduces `services/litellm/` of the reference solution verbatim. The gateway handles all of the following in one place:

- **Model routing** — Claude (`bedrock/`) + GPT-5.x (`bedrock_mantle/`, `BEDROCK_MANTLE_REGION`/`BEDROCK_MANTLE_API_BASE`=us-east-1) behind a single OpenAI/Anthropic-compatible endpoint
- **Authentication** — Claude: SigV4 (Task Role, tokenless). Mantle: runtime-minted short-term Bearer key (Task Role → `aws-bedrock-token-generator` → `BEDROCK_MANTLE_API_KEY`, refreshed by a callback)
- **Guardrails** — layered defense per request (Bedrock content filter is Claude-only)
- **MCP (WebSearch)** — calls the AgentCore Gateway's built-in Web Search Tool via cross-region SigV4 (`bedrock-agentcore`, `InvokeGateway`)

Core design principle: **inject all dynamic values via environment variables** so the same image works
across all environments (dev/prod); config.yaml never hardcodes secrets or environment-specific values. The values
are injected by the ECS Task Definition (Secrets Manager + plaintext env).

Cross-layer mapping:

| Layer | What it provides | Where |
|--------|----------------|--------|
| CDK `LiteLLMStack` | the ECS Fargate Task Def injects env/secret, and grants the Task Role Bedrock (Claude) / bedrock-mantle / `InvokeGateway` / `aws-marketplace:Subscribe` permissions | `lib/litellm-stack.ts` |
| this pattern (image/config) | defines the routing/auth/Guardrail/MCP rules + the Mantle token-refresh callback | `services/litellm/` |
| CDK `AgentCoreGatewayStack` (us-east-1) | hosts the built-in Web Search Tool connector (MCP, AWS_IAM) | `shared/patterns/agentcore-websearch.md` |
| Token Service | issues virtual keys via `/key/generate` + grants `mcp_access_groups` | `lambda/token-service/` |

---

## Section 1: config.yaml — routing · auth · Guardrail · MCP

The full `services/litellm/config.yaml`:

```yaml
# LiteLLM gateway config. All dynamic values come from environment variables
# (injected by the ECS task definition) so the same image works across envs.
#
# Routing:
#   - Claude (Opus 4.8 / Sonnet 5 / Fable 5 / Haiku 4.5) via bedrock/ (Anthropic
#     Messages/Converse) -> bedrock-runtime (gateway region)
#   - GPT-5.5 / GPT-5.4 via bedrock_mantle/ (OpenAI Responses) -> bedrock-mantle
#     (us-east-1, reached over cross-region VPC peering)
#
# Auth: Claude (bedrock/) is tokenless — the ECS Task Role signs every request
# with SigV4, nothing to rotate. GPT-5.5/5.4 (bedrock_mantle/) is DIFFERENT:
# that route has no SigV4 support (verified against the actual installed source)
# and requires a Bearer token. BEDROCK_MANTLE_API_KEY is minted at runtime from
# the same Task Role's credentials and kept fresh by
# callbacks/mantle_token_refresh.py -- still no long-term secret, but not
# literally "tokenless" like Claude.
#
# MCP (AgentCore Web Search):
#   - LiteLLM calls the AgentCore Gateway's built-in Web Search Tool connector
#     (us-east-1) directly via cross-region SigV4 (bedrock-agentcore service).

model_list:
  # Anthropic Claude Opus 4.8 (top performance, deep reasoning)
  - model_name: os.environ/CLAUDE_OPUS_MODEL
    litellm_params:
      model: os.environ/CLAUDE_OPUS_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Anthropic Claude Sonnet 5 (balanced, default coding model)
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

  # Anthropic Claude Fable 5 (Mythos-class) — see constraints.md: requires the
  # account data-retention mode `provider_data_share` (per region) or calls are
  # blocked; that is a GATE-1 opt-in (30-day Anthropic retention + human review).
  - model_name: os.environ/CLAUDE_FABLE_MODEL
    litellm_params:
      model: os.environ/CLAUDE_FABLE_BACKEND
      aws_region_name: os.environ/AWS_REGION
      guardrails: ["bedrock-content-filter"]

  # Bedrock Mantle models (GPT-5.5 / GPT-5.4) — Bearer token auth (NOT SigV4;
  # verified against the actual installed source of this image tag). No
  # api_key is set here in litellm_params on purpose: validate_environment()
  # in bedrock_mantle/responses/transformation.py falls back to
  # get_secret_str("BEDROCK_MANTLE_API_KEY"), which re-reads the live process
  # environment on every call (no caching) -- so callbacks/mantle_token_refresh.py
  # can keep it fresh in-process without ever needing a LiteLLM restart.
  # NO guardrails key: Bedrock Guardrails are bedrock-runtime only, not Mantle.
  #
  # REGION PINNING (do NOT rely on MANTLE_REGION — it is NOT read by LiteLLM):
  #   The bedrock_mantle provider's _resolve_region reads region from, in order:
  #     1) litellm_params.aws_region_name  2) BEDROCK_MANTLE_API_BASE host
  #     3) BEDROCK_MANTLE_REGION  4) AWS_REGION_NAME  5) AWS_REGION  6) default.
  #   So GPT models set aws_region_name to BEDROCK_MANTLE_REGION (us-east-1, NOT
  #   the gateway region), and the CDK injects env BEDROCK_MANTLE_REGION=us-east-1
  #   + BEDROCK_MANTLE_API_BASE=https://bedrock-mantle.us-east-1.api.aws.

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
  # ⚠️ each callback value MUST be a litellm CustomLogger SUBCLASS INSTANCE, not a
  #    bare function. Both callbacks below are instances (see callbacks/*.py).
  #    - user_trace: injects the SSO/Cognito user identity into traces.
  #    - mantle_token_refresh: mints/refreshes BEDROCK_MANTLE_API_KEY in-process.
  #    When enableLangfuse=false, OMIT success_callback/failure_callback/
  #    langfuse_default_tags (and user_trace if desired) — but KEEP
  #    mantle_token_refresh whenever any GPT/Mantle model is offered.
  callbacks: ["user_trace.user_trace_callback", "mantle_token_refresh.mantle_token_refresh_callback"]
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
    # group. The Token Service grants each virtual key this group at /key/generate
    # (object_permission.mcp_access_groups), so access is per-key/auditable. To
    # expose a new MCP server to all users, just add it to this access group — no
    # token service change needed.
    access_groups: ["default_tools"]

mcp_settings:
  require_approval: "never"

# Guardrails — layered defense
guardrails:
  # Layer 1: Secret/API key detection (LiteLLM built-in) — applies to ALL models
  # including GPT/Mantle (which has no Bedrock Guardrail coverage).
  - guardrail_name: "secret-detection"
    litellm_params:
      guardrail: "hide-secrets"
      mode: "pre_call"
      default_on: true

  # Layer 2: Bedrock Guardrails (content filter + PII + denied topics).
  # Only compatible with bedrock-runtime models (Claude). Not Mantle (GPT-5.5/5.4).
  - guardrail_name: "bedrock-content-filter"
    litellm_params:
      guardrail: bedrock
      mode: "during_call"
      default_on: false
      guardrailIdentifier: os.environ/BEDROCK_GUARDRAIL_ID
      guardrailVersion: os.environ/BEDROCK_GUARDRAIL_VERSION
```

### 1.1 `model_list` — two backends, two different auth models

The `os.environ/XXX` syntax makes LiteLLM pull the value from an environment variable at startup.
**WHY**: by externalizing both the alias (`model_name`) and the backend route (`model`) into env, you can do a
model version upgrade by changing the Task Definition env, without rebuilding the image.

Two routes coexist, and they authenticate **differently**:

- **Claude** → `bedrock/` prefix → `bedrock-runtime` (Anthropic Messages/Converse). No `api_key` → LiteLLM signs
  with the **ECS Task Role via SigV4**. Tokenless, nothing to rotate. Each entry sets `guardrails: ["bedrock-content-filter"]`.
- **GPT-5.5 / GPT-5.4** → `bedrock_mantle/` prefix → `bedrock-mantle` (OpenAI Responses). **No SigV4 path exists**
  on this route (verified against the installed source). No `api_key` is set in `litellm_params` **on purpose**:
  `validate_environment()` falls back to `get_secret_str("BEDROCK_MANTLE_API_KEY")`, which re-reads `os.environ`
  on every call, so the `mantle_token_refresh` callback (Section 3) keeps the key fresh in-process. **No `guardrails` key.**

**Backend model IDs** — Claude backends are `bedrock/global.anthropic.<model-id>` inference profiles. **Verify with
`aws bedrock list-inference-profiles`; do not assume a `us.` prefix** — recent (2026) models are `global.`-only and a
`us.` id returns `The provided model identifier is invalid.` (See `constants.ts` in `cdk-stacks.md`.)

**Pitfall — do NOT set `AWS_BEARER_TOKEN_BEDROCK`.** `validate_environment()` accepts either `BEDROCK_MANTLE_API_KEY`
or `AWS_BEARER_TOKEN_BEDROCK`, but the latter is a **boto3-reserved** name: setting it flips *every* bedrock-runtime
client in the process (including Claude's SigV4 route) to Bearer auth and 403s all Claude models. Always
`BEDROCK_MANTLE_API_KEY`.

**Pitfall — verify before adding another Mantle family** (grok/gemma/`gpt-oss`): each Mantle model family and route
(responses vs chat/completions) can differ; verify auth + route support against the pinned LiteLLM tag's actual source
before adding, don't assume GPT-5.x behavior generalizes.

### 1.2 `general_settings` / `litellm_settings`

- `master_key` / `database_url` / `proxy_base_url` — all env. `master_key` from Secrets Manager; `database_url`
  assembled by the entrypoint (Section 2); **`proxy_base_url` = the gateway URL (the `GatewayUrl` output = the ALB domain)** so the Admin UI SPA builds correct absolute redirects (this is the ONLY redirect mechanism — `--forwarded-allow-ips` does not exist in the pinned image's CLI; see Section 2).
- `drop_params` / `drop_params_if_unset` / `modify_params: true` — **WHY**: Claude and GPT accept different parameter
  specs; dropping incompatible params lets one client call target either model.
- `callbacks: ["user_trace.user_trace_callback", "mantle_token_refresh.mantle_token_refresh_callback"]` — two custom
  `CustomLogger` instances bundled in the image: `user_trace` injects the caller identity into traces;
  `mantle_token_refresh` mints/refreshes `BEDROCK_MANTLE_API_KEY` (Section 3). Both must be **instances**, not bare functions.
- `success_callback` / `failure_callback: ["langfuse"]` + `langfuse_default_tags` — observe every request via Langfuse;
  the `user_api_key_user_id`/`user_api_key_alias` tags trace each call back to the virtual key → the user (governance/audit).

### 1.3 `mcp_servers.websearch` — scoped MCP access (AgentCore Web Search)

- `auth_type: "aws_sigv4"` + `aws_service_name: "bedrock-agentcore"` — LiteLLM signs MCP calls to the **AgentCore
  Gateway's built-in Web Search Tool connector** (us-east-1) with cross-region SigV4 (`InvokeGateway`). The URL/region
  are env-injected from `AgentCoreGatewayStack`. No third-party API key, no self-hosted MCP runtime — queries never leave AWS.
- `access_groups: ["default_tools"]` — this MCP is not public. It is tagged into the `default_tools` access group, and
  the Token Service grants that group to each virtual key at `/key/generate` (`object_permission.mcp_access_groups`), so
  access is **auditable per key**. To expose a new MCP server to all users, add it to this access group — no token-service change.
- `mcp_settings.require_approval: "never"` — no human approval step on a tool call (autonomous agent execution).
- **Client-side note**: a developer's Claude Code must still register this MCP endpoint (`claude mcp add-json` +
  `headersHelper`); LiteLLM registering it server-side does not auto-enable it in the client. See `developer-onboarding.md`.

### 1.4 Guardrails — layered defense and its boundary

1. **Layer 1 — `secret-detection` (`hide-secrets`, `pre_call`, `default_on: true`)**: LiteLLM built-in, for **every
   request** (Claude **and** Mantle). Blocks credentials a developer accidentally pastes from reaching the model. This
   is the **only** content guard Mantle gets.
2. **Layer 2 — `bedrock-content-filter` (`guardrail: bedrock`, `during_call`)**: Bedrock Guardrails (content filter +
   PII + denied topics). `default_on: false` in config, applied **explicitly** via `guardrails: ["bedrock-content-filter"]`
   on each Claude model entry.

| Model | Layer 1 hide-secrets | Layer 2 bedrock-content-filter |
|------|:--:|:--:|
| Claude (bedrock/) | ✅ (pre_call, global) | ✅ (explicit on the model entry) |
| GPT-5.5/5.4 (bedrock_mantle/) | ✅ (pre_call, global) | ❌ (bedrock-runtime only, not applied) |

**Pitfall**: adding `guardrails` to a Mantle model entry breaks the call — Bedrock Guardrails are not compatible with
`bedrock_mantle`. For content control over Mantle, use a mechanism other than Bedrock Guardrails.

---

## Section 2: Dockerfile + entrypoint.sh — ARM64 image (installs the Mantle token generator)

> **v1.2 update**: the base image has **no `pip`** (uv-managed venv, pip stripped), so the one extra dependency
> (`aws-bedrock-token-generator`, needed to mint the Mantle Bearer key) is installed by copying the `uv` binary from
> its official image and running `uv pip install` into the existing venv. The earlier claim that Mantle used SigV4 and
> needed "no extra dependency" was wrong (see the header note).

### 2.1 Dockerfile

The full `services/litellm/Dockerfile`:

```dockerfile
# LiteLLM proxy image for ECS Fargate (ARM64/Graviton).
#
# IMPORTANT (verified by extracting the actual installed source from this exact
# image tag): the bedrock_mantle Responses API route
# (litellm/llms/bedrock_mantle/responses/transformation.py) does NOT support
# SigV4/IAM-role auth. Its validate_environment() requires a Bearer token
# (BEDROCK_MANTLE_API_KEY or AWS_BEARER_TOKEN_BEDROCK) and raises ValueError if
# neither is set -- there is no SigV4 code path in this file at all. GPT-5.5/5.4
# therefore need a short-term Bedrock API key, generated at runtime from the ECS
# Task Role's own credentials via aws-bedrock-token-generator (no long-term IAM
# user, no static secret) and refreshed by a LiteLLM callback before it expires
# (see callbacks/mantle_token_refresh.py).
FROM ghcr.io/berriai/litellm:v1.89.0-rc.1

# The base image's venv has no pip (uv-managed, pip stripped from the final
# layer). Pull the uv binary from its own official distroless image and use it
# to add the one extra dependency into the existing venv in place.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN /usr/local/bin/uv pip install --python /app/.venv/bin/python3 \
      aws-bedrock-token-generator==1.1.0

# Bundle config, callbacks, and entrypoint.
COPY callbacks/user_trace.py /app/user_trace.py
COPY callbacks/mantle_token_refresh.py /app/mantle_token_refresh.py
COPY config.yaml /app/config.yaml
COPY entrypoint.sh /app/entrypoint.sh

EXPOSE 4000

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
```

**WHY, item by item**:

- **`FROM ghcr.io/berriai/litellm:v1.89.0-rc.1`** — ARM64/Graviton base with the bedrock_mantle Responses route. Do
  **not** assume this route does SigV4 — it does not (see the file header). If you bump the tag, re-verify by extracting
  the real `transformation.py` from the new tag, not from release notes.
- **`COPY --from=ghcr.io/astral-sh/uv` + `uv pip install`** — the base image has no `pip` (`No module named pip`), so
  packages are added with `uv` into `/app/.venv`. This is the only supported way to add `aws-bedrock-token-generator`.
- **Bundled files** — `user_trace.py`, `mantle_token_refresh.py` (Section 3), `config.yaml` (Section 1), `entrypoint.sh`.
- **`EXPOSE 4000`** — the LiteLLM Proxy port; must match the ALB target port.

### 2.2 entrypoint.sh

The full `services/litellm/entrypoint.sh`:

```sh
#!/bin/sh
# LiteLLM container entrypoint.
#
# Auth model:
#   - Claude (bedrock/*) is SigV4 via the ECS Task Role — no tokens, nothing to
#     rotate.
#   - Bedrock Mantle (GPT-5.5 / GPT-5.4) does NOT support SigV4 on the Responses
#     API route (verified against the actual installed LiteLLM source). It needs
#     a Bearer token (BEDROCK_MANTLE_API_KEY), minted at runtime from this same
#     Task Role's credentials and kept fresh in-process by the
#     mantle_token_refresh LiteLLM callback (services/litellm/callbacks/) — still
#     no long-term secret checked in or stored anywhere.
#
# Edge: CloudFront is removed — the ALB is the edge (HTTPS:443 for acm, HTTP:80 for
# http), forwarding X-Forwarded-Proto/X-Forwarded-Host.
# ⚠️ Do NOT pass `--forwarded-allow-ips` (or FORWARDED_ALLOW_IPS env): the pinned
# image's litellm CLI does not have that option — the container dies instantly with
# `Error: No such option: --forwarded-allow-ips` (exitCode 2, verified against the
# actual image; the proxy_cli.py builds uvicorn args explicitly and reads neither).
# UI redirects therefore rely on PROXY_BASE_URL alone:
#   acm  → PROXY_BASE_URL = https://<custom-domain> (set by the stack; redirects correct)
#   http → PROXY_BASE_URL is empty; the /ui -> /ui/ 307 may come back as http://<host>
#          on the request host — cosmetic only (the API is unaffected).
# Before adopting ANY CLI flag from docs, verify it against the pinned image first:
#   docker run --rm --entrypoint litellm <image> --help

# DATABASE_* and LITELLM_MASTER_KEY are injected by the ECS task definition
# (Secrets Manager); AWS_REGION and the model name/backend vars are plain env.

export DATABASE_URL="postgresql://${DATABASE_USER}:${DATABASE_PASSWORD}@${DATABASE_HOST}:5432/litellm"

exec litellm --config /app/config.yaml --port 4000 --num_workers 2
```

**WHY, item by item**:

- **`DATABASE_URL` assembly** — the Aurora connection pieces (`DATABASE_USER`/`DATABASE_PASSWORD`/`DATABASE_HOST`) are
  injected individually from Secrets Manager; the entrypoint combines them into the single postgres DSN LiteLLM expects.
  Port `5432` and DB name `litellm` are fixed.
- **`exec litellm ... --num_workers 2`** — `exec` hands PID 1 to LiteLLM so SIGTERM reaches it directly (graceful
  shutdown). Two workers provide concurrency. **No `--forwarded-allow-ips`** — that flag does not exist in the pinned image's CLI and kills the container at boot (exitCode 2; a real-deploy incident — the circuit breaker retried 5x then rolled back). Verify every flag against the pinned image (`docker run --rm --entrypoint litellm <image> --help`) before putting it in the entrypoint. UI redirect correctness comes from `PROXY_BASE_URL` (acm); in http mode the missing header trust is a cosmetic-only redirect-scheme quirk. The `mantle_token_refresh` callback signs a fresh `BEDROCK_MANTLE_API_KEY` per request in-process (Section 3).

---

## Section 3: callbacks/mantle_token_refresh.py — sign a fresh `BEDROCK_MANTLE_API_KEY` per request

> ⚠️ **Do NOT cache the minted token on a timer.** An earlier version of this pattern minted a token
> with a requested TTL (10h) and cached it, refreshing on that TTL. It **broke in production** at
> ~6h40m with `401 "The security token included in the request is expired"`: the minted token is a
> SigV4-presigned artifact whose real lifetime is `min(requested TTL, remaining lifetime of the Task
> Role session that signed it)` — and Fargate Task Role sessions last ≤~6h. botocore rotates the
> session automatically, but a cached token string signed with the OLD session does not follow that
> rotation, and **no timer interval can be correct** because the callback cannot know when the signing
> session rotates. The fix (below, production-verified): sign a fresh token on every request from a
> once-initialized auto-refreshing credentials handle — signing is pure local HMAC (µs), per AWS's own
> guidance ("It can be used for each API call as it is inexpensive").

The full `services/litellm/callbacks/mantle_token_refresh.py`:

```python
"""
LiteLLM custom callback: keeps BEDROCK_MANTLE_API_KEY fresh for the Bedrock
Mantle Responses API route (GPT-5.5 / GPT-5.4) by SIGNING A FRESH TOKEN ON
EVERY REQUEST -- no token caching, no TTL guessing, no background thread.

Why this exists: the bedrock_mantle Responses API transformation
(litellm/llms/bedrock_mantle/responses/transformation.py, verified by
extracting the actual installed source from the pinned image tag) has NO
SigV4/IAM-role code path -- it only accepts a Bearer token via
BEDROCK_MANTLE_API_KEY or AWS_BEARER_TOKEN_BEDROCK and raises ValueError if
neither is set.

CRITICAL: this callback intentionally sets BEDROCK_MANTLE_API_KEY, NOT
AWS_BEARER_TOKEN_BEDROCK, even though validate_environment() accepts either.
AWS_BEARER_TOKEN_BEDROCK is a name boto3's SDK itself recognizes globally: the
moment it is present in the process environment, EVERY boto3 bedrock-runtime
client -- including the Claude (bedrock/) route's SigV4 calls -- switches to
Bearer-token auth and starts rejecting the Task Role's SigV4 credentials
(verified by reproduction: it broke all four Claude models).
BEDROCK_MANTLE_API_KEY is not a name boto3 recognizes anywhere.

Design: per-request signing from a once-initialized botocore
RefreshableCredentials handle.
  - The handle is NOT a cached credential VALUE: each get_frozen_credentials()
    re-checks expiry and botocore transparently pulls a new session from the
    ECS container credential endpoint inside its refresh window. The atomic
    snapshot avoids an access_key/secret_key torn read across a rotation.
  - Signing is pure local HMAC (SigV4QueryAuth presign -- verified against
    aws-bedrock-token-generator 1.1.0 source: no network call), so per-request
    cost is microseconds.
  - The token is consumed milliseconds after signing (same request), so a
    "token outlives its signing session" scenario cannot occur.
  - We deliberately do NOT call provide_token() per request: its convenience
    path constructs a new botocore Session per call.

get_secret_str()/os.getenv() are called fresh on every request in this LiteLLM
build (verified: no caching for plain env vars), so updating os.environ here
takes effect immediately for the Mantle call this hook precedes.
"""

import logging
import os
import sys
import threading

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("mantle_token_refresh")
if not logger.handlers:
    # This LiteLLM build's logging config does not propagate third-party
    # module loggers to stdout -- without an explicit handler, logger.info()/
    # logger.exception() silently vanish (confirmed in production: zero log
    # lines over 7+ hours while tokens were expiring).
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s mantle_token_refresh %(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# NOT AWS_BEARER_TOKEN_BEDROCK -- see module docstring above for why.
_ENV_VAR = "BEDROCK_MANTLE_API_KEY"


class MantleTokenRefresh(CustomLogger):
    def __init__(self) -> None:
        self._init_lock = threading.Lock()
        self._credentials = None  # botocore RefreshableCredentials (auto-rotating handle)
        self._generator = None  # BedrockTokenGenerator (stateless signer)
        self._last_access_key: str = ""  # for logging session rotations only

    def _signing_materials(self):
        """Resolve the credential handle + signer exactly once (lazy, locked)."""
        if self._credentials is None:
            with self._init_lock:
                if self._credentials is None:
                    import botocore.session
                    from aws_bedrock_token_generator import BedrockTokenGenerator

                    creds = botocore.session.get_session().get_credentials()
                    if creds is None:
                        raise RuntimeError("no AWS credentials resolved (expected the ECS Task Role)")
                    self._generator = BedrockTokenGenerator()
                    self._credentials = creds
                    logger.info("initialized credential handle (provider=%s)", getattr(creds, "method", "unknown"))
        return self._generator, self._credentials

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Per-request: atomic credential snapshot (auto-refreshed by botocore
        # when the Task Role session nears expiry) + local SigV4 presign.
        try:
            generator, credentials = self._signing_materials()
            frozen = credentials.get_frozen_credentials()
            region = os.environ.get("BEDROCK_MANTLE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
            os.environ[_ENV_VAR] = generator.get_token(frozen, region)
            if frozen.access_key != self._last_access_key:
                # First mint, or the Task Role session just rotated (~6h cadence).
                logger.info(
                    "signing session %s (access key ...%s, region=%s)",
                    "initialized" if not self._last_access_key else "rotated",
                    frozen.access_key[-4:],
                    region,
                )
                self._last_access_key = frozen.access_key
        except Exception:  # noqa: BLE001 - never break a request over token minting
            logger.exception("failed to mint Bedrock API key for this request")
        return data


mantle_token_refresh_callback = MantleTokenRefresh()
```

**WHY, item by item**:

- **Sets `BEDROCK_MANTLE_API_KEY`, never `AWS_BEARER_TOKEN_BEDROCK`** — the single most important line. See the docstring
  and Hard Constraint #6 / #16: the reserved name would hijack Claude's SigV4 and cause a full Claude outage.
- **Per-request signing, no token cache** — the production lesson (header note above): a cached token cannot outlive the
  session that signed it, and no timer can track session rotation. Signing per request closes the gap structurally.
- **One credentials handle, reused** — `RefreshableCredentials` is an auto-rotating *handle*, not a value; botocore follows
  the ~6h ECS Task Role rotation on access. `get_frozen_credentials()` gives an atomic per-request snapshot. This is why
  reusing the handle is NOT "caching the token": the token is new every request, only the resolver is reused.
- **Explicit stdout handler** — this LiteLLM build swallows third-party module logs otherwise; the rotation log line
  (`signing session rotated`) is the observability signal that the fix is working in production.
- **Immediate effect via `os.environ`** — because this LiteLLM build re-reads plain env vars per request, updating
  `os.environ[_ENV_VAR]` is picked up by the Mantle call this hook precedes.

**Cross-layer**: the Task Role (in `lib/litellm-stack.ts`) needs `bedrock-mantle:CreateInference`/`GetInference`/
`GetProject`/`ListProjects` on `project/*` (**not** `foundation-model`) + `bedrock-mantle:CallWithBearerToken` on `*` +
`aws-marketplace:Subscribe` (first-call auto-subscribe). See `mantle-peering.md` and `constraints.md`.

---

## Summary — what this pattern guarantees

- **Single entry point**: clients call Claude/GPT at the same LiteLLM endpoint.
- **Claude is tokenless SigV4; Mantle is a runtime-minted short-term Bearer key** (`BEDROCK_MANTLE_API_KEY`, refreshed by a callback) — no long-term secret for either, but the two auth models are different and must not be conflated.
- **Governance per request**: hide-secrets (global, incl. Mantle) + Bedrock content filter (Claude only) + Langfuse tracing.
- **Environment-agnostic image**: all dynamic values are env; a model upgrade is just a Task Definition env change.
- **Web search is AWS-managed**: the AgentCore Gateway Web Search Tool via SigV4 — no third-party key, no self-hosted MCP.
- **Explicit boundary**: that GPT (Mantle) cannot receive a Bedrock Guardrail is nailed down consistently by the config, the code, and this document.
