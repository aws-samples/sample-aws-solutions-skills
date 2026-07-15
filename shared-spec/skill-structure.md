# Skill Directory Structure Spec

This is the standard layout every skill in `aws-solution-skills/` MUST follow. The repo adopts the **Anthropic Agent Skills standard** ([agentskills.io/specification](https://agentskills.io/specification)) and applies it consistently to all three target tools (Claude Code, Kiro, Codex).

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
├── codex/
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
- Three install commands (Claude Code / Kiro / Codex)
- Output summary
- MCP requirements

### `<skill>/{claude-code,kiro,codex}/skills/<skill-name>/SKILL.md`

**Three identical copies of the same file**. Content MUST be md5-identical.

Frontmatter follows [agentskills.io spec](https://agentskills.io/specification):

```yaml
---
name: <skill-name>            # Lowercase, hyphens only, max 64 chars, must match parent dir name
description: |
  What the skill does + when to use it. Include trigger keywords (English and Korean).
  Max 1024 chars. Claude/Kiro/Codex all use this for skill activation matching.
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
| Codex | `~/.agents/skills/<skill-name>/SKILL.md` (+ `shared/`, `evals/`) |

The repo keeps explicit per-tool source directories (`{claude-code,kiro,codex}/skills/<name>/`). Codex uses the official user-scope discovery path `~/.agents/skills/<name>/`; symlink the repo's `codex/skills/<name>/` directory there.

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
  md5_x=$(md5sum "$skill/codex/skills/"*/SKILL.md | awk '{print $1}')
  [ "$md5_a" = "$md5_k" ] && [ "$md5_k" = "$md5_x" ] || echo "DRIFT in $skill"
done
```

Drift between the three copies is the #1 bug to catch in CI.

## Impact of missing pieces

| Missing | Tool-specific impact |
|---|---|
| Any of the three SKILL.md files | Tool will not discover the skill in its documented install path |
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

The third skill host must expose the shell/terminal capabilities required by these skills' build, validation, and AWS deployment phases. Codex supports the open Agent Skills format and can execute the required project commands under its sandbox and approval controls.

1. **All three target tools accept the Agent Skills `SKILL.md` format** — there is no semantic gain from maintaining different entry formats.
2. **Tool-specific entry formats require duplicate authoring effort** and create drift over time.
3. **The open Agent Skills standard is the lowest common denominator** — it covers trigger descriptions, progressive disclosure, and supporting files across Claude Code, Kiro, and Codex.

The current layout keeps one byte-identical `SKILL.md` in three explicit source directories and uses each host's documented installation path.
