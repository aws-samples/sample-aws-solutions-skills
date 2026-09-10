# Decision tree

> **Use this document at Phase 2 Design.** Convert every Discovery answer into one
> explicit selection, state WHY it was selected, and write the resulting value to
> `config/media-archive.yaml`. Do not generate infrastructure until the GATE 2 design
> table at the end is complete.

## How to apply the tree

1. Ask only the questions needed to choose a branch. Do not infer a region, tenancy
   boundary, rights policy, or model from a customer name.
2. Verify volatile facts at runtime. A source document, a previous deployment, and a
   model family name are clues, not proof of current availability.
3. Prefer the smallest secure design that satisfies the stated workflow. Record every
   exception to a default in the generated configuration and GATE 2 table.
4. Preserve these invariants on every branch: originals remain immutable, rights are
   checked server-side for preview and download, archive is human-confirmed, and clip
   claims contain source-relative evidence.
5. When a branch requires a customer choice, stop at GATE 2 rather than silently
   selecting an irreversible or higher-cost option.

## 1. Model selection by runtime discovery

**WHY first:** model availability, video modalities, embedding dimension, invocation
limits, and regional access change independently. A fixed model list becomes invalid and
can generate an index mapping that rejects valid vectors or accepts incompatible ones.

### Required discovery sequence

| Step | If | Then | Record in configuration |
|---|---|---|---|
| 1 | A region is proposed | Run `aws bedrock list-foundation-models --region <region>` or use AWS Knowledge MCP. | discovery timestamp and region |
| 2 | A candidate advertises `VIDEO` in `inputModalities` and `EMBEDDING` in `outputModalities` | Present it as a video embedding candidate. | candidate ID, modalities, provider limits |
| 3 | A candidate advertises `VIDEO` in `inputModalities` and `TEXT` in `outputModalities` | Present it as a video-understanding candidate. | candidate ID, modalities, provider limits |
| 4 | Multiple candidates pass either filter | Let the user choose one embedding model and one understanding model after discussing limits and cost. | selected model IDs and selection rationale |
| 5 | An embedding candidate is selected | Read its dimension from current model metadata or a probe invocation. | `embedding_dimension` as an integer |
| 6 | The dimension is known | Generate the AOSS vector mapping and Lambda extraction guard from that value. | index name, alias, mapping dimension |
| 7 | A candidate has no verified dimension or invocation contract | Reject it for generation until a probe or official runtime metadata provides both. | blocked decision and verification needed |

### Filtering and selection rules

| Condition | If | Then |
|---|---|---|
| Video embedding | The candidate lacks `VIDEO` input or `EMBEDDING` output | Exclude it. A text-only embedding model is not a fallback for multimodal scene retrieval. |
| Video understanding | The candidate lacks `VIDEO` input or `TEXT` output | Exclude it from the enrichment Map; it cannot satisfy structured video analysis. |
| Asynchronous video embedding | The embedding model supports asynchronous output to S3 | Use it for asset and clip embedding; persist output under `analysis/`. |
| Query embedding | The selected embedding model supports synchronous text input | Use it to encode `search_assets` queries in the same vector space. |
| Structured metadata | The understanding model supports constrained JSON or an equivalent schema contract | Use it for segment analysis and mark all emitted concepts advisory. |
| Unbounded free text | The understanding model cannot produce a reliably parseable schema | Keep it out of the primary enrichment workflow unless a parser, retry policy, and eval proves it safe. |

### Dimension contract

The selected `embedding_dimension` is a compatibility boundary, not a tuning preference.

```text
config embedding dimension
      ↓
AOSS index mapping dimension
      ↓
embedding extraction guard
      ↓
query vector validation
      ↓
model-specific index alias
```

- A probe response is sufficient only when it is stored with the selected model ID and date.
- The index must reject a vector whose length differs from the configured dimension.
- A model change creates a new index. Reindex existing analysis output, dual-write while
  evaluated, validate search evidence, then move an alias to the new index.
- Never place vectors from two model IDs or two dimensions in the same live index merely
  because both calls returned floating-point arrays.

### TwelveLabs regional fallback

