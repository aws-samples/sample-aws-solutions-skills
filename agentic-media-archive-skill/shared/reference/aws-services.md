# AWS services, model discovery, quotas, and cost catalog

This reference supports the AWS-native `media-archive` skill. It is deliberately
runtime-verified: model IDs, regional availability, quotas, and prices change more
often than skill releases. Generate the fixed 16-tool contract only after recording
current results in `config/media-archive.yaml`:

- Catalog target `media-catalog`: 10 read tools.
- Control target `media-commands`: 6 write tools.
- Gateway-exposed names may be prefixed as `target___tool`; clients must preserve
  the contract names after removing only an unambiguous prefix.

## 1. Bedrock video model discovery

Do not treat the table below as an allowlist. During Discovery, run both commands in
the selected deployment region and present the resulting candidates to the user.

```bash
aws bedrock list-foundation-models --region <region> --by-output-modality EMBEDDING \
  --query 'modelSummaries[?contains(inputModalities, `VIDEO`) && contains(outputModalities, `EMBEDDING`)].{id:modelId,name:modelName,provider:providerName,input:inputModalities,output:outputModalities,inference:inferenceTypesSupported}' \
  --output table

aws bedrock list-foundation-models --region <region> --by-output-modality TEXT \
  --query 'modelSummaries[?contains(inputModalities, `VIDEO`) && contains(outputModalities, `TEXT`)].{id:modelId,name:modelName,provider:providerName,input:inputModalities,output:outputModalities,inference:inferenceTypesSupported}' \
  --output table
```

The first filter finds video-capable embedding candidates. The second finds
video-understanding candidates; it can include dedicated video models and any
multimodal model that reports both `VIDEO` input and `TEXT` output. An empty result
means the model is unavailable in that region or not published through that API;
it is not permission to substitute a text-only model silently.

Use AWS Knowledge MCP as a second, current-source check before generating
infrastructure:

```text
aws___search_documentation({
  search_phrase: "Amazon Bedrock video embedding and video understanding model availability in <region>",
  topics: ["reference_documentation"]
})
aws___get_regional_availability({
  resource_type: "product", filters: ["Amazon Bedrock", "<selected-model-id>"]
})
```

If the installed MCP schema uses different argument names, use its documented
search and regional-availability equivalents. Capture the model card, invocation
mode, input contract, output format, and quota evidence alongside the generated
configuration.

### Known as of 2026-09 — re-verify

| Role | Known model and ID | Contract to generate | Limits and cautions |
|---|---|---|---|
| Video embedding | TwelveLabs Marengo Embed 3.0 — `twelvelabs.marengo-embed-3-0-v1:0` | 512-D embeddings; asynchronous video invocation with S3 output; visual, audio, and transcription modalities; separate clip and asset embeddings | Dynamic segmentation has a minimum segment of 4 s. Input-size and duration limits are **verify current values**; do not hardcode an old model-card limit. |
| Video understanding | TwelveLabs Pegasus 1.2 — `twelvelabs.pegasus-1-2-v1:0` | Video input from S3; plain-text or JSON-schema-constrained output; low-temperature archive metadata and whole-asset answers | Input must be under 1 h and under 2 GB; both are **verify current values**. Treat `finishReason == "length"` as failure, never partial truth. |

These records are known candidates, not a promise that either model is enabled in
every account or region. Let the user choose one embedding model and one
understanding model from the current discovery output. Persist both IDs, the
embedding dimension, and the evidence date:

```yaml
models:
  embedding_model_id: <selected-video-embedding-model-id>
  embedding_dimension: <dimension-from-model-card-or-probe>
  understanding_model_id: <selected-video-understanding-model-id>
  verified_at: <ISO-8601-timestamp>
```

The model card or a small approved probe is the source of the dimension. The index
mapping and response-length guard must read this configuration; a literal `512` is
valid only when the selected model and current probe explicitly report 512.

## 2. Regional availability and model access

Do not publish a static region list. For each candidate region, run the discovery
commands above, then verify the selected ID directly:

```bash
aws bedrock get-foundation-model \
  --region <region> \
  --model-identifier <selected-model-id>
aws service-quotas list-service-quotas \
  --service-code bedrock --region <region>
```

Also use the AWS Knowledge MCP regional-availability call for Bedrock AgentCore
Gateway, OpenSearch Serverless, MediaConvert, and the chosen authentication path.
A stack is deployable only in a region where every mandatory control-plane and
data-plane dependency is available.

### Model access procedure

1. Sign in with a least-privilege deployment role for the target account and region.
2. Open **Amazon Bedrock → Model access** and request/enable the selected provider
   model when the account requires it. Complete any provider use-case workflow.
3. Confirm availability with `get-foundation-model` and a minimal authorized invoke
   or async-invoke smoke test; do not rely on console appearance alone.
