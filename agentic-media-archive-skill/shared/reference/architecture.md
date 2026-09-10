# Architecture

> **Design boundary:** this solution is an AWS-native, agent-ready media intelligence layer.
> It exposes trusted archive operations as MCP tools rather than recreating a chat product.
> Every generated deployment must preserve originals, enforce rights server-side, and return
> source-relative evidence for any clip-level claim.

## High-level diagram

```text
                                     control plane
┌──────────────────────────────────────────────────────────────────────────────┐
│ Cognito OAuth 2.0 client credentials                                          │
│   media-archive/read                    media-archive/write                   │
└──────────────────────────────────────────────────────────────────────────────┘
                    │ bearer access token                 │ bearer access token
                    ▼                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Amazon Bedrock AgentCore Gateway                                               │
│ MCP semantic tool discovery; canonical target prefixes remain authoritative   │
└───────────────┬───────────────────────────────────────────┬──────────────────┘
                │ media-catalog                              │ media-commands
                ▼                                            ▼
      ┌─────────────────────┐                      ┌─────────────────────┐
      │ Catalog Lambda      │                      │ Control Lambda      │
      │ 10 read tools       │                      │ 6 write tools       │
      └──────┬───────┬──────┘                      └──────┬───────┬──────┘
             │       │                                     │       │
             │       └─────────────┐                       │       └─── presigned PUT
             │                     │                       │
             ▼                     ▼                       ▼
      ┌───────────────┐    ┌──────────────────┐    ┌─────────────────────┐
      │ DynamoDB      │    │ AOSS vector      │    │ S3/KMS media        │
      │ catalog,      │    │ projection       │    │ archive             │
      │ rights, jobs  │    │ and aliases      │    │ raw/{tenant}/...    │
      └───────────────┘    └──────────────────┘    └──────────┬──────────┘
                                                               │ Object Created
                                                               ▼
                                                        ┌──────────────┐
                                                        │ EventBridge  │
                                                        └──────┬───────┘
                                                               ▼
                                                        ┌──────────────┐
                                                        │ Dispatch     │
                                                        │ Lambda       │
                                                        │ idempotency  │
                                                        └──────┬───────┘
                                                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Step Functions enrichment state machine                                       │
│                                                                              │
│ Prepare → MediaConvert analysis proxies → async embedding model →            │
│ poll → understanding-model Map → merge → AOSS index → READY                  │
│            │ ≤55-min source-relative chunks       │ configurable concurrency │
│            │ proxies/{tenant}/{asset_id}/          │ default: 2               │
└────────────┼───────────────────────────────────────┼─────────────────────────┘
             │                                       │
             ▼                                       ▼
   ┌──────────────────────┐                 ┌──────────────────────────┐
   │ analysis/             │                 │ derivatives/             │
   │ manifests, model      │                 │ non-destructive          │
   │ output, proxy plans   │                 │ highlight renders        │
   └──────────────────────┘                 └──────────────────────────┘

 client strip
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ ChatGPT/Codex plugin │ Claude │ Kiro │ Quick Web                           │
 │                         │                                               │
 │                         ▼                                               │
 │                 local_bridge (stdio MCP)                                │
 │                 - strips unique Gateway target prefixes                 │
 │                 - refreshes OAuth tokens in memory                      │
 │                 - serves MCP Apps resource                              │
 │                   ui://media-archive/media-results.v1.html              │
 │ Quick Desktop: use the local bridge when native OAuth refresh is absent │
 └───────────────────────────────────────────────────────────────────────────┘
```

**Flow reading order:** upload and ingest traffic is isolated from query traffic. The
write target creates durable state and sends large bytes directly to S3; the read target
uses DynamoDB for authoritative catalog and rights decisions, then AOSS only for vector
retrieval. The Step Functions workflow creates all AI-derived state and never mutates a
source object.

## Component decisions and WHY

### S3 layout and lifecycle

