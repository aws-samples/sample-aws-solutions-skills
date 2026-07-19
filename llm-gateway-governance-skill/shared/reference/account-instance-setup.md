# `cognito-native` Setup — Amazon Cognito as the sole identity source

Use this document when Discovery determines that `org-sso` is **not usable** — most commonly when the target IAM Identity Center (IdC) instance is an **account instance** (a member/standalone account, e.g. a partner is the payer and owns the organization IdC), or when the account has no usable IdC at all.

> ⚠️ **Why not IdC federation on an account instance?** An IdC **account instance cannot host a SAML 2.0 customer-managed application** — confirmed against the AWS IdC instance-type capability matrix (SAML customer-managed applications = *Yes* for organization instances, *No* for account instances) and reproduced in the console (the "add application" flow offers no SAML option, only OAuth 2.0). The only customer-managed application type an account instance supports is OAuth 2.0 for **trusted identity propagation**, which is the *inverse* direction (an already-authenticated external app propagates identity **to** IdC) and cannot serve as a login/IdP for this gateway. Therefore "Cognito SAML SP ↔ IdC SAML IdP federation" is **impossible at the AWS level** on an account instance. Do not attempt it. This mode replaces the earlier `account-sso` design, which assumed that (impossible) federation.

In `cognito-native`, an **Amazon Cognito User Pool is the only identity store**: no external IdP, no IdC federation, no Identity Store lookups. Team membership is modeled as **native Cognito User Pool Groups**, and the group name Cognito stamps into the `cognito:groups` JWT claim is read directly by the Token Lambda.

## What changes vs. organization SSO

| Topic | `org-sso` organization instance | `cognito-native` |
|---|---|---|
| Developer login command | `aws sso login --profile <profile>` | `llmgw-login` (Cognito Hosted UI, PKCE) |
| Token Service trust anchor | API Gateway `AWS_IAM` + SigV4 caller ARN | API Gateway `COGNITO_USER_POOLS` authorizer (verifies the Cognito JWT) |
| Primitive used for teams | Permission set name | Cognito User Pool Group name |
| Team mapping invariant | Permission set name == LiteLLM `team_alias` | Cognito group name == LiteLLM `team_alias` |
| Identity source | IAM Identity Center | Cognito User Pool only (no IdC, no external IdP) |
| Client credential material | SSO-provided AWS temporary credentials | Cognito access token cached locally, refreshed via refresh token |
| Generated config | `config.authMode="org-sso"`, `config.sso` | `config.authMode="cognito-native"`, `config.cognitoNative` |

`aws sso login` is **not used at all** in this mode.

## Required AWS setup (all created by AuthStack — no manual IdC work)

The `AuthStack` (CDK) provisions everything; there is no permission set, no Identity Store, and no SAML app to configure by hand:

1. **Cognito User Pool** — `selfSignUpEnabled: false`, sign-in alias `email`, a password policy driven by `cognitoNative.passwordMinLength` (default 12), `removalPolicy: RETAIN`.
2. **Hosted UI domain** — Cognito's own login form is the entire auth surface (`supportedIdentityProviders: [COGNITO]`, no external IdP).
3. **App client** — `generateSecret: false`, Authorization Code + PKCE, loopback redirect URIs `http://127.0.0.1:8400/callback` and `http://localhost:8400/callback`, `refreshTokenValidity` from `cognitoNative.refreshTokenValidityDays` (default 30). Refresh-token validity is effectively "how long a developer stays logged in before re-running `llmgw-login`".
4. **User Pool Groups = teams** — one `cognito.CfnUserPoolGroup` per team, group name == LiteLLM `team_alias` (e.g. `llmgw-dev1`, `llmgw-dev2`). Cognito automatically adds a `cognito:groups` claim to every issued token based on membership.
5. **Token Service** — a Lambda behind a `CognitoUserPoolsAuthorizer`; **no `identitystore:*` IAM is granted** because there is no Identity Store round-trip.

⚠️ **The AuthStack creates ZERO users.** Right after deploy, the agent creates the **initial user(s)** captured in Discovery (email + team group) — without at least one group-assigned user, the full-path verification (login → token → virtual key) cannot run:

```bash
# 1) Create the user (email invite with a temporary password)
aws cognito-idp admin-create-user \
  --user-pool-id <pool-id> --username dev@example.com \
  --user-attributes Name=email,Value=dev@example.com \
  --desired-delivery-mediums EMAIL

# 2) Assign to exactly ONE team group (group name == LiteLLM team_alias)
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <pool-id> --username dev@example.com --group-name llmgw-dev1

# (optional) Skip the email invite and activate immediately with an operator-supplied password
aws cognito-idp admin-set-user-password \
  --user-pool-id <pool-id> --username dev@example.com --password '<password>' --permanent

# 3) Full-path verification with that user (NOT the master key):
#    llmgw-login → access token → Token Service → virtual key → GET /v1/models
```

Ongoing admin work (create more users, assign to groups, disable/offboard) is **Cognito console/CLI only** — the same commands, documented in `admin-onboarding.html`.

## Generated `config.cognitoNative`

```jsonc
{
  "awsRegion": "ap-northeast-2",
  "authMode": "cognito-native",
  "cognitoNative": {
    "teamGroupPrefix": "llmgw-",
    "multiGroupStrategy": "require-single-team-group",
    "passwordMinLength": 8,
    "refreshTokenValidityDays": 30
  }
}
```

Validation rules (enforced in `lib/config/schema.ts`):
- `authMode` defaults to `org-sso`; `cognito-native` must be explicit (or accepted from auto-detection at GATE 1).
- `cognitoNative` is optional — all fields have defaults applied in AuthStack. Validate only when provided.
- `multiGroupStrategy`, if set, must be `require-single-team-group` (the only supported value today: exactly one matching group is required; zero or more than one → 403).
- `refreshTokenValidityDays`, if set, must be 1–3650 (Cognito App Client limit).
- `passwordMinLength`, if set, must be 6–99 (Cognito User Pool limit).
- `teamGroupPrefix` scopes which Cognito groups count as teams; groups not starting with the prefix are ignored. Recommended for any pool that may hold non-team groups.

## Token Service behavior for `cognito-native`

1. API Gateway's `CognitoUserPoolsAuthorizer` validates the Cognito JWT (signature/issuer/audience/expiry) **before** the Lambda runs, and exposes the verified claims at `requestContext.authorizer.claims` — including `cognito:groups`.
2. The Lambda reads `cognito:groups` from the verified claims (**no Identity Store call** — the group list is already inside the trusted JWT). The claim may arrive as a JSON string, a native list, or a comma-separated string; parse all three defensively.
3. Filter group names by `COGNITO_TEAM_GROUP_PREFIX`; apply `COGNITO_MULTI_GROUP_STRATEGY=require-single-team-group` (exactly one match, else 403).
4. That single group name **is** the LiteLLM `team_alias`, unbranched (no `if group in {...}` mapping). Onboarding a new team is Cognito console work only (create group + add users), never a Lambda redeploy.
5. Look up the cached virtual key in DynamoDB (`pk=USER#cognito-native:<sub>`); on a miss, call LiteLLM `/key/generate` (master key from Secrets Manager) with the resolved `team_id`, cache best-effort, and return `{"api_key": "sk-..."}`.

No `identitystore:*` (or any IdC) IAM actions are required.

## Client onboarding — cross-platform

`cognito-native` clients use a common Python core and thin OS launchers:

| Purpose | macOS/Linux | Windows |
|---|---|---|
| Login | `scripts/llmgw-login.sh` | `scripts/llmgw-login.ps1` |
| Token helper | `scripts/get-gateway-token.sh` | `scripts/get-gateway-token.ps1` |
| Setup | `scripts/setup-developer.sh` | `scripts/setup-developer.ps1` |
| Healthcheck | `scripts/healthcheck.sh` | `scripts/healthcheck.ps1` |
| Shared core | `scripts/gateway_auth.py` | same file (`python gateway_auth.py ...`) |

`gateway_auth.py` uses only `webbrowser`, `http.server`, `pathlib`, `urllib`, `json` (no shell-only behavior), so it runs unmodified from a `.sh` or `.ps1` launcher. Its subcommands are `setup`, `login`, `token`, `healthcheck`, and `mcp-headers` — **`setup` holds ALL derivation/merge logic once** (the `.sh`/`.ps1` setup scripts are thin wrappers), copies the core to `~/.llm-gateway/gateway_auth.py` (a stable, repo-independent path), and writes `~/.llm-gateway/config.json`. Tokens are stored under `~/.llm-gateway/` (= `%USERPROFILE%\.llm-gateway` on Windows) with user-only permissions — POSIX `0600`, plus `icacls` on Windows where `chmod` is a no-op; refresh tokens are never printed.