4. Record the selected model, dimension, invocation mode, quota, and evidence date
   in `config/media-archive.yaml`.
5. If access is denied, stop before generating a fallback. Ask the user to select
   an available model or to complete access enablement.

Model access can be organization-, account-, provider-, and region-specific. Never
embed approval state, a client secret, or an assumed cross-region inference profile
in generated source.

## 3. MediaConvert: proxy and derivative policy

Use a dedicated queue so analysis proxies and user-requested highlight renders are
observable and controllable. Choose the queue pricing plan during Design:

| Queue mode | Use when | Cost behavior | Generation guidance |
|---|---|---|---|
| On-demand | Dev, PoC, uneven workloads, or uncertain monthly minutes | Charged for processed media according to current regional pricing and output tier | Default choice. Set queue concurrency through Service Quotas and use request tokens for retry safety. |
| Reserved | Sustained, predictable throughput with a committed operating plan | Reservation and utilization economics vary by region and current offering | Require a written utilization forecast and current pricing-calculator comparison before choosing it. |

Cost is driven by source minutes, codec, resolution, frame rate, output count,
queue plan, and regional price. Re-price with the AWS Pricing Calculator rather
than copying any historical per-minute rate.

For analysis proxies, generate 1280×720 H.264 **CBR** at 4 Mbps with AAC 128 kbps
CBR. CBR produces predictable sizes and makes the under-2-GB Pegasus input guard
credible. For user-facing highlights, generate H.264 **QVBR** with a bounded
maximum bitrate and quality level because visual quality matters more than exact
byte predictability.

All source-relative clipping uses `ZEROBASED` timecode. A proxy job has one clipping
range per MediaConvert job; each input declares both a video selector and an audio
selector. Do not rely on an implicit default track, source timecode, or a single job
with multiple input clippings for proxy chunking.

## 4. OpenSearch Serverless vector collection

Use a `VECTORSEARCH` collection with an IAM data-access policy and a network policy.
The pilot can use a public endpoint protected by IAM; production should normally use
an interface VPC endpoint and a network policy that allows only the approved
endpoint IDs. Do not confuse an IAM policy with a private network boundary.

OpenSearch Serverless charges for configured or consumed OpenSearch Compute Units
(OCUs), storage, and applicable data transfer. Vector collections have a baseline
capacity model; development workloads can therefore be dominated by OCU-hours even
when the video corpus is small. Standby replicas improve availability but can raise
active capacity and cost. Make `standbyReplicas` an explicit design decision:

| Setting | Appropriate use | Capacity implication |
|---|---|---|
| `DISABLED` | Dev/PoC where a temporary interruption is acceptable | Lower baseline capacity; validate recovery expectations. |
| `ENABLED` | Production availability requirement | Plan and price redundant capacity; verify current OCU behavior in the selected region. |

### 512-D index sizing worksheet

A float32 vector is `512 × 4 = 2,048 bytes` before index structures and metadata.
Use this planning formula, then validate with a representative corpus:

```text
raw_vectors_bytes = document_count × 512 × 4
stored_bytes = document_count × (2,048 + average_metadata_bytes)
planned_index_bytes = stored_bytes × hnsw_and_segment_overhead_factor × replica_factor
```

Start a capacity exercise with an HNSW/segment overhead factor of **3–6×** as a
planning envelope, not a guarantee. Measure actual index size after loading a
representative sample, then choose OCUs and retention from telemetry. Include one
index per embedding model version; mixing dimensions or vector semantics in a single
index invalidates retrieval.

Use a server-generated `_id` for AOSS documents. Keep the deterministic
`document_key` field for de-duplication, audit, and reindexing; do not attempt to
force the server document ID through a write path that does not support it.

## 5. Storage, encryption, and state services

| Service | Design use | Quota / operating check | Principal cost drivers |
|---|---|---|---|
| Amazon S3 | Versioned originals; `raw/`, `proxies/`, `analysis/`, and `derivatives/` prefixes | Bucket policy, event notifications, lifecycle transitions, restore behavior, request rates | Storage class, object size/duration, PUT/GET/LIST, lifecycle transitions, retrieval, egress. |
| AWS KMS | Customer-managed encryption for S3, DynamoDB, queue, and model output | Key policy must permit the service principals without circular dependencies; request-rate quota | Key storage, API requests, cross-account use when enabled. |
| Amazon DynamoDB | Authoritative assets, jobs, idempotency records, rights, and ontology candidates | On-demand or provisioned choice; item size; GSI partition distribution; PITR | Reads, writes, storage, GSI, backup/PITR, streams if added. |
| AWS Step Functions | Durable enrichment orchestration and polling | Standard workflow execution, transition, history, and runtime quota; state-machine timeout | State transitions, execution history, logging, retries. |
| AWS Lambda | Dispatch, workflow steps, catalog tools, and control tools | Reserved/concurrent execution, duration, memory, ARM64 runtime support | Requests, GB-seconds, provisioned concurrency if chosen, logs, VPC networking if used. |
| Amazon EventBridge | S3 object-created routing to dispatch | Event delivery, retry, DLQ permission, event-bus quota | Ingested/custom events, archive/replay if enabled, cross-account routing. |
| Amazon SQS | DLQ for failed EventBridge delivery | Retention, visibility timeout, in-flight messages, KMS permissions | Requests, payload size, extended retention, KMS requests. |
| Amazon Cognito | Service-to-service OAuth for Gateway clients | Client-credentials token rate and domain configuration; secret storage is external | MAUs where applicable, advanced features, token requests by current pricing. |
| Bedrock AgentCore Gateway | Remote MCP endpoint and target routing | Gateway/target limits, authentication configuration, target invoke permissions | Current Gateway request pricing, target invocations, logs, authentication dependencies. |

