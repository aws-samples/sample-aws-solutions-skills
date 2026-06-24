# Multi-Agent Format Spec

> **Core principle**: Write once, install three places. A single `SKILL.md` is placed verbatim into Claude Code, Kiro, and Amazon Quick — they differ only in install location.

## Why a single format

All three target tools natively accept the **[Anthropic Agent Skills](https://agentskills.io/specification)** SKILL.md format:

| Tool | Skills directory | SKILL.md format |
|---|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md` | ✅ Native |
| Kiro | `~/.kiro/skills/<name>/SKILL.md` | ✅ Native (in addition to Steering Rules / Specs) |
| Amazon Quick | `~/.quickwork/skills/<name>/SKILL.md` | ✅ Native |

→ Anthropic's Agent Skills spec is the **lowest common denominator across all three tools**, so we use it as our single canonical format. No per-tool entry-point variation is needed or beneficial.

## SKILL.md format

### Frontmatter

```yaml
---
name: <skill-name>
description: |
  Concise statement of what the skill does and when it should activate. Include
  high-signal trigger keywords (English and Korean if applicable). Max 1024 characters.
  Claude/Kiro/Quick all match user queries against this description.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---
```

Required fields (per agentskills.io):
- `name` — Lowercase + hyphens only, max 64 chars, must equal parent directory name
- `description` — 1–1024 chars, explains what + when

Optional fields:
- `license`, `compatibility`, `metadata`, `allowed-tools`

### Body

Recommended sections (≤ 500 lines per agentskills.io best practice):

```markdown
# <Skill Name>

## Purpose
1-2 paragraph statement.

## Knowledge sources
List of `shared/...` files this SKILL.md references.

## Workflow
### Phase 1: Discovery (conversational requirements)
### Phase 2: Architecture Design
⛔ GATE: Confirm with user before proceeding.
### Phase 3: Code Generation
### Phase 4: Validate
### Phase 5: Deploy

## Generation rules
TypeScript/Python/etc. specifics.

## Hard Constraints
Numbered one-liners — must-not-violate items, with shared/ deep-link.

## When to call MCP
| When | MCP | Call |
```

### Body length and progressive disclosure

The body of SKILL.md should be **thin**: workflow phases, references to `shared/*`, and a Hard Constraints summary. Don't inline code blocks longer than ~10 lines or full architecture diagrams — those belong in `shared/patterns/` or `shared/reference/`.

| Goes in SKILL.md | Goes in `shared/` |
|---|---|
| Trigger / discovery questions | Architecture diagrams |
| Workflow phase headers | Full code patterns (CDK, Lambda, Frontend) |
| Hard Constraints (1-line each) | Detailed Constraint explanations |
| MCP call table | Decision matrices |
| File-generation order | Service catalog (model IDs, regions, pricing) |
| Examples list (link only) | Example bodies |

## Three identical copies — and why

The same SKILL.md is placed verbatim under each tool's `skills/` directory:

```
<skill>/claude-code/skills/<name>/SKILL.md   # md5 = X
<skill>/kiro/skills/<name>/SKILL.md          # md5 = X
<skill>/quick/skills/<name>/SKILL.md         # md5 = X
```

**Why three copies and not a single source-of-truth + symlinks?**

| Approach | Pros | Cons |
|---|---|---|
| Three copies (chosen) | Cross-platform (no symlink issues on Windows / git on some systems), explicit, simple cp/rsync to install | Drift risk — mitigated by CI hash check |
| Symlink to canonical | Drift-impossible | macOS symlinks break in some git workflows; Windows compatibility |
| Single canonical + build script | Drift-impossible | Adds tooling, contributors must run the build |

Three copies + CI md5 check is the same approach datalab-skills uses, and works across all platforms.

### Drift detection

Hard CI rule: three SKILL.md files per skill must be md5-identical.

```bash
SKILL=<skill-dir>
md5_a=$(md5sum "$SKILL/claude-code/skills/"*/SKILL.md | awk '{print $1}')
md5_k=$(md5sum "$SKILL/kiro/skills/"*/SKILL.md | awk '{print $1}')
md5_q=$(md5sum "$SKILL/quick/skills/"*/SKILL.md | awk '{print $1}')
[ "$md5_a" = "$md5_k" ] && [ "$md5_k" = "$md5_q" ] || { echo "DRIFT in $SKILL"; exit 1; }
```

Editing workflow: pick one of the three (recommended: `claude-code/skills/<name>/SKILL.md`), edit, then run `scripts/sync-skills.sh` (provided in template) which copies it to the other two locations.

## Activation model

All three tools use **trigger-driven activation** for Skills:

1. User issues a query.
2. Tool matches query against the skill's `description` field.
3. If matched (or user invokes explicitly with `/<skill-name>`), the skill body is loaded.
4. Once loaded, the body stays in conversation context across turns.
5. After auto-compaction, Claude Code re-attaches the most recent invocation's first 5,000 tokens.

Implication for authoring: **`description` quality determines activation accuracy.** Include trigger keywords explicitly. Korean + English if relevant.

Example of a good description:
```yaml
description: |
  Build a production-ready unified customer profile system on AWS using
  Connect Customer Profiles + Entity Resolution + Bedrock. Use when the user
  asks for "customer 360", "entity resolution",
  "unified profile", or describes scenarios with multi-channel
  customer data needing identity resolution.
```

## Hard Constraints — keep them short

Hard Constraints in the SKILL.md body should be 1-line each. Full explanation lives in `shared/reference/constraints.md`. Example:

```markdown
## Hard Constraints
1. **CP `_profileId` reserved** — never define as a key. See `shared/reference/constraints.md` #7.
2. **Memory `actor_id` stable ID** — frontend MUST send Cognito sub. See `shared/reference/constraints.md` #25.
3. ...
```

This keeps the always-loaded body small while preserving a single source of truth (`shared/reference/constraints.md`) for the detail.

## What the SKILL.md is NOT

Not a Slash Command file (`.claude/commands/<name>.md`):
- Slash commands are a separate, legacy mechanism. Anthropic has merged them into Skills.
- Skills auto-trigger on description match; Slash Commands only on explicit `/cmd`.

Not a CLAUDE.md (project memory):
- CLAUDE.md is project-wide always-on guidance. Different mechanism, different tradeoffs.
- Use CLAUDE.md inside a generated CDK project if the user wants the guidance to follow them around in that project. The skill itself ships a SKILL.md, not a CLAUDE.md.

Not a Kiro Steering Rule:
- Kiro Steering is always-on guidance for a workspace. We use Kiro's Skills mechanism instead, which matches the trigger-driven model of the other two tools.

## Migration notes (historical)

Previous versions of this repo used per-tool entry-point variation:
- `claude-code/CLAUDE.md` + `claude-code/commands/<name>.md`
- `kiro/steering.md` + `kiro/specs/generate-<name>.md`
- `quick/SKILL.md` (with custom frontmatter)

This was abandoned because:

1. All three tools accept Anthropic SKILL.md natively — per-tool variation provided no semantic gain.
2. Three different entry formats meant drift between them was inevitable.
3. The Anthropic Skills standard is actively maintained and supported across the ecosystem; rolling our own was reinventing.

Skills authored in the old format have been migrated. The `template/` directory shows the new format.