**WHY:** media originals, temporary AI output, and deliverable derivatives have different
retention and authorization rules. Mixing them under one prefix makes lifecycle changes
or accidental overwrite paths unsafe.

| Prefix | Contains | Lifecycle and access decision |
|---|---|---|
| `raw/{tenant}/{asset_id}/v1/` | immutable uploaded originals | Transition to Intelligent-Tiering after 30 days; noncurrent versions transition to Glacier after 90 days. |
| `proxies/{tenant}/{asset_id}/` | MediaConvert analysis proxies | Keep only while needed by enrichment and configured review windows; never treat as the source of record. |
| `analysis/{tenant}/{asset_id}/` | manifests, segment metadata, embedding output, proxy plans | Expire after 90 days by default because it is regenerable derived data. |
| `derivatives/{tenant}/{asset_id}/` | highlights and other renders | Retain according to the customer delivery policy; all keys are new objects. |

- Enable bucket versioning, block public access, enforce TLS, and use signed S3 URLs rather
  than proxying video through Lambda or an MCP response.
- Use a server-created `asset_id` and versioned raw key; callers never choose a raw key that
  could overwrite another tenant or asset.
- Make `archive=true` an object tag set only after `request_archive(confirm_archive=true)`.
  Lifecycle, rather than application code, performs the cold-storage transition.
- Keep preview URLs short lived (60–300 seconds), download URLs short lived (60–900 seconds),
  and upload URLs short lived (one hour).
- Validate every tool-provided path against the server-resolved tenant and expected prefix.

### KMS encryption boundary

**WHY:** media may contain sensitive footage, and a storage default without key policy
separation makes tenant expansion, incident response, and audit difficult.

- Use a customer-managed KMS key for S3, DynamoDB, AOSS encryption policy, and the encrypted
  EventBridge DLQ where the deployment supports that combination.
- Grant decrypt/encrypt only to the minimum Lambda, MediaConvert, and delivery roles that need
  it; a catalog read role receives no write authority to originals.
- Enable rotation and retain the key and media by default when the stack is removed. Deletion
  or key disablement is an explicit customer operation, not a generated cleanup default.
- For multi-tenant regulated isolation, select a tenant key or a dedicated account as described
  in `shared/reference/decision-tree.md`; do not assume one key is a sufficient boundary.

### DynamoDB single-table catalog and rights ledger

**WHY:** vector indexes are optimized for similarity, not durable state transitions or
authorization. A query index must never become the authority for an asset's rights.

| Item | Key shape | Authoritative facts |
|---|---|---|
| Asset | `TENANT#{tenant}` / `ASSET#{asset_id}` | raw object version, status, ownership, rights state, retention and analysis summary |
| Job | `TENANT#{tenant}` / `JOB#{job_id}` | workflow stage, provider identifiers, render state, errors and timestamps |
| Ontology concept | `TENANT#{tenant}` / `CONCEPT#{concept_id}` | provenance, review state and source assets; model-generated values begin as suggested |
| Idempotency record | `TENANT#{tenant}` / `IDEMPOTENCY#{fingerprint}` | stable result for an upload, enrich, render or archive request |

- Add a GSI for recent assets, jobs, and concepts instead of scans.
- Use conditional writes for ingestion identity and render specifications so at-least-once events
  do not create duplicated workflows or derivatives.
- Re-read the asset and rights record before generating a preview or download URL, even if a
  vector result or an earlier response said the asset was visible.
- Store the selected model identifiers, embedding dimension, index alias, source version, and
  evidence references with an asset or job so a future reindex is explainable.

### AOSS vector index as a projection

**WHY:** semantic retrieval needs approximate nearest-neighbor search, but it cannot enforce
rights or recover an interrupted workflow reliably by itself.

- Create an AOSS vector-search collection and index from the chosen embedding dimension in
  `config/media-archive.yaml`; never leave a hidden dimension literal in Lambda code.
- Store `tenant_id`, `asset_id`, `model_id`, `embedding_scope`, `embedding_option`,
  `start_sec`, `end_sec`, `source_id`, `summary`, `topics`, and the vector in each document.
