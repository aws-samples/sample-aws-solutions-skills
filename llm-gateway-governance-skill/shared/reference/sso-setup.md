# IAM Identity Center (SSO) Setup — preparation the skill must drive

The Token Service **only** accepts IAM Identity Center principals (`AWSReservedSSO_` ARN). So before anyone can use the gateway, the operator must provision IdC. The skill MUST ask the SSO Discovery questions and then drive (or hand off) this setup — it is not optional for the SSO path.

## Discovery questions (ask in Phase 1)

1. **Is IAM Identity Center already enabled** in the target account/org? If not, it must be enabled first (org management or delegated admin).
2. **IdC instance region** — which region hosts the IdC instance? (SSO login uses this region; it may differ from the gateway region — that's fine.)
3. **Identity source** — IdC built-in directory (default) or external IdP (Okta / Entra ID / Google)? Determines where users + passwords live.
4. **Permission set — create new or reuse?** Ask explicitly: *should I create a NEW permission set for this gateway, or reuse an existing one? what name?* Default to **creating a new, uniquely-named** one (e.g. `LlmGatewaySeoul`). ⚠️ Do **not** silently reuse a permission set just because its name matches a default (`ClaudeCodeUser`) — a name match is **not** ownership; an existing one may belong to another gateway/groups and editing it changes their access. If the user chooses reuse, confirm the exact ARN and that its inline policy `Resource` targets **this** gateway's region + Token Service API id. Then map group(s)/permission-set name(s) → LiteLLM team (budget cap + model allowlist) as the user wants.
5. **Assignment — which group or users?** Ask which IdC **group(s)** (preferred) or individual users to assign the permission set to, and create the assignment(s) accordingly. Never assume the assignment.
6. **Session duration** — default PT1H.

⛔ If the user wants the SSO path but IdC is not enabled / no users exist, surface this as a prerequisite at GATE 1.

## `config.sso` (generated) + AuthStack outputs

The SSO Discovery answers are written into a first-class **`config.sso`** block, consumed by `AuthStack`:

```jsonc
"sso": {
  "startUrl": "https://<IDC_ID>.awsapps.com/start",  // IdC access portal URL
  "region": "us-east-1",                              // IdC home region (may differ from awsRegion)
  "accountId": "123456789012",                        // 12-digit account devs assume into
  "roleName": "ClaudeCodeUser"                         // permission set / role name (no underscore)
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
  --name ClaudeCodeUser --session-duration PT1H \
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
#   sso_role_name  = ClaudeCodeUser
#   region = <gw-region>
# NOTE: the profile is named after the gateway ("llm-gateway"), not a single
# client — Claude Code AND Codex share this one profile via the key helper.
aws sso login --profile llm-gateway
```
Then the key helper (`apiKeyHelper`/`auth.command`) fetches the virtual key automatically. See `developer-onboarding.md`.

## Gotchas (must respect)

- **Permission-set name MUST NOT contain `_`.** The Token Lambda parses `AWSReservedSSO_<PermissionSetName>_<id>` with `[^_/]+` for the name — an underscore truncates/breaks the tier match. Use `ClaudeCodeUser`, `ClaudeCodeEconomy` (camelCase, no underscore).
- **Least privilege**: the permission set needs only `execute-api:Invoke` on the Token Service API ARN — nothing else to use the gateway.
- **IdC region ≠ gateway region is OK.** SSO login uses the IdC region; the token helper derives the SigV4 region from the Token Service URL, so they're independent.
- **Password activation is console-only** — plan a human handoff step; full automation isn't possible for the built-in directory.
- **Teams**: assign a **group** (PrincipalType=GROUP) instead of per-user assignments — scales and is easier to revoke.
- **Non-SSO callers are rejected (403)** by design — verify with `aws sts get-caller-identity` that the assumed role is `AWSReservedSSO_...` before debugging the gateway.
