# Unified Customer Profile Builder — AI Skill

> **Knowledge distribution, not template distribution.** Instead of handing users code, it gives them **the ability to generate code**.

This directory is an AI Skill that **conversationally generates** a Unified Customer Profile (AWS Connect CP + Entity Resolution + Bedrock AI) system.

It is written in the **Anthropic Agent Skills standard (`SKILL.md`) format**, so Claude Code, Kiro, and Amazon Quick all receive the same SKILL.md (md5-identical).

## Triggers (invoke in natural language)

```
"Build me a unified customer profile system"
"Create a Customer 360"
"Set up a customer profile integration"
```

For detailed trigger keywords, see the `description` field in `claude-code/skills/unified-customer-profile/SKILL.md`.

## Directory structure

```
unified-customer-profile-skill/
├── README.md                                                       (this file)
├── claude-code/skills/unified-customer-profile/SKILL.md            ★ identical in 3 places (md5-identical)
├── kiro/skills/unified-customer-profile/SKILL.md                   ★
├── quick/skills/unified-customer-profile/SKILL.md                  ★
├── shared/                                                          ⭐ the actual knowledge
│   ├── reference/
│   │   ├── architecture.md
│   │   ├── aws-services.md
│   │   ├── decision-tree.md
│   │   ├── constraints.md
│   │   └── calculated-attributes.md
│   ├── patterns/
│   │   ├── cdk-stacks.md
│   │   ├── lambda-handlers.md
│   │   ├── frontend-pages.md
│   │   ├── etl-transforms.md
│   │   ├── bedrock-prompts.md
│   │   └── er-strategies.md
│   └── examples/
│       ├── travel.md
│       ├── hotel.md
│       └── retail.md
└── evals/
    ├── travel-scenario.md
    ├── hotel-scenario.md
    └── retail-scenario.md
```

## Installation

### Claude Code
```bash
mkdir -p ~/.claude/skills
cp -r claude-code/skills/unified-customer-profile ~/.claude/skills/
cp -r shared ~/.claude/skills/unified-customer-profile/shared
```

Or symlink (changes apply immediately when editing):
```bash
ln -sf "$(pwd)/claude-code/skills/unified-customer-profile" ~/.claude/skills/unified-customer-profile
ln -sf "$(pwd)/shared" ~/.claude/skills/unified-customer-profile/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
cp -r kiro/skills/unified-customer-profile ~/.kiro/skills/
cp -r shared ~/.kiro/skills/unified-customer-profile/shared
```

### Amazon Quick
```bash
mkdir -p ~/.quickwork/skills
cp -r quick/skills/unified-customer-profile ~/.quickwork/skills/
cp -r shared ~/.quickwork/skills/unified-customer-profile/shared
```

## Core design principles

1. **Single SKILL.md** — one Anthropic Agent Skills standard file deployed as an identical copy to 3 tools (drift prevention, CI md5 hash verification)
2. **Shared knowledge** — the actual knowledge lives in one place, `shared/`. SKILL.md is a thin wrapper (~170 lines)
3. **MCP usage** — Bedrock model IDs, AgentCore regional availability, and IAM actions are all verified at runtime via the AWS Knowledge MCP
4. **Gate pattern** — Discovery → Design → Generate → Validate → Deploy (user approval at each phase)
5. **Incremental generation** — Core → Matching → Enrichment → Graph (added layer by layer)
6. **Golden Examples** — based on actual working demo code for Travel/Hotel/Retail

## MCP requirements

| MCP | Purpose | Required |
|-----|------|-----------|
| AWS Knowledge MCP | Look up service docs/availability/model IDs | Recommended |
| CloudFormation MCP | Stack validation/deployment | Optional |

## Editing workflow

The SKILL.md across all 3 tools must be md5-identical. When editing:

```bash
# 1. Edit only the canonical (claude-code/) copy
$EDITOR claude-code/skills/unified-customer-profile/SKILL.md

# 2. Sync to the other two locations
../scripts/sync-skills.sh unified-customer-profile-skill

# 3. (Optional) Verify
../scripts/sync-skills.sh verify
```

## Eval scenarios

Validate skill behavior with the industry-specific checklists in `evals/`:
- `travel-scenario.md` — travel/airline/hotel multi-domain
- `hotel-scenario.md` — hotel single domain + Calc Attribute
- `retail-scenario.md` — retail + loyalty program