- Filter tenant and model/index alias before k-NN search. Hydrate candidate assets from DynamoDB
  before exposing preview, download, or rights-sensitive metadata.
- Treat AOSS as rebuildable. DynamoDB plus `analysis/` is enough to restore the projection after
  an index migration or disaster recovery event.
- Never mix vectors from different embedding models or dimensions in one live index. Reindex,
  dual-write while validation runs, then cut an alias to the new index.

### Step Functions workflow structure and Map concurrency

**WHY:** proxy creation, asynchronous embedding, and per-segment understanding each have
different retry and timeout behavior. One Lambda invocation cannot safely own that lifecycle.

```text
Prepare
  → Start analysis proxies → poll proxies
  → Start async embedding → poll embedding
  → Prepare source-relative segments
  → Map: understand one proxy segment
  → Merge analysis and ontology suggestions
  → Index embeddings
  → Finish asset and job as READY
```

- Dispatch computes an identity from S3 ETag/version and the server-resolved asset. It starts a
  deterministic execution name, tolerates `ExecutionAlreadyExists`, and relies on conditional
  writes to make ingest at-least-once safe.
- Configure the state-machine timeout with a default of 12 hours and configure the per-model
  poll bound with a default of 6 hours. Do not make either bound a hidden constant.
- Use exponential retry only for retryable service and Lambda errors. Mark both asset and job
  terminal state on a non-retryable failure.
- Use a Map state for understanding-model work. Default `maxConcurrency` is 2; the generated
  configuration, not source code, owns the selected value.
- Persist deterministic proxy plans and per-segment analysis keys so a retry reuses completed
  work instead of issuing another paid model call.

### MediaConvert analysis proxies and 55-minute chunks

**WHY:** the understanding model has per-input duration and size limits. A predictable,
source-relative proxy avoids sending oversized or variable-bitrate originals to a constrained
model and preserves timeline grounding.

- Generate 1280×720 H.264 CBR 4 Mbps video with AAC 48 kHz stereo, CBR 128 kbps audio.
- Split sources into contiguous source-relative chunks of at most 55 minutes. The five-minute
  margin protects a one-hour understanding-input limit and leaves tolerance for container
  metadata or duration rounding.
- Default to at most five chunks per asset, which supports about four hours and 35 minutes of
  source runtime. Make both chunk duration and count configurable at GATE 2.
- Validate each completed proxy has exactly one expected MP4 object, remains below the selected
  model's size limit, and stays under its planned `proxies/` prefix.
- Shift any segment-local timestamps to source time during merge; client evidence must never be
  expressed only as a proxy-local offset.

### Embedding model versus understanding model

**WHY:** similarity retrieval and descriptive reasoning are different operations. Forcing one
model to do both loses either scalable vector search or structured, reviewable metadata.

| Role | Invocation and output | What it is allowed to decide |
|---|---|---|
| Video embedding model | asynchronous video invocation to S3; query-text embedding synchronously; clip and asset vectors | similarity candidates and source-time retrieval evidence |
| Video understanding model | constrained JSON/text per analysis proxy through the Map state | advisory summary, topics, entities, editorial tags and safety notes |

- Discover both models at runtime rather than writing a permanent model list. The embedding
  candidate must advertise `VIDEO` input and `EMBEDDING` output; the understanding candidate
  must advertise `VIDEO` input and `TEXT` output.
- Record the chosen embedding dimension from a model card or probe response. Validate every
  extracted vector against that configured value before index insertion.
- Treat people, brands, places, objects, rights, and ontology labels from the understanding
  model as advisory. Suggested ontology concepts require a human or external policy approval
  before becoming controlled vocabulary.
- `analyze_asset` may answer a whole-asset question but must not claim frame-accurate timing
  unless it returns independently grounded source-time evidence.

### AgentCore Gateway and Cognito client credentials

