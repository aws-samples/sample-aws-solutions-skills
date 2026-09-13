# Agentic Media Archive Skill

Generate an AWS-native, **MCP-exposed media archive** tailored to a customer's footage,
rights, retention and client requirements — instead of handing them one frozen CDK template.

The generated system ingests video, builds analysis proxies with MediaConvert, enriches
them with Bedrock video models (video embeddings for retrieval, a video-language model for
descriptions), indexes clip-level vectors in OpenSearch Serverless, and exposes **16 MCP
tools** through Amazon Bedrock AgentCore Gateway. AI clients the customer already uses —
ChatGPT/Codex (plugin + MCP Apps UI), Claude, Kiro, Amazon Quick — search the archive by
natural language, receive **source-time evidence with confidence bands**, render
**non-destructive highlights**, and download only what rights allow.

## Triggers (invoke via natural language)

```
"Build a video archive with AI search on AWS"
"Set up a media asset management MCP for our footage"
"I need a searchable video library that ChatGPT and Kiro can use"
"Render highlights from our match recordings without touching the originals"
"영상 아카이브 MCP 구축해줘"
"방송 영상 자산 관리 인프라를 만들어줘 — 자연어로 검색하고 하이라이트 편집까지"
```

## Directory structure

```
agentic-media-archive-skill/
├── README.md
├── claude-code/skills/agentic-media-archive/SKILL.md   ★ canonical (md5-identical ↓)
├── kiro/skills/agentic-media-archive/SKILL.md          ★
├── codex/skills/agentic-media-archive/SKILL.md         ★
├── shared/
│   ├── reference/
│   │   ├── architecture.md        pipeline, component decisions & WHY, request lifecycles
│   │   ├── decision-tree.md       13 design decisions → GATE 2 design table
│   │   ├── aws-services.md        runtime model discovery, limits, cost formulas
│   │   └── constraints.md         23 implementation traps with fixes
│   ├── patterns/
│   │   ├── cdk-stacks.md          config schema + loader, full MediaArchiveStack, variants, scripts
│   │   ├── lambda-handlers.md     common.py, dispatch, enrichment workflow, model adapter seams
│   │   ├── mcp-tools.md           10 catalog + 6 command tool schemas and handlers
│   │   └── client-integration.md  stdio bridge, MCP Apps UI, Codex/ChatGPT plugin, Kiro, Claude, Quick
│   └── examples/
│       ├── broadcaster-sports-highlights.md
│       ├── enterprise-training-archive.md
│       └── news-agency-archive.md
└── evals/
    ├── sports-broadcaster-scenario.md
    └── enterprise-training-scenario.md
```

## Installation

### Claude Code
```bash
mkdir -p ~/.claude/skills
ln -sf "$(pwd)/claude-code/skills/agentic-media-archive" ~/.claude/skills/agentic-media-archive
ln -sf "$(pwd)/shared" ~/.claude/skills/agentic-media-archive/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
ln -sf "$(pwd)/kiro/skills/agentic-media-archive" ~/.kiro/skills/agentic-media-archive
ln -sf "$(pwd)/shared" ~/.kiro/skills/agentic-media-archive/shared
```

### Codex
```bash
mkdir -p ~/.agents/skills
ln -sf "$(pwd)/codex/skills/agentic-media-archive" ~/.agents/skills/agentic-media-archive
ln -sf "$(pwd)/shared" ~/.agents/skills/agentic-media-archive/shared
```

On Windows or without symlink support, replace `ln -sf` with `cp -r` (see `../CONTRIBUTING.md`).

## Core design principles

1. **Clients stay, capability moves to AWS.** No bespoke chat UI is generated. The archive
   is a domain capability behind MCP; ChatGPT/Codex, Claude, Kiro and Quick are the UX.