| Situation | If | Then | GATE 2 note |
|---|---|---|---|
| Preferred TwelveLabs family is available in the selected region | Runtime discovery and account access both pass | Offer the discovered embedding and understanding candidates; do not assume a particular version. | chosen IDs, dimension, limits |
| Preferred family is unavailable locally but cross-region inference is supported and approved | Data residency, latency, cost, and service support are accepted | Select the approved cross-region inference route and document the source and execution regions. | cross-region rationale and data boundary |
| Preferred family is unavailable and cross-region is unacceptable | Another region can satisfy required services and residency policy | Propose that region and rerun full discovery there. | proposed region and availability evidence |
| No region satisfies the required video model contract | User accepts a changed product scope | Pause generation and redesign around an explicitly approved alternate capability. | scope change; do not silently substitute text RAG |

Known provider families are only prompts for discovery. Treat every model ID as volatile and
verify it at runtime before synthesizing code or creating an index.

## 2. Region selection

**WHY first:** a region is viable only when the complete media path is available, not when
one Bedrock model appears in a static regional table.

| User priority | If | Then | Required verification |
|---|---|---|---|
| Data residency | A mandated region is named | Start there and verify every required service. Do not relocate media silently. | Bedrock candidates, async invocation, AgentCore Gateway, Cognito, Step Functions, MediaConvert, AOSS, KMS |
| Lowest latency to archive | Media and operators are concentrated in one eligible region | Prefer it if models and service limits pass. | upload path, model availability, quota, AOSS support |
| Existing AWS landing zone | The customer has an approved region | Use it only after the complete service matrix passes. | organization policy, KMS, network and logging constraints |
| Preferred models only in another region | Residency permits an alternate or cross-region route | Present both the regional and cross-region trade-offs at GATE 2. | model route, data movement, cost and latency |
| No single region satisfies requirements | The system can be split without violating rights or retention policy | Use complete regional stacks or reject the design until a compliant topology exists. | replication, index, key and workflow ownership |

Select one deployment region by default. Cross-region model inference is a deliberate data
boundary. It must be recorded in configuration and reviewed with the customer, never activated
as an invisible SDK setting.

## 3. Tenancy model

**WHY first:** tenant isolation is an authorization and cryptographic data-boundary decision.
A client-provided `tenant_id` cannot be trusted as an access-control mechanism.

| Model | If | Then | Required controls |
|---|---|---|---|
| Single server-side tenant | Pilot or one organization operates the archive | Resolve the tenant in trusted server configuration; do not expose it as a caller-selectable tool parameter. | Lambda validation, one scoped S3 prefix, DynamoDB partitioning, AOSS tenant filter |
| Shared multi-tenant service | Several tenants share a deployment and risk is acceptable | Add a Gateway interceptor that maps the authenticated principal to tenant and membership. | interceptor, Lambda revalidation, tenant-specific prefix, PK, AOSS filter and KMS policy |
| Regulated or strongly isolated tenant | Contract or risk profile requires a hard boundary | Use a tenant-specific AOSS collection and KMS key; prefer a separate account when needed. | per-tenant account or collection/key, scoped roles, independent retention and audit |
| Unknown tenant model | No owner has accepted a tenant boundary | Keep the single-server-side pilot model and block multi-tenant generation. | unresolved decision in GATE 2 |

For every multi-tenant branch, enforce the same server-resolved tenant across S3 key prefix,
DynamoDB partition key, Step Functions input, AOSS filter, derivative path, KMS policy, and
presigned URL authorizer. A Gateway interceptor is additive; each Lambda must revalidate the
context before data access.

## 4. Rights model

**WHY first:** model output can describe footage but cannot grant permission to see, edit, or
download it. Rights are an authoritative business decision, not vector metadata.

| Rights model | If | Then | Server-side behavior |
|---|---|---|---|
| None for synthetic demo media | The customer confirms all content is non-sensitive and universally visible | Store an explicit demo rights state, but retain the rights-check code path. | Every preview/download check resolves to the demo policy; no model output affects it. |
| Simple allow/deny ledger | One owner or a small role set manages visibility | Store `rights_state`, owner, allowed roles, and optional expiry in DynamoDB. | `get_preview_url`, `get_download_url`, and `render_highlight` reload current ledger state before action. |
| External rights system | A media rights, DAM, legal, or entitlement system is authoritative | Store a stable external reference and call the approved rights lookup during authorization. | Fail closed on timeout, missing record, revoked entitlement, or ambiguous response. |
| Approval workflow required | A human must confirm distribution, archival, or a derivative | Add an approval queue and status transition before signing a URL or rendering. | Audit approver, decision time, policy version, and source asset. |