**WHY:** clients need one standards-based MCP endpoint while backend functions need least-
privilege separation between search and mutating archive operations.

- Create one AgentCore Gateway with semantic search because it exposes 16 backend tools.
- Register `media-catalog` as the read Lambda target and `media-commands` as the write Lambda
  target. Gateway-visible names may be prefixed, for example
  `media-catalog___search_assets`; the prefix remains canonical remotely.
- Define `media-archive/read` and `media-archive/write` resource-server scopes. Default to
  separate Cognito client-credentials clients for read-only and write-capable integrations.
- Permit a combined client only when an explicitly documented server-to-server integration needs
  both scopes and its risk owner accepts that expanded authority.
- Do not put a OAuth client secret in source, generated client configuration, plugin metadata, or
  an MCP transcript. The bridge retrieves it from Secrets Manager or Cognito APIs at runtime.
- Lambda validates tool input, tenant context, and rights independently; Gateway authorization
  reduces exposure but does not replace data-plane authorization.

### Local stdio bridge and MCP Apps UI

**WHY:** desktop tools have different remote-MCP and token-refresh capabilities. A small local
bridge gives them a secure common transport without duplicating archive business logic.

- `local_bridge/server.py` presents stdio MCP and calls `<gateway-url>` using fresh bearer tokens
  cached only in process memory.
- It removes a Gateway target prefix only when the remaining name is unique; otherwise it keeps
  the complete remote name to avoid tool-routing ambiguity.
- Add local-only helpers `upload_local_video`, `render_media_results`, and
  `resolve_media_playback`. They orchestrate direct upload or display; they do not bypass
  server-side catalog, rights, or derivative controls.
- Serve `ui://media-archive/media-results.v1.html` as an MCP Apps resource for cited result
  cards, a timeline, and a media player. The UI receives short-lived playback URLs only after a
  server-side rights check.
- Restrict local upload paths to a configured approved root and stream large media to presigned
  S3 URLs instead of encoding video into MCP content blocks.

### No bespoke frontend

**WHY:** the value is cross-assistant archive capability, not a fifth chat shell with another
identity model, session store, and deployment surface.

- ChatGPT/Codex plugin, Claude, Kiro, and Quick Web are supported client surfaces; all use the
  same remote tools or the local bridge.
- Use the small MCP Apps resource only where a visual result card or video timeline materially
  improves media review. It is not a general-purpose web application.
- This avoids duplicating conversation orchestration, user management, OAuth refresh, and tool
  semantics that the host clients already provide.
- Add a custom frontend only when a separately funded workflow needs roles, approval queues, or
  editorial collaboration that cannot be represented through MCP and MCP Apps.

### EventBridge dispatch, DLQ, and observability

**WHY:** object-created notifications are at least once and external model calls can fail after
media has been stored. The system needs durable evidence of a dispatch failure.

- Route matching `raw/` object-created events through EventBridge to the dispatch Lambda.
- Attach a KMS-encrypted SQS DLQ and alarms for failed delivery and visible backlog.
- Enable Step Functions execution tracing and error-level logs without persisting media bytes or
  credential values in logs.
- Make metrics and alarms optional in a low-cost pilot only when the design table records the
  operational risk and a manual inspection path.

## MCP tool surface

The generated schemas and client allowlists MUST use these 16 backend tool names exactly.
Do not collapse, rename, or silently omit tools when generating a client integration.

| Target | Tool names |
|---|---|
| `media-catalog` — 10 read tools | `list_assets`, `get_asset`, `search_assets`, `analyze_asset`, `get_job_status`, `get_preview_url`, `get_download_url`, `get_ontology`, `list_collections`, `get_collection` |
| `media-commands` — 6 write tools | `create_upload_session`, `complete_upload`, `request_enrichment`, `render_highlight`, `request_archive`, `create_collection` |

The local bridge can additionally expose `upload_local_video`, `render_media_results`, and
`resolve_media_playback`. Those are local integration helpers, not extra Gateway tools, and do
not change the 16-tool backend contract.

