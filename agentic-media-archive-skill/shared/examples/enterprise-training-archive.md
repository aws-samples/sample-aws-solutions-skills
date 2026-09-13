# Enterprise Training Archive — Governed Multilingual Learning

> A global learning-and-development archive for thousands of 20–60 minute training videos, with short-lived analysis, seven-year raw retention, federated enterprise identity, and deliberate low-confidence abstention.

## User Answers (Discovery)

| Discovery question | Answer recorded for this example |
|---|---|
| Primary users | Employees searching training, L&D curators, compliance reviewers, and course authors |
| Asset scale | Thousands of videos, normally 20–60 minutes each, plus new course releases each month |
| Content scope | Product training, policy instruction, instructor-led recordings, and accessible captions |
| Search languages | Multilingual queries and metadata; return evidence in the source asset timeline |
| Primary client | Amazon Quick Web using service-to-service OAuth |
| Authoring client | Claude Code for L&D authors preparing collections and validation prompts |
| Identity | Existing enterprise IdP federated into Cognito; group claims determine archive roles |
| Tenancy | One enterprise tenant with server-resolved identity and no caller-selectable tenant parameter |
| Region | `<region>` after regional availability and data-residency review |
| Data classification | Internal learning material; apply organization retention and access policies before ingest |
| Raw retention | Seven years minimum, including the authoritative original and version history |
| Analysis retention | Thirty days; summaries and vectors must be rebuilt from raw when policy permits |
| Rights model | Course ownership and employee entitlement are checked by the server for every preview/download |
| Runtime model discovery | `aws bedrock list-foundation-models --region <region>` |
| Embedding filter | `VIDEO` input and `EMBEDDING` output modalities |
| Understanding filter | `VIDEO` input and `TEXT` output modalities |
| Chosen embedding model | `<embedding-model-id>` — discovered via `list-foundation-models`; Marengo capability family as of 2026-09, re-verify |
| Chosen understanding model | `<understanding-model-id>` — discovered via `list-foundation-models`; Pegasus capability family as of 2026-09, re-verify |
| Embedding dimension | `512`, verified from model documentation or a probe before index creation |
| Model migration | Reindex to a model-specific index, dual-write, validate recall, then move the alias |
| Confidence policy | High and medium hits carry evidence; low-only result sets become “no confident match” |
| Ontology policy | Model suggestions are review candidates for the L&D taxonomy, never automatically approved terms |
| Proxy policy | 720p 4 Mbps H.264/AAC analysis proxies; a 60-minute source may split into two ≤55-minute chunks |
| Map concurrency | `2`, backed by a queue and quota-aware operational limits |
| Gate 1 | Learning scope, retention, federation, and abstention policy approved |
| Gate 2 | Single-stack design and evidence/retention table approved |
| Gate 3 | `cdk synth`, `python scripts/verify.py`, and pytest pass before deployment |

## Resulting config/media-archive.yaml

