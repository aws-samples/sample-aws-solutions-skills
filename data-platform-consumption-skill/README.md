# data-platform-consumption

A production-grade Agent Skill that connects existing queryable data to **Amazon Quick** — Quick Sight dashboards and chat agents (Dataset Q&A) — for visualization and natural-language analytics.

Works with any queryable source: Athena over a Glue Catalog (most common), Redshift, S3 manifest, or RDS via federated query. Self-contained; doesn't depend on any other skill having run.

**Anthropic Agent Skills format** — single `SKILL.md` deployed verbatim to all three tools (Claude Code, Kiro, Amazon Quick).

## What it produces

- **Quick Sight Enterprise account** initialized in the target region
- **Datasets in SPICE**, one per business domain, with calculated fields and refresh schedules
- **Dashboards** following domain patterns (manufacturing / retail / generic) with KPI cards, time series, comparisons, drill-down tables, and standard filters
- **Amazon Quick chat agent (Dataset Q&A)** with a persona, speculation refusal rules baked verbatim into the topic description, and a test-question matrix
- **Topics (semantic model)** with column → business-term mapping, calculated metrics, and named-entity synonyms (Korean + English)
- **Spaces for multi-tenant access control** — one per team — with per-Space chat persona and Row-Level Security
- **`{prefix}-quicksight-role` IAM role** with minimum permissions

## Layout

```
data-platform-consumption-skill/
├── README.md                                                              (this file)
├── LICENSE
├── claude-code/skills/data-platform-consumption/SKILL.md                   ★ md5-identical
├── kiro/skills/data-platform-consumption/SKILL.md                          ★
└── quick/skills/data-platform-consumption/SKILL.md                         ★
```

> Note: This skill is monolithic (single ~60 KB SKILL.md, no separate `shared/`). All knowledge lives in the SKILL.md itself.

## Installation

### Claude Code

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/claude-code/skills/data-platform-consumption" ~/.claude/skills/data-platform-consumption
```

### Kiro

```bash
mkdir -p ~/.kiro/skills
ln -s "$(pwd)/kiro/skills/data-platform-consumption" ~/.kiro/skills/data-platform-consumption
```

Auto-triggers on the same phrases (e.g. "Quick Sight setup", "build a dashboard", "BI setup", "chat agent").

### Amazon Quick

```bash
mkdir -p ~/.quickwork/skills
ln -s "$(pwd)/quick/skills/data-platform-consumption" ~/.quickwork/skills/data-platform-consumption
```

## Usage

Trigger phrases: *"set up Quick Sight", "build a dashboard", "Amazon Quick chat agent", "natural language analytics", "BI setup", "data visualization", "connect to Quick Sight"*.

The skill will ask for these inputs:

| Input | Example |
|---|---|
| `data_source_type` | `athena` / `redshift` / `s3` / `other` |
| `data_source_details` | Glue DB + workgroup, OR Redshift endpoint, OR S3 manifest |
| `project_prefix` | `acme` (optional — if `{prefix}_db` exists, tables auto-discover) |
| `aws_region` | `ap-northeast-2` (chat features may require a different region) |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by vendor, next-month defect-rate forecast" |
| `target_users` | "5 quality-team members, 2 executives" |

### Region selection

Quick Sight dashboards are broadly available, but **Amazon Quick's agentic AI features (chat agents, Dataset Q&A, Topics) ship in a smaller, changing region footprint.** The skill includes a region availability gate and a two-option selection flow:

- **Option A — dashboards only, in home region** (no chat agent)
- **Option B — full stack in a supported region** (us-east-1 / us-west-2 / eu-west-1; data stays in home region, queried cross-region)

## Pairs with

[`data-platform-pipeline-skill`](../data-platform-pipeline-skill/) for the upstream lake — but only if you don't already have queryable data. This skill is fully standalone otherwise.

## License

MIT — see [LICENSE](LICENSE).
