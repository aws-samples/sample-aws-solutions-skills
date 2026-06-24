# Lambda Handlers Pattern (SSO Token Service + DB Init Custom Resource)

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
| `KEY_CACHE_TTL_SECONDS` | Auth Stack (default 2592000=30 days) | cache entry TTL |
| `RESPONSE_KEY` | Auth Stack (default `api_key`) | key name in the response JSON |

---

## Section 1: SSO Token Service (`lambda/token-service/handler.py`)

### Core idea (WHY)

The **entire purpose** of this service is exactly one thing: hand out a LiteLLM virtual key
**only to a principal verified by IAM Identity Center (SSO)**. A direct IAM role (e.g. an `assumed-role`
without the `AWSReservedSSO_` prefix) is **rejected with 403**. Without this rejection,
anyone with mere IAM permissions could bypass the gateway.

Flow:
1. API Gateway (IAM Auth) → the caller ARN is carried in `requestContext.identity.userArn`
2. Parse the SSO assumed-role ARN and enforce the `AWSReservedSSO_` prefix
3. Look up the user's cached virtual key in DynamoDB
4. Cache miss → call LiteLLM `/key/generate` (using the master key from Secrets Manager)
5. Cache the key (best-effort) and return `{"api_key": "sk-..."}`

### Full source + WHY comments

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

# Tier routing by SSO permission set → LiteLLM team.
#   - Standard: all models, no extra budget cap.
#   - Economy:  cheaper models only (GPT-5.4 instead of 5.5; no Opus/Fable) + a
#     budget cap. Admins assign the economy permission set in IAM Identity Center
#     to economy users/orgs. Both tiers inherit the "default_tools" MCP group.
# WHY: the "tier" is decided by the SSO permission set name. Since permission set assignment
#      is managed by an admin in IAM Identity Center, the model allowlist/budget policy is
#      bound 1:1 with the identity system (IdC) — no separate user management at the gateway.
STANDARD_TEAM_ALIAS = "sso-users"
ECONOMY_TEAM_ALIAS = "sso-economy"
# SSO permission set name(s) routed to the economy tier (edit to match your IdC).
ECONOMY_PERMISSION_SETS = {"ClaudeCodeEconomy"}
# Economy allowlist excludes the priciest models (gpt-5.5, claude-opus-4-8, claude-fable-5).
ECONOMY_MODELS = ["gpt-5.4", "claude-sonnet-4-6", "claude-haiku-4-5"]
ECONOMY_MAX_BUDGET_USD = 50.0


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
            if exc.code == 400:
                virtual_key = _recover_existing_key(endpoint, master_key, username)
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
    # WHY: single-item structure pk=USER#<username>, sk=VIRTUAL_KEY. Auto-expires via TTL (default 30 days).
    #      Even if the write fails, the key is already issued, so it is best-effort — swallow the exception.
    ttl_seconds = int(os.environ.get("KEY_CACHE_TTL_SECONDS", "2592000"))
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
        # WHY: stamping the SSO origin into metadata lets you trace, from LiteLLM logs/Langfuse traces,
        #      which SSO identity/account/permission set made the call (audit trail).
        "metadata": {"sso_arn": user_arn, "account": account, "permission_set": permission_set},
    }
    # Assign the key to its tier team (standard/economy) so it inherits the team's
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
    """Map the caller's SSO permission set to a tier team (lookup-or-create)."""
    # WHY: if the permission set is in the economy set, route to the economy team (model restriction + budget cap),
    #      otherwise to the standard team (all models, no extra budget cap).
    if permission_set in ECONOMY_PERMISSION_SETS:
        return _ensure_team(
            endpoint, master_key, ECONOMY_TEAM_ALIAS,
            models=ECONOMY_MODELS, max_budget=ECONOMY_MAX_BUDGET_USD,
        )
    return _ensure_team(endpoint, master_key, STANDARD_TEAM_ALIAS, models=None, max_budget=None)


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
    #      and add models/max_budget only for economy (if None, no constraint is applied to the key).
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


