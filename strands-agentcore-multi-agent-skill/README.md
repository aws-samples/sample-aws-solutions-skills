# Strands × AgentCore Multi-Agent Builder — AI Skill

> **Knowledge distribution, not template distribution.** Instead of handing the user code, it gives them **the ability to build code**.

This directory is an AI Skill that **conversationally generates** a multi-agent system based on **Strands Agents + Amazon Bedrock AgentCore**.

The generated system: a single Orchestrator classifies intent and routes to the appropriate destination among (1) MCP server tools, (2) specialized Strands agents, and (3) a Knowledge Base.

It is written in the **Anthropic Agent Skills standard (`SKILL.md`)** format, so Claude Code, Kiro, and Codex all receive the same SKILL.md (md5-identical).

## Triggers (invoke via natural language)

```
"Build me a Strands AgentCore multi-agent system"
"Build me a multi-agent system with Strands and AgentCore"
"An integrated agent system for Jira and GitHub"
"A natural-language data analytics agent"
```

## Directory structure

```
strands-agentcore-multi-agent-skill/
├── README.md                                                          (this file)
├── claude-code/skills/strands-agentcore-multi-agent/SKILL.md          ★ identical in 3 places (md5-identical)
├── kiro/skills/strands-agentcore-multi-agent/SKILL.md                 ★
├── codex/skills/strands-agentcore-multi-agent/SKILL.md                ★
├── shared/                                                             ⭐ the actual knowledge (~5,000 lines)
│   ├── reference/
│   │   ├── architecture.md
│   │   ├── agentcore-primitives.md
│   │   ├── decision-tree.md
│   │   ├── aws-services.md
│   │   └── constraints.md (25 pitfalls)
│   ├── patterns/
│   │   ├── strands-agents.md
│   │   ├── mcp-servers.md
│   │   ├── cdk-stacks.md
│   │   ├── memory-hooks.md
│   │   ├── auth-patterns.md
│   │   └── frontend-pages.md
│   └── examples/
│       ├── devops-assistant.md
│       ├── data-analytics-agent.md
│       └── customer-support-agent.md
└── evals/
    ├── devops-scenario.md
    └── data-analytics-scenario.md
```

## Installation

### Claude Code
```bash
mkdir -p ~/.claude/skills
cp -r claude-code/skills/strands-agentcore-multi-agent ~/.claude/skills/
cp -r shared ~/.claude/skills/strands-agentcore-multi-agent/shared
```

Or symlink (reflects edits immediately):
```bash
ln -sf "$(pwd)/claude-code/skills/strands-agentcore-multi-agent" ~/.claude/skills/strands-agentcore-multi-agent
ln -sf "$(pwd)/shared" ~/.claude/skills/strands-agentcore-multi-agent/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
cp -r kiro/skills/strands-agentcore-multi-agent ~/.kiro/skills/
cp -r shared ~/.kiro/skills/strands-agentcore-multi-agent/shared
```

### Codex
```bash
mkdir -p ~/.agents/skills
cp -r codex/skills/strands-agentcore-multi-agent ~/.agents/skills/
cp -r shared ~/.agents/skills/strands-agentcore-multi-agent/shared
```

## Core design principles

1. **Single SKILL.md** — one Anthropic Agent Skills standard file, with identical copies across the 3 tools (CI verifies md5)
2. **Shared knowledge** — the actual knowledge (~5,000 lines) lives in one place, `shared/`. SKILL.md is a thin wrapper (~170 lines)
3. **MCP-first** — Bedrock model IDs, AgentCore region availability, and IAM actions are all verified at runtime via the AWS Knowledge MCP
4. **Gate pattern** — Discovery → Design → Generate → Validate → Deploy
5. **Multi-pattern integration**:
   - **MCP server (Gateway target)** — simple function calls (e.g., Jira REST)
   - **Specialized Strands Agent (direct invoke)** — multi-step reasoning (e.g., Text2SQL)
   - **Local tool** — a single AWS API call
6. **Golden Examples** — 3 industries: DevOps assistant / Data analytics / Customer support

## Generated system

```
Frontend (React + Vite + Tailwind + shadcn/ui + Amplify Authenticator)
   │ JWT (USER_PASSWORD_AUTH)
   ▼
Orchestrator Agent (Strands + AgentCore Runtime)
   │ ── SigV4 ─────────────► AgentCore Gateway ── OAuth2 ──► MCP Servers (FastMCP)
   │ ── Bearer ────────────► Specialized Strands Agents (direct invoke)
   │ ── bedrock:Retrieve ──► Bedrock Knowledge Base
   └── HookProvider ────► AgentCore Memory (short-term / semantic / user_pref)
```

## MCP requirements

| MCP | Purpose | Required |
|-----|------|-----------|
| AWS Knowledge MCP | Verify Bedrock model IDs, AgentCore region availability, IAM actions | Recommended |
| CloudFormation MCP | Stack validation / deployment | Optional |

## Cost estimates

See `shared/reference/aws-services.md` for detailed scenarios.

| Scenario | Monthly cost |
|---|---|
| Dev / PoC (single user, 100 queries/day, KB included) | ~$370 (KB is the largest share) |
| Dev / PoC (no KB) | ~$26 |
| Prod (1000 users, 10K queries/day) | ~$2,000 |

## Reference project

The patterns in this skill are extracted from the production-ready code in [`agentcore-multi-agent-workshop`](https://github.com/aws-samples/agentcore-multi-agent-workshop).

## Editing workflow

The SKILL.md across the 3 tools must be md5-identical:

```bash
# 1. Edit only the canonical copy (claude-code/)
$EDITOR claude-code/skills/strands-agentcore-multi-agent/SKILL.md

# 2. Sync to the other two locations
../scripts/sync-skills.sh strands-agentcore-multi-agent-skill

# 3. (Optional) Verify
../scripts/sync-skills.sh verify
```
