# Korean Sports Broadcaster — Live Highlights

> A production-oriented starting point for bilingual sports search, rights-aware review, and non-destructive reel rendering from four-hour match recordings.

## User Answers (Discovery)

| Discovery question | Answer recorded for this example |
|---|---|
| Primary users | Match producers, highlight editors, and rights-desk staff |
| Source assets | Four-hour live-match recordings, post-match studio shows, and short owned analysis segments |
| Search languages | Korean and English; preserve the original query and translate supporting context when useful |
| Editorial outcome | Find a play, verify its source-relative range, preview it, and build a highlight reel |
| Rights split | Licensed league footage is conditional; studio segments are owned |
| Rights authority | Server-side rights service and catalog record, never AOSS or caller input |
| Original handling | Originals remain under `raw/`; all edits write a derivative under `derivatives/` |
| Region | `<region>` after regional service and model availability checks |
| Tenancy | Single-tenant production deployment for one broadcaster business unit |
| Search networking | AOSS collection `media-archive` behind a VPC endpoint and private network policy |
| Authentication | Cognito OAuth client credentials; secret resolved at runtime, never placed in config |
| Clients | ChatGPT plugin for producers and Kiro for editorial operations |
| Runtime model discovery | `aws bedrock list-foundation-models --region <region>` |
| Embedding filter | `VIDEO` input plus `EMBEDDING` output modalities |
| Understanding filter | `VIDEO` input plus `TEXT` output modalities |
| Chosen embedding model | `<embedding-model-id>` — discovered via `list-foundation-models`; Marengo capability family as of 2026-09, re-verify |
| Chosen understanding model | `<understanding-model-id>` — discovered via `list-foundation-models`; Pegasus capability family as of 2026-09, re-verify |
| Embedding dimension | `512`, confirmed from model card or a probe before index generation |
| Migration policy | New model-specific index → reindex and dual-write → alias cutover; never mix vectors |
| Proxy policy | 720p H.264/AAC source-relative chunks of at most 55 minutes; five chunks maximum |
| Understanding concurrency | Pegasus Map `2`, adjusted only after Bedrock quota verification |
| Highlight format | H.264 QVBR ≤5 Mbps, quality 7, AAC 48 kHz stereo at 96 kbps |
| Gate 1 | Requirements and rights summary approved by the product owner |
| Gate 2 | Single-stack design and private AOSS endpoint approved |
| Gate 3 | `cdk synth`, `python scripts/verify.py`, and pytest pass before deployment |

## Resulting config/media-archive.yaml

