# Lambda Handlers Pattern (Auth-mode Token Service + DB Init Custom Resource)

This pattern document transcribes the **two Lambda handlers** of the `llm-gateway-multi-agent` reference solution
**nearly verbatim from the actual source code**, with English explanations + WHY comments +
cross-layer mapping added, so that an AI agent can regenerate them as-is.

Target files:
- `lambda/token-service/handler.py` — the token service that issues a LiteLLM virtual key after verifying SSO identity
- `lambda/db-init/handler.py` — a CloudFormation Custom Resource that creates a service DB/user on Aurora PostgreSQL

> ⚠️ These two handlers are the **auth boundary of the governance gateway** itself.
> The token service enforces "only people verified by SSO receive a LiteLLM virtual key",
> and db-init enforces "a service (Langfuse) attaches to the DB with a least-privilege dedicated account, not master credentials".

---

## Cross-Layer Mapping

The token service handler does not work in isolation. It is bound together with the following layers to form one auth flow.

```
aws sso login
   │
   ▼
[client key helper]  scripts/get-gateway-token.sh   ← shared by Claude apiKeyHelper / Codex auth.command
   │  SigV4 signing
   ▼
[API Gateway]  IAM Auth                                ← lib/auth-stack.ts (RestApi, IAM authorizer)
   │  requestContext.identity.userArn = caller ARN
   ▼
[this Lambda]  lambda/token-service/handler.py         ← the AWSReservedSSO_ regex validation is the key
   │
   ├──▶ [DynamoDB]  ConfigTable (key cache, TTL)        ← lib/auth-stack.ts (Table, pk/sk)
   ├──▶ [Secrets Manager]  LITELLM_MASTER_KEY_ARN       ← lib/litellm-stack.ts (master key secret)
   ├──▶ [SSM Parameter]  LITELLM_ENDPOINT_SSM           ← lib/litellm-stack.ts (endpoint export)
   └──▶ [LiteLLM /key/generate, /team/*]  HTTPS ALB     ← lib/litellm-stack.ts (ECS Fargate + ALB)
            │  team mapping inherits the model allowlist + budget + MCP access group
            ▼
        returns virtual key sk-... → client uses it as Bearer
```

Key environment variables (all injected by CDK):

| Env var | Source (CDK stack) | Purpose |
|-----------|----------------|------|
| `CONFIG_TABLE_NAME` | Auth Stack (DynamoDB) | per-user virtual-key cache table |
| `LITELLM_MASTER_KEY_ARN` | LiteLLM Stack (Secrets Manager) | master key for the `/key/generate` call |
| `LITELLM_ENDPOINT_SSM` | LiteLLM Stack (SSM Parameter) | LiteLLM ALB endpoint URL |
| `KEY_DURATION_SECONDS` | Auth Stack (default 3600 = 1h) | virtual-key `duration` passed to `/key/generate` — keys **expire** so access cannot outlive SSO/Cognito re-auth by more than this window (governance: SSO expiry alone never kills an already-issued key). 1h is deliberate: helpers re-mint silently while the SSO/Cognito session lives, so login cadence is unchanged — shorter duration only narrows the residual-access window (real-deploy validated) |
| `KEY_CACHE_TTL_SECONDS` | Auth Stack (default 2592000=30 days) | cache entry TTL — effective TTL is `min(this, KEY_DURATION_SECONDS - 300)` so the cache can never serve a key past its expiry |
| `RESPONSE_KEY` | Auth Stack (default `api_key`) | key name in the response JSON |

---

## Section 1: Token Service (`lambda/token-service/handler.py`)

### Core idea (WHY)

The **entire purpose** of this service is exactly one thing: hand out a LiteLLM virtual key
**only to a principal verified through the configured auth mode**. In `org-sso`, that is an IAM Identity Center permission-set role ARN. In `cognito-native`, that is a Cognito access-token JWT validated by the API Gateway Cognito authorizer (Cognito is the sole identity source — no external IdP, no IdC federation, no Identity Store). A direct IAM role (e.g. an `assumed-role`
without the `AWSReservedSSO_` prefix) is **rejected with 403**. Without this rejection,
anyone with mere IAM permissions could bypass the gateway.

Flow (`org-sso`):
1. API Gateway (IAM Auth) → the caller ARN is carried in `requestContext.identity.userArn`
2. Parse the SSO assumed-role ARN and enforce the `AWSReservedSSO_` prefix
3. Look up the user's cached virtual key in DynamoDB
4. Cache miss → call LiteLLM `/key/generate` (using the master key from Secrets Manager)
5. Cache the key (best-effort) and return `{"api_key": "sk-..."}`

### Recommended integrated source shape (auth-mode aware)

Generate the Token Service as one auth-mode-aware handler. The org-sso branch preserves the existing ARN parser; the cognito-native branch consumes the `cognito:groups` claim validated by API Gateway's Cognito authorizer — **no Identity Store call**.

