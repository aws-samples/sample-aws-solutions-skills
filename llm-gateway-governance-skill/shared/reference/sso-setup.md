# IAM Identity Center Organization Instance (`org-sso`) Setup — permission-set path

This document applies only when `authMode="org-sso"` and the IAM Identity Center instance is an **organization instance** with permission sets. The Token Service accepts IAM Identity Center permission-set principals (`AWSReservedSSO_` ARN). If Discovery finds an **account instance**, do not use this procedure; use `account-instance-setup.md` instead.

## Discovery questions (ask in Phase 1 for `org-sso`)

1. **Is IAM Identity Center already enabled** in the target account/org? If not, it must be enabled first (org management or delegated admin).
2. **IdC instance region** — which region hosts the IdC instance? (SSO login uses this region; it may differ from the gateway region — that's fine.)
3. **Identity source** — IdC built-in directory (default) or external IdP (Okta / Entra ID / Google)? Determines where users + passwords live.
4. **Permission set — create new or reuse?** Ask explicitly: *should I create a NEW permission set for this gateway, or reuse an existing one? what name?* Default to **creating a new, uniquely-named** one (e.g. `LlmGatewaySeoul`). ⚠️ Do **not** silently reuse a permission set just because its name matches a default (`LlmGatewayUser`) — a name match is **not** ownership; an existing one may belong to another gateway/groups and editing it changes their access. If the user chooses reuse, confirm the exact ARN and that its inline policy `Resource` targets **this** gateway's region + Token Service API id. Then map group(s)/permission-set name(s) → LiteLLM team (budget cap + model allowlist) as the user wants.
5. **Assignment — which group or users?** Ask which IdC **group(s)** (preferred) or individual users to assign the permission set to, and create the assignment(s) accordingly. Never assume the assignment.
6. **Session duration** — default PT1H.

⛔ If the user wants `org-sso` but IdC is not enabled / no users exist / no permission sets are available, surface this as a prerequisite at GATE 1. If the instance is an **account instance** (or there is no usable organization IdC), switch to `authMode="cognito-native"` and follow `account-instance-setup.md` — do **not** attempt IdC federation, which an account instance cannot support.

## `config.sso` (generated) + AuthStack outputs

The SSO Discovery answers are written into a first-class **`config.sso`** block, consumed by `AuthStack`:

```jsonc
"sso": {
  "startUrl": "https://<IDC_ID>.awsapps.com/start",  // IdC access portal URL
  "region": "us-east-1",                              // IdC home region (may differ from awsRegion)
  "accountId": "123456789012",                        // 12-digit account devs assume into
  "roleName": "LlmGatewayUser"                         // permission set / role name (no underscore)
}
```

Schema (`lib/config/schema.ts`): `SsoConfig { startUrl; region; accountId; roleName }` — validate `accountId` is 12 digits and `startUrl` non-empty.

`AuthStack` consumes `config.sso` and **emits onboarding outputs** so `scripts/setup-developer.sh` and the client profile can be generated without hand-copying:
```ts
new cdk.CfnOutput(this, 'SsoStartUrl',  { value: sso.startUrl });
new cdk.CfnOutput(this, 'SsoRegion',    { value: sso.region });
new cdk.CfnOutput(this, 'SsoAccountId', { value: sso.accountId });
new cdk.CfnOutput(this, 'SsoRoleName',  { value: sso.roleName });
```

The generated `~/.aws/config` profile maps 1:1: `region = <awsRegion>`, `sso_region = config.sso.region`, `sso_account_id = config.sso.accountId`, `sso_role_name = config.sso.roleName`, `sso_start_url = config.sso.startUrl`. (IdC region may differ from the gateway region — that's fine; the token helper derives the SigV4 region from the Token Service URL.)

## One-time per-account provisioning (CLI)

```bash
# 0) Find the IdC instance + identity store
aws sso-admin list-instances --region <idc-region> \
  --query 'Instances[0].[InstanceArn,IdentityStoreId]' --output text
INST=arn:aws:sso:::instance/ssoins-xxxx ; IDS=d-xxxx ; ACCT=<account-id>

# 1) Permission set — NO UNDERSCORE in the name (see Gotchas), short session
PS=$(aws sso-admin create-permission-set --instance-arn "$INST" \
  --name LlmGatewayUser --session-duration PT1H \
  --description "Invoke the LLM gateway SSO Token Service" \
  --query 'PermissionSet.PermissionSetArn' --output text)

# 2) Least-privilege inline policy: only execute-api:Invoke on the Token Service API
aws sso-admin put-inline-policy-to-permission-set --instance-arn "$INST" --permission-set-arn "$PS" \
  --inline-policy '{"Version":"2012-10-17","Statement":[{"Sid":"InvokeTokenService","Effect":"Allow","Action":"execute-api:Invoke","Resource":"arn:aws:execute-api:<gw-region>:<account-id>:<api-id>/*"}]}'

# 3) Create user (or skip and assign an existing user/group)
USERID=$(aws identitystore create-user --identity-store-id "$IDS" \
  --user-name user@example.com --display-name "Dev" \
  --name 'GivenName=Dev,FamilyName=User' \
  --emails 'Value=user@example.com,Type=work,Primary=true' \
  --query 'UserId' --output text)
# NOTE: bash var must not be named UID (reserved → readonly). Use USERID.

# 4) Assign user + permission set to the account (auto-provisions the permission set)
aws sso-admin create-account-assignment --instance-arn "$INST" --permission-set-arn "$PS" \
  --principal-type USER --principal-id "$USERID" \
  --target-id "$ACCT" --target-type AWS_ACCOUNT
#   For a team, prefer PrincipalType=GROUP with a group id.
```

## Per-user activation (CONSOLE ONLY — hand off)

IdC built-in directory has **no public API** to set/reset a user password. After `create-user`, the operator must, in the **IdC console → Users → (user) → Reset password**:
- **Send email** to the user (they set password + MFA), or
- **Generate a one-time password** (share securely).

(External IdP: activation happens in the IdP, not here.)

## Client profile (after activation)

```bash
# ~/.aws/config (additive — do not clobber existing profiles)
# [sso-session llm-gateway]
#   sso_start_url = https://d-xxxx.awsapps.com/start   # IdC access portal (Settings → portal URL)
#   sso_region    = <idc-region>
#   sso_registration_scopes = sso:account:access
# [profile llm-gateway]
#   sso_session = llm-gateway
#   sso_account_id = <account-id>
#   sso_role_name  = LlmGatewayUser
#   region = <gw-region>
# NOTE: the profile is named after the gateway ("llm-gateway"), not a single
# client — Claude Code AND Codex share this one profile via the key helper.
aws sso login --profile llm-gateway
```
Then the key helper (`apiKeyHelper`/`auth.command`) fetches the virtual key automatically. See `developer-onboarding.md`.

## Gotchas (must respect)

- **Permission-set name MUST NOT contain `_`.** The Token Lambda parses `AWSReservedSSO_<PermissionSetName>_<id>` with `[^_/]+` for the name — an underscore truncates/breaks the tier match. Use `LlmGatewayUser`, `LlmGatewayEconomy` (camelCase, no underscore).
- **Least privilege**: the permission set needs only `execute-api:Invoke` on the Token Service API ARN — nothing else to use the gateway.
- **IdC region ≠ gateway region is OK.** SSO login uses the IdC region; the token helper derives the SigV4 region from the Token Service URL, so they're independent.
- **Password activation is console-only** — plan a human handoff step; full automation isn't possible for the built-in directory.
- **Teams**: assign a **group** (PrincipalType=GROUP) instead of per-user assignments — scales and is easier to revoke.
- **Non-SSO callers are rejected (403)** by design — verify with `aws sts get-caller-identity` that the assumed role is `AWSReservedSSO_...` before debugging the gateway.

## End-to-end: new user/group → LiteLLM permissions + budget

**The question that matters for an admin: once the gateway is deployed, can a new org/team be onboarded
entirely through IdC + LiteLLM Admin UI clicks, or does someone have to edit and redeploy Lambda code every
time?** With the reference `handler.py` (`economy-tiering.md`) as shipped, it's the latter — every new
tier/org means adding a Python constant and redeploying. That is **not required by LiteLLM or IAM Identity
Center** — it's just how the sample code happens to branch. Set it up the way described below instead, and
onboarding becomes pure console work with **zero code changes per new org**, forever, after one small
one-time edit.

### Recommended: console-only onboarding (one-time code edit, then never again)

Make the permission-set name **be** the `team_alias` (e.g. permission set `team-research` → LiteLLM team
`team-research`), and change `_resolve_team_id` **once** to always call
`_ensure_team(endpoint, master_key, permission_set, models=None, max_budget=None)` — no `if permission_set
in {...}` branching, no per-tier constant. After that one edit + redeploy, every future onboarding is:

1. **IdC console → Users → Add user** (skip if the person already has an IdC identity).
2. **IdC console → Groups → Create group** matching the team name you want (e.g. `team-research`) →
   **Add users to group**.
3. **IdC console → Permission sets → Create**, name it identically to the group/team (no underscore) →
   attach the `execute-api:Invoke`-only inline policy → **AWS accounts → assign** it to the group.
4. **LiteLLM Admin UI → Teams → + New Team**, `team_alias` = the same name → set `Models` (allowlist) and
   `Max Budget`/`Budget Duration` right there in the UI. (Or skip this and let the Lambda auto-create the
   team with no restrictions on first login, then edit budget/models in the UI afterward.)
5. **IdC console → Reset password** for the new user (console-only, no API) → user runs `aws sso login`.

None of that touches `handler.py` again. Raising a team's budget, changing its model allowlist, or adding
another org later is **also** pure UI (`Teams` → edit) or IdC console work — an admin can do all of it
without a developer.

### Anti-pattern to avoid: hard-coded tier branches

Do **not** generate Lambda code that branches on specific permission-set names (for example,
`if permission_set in {...}: use economy_team else standard_team`). That design makes every new org/tier a
code change and redeploy, which violates this skill's steady-state onboarding goal.

The supported pattern is:

- `team_alias = permission_set` in `org-sso`.
- Optional `TIER_CONFIG[team_alias]` only seeds first-time team creation with starter `models`/`max_budget`.
- Once the team exists, LiteLLM Admin UI is the source of truth for model allowlist and budgets.

A third option — an external DynamoDB/SSM mapping table instead of in-code constants — can also remove
per-onboarding redeploys, but only use it if the customer explicitly needs a separately auditable mapping
outside IdC/LiteLLM. It is not required for the default skill output.

The LiteLLM team itself can be pre-created via Admin UI (**Teams → + New Team**) or left to auto-create on
the group's first login — see `shared/reference/litellm-admin-guide.md` §2.

The most common failure mode is a name mismatch: IdC looks fully provisioned (user, group, permission set,
account assignment all correct), but calls land in an unexpected team because the permission-set name and
LiteLLM `team_alias` differ. Verify with: `aws sso login` → first gateway call → Admin UI **Teams** shows the
expected `team_alias`/`team_id`/`max_budget` for that login.