```yaml
schema_version: "1.0"
solution:
  name: broadcaster-sports-highlights
  description: bilingual sports search and non-destructive highlight workflow
  region: <region>
  environment: production
stack:
  name: MediaArchiveStack
  composition: single-stack
  tags: {workload: sports-media-archive, owner: broadcast-production}
identity:
  tenancy_mode: single-tenant-pilot
  server_resolved_tenant_id: broadcaster
  # WHY: callers cannot select another tenant in the pilot.
  multi_tenant_interceptor_enabled: false
  cognito: {user_pool_id: <user-pool-id>, oauth_flow: client_credentials, client_secret_source: runtime-secret-resolution}
network:
  aoss: {collection_name: media-archive, endpoint_mode: vpc, vpc_endpoint_id: <aoss-vpc-endpoint-id>, public_access: false}
  lambda: {private_subnets: true, outbound_access: controlled}
storage:
  bucket_name: <media-archive-bucket>
  kms_key_id: <kms-key-id>
  prefixes:
    raw: raw/{tenant}/{asset_id}/v1/
    proxies: proxies/{tenant}/{asset_id}/
    analysis: analysis/{tenant}/{asset_id}/
    derivatives: derivatives/{tenant}/{asset_id}/
  versioning: true
  originals: {overwrite_allowed: false} # WHY: edits must never replace evidence.
  lifecycle: {raw_intelligent_tiering_after_days: 30, noncurrent_to_glacier_after_days: 90, archive_tag: archive=true, archive_tag_to_glacier_after_days: 1, analysis_expire_after_days: 90}
media:
  accepted_asset_types: [live_match_recording, owned_studio_segment]
  max_source_duration_minutes: 240
  analysis_proxy:
    max_chunk_duration_minutes: 55
    max_chunks_per_asset: 5
    video: {width: 1280, height: 720, codec: H_264, rate_control: CBR, bitrate_mbps: 4}
    audio: {codec: AAC, bitrate_kbps: 128}
  highlight_render:
    output_prefix: derivatives/{tenant}/{asset_id}/
    source_time_rounding_fps: 30
    video: {codec: H_264, rate_control: QVBR, max_bitrate_mbps: 5, quality_level: 7}
    audio: {codec: AAC, sample_rate_hz: 48000, channels: 2, bitrate_kbps: 96}
models:
  discovery:
    command: aws bedrock list-foundation-models --region <region>
    embedding_filter: {input_modality: VIDEO, output_modality: EMBEDDING}
    understanding_filter: {input_modality: VIDEO, output_modality: TEXT}
  embedding:
    id: <embedding-model-id> # discovered via list-foundation-models
    family_note: TwelveLabs Marengo capability family as of 2026-09; re-verify
    dimension: 512
    scope: [asset, clip]
    modalities: [visual, audio, transcription]
    video_invocation: async_to_s3
  understanding:
    id: <understanding-model-id> # discovered via list-foundation-models
    family_note: TwelveLabs Pegasus capability family as of 2026-09; re-verify
    output_contract: structured_advisory_metadata
    video_input_constraints_verified_at_runtime: true
  migration: {index_per_embedding_model: true, require_reindex: true, dual_write_before_alias_cutover: true}
index:
  engine: aoss
  collection: media-archive
  alias: media-archive-current
  vector: {field: embedding, dimension_from: models.embedding.dimension, similarity: cosine, index_type: hnsw}
  document_fields: [tenant_id, asset_id, source_id, start_sec, end_sec, modality, score, confidence_band, model_id]
  # WHY: AOSS is a search projection; DynamoDB and the rights service remain authoritative.
workflow:
  ingest_delivery: at-least-once
  identity: {source: object_etag_and_version_id, conditional_catalog_write: true, tolerate_execution_already_exists: true}
  state_machine_timeout_hours: 12
  embedding_poll_bound_hours: 6
  pegasus_map_concurrency: 2
  retry: {transient_attempts: 4, exponential_backoff: true}
  states: [AWAITING_UPLOAD, QUEUED, PROCESSING, PROXYING, EMBEDDING, PEGASUS_MAP, ENRICHED, INDEXED, READY, FAILED]
rights:
  source_of_truth: broadcaster-rights-service
  asset_classes:
    licensed_league_footage: {preview_allowed: conditional, download_allowed: conditional, required_entitlement: league-license}
    owned_studio_segment: {preview_allowed: true, download_allowed: true, required_entitlement: broadcaster-editor}
  recheck_on: [get_preview_url, get_download_url]
  signed_url_ttl_seconds: {upload: 3600, preview_min: 60, preview_max: 300, download_min: 60, download_max: 900}
metadata:
  languages: [ko, en]
  ontology: {model_suggestions_status: MODEL_SUGGESTED, approved_vocabulary_source: editorial-catalog, require_human_approval: true}
  confidence: {high_min_score: 0.80, medium_min_score: 0.60, low_response: no confident match}
  evidence_required: [asset_id, start_sec, end_sec, modality, score, confidence_band, source_id]
collections: {enabled: true, owner_role: producer, use_cases: [match-reel-draft, studio-segment-package]}
archive: {enabled: false, require_confirm_archive: true, behavior: tag-only-lifecycle-performs-move}
clients:
  chatgpt_plugin:
    enabled: true
    plugin_name: media-archive
    gateway_url: <gateway-url>
    allowlisted_tools: [list_assets, get_asset, search_assets, analyze_asset, get_job_status, get_preview_url, get_download_url, get_ontology, list_collections, get_collection, create_upload_session, complete_upload, request_enrichment, render_highlight, request_archive, create_collection]
  kiro:
    enabled: true
    bridge_name: local_bridge
    gateway_url: <gateway-url>
    ui_resource: ui://media-archive/media-results.v1.html
    local_extensions: [upload_local_video, render_media_results, resolve_media_playback]
tools:
  catalog_target: media-catalog
  catalog_tools: [list_assets, get_asset, search_assets, analyze_asset, get_job_status, get_preview_url, get_download_url, get_ontology, list_collections, get_collection]
  control_target: media-commands
  control_tools: [create_upload_session, complete_upload, request_enrichment, render_highlight, request_archive, create_collection]
evaluation:
  synthetic_fixture_count: 3
  require_source_relative_evidence: true
  require_low_confidence_abstention: true
  deployment_checks: [cdk synth, python scripts/verify.py, pytest]
```

