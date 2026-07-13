# LiteLLM Admin Operations Guide (Admin UI + API)

Operator-facing guide for running the deployed gateway day-to-day: logging into the LiteLLM Admin UI,
creating users/teams (and mapping them to IdC auth units), checking request logs/traces, and applying per-user budgets.
This complements `shared/patterns/litellm-gateway.md` (config.yaml/routing/auth design) and
`shared/reference/sso-setup.md` (IdC provisioning) — this doc is about **operating** the already-deployed
LiteLLM proxy, mostly through its built-in Admin UI at `/ui/`.

> Everything here talks to the **LiteLLM proxy itself** (`POST/GET <gateway-url>/...` or the
> Admin UI at `<gateway-url>/ui/`, where `<gateway-url>` is the `GatewayUrl` output = the ALB domain — `https://…` for `certMode='acm'`, `http://…` for `http`) — not the AWS CDK stacks. No LiteLLM Enterprise license is
> required for any of this (Admin UI, teams, budgets are OSS features).

---

## 1. Logging into the LiteLLM Admin UI

### 1.1 Get the master key (the only Admin UI credential)

The proxy has exactly one admin credential: the **LiteLLM master key**, generated at deploy time and stored
in Secrets Manager (`masterKeySecret`, owned by `LiteLLMStack`). It is **never** printed in CDK outputs or
logs by design (Hard Constraint #4 — never hard-code/echo secrets).

```bash
# Find the secret (name/ARN comes from the LiteLLMStack outputs or Secrets Manager console)
aws secretsmanager list-secrets --query "SecretList[?contains(Name,'LiteLLM') && contains(Name,'Master')].Name" --output text

# Retrieve the value (requires secretsmanager:GetSecretValue — treat this like any other credential)
aws secretsmanager get-secret-value --secret-id <secret-name-or-arn> --query SecretString --output text
```

> Only operators who need admin access should have `secretsmanager:GetSecretValue` on this secret. Developers
> never need it — they authenticate via identity-backed virtual keys (`org-sso` or `cognito-native`), not the master key.

### 1.2 Open the Admin UI

```
<gateway-url>/ui/        # e.g. https://llmgw.example.com/ui/ (acm) or http://<alb-dns>/ui/ (http)
```

- Login form asks for the master key as the password (username field can be left as `admin` or blank
  depending on the LiteLLM version — the master key is what's checked).
- If you see a redirect to a dead/default host after login, that's the known redirect gotcha (Hard
  Constraint #8) — a client-visible symptom of a misconfigured `PROXY_BASE_URL` (the SPA's only
  absolute-URL source; the pinned image has no `--forwarded-allow-ips` option, so don't chase
  forwarded-header settings), not a credential problem. Fix at the infra level, not by retrying login.
- The UI is served by the same ECS Fargate task as the API — if `/ui/` 502s but `/v1/models` works, check
  the LiteLLM proxy's `--ui` flag / static assets rather than networking.

### 1.3 Master-key API access (no UI, for scripting)

Everything the UI does is also a REST call authenticated with `Authorization: Bearer <master-key>` against
the same gateway URL — useful for scripting the operations below instead of clicking through the UI:

```bash
export LITELLM_BASE=<gateway-url>   # https://… (acm) or http://<alb-dns> (http)
export MASTER_KEY=<value from Secrets Manager>

curl -s "$LITELLM_BASE/v1/models" -H "Authorization: Bearer $MASTER_KEY" | jq .
```

---

## 2. Creating teams and mapping them to SSO groups

In this gateway, **"users" are governed at the team level**, and team membership is derived automatically
from the caller's authorization unit — permission set in `org-sso`, Cognito User Pool Group in `cognito-native` — so you rarely create individual LiteLLM users by hand. Understand this
flow before creating anything manually in the UI, so you don't fight the automation.

### 2.1 The automatic path (recommended — authorization unit → team)

This is what actually provisions users/teams in this architecture, end to end:

1. An operator provisions the authorization unit per team, **named identically to the LiteLLM team it should map to**. In `org-sso`, this is a permission set assigned to a group (see `sso-setup.md`). In `cognito-native`, this is a Cognito User Pool Group whose name matches `teamGroupPrefix` (see `account-instance-setup.md`).
2. A developer authenticates with the selected mode and calls the gateway. The Token Lambda (`lambda/token-service/handler.py`) parses the permission set from the signed SSO ARN (`org-sso`) or reads the `cognito:groups` claim from the API-Gateway-verified Cognito JWT (`cognito-native`) and calls **lookup-or-create** against LiteLLM, **using that name directly as the `team_alias`** (no separate mapping table, no per-org branch in code):
   - `GET /team/list` → does a team with this `team_alias` already exist?
   - If not: `POST /team/new` with `team_alias` (= the permission set / group name), optional `models` (allowlist)
     and `max_budget` (cap) seeded from `TIER_CONFIG` if this team alias has an entry there, and the
     `object_permission.mcp_access_groups` for web-search access.
3. `POST /key/generate` issues the developer a **virtual key** scoped to that `team_id` — this is the
   "user" that shows up in the Admin UI's Keys/Usage views.

So: **creating a new tier/org = provisioning the IdC auth unit/group named the same as the LiteLLM team you want**, not clicking "New Team" in the UI first (though you can — see §2.2). The team is created lazily on
that group's first login if it doesn't already exist. Full worked example: `shared/examples/economy-tiering.md`.

### 2.2 Creating a team manually (UI or API) — when you need it upfront

Useful when you want the team to exist (with its budget/allowlist already set) **before** the first user
logs in, e.g. to pre-configure billing separation.

**Admin UI**: `/ui/` → **Teams** → **+ New Team** → set `Team Alias` **to exactly the authorization-unit name** (permission set for `org-sso`, Cognito User Pool Group name for `cognito-native`) — that name match is what connects the caller's identity to this team — optionally set
`Models` (allowlist) and `Max Budget`.

**API equivalent**:
```bash
curl -s -X POST "$LITELLM_BASE/team/new" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{
    "team_alias": "TeamResearch",
    "models": ["claude-sonnet-5", "claude-haiku-4-5"],
    "max_budget": 200.0,
    "object_permission": {"mcp_access_groups": ["default_tools"]}
  }' | jq .
```

> The `team_alias` string **is** the authorization-unit name — permission set in `org-sso`, Cognito User Pool Group name in `cognito-native`. It is the *only* join key between identity and LiteLLM.
> A typo in either one silently creates/resolves an unrelated team instead of erroring. If a developer's key
> isn't getting the budget/allowlist you expect, the first thing to check is whether the team you set up in
> the Admin UI (`GET /team/list`) has a
> `team_alias` that matches their IdC permission set or group display name **exactly**.

### 2.3 Creating an individual key/user manually (rare — SSO users never need this)

Only relevant for a non-developer "admin test" credential or a service account outside the SSO flow (Hard
Constraint #9's "Quick admin test (no SSO)" path uses the **master key** directly, not a generated key, so
even that doesn't need this). If you do need a standalone key:

```bash
curl -s -X POST "$LITELLM_BASE/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "<team_id from /team/list>", "key_alias": "ops-smoke-test", "max_budget": 5.0}' | jq .
```
Or via the UI: **Virtual Keys** → **+ Create New Key** → pick the **Team**, set an optional per-key budget.

---

## 3. Checking logs / traces

Three layers, from "did it get called at all" to "what exactly did the model see":

### 3.1 LiteLLM Admin UI — Usage / Logs tab

`/ui/` → **Usage** (or **Logs**, depending on version) shows recent requests: model, team/key, status,
tokens, cost, latency. This is the fastest way to answer "is the gateway receiving traffic" and "which
team/key is calling which model" without leaving the browser.

- Filter by team or key to isolate one developer/org's traffic.
- A request that never appears here (but the client got an error) means it failed **before** reaching
  LiteLLM — check the SSO Token Service / ALB layer instead (see §4 CloudWatch).
- A request that appears with a 4xx (e.g. budget/allowlist rejection) confirms LiteLLM-level governance is
  working as designed — this is not an infra bug.

### 3.2 Langfuse (optional, prompt/response trace level) — only if `enableLangfuse=true` (requires `certMode='acm'`)

If Observability was answered "Langfuse" in Phase 1, `LangfuseStack` self-hosts Langfuse (own Aurora-backed
DB, own admin login via Secrets Manager) behind **its own internet-facing ALB + ACM cert** and LiteLLM is wired to send traces to it (`success_callback`/
`failure_callback` in `config.yaml`). Langfuse gives you the **full prompt + response + intermediate steps**
per call, not just the summary row the LiteLLM UI shows — use it when you need to debug *why* a model
answered a certain way, not just *that* it was called. (Langfuse is deployed **only** with `certMode='acm'`;
`http` deploys are CloudWatch-only.)

```
https://<langfuse-acm-domain>/
```
Login credentials are the Langfuse admin secret (Secrets Manager, same pattern as the LiteLLM master key —
see `cdk-stacks.md` → `LangfuseExports`). Trace-level detail: per-request spans, token counts per step, and
(if enabled) the raw prompt/completion text — be mindful this may include sensitive customer content;
scope access to this UI accordingly.

If Langfuse was not enabled, this layer doesn't exist — rely on §3.1 + §3.3 only. Don't assume a Langfuse
domain exists before checking `config.enableLangfuse` in the deployed `config/dev.json`.

### 3.3 CloudWatch — infrastructure-level logs (everything LiteLLM doesn't see)

For requests that fail before/outside LiteLLM (SSO auth rejection, ALB errors, Lambda errors in
the Token Service, ECS task crash-loop):

```bash
# ECS/Fargate LiteLLM container logs (proxy stdout/stderr — startup errors, model routing errors, etc.)
aws logs tail /ecs/<litellm-log-group-name> --follow

# Token Service Lambda logs (SSO ARN parsing, team resolution, /key/generate failures)
aws logs tail /aws/lambda/<token-service-function-name> --follow

# ALB access — enable ALB access logging to S3 (PROD) and query via Athena at the failure timestamp
```

Use this layer when the symptom is a **403/500 the developer sees before any model call happens**, or when
you need to confirm which permission set (`org-sso`) or Cognito User Pool Group (`cognito-native`) a specific caller resolved to (the Token Lambda logs the
resolved `team`/`team_id` on each call — see `_resolve_team_id` in
`shared/examples/economy-tiering.md`).

### Decision guide — which layer to check first

| Symptom | Check first |
|---|---|
| "Is my traffic reaching the gateway at all?" | LiteLLM UI → Usage (§3.1) |
| "Why did the model answer that way?" / need the actual prompt | Langfuse (§3.2, if enabled) |
| "I get a 403 before any model call" | CloudWatch: Token Service Lambda logs (§3.3) |
| "The whole gateway is down / 5xx on every call" | CloudWatch: ECS/LiteLLM container logs (§3.3) |
| "Budget/allowlist rejected my call" | LiteLLM UI → Usage (shows the rejection) — this is expected governance, not a bug |

---

## 4. Applying a budget to a user

"User" in this gateway = **virtual key**, scoped to a **team**. Budgets can be set at either level; team-level
is the primary governance lever (matches the SSO-driven tiering design), per-key is for a one-off override.

### 4.1 Team-level budget (the primary mechanism — applies to everyone in that IdC group/permission set)

This is what `economy-tiering.md` demonstrates: set `max_budget` when the team is created (§2.2), or update
an existing team:

```bash
curl -s -X POST "$LITELLM_BASE/team/update" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "<team_id>", "max_budget": 50.0, "budget_duration": "30d"}' | jq .
```

**Admin UI**: **Teams** → select the team → edit **Max Budget** (and **Budget Duration** if you want it to
reset periodically, e.g. `30d` for monthly, `1mo` depending on version's accepted values — check the UI's
dropdown for the exact enum it supports).

- `max_budget` is a **cumulative USD cap** across every key on that team. Once exceeded, LiteLLM rejects
  further calls with a budget-exceeded error — this is what the economy tier's "$50 per-person cap" example
  relies on (each economy user gets their **own key on the shared economy team**, but if you want a true
  per-person cap rather than a shared pool, budget the **key**, not just the team — see §4.2).
- To change a tier's budget for everyone at once (e.g. raise the intern cap from $50 to $75), update the team
  once — no need to touch individual keys.

### 4.2 Per-key budget (per-individual cap, or a one-off override)

If the governance model requires each person to have their *own* $N cap (not a shared team pool), set
`max_budget` on the **key** at issuance time. The reference Token Lambda's `TIER_CONFIG` seeds `max_budget`
onto the **team** (a shared pool across everyone in it) when that team is first created — see
`economy-tiering.md`. If you need strict per-person caps instead, set `max_budget` in the `body` passed to
`POST /key/generate` in `_create_virtual_key` (per-key, not per-team):

```bash
curl -s -X POST "$LITELLM_BASE/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"team_id": "<team_id>", "key_alias": "user@example.com", "max_budget": 50.0, "budget_duration": "30d"}' | jq .
```

Or edit an existing key: **Admin UI** → **Virtual Keys** → select the key → edit **Max Budget**/**Budget
Duration**; or `POST /key/update` with `{"key": "<sk-...>", "max_budget": ...}`.

> **Team pool vs. per-person cap — pick deliberately.** A team `max_budget` is spent by the *first* callers
> to hit it — if ten interns share one team budget of $50, the first two heavy users can exhaust it for the
> other eight. If the intent is "$50 **per person**", budget the **key** (§4.2), not the team. Decide this
> during Phase 1 Discovery (the tiering question) and say so explicitly in `config/dev.json`'s generated
> comments so the next operator doesn't have to reverse-engineer it from `economy-tiering.md`.

### 4.3 Checking current spend against a budget

```bash
# Team-level spend
curl -s "$LITELLM_BASE/team/info?team_id=<team_id>" -H "Authorization: Bearer $MASTER_KEY" | jq '.team_info.spend, .team_info.max_budget'

# Key-level spend
curl -s "$LITELLM_BASE/key/info?key=<sk-...>" -H "Authorization: Bearer $MASTER_KEY" | jq '.info.spend, .info.max_budget'
```
Or **Admin UI** → **Teams**/**Virtual Keys** → the list view shows current spend next to the budget for each
row — no need to query the API for a quick visual check.

### 4.4 What the developer sees when they hit the cap

LiteLLM returns an HTTP 4xx (budget-exceeded) error on the next call once the cap is crossed — the same
symptom described in `economy-tiering.md`'s verification checkpoints. There is no soft-warning step by
default; plan communication to end users (e.g. a Slack alert wired to the LiteLLM UI's spend view, or a
scheduled `team/info`/`key/info` poll) if you want advance warning before the hard cutoff.

---

---

## 5. Four common Admin UI tasks (quick reference)

Condensed operator checklist for the four tasks asked about most — each maps 1:1 to an Admin UI screen and
an official LiteLLM doc page. Use these when you just need the click-path, not the full design rationale in
§1–4 above.

### 5.1 Model setup (including Bedrock)

**Admin UI** → **Models** → **Add Model** → fill in `model_name` and `litellm_params`.

- General model management (add/edit/delete, `model_name` vs. `litellm_params.model` distinction):
  <https://docs.litellm.ai/docs/proxy/model_management>
- Bedrock-specific parameters (model ID prefix e.g. `bedrock/`, AWS region, AWS credentials/role):
  <https://docs.litellm.ai/docs/providers/bedrock>

> In this gateway, **Claude** (`bedrock/`) auth is via the ECS **Task Role** (SigV4, tokenless) — do not paste
> static AWS access keys into `litellm_params`; leave credential fields empty so LiteLLM falls back to the
> task's IAM role. **Mantle** (`bedrock_mantle/`, GPT-5.x) is the exception: its Responses route has no SigV4
> path, so a short-term Bearer key is minted at runtime into `BEDROCK_MANTLE_API_KEY` by the
> `mantle_token_refresh` callback (never `AWS_BEARER_TOKEN_BEDROCK` — that boto3-reserved name would break
> Claude). This is Hard Constraint #6. Only `model_name`/`model`/`aws_region_name` need to be set per model per
> the Bedrock docs above; do not add an `api_key` for Mantle in `litellm_params` (the callback supplies it via env).

### 5.2 Per-user budget

**Admin UI** → **Internal Users** → select a user → set `max_budget` and `budget_duration`.

- <https://docs.litellm.ai/docs/proxy/users>

> "Internal Users" here is a **different scoping level than Team budgets** (§4 above): a Team `max_budget`
> caps everyone on that team combined, while an Internal User budget caps that one login/key regardless of
> team. Pick whichever matches the governance model you actually want — see the "team pool vs. per-person
> cap" callout in §4.2.

### 5.3 Viewing user prompts (request/response bodies)

**Admin UI** → **Logs** → Settings (gear icon) → enable **"Store Prompts in Spend Logs"** → **Save**.
No restart required — takes effect on the next request.

- Spend-log prompt storage setting: <https://docs.litellm.ai/docs/proxy/ui_spend_log_settings>
- Logs UI in general (filtering, drill-down): <https://docs.litellm.ai/docs/proxy/ui_logs>

> This stores the raw prompt/response **in LiteLLM's own DB** (Aurora, via the spend logs table) — it's a
> lighter-weight alternative to standing up Langfuse (§3.2) when you only need occasional prompt inspection
> rather than full distributed tracing. Treat this the same as Langfuse's raw-content warning: it may
> capture sensitive customer content, so scope Admin UI access accordingly once enabled.

### 5.4 Per-customer usage

**Admin UI** → **Usage** → **Customer Usage** tab → view spend by customer, daily spend trend, and
model-mix breakdown per customer.

- Customer usage tab: <https://docs.litellm.ai/docs/proxy/customer_usage>
- Spend tracking in general (how LiteLLM computes/attributes cost): <https://docs.litellm.ai/docs/proxy/cost_tracking>

> "Customer" is a LiteLLM concept distinct from Team/Internal User — it's typically used when the gateway's
> caller is itself multi-tenant (e.g. billing an end-customer of the SSO'd developer, not the developer
> directly). If this gateway's deployment only tracks SSO users/teams and never sets a `customer_id` on
> requests, this tab will show no data — that's expected, not a bug.

---

## Related documents

- `shared/reference/sso-setup.md` — provisioning the IdC permission sets/groups that drive team creation
- `shared/examples/economy-tiering.md` — the exact Token Lambda code implementing tier→team→budget mapping
- `shared/patterns/litellm-gateway.md` — `config.yaml`, model routing, Guardrails, MCP wiring
- `shared/patterns/lambda-handlers.md` — Token Service `_resolve_team_id`/`_ensure_team`/`_create_virtual_key` source
- `shared/reference/prerequisites.md` — tooling/access needed before any of the above is reachable
