---
name: agentic-media-archive
description: |
  Build an AWS-native, MCP-exposed agentic media archive: video ingest → MediaConvert
  analysis proxies → Bedrock video embedding + video-language enrichment → OpenSearch
  Serverless vector search with source-time evidence → non-destructive highlight rendering
  → rights-checked downloads, exposed as 16 MCP tools through Amazon Bedrock AgentCore
  Gateway (Cognito OAuth) so ChatGPT/Codex, Claude, Kiro and Amazon Quick can search, cite,
  cut and download footage. Output: CDK (TypeScript) + Python Lambda + local stdio MCP
  bridge + MCP Apps UI + per-client configs, tailored from a requirements conversation.
  Use when the user asks for "video archive with AI search", "media asset management MCP",
  "agentic media archive", "searchable video library", "highlight rendering from an
  archive", "MAM on AWS", "TwelveLabs Marengo/Pegasus on Bedrock", "영상 아카이브",
  "미디어 아카이브 MCP 구축", "영상 검색 인프라", "하이라이트 자동 편집", "방송 영상 자산 관리",
  or describes footage that must be found by natural language from an AI assistant.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---

# Agentic Media Archive Builder

## Purpose
Gather requirements in conversation and generate a governed, agent-ready **media
intelligence backend** on AWS. The customer keeps using the AI clients they already have
(ChatGPT/Codex, Claude, Kiro, Amazon Quick); this skill builds the domain capability
those clients call: a video archive whose contents can be found by natural language,
cited with source-relative evidence, cut into derivative highlights, and downloaded only
when rights allow — exposed as MCP tools behind Amazon Bedrock AgentCore Gateway.

This is **not** a chat UI or a static CDK template. Every Discovery answer becomes a value
in `config/media-archive.yaml`, and infrastructure, handlers, bridge, client configs,
scripts and tests are generated from that file.

## Knowledge sources
All architecture knowledge, code patterns and examples live in `shared/`:
- `shared/reference/architecture.md` — pipeline, component decisions and WHY, request lifecycles, alternatives
- `shared/reference/decision-tree.md` — 13 design decisions and the GATE 2 design table (must-read at Phase 2)
- `shared/reference/aws-services.md` — how to discover video models at runtime, service limits, cost formulas
- `shared/reference/constraints.md` — 23 implementation traps with fixes (must-read before Phase 3)
- `shared/patterns/cdk-stacks.md` — `config/media-archive.yaml`, loader, full `MediaArchiveStack`, variants, scripts
- `shared/patterns/lambda-handlers.md` — `common.py`, dispatch, enrichment workflow, model adapter seams, tests
- `shared/patterns/mcp-tools.md` — 10 catalog + 6 command tool schemas and handlers, Gateway wiring
- `shared/patterns/client-integration.md` — local stdio bridge, MCP Apps UI, Codex/ChatGPT plugin, Kiro, Claude, Quick
- `shared/examples/{broadcaster-sports-highlights,enterprise-training-archive,news-agency-archive}.md`
- `evals/{sports-broadcaster,enterprise-training}-scenario.md` — black-box verification scenarios

## Workflow

### Phase 1: Discovery (conversational requirements gathering)

Ask only what is not already known. Record every answer as a field of
`config/media-archive.yaml` (schema in `shared/patterns/cdk-stacks.md`).

```
1. Footage profile: typical/max duration, formats, monthly ingest hours, languages spoken
2. Region + account: where the archive must live; data-residency constraints
3. Video models — DISCOVER, do not assume (see "Model discovery" below)
4. Tenancy: one organisation (server-side tenant) or several isolated tenants
5. Rights model: none / simple allow-deny per asset / lookup in an external rights system
6. Retention: raw originals, analysis artifacts, derivatives; archive-to-Glacier policy
7. Proxy profile: chunk length, max chunks per asset (→ max source duration), resolution
8. Enrichment budget: understanding-model parallelism, cost cap per month
9. Search posture: OpenSearch Serverless public endpoint (pilot) or VPC endpoint (prod)
10. Auth: Cognito client-credentials (default) or enterprise IdP; split read/write clients?
11. Client surfaces: ChatGPT/Codex plugin, Claude, Kiro, Amazon Quick Web (any subset)
12. Evidence semantics: confidence thresholds; is low-confidence "no match" acceptable?
13. Optional components: collections, ontology suggestions, MCP Apps UI, DLQ alarms
```