## Generated Stack Composition

- **Composition:** one `MediaArchiveStack`; the private AOSS endpoint is a production override, not a separate application.
- **Ingress:** S3/KMS media archive → EventBridge → dispatch Lambda → Step Functions enrichment workflow.
- **Processing:** MediaConvert produces reusable proxies; asynchronous embeddings write to S3; source-relative understanding results merge before indexing.
- **Catalog:** DynamoDB owns assets, jobs, rights, collections, and idempotency; AOSS supplies bilingual vector retrieval.
- **Gateway:** AgentCore Gateway exposes `media-catalog` and `media-commands`; `local_bridge` strips a unique remote prefix.
- **Read side:** all ten catalog tools run in catalog Lambda; preview/download issuance performs a fresh rights check.
- **Write side:** all six command tools run in control Lambda; uploads and renders use stable identities for retries.
- **Clients:** the `media-archive` ChatGPT plugin allowlists all sixteen remote tools; Kiro receives its bridge configuration and UI resource.
- **Generation order:** config → infra → Lambdas → `local_bridge` → clients → scripts → evaluation/tests.
- **Gate mapping:** Gate 1 records bilingual and rights needs, Gate 2 approves the private design, and Gate 3 verifies synth/config/tests.
- **Workflow phases:** Phase 1 Discovery → Gate 1, Phase 2 Design → Gate 2, Phase 3 Generate → Gate 3, Phase 4 Validate, Phase 5 Deploy.

## Demonstration Scenarios

### 1. Bilingual play search with usable evidence

1. **Producer:** “Find the goalkeeper save around the second half in yesterday’s match; answer in Korean.”
2. **Tool:** `search_assets(query="goalkeeper save second half", languages=["ko", "en"], asset_type="live_match_recording")`.
3. **Result:** `{asset_id: "match-042", start_sec: 4482.0, end_sec: 4494.5, modality: "visual+commentary", score: 0.91, confidence_band: "high", source_id: "league-feed-042"}`.
4. **Assistant:** describes the event in Korean and cites the source-relative range rather than inventing a game-clock claim.
5. **Tool:** `get_preview_url(asset_id="match-042", start_sec=4482.0, end_sec=4494.5)`.
6. **Rights evidence:** the server rereads the current league entitlement and returns a 180-second URL only when the producer is entitled.

### 2. Low-confidence player claim becomes an abstention

1. **Producer:** “Show the celebration where an unlisted reserve player waved to the camera.”
2. **Tool:** `search_assets(query="reserve player waving celebration", languages=["ko", "en"])` returns nearest clips at `0.46` and `0.41`, both `low` with evidence retained.
3. **Tool:** `analyze_asset(asset_id="match-042", question="Is the unlisted reserve player waving at the returned clips?")` returns insufficient visual certainty.
4. **Assistant:** “No confident match.” It may show evidence ranges for manual review, but does not claim the player appears or upgrade ontology metadata.

### 3. Producer builds a non-destructive highlight reel