```python
"""Auth-mode Token Service Lambda.

AUTH_MODE=org-sso:
  API Gateway IAM auth validates SigV4 and provides requestContext.identity.userArn.
  Lambda parses AWSReservedSSO_<PermissionSet> and maps PermissionSet == team_alias.

AUTH_MODE=cognito-native:
  API Gateway Cognito authorizer validates the Cognito ACCESS-token JWT and provides
  requestContext.authorizer.claims (incl. cognito:groups). Lambda reads that claim
  directly (no Identity Store round-trip), filters by COGNITO_TEAM_GROUP_PREFIX,
  requires exactly one match, and maps that group name == team_alias.
"""

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_ssm = boto3.client("ssm")
# NOTE: no identitystore client — cognito-native reads team membership from the
# JWT's cognito:groups claim, not from Identity Store.

_master_key_cache: Optional[str] = None
_endpoint_cache: Optional[str] = None
_team_id_cache: dict[str, str] = {}

AUTH_MODE = os.environ.get("AUTH_MODE", "org-sso")
RESPONSE_KEY = os.environ.get("RESPONSE_KEY", "api_key")
_SSO_ARN_RE = re.compile(r"^arn:aws:sts::(\d+):assumed-role/AWSReservedSSO_([^_/]+)_[^/]+/(.+)$")
MCP_ACCESS_GROUPS = ["default_tools"]
TIER_CONFIG: dict[str, dict[str, Any]] = {
    # Optional one-time team creation seeds, keyed by team_alias.
    # "llmgw-economy": {"models": ["gpt-5.4", "claude-haiku-4-5"], "max_budget": 50.0},
}

@dataclass(frozen=True)
class Principal:
    user_key: str
    display_name: str
    team_alias: str
    source: str
    metadata: dict[str, Any]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    try:
        principal = _resolve_principal(event)
        if principal is None:
            return _resp(403, {"error": "caller is not authorized for this gateway"})
        logger.info("principal verified: source=%s user=%s team=%s", principal.source, principal.user_key, principal.team_alias)

        cached = _get_cached_key(principal.user_key)
        if cached:
            return _resp(200, {RESPONSE_KEY: cached})

        endpoint = _get_endpoint()
        master_key = _get_master_key()
        try:
            virtual_key = _create_virtual_key(endpoint, master_key, principal)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # Alias collision. Recover the live key — or, if the existing key has
                # EXPIRED (KEY_DURATION_SECONDS), recover deletes it and returns None:
                # retry the create once so the user gets a fresh key, not a dead one.
                virtual_key = _recover_existing_key(
                    endpoint, master_key, principal.user_key, f"{principal.source}-{principal.user_key}"
                )
                if virtual_key is None:
                    virtual_key = _create_virtual_key(endpoint, master_key, principal)
            else:
                raise
        _cache_key(principal.user_key, virtual_key, principal.source)
        return _resp(200, {RESPONSE_KEY: virtual_key})
    except Exception:
        logger.exception("token issuance failed")
        return _resp(500, {"error": "internal server error"})


def _resolve_principal(event: dict[str, Any]) -> Optional[Principal]:
    if AUTH_MODE == "org-sso":
        return _resolve_org_sso_principal(event)
    if AUTH_MODE == "cognito-native":
        return _resolve_cognito_native_principal(event)
    raise ValueError(f"unsupported AUTH_MODE={AUTH_MODE}")


def _resolve_org_sso_principal(event: dict[str, Any]) -> Optional[Principal]:
    arn = event.get("requestContext", {}).get("identity", {}).get("userArn")
    if not isinstance(arn, str):
        return None
    match = _SSO_ARN_RE.match(arn)
    if not match:
        logger.warning("rejected non-SSO principal: %s", arn)
        return None
    account, permission_set, username = match.group(1), match.group(2), match.group(3)
    return Principal(
        user_key=f"org-sso:{account}:{username}",
        display_name=username,
        team_alias=permission_set,
        source="org-sso",
        metadata={"sso_arn": arn, "account": account, "permission_set": permission_set},
    )


def _extract_groups(raw: Any) -> list[str]:
    """`cognito:groups` in API Gateway's authorizer.claims can arrive as a
    JSON-encoded string ('["llmgw-dev1"]'), a native list, or a comma-separated
    string depending on the integration. Handle all three defensively.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(g) for g in parsed]
        except json.JSONDecodeError:
            pass
        return [g.strip() for g in s.strip("[]").replace('"', "").split(",") if g.strip()]
    return []


def _resolve_cognito_native_principal(event: dict[str, Any]) -> Optional[Principal]:
    # The API Gateway CognitoUserPoolsAuthorizer has already validated the access
    # token (signature/issuer/audience/expiry) before this Lambda runs. Trust ONLY
    # requestContext.authorizer.claims — not arbitrary body/header claims.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims") or {}
    if not isinstance(claims, dict) or not claims:
        logger.warning("cognito-native rejected: missing Cognito authorizer claims")
        return None

    user_key = claims.get("sub") or claims.get("cognito:username") or claims.get("email")
    if not user_key:
        logger.warning("cognito-native rejected: missing sub/username claim")
        return None
    display_name = str(claims.get("email") or claims.get("cognito:username") or user_key)

    groups = _extract_groups(claims.get("cognito:groups"))
    prefix = os.environ.get("COGNITO_TEAM_GROUP_PREFIX", "")
    candidates = [g for g in groups if not prefix or g.startswith(prefix)]

    strategy = os.environ.get("COGNITO_MULTI_GROUP_STRATEGY", "require-single-team-group")
    if strategy == "require-single-team-group" and len(candidates) != 1:
        logger.warning("cognito-native rejected: expected exactly one matching team group, got %s", candidates)
        return None

    team_alias = candidates[0]
    return Principal(
        user_key=f"cognito-native:{user_key}",
        display_name=display_name,
        team_alias=team_alias,
        source="cognito-native",
        metadata={"cognito_sub": str(user_key), "team_group": team_alias},
    )
```

Continue the source with the same cache, Secrets Manager, SSM, LiteLLM, `_ensure_team`, `_recover_existing_key`, and `_resp` helpers shown below. The key change is that `_create_virtual_key` accepts `Principal` and uses `principal.team_alias` for unbranched team lookup:

```python
def _create_virtual_key(endpoint: str, master_key: str, principal: Principal) -> str:
    body = {
        "key_alias": f"{principal.source}-{principal.user_key}",
        "user_id": principal.user_key,
        "end_user_id": principal.display_name,
        # WHY duration: without it LiteLLM issues a NON-EXPIRING key, so a user whose
        # SSO/Cognito access was revoked keeps working forever on the old key. With it,
        # residual access after revocation is bounded by KEY_DURATION_SECONDS (re-mint
        # requires a live SSO/Cognito login). Immediate cutoff = admin /key/delete
        # (litellm-admin-guide.md → Offboarding).
        "duration": f"{int(os.environ.get('KEY_DURATION_SECONDS', '3600'))}s",
        "metadata": principal.metadata | {"auth_mode": principal.source},
    }
    team_id = _resolve_team_id(endpoint, master_key, principal.team_alias)
    if team_id:
        body["team_id"] = team_id
    return _litellm("POST", f"{endpoint}/key/generate", master_key, body)["key"]


def _resolve_team_id(endpoint: str, master_key: str, team_alias: str) -> Optional[str]:
    seed = TIER_CONFIG.get(team_alias, {})
    return _ensure_team(endpoint, master_key, team_alias, models=seed.get("models"), max_budget=seed.get("max_budget"))
```

If you use a Lambda authorizer instead of `CognitoUserPoolsAuthorizer`, put the JWT signature/issuer/audience/JWKS validation in that authorizer and pass only validated claims to the Token Lambda. Do not parse unvalidated JWT payloads in the Token Lambda.

### Legacy org-sso helper source excerpt

The following excerpt is the original org-sso implementation. Keep it for the `org-sso` branch and helper functions, but do not emit it as the whole handler when `authMode='cognito-native'`.