Do not use AI labels, ontology suggestions, transcript content, or search scores to set rights.
A signed URL is a delivery capability, so issue it only after a fresh successful authorization.

## 5. Retention and archival policy

**WHY first:** originals, derived analysis, and customer-visible derivatives have different
legal and economic lifecycles. An archival command must never double as a destructive delete.

| Data class | Default | If | Then |
|---|---|---|---|
| `raw/` originals | Intelligent-Tiering after 30 days | A newer version exists | Transition noncurrent versions to Glacier after 90 days. |
| `raw/` originals | Retained under normal policy | `request_archive(confirm_archive=true)` is explicitly approved | Add only `archive=true`; S3 lifecycle moves the object to Glacier after one day. |
| `analysis/` | Regenerable data expires after 90 days | A compliance policy requires longer evidence retention | Override through configuration and record the reason. |
| `proxies/` | Derived analysis input | Re-enrichment is likely or human review needs proxies | Retain for the configured review window; otherwise expire after safe workflow completion. |
| `derivatives/` | Deliverable output | Retention differs from raw policy | Configure its own lifecycle; do not assume it can be deleted with analysis. |
| Legal hold or retention override | Required by policy | A lifecycle transition or deletion conflicts | Apply the hold/override and block automation until released by the authorized owner. |

Archive rules:

1. `request_archive` requires `confirm_archive=true` and an explicit human approval path.
2. The command may tag a source object; it may not overwrite, relocate, or delete the original.
3. A cold original may require a documented restore workflow before a high-quality new derivative.
4. The design table must state whether restores are manual, queued, or out of scope.

## 6. Proxy profile and maximum duration

**WHY first:** a normalized proxy makes model input sizes predictable and preserves the source
timeline through a model-constrained Map workflow.

| Decision | If | Then | Configuration field |
|---|---|---|---|
| Chunk duration | The understanding model supports at most about one hour per input | Use 55-minute source-relative chunks to retain margin. | `proxy.chunk_minutes: 55` |
| Chunk count | An asset duration requires fewer than or equal to the configured maximum | Create one contiguous proxy per chunk. | `proxy.max_chunks: 5` by default |
| Source duration | It exceeds chunk duration × maximum chunks | Reject or require an explicit higher cap and cost approval. | `proxy.max_chunks` and source-duration policy |
| Resolution/bitrate | Standard analysis quality is sufficient | Generate 1280×720 H.264 CBR 4 Mbps with AAC 128 kbps CBR. | profile version and bitrate fields |
| Understanding input size | A completed proxy exceeds the selected model limit | Fail the segment before calling the model; tune profile or chunk settings. | verified model limits |
| High-fidelity editorial output | The customer needs final delivery quality | Keep the original as render input; analysis proxies are not a delivery source. | render profile, not proxy profile |

At the defaults, five 55-minute chunks cover about four hours and 35 minutes of source
runtime. Validate actual duration, codec, and output size rather than trusting a file extension
or a client claim. All segment timestamps must be shifted to source time at merge.

For `render_highlight`, use a different output profile: H.264 QVBR, maximum 5 Mbps, quality 7,
and AAC 48 kHz stereo at 96 kbps. Round editing ranges deterministically to 30-fps timecode and
reject unordered, overlapping, out-of-range, or empty ranges.

## 7. Enrichment parallelism and cost caps

**WHY first:** increasing Map concurrency speeds delivery but can exceed provider quotas,
concentrate spend, and amplify retries during a model outage.

| Condition | If | Then | Configuration and guard |
|---|---|---|---|
| New pilot | Expected traffic and quota are unknown | Start understanding Map concurrency at 2. | `enrichment.map_concurrency: 2` |
| Stable quota and successful load test | Customer accepts increased spend and failure fan-out | Raise concurrency gradually with measured per-segment latency and throttling rate. | tested cap and rollback threshold |
| High-volume ingest | Concurrent assets could exceed an account-level budget | Add a queue, per-tenant admission cap, or scheduled backlog processing. | max active assets and spend threshold |
| Long-running asynchronous embedding | Polls continue beyond the configured bound | Stop and mark the job terminal; do not poll indefinitely. | state-machine timeout default 12 h; per-model poll default 6 h |
| Retryable service error | The error is classified as transient | Use bounded exponential retry with deterministic outputs. | retry count, backoff, error classes |
| Non-retryable schema or rights error | The next retry would not change the result | Fail fast and record actionable state in asset and job records. | terminal error policy |

Cost controls to require in the design:

- maximum source duration and maximum chunks per asset;
- maximum active enrichment jobs per tenant or account;
- Map concurrency and model request budget;
- allowed re-enrichment frequency and model-version migration plan;
- CloudWatch alarms for DLQ backlog, failures, throttling, and unexpected model duration;
- an explicit stop condition when a monthly or campaign budget is reached.

Do not “solve” a cost cap by silently lowering evidence quality or removing a required source-
time field. Ask the customer to choose an approved profile, chunk, or queue trade-off.

## 8. AOSS network posture and index sizing

**WHY first:** a pilot can prioritize build speed, while production must reduce the network
attack surface. Both need a model-compatible index and an authoritative DynamoDB rights check.

| Posture | If | Then | Required safeguards |
|---|---|---|---|
| Public endpoint pilot | Lambda-to-AOSS setup needs a fast first deployment and the risk owner approves | Allow a public network policy only for the collection endpoint. | IAM data access policy limited to catalog/workflow roles; no public application credentials; documented sunset date |
| Production private endpoint | Sensitive media, production policy, or a mature VPC exists | Use an AOSS VPC endpoint and private network policy. | VPC routing, security groups, private DNS, IAM data policies, operational access plan |
| Mixed environments | Pilot and production have different controls | Use separate collections and policies; never reuse a pilot public collection as production. | environment naming, deployment guard, migration plan |

### Index sizing rule

| If | Then |
|---|---|
| The embedding dimension is discovered | Generate a mapping with that exact dimension and a model-specific alias. |
| Expected vector count is estimated | Plan capacity from `vector_count × embedding_dimension × 4 bytes` plus ANN graph, metadata, replica, and growth overhead. Validate current service limits at runtime. |
| One model is replaced | Create a new index and alias; reindex and dual-write before cutover. |
| More than one tenant shares an index | Require a tenant filter on every query and assess whether a per-tenant collection is safer. |
| Search result lacks current catalog rights | Remove it from the client response or return only a policy-permitted redacted result. |

AOSS scores are retrieval signals. They do not replace confidence-band evaluation, rights
checks, catalog state, or source-time evidence.

## 9. Authentication and authorization

**WHY first:** a remote MCP client needs renewable machine identity, while archive operations
need least privilege and a second data-plane authorization check.

| Client identity choice | If | Then | Generated design |
|---|---|---|---|
| Cognito client credentials | The client can complete OAuth 2.0 service-to-service token refresh | Use AgentCore Gateway with `media-archive/read` and `media-archive/write` scopes. | Cognito resource server, two clients by default, Gateway authorizer |
| Separate read and write clients | A client only searches or an integration can be segmented | Issue a read-only client or a write-capable client, never both by default. | separate secret rotation, scopes, client configs |
| Combined service client | An approved backend requires both read and write functions | Grant both scopes only after risk acceptance and audit ownership are recorded. | named exception and review date |
| Enterprise IdP federation | The enterprise requires its existing identity provider | Federate through an approved OAuth broker or Cognito configuration after verifying Gateway and client support. | issuer, audience, claim mapping, token refresh and revocation behavior |
| Client cannot refresh OAuth | The host supports only static headers or static tokens | Use the local stdio bridge or a host surface with supported OAuth. | bridge configuration without embedded secret |

Authorization rules:

- Keep the OAuth client secret out of repositories, sample config, plugin manifests, and logs.
- Retrieve secret metadata through Secrets Manager or approved Cognito APIs at bridge runtime,
  using the user’s configured AWS credentials rather than a committed secret.
- Gateway auth controls target access. Lambda still validates tool parameters, server-resolved
  tenant, asset state, rights, and requested S3 prefix before acting.
- Split read and write clients by default. The read surface cannot call
  `create_upload_session`, `complete_upload`, `request_enrichment`, `render_highlight`,
  `request_archive`, or `create_collection`.

## 10. Client surfaces

**WHY first:** the same archive should work in existing agent environments without forcing each
client to reinvent OAuth refresh, upload streaming, or media playback controls.