## Request lifecycle

### `search_assets`

1. The client calls `search_assets` through the local bridge or remote Gateway with the read
   scope; a Gateway-prefixed remote name is valid.
2. Gateway authorizes the client and routes the call to the `media-catalog` Lambda target.
3. Catalog Lambda resolves the tenant server-side. It rejects caller-supplied tenant selection in
   the single-tenant deployment and revalidates context in a multi-tenant deployment.
4. The Lambda embeds the query using the configured active embedding model and searches only the
   active index alias, tenant filter, and compatible model/dimension space.
5. It collects candidate vectors with `asset_id`, source-relative `start_sec` and `end_sec`, and
   a stable `source_id` or index document identifier.
6. It hydrates candidate asset records from DynamoDB, applies catalog visibility and rights
   policy, and removes stale, failed, deleted, or inaccessible results.
7. It converts scores through the configured, evaluated confidence thresholds: high, medium, or
   low. A response with no qualifying hit states **no confident match**.
8. It returns a compact evidence object for every clip claim: `asset_id`, `start_sec`, `end_sec`,
   `modality`, `score`, `confidence_band`, and `source_id`.
9. If the client requests a preview, it separately calls `get_preview_url`; that tool rechecks
   current rights before minting a short-lived URL.
10. `render_media_results` may turn the cited results into the MCP Apps view, but the structured
    evidence remains the source of truth for any textual summary.

### `render_highlight`

1. The client submits asset ID and an edit decision list through the write-scoped
   `render_highlight` tool.
2. Control Lambda resolves the server-side tenant, loads the authoritative asset, and rechecks
   rights and asset state before authorizing any render.
3. It validates that every requested range is source-relative, ordered, non-overlapping, bounded
   by asset duration, and suitable for 30-fps timecode rounding.
4. It creates a stable render specification hash. A duplicate request returns the existing job
   rather than allocating another MediaConvert job.
5. The Lambda sends MediaConvert a H.264 QVBR job with maximum 5 Mbps, quality 7, and AAC
   48 kHz stereo at 96 kbps.
6. MediaConvert reads the original under its allowed role and writes a new object under
   `derivatives/{tenant}/{asset_id}/`; it never replaces `raw/`.
7. Control Lambda records the render job and output key in DynamoDB for status polling and audit.
8. When the render is complete, `get_job_status` exposes state; `get_preview_url` or
   `get_download_url` performs a new rights check before it signs the derivative URL.
9. The response distinguishes a pending render from a deliverable file. It never asserts that an
   output is available merely because a render request was accepted.

## Stack composition

### Single-stack default

Use one `MediaArchiveStack` for the first deployment. It owns the KMS key, S3 bucket, DynamoDB
table, AOSS collection and policies, EventBridge rule and DLQ, MediaConvert queue and role, Step
Functions state machine, Lambdas, Cognito resources, and AgentCore Gateway targets.

| Why the default is one stack | Consequence |
|---|---|
| The archive is one security boundary in a pilot. | IAM and KMS policies are easier to audit as one unit. |
| The Gateway needs Lambda target references. | There is no brittle hand-maintained endpoint configuration. |
| The workflow needs table, bucket, KMS, and AOSS references. | Deploy order and rollback are simpler. |
| The generated customer config describes one region and tenant model. | GATE 2 remains understandable and reviewable. |

### When to split

Split only when a named operational boundary outweighs cross-stack dependency cost.

| Trigger | Recommended split |
|---|---|
| Separate platform and media-data teams | Foundation data plane (KMS/S3/DynamoDB/AOSS) and application plane (workflow/Lambdas/Gateway). |
| Independent regional archive deployments | One complete stack per region, with a documented replication and index strategy. |
| Regulated tenants require account isolation | One account/stack/key/collection boundary per regulated tenant. |
| Large enrichment traffic needs independent rollout cadence | Workflow and worker plane separated from the MCP edge, with explicit versioned contracts. |