```python
"""
SSO Token Service Lambda.

Flow:
  1. API Gateway (IAM Auth) -> requestContext.identity.userArn = caller ARN
  2. Parse the SSO assumed-role ARN, enforce the AWSReservedSSO_ prefix
  3. Look up the user's cached virtual key in DynamoDB
  4. Cache miss -> call LiteLLM /key/generate (master key from Secrets Manager)
  5. Cache the key (best-effort) and return {"api_key": "sk-..."}

Returning a virtual key only to verified SSO principals is the whole point: a
direct IAM role (no AWSReservedSSO_ prefix) is rejected, preventing bypass.
"""

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# WHY: create the boto3 clients/resources at module level. While the Lambda container
# is reused warm, this avoids creating new clients per handler invocation, reducing latency.
_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_ssm = boto3.client("ssm")

# WHY: master key / endpoint / team_id are memory-cached for the warm container's lifetime.
# This reduces the number of Secrets Manager / SSM / LiteLLM calls, speeding up post-cold calls.
_master_key_cache: Optional[str] = None
_endpoint_cache: Optional[str] = None
_team_id_cache: dict[str, str] = {}

# arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_<PermSet>_<id>/<username>
# WHY (key): this regex is the auth boundary. Only an ARN with the AWSReservedSSO_ prefix is an SSO principal.
#   group(1)=account, group(2)=permission set name, group(3)=username.
#   In the permission set name, the _<id> part is cut off with [^_/]+ to extract the pure permset name.
_SSO_ARN_RE = re.compile(
    r"^arn:aws:sts::(\d+):assumed-role/AWSReservedSSO_([^_/]+)_[^/]+/(.+)$"
)

RESPONSE_KEY = os.environ.get("RESPONSE_KEY", "api_key")

# Scoped MCP access: SSO-issued keys join a team, which carries the
# "default_tools" MCP access group. LiteLLM requires team membership to assign
# scoped MCP permissions (a teamless key can only use allow_all_keys servers).
# WHY: a virtual key must belong to a team to inherit the MCP access group ("default_tools").
#      A teamless key can only use allow_all_keys servers, so MCP scope control does not work.
MCP_ACCESS_GROUPS = ["default_tools"]

# Tier routing: the authorization unit *is* the LiteLLM team_alias, 1:1, no
# code-side branching. In org-sso this is the permission set name; in cognito-native
# this is the Cognito User Pool Group name (from the cognito:groups claim).
# Onboarding a new team is therefore pure console work:
#   org-sso:        1) IdC: create a group + a permission set named identically to
#                      the team; 2) assign it to the account.
#   cognito-native: 1) Cognito: create a User Pool Group named identically to the
#                      team; 2) add users to it.
#   both:           3) LiteLLM Admin UI: Teams -> + New Team, team_alias = the same
#                      name, set Models (allowlist) + Max Budget right there.
# No Lambda edit, no redeploy, for the steps above -- ever. `_resolve_team_id` below
# never branches on a specific group/permission-set name; it always resolves 1:1.
#
# WHY (identity-bound governance, zero-touch onboarding): tier assignment is owned
#      entirely by the identity source (who's in which group) + the LiteLLM Admin UI
#      (what that team's models/budget are). The Lambda is just plumbing between them
#      and never needs to know org/tier names in advance.
#
# TIER_CONFIG is OPTIONAL and only matters for the very first time a given team is
# auto-created (i.e. before anyone has set it up via the Admin UI yet): if a
# team_alias has an entry here, the team is created with that initial
# models/max_budget instead of "no restriction". This exists purely to let this
# skill's Discovery phase seed a couple of starter tiers (e.g. "economy" for interns)
# without requiring the operator to click through the UI before the first login.
# Once the team exists, every future change to its budget/allowlist happens in the
# Admin UI (Teams -> edit) -- this dict is never read again for that team_alias.
TIER_CONFIG: dict[str, dict[str, Any]] = {
    # Example seeded by Discovery answers -- delete or add entries as needed.
    # "team-economy": {"models": ["gpt-5.4", "claude-sonnet-5", "claude-haiku-4-5"], "max_budget": 50.0},
}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    try:
        # 1) Extract caller ARN. 400 if absent.
        user_arn = _extract_user_arn(event)
        if not user_arn:
            return _resp(400, {"error": "caller ARN not found in request context"})

        # 2) Parse SSO ARN + enforce AWSReservedSSO_. Reject non-SSO principals with 403.
        parsed = _parse_sso_arn(user_arn)
        if not parsed:
            # Non-SSO principal (e.g. a direct IAM role) -> reject.
            # WHY: this rejection is the core of bypass prevention. A direct IAM role cannot get a key.
            logger.warning("rejected non-SSO principal: %s", user_arn)
            return _resp(403, {"error": "caller is not an IAM Identity Center (SSO) principal"})

        username, permission_set, account = parsed
        logger.info("SSO verified: user=%s permset=%s account=%s", username, permission_set, account)

        # 3) On a DynamoDB cache hit, return immediately (fast path with no LiteLLM call).
        cached = _get_cached_key(username)
        if cached:
            return _resp(200, {RESPONSE_KEY: cached})

        # 4) Cache miss → load master key/endpoint then create a virtual key.
        master_key = _get_master_key()
        endpoint = _get_endpoint()
        try:
            virtual_key = _create_virtual_key(endpoint, master_key, username, account, user_arn, permission_set)
        except urllib.error.HTTPError as exc:
            # WHY: if the same key_alias already exists, LiteLLM returns 400.
            #      In that case, do not create a new one — recover the existing key (idempotency).
            #      EXCEPT when the existing key has EXPIRED (KEY_DURATION_SECONDS): recover
            #      deletes the stale row (freeing the alias) and returns None → retry create
            #      once, so the user gets a fresh key instead of a dead one (401 loop).
            if exc.code == 400:
                virtual_key = _recover_existing_key(endpoint, master_key, username, f"sso-{username}")
                if virtual_key is None:
                    virtual_key = _create_virtual_key(endpoint, master_key, username, account, user_arn, permission_set)
            else:
                raise

        # 5) Write to the cache (best-effort) and return.
        _cache_key(username, virtual_key)
        return _resp(200, {RESPONSE_KEY: virtual_key})

    except Exception:  # noqa: BLE001 - top-level guard
        # WHY: top-level guard. Do not expose internal error details to the client; return only 500.
        logger.exception("token issuance failed")
        return _resp(500, {"error": "internal server error"})


# --------------------------------------------------------------------------- #
# ARN extraction / parsing
# --------------------------------------------------------------------------- #
def _extract_user_arn(event: dict[str, Any]) -> Optional[str]:
    # WHY: API Gateway (IAM Auth) puts the verified caller ARN into requestContext.identity.userArn.
    #      It is a value API GW filled in after SigV4 verification, not one sent by the client, so it is trustworthy.
    try:
        return event["requestContext"]["identity"]["userArn"]
    except (KeyError, TypeError):
        return None


def _parse_sso_arn(arn: str) -> Optional[tuple[str, str, str]]:
    """Return (username, permission_set, account) or None for non-SSO ARNs."""
    if not isinstance(arn, str):
        return None
    match = _SSO_ARN_RE.match(arn)
    if not match:
        return None
    account, permission_set, username = match.group(1), match.group(2), match.group(3)
    return username, permission_set, account


# --------------------------------------------------------------------------- #
# DynamoDB cache
# --------------------------------------------------------------------------- #
def _table():
    return _dynamodb.Table(os.environ["CONFIG_TABLE_NAME"])


def _get_cached_key(username: str) -> Optional[str]:
    # WHY: a cache read failure is not fatal — treat it as a miss and proceed to the new-key issuance path.
    #      So swallow the exception and return None (auth must not depend on cache availability).
    try:
        result = _table().get_item(Key={"pk": f"USER#{username}", "sk": "VIRTUAL_KEY"})
        item = result.get("Item")
        if item and item.get("virtual_key"):
            return str(item["virtual_key"])
    except Exception:  # noqa: BLE001
        logger.warning("cache read failed for user=%s", username, exc_info=True)
    return None


def _cache_key(username: str, virtual_key: str) -> None:
    # WHY: single-item structure pk=USER#<username>, sk=VIRTUAL_KEY. Auto-expires via TTL.
    #      Even if the write fails, the key is already issued, so it is best-effort — swallow the exception.
    # WHY min(): the cache must expire BEFORE the key does (KEY_DURATION_SECONDS), or the
    #      Token Service would keep serving an already-expired key from cache → 401 loop.
    ttl_seconds = min(
        int(os.environ.get("KEY_CACHE_TTL_SECONDS", "2592000")),
        max(int(os.environ.get("KEY_DURATION_SECONDS", "3600")) - 300, 300),
    )
    try:
        _table().put_item(
            Item={
                "pk": f"USER#{username}",
                "sk": "VIRTUAL_KEY",
                "virtual_key": virtual_key,
                "key_alias": f"sso-{username}",
                "ttl": int(time.time()) + ttl_seconds,
            }
        )
    except Exception:  # noqa: BLE001 - cache write is best-effort
        logger.warning("cache write failed for user=%s", username, exc_info=True)


# --------------------------------------------------------------------------- #
# Secrets / SSM
# --------------------------------------------------------------------------- #
def _get_master_key() -> str:
    # WHY: the master key is stored in Secrets Manager. It may be in JSON form {"key": "..."}
    #      or plaintext, so handle both. Once read, memory-cache it for the warm lifetime.
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache
    raw = _secrets.get_secret_value(SecretId=os.environ["LITELLM_MASTER_KEY_ARN"])["SecretString"]
    try:
        _master_key_cache = json.loads(raw)["key"]
    except (json.JSONDecodeError, KeyError):
        _master_key_cache = raw
    return _master_key_cache


def _get_endpoint() -> str:
    # WHY: the LiteLLM ALB endpoint is determined at deploy time, so it is injected via an SSM Parameter and cached.
    global _endpoint_cache
    if _endpoint_cache is not None:
        return _endpoint_cache
    _endpoint_cache = _ssm.get_parameter(Name=os.environ["LITELLM_ENDPOINT_SSM"])["Parameter"]["Value"]
    return _endpoint_cache


# --------------------------------------------------------------------------- #
# LiteLLM API
# --------------------------------------------------------------------------- #
def _create_virtual_key(
    endpoint: str, master_key: str, username: str, account: str, user_arn: str, permission_set: str
) -> str:
    body = {
        "key_alias": f"sso-{username}",
        "user_id": username,
        "end_user_id": username,
        # WHY duration: without it LiteLLM issues a NON-EXPIRING key — SSO expiry or even
        # removing the user from IdC does NOT kill an already-issued key. With it, residual
        # access is bounded by KEY_DURATION_SECONDS (default 24h); re-minting requires a live
        # SSO login. Immediate cutoff = admin /key/delete (litellm-admin-guide.md → Offboarding).
        "duration": f"{int(os.environ.get('KEY_DURATION_SECONDS', '3600'))}s",
        # WHY: stamping the SSO origin into metadata lets you trace, from LiteLLM logs/Langfuse traces,
        #      which SSO identity/account/permission set made the call (audit trail).
        "metadata": {"sso_arn": user_arn, "account": account, "permission_set": permission_set},
    }
    # Assign the key to its team (same name as the permission set) so it inherits the
    # MCP access group, model allowlist, and budget. Scoped MCP permissions require
    # team membership in LiteLLM; failure to resolve the team degrades gracefully
    # (key is still issued, just without team scoping).
    # WHY (graceful degradation): even if team resolution fails, issue the key without team_id.
    #      Auth must not depend on team-wiring availability (auth must not break).
    team_id = _resolve_team_id(endpoint, master_key, permission_set)
    if team_id:
        body["team_id"] = team_id
    response = _litellm("POST", f"{endpoint}/key/generate", master_key, body)
    return response["key"]


def _resolve_team_id(endpoint: str, master_key: str, permission_set: str) -> Optional[str]:
    """Map the caller's IdC authorization unit directly to a same-named LiteLLM team (lookup-or-create).

    No branching on specific permission-set names: the permission set IS the team_alias.
    This is what makes future onboarding console-only (IdC group/permission-set + LiteLLM
    Admin UI team) instead of a Lambda code change + redeploy. TIER_CONFIG only supplies an
    initial models/max_budget if this exact team doesn't exist yet (first-ever login for it);
    once a human has touched the team in the Admin UI, this dict is irrelevant to it.
    """
    seed = TIER_CONFIG.get(permission_set, {})
    return _ensure_team(
        endpoint, master_key, permission_set,
        models=seed.get("models"), max_budget=seed.get("max_budget"),
    )


def _ensure_team(
    endpoint: str, master_key: str, alias: str,
    models: Optional[list] = None, max_budget: Optional[float] = None,
) -> Optional[str]:
    """Lookup-or-create a team carrying the MCP access group, with an optional model
    allowlist and budget cap. Cached per warm Lambda. Returns None on failure so key
    issuance still proceeds (auth must not break if team wiring is unavailable).
    """
    # WHY: cache alias→team_id in the warm container. Do not hit team/list on every call.
    if alias in _team_id_cache:
        return _team_id_cache[alias]
    # 1. Find an existing team by alias.
    # WHY: depending on the LiteLLM version, the team/list response may be a list,
    #      {"teams":...}, {"data":...}, etc., so absorb all three (defensive parsing).
    try:
        teams = _litellm("GET", f"{endpoint}/team/list", master_key)
        if isinstance(teams, dict):
            teams = teams.get("teams") or teams.get("data") or []
        for team in teams or []:
            if isinstance(team, dict) and team.get("team_alias") == alias and team.get("team_id"):
                _team_id_cache[alias] = str(team["team_id"])
                return _team_id_cache[alias]
    except Exception:  # noqa: BLE001
        logger.warning("team lookup failed for alias=%s", alias, exc_info=True)
    # 2. Create it if missing.
    # WHY: create the team if absent. Grant the MCP scope via object_permission.mcp_access_groups,
    #      and add models/max_budget only if TIER_CONFIG seeded them for this team's first creation
    #      (if None, no constraint is applied — an admin can still add them later via the Admin UI).
    try:
        new_team = {"team_alias": alias, "object_permission": {"mcp_access_groups": MCP_ACCESS_GROUPS}}
        if models is not None:
            new_team["models"] = models
        if max_budget is not None:
            new_team["max_budget"] = max_budget
        resp = _litellm("POST", f"{endpoint}/team/new", master_key, new_team)
        if resp.get("team_id"):
            _team_id_cache[alias] = str(resp["team_id"])
            return _team_id_cache[alias]
    except Exception:  # noqa: BLE001
        logger.warning("team create failed for alias=%s", alias, exc_info=True)
    return None


def _recover_existing_key(endpoint: str, master_key: str, username: str, alias: str) -> Optional[str]:
    # WHY: recovery path for a key_alias collision (400). Find the previously issued key in
    #      user/info and reuse its token (idempotent issuance) — but ONLY if it is not
    #      expired/near-expiry. Returns None after freeing the alias so the caller re-creates.
    #      Three real-deploy lessons baked in (constraints.md → "Virtual-key lifetime"):
    try:
        # ① user_key contains ':' and '+' (e.g. org-sso:<acct>:user+tag) — it MUST be
        #    percent-encoded or '+' arrives as a space and LiteLLM 404s "user not found";
        #    unhandled, that crashed recovery and EVERY issuance 500ed until LiteLLM's
        #    periodic expired-key cleanup happened to free the alias (~30 min outage).
        response = _litellm("GET", f"{endpoint}/user/info?user_id={quote(username, safe='')}", master_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # ② /key/generate does NOT create a user record, so user/info can 404 even
            #    though the alias row exists — free the alias directly and re-create.
            _litellm("POST", f"{endpoint}/key/delete", master_key, {"key_aliases": [alias]})
            return None
        raise
    for key_info in response.get("keys", []):
        k_alias = str(key_info.get("key_alias", ""))
        if not k_alias.startswith(("sso-", "cognito-", "org-sso-")):
            continue
        expires = key_info.get("expires")  # ISO-8601 string, or None for legacy non-expiring keys
        if expires:
            expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            # ③ 120s margin: a key about to expire is as good as expired — recovering it
            #    would cache a soon-dead key and 401-loop clients until the cache clears.
            if expires_at <= datetime.now(timezone.utc) + timedelta(seconds=120):
                _litellm("POST", f"{endpoint}/key/delete", master_key, {"key_aliases": [k_alias]})
                return None
        return key_info["token"]
    raise RuntimeError(f"could not recover existing key for user={username}")


def _litellm(method: str, url: str, master_key: str, body: Optional[dict] = None) -> dict:
    # WHY: uses only urllib with no dependencies (no need to vendor packages beyond boto3 → small deploy package).
    #      timeout=10 keeps the Lambda from hanging when LiteLLM responses are delayed.
    #      ssl.create_default_context() enables TLS certificate verification (the S310 warning is an intended use).
    headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    # WHY: API Gateway Lambda proxy integration format (statusCode/headers/body). body is a JSON string.
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}
```

