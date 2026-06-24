# Contributing — Adding a new Solution Skill

This document describes how to add a new solution use case to `aws-solution-skills/`.

## Scope guide (right size for one skill)

A single skill = **one identifiable AWS solution pattern**.

| ✅ Right size | ❌ Too narrow | ❌ Too broad |
|---|---|---|
| Unified Customer Profile (CP + ER + Bedrock) | "Create a Lambda function" | "Build AWS infrastructure" |
| Bedrock RAG (KB + OpenSearch + Agent) | "Create an S3 bucket" | "Build an AI system" |
| Data Lake (S3 Tables + Glue + Athena) | "Write an IAM policy" | "Build a data platform" |
| Multi-Agent Orchestrator (Strands + AgentCore) | "Provision a Bedrock model" | "Event processing" |

Criteria:
- The user can invoke it with **one trigger sentence** (or a small set including Korean variants)
- It produces a **CDK + Lambda + Frontend** full stack (or equivalent for the domain)
- It accepts **industry/domain inputs** and customizes the output

## Authoring style — choose one

The repo accepts two styles. Both produce md5-identical SKILL.md across 3 tool directories.

### Style 1 — `shared/` + thin SKILL.md (UCP, Strands)

Best when:
- Solution has substantial reference content (architecture diagrams, decision trees, code patterns, multiple examples)
- Multiple related concerns benefit from being separately addressable (e.g., CDK patterns separate from Lambda patterns separate from frontend patterns)
- You want SKILL.md ≤ 500 lines (Anthropic best-practice) while keeping deep knowledge searchable

Layout:
```
<skill>-skill/
├── README.md
├── claude-code/skills/<name>/SKILL.md     ★ thin wrapper (~200-400 lines)
├── kiro/skills/<name>/SKILL.md            ★ md5-identical
├── quick/skills/<name>/SKILL.md           ★ md5-identical
├── shared/
│   ├── reference/{architecture, decision-tree, aws-services, constraints, ...}.md
│   ├── patterns/{cdk-stacks, lambda-handlers, frontend-pages, ...}.md
│   └── examples/<industry>.md
└── evals/<scenario>.md
```

SKILL.md references `shared/...` paths. Install scripts bundle `shared/` alongside SKILL.md so paths resolve.

### Style 2 — Monolithic SKILL.md (data-platform pair)

Best when:
- Solution is tightly scoped and the SKILL.md is naturally self-contained
- Production guide already exists as a single coherent document
- Reference content does not benefit from separate addressability
- SKILL.md naturally grows to 1000+ lines but stays navigable via internal sections

Layout:
```
<skill>-skill/
├── README.md
├── LICENSE
├── claude-code/skills/<name>/SKILL.md     ★ self-contained (~50-60 KB)
├── kiro/skills/<name>/SKILL.md            ★ md5-identical
└── quick/skills/<name>/SKILL.md           ★ md5-identical
```

No `shared/` directory — knowledge is inline.

### Decision criteria

| Question | Style 1 (`shared/`) | Style 2 (monolithic) |
|---|---|---|
| Total content size | > 100 KB | ≤ 100 KB |
| Multiple distinct knowledge domains within the skill (CDK + Lambda + Frontend + ETL + …) | Yes | No |
| Already authored as a single coherent guide | No | Yes |
| Will the skill be edited frequently in independent sections | Yes | No |
| Need cross-skill content sharing | Possibly via `sample-data/` | Possibly via `sample-data/` |

When in doubt: start with Style 2 (monolithic) and refactor to Style 1 if the SKILL.md exceeds 1500 lines or becomes hard to navigate.

## Procedure

### 1. Create the directory

Copy the [`template/`](./template/) folder verbatim and rename:

```bash
cp -r template <solution-name>-skill
cd <solution-name>-skill
```

The skill folder name MUST be **kebab-case + `-skill` suffix** (e.g., `data-lake-skill`).

The skill `name` (the directory under `<tool>/skills/`) is the same kebab-case **without** the `-skill` suffix.

### 2. Verify the directory structure

Must match [`shared-spec/skill-structure.md`](./shared-spec/skill-structure.md). Three SKILL.md files at:
- `claude-code/skills/<name>/SKILL.md`
- `kiro/skills/<name>/SKILL.md`
- `quick/skills/<name>/SKILL.md`

### 3. Write the SKILL.md (and shared/ if Style 1)