def _recover_existing_key(endpoint: str, master_key: str, username: str) -> str:
    # WHY: recovery path for a key_alias collision (400). Find the sso- prefixed key in user/info and reuse its token.
    #      This way, a key is not duplicated for the same SSO user (idempotent issuance).
    response = _litellm("GET", f"{endpoint}/user/info?user_id={username}", master_key)
    for key_info in response.get("keys", []):
        if str(key_info.get("key_alias", "")).startswith("sso-"):
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

- [ ] The caller ARN is read **not from client input** but from `requestContext.identity.userArn` (filled in by API GW IAM Auth).
- [ ] The `_SSO_ARN_RE` regex enforces `AWSReservedSSO_` → non-SSO gets **403**.
- [ ] DynamoDB single-item cache: `pk=USER#<username>`, `sk=VIRTUAL_KEY`, `ttl`.
- [ ] The master key is from Secrets Manager (`LITELLM_MASTER_KEY_ARN`), the endpoint from SSM (`LITELLM_ENDPOINT_SSM`).
- [ ] permission set → team (standard/economy) mapping inherits the **model allowlist + budget cap + MCP access group**.
- [ ] Even if team resolution fails, the key is still issued (**graceful degradation**).
- [ ] A `key_alias` collision (400) is handled idempotently by recovering the existing key.

### Pitfalls / cautions (Section 1)

- **Cache writes are best-effort**: `_cache_key` swallows exceptions. Even if the DynamoDB write fails, the key is already issued so the user flow is not blocked. However, the next call will be a cache miss and hit LiteLLM every time — catch DynamoDB permission/capacity errors with an alarm.
- **A cache read failure = treated as a miss**: `_get_cached_key` also swallows exceptions and returns `None`. This is intentional so that auth does not depend on cache availability, but if the cache dies, the LiteLLM load can spike.
- **`urlopen` timeout=10 is fixed**: if LiteLLM (ALB→ECS) is slow, it cuts off after 10 seconds. With a cold start + team creation overlapping, a single request accumulates multiple calls `team/list` → `team/new` → `key/generate`, so set the Lambda timeout generously (e.g. 30s+).
- **SSM/Secrets endpoint reachability**: if the Lambda is inside a VPC, there must be a path (VPC Endpoint or NAT) to SSM/Secrets Manager/DynamoDB. Without it, `get_parameter`/`get_secret_value` will hang on timeout. (The reference provides Interface Endpoints in the Network Stack.)
- **Regex group extraction trap**: if the permission set name contains `_`, `([^_/]+)` cuts off at the first `_`. If you use underscores in IdC permission set names, `ECONOMY_PERMISSION_SETS` matching can go wrong, so mind the naming convention.
- **Master key format compatibility**: parsing diverges depending on whether the secret is `{"key": "..."}` JSON or plaintext. If you change the secret creation format in CDK, verify this handler's parsing along with it.
- **team/list response schema drift**: on a LiteLLM upgrade, the response may change among `list`/`{"teams"}`/`{"data"}`. Defensive parsing is in place, but if a new format appears, lookup can silently fail (→ create attempted every time → possible 400), so validate on version bumps.
- **Economy policy depends on IdC**: `ECONOMY_PERMISSION_SETS`/`ECONOMY_MODELS`/`ECONOMY_MAX_BUDGET_USD` are code constants. They must exactly match the actual permission set names in IAM Identity Center for economy routing to work.

---

## Section 2: db-init Custom Resource (`lambda/db-init/handler.py`)

### Core idea (WHY)

Aurora PostgreSQL can be created up to the cluster/secret level via CloudFormation, but it cannot run
SQL like **`CREATE USER` / `CREATE DATABASE` / `GRANT`** inside the DB. So this Lambda is run once via a
**CloudFormation Custom Resource**, connecting with **master credentials (Secrets Manager)** to create a
**dedicated DB and least-privilege user** for a service (e.g. Langfuse).

This way the service attaches to the DB only with its **own dedicated account**, not master credentials (separation of privilege).

### Full source + WHY comments

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