| Surface | If | Then | Required configuration |
|---|---|---|---|
| ChatGPT/Codex plugin | The user wants a bundled local integration and inline media results | Install the `media-archive` plugin and connect it to the local bridge. | plugin manifest, full 16-tool allowlist, MCP Apps resource registration |
| Claude | A local stdio MCP configuration is supported | Configure the local bridge as the MCP server. | bridge command, approved root, non-secret region and Gateway settings |
| Kiro | Kiro MCP settings are available | Configure the same local bridge and archive operations skill. | MCP server entry and local client guidance |
| Quick Web | The connector supports service-to-service OAuth refresh | Configure remote Gateway MCP with a read or write client as appropriate. | token endpoint placeholder, client ID placeholder, scope selection |
| Quick Desktop | Native OAuth client-credentials refresh is unavailable or static headers are the only option | Use the local bridge; do not place a long-lived bearer token or secret in settings. | local bridge instruction and approved media root |
| Headless automation | A controlled server process needs archive tools | Use remote Gateway client credentials with a dedicated least-privilege client. | secret manager reference, scope and audit owner |

Tool naming rule:

| If | Then |
|---|---|
| A remote Gateway returns `media-catalog___search_assets` | Treat that fully qualified name as canonical. |
| The local bridge sees a unique suffix | It may expose `search_assets` as a convenience alias. |
| Two targets share a suffix | Preserve the complete Gateway name; never guess which tool was intended. |
| A plugin allowlist is generated | Include all 16 backend tools: 10 catalog and 6 control tools. |

The local bridge additionally offers `upload_local_video`, `render_media_results`, and
`resolve_media_playback`. These are client helpers and do not expand the remote 16-tool contract.

## 11. Evidence semantics and confidence thresholds

**WHY first:** semantic search is useful only when a client can distinguish grounded clips from
a plausible-but-weak match. Scores alone do not justify a claim about a moment in a video.

Every clip claim returned by `search_assets` must contain:

| Field | Requirement |
|---|---|
| `asset_id` | Stable catalog identity for the source asset. |
| `start_sec` | Source-relative clip start, not proxy-relative time. |
| `end_sec` | Source-relative clip end, not proxy-relative time. |
| `modality` | Embedding option or evidence modality used for retrieval. |
| `score` | Raw or normalized retrieval score with documented scale. |
| `confidence_band` | `high`, `medium`, or `low`, calculated by evaluated configured thresholds. |
| `source_id` | Stable source or index-document reference for traceability. |

| Result condition | If | Then |
|---|---|---|
| High confidence | Score and evaluation rules meet the high threshold | Return evidence and permit the client to describe a grounded matching clip. |
| Medium confidence | It passes the minimum threshold but needs human review | Return evidence with uncertainty language; do not overstate a fact. |
| Low confidence | It is below the configured confidence threshold | Report **no confident match** instead of presenting it as a retrieval answer. |
| No sources | A result cannot include the required evidence fields | Do not represent it as a clip claim; return a non-grounded explanation only if policy allows. |
| Whole-asset analysis | `analyze_asset` is used | Mark its answer as whole-asset grounded unless it independently returns source-time evidence. |

Evaluate thresholds with the generated golden fixtures before deployment and whenever a model,
proxy profile, embedding dimension, or index alias changes. Human review can approve a business
claim; it must not retrofit missing source evidence.

## 12. Single stack versus multi-stack

**WHY first:** one stack keeps a pilot’s security graph reviewable. Splitting too early creates
cross-stack deployment order, retention ownership, and policy drift without improving isolation.

| If | Then | Required design record |
|---|---|---|
| One region, one tenant boundary, and one operating team | Generate one `MediaArchiveStack`. | single-stack default, shared KMS/data plane, deploy order |
| Separate platform and application teams own different release cycles | Split data foundation from workflow/Gateway edge only after contract ownership is named. | exports, IAM ownership, rollback and retention owner |
| A tenant needs cryptographic or account isolation | Generate separate tenant stack/account boundaries. | account model, KMS keys, AOSS collection, ingress/egress policy |
| Multiple regions are required | Deploy a complete stack per region; document replication and search boundaries. | regional source of truth, replication, recovery, DNS/client routing |
| No operational owner accepts split dependencies | Keep the single stack. | explicit rationale |

A multi-stack design must preserve the same non-destructive storage rule, authoritative rights
ledger, source-time evidence contract, and read/write scope split. It is not a license to have
different implementations of the 16 tools by environment.

## 13. Optional components

**WHY first:** optional pieces should solve a demonstrated operating need. Adding every possible
component increases attack surface, cost, and approval burden without improving archive evidence.

