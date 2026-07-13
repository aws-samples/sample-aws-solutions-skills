# Eval — `cognito-native` rollout (IdC account instance / no usable org SSO)

## Scenario

A partner-owned (payer) account has only an IAM Identity Center **account instance**: `aws sso-admin list-instances` returns an `OwnerAccountId` that is **not** the AWS Organizations management account, and the team cannot use the organization's IdC. The user asks the skill to build the LLM gateway and wants per-team model/budget governance without any org SSO.

## Expected skill behavior

### A. Discovery + `authMode` selection
- [ ] Discovery detects the account instance (or absence of a usable organization instance) and selects **`authMode="cognito-native"`** — not `org-sso`, and **not** `account-sso`.
- [ ] The skill explicitly states that an IdC **account instance cannot host a SAML 2.0 customer-managed application** (AWS-confirmed), so Cognito↔IdC SAML federation is impossible, and that `cognito-native` uses an Amazon Cognito User Pool as the **sole identity source** (no external IdP, no IdC, no Identity Store).
- [ ] The skill states `aws sso login` is not used at all in this mode.
- [ ] GATE 1 summarizes `config/dev.json` incl. `awsRegion`, `authMode="cognito-native"`, `litellm.certMode` (acm/http — orthogonal to authMode; Langfuse only with `certMode='acm'`) + `litellm.albIngressCidrs` (with the plaintext acknowledgement if `http`), `cognitoNative` (teamGroupPrefix / multiGroupStrategy / optional passwordMinLength / refreshTokenValidityDays), the **initial user(s)** to create post-deploy (email + team group — the pool ships with zero users), `agentcore`, `mantle`, and — if Fable/Mythos-class models are requested — the `provider_data_share` opt-in acknowledgement.

### B. config/dev.json derivation
- [ ] Contains `authMode: "cognito-native"` and a `cognitoNative` block; **no** `sso` block and **no** `accountSso` block.
- [ ] `cognitoNative.teamGroupPrefix` (e.g. `llmgw-`) and `multiGroupStrategy: "require-single-team-group"` present or defaulted.

### C. AuthStack generation
- [ ] AuthStack **creates** a Cognito User Pool (sole identity source), a Hosted UI domain, an app client (Authorization Code + PKCE, loopback redirect `127.0.0.1:8400`/`localhost:8400`, `supportedIdentityProviders: [COGNITO]`), and one `CfnUserPoolGroup` per team (group name == `team_alias`).
- [ ] The Token Service method uses a `CognitoUserPoolsAuthorizer` (`AuthorizationType.COGNITO`).
- [ ] Token Lambda env includes `AUTH_MODE=cognito-native`, `COGNITO_TEAM_GROUP_PREFIX`, `COGNITO_MULTI_GROUP_STRATEGY`; **no** `identitystore:*` IAM is granted and **no** `ACCOUNT_SSO_*` / Identity Store env appears.
- [ ] Outputs include `CognitoUserPoolId`, `CognitoAppClientId`, `CognitoHostedUiDomain`, `CognitoIssuer`, `CognitoTeamGroupPrefix`, `LoginCommand=llmgw-login`.

### D. Token Lambda behavior
- [ ] Reads the verified `cognito:groups` claim from `requestContext.authorizer.claims` (no Identity Store call), parses it defensively (JSON string / list / comma-separated), filters by `teamGroupPrefix`, and requires exactly one match (else 403).
- [ ] Maps that single group name **1:1, unbranched** to the same-named LiteLLM `team_alias`; no `if group in {...}` tier logic. `TIER_CONFIG` seeds first-team-creation only.

### E. Onboarding
- [ ] Right after deploy, the agent **creates the initial user(s)** from the Discovery answer itself: `admin-create-user` (email invite, or `admin-set-user-password --permanent` when a password was supplied) + `admin-add-user-to-group` into exactly one `teamGroupPrefix` group — then runs the **full-path verification** with that user (`llmgw-login` → access token → Token Service → virtual key → `GET /v1/models`), not just a master-key test.
- [ ] Developer onboarding uses `llmgw-login` + `gateway_auth.py` (login/token/healthcheck/mcp-headers), never `aws sso login`.
- [ ] The token helper sends the Cognito **access token** (not the id_token).
- [ ] Claude Code settings include **all four** `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` vars.
- [ ] AgentCore Web Search MCP is registered client-side via `claude mcp add-json` + `headersHelper` → `gateway_auth.py mcp-headers` (not a static token).
- [ ] Base URL = the **`GatewayUrl` output** = the ALB domain (CloudFront is removed; `certMode` acm/http is orthogonal to `cognito-native`): `https://<custom-domain>` for `acm`, `http://<alb-dns>` for `http` (plaintext, SG-allowlisted — no `ca.pem`, no SSM tunnel).
- [ ] The Token Service (API Gateway execute-api) reaches LiteLLM over the **internal ALB (HTTP:4000)** SSM URL — unchanged by the edge choice (the Token Service was never behind CloudFront).
- [ ] Ends by generating the **two HTML onboarding docs** (`developer-setup.html` + `admin-onboarding.html`) via `scripts/gen-onboarding.py`.

## Failure checks (must NOT happen)
- [ ] Must not choose `authMode="account-sso"` or attempt Cognito↔IdC SAML federation on an account instance.
- [ ] Must not instruct the operator to create permission sets or use `aws sso login`.
- [ ] Must not grant `identitystore:*` or perform any Identity Store lookup in the Token Lambda.
- [ ] Must not claim IdC issues OIDC JWTs to the native helper — Cognito is the sole issuer.
- [ ] Must not hard-code group-to-tier `if` branches in the Lambda.
- [ ] Must not send the id_token to the Cognito authorizer (it 401s; access token only).
- [ ] Must not assume CloudFront/CdnStack or a `*.cloudfront.net` base URL (removed — the ALB is the edge), and must not tie `certMode` to `authMode`.

## Pass criteria
All of A–E satisfied and no failure check triggered. The deployed gateway lets a Cognito user in exactly one `llmgw-`-prefixed group obtain a virtual key scoped to the same-named LiteLLM team, with Claude (SigV4) and GPT-5.x (runtime-minted Bearer) both reachable.
