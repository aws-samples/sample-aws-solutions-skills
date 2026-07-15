# Shared Knowledge Pattern

This document specifies how each skill MUST organize its knowledge so that the SKILL.md remains thin while the `shared/` directory holds the deep content. The `shared/` directory is bundled with SKILL.md when the skill is installed, so relative paths work both in the repo and after installation.

## Core principle

**Write knowledge once. SKILL.md references it.**

```
                ┌──────────────────────────────────┐
                │   <skill>/shared/                │
                │     - reference/                 │
                │     - patterns/                  │
   SKILL.md ───►│     - examples/                  │
   (3 copies)   │                                  │
                └──────────────────────────────────┘
```

The same `shared/` directory is referenced by all three SKILL.md copies (Claude Code / Kiro / Codex). Since the three SKILL.md files are byte-identical, the references resolve identically in all three install locations as long as `shared/` is bundled alongside them.

## What goes in `shared/`

### `shared/reference/`

Stable knowledge that does not depend on the user's request:

| File | Contents |
|---|---|
| `architecture.md` | Solution architecture diagram + WHY each stack exists |
| `decision-tree.md` | Conditional logic mapping user answers → component choices |
| `aws-services.md` | Service quotas, pricing, model IDs, region availability summary |
| `constraints.md` | Known limits, gotchas, reserved names, region-specific behavior |
| `<solution-specific>.md` | Solution-specific deep dives |

### `shared/patterns/`

Concrete code blocks the agent will adapt and emit:

```
shared/patterns/
├── cdk-stacks.md            Full CDK Construct/Stack source
├── lambda-handlers.md       Full Lambda handler source + pitfalls
├── frontend-pages.md        React + Tailwind + shadcn/ui pages
├── etl-transforms.md        (optional) ETL / PySpark
└── <domain>.md              other solution-specific patterns
```

Each pattern file MUST contain:
- **Working code**, not summaries — agents copy verbatim then adapt
- **WHY comments** — what trap is being avoided, why this design
- **Cross-layer mapping** — one feature flows through CDK + Lambda + Frontend; show all three

### `shared/examples/`

At least 2–3 concrete domain instantiations, ideally as `config/schema.yaml` snippets so the agent can offer them as starting points.

```
shared/examples/
├── travel.md
├── hotel.md
└── retail.md
```

## What goes in SKILL.md (the body)

The SKILL.md body is a **thin wrapper** that:

1. States frontmatter (name, description with trigger keywords, license)
2. Lists workflow phases (Discovery → Design → Generate → Validate → Deploy)
3. **References `shared/*` paths** — does not repeat their content
4. Lists Hard Constraints as 1-line items pointing back to `shared/reference/constraints.md`

SKILL.md MAY contain:
- High-level workflow phase headings
- A list of Discovery questions (since these are runtime UX, not static knowledge)
- Hard Constraints summary (1 line per item, with shared/ deep-link)
- MCP call table (when to invoke which MCP)
- Generation rules (TypeScript / Python / etc. one-liners)

SKILL.md MUST NOT contain:
- Code blocks longer than ~10 lines (those belong in `shared/patterns/`)
- Architecture diagrams (those belong in `shared/reference/architecture.md`)
- Decision matrices (those belong in `shared/reference/decision-tree.md`)
- Service quotas, model IDs, or pricing tables (those belong in `shared/reference/aws-services.md`)
- Full Constraint explanations (those belong in `shared/reference/constraints.md`)

## Reference format

Use forward-slash relative paths from the skill root:

```markdown
- `shared/reference/architecture.md` — architecture decisions
- `shared/patterns/lambda-handlers.md` — handler patterns
- `shared/reference/constraints.md` #25 — Memory actor_id stability
```

Sub-section deep-links use `#N` (the constraint number) or markdown anchors.

## Install bundling

When a user installs a skill, the install script copies BOTH the SKILL.md AND the `shared/` directory:

```bash
# Claude Code
cp -r <skill>/shared "$HOME/.claude/skills/<name>/shared"
cp <skill>/claude-code/skills/<name>/SKILL.md "$HOME/.claude/skills/<name>/SKILL.md"

# Kiro
cp -r <skill>/shared "$HOME/.kiro/skills/<name>/shared"
cp <skill>/kiro/skills/<name>/SKILL.md "$HOME/.kiro/skills/<name>/SKILL.md"

# Codex
cp -r <skill>/shared "$HOME/.agents/skills/<name>/shared"
cp <skill>/codex/skills/<name>/SKILL.md "$HOME/.agents/skills/<name>/SKILL.md"
```

Result on disk:

```
~/.claude/skills/<name>/
├── SKILL.md           ← references "shared/reference/architecture.md"
└── shared/
    ├── reference/
    ├── patterns/
    └── examples/
```

The relative path `shared/reference/architecture.md` from SKILL.md resolves correctly. Same on Kiro and Codex.

For convenience, every skill's README provides ready-to-run install commands.

## Why this matters (real-world drift examples)

When per-tool entry files contain duplicated knowledge (the previous repo design):

| Scenario | What goes wrong |
|---|---|
| New AWS region launches a feature | Need to update three different files; forgetting one means one tool gives wrong region advice |
| New Bedrock model ID released | Three files have the old ID; one tool generates code with stale model |
| Service quota raised by AWS | One file says "max 4 instances," other says "max 10" |
| Critical pitfall discovered | Hard Constraint added to one tool, forgotten in others |

When knowledge is centralized in `shared/` (current design):

| Scenario | What happens |
|---|---|
| Update region table in `shared/reference/aws-services.md` | All three tools immediately consume the new info on next invocation |
| Add a Hard Constraint | Add 1 line to SKILL.md (3 copies — sync script handles) + full detail to `shared/reference/constraints.md` once |

## Audit checklist

Before merging changes to a skill:

```bash
SKILL=<skill-dir>

# 1. shared/ contains the actual knowledge?
find $SKILL/shared -name "*.md" | xargs wc -l | tail -1
# Expect total > 1500 lines for a real solution

# 2. SKILL.md is thin?
wc -l $SKILL/claude-code/skills/*/SKILL.md
# Should be 200-400 lines, < 500

# 3. Three SKILL.md files are md5-identical?
md5sum $SKILL/{claude-code,kiro,codex}/skills/*/SKILL.md

# 4. SKILL.md references valid shared/* paths?
grep -oE "shared/[^ \`]+\.md" $SKILL/claude-code/skills/*/SKILL.md | sort -u | while read f; do
  [ -f "$SKILL/$f" ] || echo "BROKEN REF: $f"
done

# 5. No code blocks > 10 lines in SKILL.md?
awk '/^```/{c=!c; if(c==0 && lines>10) print "Long block at line " NR-lines ": " lines " lines"; lines=0} c{lines++}' \
  $SKILL/claude-code/skills/*/SKILL.md
```

## Migration: from legacy per-tool entry formats

If a legacy skill still uses tool-specific commands, steering files, or custom frontmatter:

1. **Extract** workflow phases, Hard Constraints, Discovery questions, and generation rules into one canonical SKILL.md.
2. **Use** Agent Skills frontmatter (`name`, `description`, `license`, `metadata`).
3. **Encode** trigger keywords in `description` because description matching drives activation.
4. **Place** the new SKILL.md at all three locations:
   - `claude-code/skills/<name>/SKILL.md`
   - `kiro/skills/<name>/SKILL.md`
   - `codex/skills/<name>/SKILL.md`
5. **Verify** byte identity with `scripts/sync-skills.sh verify`.
6. **Delete** obsolete tool-specific entry files after preserving any unique guidance.
7. **Update** the skill README installation commands, using `~/.agents/skills/<name>/` for Codex.

The `template/` directory shows the current layout from scratch.

## When to bend the rule

Acceptable to inline in SKILL.md:

- **Hard Constraints summary list** — 1 line per item is fine since the body needs at-a-glance during workflow execution. Detail still in `shared/`.
- **Workflow phase headings** — phase names (Discovery → Design → Generate → Validate → Deploy) are workflow structure, not knowledge.
- **Discovery question list** — these are runtime UX prompts, not static knowledge.

Never inline:

- Anything that could be wrong (model ID, quota, code snippet, pitfall explanation)
- Anything > 10 lines that could equally well live in `shared/`
