# News Agency Archive — Fast Rights-Governed Clip Discovery

> A high-ingest archive for 30-second to five-minute news clips, designed for rapid evidence-backed search while keeping rights decisions external and librarian review in control of the ontology.

## User Answers (Discovery)

| Discovery question | Answer recorded for this example |
|---|---|
| Primary users | News desk producers, researchers, video editors, and archive librarians |
| Asset volume | High, continuous ingest of short clips from 30 seconds to five minutes |
| Content types | Field footage, wire clips, press events, owned packages, and agency b-roll |
| Turnaround goal | Make completed enrichment searchable within a measured `<searchable-slo-minutes>` target after load testing |
| Search behavior | Natural-language topical, visual, speech, and named-event queries with evidence-bearing results |
| Rights authority | External rights system lookup is required before any preview or download URL; AOSS rights fields are hints only |
| Ontology policy | Model suggestions are reviewed and approved by librarians in the authoritative archive vocabulary |
| Archive policy | A raw asset becomes eligible at 30 days, but Glacier movement starts only after a human confirms `request_archive` |
| Original handling | Original raw media is versioned and never overwritten; clips and reels are derivatives |
| Tenancy | Single agency tenant with tenant identity resolved at the Gateway/Lambda boundary |
| Region | `<region>` after data residency, service availability, and disaster-recovery requirements are reviewed |
| Network boundary | AOSS uses a VPC endpoint; rights-service egress is constrained to approved connectivity |
| Authentication | Cognito OAuth client credentials; no client secret is committed to generated project files |
| Clients | Codex for reporters/researchers and Kiro for archive operations |
| Runtime model discovery | `aws bedrock list-foundation-models --region <region>` |
| Embedding filter | `VIDEO` input and `EMBEDDING` output modalities |
| Understanding filter | `VIDEO` input and `TEXT` output modalities |
| Chosen embedding model | `<embedding-model-id>` — discovered via `list-foundation-models`; Marengo capability family as of 2026-09, re-verify |
| Chosen understanding model | `<understanding-model-id>` — discovered via `list-foundation-models`; Pegasus capability family as of 2026-09, re-verify |
| Embedding dimension | `512`, read from selected-model documentation or probe output before index mapping is generated |
| Model migration | New per-model index, reindex, dual-write, relevance validation, then alias cutover |
| Proxy policy | One 720p H.264/AAC source-relative proxy chunk per short asset |
| Throughput control | Pegasus Map concurrency `4` only after quota confirmation; dispatch uses at-least-once identity and retries |
| Confidence policy | High/medium results include evidence; low-only results are reported as “no confident match” |
| Gate 1 | Ingest, rights, librarian-review, and archive-confirmation requirements approved |
| Gate 2 | Single-region architecture and throughput/cost plan approved |
| Gate 3 | `cdk synth`, `python scripts/verify.py`, and pytest pass before deployment |

## Resulting config/media-archive.yaml

