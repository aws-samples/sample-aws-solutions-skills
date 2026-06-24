# Example — Economy Tiering (permission set → economy team: model allowlist + $50 budget cap)

Hypothetical customer **"Orion Edu"** — a cost-governance scenario where full-time engineers get all models, while the
intern/bootcamp-student org is limited to **low-cost models only + a $50 per-person budget cap**. The key is to **map IAM
Identity Center permission sets to LiteLLM teams**, controlling the tier by identity with no code/infrastructure changes.

> This example focuses on the tiering mechanism itself. For the full stack combination see `enterprise-sso.md`; for the domain branching see `domainless-poc.md`.

> ⚠️ **This is just one example of the general mechanism.** The core pattern is *"SSO group/permission set (typically the org name) → LiteLLM team → per-team budget cap + model allowlist"*, and in practice you typically **create teams by org/team name and apply a budget to each org** (e.g. `org-research`, `team-frontend`). "economy/standard" is not a mandatory classification but just a label illustrating the pattern — replicate the flow below for as many orgs as you have (group→team→budget/allowlist). See `decision-tree.md` §3.

---

## 1. Requirements (Discovery answers)

| Question | Orion Edu answer |
|---|---|
| Tiers? | 2 — standard (all models, unlimited budget) + **economy** (low-cost models + $50/person cap) |
| Economy target? | Interns/students. Separated by the IdC permission set `ClaudeCodeEconomy` |
| Standard target? | Full-time engineers. Permission set `ClaudeCodeUser` |
| Model policy? | The economy tier **blocks** high-cost models like Opus/GPT-5.5/Fable, allowing only Sonnet/Haiku/GPT-5.4 |
| MCP? | Both tiers are allowed AgentCore Web Search (`default_tools`) |

---

## 2. Where tiering is implemented — a 3-layer mapping

| Layer | Role | File |
|---|---|---|
| **IAM Identity Center** | Assigns users to a tier via permission sets (`ClaudeCodeUser` / `ClaudeCodeEconomy`) | (external, admin configuration) |
| **Token Lambda** | Parses the permission set from the signed SSO ARN → resolves/creates the tier team ID | `lambda/token-service/handler.py` |
| **LiteLLM** | Enforces the team's `models` allowlist + `max_budget` + `mcp_access_groups` | LiteLLM runtime (`/team/new`, `/key/generate`) |

> **WHY identity-based tiering?** To promote a user from economy → standard, an admin just changes the permission set in IdC.
> No need to reissue keys or touch the LiteLLM UI. The single source of truth for identity = IAM Identity Center.

---

## 3. Token Lambda constants (Orion Edu settings)

Customize only the tier constants at the top of `lambda/token-service/handler.py`:

```python
# Scoped MCP access: SSO-issued keys join a team, which carries the
# "default_tools" MCP access group.
MCP_ACCESS_GROUPS = ["default_tools"]

# Tier routing by SSO permission set → LiteLLM team.
STANDARD_TEAM_ALIAS = "sso-users"
ECONOMY_TEAM_ALIAS = "sso-economy"
# SSO permission set name(s) routed to the economy tier (edit to match your IdC).
ECONOMY_PERMISSION_SETS = {"ClaudeCodeEconomy"}                        # ← Orion Edu's economy permission set
# Economy allowlist excludes the priciest models (gpt-5.5, claude-opus-4-8, claude-fable-5).
ECONOMY_MODELS = ["gpt-5.4", "claude-sonnet-4-6", "claude-haiku-4-5"]  # ← low-cost models only
ECONOMY_MAX_BUDGET_USD = 50.0                                         # ← $50 per-person cap
```

---

## 4. Permission set → team resolution logic (actual code)

The `permission_set` parsed from the logged-in developer's SSO ARN determines which team the key is attributed to.

```python
def _resolve_team_id(endpoint: str, master_key: str, permission_set: str) -> Optional[str]:
    """Map the caller's SSO permission set to a tier team (lookup-or-create)."""
    if permission_set in ECONOMY_PERMISSION_SETS:
        return _ensure_team(
            endpoint, master_key, ECONOMY_TEAM_ALIAS,
            models=ECONOMY_MODELS, max_budget=ECONOMY_MAX_BUDGET_USD,   # ← inject allowlist + cap
        )
    return _ensure_team(endpoint, master_key, STANDARD_TEAM_ALIAS, models=None, max_budget=None)
```