**Model discovery (mandatory).** The set of video-capable Bedrock models changes; never
present a fixed list as the only choice. Run `aws bedrock list-foundation-models` in the
target region (or AWS Knowledge MCP) and filter: embedding models with `VIDEO` input and
`EMBEDDING` output; understanding models with `VIDEO` input and `TEXT` output. Present the
filtered list, let the user pick one of each, then record the model IDs **and the embedding
dimension** in config. Exact commands and filters: `shared/reference/aws-services.md` §1.
If no video embedding model is available in the region, apply decision-tree §1 fallbacks.

⛔ **GATE 1**: Summarize requirements as the draft `config/media-archive.yaml` → get approval.

### Phase 2: Architecture Design

Apply `shared/reference/decision-tree.md` section by section and explain each choice:

1. Models + embedding dimension (verified), region, cross-region inference if needed
2. Tenancy → single stack vs per-tenant prefixes / KMS keys / AOSS collections
3. Rights model → download/preview enforcement path in the catalog Lambda
4. Retention → S3 lifecycle rules, archive confirm gate
5. Proxy profile → chunk minutes × max chunks; MediaConvert CBR settings
6. Enrichment parallelism → Step Functions Map concurrency vs model quota
7. AOSS posture → network policy, OCU sizing (formula in `aws-services.md` §4)
8. Auth → Cognito resource server scopes `media-archive/read|write`, client split
9. Client surfaces → which of `clients/*` to generate; Quick Desktop caveat
10. Cost estimate (formulas in `aws-services.md` §6; label as estimate)

⛔ **GATE 2**: Present the design table from decision-tree "Output" plus the architecture
diagram from `architecture.md` → get approval. Do not generate before this.

### Phase 3: Code Generation

Generate in this order, copying from `shared/patterns/*` and adapting to config:

1. `config/media-archive.yaml` (final), `cdk.json`, `package.json`, `tsconfig.json`
2. `infra/lib/config.ts`, `infra/lib/media-archive-stack.ts`, `infra/bin/app.ts` — `cdk-stacks.md`
3. `lambdas/common.py`, `lambdas/dispatch/index.py`, `lambdas/workflow/index.py` — `lambda-handlers.md`
4. `lambdas/catalog/{schema.json,index.py}`, `lambdas/control/{schema.json,index.py}` — `mcp-tools.md`
5. `local_bridge/{server.py,remote_client.py,ui/media-results.html}` — `client-integration.md`
6. `clients/{codex-plugin,kiro,claude,quick}/…` for the selected surfaces — `client-integration.md`
7. `scripts/{check-prerequisites.sh,deploy.sh,destroy.sh,verify.py}` — `cdk-stacks.md` §4–5
8. `evaluation/{golden.json,generate_fixtures.py,run_eval.py,live_test.py}`, `tests/`

Model-specific request/response shapes live only in the adapter functions
(`embed_request/parse_embedding`, `understand_request/parse_understanding`) described in
`lambda-handlers.md`; everything else reads model IDs and dimension from environment.

⛔ **GATE 3**: `npm run build && npx cdk synth --quiet`, `python scripts/verify.py`,
`pytest` all pass; secret/identifier scan is clean → get approval to deploy.

### Phase 4: Validate
- `cdk synth` clean; stack contains exactly two Gateway Lambda targets with 10 + 6 tools
- `scripts/verify.py`: required files present, schemas unique/object-typed, no credentials,
  configured model IDs and dimension propagate to Lambda environment
- `pytest`: serialization, proxy planning, dimension guard, clip validation, archive gate,
  UI `_meta` containment
- Map the run against `evals/<scenario>.md` closest to the customer's case

### Phase 5: Deploy
1. Model access: confirm the chosen models are enabled in the account/region
2. `scripts/check-prerequisites.sh` → `cdk bootstrap` → `scripts/deploy.sh`
3. `deploy.sh` writes every `clients/*` config from stack outputs (no secret ever written)
4. Synthetic eval: `python evaluation/generate_fixtures.py && python evaluation/run_eval.py`
   — three fixtures ingested, three queries return the intended asset with sources
5. Live smoke via a client: search returns evidence with confidence; download of a
   restricted asset is refused; `render_highlight` writes under `derivatives/` and the
   source ETag is unchanged