```yaml
schema_version: "1.0"
solution:
  name: news-agency-archive
  description: fast short-clip discovery with external-rights enforcement and librarian ontology review
  region: <region>
  environment: production
stack:
  name: MediaArchiveStack
  composition: single-stack
  tags: {workload: news-media-archive, owner: newsroom-archive}
identity:
  tenancy_mode: single-tenant-pilot
  server_resolved_tenant_id: news-agency
  multi_tenant_interceptor_enabled: false
  cognito: {user_pool_id: <user-pool-id>, oauth_flow: client_credentials, client_secret_source: runtime-secret-resolution}
network:
  aoss: {collection_name: media-archive, endpoint_mode: vpc, vpc_endpoint_id: <aoss-vpc-endpoint-id>, public_access: false}
  rights_service: {egress_mode: approved-private-connectivity, endpoint: <rights-service-endpoint>}
storage:
  bucket_name: <media-archive-bucket>
  kms_key_id: <kms-key-id>
  prefixes:
    raw: "raw/{tenant}/{asset_id}/v1/"
    proxies: "proxies/{tenant}/{asset_id}/"
    analysis: "analysis/{tenant}/{asset_id}/"
    derivatives: "derivatives/{tenant}/{asset_id}/"
  versioning: true
  originals: {overwrite_allowed: false}
  lifecycle:
    raw_intelligent_tiering_after_days: 30
    noncurrent_to_glacier_after_days: 90
    analysis_expire_after_days: 90
    archive_tag: archive=true
    archive_tag_to_glacier_after_days: 1
media:
  accepted_asset_types: [field_footage, wire_clip, press_event, owned_package, agency_broll]
  min_source_duration_seconds: 30
  max_source_duration_minutes: 5
  analysis_proxy:
    max_chunk_duration_minutes: 55
    max_chunks_per_asset: 1
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
  document_fields: [tenant_id, asset_id, source_id, start_sec, end_sec, modality, score, confidence_band, model_id, rights_state]
  # WHY: a result may carry stale rights_state; the external system decides URL access.
workflow:
  ingest_delivery: at-least-once
  identity: {source: object_etag_and_version_id, conditional_catalog_write: true, tolerate_execution_already_exists: true}
  state_machine_timeout_hours: 12
  embedding_poll_bound_hours: 6
  pegasus_map_concurrency: 4
  quota_verified_before_deploy: true
  retry: {transient_attempts: 4, exponential_backoff: true}
  states: [AWAITING_UPLOAD, QUEUED, PROCESSING, PROXYING, EMBEDDING, PEGASUS_MAP, ENRICHED, INDEXED, READY, FAILED]
rights:
  source_of_truth: external-rights-system
  lookup: {required_for_preview: true, required_for_download: true, cache_policy: bounded-and-never-authoritative}
  recheck_on: [get_preview_url, get_download_url]
  signed_url_ttl_seconds: {upload: 3600, preview_min: 60, preview_max: 300, download_min: 60, download_max: 900}
metadata:
  languages: [en, ar, fr, de, ja, ko, es]
  ontology: {model_suggestions_status: MODEL_SUGGESTED, approved_vocabulary_source: librarian-reviewed-archive-taxonomy, require_human_approval: true}
  confidence: {high_min_score: 0.82, medium_min_score: 0.62, low_response: no confident match}
  evidence_required: [asset_id, start_sec, end_sec, modality, score, confidence_band, source_id]
collections: {enabled: true, owner_role: news-producer, use_cases: [breaking-news-package, research-board]}
archive:
  enabled: true
  eligible_after_asset_age_days: 30
  require_confirm_archive: true
  behavior: tag-only-lifecycle-performs-move
clients:
  codex:
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

- **Composition:** one `MediaArchiveStack` for the initial single-region agency deployment; do not claim global indexing before residency and rights reviews.
- **Ingress:** S3/KMS → EventBridge → dispatch Lambda → Step Functions; ETag/version identity and conditional writes make high-volume delivery safe to retry.
- **Processing:** each short clip produces one reusable proxy, asynchronous embeddings, source-relative understanding metadata, and an AOSS projection.
- **Flow control:** concurrency is configurable, queue-backed, and validated against model quotas before `4` is used in production.
- **Catalog:** DynamoDB owns assets, jobs, idempotency, collections, and archive state; AOSS never authorizes a rights-sensitive action.
- **Rights boundary:** catalog Lambda calls the external rights system before issuing both preview and download URLs.
- **Ontology:** model outputs enter as `MODEL_SUGGESTED`; librarians approve or reject concepts in the authoritative taxonomy workflow.
- **Clients:** Codex gets a `media-archive` plugin; Kiro gets `local_bridge` and the versioned media-results UI resource.
- **Archive behavior:** `request_archive` applies a confirmed tag only; S3 lifecycle performs a later Glacier transition.
- **Generation order:** config → infra → Lambdas → `local_bridge` → clients → scripts → evaluation/tests.
- **Gate mapping:** Gate 1 confirms rights/archive rules, Gate 2 approves the throughput design, Gate 3 validates synth/config/tests.
- **Workflow phases:** Phase 1 Discovery → Gate 1, Phase 2 Design → Gate 2, Phase 3 Generate → Gate 3, Phase 4 Validate, Phase 5 Deploy.

## Demonstration Scenarios

### 1. Fast breaking-news search keeps evidence attached

1. **Reporter:** “Find the aerial footage of the bridge closure from this morning.”
2. **Tool:** `search_assets(query="aerial bridge closure", time_window="today")`.
3. **Result:** `{asset_id: "clip-709", start_sec: 18.2, end_sec: 46.9, modality: "visual+speech", score: 0.93, confidence_band: "high", source_id: "field-unit-17"}`.
4. **Assistant:** reports the high-confidence match and gives the source-relative interval; it does not claim location details absent from the source evidence.
5. **Tool:** `get_preview_url(asset_id="clip-709", start_sec=18.2, end_sec=46.9)` performs the external rights lookup and returns a 120-second preview only if clearance is current.

### 2. Unconfirmed event query becomes “no confident match”

1. **Reporter:** “Show the video proving a second explosion at the site.”
2. **Tool:** `search_assets(query="second explosion site")` returns low-score clips at `0.49` and `0.43`, with their source IDs and ranges retained for review.
3. **Tool:** `analyze_asset(asset_id="clip-709", question="Does this source establish a second explosion?")` returns insufficient evidence.
4. **Assistant:** “No confident match.” It may disclose the low-confidence clips for editorial verification but never phrases them as proof of a developing event.

### 3. Librarian review and an external-rights denial

1. **Librarian:** “Collect footage for the coastal-storm research board and show suggested entities.”
2. **Tools:** `search_assets(query="coastal storm")` returns evidence-bearing candidate clips, then `get_ontology()` returns related `MODEL_SUGGESTED` entities.
3. **Assistant:** labels each concept as an advisory candidate; the librarian accepts or rejects it outside the MCP tool set in the authoritative taxonomy process.
4. **Tool:** `create_collection(name="Coastal storm research", asset_refs=[...evidence-bearing clips...])` records references without copying or licensing media.
5. **Editor:** “Download the wire clip for a partner.”
6. **Tool:** `get_download_url(asset_id="wire-204", start_sec=0, end_sec=53.0)` calls the external rights system; a restriction result returns no signed URL even if the search index said `rights_state: usable`.

### 4. Archive-to-Glacier has a confirmation gate

1. **Archive operator:** “Move thirty-day-old raw clips to cold storage.”
2. **Tool:** `list_assets(age_days_gte=30, state="READY")`, then `get_asset(asset_id="clip-709")` confirm age, legal status, and version.
3. **Tool:** `request_archive(asset_id="clip-709", confirm_archive=false)` responds that confirmation is mandatory and leaves the object unchanged.
4. **Operator:** “I confirm archive for this eligible asset.”
5. **Tool:** `request_archive(asset_id="clip-709", confirm_archive=true)` sets `archive=true`; lifecycle moves it after the configured delay, while raw evidence is never overwritten.

## Key Learning Points

1. Fast ingest is safe only when identity is ETag/version-based and duplicate workflow starts are tolerated.
2. Every factual clip statement needs source-relative evidence and a confidence band, especially during breaking coverage.
3. External rights lookup is a live policy decision; index metadata cannot authorize preview or download.
4. Librarians, not the model, approve newsroom ontology terms and their meanings.
5. Archive eligibility at day 30 is not automatic deletion or movement; a person must explicitly confirm the tag action.
6. Short clips can be fast to enrich without relaxing the non-destructive rendering and rights invariants.

## Cost Estimate (formula-based, labelled estimate)

**Estimate only — populate current regional prices and the selected Bedrock model rates after load testing the high-ingest profile.**

| Component | Monthly estimate formula |
|---|---|
| Raw and derivative storage | `(raw_GB + derivative_GB) × current_S3_storage_rate` |
| Proxies and analysis | `ingested_clip_minutes × proxy_ratio × storage_rate` |
| MediaConvert | `proxy_minutes × current_MediaConvert_rate + rendered_minutes × render_rate` |
| Embedding | `analyzed_clip_minutes × selected_embedding_model_rate` |
| Understanding | `clip_minutes × selected_understanding_model_rate` |
| AOSS | `OCU_hours × current_AOSS_OCU_rate + indexed_GB × storage_rate` |
| Event/workflow/compute | `EventBridge_events + state_transitions + Lambda_GB_seconds + DynamoDB_requests` |
| Rights integration | `rights_lookups × external_rights_service_unit_rate` |
| Cold archive | `archived_GB × Glacier_storage_rate + expected_restore_requests × restore_rate` |
| Total | Sum the rows; report baseline, per-ingested-minute, and per-rights-checked-download costs |

Measure `cost_per_ingested_minute` and `median_time_to_searchable` from a representative newsroom burst before raising Map concurrency.

## Pitfalls / Cautions

| Risk | Required control |
|---|---|
| High ingest duplicates work | Use ETag/version identity, conditional writes, and tolerated duplicate execution starts. |
| Search result bypasses wire restrictions | Make a live external rights lookup mandatory for preview and download. |
| Low-score footage becomes a breaking-news claim | Reply “no confident match” and preserve review evidence. |
| Model-suggested entity becomes a published taxonomy term | Require librarian review in the authoritative archive workflow. |
| Thirty-day policy moves objects automatically | Require `confirm_archive=true`; lifecycle moves only tagged eligible originals. |
| Producer renders over raw media | Restrict output paths to `derivatives/` and verify originals are versioned. |
| Concurrency exceeds model quota | Treat the configured `4` as a quota-verified ceiling, not an entitlement. |
| Client config contains OAuth secret | Resolve secrets at runtime and emit placeholders only. |
| Old vectors mix with new model vectors | Use model-specific indexes and the reindex/dual-write/alias path. |

## Variant

**Multi-region ingest with a single search index, only when policy permits metadata centralization.** Each region owns its raw bucket, KMS key, dispatch path, proxies, and local rights decision. After residency and rights checks, replicate only approved evidence metadata and embeddings to one designated regional AOSS collection; searches return a region-qualified `source_id`, and preview/download calls route back to the source region for a fresh rights lookup.

Do not use a single global index by default. If any clip’s jurisdiction, contract, or embargo rules prohibit cross-region metadata replication, keep regional indexes and federate the query instead. This preserves local control at the cost of multi-index search latency and more complex ranking.