Use lifecycle rules to keep originals and generated outputs independent. Suggested
configurable defaults are raw to Intelligent-Tiering after 30 days, noncurrent raw
versions to Glacier after 90 days, human-confirmed `archive=true` raw objects to
Glacier after one day, and transient analysis output expiry after 90 days. Archival
only tags the source; lifecycle performs the storage-class transition.

## 6. Cost estimate scenarios

All dollar outcomes in this section are **estimates to be re-priced with the AWS
Pricing Calculator** for `<region>`, selected models, queue plan, data transfer,
reservation status, and organization discounts. The formulas are intentionally
visible so an agent does not convert an old unit price into a promise.

### Dev / PoC: 50 video hours/month, 100 searches/day

Assumptions: 30-day month, 3,000 searches/month, 720p CBR proxy at 4 Mbps video +
128 kbps audio, and an independently selected source bitrate/retention period.
The proxy output estimate is about 86.5 GiB before container and index overhead.

| Line item | Monthly quantity / formula | Estimate input to re-price |
|---|---|---|
| Raw S3 storage | `50 h × average_source_GiB_per_hour × retained_months` | S3 storage-class rate plus PUT/GET/LIST and lifecycle requests. |
| Analysis proxy storage | `50 × 3,600 × (4,000,000 + 128,000) / 8 / 2^30 ≈ 86.5 GiB` | S3 Standard or selected analysis class; include 90-day expiry. |
| MediaConvert proxies | `50 source hours × current regional HD transcode rate` | On-demand queue rate unless a reservation was explicitly approved. |
| Marengo embeddings | `50 video hours × current selected-model video embedding rate` | Price visual, audio, and transcription modalities as configured. |
| Pegasus enrichment | `ceil(50 × 60 / 55) = 55 proxy segments × current input/output model price` | Include JSON output tokens and retries after fail-closed length results. |
| AOSS | `configured active OCUs × ~730 h × OCU-hour rate` | Include standby-replica factor, storage, and data transfer. |
| Search embedding | `3,000 searches × current text-embedding invocation rate` | Query model call plus AOSS retrieval request/capacity. |
| DynamoDB | `assets/jobs/idempotency writes + 3,000 search/read paths` | On-demand RRU/WRU or provisioned capacity and PITR. |
| Lambda + Step Functions | `workflow invocations × GB-s + state transitions` | Include polling, Map segments, retries, and control/catalog calls. |
| EventBridge + SQS + KMS | `object events + failed-delivery retries + encrypted API requests` | Include KMS calls made by encrypted S3, SQS, and model outputs. |
| Cognito + Gateway + logs | `active clients/tokens + MCP requests + log GiB` | Include token endpoint volume, Gateway requests, and CloudWatch retention. |

### Production: 2,000 video hours/month, 5,000 searches/day

Assumptions: 30-day month, 150,000 searches/month, same proxy profile, model choices
verified at deployment, and multiple assets so the per-asset chunk limit remains a
separate admission-control check. Proxy output is about 3.38 TiB before metadata,
container, lifecycle, or replica overhead.