### Behavior checklist (regeneration checklist)

- [ ] `org-sso`: the caller ARN is read **not from client input** but from `requestContext.identity.userArn` (filled in by API GW IAM Auth).
- [ ] `cognito-native`: the API Gateway Cognito authorizer verifies the **access token** (id_token → 401) before the Lambda runs; the Lambda reads `cognito:groups` from `requestContext.authorizer.claims` and makes **no Identity Store / `identitystore:*` call**.
- [ ] The `_SSO_ARN_RE` regex enforces `AWSReservedSSO_` → non-SSO gets **403** (org-sso).
- [ ] DynamoDB single-item cache: `pk=USER#<user_key>`, `sk=VIRTUAL_KEY`, `ttl`.
- [ ] The master key is from Secrets Manager (`LITELLM_MASTER_KEY_ARN`), the endpoint from SSM (`LITELLM_ENDPOINT_SSM`).
- [ ] The authorization unit maps **1:1, unbranched** to a same-named LiteLLM team — permission set for `org-sso`, Cognito User Pool Group name for `cognito-native`; no `if team in {...}` tier logic in code. `TIER_CONFIG` only seeds a first-time team's initial `models`/`max_budget`; it is not consulted again once the team exists.
- [ ] Onboarding a new team is console + LiteLLM Admin UI (Teams → edit) only: group + permission set for `org-sso`, Cognito group + membership for `cognito-native`; regenerating/redeploying this Lambda is never part of the steady-state onboarding flow.
- [ ] Even if team resolution fails, the key is still issued (**graceful degradation**).
- [ ] A `key_alias` collision (400) is handled idempotently by recovering the existing key.

