# LLM Gateway Governance Skill

> Build a **governed LLM gateway** so internal developers can use code agents (Claude Code, Codex)
> against Amazon Bedrock through one control point — identity (SSO **or** Cognito), per-user virtual
> keys, model/cost tiering, Bedrock Guardrails, network isolation, and tracing. Generates AWS CDK +
> Lambda + LiteLLM config + onboarding scripts, customized to the user's requirements.
>
> Reference implementation: `llm-gateway-multi-agent` (LiteLLM + Bedrock + AgentCore, CDK TypeScript).

## Trigger phrases

```
"Build an LLM gateway on AWS"
"Govern Bedrock access for developers / central proxy for Claude Code and Codex"
"Deploy LiteLLM on AWS with SSO virtual keys"
"Build a developer-facing Bedrock governance gateway"
"Build an internal proxy for Claude Code and Codex"
"Deploy LiteLLM and issue virtual keys via SSO or Cognito"
"Govern Bedrock when we only have an IAM Identity Center account instance (no org SSO)"
```

## What it builds

A LiteLLM proxy on ECS Fargate behind an ALB edge (TLS via `certMode`: `acm` / `http` — always internet-facing, SG CIDR-restricted), with:
- **Token Service** issuing per-user LiteLLM virtual keys, in one of two auth modes: `org-sso` (API Gateway IAM auth, IdC **organization** instance, permission sets → `config.sso`) or **`cognito-native`** (API Gateway Cognito authorizer; an Amazon Cognito User Pool is the sole identity source — for IdC **account** instances or accounts with no usable org SSO → `config.cognitoNative`)
- **Bedrock Guardrails** (content / PII / denied topics) for Claude routes
- **Managed web search** — AgentCore **Web Search Tool** (built-in `web-search` connector on an AgentCore Gateway, us-east-1; AWS_IAM SigV4). No third-party API key. (Replaces Tavily MCP.)
- **Model & cost tiering** (model allowlist + budget cap, e.g. `gpt-5.4`) by authorization unit — permission set in `org-sso`, Cognito User Pool Group in `cognito-native`
- **Bedrock Mantle (GPT-5.x)** reached privately in **us-east-1** over cross-region **VPC peering** (Bearer-token auth minted at runtime from the Task Role — the Mantle Responses route has no SigV4 path)
- **Aurora Serverless v2** for LiteLLM + (optional) Langfuse trace storage
- **Network isolation** (isolated DB, VPC endpoints; a public ALB edge in both modes with an `albIngressCidrs` SG allowlist — `acm` HTTPS:443, `http` HTTP:80 [plaintext, PoC-only]; a separate internal ALB for the Token Service)
- **Claude tokenless auth** (ECS Task Role SigV4 — nothing to rotate); Mantle uses a short-term runtime-minted Bearer key (no long-term secret, no scheduler)
- **Selectable region** (`config.awsRegion`); optional **Langfuse** and optional **custom domain** (works domain-less too). The 11 stacks include conditional ones — AgentCoreGateway, MantleNetwork/MantlePeeringRoutes (only with GPT/web search), and Langfuse (only if enabled) — so a minimal PoC deploys fewer.

## Install

### Claude Code
```bash
mkdir -p ~/.claude/skills
ln -sf "$(pwd)/claude-code/skills/llm-gateway-governance" ~/.claude/skills/llm-gateway-governance
ln -sf "$(pwd)/shared" ~/.claude/skills/llm-gateway-governance/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
ln -sf "$(pwd)/kiro/skills/llm-gateway-governance" ~/.kiro/skills/llm-gateway-governance
ln -sf "$(pwd)/shared" ~/.kiro/skills/llm-gateway-governance/shared
```

### Amazon Quick
```bash
mkdir -p ~/.quickwork/skills
ln -sf "$(pwd)/quick/skills/llm-gateway-governance" ~/.quickwork/skills/llm-gateway-governance
ln -sf "$(pwd)/shared" ~/.quickwork/skills/llm-gateway-governance/shared
```

(Use `cp -r` instead of `ln -sf` on Windows or where symlinks are unavailable.)

## Outputs

This skill generates a complete CDK app:
- `bin/app.ts`, `cdk.json`, `package.json`, `config/dev.json`
- `lib/*-stack.ts` — Network, Data, Guardrail, AgentCoreGateway(us-east-1), LiteLLM, Langfuse, Auth, Observability, Cdn(us-east-1), MantleNetwork(us-east-1), MantlePeeringRoutes
- `lib/interfaces.ts`, `lib/config/{constants,schema}.ts`, `lib/nag-suppressions.ts`
- `lambda/token-service/handler.py`, `lambda/db-init/handler.py`
- `services/litellm/{config.yaml,Dockerfile,entrypoint.sh}`
- `scripts/{get-gateway-token,setup-developer,healthcheck}.sh`, `templates/{claude-settings.json,codex-config.toml}`

## MCP requirements

| MCP | Purpose | Required? |
|---|---|---|
| AWS Knowledge MCP | Verify model IDs, regional availability, service docs | **Recommended** (model IDs are volatile) |
| CloudFormation MCP | Stack validation | Optional |

## Knowledge sources

All real knowledge lives in [`shared/`](./shared/):
- `shared/reference/` — **prerequisites** (Docker/CLI/IAM/Bedrock model access/IdC readiness), architecture, decision tree, AWS service/model catalog, constraints, SSO setup, **LiteLLM admin operations** (Admin UI login, team/user creation via SSO mapping, log/trace inspection, budget management)
- `shared/patterns/` — CDK stacks, Lambda handlers, LiteLLM gateway config, developer onboarding
- `shared/examples/` — industry/domain instantiations

## Layout

```
llm-gateway-governance-skill/
├── README.md                                              (this file)
├── claude-code/skills/llm-gateway-governance/SKILL.md     (md5-identical to the other two)
├── kiro/skills/llm-gateway-governance/SKILL.md            (md5-identical)
├── quick/skills/llm-gateway-governance/SKILL.md           (md5-identical)
├── shared/{reference,patterns,examples}/
└── evals/<scenario>.md
```

Editing: change `claude-code/.../SKILL.md`, then `../scripts/sync-skills.sh llm-gateway-governance-skill` and `../scripts/sync-skills.sh verify`.
