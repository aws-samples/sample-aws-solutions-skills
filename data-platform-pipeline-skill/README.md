# data-platform-pipeline

A production-grade Agent Skill that builds the **ingestion → storage → catalog → query** layers of a serverless data lake on AWS. Stops once data is queryable in Athena. Output is a working CDK TypeScript project plus Glue scripts and Athena DDL.

**Anthropic Agent Skills format** — a `SKILL.md` plus a `reference/` library, deployed verbatim to all three tools (Claude Code, Kiro, Codex).

## What it produces

A CDK TypeScript project with a multi-stack layout:

```
cdk-data-platform/
├── lib/
│   ├── storage-stack.ts      ← S3 buckets (raw / curated / analytics) with lifecycle + encryption
│   ├── catalog-stack.ts      ← Glue Database + Crawlers + Lake Formation IAM-only config
│   ├── pipeline-stack.ts     ← Glue ETL Jobs + native Glue trigger
│   └── query-stack.ts        ← Athena Workgroup + Named Queries
├── glue-scripts/
│   ├── ingest-jdbc.py        ← JDBC → S3 raw (Parquet)
│   └── transform.py          ← Raw → Curated (Parquet+Snappy)
├── athena-views/
│   └── views.sql             ← CREATE OR REPLACE VIEW with code→name enrichment
└── README.md                 ← Naming convention, output contract
```

Plus per-function IAM roles, resource tags on everything, post-run data-quality SQL, and cost guardrails (1 GB Athena scan cap, 2-DPU Glue default, S3 lifecycle).

## Layout

```
data-platform-pipeline-skill/
├── README.md                                                          (this file)
├── LICENSE
├── claude-code/skills/data-platform-pipeline/SKILL.md                  ★ md5-identical
│                                            └── reference/*.md         ★
├── kiro/skills/data-platform-pipeline/SKILL.md                         ★
│                                      └── reference/*.md               ★
└── codex/skills/data-platform-pipeline/SKILL.md                        ★
                                        └── reference/*.md              ★
```

> Note: The `SKILL.md` carries the core workflow; deeper material (CDK gotchas, Glue scripts, Iceberg/Hive paths, VPC connectivity) lives in `reference/*.md` loaded on demand. The `SKILL.md` + `reference/` set is md5-identical across all three tools.

## Installation

### Claude Code

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/claude-code/skills/data-platform-pipeline" ~/.claude/skills/data-platform-pipeline
```

Verify in a Claude Code session: type `/help` — the skill should appear under the user-invocable list. Auto-triggers on phrases like "build a data pipeline", "set up a data lake", "Glue job", "Data Lab build".

### Kiro

```bash
mkdir -p ~/.kiro/skills
ln -s "$(pwd)/kiro/skills/data-platform-pipeline" ~/.kiro/skills/data-platform-pipeline
```

Auto-triggers on the same phrases as the Claude Code skill (e.g. "build a data pipeline", "data lake setup", "Glue job", "ETL pipeline").

### Codex

```bash
mkdir -p ~/.agents/skills
ln -s "$(pwd)/codex/skills/data-platform-pipeline" ~/.agents/skills/data-platform-pipeline
```

## Usage

Trigger phrases: *"build a data pipeline", "set up a data lake", "ingest data from SQL Server", "ETL pipeline", "Glue job", "Data Lab build", "data platform", "serverless analytics"*.

The skill will ask for these inputs:

| Input | Example |
|---|---|
| `project_prefix` | `acme` |
| `aws_region` | `ap-northeast-2` |
| `source_type` | `jdbc` / `s3` / `cdc` |
| `source_details` | DB endpoint + Secrets Manager ARN, OR existing S3 path |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by vendor" |

Then six follow-up questions with **recommended defaults** (data volume, run cadence, table relationships, code-to-name mappings, partitioning, sensitive columns). The user can accept all defaults with "go with your recommendations" and skip the back-and-forth.

## Sample data

The parent repo's [`sample-data/erp/`](../sample-data/erp/) provides a cosmetics manufacturer ERP fixture for end-to-end testing without a real customer source. Use the S3 path of the generated CSVs as the `source_type=s3` input.

## Pairs with

[`data-platform-consumption-skill`](../data-platform-consumption-skill/) for the BI / chat-agent layer on top. Coupling is by `{prefix}` naming convention — no direct dependency.

## License

MIT — see [LICENSE](LICENSE).