| Optional component | If | Then | Default |
|---|---|---|---|
| Collections | Users need named cross-asset sets for a project, campaign, or review group | Use `create_collection`, `list_collections`, and `get_collection` backed by catalog state. | Include the tool contract; enable feature when a workflow needs it. |
| Ontology approval | Search tags must become controlled vocabulary | Store model output as suggested with provenance, then add human or external-system approval. | Suggested-only; never auto-approve. |
| MCP Apps UI | Users need visual result cards, timeline review, playback, or highlight confirmation | Serve `ui://media-archive/media-results.v1.html` from the local bridge. | Include for media-rich clients; do not build a full frontend. |
| DLQ and CloudWatch alarms | Production ingest needs actionable failure detection | Add encrypted DLQ, delivery-failure alarm, backlog alarm, workflow failure metrics, and budget signals. | Recommended for all deployments; mandatory for production. |
| Malware/content validation | Untrusted upload sources exist | Insert validation before enrichment and block unsafe originals from model calls. | Add when threat model requires it. |
| Legal hold and restore workflow | Retention policy includes frozen assets or cold originals | Add explicit hold/restore states and approval actors. | Add when compliance or editorial recovery requires it. |
| Cross-region DR | Recovery objectives require another region | Design replicated catalog/media/index strategy with keys and rights checks. | Add only after residency and cost review. |
| Usage metering | Multiple tenants or chargeback is required | Meter upload, duration, model use, storage, and render consumption by server-resolved tenant. | Add for multi-tenant or billable services. |

## Output (the design table produced at GATE 2)

The agent MUST present exactly this table shape, complete every row, and use placeholders only
for facts that are explicitly awaiting customer confirmation or runtime verification.

| Decision area | Selected option | WHY / verified evidence | Generated configuration or IaC consequence |
|---|---|---|---|
| Deployment region | `<region>` | `<runtime service and model availability evidence>` | `region`, cross-region route or `none` |
| Embedding model | `<embedding-model-id>` | `<VIDEO input + EMBEDDING output, account access, selection rationale>` | `embedding_model_id`, `embedding_dimension`, model-specific index alias |
| Understanding model | `<understanding-model-id>` | `<VIDEO input + TEXT output, input limits, selection rationale>` | `understanding_model_id`, Map input contract |
| Embedding dimension | `<integer>` | `<model metadata or probe evidence>` | AOSS mapping and Lambda vector-length guard |
| Tenancy | `<single server-side | shared multi-tenant | isolated tenant>` | `<isolation requirement>` | interceptor/revalidation, prefixes, KMS, collection or account boundary |
| Rights model | `<demo | DynamoDB allow/deny | external lookup | approval workflow>` | `<policy owner and fail-closed behavior>` | rights attributes, lookup/approval integration, URL authorization |
| Storage and retention | `<lifecycle policy>` | `<raw, analysis, proxy, derivative, archive and restore decisions>` | S3 prefixes, lifecycle rules, `confirm_archive=true` path |
| Proxy profile | `<chunk duration, max chunks, resolution, bitrates>` | `<selected-model duration/size limits and cost trade-off>` | MediaConvert settings and source-duration guard |
| Enrichment controls | `<Map concurrency and budgets>` | `<quota, latency, cost, retry evidence>` | Step Functions timeout/poll bounds, retries, queue/caps, alarms |
| Vector index | `<collection, index, alias, network posture>` | `<dimension, growth estimate, pilot or production network decision>` | AOSS policies, mapping, tenant/model filters, migration plan |
| Authentication | `<Cognito client credentials | enterprise federation>` | `<client refresh capability and identity constraints>` | read/write scopes, separate clients, secret retrieval method |
| Client surfaces | `<ChatGPT/Codex plugin, Claude, Kiro, Quick Web, Quick Desktop bridge>` | `<host capabilities and OAuth support>` | remote/stdio config, 16-tool allowlist, MCP Apps resource where selected |
| Evidence semantics | `<confidence thresholds and review policy>` | `<golden-fixture evaluation results>` | required evidence fields, `no confident match` rule |
| Stack composition | `<single MediaArchiveStack | named multi-stack topology>` | `<operational owner and isolation/release rationale>` | stack boundaries, typed references, deployment and retention ownership |
| Optional components | `<collections, ontology approval, MCP Apps UI, DLQ alarms, other approved options>` | `<demonstrated workflow or operating need>` | enabled resources, schemas, tests and operational runbook |
| Cost and stop conditions | `<approved caps and escalation path>` | `<budget owner and monitored thresholds>` | quotas, admission control, alarms and manual approval gates |