```yaml
schema_version: "1.0"
solution:
  name: enterprise-training-archive
  description: governed multilingual learning-video discovery with evidence and abstention
  region: <region>
  environment: production
stack:
  name: MediaArchiveStack
  composition: single-stack
  imports: [enterprise-identity-provider]
identity:
  tenancy_mode: single-tenant-pilot
  server_resolved_tenant_id: enterprise-learning
  multi_tenant_interceptor_enabled: false
  cognito:
    user_pool_id: <user-pool-id>
    oauth_flow: client_credentials
    federation: {protocol: SAML_or_OIDC, enterprise_idp: <enterprise-idp>, principal_claim: sub, group_claim: groups}
    role_mapping: {learner: learning-viewer, curator: learning-curator, compliance: learning-reviewer}
    client_secret_source: runtime-secret-resolution
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
    raw_minimum_retention_days: 2555 # seven years; legal hold rules may extend it.
    noncurrent_to_glacier_after_days: 90
    analysis_expire_after_days: 30
    archive_tag: archive=true
    archive_tag_to_glacier_after_days: 1
media:
  accepted_asset_types: [training_course, instructor_recording, policy_module]
  max_source_duration_minutes: 60
  analysis_proxy:
    max_chunk_duration_minutes: 55
    max_chunks_per_asset: 2
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
  network: {endpoint_mode: vpc, vpc_endpoint_id: <aoss-vpc-endpoint-id>, public_access: false}
  vector: {field: embedding, dimension_from: models.embedding.dimension, similarity: cosine, index_type: hnsw}
  document_fields: [tenant_id, asset_id, source_id, start_sec, end_sec, modality, score, confidence_band, model_id, language]
workflow:
  ingest_delivery: at-least-once
  identity: {source: object_etag_and_version_id, conditional_catalog_write: true, tolerate_execution_already_exists: true}
  state_machine_timeout_hours: 12
  embedding_poll_bound_hours: 6
  pegasus_map_concurrency: 2
  retry: {transient_attempts: 4, exponential_backoff: true}
  states: [AWAITING_UPLOAD, QUEUED, PROCESSING, PROXYING, EMBEDDING, PEGASUS_MAP, ENRICHED, INDEXED, READY, FAILED]
rights:
  source_of_truth: learning-entitlement-service
  recheck_on: [get_preview_url, get_download_url]
  policy: {course_owner_review: required, employee_group_entitlement: required, stale_index_rights_allowed: false}
  signed_url_ttl_seconds: {upload: 3600, preview_min: 60, preview_max: 300, download_min: 60, download_max: 900}
metadata:
  languages: [en, fr, de, ja, ko, es]
  ontology: {model_suggestions_status: MODEL_SUGGESTED, approved_vocabulary_source: learning-taxonomy, require_human_approval: true}
  confidence: {high_min_score: 0.83, medium_min_score: 0.65, low_response: no confident match}
  evidence_required: [asset_id, start_sec, end_sec, modality, score, confidence_band, source_id]
  abstention: {return_low_score_evidence_for_review: true, never_infer_unseen_instruction: true}
collections: {enabled: true, owner_role: learning-curator, use_cases: [role-based-curriculum, compliance-pathway]}
archive:
  enabled: true
  require_confirm_archive: true
  eligible_after_raw_retention_days: 2555
  behavior: tag-only-lifecycle-performs-move
clients:
  quick_web:
    enabled: true
    primary: true
    oauth_mode: service_to_service
    gateway_url: <gateway-url>
    allowlisted_tools: [list_assets, get_asset, search_assets, analyze_asset, get_job_status, get_preview_url, get_download_url, get_ontology, list_collections, get_collection, create_upload_session, complete_upload, request_enrichment, render_highlight, request_archive, create_collection]
  claude_code:
    enabled: true
    bridge_name: local_bridge
    gateway_url: <gateway-url>
    ui_resource: ui://media-archive/media-results.v1.html
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

- **Composition:** one `MediaArchiveStack` imports the pre-existing enterprise IdP relationship rather than creating a second identity stack.
- **Ingress and workflow:** S3/KMS → EventBridge → dispatch Lambda → Step Functions → MediaConvert proxy, asynchronous embeddings, understanding Map, catalog/index merge.
- **Authoritative state:** DynamoDB holds access, retention, job, collection, ontology-candidate, and idempotency state; AOSS is a private retrieval projection.
- **Federation:** Cognito maps IdP claims and groups to learning roles before the Gateway forwards the request context.
- **Read tools:** catalog Lambda implements ten read tools and checks current employee entitlement again before every short-lived URL.
- **Write tools:** control Lambda implements six write tools; only authorized curators can create collections or request retention actions.
- **Clients:** Quick Web is the primary service-to-service MCP client; Claude Code uses `local_bridge` for L&D author workflows.
- **Retention override:** analysis data expires after 30 days while raw originals stay retained for at least seven years; both durations are testable configuration.
- **Generation order:** config → infra → Lambdas → `local_bridge` → clients → scripts → evaluation/tests.
- **Gate mapping:** Gate 1 approves retention/federation, Gate 2 approves the design table, Gate 3 validates synthesis and tests.
- **Workflow phases:** Phase 1 Discovery → Gate 1, Phase 2 Design → Gate 2, Phase 3 Generate → Gate 3, Phase 4 Validate, Phase 5 Deploy.

## Demonstration Scenarios

### 1. Quick Web finds a multilingual course segment

1. **Learner:** “Where does the onboarding course explain data classification? Answer in French.”
2. **Tool:** `search_assets(query="data classification onboarding", preferred_language="fr")`.
3. **Result:** `{asset_id: "course-118", start_sec: 812.0, end_sec: 876.0, modality: "speech+slide", score: 0.90, confidence_band: "high", source_id: "course-version-2026-03"}`.
4. **Assistant:** answers in French, distinguishes translated explanation from the original English evidence, and includes the source-relative interval.
5. **Tool:** `get_preview_url(asset_id="course-118", start_sec=812.0, end_sec=876.0)` verifies the learner’s current group entitlement before issuing a 120-second URL.

### 2. Low confidence is an explicit learning-safe abstention

1. **Learner:** “Which video guarantees that every contractor may export customer data?”
2. **Tool:** `search_assets(query="contractor export customer data guarantee")` returns two `low` records with scores `0.48` and `0.44`, each retaining source evidence.
3. **Tool:** `analyze_asset(asset_id="policy-041", question="Does this course guarantee contractor export permission?")` finds no supported policy statement.
4. **Assistant:** “No confident match.” It offers the evidence ranges for a compliance reviewer but does not turn nearby training material into authorization advice.

### 3. Claude Code prepares a curator-reviewed learning collection

1. **L&D author:** “Build a role-based collection for new managers covering coaching and incident escalation.”
2. **Tools:** `search_assets(query="manager coaching")`, then `search_assets(query="incident escalation")`, return medium-or-higher clips with evidence records.
3. **Tool:** `get_ontology()` returns model-suggested concepts marked `MODEL_SUGGESTED` alongside the approved learning taxonomy.
4. **Assistant:** presents suggestions as candidates; a curator reviews concepts in the authoritative taxonomy workflow, because no MCP result may auto-approve vocabulary.
5. **Tool:** `create_collection(name="New manager pathway", asset_refs=[...evidence-bearing clips...])` creates a reference collection, not duplicated media.

### 4. Archive remains a human-confirmed retention action

1. **Compliance reviewer:** “Archive this course after its seven-year retention term has ended.”
2. **Tool:** `get_asset(asset_id="course-002")` verifies current retention, legal-hold state, and source version.
3. **Tool:** `request_archive(asset_id="course-002", confirm_archive=false)` returns that an explicit confirmation is required and changes nothing.
4. **Reviewer:** “I confirm archival under the approved retention policy.”
5. **Tool:** `request_archive(asset_id="course-002", confirm_archive=true)` tags the eligible raw object; lifecycle performs the Glacier transition and no tool moves or overwrites the original directly.

## Key Learning Points

1. Short-lived analysis and long-lived originals are independent policies; expiry of vectors never erases the retained source.
2. Federated identity determines access, but URL issuance still checks current entitlement on the server.
3. “No confident match” protects learners from treating an uncertain retrieval result as mandatory policy guidance.
4. Multilingual answers can be localized while evidence remains source-relative and independently verifiable.
5. Ontology suggestions speed curation but never become the approved learning taxonomy without human review.
6. Cold archival is a confirmed tag action after retention eligibility, not an automatic or destructive client operation.

## Cost Estimate (formula-based, labelled estimate)

**Estimate only — insert current prices for `<region>`, the selected models, AOSS, and lifecycle storage classes before Gate 2 approval.**

| Component | Monthly estimate formula |
|---|---|
| Seven-year raw estate | `raw_GB_months × storage_class_rate + noncurrent_GB × lifecycle_rate` |
| Thirty-day analysis | `analysis_GB × 30/average_month_days × storage_rate` |
| Proxies | `source_minutes × proxy_output_ratio × proxy_storage_rate` |
| MediaConvert | `proxy_minutes × current_MediaConvert_rate + rendered_minutes × render_rate` |
| Embedding and understanding | `analyzed_minutes × embedding_rate + proxy_chunk_minutes × understanding_rate` |
| AOSS | `OCU_hours × current_AOSS_OCU_rate + indexed_GB × storage_rate` |
| Compute and workflow | `Lambda_GB_seconds + state_transitions + DynamoDB_requests` |
| Total | Sum the above; show both recurring seven-year storage and monthly enrichment marginal cost |

Track `cost_per_course = ingest + enrichment + expected search-index retention` and separately forecast the seven-year raw-storage commitment.

## Pitfalls / Cautions

| Risk | Required control |
|---|---|
| Analysis expiry is mistaken for raw deletion | Test raw and analysis lifecycle prefixes independently. |
| IdP group is trusted without Lambda validation | Pass the verified request context and revalidate authorization server-side. |
| A low score becomes policy advice | Return “no confident match” and route to a compliance reviewer. |
| Model taxonomy becomes official | Keep it `MODEL_SUGGESTED` until curator review. |
| Course is downloaded after access changes | Recheck entitlement before every preview or download URL. |
| Raw content archives before seven years | Reject archive requests until retention and legal-hold checks pass. |
| Claude Code receives a stored secret | Use runtime secret resolution and placeholders only. |
| Quick Web and Quick Desktop are conflated | Generate Quick Web service-to-service instructions; document Desktop separately if needed. |
| Embeddings mix after a model update | Use the reindex, dual-write, alias-cutover migration path. |

## Variant

**Cost-capped enrichment.** Set `pegasus_map_concurrency: 1`, queue eligible uploads, and run the enrichment dispatcher during a nightly batch window. Keep ingest idempotent so the same ETag/version never creates a duplicate workflow, and expose `get_job_status` so authors know when overnight analysis is pending.

This variant lowers concurrency-driven spend and simplifies quota management, but it trades near-real-time discoverability for a next-day SLA. Raw retention, evidence requirements, entitlement checks, and low-confidence abstention do not change.
