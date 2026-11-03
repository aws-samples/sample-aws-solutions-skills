# data-platform-pipeline

A production-grade Agent Skill that builds the **ingestion → storage → catalog → query** layers of a serverless data lake on AWS. Stops once data is queryable in Athena. Output is a working CDK TypeScript project plus Glue scripts and Athena DDL.

**Anthropic Agent Skills format** — a `SKILL.md` plus a `reference/` library, deployed verbatim to all three tools (Claude Code, Kiro, Codex).

## Customer chooses SMUS or standalone

Before any provisioning, the skill asks whether the customer wants **Amazon
SageMaker Unified Studio integration** or a **standalone Glue/S3 Tables/Athena
pipeline**. Neither is preselected. “Use the defaults” does not answer this
question. An existing explicit choice is preserved for the same target.

Both branches record the answer in `.platform-decision.json` and validate it
before bootstrap, uploads, IAM/LF changes, CDK deployments or schedules. The
standalone branch creates no SMUS resources. The SMUS branch can reuse an existing
project or provision an IdC-backed V2 domain and SQL/ETL project, associate the
Glue jobs, add scoped project access, and verify queries/jobs as that project.

The included adapter covers same-account, same-region S3 Tables and Glue code
jobs. Visual ETL graph authoring, Canvas and actual IdC sign-in are separate
validation items. See
[`reference/smus.md`](claude-code/skills/data-platform-pipeline/reference/smus.md).

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

For the default Iceberg path, the curated layer is an S3 Tables bucket and
`mart_*` tables; the Hive/crawler layout above applies when Hive is selected.
With SMUS selected, the generated app also includes the missing SMUS foundation
and scoped grant stacks, plus project integration and verification scripts.
Existing SMUS governance is preserved instead of applying the standalone
IAM-only Lake Formation recipe.

## Layout

```
data-platform-pipeline-skill/
├── README.md                                                          (this file)
├── LICENSE
├── evals/                                                             ← black-box scenario checklists
│   ├── iceberg-erp-scenario.md                                          default path regression guard
│   └── split-region-quick-scenario.md                                   split-region catalog + query
├── claude-code/skills/data-platform-pipeline/SKILL.md                  ★ md5-identical
│                                            └── reference/*.md         ★
├── kiro/skills/data-platform-pipeline/SKILL.md                         ★
│                                      └── reference/*.md               ★
└── codex/skills/data-platform-pipeline/SKILL.md                        ★
                                        └── reference/*.md              ★
```

> Note: The `SKILL.md` carries the core workflow; deeper material (CDK gotchas, Glue scripts, Iceberg/Hive paths, SAP sources, VPC connectivity, split-region catalog + query) lives in `reference/*.md` loaded on demand. The `SKILL.md` + `reference/` set is md5-identical across all three tools.

The installable bundle also includes `scripts/deployment_gate.py`,
`scripts/smus_project.py`, and `assets/smus/*.ts`. Install/copy the **whole skill
directory**, not only SKILL.md. All three bundles are checked byte-for-byte by
the test suite.

### SAP sources

Glue has **native SAP connectors** — SAP OData (source + target, with ODP delta extraction) and SAP HANA — so an SAP source is not routed to generic JDBC or assumed to be a CSV export. The skill asks which SAP access already exists, since OData/HANA prerequisites are Basis-team work that can take weeks; if nothing is ready it builds on a representative export and hands over the prerequisite list. See `reference/sap-sources.md`.

### Validation gates

The skill stops at five named checkpoints. Two are **agent-blocking** (the agent verifies and fixes itself, no user wait): preconditions, and post-run reconciliation. Three are **user-blocking** (must present and wait): platform choice/scope & data residency, data model confirmation, and teardown intent. The SMUS choice is required even when other defaults are accepted. See `SKILL.md` → Validation Gates.

### Split-region catalog + query (opt-in)

For customers who must keep storage in one region while the catalog + query layers run in another — e.g. data residency in `ap-northeast-2` but a consumer service (BI, agentic AI features, a downstream engine) only available elsewhere. Two mechanisms, **presented to the user rather than chosen for them**: query in place via a Glue resource link, or replicate the mart layer. Off by default (follow-up #8). See `reference/cross-region-query.md`.

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
| `account_id` | Explicit target account, verified with STS |
| `use_smus` | Customer explicitly chooses `true` or `false`; no default |
| `project_prefix` | `acme` |
| `aws_region` | `ap-northeast-2` |
| `source_type` | `jdbc` / `s3` / `cdc` |
| `source_details` | DB endpoint + Secrets Manager ARN, OR existing S3 path |
| `business_questions` | "Monthly defect-rate trend, Top 5 defects by vendor" |

Then follow-up questions cover storage pattern, volume, cadence, relationships,
code mappings, partitions, sensitive columns and split-region needs. The customer
can accept those defaults, but the SMUS/standalone choice still needs an explicit
answer.

## Validation

From the repository root:

```bash
python3 -m unittest discover -s data-platform-pipeline-skill/tests -p 'test_*.py' -v
npm ci --prefix data-platform-pipeline-skill/tests --ignore-scripts
npm test --prefix data-platform-pipeline-skill/tests
bash scripts/sync-skills.sh verify
```

Python tests exercise the pre-provisioning gate, mode/account mismatches, scoped
integration, repeat apply, project credentials, reconciliation, failures and
timeouts. Node tests compile the shipped TypeScript assets and inspect synthesized
CloudFormation for both branches. They make no AWS calls.

The black-box scenarios in `evals/smus-choice-scenarios.md` supplement automated
tests; they are not a claim that every coding-agent model has been evaluated.
For account-level acceptance, follow `reference/smus.md` §5 with authorized,
bounded resources. Keep raw account/identity evidence outside public commits.

When editing, update the Claude Code bundle, run the repository SKILL.md sync,
and copy changed `reference/`, `scripts/`, and `assets/` into both other bundles.
The bundle-identity test fails if any installable file differs.

## Sample data

The parent repo's [`sample-data/erp/`](../sample-data/erp/) provides a cosmetics manufacturer ERP fixture for end-to-end testing without a real customer source. Use the S3 path of the generated CSVs as the `source_type=s3` input.

## Pairs with

[`data-platform-consumption-skill`](../data-platform-consumption-skill/) for the BI / chat-agent layer on top. Coupling is by `{prefix}` naming convention — no direct dependency.

## License

MIT — see [LICENSE](LICENSE).