### Pitfalls / cautions (Section 1)

- **Cache writes are best-effort**: `_cache_key` swallows exceptions. Even if the DynamoDB write fails, the key is already issued so the user flow is not blocked. However, the next call will be a cache miss and hit LiteLLM every time — catch DynamoDB permission/capacity errors with an alarm.
- **A cache read failure = treated as a miss**: `_get_cached_key` also swallows exceptions and returns `None`. This is intentional so that auth does not depend on cache availability, but if the cache dies, the LiteLLM load can spike.
- **`urlopen` timeout=10 is fixed**: if LiteLLM (ALB→ECS) is slow, it cuts off after 10 seconds. With a cold start + team creation overlapping, a single request accumulates multiple calls `team/list` → `team/new` → `key/generate`, so set the Lambda timeout generously (e.g. 30s+).
- **SSM/Secrets endpoint reachability**: if the Lambda is inside a VPC, there must be a path (VPC Endpoint or NAT) to SSM/Secrets Manager/DynamoDB. Without it, `get_parameter`/`get_secret_value` will hang on timeout. (The reference provides Interface Endpoints in the Network Stack.)
- **Regex group extraction trap, now doubly important**: if the permission set name contains `_`, `([^_/]+)` cuts off at the first `_`. Since the permission set name **is** the `team_alias` now (no separate mapping to catch a typo/truncation against), an underscore silently creates/resolves the wrong team with no error. Enforce no-underscore permission-set names at IdC-provisioning time (see `sso-setup.md` Gotchas), not just as an internal-code convention.
- **Master key format compatibility**: parsing diverges depending on whether the secret is `{"key": "..."}` JSON or plaintext. If you change the secret creation format in CDK, verify this handler's parsing along with it.
- **team/list response schema drift**: on a LiteLLM upgrade, the response may change among `list`/`{"teams"}`/`{"data"}`. Defensive parsing is in place, but if a new format appears, lookup can silently fail (→ create attempted every time → possible 400), so validate on version bumps.
- **`TIER_CONFIG` is a one-time seed, not an allowlist**: it is only consulted the moment a team is auto-created for the first time. If an operator later changes a team's budget/models in the Admin UI and then someone edits `TIER_CONFIG` for the same key expecting it to take effect, it won't — the team already exists, so `_ensure_team`'s lookup branch returns early. This is by design (Admin UI is the source of truth post-creation) but is a common point of confusion; document it where `TIER_CONFIG` is edited.

