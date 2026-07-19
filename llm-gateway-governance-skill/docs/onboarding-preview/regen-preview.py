#!/usr/bin/env python3
"""regen-preview.py — rebuild docs/onboarding-preview/*.html from the CURRENT
templates in shared/patterns/onboarding/, with fixed SAMPLE values.

WHY: the preview HTMLs are generated artifacts. Editing them by hand drifts
from the templates on every template change — instead, re-run this script
whenever `developer-setup.html.tmpl` / `admin-onboarding.html.tmpl` /
`gen-onboarding.py` change:

    python3 docs/onboarding-preview/regen-preview.py

SHOWCASE MODE: unlike a real deploy (where gen-onboarding.py strips the
conditional blocks that do not match the deploy's authMode/certMode), the
preview keeps ALL conditional blocks so a reader sees the org-sso AND
cognito-native AND http-mode instructions in one document. Every value below
is an obviously-fake sample — no real endpoint, account id, or secret.
"""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent                      # docs/onboarding-preview/
SKILL_ROOT = HERE.parent.parent                             # llm-gateway-governance-skill/
GEN_PATH = SKILL_ROOT / "shared" / "patterns" / "onboarding" / "gen-onboarding.py"
TPL_DIR = SKILL_ROOT / "shared" / "patterns" / "onboarding"

spec = importlib.util.spec_from_file_location("gen_onboarding", GEN_PATH)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

# ---- fixed sample inputs (mirror a full-featured acm + langfuse deploy) ------
SAMPLE_OUTPUTS = {
    "LiteLLMStack": {
        "GatewayUrl": "https://llmgw.example.com",
        "AdminUiUrl": "https://llmgw.example.com/ui/",
        "AlbDns": "llmgw-alb-123456.us-east-1.elb.amazonaws.com",
        "MasterKeySecretArn": "llmgw-litellm-admin-key",
        "EcsCluster": "llmgw-litellm",
        "EcsService": "litellm",
    },
    "AuthStack": {
        "TokenServiceUrl": "https://abc123.execute-api.us-east-1.amazonaws.com/v1/auth/token",
        # cognito-native sample outputs
        "CognitoUserPoolId": "us-east-1_ABC123XYZ",
        "CognitoAppClientId": "1a2b3c4d5e6f7g8h9i0j",
        "CognitoHostedUiDomain": "llmgw-auth.auth.us-east-1.amazoncognito.com",
        "CognitoIssuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123XYZ",
        "CognitoTeamGroupPrefix": "llmgw-",
        # org-sso sample outputs (showcase keeps both variants)
        "SsoStartUrl": "https://d-1234567890.awsapps.com/start",
        "SsoAccountId": "111122223333",
        "SsoRoleName": "LlmGatewayUser",
    },
    "LangfuseStack": {"LangfuseUrl": "https://langfuse.example.com"},
    "ObservabilityStack": {
        "DashboardName": "llmgw-dev-dashboard",
        "DashboardUrl": "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
                        "?region=us-east-1#dashboards:name=llmgw-dev-dashboard",
    },
}
SAMPLE_CONFIG = {
    "awsRegion": "us-east-1",
    "authMode": "cognito-native",
    "enableLangfuse": True,
    "projectPrefix": "llmgw",
    "litellm": {"certMode": "acm", "masterKey": "sk-Xy9EXAMPLEmasterKEYdoNOTshare"},
    "langfuse": {"adminEmail": "admin@example.com", "adminPassword": "Adm1n-EXAMPLE-pw"},
}

# Showcase: keep EVERY conditional block (a real deploy keeps exactly one of each pair).
ALL_CONDITIONS = {"authMode=org-sso", "authMode=cognito-native",
                  "certMode=acm", "certMode=http", "langfuse=on"}


def main() -> None:
    flat = gen.flatten_outputs(SAMPLE_OUTPUTS)
    tokens, _real_active = gen.build_tokens(flat, SAMPLE_CONFIG)
    for name in ("developer-setup", "admin-onboarding"):
        html = (TPL_DIR / f"{name}.html.tmpl").read_text(encoding="utf-8")
        html = gen.fill(gen.strip_conditionals(html, ALL_CONDITIONS), tokens)
        dst = HERE / f"{name}.html"
        dst.write_text(html, encoding="utf-8")
        leftovers = [m for m in ("{{", "<!--IF", "<!--ENDIF") if m in html]
        status = f"  ⚠ leftover markers: {leftovers}" if leftovers else ""
        print(f"wrote {dst}{status}")


if __name__ == "__main__":
    main()