1. **Producer:** “Create a 45-second reel from the save and the owned studio reaction.”
2. **Tools:** `search_assets` returns the high-confidence save; a second `search_assets` returns `{asset_id: "studio-018", start_sec: 92.0, end_sec: 118.0, modality: "speech+visual", score: 0.88, confidence_band: "high", source_id: "studio-cam-a"}`.
3. **Tool:** `render_highlight(clips=[...source-relative ranges...], title="Save and reaction")` validates ordered, non-overlapping ranges and hashes the render specification.
4. **Result:** a render job writes only a new object under `derivatives/broadcaster/`; original source keys remain untouched.
5. **Tools:** `get_job_status(job_id="<render-job-id>")` reports completion, then `get_preview_url(asset_id="<derivative-asset-id>")` issues an entitled review URL.

### 4. Collection sharing and a denied licensed-footage download

1. **Producer:** “Save the approved match moments as tonight’s reel collection.”
2. **Tools:** `create_collection(name="Tonight match reel", asset_refs=[...evidence-bearing clips...])`, then `get_collection(collection_id="<collection-id>")` return ordered references without copying media.
3. **Editor:** “Download the league feed segment for local finishing.”
4. **Tool:** `get_download_url(asset_id="match-042", start_sec=4482.0, end_sec=4494.5)`.
5. **Rights evidence:** a fresh league-rights lookup can deny the request with no signed URL; collection membership never grants download permission.

## Key Learning Points

1. Bilingual retrieval changes search and presentation, not the language-neutral evidence contract.
2. AOSS similarity never grants rights; catalog and rights data are reread before a URL is issued.
3. Low similarity is a reason to abstain, not a weak factual assertion.
4. Render jobs consume source-relative ranges and generate derivatives only, making retries safe.
5. Collections store evidence-bearing references, not copied video or stale URLs.
6. Model-generated entities and topics stay advisory until editorial approval.

## Cost Estimate (formula-based, labelled estimate)

**Estimate only — use current regional AWS, MediaConvert, AOSS, and selected Bedrock prices before Gate 2 approval.**

| Component | Monthly estimate formula |
|---|---|
| Raw storage | `raw_GB_months × current_S3_storage_rate` |
| Analysis proxies | `source_minutes × proxy_output_ratio × storage_rate` |
| MediaConvert analysis | `proxy_output_minutes × current_MediaConvert_rate` |
| Video embeddings | `analyzed_video_minutes × selected_embedding_model_rate` |
| Video understanding | `proxy_chunk_minutes × selected_understanding_model_rate` |
| AOSS | `OCU_hours × current_AOSS_OCU_rate + indexed_GB × storage_rate` |
| Compute | `requests + workflow_transitions + GB_seconds` at current regional rates |
| Highlight rendering | `rendered_minutes × current_MediaConvert_render_rate` |
| Total | Sum fixed baseline and per-match marginal cost separately |

Use `cost_per_live_match = ingestion + model enrichment + expected render minutes` for a representative four-hour event before enabling a full season.

## Pitfalls / Cautions

| Risk | Required control |
|---|---|
| Search result treated as proof | Return evidence and confidence with every clip claim. |
| Licensed clip in a collection | Recheck rights on every preview/download; collection membership grants nothing. |
| Original overwritten during editing | Allow MediaConvert output only beneath `derivatives/`. |
| Input exceeds model limits | Reuse ≤55-minute source-relative proxy chunks, maximum five. |
| Old and new embeddings mixed | Reindex, dual-write, then cut over an alias. |
| Plugin omits collection tools | Keep all sixteen remote tools in its allowlist. |
| AOSS becomes publicly reachable | Require its VPC endpoint and network-policy validation. |
| OAuth material leaks into config | Resolve it at runtime; emit placeholders only. |
| Archive occurs accidentally | Keep it disabled here and require `confirm_archive=true` when enabled. |

## Variant

**Two-channel multi-tenant broadcaster.** Use this only when Channel A and Channel B require independent media, rights, and editorial users. Add a Gateway interceptor that maps the authenticated principal to a server-side tenant, revalidate it in both Lambdas, and allocate a separate `{tenant}` S3 prefix, KMS key, and AOSS collection per channel. Do not add a `tenant_id` tool parameter.

This is more than a configuration switch: it requires per-channel metering, cross-tenant authorization tests, collection-ownership checks, and separate retention and recovery reviews. Tool names and the source-evidence contract remain unchanged.
