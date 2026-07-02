# Example — Economy Tiering (permission set → economy team: model allowlist + $50 budget cap)

Hypothetical customer **"Orion Edu"** — a cost-governance scenario where full-time engineers get all models, while the
intern/bootcamp-student org is limited to **low-cost models only + a $50 per-person budget cap**. The mechanism itself
is generic and console-driven: **the SSO permission set name *is* the LiteLLM team_alias**, 1:1, with no per-org
branching in code (see `shared/patterns/lambda-handlers.md`'s `_resolve_team_id`). `TIER_CONFIG` below only seeds the
economy team's *first-ever* creation with its starter budget/allowlist — after that, an admin manages it entirely
through the LiteLLM Admin UI, never by editing this Lambda again.

> This example focuses on the tiering mechanism itself. For the full stack combination see `enterprise-sso.md`; for the domain branching see `domainless-poc.md`.

> ⚠️ **This is just one example of the general mechanism.** The core pattern is *"SSO group/permission set (typically the org name) → LiteLLM team of the same name → per-team budget cap + model allowlist, set once via `TIER_CONFIG` or the Admin UI"*, and in practice you typically **create teams by org/team name and apply a budget to each org** (e.g. `org-research`, `team-frontend`). "economy/standard" is not a mandatory classification but just a label illustrating the pattern — replicate the flow below for as many orgs as you have (permission set → same-named team → budget/allowlist, seeded once). See `decision-tree.md` §3.

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
| **IAM Identity Center** | Assigns users to a tier via permission sets (`ClaudeCodeUser` / `ClaudeCodeEconomy`) — the permission set name is chosen to match the LiteLLM team name it should resolve to | (external, admin configuration) |
| **Token Lambda** | Parses the permission set from the signed SSO ARN → resolves/creates a **same-named** LiteLLM team; consults `TIER_CONFIG` only if the team doesn't exist yet | `lambda/token-service/handler.py` |
| **LiteLLM** | Enforces the team's `models` allowlist + `max_budget` + `mcp_access_groups` | LiteLLM runtime (`/team/new`, `/key/generate`) |

> **WHY identity-based tiering?** To promote a user from economy → standard, an admin just changes the permission set in IdC.
> No need to reissue keys or touch the LiteLLM UI. The single source of truth for identity = IAM Identity Center.
> **WHY unbranched (permission set = team_alias)?** Onboarding a *new* org later — say `ClaudeCodeResearch` — needs zero
> Lambda changes: create the group + permission set named `ClaudeCodeResearch` in IdC, then create/edit the
> `ClaudeCodeResearch` team's budget/models in the LiteLLM Admin UI. `TIER_CONFIG` in this example only pre-seeds the
> two tiers Orion Edu already knows about at deploy time — it is not required for every future org.

---

## 3. Token Lambda constants (Orion Edu settings)

Customize only `TIER_CONFIG` at the top of `lambda/token-service/handler.py` — this seeds the **initial** budget/models
the *first* time each of these teams is auto-created; an admin can still change either afterward via the Admin UI:

```python
# Scoped MCP access: SSO-issued keys join a team, which carries the
# "default_tools" MCP access group.
MCP_ACCESS_GROUPS = ["default_tools"]

# One-time seed for each team's first auto-creation. Key = permission set name = LiteLLM
# team_alias (they are the same string — see _resolve_team_id, which never branches on
# a specific name). Once a team already exists, this dict is not consulted for it again;
# manage models/max_budget going forward via the LiteLLM Admin UI (Teams -> edit).
TIER_CONFIG = {
    # Orion Edu's economy permission set/team: low-cost models only + $50 per-person cap.
    "ClaudeCodeEconomy": {
        "models": ["gpt-5.4", "claude-sonnet-4-6", "claude-haiku-4-5"],  # excludes gpt-5.5, claude-opus-4-8, claude-fable-5
        "max_budget": 50.0,
    },
    # "ClaudeCodeUser" (standard) has no entry -> first login creates it with no
    # allowlist/budget restriction. Nothing stops you from seeding it too, e.g. with a
    # generous org-wide cap, if you'd rather not rely on the Admin UI default.
}
```

---

## 4. Permission set → team resolution logic (actual code)

The `permission_set` parsed from the logged-in developer's SSO ARN **is** the `team_alias` — no per-org `if` branch:

```python
def _resolve_team_id(endpoint: str, master_key: str, permission_set: str) -> Optional[str]:
    """Map the caller's SSO permission set directly to a same-named LiteLLM team (lookup-or-create)."""
    seed = TIER_CONFIG.get(permission_set, {})
    return _ensure_team(
        endpoint, master_key, permission_set,
        models=seed.get("models"), max_budget=seed.get("max_budget"),
    )
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
            new_team["models"] = models            # ← from TIER_CONFIG, only at first creation
        if max_budget is not None:
            new_team["max_budget"] = max_budget     # ← from TIER_CONFIG, only at first creation
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
> `permission_set` (group 2) is now **directly** the tier-routing key — it *is* the `team_alias`, not a lookup key into a
> branch. This is also why permission-set names must not contain `_` (see §6): `([^_/]+)` would truncate the very string
> that becomes the team name.

---

## 6. LiteLLM-side consistency (must be kept in sync)

- The model names in `TIER_CONFIG["ClaudeCodeEconomy"]["models"]` must **exactly** match the names in LiteLLM `config.yaml`'s `model_list`.
  Putting a non-existent model name in the allowlist causes economy users to be rejected when calling that model (an unintended block).
- `MCP_ACCESS_GROUPS = ["default_tools"]` only matches if the same group name exists in LiteLLM `mcp_servers.<name>.access_groups` (the AgentCore Web Search MCP).
- **Permission-set names must not contain `_`** — since the name is now used verbatim as the `team_alias`, an underscore doesn't just break the old regex match, it silently produces the wrong team name with no error.

> **Pitfall**: model IDs are volatile. Before finalizing `TIER_CONFIG`'s model names, verify model IDs and
> regional availability via AWS Knowledge MCP (`aws___get_regional_availability`). Do not hard-code stale IDs.

---

## 7. Onboarding a *new* org later (no code change)

Say Orion Edu later adds a `research` org that should get all models but a $500/month team-wide cap. With the
unbranched design above, this needs **zero edits to `handler.py`**:

1. IdC console → **Groups** → create `team-research` → add its members.
2. IdC console → **Permission sets** → create `TeamResearch` (no underscore) → attach the
   `execute-api:Invoke`-only inline policy → assign to the account for the `team-research` group.
3. LiteLLM Admin UI → **Teams** → **+ New Team**, `team_alias = TeamResearch` → set `Max Budget = 500`,
   `Budget Duration = 30d` (leave `Models` empty for "all models") right there in the UI.
4. First `aws sso login` + gateway call from that group resolves straight to the `TeamResearch` team.

`TIER_CONFIG` in `handler.py` is untouched — it's not consulted because the team already exists by the time
anyone logs in (step 3 created it ahead of time). Redeploying the Lambda is only needed if you *skip* step 3 and
want that org's first-ever login to auto-create the team with a non-default budget/allowlist baked in.

---

## Verification checkpoints

- Logging in with the `ClaudeCodeEconomy` permission set → the issued key belongs to the `ClaudeCodeEconomy` team, `models`=the 3 low-cost types, `max_budget`=50.
- Calling `claude-opus-4-8` with an economy key → LiteLLM rejects it as an allowlist violation.
- When the $50 cumulative cap is exceeded, LiteLLM returns a budget-exceeded error (guide the user on the path to request an increase from an admin).
- The `ClaudeCodeUser` permission set → the `ClaudeCodeUser` team (unlimited, since `TIER_CONFIG` has no entry for it). Both tiers can use the AgentCore Web Search MCP.
- Onboarding `TeamResearch` per §7 requires no Lambda redeploy — only IdC + Admin UI console actions.