If the team does not exist it is created (lookup-or-create), registering `models`/`max_budget`/the MCP group with LiteLLM on creation:

```python
def _ensure_team(
    endpoint: str, master_key: str, alias: str,
    models: Optional[list] = None, max_budget: Optional[float] = None,
) -> Optional[str]:
    """Lookup-or-create a team carrying the MCP access group, with an optional model
    allowlist and budget cap. Cached per warm Lambda. Returns None on failure so key
    issuance still proceeds (auth must not break if team wiring is unavailable)."""
    if alias in _team_id_cache:
        return _team_id_cache[alias]
    # 1. Find an existing team by alias.
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
    try:
        new_team = {"team_alias": alias, "object_permission": {"mcp_access_groups": MCP_ACCESS_GROUPS}}
        if models is not None:
            new_team["models"] = models            # ← attach allowlist only to the economy team
        if max_budget is not None:
            new_team["max_budget"] = max_budget     # ← attach $50 cap only to the economy team
        resp = _litellm("POST", f"{endpoint}/team/new", master_key, new_team)
        if resp.get("team_id"):
            _team_id_cache[alias] = str(resp["team_id"])
            return _team_id_cache[alias]
    except Exception:  # noqa: BLE001
        logger.warning("team create failed for alias=%s", alias, exc_info=True)
    return None
```

On issuance, the `team_id` is assigned to the virtual key (`_create_virtual_key`):

```python
team_id = _resolve_team_id(endpoint, master_key, permission_set)
if team_id:
    body["team_id"] = team_id          # ← the key inherits the team's model allowlist/budget/MCP group
response = _litellm("POST", f"{endpoint}/key/generate", master_key, body)
return response["key"]
```

> **WHY return None on failure (graceful degradation)?** Even if team lookup/creation fails, **key issuance itself proceeds**.
> Authentication (virtual key issuance) must not break depending on team-wiring availability. In that case the key is issued
> without team scoping (a teamless key can only use the `allow_all_keys` MCP server). This is the intended behavior noted in the comment.

---

## 5. SSO ARN parsing — where the permission set comes from

The permission set name is extracted from the signed caller ARN via regex:

```python
# arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_<PermSet>_<id>/<username>
_SSO_ARN_RE = re.compile(
    r"^arn:aws:sts::(\d+):assumed-role/AWSReservedSSO_([^_/]+)_[^/]+/(.+)$"
)

def _parse_sso_arn(arn: str) -> Optional[tuple[str, str, str]]:
    """Return (username, permission_set, account) or None for non-SSO ARNs."""
    match = _SSO_ARN_RE.match(arn)
    if not match:
        return None
    account, permission_set, username = match.group(1), match.group(2), match.group(3)
    return username, permission_set, account
```

> **WHY enforce `AWSReservedSSO_`?** An ARN without this prefix (= a direct IAM role) returns `None` and is **rejected with 403**.
> That is, you cannot obtain a key with direct IAM credentials that bypass SSO — before any tiering, identity itself is pinned to SSO.
> `permission_set` (group 2) is the tier-routing key.

---

## 6. LiteLLM-side consistency (must be kept in sync)

- The model names in the economy team's `ECONOMY_MODELS` must **exactly** match the names in LiteLLM `config.yaml`'s `model_list`.
  Putting a non-existent model name in the allowlist causes economy users to be rejected when calling that model (an unintended block).
- `MCP_ACCESS_GROUPS = ["default_tools"]` only matches if the same group name exists in LiteLLM `mcp_servers.<name>.access_groups` (the AgentCore Web Search MCP).

> **Pitfall**: model IDs are volatile. Before finalizing the `ECONOMY_MODELS`/`STANDARD` model names, verify model IDs and
> regional availability via AWS Knowledge MCP (`aws___get_regional_availability`). Do not hard-code stale IDs.

---

## Verification checkpoints

- Logging in with the `ClaudeCodeEconomy` permission set → the issued key belongs to the `sso-economy` team, `models`=the 3 low-cost types, `max_budget`=50.
- Calling `claude-opus-4-8` with an economy key → LiteLLM rejects it as an allowlist violation.
- When the $50 cumulative cap is exceeded, LiteLLM returns a budget-exceeded error (guide the user on the path to request an increase from an admin).
- The `ClaudeCodeUser` permission set → the `sso-users` team (unlimited). Both tiers can use the AgentCore Web Search MCP.
