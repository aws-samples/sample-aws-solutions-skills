#!/usr/bin/env python3
"""gen-onboarding.py — generate post-deploy onboarding HTML from cdk outputs.

Emitted into the generated CDK app as `scripts/gen-onboarding.py`. Produces two
self-contained HTML docs from the token-ized templates in `templates/onboarding/`:
  - developer-setup.html   (SHAREABLE: Claude Code / Codex setup only, NO secrets)
  - admin-onboarding.html  (OPERATOR-ONLY: real values incl. secrets, password-change, offboarding)

Inputs: outputs.json (from `cdk deploy --outputs-file`) + config/dev.json.
The admin doc embeds the LiteLLM master key (config.litellm.masterKey) and, with
--fetch-secrets, the Langfuse admin secret from AWS Secrets Manager. The admin file is
written 0600 with a "do not share/commit" banner — add it to .gitignore.

Usage:
  python scripts/gen-onboarding.py --outputs outputs.json --config config/dev.json \
      --templates templates/onboarding --out-dir onboarding [--fetch-secrets]
"""
import argparse, json, os, re, stat, sys
from pathlib import Path


def flatten_outputs(o):
    flat = {}
    for _stack, kv in (o or {}).items():
        if isinstance(kv, dict):
            flat.update(kv)
    return flat


def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def strip_conditionals(html, active):
    """Keep <!--IF cond-->..<!--ENDIF--> only when `cond` is in `active`."""
    def repl(m):
        return m.group(2) if m.group(1).strip() in active else ""
    return re.sub(r"<!--IF ([^>]+)-->(.*?)<!--ENDIF-->", repl, html, flags=re.S)


def fill(html, tokens):
    for k, v in tokens.items():
        html = html.replace("{{%s}}" % k, str(v))
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", default="outputs.json")
    ap.add_argument("--config", default="config/dev.json")
    ap.add_argument("--templates", default="templates/onboarding")
    ap.add_argument("--out-dir", default="onboarding")
    ap.add_argument("--fetch-secrets", action="store_true",
                    help="pull the Langfuse admin secret value from AWS Secrets Manager (needs AWS creds)")
    args = ap.parse_args()

    flat = flatten_outputs(json.loads(Path(args.outputs).read_text()))
    config = json.loads(Path(args.config).read_text())
    litellm = config.get("litellm", {})
    lf = config.get("langfuse", {})
    aliases = litellm.get("modelAliases", {})

    auth_mode = config.get("authMode", "org-sso")
    cert_mode = litellm.get("certMode", "acm")
    region = config.get("awsRegion") or os.environ.get("AWS_REGION", "us-east-1")

    # Gateway URL: prefer an explicit output; fall back to ALB DNS (http mode serves plain HTTP).
    gateway_url = pick(flat, "GatewayUrl", "LiteLlmUrl")
    if not gateway_url:
        alb = pick(flat, "AlbDns", "PublicAlbDns")
        scheme = "http" if cert_mode == "http" else "https"
        gateway_url = ("%s://%s" % (scheme, alb)) if alb else "https://REPLACE-with-gateway-url"

    langfuse_url = pick(flat, "LangfuseUrl", "LangfuseCfDomain")
    langfuse_on = bool(langfuse_url) or bool(config.get("enableLangfuse"))

    langfuse_pw = lf.get("adminPassword", "(Secrets Manager 참조)")
    langfuse_secret = pick(flat, "LangfuseAdminSecretArn", default="%s-langfuse-admin" % config.get("projectPrefix", "llmgw"))
    if args.fetch_secrets and langfuse_on:
        try:
            import boto3
            sm = boto3.client("secretsmanager", region_name=region)
            val = json.loads(sm.get_secret_value(SecretId=langfuse_secret)["SecretString"])
            langfuse_pw = val.get("password", langfuse_pw)
        except Exception as e:  # noqa: BLE001
            print("WARN: could not fetch Langfuse secret: %s" % e, file=sys.stderr)

    token_cmd = litellm.get("helperPath", "~/.local/bin/get-gateway-token.sh")
    mcp_headers_cmd = ("~/.local/bin/gateway_auth.py mcp-headers"
                       if auth_mode == "cognito-native" else token_cmd + " mcp-headers")

    tokens = {
        "GATEWAY_URL": gateway_url,
        "ADMIN_UI_URL": pick(flat, "AdminUiUrl", default=gateway_url + "/ui/"),
        "TOKEN_SERVICE_URL": pick(flat, "TokenServiceUrl"),
        "LANGFUSE_URL": langfuse_url or "(비활성)",
        "REGION": region, "AUTH_MODE": auth_mode, "CERT_MODE": cert_mode,
        "OPUS": aliases.get("opus", "claude-opus-4-8"),
        "SONNET": aliases.get("sonnet", "claude-sonnet-5"),
        "HAIKU": aliases.get("haiku", "claude-haiku-4-5"),
        "FABLE": aliases.get("fable", "claude-fable-5"),
        "GPT": aliases.get("gpt", "gpt-5.5"),
        "APIKEY_HELPER": token_cmd, "TOKEN_CMD": token_cmd,
        "MCP_HEADERS_CMD": mcp_headers_cmd, "LOGIN_CMD": "llmgw-login",
        "COGNITO_POOL_ID": pick(flat, "CognitoUserPoolId"),
        "COGNITO_CLIENT_ID": pick(flat, "CognitoAppClientId"),
        "COGNITO_HOSTED_UI": pick(flat, "CognitoHostedUiDomain"),
        "COGNITO_ISSUER": pick(flat, "CognitoIssuer"),
        "TEAM_GROUP_PREFIX": pick(flat, "CognitoTeamGroupPrefix", default="llmgw-"),
        "SSO_START_URL": pick(flat, "SsoStartUrl"), "SSO_ACCOUNT_ID": pick(flat, "SsoAccountId"),
        "SSO_ROLE_NAME": pick(flat, "SsoRoleName"),
        "MASTER_KEY": litellm.get("masterKey", "(config.litellm.masterKey 참조)"),
        "MASTER_KEY_SECRET": pick(flat, "MasterKeySecretArn", default="%s-litellm-admin-key" % config.get("projectPrefix", "llmgw")),
        "LANGFUSE_ADMIN_EMAIL": lf.get("adminEmail", "admin@example.com"),
        "LANGFUSE_ADMIN_PW": langfuse_pw, "LANGFUSE_SECRET": langfuse_secret,
        "ECS_CLUSTER": pick(flat, "EcsCluster", default="%s-litellm" % config.get("projectPrefix", "llmgw")),
        "ECS_SERVICE": pick(flat, "EcsService", default="litellm"),
        "EXAMPLE_TEAM": pick(flat, "CognitoTeamGroupPrefix", default="llmgw-") + "team1",
    }
    active = {"authMode=%s" % auth_mode, "certMode=%s" % cert_mode}
    if langfuse_on:
        active.add("langfuse=on")

    tdir = Path(args.templates)
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)

    for name, secret in (("developer-setup", False), ("admin-onboarding", True)):
        html = (tdir / ("%s.html.tmpl" % name)).read_text(encoding="utf-8")
        html = fill(strip_conditionals(html, active), tokens)
        dst = out / ("%s.html" % name)
        dst.write_text(html, encoding="utf-8")
        if secret:
            os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            print("⚠  %s contains REAL admin secrets (master key, Langfuse pw)." % dst)
            print("   Do NOT commit or share broadly. Add it to .gitignore.")
        print("wrote %s" % dst)


if __name__ == "__main__":
    main()