6. Hand over the operating skill (`clients/*/skills/media-archive-operations`) to end users

## Generation rules

- **CDK**: TypeScript, `aws-cdk-lib` v2 + `@aws-cdk/aws-bedrock-agentcore-alpha` in lockstep
  (exact pins in `cdk-stacks.md`; check npm for newer lockstep pairs before generating)
- **Lambda**: Python 3.13, ARM64, boto3 only (no heavy SDK layers); env-driven config
- **Storage layout**: `raw/{tenant}/{asset}/v1/`, `proxies/`, `analysis/`, `derivatives/`
- **Tool contract**: 16 fixed names — catalog `list_assets get_asset search_assets
  analyze_asset get_job_status get_preview_url get_download_url get_ontology
  list_collections get_collection`; commands `create_upload_session complete_upload
  request_enrichment render_highlight request_archive create_collection`
- **Tool descriptions**: include a "Use for queries like:" line — Gateway semantic search depends on it
- **Evidence**: every clip hit returns `asset_id, start_sec, end_sec, modality, score,
  confidence (high|medium|low), source_id`
- **Clients**: local stdio bridge is the default path for ChatGPT/Codex, Claude, Kiro;
  Quick Web connects directly with service-to-service OAuth
- **Secrets**: OAuth client secret fetched at runtime (Cognito API / Secrets Manager) —
  never in `.mcp.json`, plugin manifests, CFN outputs or exports
- **Language**: domain terms, UI strings and skill text follow the user's language (KO/EN)

## Hard Constraints

Full detail in `shared/reference/constraints.md`. One-line summary:

1. **Originals are immutable** — all edits write to `derivatives/`; never overwrite `raw/` (#11)
2. **Embedding dimension from config, fail closed** — no literal `512`; guard on mismatch (#6)
3. **Never mix embeddings across model versions** — reindex → dual-write → alias cutover (#5)
4. **Understanding-model input bounds** — chunk proxies (≤55 min, <2 GB); `finishReason=length` fails (#8)
5. **Map concurrency ≤ verified model quota** (#9)
6. **MediaConvert proxies**: one clip per job, zero-based timecode, explicit selectors, CBR (#10)
7. **Highlight ranges** sorted, non-overlapping, in range; 30-fps rounding (#11)
8. **At-least-once ingest**: ETag/version identity, conditional writes, tolerate `ExecutionAlreadyExists` (#12)
9. **Low vector scores are not evidence** — confidence bands; report "no confident match" (#13)
10. **Tenant resolved server-side**, never from caller input (#14)
11. **Rights re-checked before every URL**; TTL bounds preview 60–300 s, download 60–900 s (#15, #16)
12. **Archive requires `confirm_archive=true`**; tag only, lifecycle moves the object
13. **KMS key policy**: account-scoped `rule/*` ARN to avoid EventBridge/SQS cycles (#1)
14. **DynamoDB boundary serializer**: float→Decimal, set→sorted list (#2)
15. **AOSS**: SigV4 service `aoss`, payload hash, frozen creds; server-owned `_id` (#3, #4)
16. **Client secret never in repo or CFN export** (#17)
17. **Gateway prefixes tools** `target___tool`; strip only when unambiguous (#18)
18. **State-machine timeout ≠ model poll bound** — both configurable (#20)
19. **MCP Apps UI**: bearer URLs only in `_meta`; keep old resource URIs as aliases (#21)
20. **Quick Desktop cannot refresh client-credentials tokens** — use Quick Web or the bridge (#23)

## When to call MCP

| When | MCP | Call |
|---|---|---|
| Discover video-capable models in region | AWS CLI / AWS Knowledge | `aws bedrock list-foundation-models` filters in `aws-services.md` §1; `aws___search_documentation` |
| Confirm AgentCore Gateway / AOSS / MediaConvert region availability | AWS Knowledge | `aws___get_regional_availability` |
| Verify model input limits (duration, size, modalities) | AWS Knowledge | `aws___read_documentation(url=<model page>)` |
| CDK construct props (agentcore-alpha, opensearchserverless) | AWS Knowledge | `aws___read_documentation(url=<aws-cdk doc>)` |
| Validate generated template | (optional) CloudFormation | validate-template |