The launchers resolve their own real path (bash: a `readlink` loop over `$BASH_SOURCE`; PowerShell: `$PSScriptRoot`) so they work from **any** working directory — including a `~/.local/bin` symlink or a PowerShell-profile function. Do not write launchers that assume the repository cwd. **Every `.ps1` launcher must end with `exit $LASTEXITCODE`** (PowerShell 5.1 does not propagate native exit codes otherwise — the token helper's non-zero-exit contract breaks silently) and should prefer the `py -3` launcher over bare `python` (Microsoft Store alias stub risk).

For Windows, `setup` writes the client config values as an explicit Python invocation (no execution-policy dependency, no bash). It uses `sys.executable` (never bare `python`) plus the `~/.llm-gateway` copy it just installed, so this path really exists:

```jsonc
// written automatically by `gateway_auth.py setup` on Windows
"apiKeyHelper": "\"C:\\Program Files\\Python312\\python.exe\" \"C:\\Users\\<user>\\.llm-gateway\\gateway_auth.py\" token"
```

## Verification checklist

- [ ] The **initial user(s)** from Discovery exist (`admin-create-user`) and each is in exactly one `teamGroupPrefix` group (`admin-add-user-to-group`) — the pool ships with zero users, so this is a deploy-time agent step, not an afterthought.
- [ ] `llmgw-login` opens a browser, completes the Cognito Hosted UI login, and caches tokens.
- [ ] The token helper sends the **access token** (`token_use=access`), not the `id_token` — the Cognito authorizer 401s on an id_token even though it also carries `cognito:groups`.
- [ ] The Token Lambda resolves the expected team from the `cognito:groups` claim (no Identity Store call).
- [ ] A user in no matching `teamGroupPrefix` group receives 403.
- [ ] A user in two matching groups receives 403 under `require-single-team-group`.
- [ ] The returned LiteLLM virtual key can call `GET /v1/models` through the gateway URL (the `GatewayUrl` output = the ALB domain; CloudFront is removed).
- [ ] Windows PowerShell setup and token helper work without `sed`, `chmod`, or Unix path assumptions, and from any cwd; each `.ps1` propagates the exit code (`exit $LASTEXITCODE`) so a failed token fetch is visible to Claude Code/Codex.
- [ ] Existing `org-sso` path still uses `aws sso login` and `AWSReservedSSO_` ARN parsing unchanged.

## Gotchas

- **Hosted UI domain prefix is GLOBALLY unique (real-deploy incident)**: a generic `domainPrefix` like `llmgw-dev-auth` may already be owned by another AWS customer anywhere in the world, and the AuthStack deploy fails with a **misleading "domain ... does not exist"** error (it means AlreadyExists). The generated AuthStack suffixes the account id (`llmgw-dev-auth-<accountId>`) — see `constraints.md` → "Cognito Hosted UI domain prefix".
- **id_token vs access_token**: the API Gateway `COGNITO_USER_POOLS` authorizer accepts only `token_use=access`. Send the id_token and you get 401 — a subtle bug because both tokens carry `cognito:groups`.
- **Stale local client config → blank Hosted UI page (real-deploy incident)**: leftover `~/.llm-gateway/config.json`/token caches from a previous/different deployment carry an old `appClientId`/domain — Cognito's Hosted UI renders a **blank page** for an invalid `client_id` (no error shown). After any redeploy, regenerate the local config from the new outputs and delete stale token caches; verify server-side with `aws cognito-idp describe-user-pool-client --client-id <id>` (`ResourceNotFoundException` = stale id) instead of debugging the browser.
- **Do not attempt SAML/IdC federation on an account instance** — it is not offered by AWS (see the callout at the top).
- Group names are operational API contracts. Renaming a Cognito group changes team routing unless an external mapping layer is introduced.
- Browser loopback redirect must be allow-listed in the Cognito app client. Some locked-down desktops block local listeners; document a device-code alternative only after validating it.
- Token cache files are bearer material. Store with user-only permissions and never print refresh tokens in diagnostics. ⚠️ `chmod 0600` is a **no-op on Windows** — back it with `icacls <file> /inheritance:r /grant:r <user>:F` (the `_restrict_perms` helper in `gateway_auth.py` does both).