Keep outputs opaque, pass typed CDK references where possible, and document the deletion and
retention ownership for every split. Do not split merely to make a diagram look more enterprise.

## Directory mapping

```text
<project>/
├── config/media-archive.yaml
│   └── discovery answers: region, models, dimensions, retention, proxy,
│       tenancy, auth, client surfaces, thresholds, and cost controls
├── cdk.json  package.json  tsconfig.json
├── infra/
│   ├── bin/app.ts
│   └── lib/{config.ts, media-archive-stack.ts}
├── lambdas/
│   ├── common.py
│   ├── dispatch/index.py
│   ├── workflow/index.py
│   ├── catalog/index.py
│   ├── catalog/schema.json
│   ├── control/index.py
│   └── control/schema.json
├── local_bridge/
│   ├── server.py
│   ├── remote_client.py
│   └── ui/media-results.html
├── clients/
│   ├── codex-plugin/
│   │   ├── .codex-plugin/plugin.json
│   │   ├── .mcp.json
│   │   └── skills/media-archive/SKILL.md
│   ├── kiro/
│   │   ├── .kiro/settings/mcp.json
│   │   └── .kiro/skills/media-archive-operations/SKILL.md
│   ├── claude/
│   │   ├── .mcp.json
│   │   └── skills/media-archive-operations/SKILL.md
│   └── quick/README.md
├── scripts/
│   ├── check-prerequisites.sh
│   ├── deploy.sh
│   ├── destroy.sh
│   └── verify.py
├── evaluation/
│   ├── golden.json
│   ├── generate_fixtures.py
│   ├── run_eval.py
│   └── live_test.py
└── tests/
```

The generator writes `config/media-archive.yaml` first. Infrastructure, handlers, bridge,
clients, scripts, evaluation fixtures, and tests must read its selected values rather than
forking their own model, region, dimension, or tenancy defaults.

## Why this composition over alternatives

| Alternative | Why this composition is preferred |
|---|---|
| Rekognition-only archive search | It does not provide the selected video embedding model's clip-level semantic retrieval contract or a separate, constrained video-language understanding stage. |
| Transcribe plus text RAG | It misses visual and audio-only evidence, cannot ground a scene without speech, and does not produce multimodal clip vectors. |
| A bespoke chat UI | It duplicates host-client conversation, identity, OAuth, and tool experience while narrowing access to one surface. MCP lets existing assistants use the same archive. |
| AgentCore Runtime | The solution exposes deterministic archive tools and workflow state, not a server-side reasoning agent. Gateway plus Lambda targets is smaller, easier to authorize, and avoids a second agent loop. |
| AOSS as the catalog | Similarity indexes cannot safely be the rights ledger, job state machine, or source-of-truth retention record. DynamoDB remains authoritative. |
| Direct original-to-understanding calls | Large or long originals may exceed model constraints and lose deterministic source-time segmentation. MediaConvert proxies enforce a reusable contract. |

## Volatile catalog

Models, service availability, quotas, supported regions, and AgentCore feature maturity are
volatile. The generated solution MUST verify them at discovery and immediately before deployment.

1. Run `aws bedrock list-foundation-models --region <region>` or use AWS Knowledge MCP.
2. Filter for video embedding candidates with `VIDEO` input and `EMBEDDING` output, then for
   video-understanding candidates with `VIDEO` input and `TEXT` output.
3. Verify AgentCore Gateway, Cognito, Step Functions, MediaConvert, AOSS, and required async
   model invocation are available in the selected region.
4. Read the selected embedding dimension from model metadata or a probe invocation, place it in
   `config/media-archive.yaml`, and generate the index mapping and extraction guard from it.
5. Record model, region, cross-region inference decision, verification date, and deployment
   evidence in the GATE 2 design table.
6. During a model migration, create a new index, reindex and dual-write, evaluate evidence and
   confidence thresholds, then cut the alias. Never silently combine incompatible embeddings.