---


## Section 1A: `cognito-native` auth adapter (Cognito `cognito:groups` claim → team)

When `config.authMode = "cognito-native"`, generate an adapter beside the existing org-sso ARN parser instead of replacing it (the full resolver is shown in Section 1 above). The handler selects the parser from `AUTH_MODE`:

```python
AUTH_MODE = os.environ.get("AUTH_MODE", "org-sso")

def _resolve_principal(event: dict[str, Any]) -> Principal | None:
    if AUTH_MODE == "org-sso":
        return _resolve_org_sso_principal(event)          # existing AWSReservedSSO_ ARN path
    if AUTH_MODE == "cognito-native":
        return _resolve_cognito_native_principal(event)   # cognito:groups claim path
    raise ValueError(f"unsupported AUTH_MODE={AUTH_MODE}")
```

`cognito-native` requirements:

1. **Do not verify the JWT in the Lambda.** The API Gateway `CognitoUserPoolsAuthorizer` already validates the **access token** (signature/issuer/audience/expiry) before the Lambda runs and exposes the verified claims at `requestContext.authorizer.claims`. (An id_token is rejected with 401 at the authorizer.)
2. Read `cognito:groups` from those verified claims — **no Identity Store round-trip, no `identitystore:*` IAM**. Parse defensively (JSON string / native list / comma-separated) — see `_extract_groups` in Section 1.
3. Filter group names by `COGNITO_TEAM_GROUP_PREFIX` when configured.
4. Apply `COGNITO_MULTI_GROUP_STRATEGY=require-single-team-group`: exactly one matching group is required; zero or multiple → 403 with a clear log message.
5. Set `team_alias` to that single group name and call the same `_ensure_team(endpoint, master_key, team_alias, ...)` used by org-sso.

Do **not** add an in-code `if group in {...}` mapping. The Cognito group name is the LiteLLM `team_alias`. `TIER_CONFIG` may seed first creation only, exactly as in org-sso.

Required environment variables in `cognito-native` mode (set by AuthStack):

```text
AUTH_MODE=cognito-native
COGNITO_TEAM_GROUP_PREFIX=llmgw-
COGNITO_MULTI_GROUP_STRATEGY=require-single-team-group
```

**Required IAM permissions for the Token Lambda in `cognito-native` mode: none beyond the shared plane** (DynamoDB key cache read/write, Secrets Manager `grantRead` on the master key, SSM read for the LiteLLM URL). There is **no `identitystore:*` grant** — team membership comes from the JWT claim, verified by the API Gateway authorizer, not from Identity Store.

## Section 2: db-init Custom Resource (`lambda/db-init/handler.py`)

### Core idea (WHY)

Aurora PostgreSQL can be created up to the cluster/secret level via CloudFormation, but it cannot run
SQL like **`CREATE USER` / `CREATE DATABASE` / `GRANT`** inside the DB. So this Lambda is run once via a
**CloudFormation Custom Resource**, connecting with **master credentials (Secrets Manager)** to create a
**dedicated DB and least-privilege user** for a service (e.g. Langfuse).

This way the service attaches to the DB only with its **own dedicated account**, not master credentials (separation of privilege).

### Recommended integrated source shape (auth-mode aware)

Generate the Token Service as one auth-mode-aware handler. The org-sso branch preserves the existing ARN parser; the cognito-native branch consumes the `cognito:groups` claim validated by API Gateway's Cognito authorizer — **no Identity Store call**.