#### Frontmatter

Anthropic Agent Skills standard ([agentskills.io](https://agentskills.io/specification)):

```yaml
---
name: <skill-name>            # Lowercase, hyphens only, max 64 chars, must equal parent dir name
description: |
  Concise statement of what + when. Include trigger keywords (English + Korean if applicable).
  Max 1024 chars. This drives skill activation matching.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---
```

#### Body (Style 1 — thin wrapper)

See [`shared-spec/multi-agent-format.md`](./shared-spec/multi-agent-format.md):
- Knowledge sources (list of `shared/...` paths)
- Workflow phases (Discovery → Design → Generate → Validate → Deploy)
- Hard Constraints (1 line per item, link to `shared/reference/constraints.md`)
- Generation rules (1-line each)
- When to call MCP (table)

#### Body (Style 2 — monolithic)

Self-contained guide with numbered sections (e.g., `1. Prerequisites`, `2. Architecture`, `3. Decision Trees`, …). See `data-platform-pipeline-skill/` for a reference.

### 4. Sync the three copies

Edit only `claude-code/skills/<name>/SKILL.md`, then sync:

```bash
../scripts/sync-skills.sh <skill-name>-skill
```

Verify md5 identity:

```bash
../scripts/sync-skills.sh verify
```

### 5. (Style 1) Write `shared/` content

[`shared-spec/shared-knowledge-pattern.md`](./shared-spec/shared-knowledge-pattern.md) has the rules. Summary:

**`shared/reference/`** — stable knowledge that doesn't depend on user request:
- `architecture.md` — diagrams + WHY each component exists
- `decision-tree.md` — conditional logic mapping user answers → component choices
- `aws-services.md` — service quotas, model IDs, pricing, regional availability
- `constraints.md` — gotchas, reserved names, region-specific behavior

**`shared/patterns/`** — concrete code agents will adapt and emit:
- `cdk-stacks.md` — full CDK source
- `lambda-handlers.md` — full Lambda source + pitfalls
- `frontend-pages.md` — React + Tailwind + shadcn/ui pages
- (domain-specific as needed)

Each pattern file MUST contain:
- Working code (not summaries)
- WHY comments
- Cross-layer mapping (CDK + Lambda + Frontend tied together)

**`shared/examples/`** — at least 2-3 industry instantiations.

### 6. (Recommended) `evals/` scenarios

```
evals/<domain>-scenario.md
```

Format: simulated user input + expected outputs (checklist).

### 7. Update READMEs

- The skill's own `README.md` (trigger phrases, install commands, MCP requirements)
- The catalog table in the root [`README.md`](./README.md)

### 8. Verify

```bash
# md5 identity check across all skills
scripts/sync-skills.sh verify

# Manually install + test in each tool
# (Claude Code, Kiro, Amazon Quick)
```

Run the eval scenario in each tool to confirm the workflow proceeds correctly.

## Modifying an existing skill

For Style 1 skills: edits inside `shared/` propagate to all three tools automatically (SKILL.md just references paths). Edits to SKILL.md require running `scripts/sync-skills.sh` afterward.

For Style 2 skills: edit one of the three SKILL.md files (recommended: `claude-code/skills/<name>/SKILL.md`), then run sync.

## Common mistakes

| Mistake | Impact | Fix |
|---|---|---|
| Three SKILL.md files have different content | Tools see different behaviors — drift bug | Always run `scripts/sync-skills.sh` after editing |
| `name` frontmatter doesn't match parent directory name | agentskills.io validation fails | Make them match |
| `description` lacks trigger keywords | Skill doesn't auto-activate | Include English + Korean keywords explicitly |
| Hardcoded volatile catalog (model IDs, prices) in SKILL.md | Stale within months | Style 1: put in `shared/reference/aws-services.md` and add MCP-verification note |
| Style 1 SKILL.md exceeds 500 lines | Violates Anthropic best practice | Move detail to `shared/`, keep SKILL.md as thin wrapper |
| Workflow does not invoke AWS Knowledge MCP | Agent generates code with stale IDs | Specify `aws___search_documentation` calls in Discovery or Design Phase |
| Including `shared/` for trivial skill | Over-engineering | Use Style 2 (monolithic) instead |

## License headers

Each skill's directory may include its own `LICENSE` (datalab-derived skills include MIT). New skills default to MIT unless otherwise specified.

## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