| Line item | Monthly quantity / formula | Estimate input to re-price |
|---|---|---|
| Raw S3 storage | `2,000 h × average_source_GiB_per_hour × retention` | Model source ingest bitrate distribution and lifecycle retrieval probability. |
| Analysis proxy storage | `2,000 × 3,600 × 4,128,000 / 8 / 2^40 ≈ 3.38 TiB` | Storage class, analysis expiry, replication, and retrieval requirements. |
| MediaConvert proxies | `2,000 source hours × queue-specific regional transcode rate` | Compare on-demand and reserved queues using the forecasted utilization. |
| Marengo embeddings | `2,000 video hours × selected modalities × current unit rate` | Include async output S3/KMS requests and retry budget. |
| Pegasus enrichment | `ceil(2,000 × 60 / 55) = 2,182 segments × current model input/output price` | Include observed average output tokens and a bounded retry rate. |
| AOSS vectors | `N clips × (512 × 4 + metadata) × HNSW factor × replica factor` | Translate measured sample bytes and availability choice into OCU/storage plan. |
| Search embedding + retrieval | `150,000 query embeddings + 150,000 k-NN searches` | Model calls, AOSS OCUs, request/load testing, and cache hit rate. |
| DynamoDB | `ingest/state writes + rights checks + 150,000 query/result reads` | Include GSI, PITR, backup, and tenant growth assumptions. |
| Lambda + Step Functions | `asset workflows × proxy chunks × retry/poll transitions` | Size ARM64 memory and concurrency from measured p95 duration. |
| EventBridge + SQS + KMS | `upload events + DLQ/replay allowance + encrypted service calls` | Include worst-case retry storms, not only happy-path events. |
| Cognito + Gateway + observability | `token refreshes + 150,000 MCP requests + trace/log volume` | Include log retention, alarms, dashboards, and security analytics. |

For either scenario, run a small representative ingestion and retrieval test first.
Replace proxy bitrate, observed clip density, payload size, retry rate, query volume,
and AOSS index growth with measured values before approving production spend.

## 7. Service quotas to inspect and request early

Quota names and default values vary by region, account, model, and release. Use the
Service Quotas console or CLI to inspect exact names, then request capacity from a
peak-throughput model rather than a copied default.

| Service | Check / request | Sizing trigger |
|---|---|---|
| Bedrock | Selected-model `InvokeModel`, `StartAsyncInvoke`, polling, RPM/TPM, and concurrent invocation quotas | Peak proxy segments and search embeddings after retry allowance. |
| MediaConvert | Concurrent jobs, queue throughput, and account job limits | Simultaneous analysis chunks plus highlight renders. |
| Step Functions | Standard execution rate, state transitions, concurrent executions, and history | Ingest fan-out, polling cadence, Map items, replay policy. |
| Lambda | Account concurrency, per-function reserved concurrency, duration, burst behavior | Dispatch bursts, catalog requests, and Pegasus Map concurrency. |
| OpenSearch Serverless | OCU capacity, collection/index limits, VPC endpoint limits | Measured vector corpus, p95 search load, replica setting. |
| DynamoDB | Account/table throughput, GSI hot partitions, item size, backup/PITR | Upload burst, workflow writes, rights checks, collection growth. |
| EventBridge and SQS | Event delivery, target invocations, in-flight messages, retention | S3 event bursts and acceptable DLQ recovery window. |
| KMS | Cryptographic request rate and grants/key-policy limits | S3, DynamoDB, SQS, Bedrock output, and concurrent uploads. |
| Cognito | Token endpoint and client-credentials rate | Client reconnect and token refresh bursts. |
| AgentCore Gateway | Gateway, target, request, and authentication limits | Expected MCP sessions and catalog/control invocation peak. |

The enrichment Map concurrency is not a quota bypass. Set it below the smallest
verified constraint among Bedrock model concurrency, Lambda reserved concurrency,
MediaConvert throughput, Step Functions, and downstream storage capacity.

## 8. CDK and runtime compatibility

At generation time, discover the current CDK v2 release rather than preserving a
stale literal:

```bash
npm view aws-cdk-lib version
npm view @aws-cdk/aws-bedrock-agentcore-alpha version
npm view constructs version
```

Pin the generated `aws-cdk-lib` version. If an AgentCore alpha package is used,
select the alpha release documented as compatible with that CDK release and validate
with `npm install` plus `cdk synth`; do not assume unrelated latest tags are
lockstep-compatible.

Generate Python Lambda functions with Python 3.13 on ARM64 unless the user has a
verified incompatible native dependency:

```ts
runtime: lambda.Runtime.PYTHON_3_13,
architecture: lambda.Architecture.ARM_64,
```

Use ARM64-compatible wheels and a reproducible build image in CI. Keep deployment
artifacts small with a `.dockerignore` or equivalent package exclusion list. The
runtime region must support Python 3.13 and every selected AWS service before a
stack is proposed.

## 9. Generation-time evidence to retain

Before the Design gate, place the following evidence in the generated configuration
or deployment record:

- selected region and all regional-availability checks;
- embedding and understanding model IDs, dimensions, limits, and access result;
- MediaConvert queue plan and estimated proxy profile;
- AOSS private/public endpoint decision, replica setting, and measured sizing plan;
- service-quota values, requested increases, and concurrency caps;
- pricing-calculator inputs and estimate date; and
- OAuth secret retrieval design proving that no secret appears in source, CDK
  outputs, plugin metadata, or client configuration.

This record prevents a later implementation phase from silently reintroducing a
stale model, a hardcoded dimension, an unsupported region, or an unpriced baseline
service.