```python
"""Auth-mode Token Service Lambda.

AUTH_MODE=org-sso:
  API Gateway IAM auth validates SigV4 and provides requestContext.identity.userArn.
  Lambda parses AWSReservedSSO_<PermissionSet> and maps PermissionSet == team_alias.

AUTH_MODE=cognito-native:
  API Gateway Cognito authorizer validates the Cognito ACCESS-token JWT and provides
  requestContext.authorizer.claims (incl. cognito:groups). Lambda reads that claim
  directly (no Identity Store round-trip), filters by COGNITO_TEAM_GROUP_PREFIX,
  requires exactly one match, and maps that group name == team_alias.
"""

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")
_ssm = boto3.client("ssm")
# NOTE: no identitystore client — cognito-native reads team membership from the
# JWT's cognito:groups claim, not from Identity Store.

_master_key_cache: Optional[str] = None
_endpoint_cache: Optional[str] = None
_team_id_cache: dict[str, str] = {}

AUTH_MODE = os.environ.get("AUTH_MODE", "org-sso")
RESPONSE_KEY = os.environ.get("RESPONSE_KEY", "api_key")
_SSO_ARN_RE = re.compile(r"^arn:aws:sts::(\d+):assumed-role/AWSReservedSSO_([^_/]+)_[^/]+/(.+)$")
MCP_ACCESS_GROUPS = ["default_tools"]
TIER_CONFIG: dict[str, dict[str, Any]] = {
    # Optional one-time team creation seeds, keyed by team_alias.
    # "llmgw-economy": {"models": ["gpt-5.4", "claude-haiku-4-5"], "max_budget": 50.0},
}

@dataclass(frozen=True)
class Principal:
    user_key: str
    display_name: str
    team_alias: str
    source: str
    metadata: dict[str, Any]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    try:
        principal = _resolve_principal(event)
        if principal is None:
            return _resp(403, {"error": "caller is not authorized for this gateway"})
        logger.info("principal verified: source=%s user=%s team=%s", principal.source, principal.user_key, principal.team_alias)

        cached = _get_cached_key(principal.user_key)
        if cached:
            return _resp(200, {RESPONSE_KEY: cached})

        endpoint = _get_endpoint()
        master_key = _get_master_key()
        try:
            virtual_key = _create_virtual_key(endpoint, master_key, principal)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # Alias collision. Recover the live key — or, if the existing key has
                # EXPIRED (KEY_DURATION_SECONDS), recover deletes it and returns None:
                # retry the create once so the user gets a fresh key, not a dead one.
                virtual_key = _recover_existing_key(
                    endpoint, master_key, principal.user_key, f"{principal.source}-{principal.user_key}"
                )
                if virtual_key is None:
                    virtual_key = _create_virtual_key(endpoint, master_key, principal)
            else:
                raise
        _cache_key(principal.user_key, virtual_key, principal.source)
        return _resp(200, {RESPONSE_KEY: virtual_key})
    except Exception:
        logger.exception("token issuance failed")
        return _resp(500, {"error": "internal server error"})


def _resolve_principal(event: dict[str, Any]) -> Optional[Principal]:
    if AUTH_MODE == "org-sso":
        return _resolve_org_sso_principal(event)
    if AUTH_MODE == "cognito-native":
        return _resolve_cognito_native_principal(event)
    raise ValueError(f"unsupported AUTH_MODE={AUTH_MODE}")


def _resolve_org_sso_principal(event: dict[str, Any]) -> Optional[Principal]:
    arn = event.get("requestContext", {}).get("identity", {}).get("userArn")
    if not isinstance(arn, str):
        return None
    match = _SSO_ARN_RE.match(arn)
    if not match:
        logger.warning("rejected non-SSO principal: %s", arn)
        return None
    account, permission_set, username = match.group(1), match.group(2), match.group(3)
    return Principal(
        user_key=f"org-sso:{account}:{username}",
        display_name=username,
        team_alias=permission_set,
        source="org-sso",
        metadata={"sso_arn": arn, "account": account, "permission_set": permission_set},
    )


def _extract_groups(raw: Any) -> list[str]:
    """`cognito:groups` in API Gateway's authorizer.claims can arrive as a
    JSON-encoded string ('["llmgw-dev1"]'), a native list, or a comma-separated
    string depending on the integration. Handle all three defensively.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(g) for g in parsed]
        except json.JSONDecodeError:
            pass
        return [g.strip() for g in s.strip("[]").replace('"', "").split(",") if g.strip()]
    return []


def _resolve_cognito_native_principal(event: dict[str, Any]) -> Optional[Principal]:
    # The API Gateway CognitoUserPoolsAuthorizer has already validated the access
    # token (signature/issuer/audience/expiry) before this Lambda runs. Trust ONLY
    # requestContext.authorizer.claims — not arbitrary body/header claims.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims") or {}
    if not isinstance(claims, dict) or not claims:
        logger.warning("cognito-native rejected: missing Cognito authorizer claims")
        return None

    user_key = claims.get("sub") or claims.get("cognito:username") or claims.get("email")
    if not user_key:
        logger.warning("cognito-native rejected: missing sub/username claim")
        return None
    display_name = str(claims.get("email") or claims.get("cognito:username") or user_key)

    groups = _extract_groups(claims.get("cognito:groups"))
    prefix = os.environ.get("COGNITO_TEAM_GROUP_PREFIX", "")
    candidates = [g for g in groups if not prefix or g.startswith(prefix)]

    strategy = os.environ.get("COGNITO_MULTI_GROUP_STRATEGY", "require-single-team-group")
    if strategy == "require-single-team-group" and len(candidates) != 1:
        logger.warning("cognito-native rejected: expected exactly one matching team group, got %s", candidates)
        return None

    team_alias = candidates[0]
    return Principal(
        user_key=f"cognito-native:{user_key}",
        display_name=display_name,
        team_alias=team_alias,
        source="cognito-native",
        metadata={"cognito_sub": str(user_key), "team_group": team_alias},
    )
```

Continue the source with the same cache, Secrets Manager, SSM, LiteLLM, `_ensure_team`, `_recover_existing_key`, and `_resp` helpers shown below. The key change is that `_create_virtual_key` accepts `Principal` and uses `principal.team_alias` for unbranched team lookup:

```python
def _create_virtual_key(endpoint: str, master_key: str, principal: Principal) -> str:
    body = {
        "key_alias": f"{principal.source}-{principal.user_key}",
        "user_id": principal.user_key,
        "end_user_id": principal.display_name,
        # WHY duration: without it LiteLLM issues a NON-EXPIRING key, so a user whose
        # SSO/Cognito access was revoked keeps working forever on the old key. With it,
        # residual access after revocation is bounded by KEY_DURATION_SECONDS (re-mint
        # requires a live SSO/Cognito login). Immediate cutoff = admin /key/delete
        # (litellm-admin-guide.md → Offboarding).
        "duration": f"{int(os.environ.get('KEY_DURATION_SECONDS', '3600'))}s",
        "metadata": principal.metadata | {"auth_mode": principal.source},
    }
    team_id = _resolve_team_id(endpoint, master_key, principal.team_alias)
    if team_id:
        body["team_id"] = team_id
    return _litellm("POST", f"{endpoint}/key/generate", master_key, body)["key"]


def _resolve_team_id(endpoint: str, master_key: str, team_alias: str) -> Optional[str]:
    seed = TIER_CONFIG.get(team_alias, {})
    return _ensure_team(endpoint, master_key, team_alias, models=seed.get("models"), max_budget=seed.get("max_budget"))
```

If you use a Lambda authorizer instead of `CognitoUserPoolsAuthorizer`, put the JWT signature/issuer/audience/JWKS validation in that authorizer and pass only validated claims to the Token Lambda. Do not parse unvalidated JWT payloads in the Token Lambda.

### Legacy org-sso helper source excerpt

The following excerpt is the original org-sso implementation. Keep it for the `org-sso` branch and helper functions, but do not emit it as the whole handler when `authMode='cognito-native'`.

