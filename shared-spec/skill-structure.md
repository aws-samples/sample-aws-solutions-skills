# Skill Directory Structure Spec

This is the standard layout every skill in `aws-solution-skills/` MUST follow. The repo adopts the **Anthropic Agent Skills standard** ([agentskills.io/specification](https://agentskills.io/specification)) and applies it consistently to all three target tools (Claude Code, Kiro, Amazon Quick).

> **Core principle — "Write once, install three places"**: a single `SKILL.md` is placed verbatim into each tool's `skills/` directory. Tools differ only in install location; content is identical (md5 hash check enforced).

## Required tree

```
<solution-name>-skill/
├── README.md                          ★ skill's own overview and usage
│
├── claude-code/
│   └── skills/
│       └── <skill-name>/
│           └── SKILL.md               ★ Anthropic Agent Skills format (frontmatter + body)
│
├── kiro/
│   └── skills/
│       └── <skill-name>/
│           └── SKILL.md               ★ same SKILL.md (md5-identical to claude-code/)
│
├── quick/
│   └── skills/
│       └── <skill-name>/
│           └── SKILL.md               ★ same SKILL.md (md5-identical to claude-code/)
│
├── shared/                            ⭐ the actual knowledge — referenced by SKILL.md
│   ├── reference/
│   │   ├── architecture.md            Solution architecture + WHY
│   │   ├── decision-tree.md           Conditional selection logic
│   │   ├── aws-services.md            Service / model catalog
│   │   └── constraints.md             Known limits and gotchas
│   │
│   ├── patterns/
│   │   ├── cdk-stacks.md              Full CDK source
│   │   ├── lambda-handlers.md         Full Lambda source + pitfalls
│   │   ├── frontend-pages.md          React + Tailwind + shadcn/ui
│   │   └── <domain-specific>.md       (optional)
│   │
│   └── examples/
│       └── <industry>.md              At least 2-3 domain instantiations
│
└── evals/
    └── <domain>-scenario.md           Verification scenarios
```

## File-by-file role

### `<skill>/README.md`
Skill-level overview. Linked from the root `README.md`. Contains:
- Trigger phrases (English + Korean if applicable)
- Three install commands (Claude Code / Kiro / Amazon Quick)
- Output summary
- MCP requirements

### `<skill>/{claude-code,kiro,quick}/skills/<skill-name>/SKILL.md`

**Three identical copies of the same file**. Content MUST be md5-identical.

Frontmatter follows [agentskills.io spec](https://agentskills.io/specification):

```yaml
---
name: <skill-name>            # Lowercase, hyphens only, max 64 chars, must match parent dir name
description: |
  What the skill does + when to use it. Include trigger keywords (English and Korean).
  Max 1024 chars. Claude/Kiro/Quick all use this for skill activation matching.
license: MIT                  # Optional but recommended
metadata:                     # Optional, free-form key-value
  version: "1.0"
  author: aws-solution-skills
---
```

Body structure (recommended):
```markdown
# <Skill Name>

## Purpose
1-2 paragraphs.

## Knowledge sources
- `shared/reference/architecture.md` — ...
- `shared/patterns/cdk-stacks.md` — ...
- (full list of shared/* files)

## Workflow
### Phase 1: Discovery
### Phase 2: Architecture Design
### Phase 3: Code Generation
### Phase 4: Validate
### Phase 5: Deploy

## Hard Constraints
1. ...

## When to call MCP
```

Length target: **200–400 lines**. Under 500 lines per agentskills.io best-practice.

### `<skill>/shared/`

**All real knowledge lives here.** SKILL.md references these paths.

When the skill is installed, `shared/` is bundled alongside SKILL.md so relative paths still resolve. Detailed authoring rules: [`shared-knowledge-pattern.md`](./shared-knowledge-pattern.md).

### `<skill>/evals/`

Black-box scenario verification. Each scenario is a checklist of expected outputs.

## Naming conventions

- Skill folder: kebab-case + `-skill` suffix (e.g., `unified-customer-profile-skill`)
- Skill name (`name` frontmatter field + parent directory of SKILL.md): kebab-case, no `-skill` suffix (e.g., `unified-customer-profile`)
- shared files: kebab-case `.md`

## Install layout (for reference)

After running install commands, files end up in:

| Tool | Install path |
|---|---|
| Claude Code | `~/.claude/skills/<skill-name>/SKILL.md` (+ `shared/`, `evals/`) |
| Kiro | `~/.kiro/skills/<skill-name>/SKILL.md` (+ `shared/`, `evals/`) |
| Amazon Quick | `~/.quickwork/skills/<skill-name>/SKILL.md` (+ `shared/`, `evals/`) |

The repo's per-tool layout (`{claude-code,kiro,quick}/skills/<name>/`) mirrors the install location, so symlinking the repo path into the install path works directly.

## Cross-skill shared assets (root level)

Some assets are shared across multiple skills (e.g., sample test data). These live at repo root:

```
aws-solution-skills/
├── sample-data/                       ← cross-skill shared (e.g., ERP fixtures)
│   └── erp/
└── <each-skill>/...
```

Skills that depend on cross-skill assets MUST document the dependency in their README and the SKILL.md description.

## Identity check (CI gate)

Three SKILL.md files per skill MUST be md5-identical:

```bash
for skill in <root>/*-skill; do
  md5_a=$(md5sum "$skill/claude-code/skills/"*/SKILL.md | awk '{print $1}')
  md5_k=$(md5sum "$skill/kiro/skills/"*/SKILL.md | awk '{print $1}')
  md5_q=$(md5sum "$skill/quick/skills/"*/SKILL.md | awk '{print $1}')
  [ "$md5_a" = "$md5_k" ] && [ "$md5_k" = "$md5_q" ] || echo "DRIFT in $skill"
done
```

Drift between the three copies is the #1 bug to catch in CI.

## Impact of missing pieces

| Missing | Tool-specific impact |
|---|---|
| Any of the three SKILL.md files | Tool will not pick up the skill on `~/.<tool>/skills/` |
| md5 mismatch between three files | Tools see different behaviors — drift bug |
| `shared/` | SKILL.md references break — skill hallucinates code |
| `evals/` | No regression check on changes |
| Frontmatter `name` field mismatch with parent directory | agentskills.io validation fails |

## Auto-skeleton

Copy [`template/`](../template/) verbatim:

```bash
cp -r template <new-name>-skill
```

The `template/` is empty placeholders following this exact structure.

## Why this layout

The repo originally had one entry format per tool (`claude-code/CLAUDE.md` + `commands/`, `kiro/steering.md` + `specs/`, `quick/SKILL.md`). That layout was discarded because:

1. **All three tools natively accept the Anthropic Agent Skills SKILL.md format** — there is no semantic gain from per-tool entry formats.
2. **Tool-specific entry formats (`CLAUDE.md`, Kiro Steering, Quick custom frontmatter) require per-tool authoring effort** that drifts over time.
3. **The Anthropic Skills standard is the actual lowest common denominator** — it covers trigger description, progressive disclosure, supporting files, and is recognized by all three tools.

The "per-tool variation" was, in retrospect, a self-imposed cost without corresponding benefit. The current layout — one SKILL.md, three install paths — is what datalab-skills demonstrated and what this repo now adopts.