2. **Models are discovered, not assumed.** The set of video-capable Bedrock models changes.
   Discovery runs `aws bedrock list-foundation-models` (or AWS Knowledge MCP), filters for
   video embedding / video understanding models, lets the user choose, and records the
   model IDs and the **embedding dimension** in `config/media-archive.yaml`. Nothing
   downstream hardcodes them.
3. **Evidence over assertion.** Every clip hit carries `asset_id, start_sec, end_sec,
   modality, score, confidence, source_id`. Low-confidence hits are reported as
   "no confident match", not as facts.
4. **Originals are immutable.** All edits are MediaConvert derivatives under `derivatives/`;
   rights are re-checked server-side on every preview/download URL; cold archival needs an
   explicit `confirm_archive=true`.
5. **One config, one contract.** Discovery answers → `config/media-archive.yaml` →
   CDK + Lambda + bridge + client configs. The 16-tool MCP contract is fixed so operating
   skills for end users stay valid across customers.

## Generated system

| Layer | Files | Notes |
|---|---|---|
| Config | `config/media-archive.yaml` | region, models, dimension, tenancy, rights, retention, proxy, parallelism, auth, clients |
| Infra (CDK TS) | `infra/lib/media-archive-stack.ts`, `infra/lib/config.ts`, `infra/bin/app.ts` | S3 + KMS + lifecycle, DynamoDB single table, AOSS vector collection, Step Functions, MediaConvert, EventBridge + DLQ + alarms, Cognito resource server, AgentCore Gateway with two Lambda targets |
| Enrichment (Python 3.13 ARM64) | `lambdas/common.py`, `lambdas/dispatch/`, `lambdas/workflow/` | ≤55-min analysis proxies, async embedding, per-segment understanding with source-timeline shift, indexing |
| MCP tools | `lambdas/catalog/{schema.json,index.py}`, `lambdas/control/{schema.json,index.py}` | 10 read tools (`media-catalog`), 6 write tools (`media-commands`) |
| Bridge + UI | `local_bridge/server.py`, `remote_client.py`, `ui/media-results.html` | stdio FastMCP bridge with runtime OAuth, MCP Apps carousel/player, `upload_local_video` |
| Clients | `clients/{codex-plugin,kiro,claude,quick}/` | plugin manifest + allowlist, Kiro/Claude MCP configs, operating skill, Quick Web OAuth guide |
| Ops | `scripts/{check-prerequisites,deploy,destroy}.sh`, `scripts/verify.py` | deploy writes client configs from stack outputs; verify checks files, schemas, secrets |
| Eval | `evaluation/{golden.json,generate_fixtures.py,run_eval.py,live_test.py}`, `tests/` | 3 synthetic fixtures → 3 grounded queries; live smoke incl. rights refusal and ETag check |

## MCP requirements

| MCP | Purpose | Required? |
|---|---|---|
| AWS Knowledge MCP | Model discovery cross-check, regional availability, model limits, CDK construct docs | Recommended |
| CloudFormation MCP | Template validation | Optional |
| AWS CLI (`aws bedrock list-foundation-models`) | Runtime model discovery in the target region | Required during Discovery |

## Cost drivers

Formulas and two worked scenarios (Dev/PoC, Prod) are in `shared/reference/aws-services.md` §6.
The dominant items are Bedrock video-model invocations (proportional to ingested hours),
MediaConvert proxy minutes, and OpenSearch Serverless OCUs; storage and Lambda are minor.
Treat every figure as an estimate to re-price with the AWS Pricing Calculator.

## Reference project

Patterns were extracted from a deployed, live-tested media archive built for a
media-and-entertainment customer engagement (multi-hour sports recordings, Korean/English
queries, ChatGPT plugin + Kiro + Quick clients). The traps in `constraints.md` are the ones
actually hit during that deployment.

## Editing workflow

1. Edit the canonical `claude-code/skills/agentic-media-archive/SKILL.md` or any `shared/` file.
2. `../scripts/sync-skills.sh agentic-media-archive-skill`
3. `../scripts/sync-skills.sh verify`