```python
"""
DB Init Lambda — Custom Resource handler.
Creates PostgreSQL databases and users for services (Langfuse etc).
Runs with master credentials from Secrets Manager.
"""
import json
import logging
import os
import boto3
import psycopg2

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")


def handler(event, context):
    """CloudFormation Custom Resource handler."""
    request_type = event.get("RequestType", "Create")
    props = event.get("ResourceProperties", {})

    # WHY: on a Delete event, do not drop the DB/User (data preservation). Return success only
    #      so stack deletion is not blocked. Echo the PhysicalResourceId as-is.
    if request_type == "Delete":
        return {"Status": "SUCCESS", "PhysicalResourceId": event.get("PhysicalResourceId", "db-init")}

    try:
        # WHY: inputs are passed by CDK as ResourceProperties.
        #      master_secret_arn = Aurora master secret, target_* = the service account/DB to create.
        master_secret_arn = props["MasterSecretArn"]
        target_db = props["DatabaseName"]
        target_user = props["Username"]
        target_password_secret_arn = props["PasswordSecretArn"]

        # WHY: read the master secret and the target password secret separately.
        #      The target secret accepts both {"password":...} and {"SecretString":...} forms.
        master = json.loads(secrets.get_secret_value(SecretId=master_secret_arn)["SecretString"])
        target_secret = json.loads(secrets.get_secret_value(SecretId=target_password_secret_arn)["SecretString"])
        target_password = target_secret.get("password", target_secret.get("SecretString", ""))

        host = master.get("host")
        port = master.get("port", 5432)
        master_user = master.get("username")
        master_pass = master.get("password")
        master_db = master.get("dbname", "postgres")

        # WHY: connect to the default DB (postgres) with the master account. autocommit=True is required
        #      so that CREATE DATABASE runs outside a transaction block (PG cannot CREATE DATABASE inside a transaction).
        #      connect_timeout=10 keeps it from hanging when the network is unreachable.
        conn = psycopg2.connect(
            host=host, port=port, user=master_user, password=master_pass, dbname=master_db,
            connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Create user if not exists
        # WHY: idempotency. If it already exists, only update the password (ALTER). A Custom Resource can be
        #      re-run on Update, so the "update if exists, create if absent" pattern is essential.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (target_user,))
        if not cur.fetchone():
            cur.execute(f"CREATE USER \"{target_user}\" WITH PASSWORD %s", (target_password,))
            logger.info("Created user: %s", target_user)
        else:
            cur.execute(f"ALTER USER \"{target_user}\" WITH PASSWORD %s", (target_password,))
            logger.info("Updated password for user: %s", target_user)

        # Create database if not exists (owned by master user, then grant all to target)
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target_db,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{target_db}"')
            logger.info("Created database: %s", target_db)
        else:
            logger.info("Database already exists: %s", target_db)

        # Grant all privileges on the database to the target user
        cur.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{target_db}" TO "{target_user}"')
        # Also grant CREATE permission so Prisma migrations can create schemas
        # WHY: Langfuse creates its schema via Prisma migrations. Without CREATEDB permission,
        #      the migration fails, so grant it up front.
        cur.execute(f'ALTER USER "{target_user}" CREATEDB')
        cur.close()
        conn.close()

        # Connect to the target DB and grant schema permissions
        # WHY: GRANT ON SCHEMA public must run inside that DB to take effect. So reconnect with a new
        #      connection to target_db (still the master account) and grant public schema permissions.
        conn2 = psycopg2.connect(
            host=host, port=port, user=master_user, password=master_pass, dbname=target_db,
            connect_timeout=10,
        )
        conn2.autocommit = True
        cur2 = conn2.cursor()
        cur2.execute(f'GRANT ALL ON SCHEMA public TO "{target_user}"')
        cur2.close()
        conn2.close()

        return {"Status": "SUCCESS", "PhysicalResourceId": f"db-init-{target_db}-{target_user}"}

    except Exception as e:
        # WHY: on failure, return FAILED + Reason so CloudFormation rolls back the stack.
        #      If a Custom Resource does not respond, the stack hangs for an hour, so always respond.
        logger.exception("DB init failed")
        return {"Status": "FAILED", "Reason": str(e), "PhysicalResourceId": event.get("PhysicalResourceId", "db-init-failed")}
```

### Behavior checklist (regeneration checklist)

- [ ] `RequestType == "Delete"` does nothing and returns SUCCESS (data preservation).
- [ ] Connect to the default DB (`postgres`) with the master secret, `autocommit=True`.
- [ ] User: query `pg_roles`, then `CREATE USER` if absent, `ALTER USER ... PASSWORD` if present (idempotent).
- [ ] Database: query `pg_database`, then `CREATE DATABASE` if absent (idempotent).
- [ ] `GRANT ALL PRIVILEGES ON DATABASE` + `ALTER USER ... CREATEDB` (for Prisma).
- [ ] Reconnect to the target DB with a **separate connection** and `GRANT ALL ON SCHEMA public`.
- [ ] On exception, return `Status: FAILED` + `Reason` (triggers stack rollback).

### Pitfalls / cautions (Section 2)

- **psycopg2 vendoring required**: `psycopg2` is not a standard library. It must be bundled (vendored) in the Lambda — include `psycopg2-binary` in a Lambda Layer or the deploy package. If the Linux x86_64/arm64 binary compatibility is not matched, it dies at the `import psycopg2` step. (Verify the function architecture matches the build platform in CDK.)
- **`CREATE DATABASE` only outside a transaction**: if you omit `conn.autocommit = True`, psycopg2 opens an implicit transaction and you get the error `CREATE DATABASE cannot run inside a transaction block`.
- **`GRANT ON SCHEMA public` must be inside that DB**: a connection attached to the master DB (postgres) cannot grant permissions on the target DB's `public` schema. You must run it on a connection (`conn2`) that **reconnected to target_db**. Skip this step and Langfuse fails to create tables.
- **Network reachability / connect_timeout**: this Lambda must have 5432 allowed by security groups from the same VPC/subnet as Aurora. Without a path, it fails after 10 seconds via `connect_timeout=10` → Custom Resource FAILED. (Check the Data Stack's SG inbound rules.)
- **A Custom Resource must always respond**: on any path (success/failure/Delete), it must return a Status. If the response is missing, CloudFormation waits up to an hour before timing out, leaving the stack stuck for a long time.
- **Identifiers are quoted, but beware SQL injection**: `target_db`/`target_user` are embedded directly into identifiers via f-string (`"{...}"`). The values are trusted input controlled by CDK, so it is safe; but if this input source changes to user-controlled, an identifier injection risk arises. The password correctly uses parameter binding (`%s`).
- **target password secret format**: it reads `{"password": ...}` first and falls back to `{"SecretString": ...}` if absent. If you change the secret creation format in CDK, `target_password` can become an empty string, so verify the format matches.
- **Beware PhysicalResourceId changes**: on success it is the form `db-init-{db}-{user}`. If this value changes between Updates, CloudFormation sends a Delete to the old resource, which is a no-op, so it is safe — still, arbitrarily changing the ID convention can trigger a replace, so be careful.

---

## Common design principles of the two handlers (summary)

1. **Enforce the auth/authorization boundary in code**: the token service uses the SSO regex, db-init uses a least-privilege dedicated account.
2. **graceful degradation**: failure of auxiliary functions (team wiring, caching) does not block the core flow (key issuance).
3. **Idempotency**: key issuance is safe to re-run via alias-collision recovery; db-init via "update/skip if exists".
4. **Minimal dependencies**: the token service uses only urllib (no vendoring beyond boto3); only db-init vendors psycopg2.
5. **CDK injects all external references**: table name/secret ARN/endpoint are passed via env vars and ResourceProperties — the handler does not hardcode them.
