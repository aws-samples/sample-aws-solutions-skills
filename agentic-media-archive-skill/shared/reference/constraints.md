# Constraints and implementation traps
Treat every item in this file as a generation-time constraint for the 16-tool
`media-archive` contract. The authoritative state is DynamoDB; OpenSearch is a
search projection; all originals stay versioned under `raw/`; and all edits write
only under `derivatives/`. Each generated handler must emit source-relative evidence
for a clip claim: `asset_id`, `start_sec`, `end_sec`, modality, score, confidence,
and `source_id`.
## 1. KMS policy must not create an EventBridge/SQS dependency cycle
**Symptom:** `cdk deploy` reports a circular dependency between the KMS key, SQS
DLQ, EventBridge rule, or target permission; alternatively, DLQ delivery fails with
KMS access denied after deployment.
**Root cause:** A key policy that references one specific generated rule ARN can
create a dependency loop. An encrypted DLQ also needs the relevant service principal
to use the key.
**Fix:** Scope the EventBridge condition to this account's `rule/*` ARN instead of
the individual rule resource, and add an explicit SQS service statement for the
queue's encryption path.
```ts
const eventRuleArn = Stack.of(this).formatArn({ service: "events", resource: "rule/*" });
key.addToResourcePolicy(new iam.PolicyStatement({
  principals: [new iam.ServicePrincipal("events.amazonaws.com")],
  actions: ["kms:Decrypt", "kms:GenerateDataKey"], resources: ["*"],
  conditions: {
    StringEquals: { "aws:SourceAccount": this.account },
    ArnLike: { "aws:SourceArn": eventRuleArn },
  },
}));
```
**Source layer:** CDK.
## 2. DynamoDB values need a boundary serializer
**Symptom:** Lambda returns `TypeError: Object of type Decimal is not JSON
serializable`, or ontology/source sets appear in a random order from one invocation
to the next.
**Root cause:** DynamoDB resource reads deserialize numbers as `Decimal` and set
attributes as Python `set` or `frozenset`. Native JSON cannot encode either safely.
**Fix:** Convert floats to `Decimal(str(value))` before writes, and recursively
convert reads to JSON-safe values. Sort sets deterministically so output and evals
are stable.
```python
def json_safe(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((json_safe(v) for v in value), key=str)
    return value
```
**Source layer:** Lambda.
## 3. AOSS requests require `aoss` SigV4, payload hashing, and frozen credentials
**Symptom:** Vector indexing or search returns 401/403, signature mismatch, or an
intermittent expiry-related authentication error.
**Root cause:** OpenSearch Serverless signs for service name `aoss`, not `es`.
Omitting `X-Amz-Content-Sha256`, signing mutable credentials, or changing a signed
body/header invalidates the request.
**Fix:** Hash the exact bytes, set the content hash header, and sign with frozen
credentials immediately before sending. Do not reuse a long-lived signed request.
```python
payload = json.dumps(body, separators=(",", ":")).encode()
request = AWSRequest(method=method, url=url, data=payload, headers={
    "Content-Type": "application/json",
    "X-Amz-Content-Sha256": hashlib.sha256(payload).hexdigest(),
})
credentials = boto3.Session().get_credentials()
if credentials is None:
    raise RuntimeError("AWS credentials are unavailable")
SigV4Auth(credentials.get_frozen_credentials(), "aoss", region).add_auth(request)
response = http.request(method, request.prepare().url, body=payload,
                        headers=dict(request.prepare().headers))
```
**Source layer:** Lambda.
## 4. OpenSearch Serverless owns the document `_id`
**Symptom:** Index requests fail, retried documents cannot be correlated, or a
future AOSS write path changes a client-chosen `_id` assumption.
**Root cause:** The Serverless document API can assign IDs itself. Treating `_id` as
a portable deterministic identifier couples the implementation to a write behavior
that is not guaranteed by the archive contract.
**Fix:** POST each vector document without forcing `_id`, preserve the returned
server ID as `source_id`, and store a deterministic `document_key` field containing
asset, model, modality, scope, and source range. Use `document_key` for audit,
dedupe, and reindex bookkeeping.
**Source layer:** Lambda.
## 5. Never mix embeddings from different model versions
**Symptom:** Search relevance drifts after a model change, score bands stop being
meaningful, or vector mapping rejects a new dimension.
**Root cause:** A vector's semantic space is model-specific even when the numeric
dimension happens to match. A mixed index makes nearest-neighbor scores
non-comparable.
**Fix:** Create a versioned index per embedding model, reindex historical media,
dual-write new media while validating recall, then atomically cut the search alias
to the new index. Retain the old index until rollback and retention requirements
are satisfied. Record `model_id` on every vector and never query a mixed index.
**Source layer:** CDK + Lambda.
## 6. Embedding dimension comes from configuration and fails closed
**Symptom:** Index creation uses 512 while the selected model emits a different
length, producing failed writes or silently corrupt retrieval assumptions.
**Root cause:** A literal dimension in the mapping or extraction guard outlives the
model selection decision.
**Fix:** Persist the verified dimension in `config/media-archive.yaml`, inject it
into the index mapping, and reject any query or clip vector whose length differs.
```python
expected = int(config["models"]["embedding_dimension"])
if expected <= 0:
    raise ValueError("embedding_dimension must be positive")
if not isinstance(vector, list) or len(vector) != expected:
    raise RuntimeError(
        f"embedding dimension mismatch: expected {expected}, got {len(vector)}"
    )
# WHY: an index with a wrong dimension is not safely recoverable in place.
```
**Source layer:** CDK + Lambda.
## 7. Marengo async output has variable wrapper shapes
**Symptom:** A completed async invocation writes output to S3, but indexing reports
that no embeddings were found despite valid data.
**Root cause:** Video output can be nested under `data.embedding`, list-wrapped, or
inside an async output envelope. A single fixed JSON path is brittle.
**Fix:** Recursively collect objects that contain an embedding list, then apply the
configured dimension guard and required source metadata checks before indexing.
```python
def extract_embeddings(value):
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("data"), dict) and isinstance(value["data"].get("embedding"), list):
            return [value["data"]]
        if isinstance(value.get("embedding"), list):
            return [value]
        for child in value.values():
            found.extend(extract_embeddings(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(extract_embeddings(child))
    return found
```
**Source layer:** Lambda.
## 8. Pegasus inputs must be bounded and `finishReason == length` fails closed
**Symptom:** Whole-asset or segment analysis fails unpredictably, exceeds model
limits, or returns truncated JSON that appears superficially valid.
**Root cause:** Pegasus input contracts are bounded to under 1 h and under 2 GB
(verify current values). A model response with `finishReason` of `length` is
incomplete, not an approved partial result.
**Fix:** Build up to 55-minute source-relative CBR proxy chunks, head each proxy
object before invocation, reject too-large files, and fail the workflow on token
truncation. Make duration, byte limit, chunk count, and segment duration
configuration values with safe defaults.
```python
if segment_seconds > 55 * 60 or proxy_size >= 2_000_000_000:
    raise ValueError("proxy exceeds the configured Pegasus input envelope")
payload = read_json_body(response)
if payload.get("finishReason") == "length":
    raise RuntimeError("Pegasus output reached its token limit")
analysis = json.loads(payload["message"])
```
**Source layer:** Lambda + Step Functions.
## 9. Map concurrency must stay below the verified model quota
**Symptom:** The enrichment Map creates repeated throttling, long retries, and
unbounded cost even though each Lambda function appears healthy.
**Root cause:** Step Functions Map concurrency is independent of Bedrock model
concurrency, account invocation quotas, Lambda reserved concurrency, and the
MediaConvert pipeline.
**Fix:** Set the Map's `maxConcurrency` from a config value whose maximum is the
smallest verified downstream limit minus a retry/headroom reserve. Start at 2 for a
pilot, load test, then request quota increases before raising it. Use exponential
backoff for throttling and expose the selected cap in the design record.
**Source layer:** CDK + Step Functions + Lambda.
## 10. MediaConvert proxy jobs need one clip, zero-based timecode, and explicit selectors
**Symptom:** Proxy chunks start at the wrong time, omit audio, produce variable
sizes, or a multi-range job is incorrectly treated as independently retryable
segments.
**Root cause:** Source timecode, implicit streams, and multiple `InputClippings`
produce ambiguous behavior for source-relative analysis chunks.
**Fix:** Submit one MediaConvert job per proxy clipping range. Set both input and
job timecode to `ZEROBASED`, declare `AudioSelectors` and `VideoSelector`, and use
H.264/AAC CBR for analysis proxies. Reserve QVBR for derived highlight output.
```python
input_settings = {
    "TimecodeSource": "ZEROBASED",
    "AudioSelectors": {"Default Audio": {"DefaultSelection": "DEFAULT"}},
    "VideoSelector": {},
    "InputClippings": [{"StartTimecode": start, "EndTimecode": end}],
}
video = {"Codec": "H_264", "H264Settings": {
    "RateControlMode": "CBR", "Bitrate": 4_000_000,
    "FramerateControl": "INITIALIZE_FROM_SOURCE",
}}
```
**Source layer:** Lambda.
## 11. Highlight ranges must be valid before 30-fps rendering
**Symptom:** A highlight job includes duplicated frames, reversed clips, an
out-of-range segment, or a timecode that shifts at frame boundaries.
**Root cause:** User-provided ranges may be unsorted, overlap, or use fractional
seconds. MediaConvert receives frame timecodes rather than source floats.
**Fix:** Validate `0 <= start_sec < end_sec <= duration`, require sorted,
non-overlapping clips, then round to the configured 30-fps timecode only after
validation. Keep the original seconds in the render spec hash for idempotency.
```python
def to_timecode(seconds, fps=30):
    total_frames = max(0, round(seconds * fps))
    frames = total_frames % fps
    total_seconds = total_frames // fps
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"
```
**Source layer:** Lambda.
## 12. Ingest must tolerate at-least-once delivery
**Symptom:** An S3 retry starts duplicate workflows, duplicate model invocations,
or conflicting jobs for the same object generation.
**Root cause:** S3 events and EventBridge delivery are at least once. A user may
also call `complete_upload` after automatic dispatch has already started.
**Fix:** Derive one identity from tenant, asset, ETag, version ID, and generation;
claim idempotency records with conditional writes; and treat Step Functions
`ExecutionAlreadyExists` as an already-running success.
```python
job_id, execution_name = ingest_identity(asset_id, etag=etag, version_id=version_id)
try:
    table.put_item(Item=record, ConditionExpression="attribute_not_exists(PK)")
    sfn.start_execution(stateMachineArn=arn, name=execution_name, input=payload)
except ClientError as error:
    code = error.response.get("Error", {}).get("Code")
    if code not in {"ConditionalCheckFailedException", "ExecutionAlreadyExists"}:
        raise
```
**Source layer:** Lambda + Step Functions.
## 13. Low vector scores are not source evidence
**Symptom:** A search returns plausible-looking but unrelated media around scores
such as 0.71–0.73, and the client presents it as a match.
**Root cause:** k-NN retrieval commonly returns nearest neighbors even when the
archive has no relevant footage; score scales are model, corpus, and index specific.
**Fix:** Calibrate confidence bands on an eval corpus. A starting policy for the
verified embedding setup is high at `>= 0.80`, medium at `>= 0.75`, and low below
that; revise only after measurement. Low-only results must say "no confident match"
and still include raw evidence only for debugging, never as a claim.
**Source layer:** Lambda + bridge.
## 14. Resolve the tenant on the server, never from caller input
**Symptom:** A caller sends `tenant_id` in an MCP argument and reads or writes a
different tenant's asset, vector, or object prefix.
**Root cause:** Tool arguments are untrusted application data. Filtering after a
caller-controlled tenant is selected cannot establish an authorization boundary.
**Fix:** Resolve the single-tenant pilot identifier from trusted Lambda
configuration. For multi-tenant deployments, Gateway interceptor identity maps to
a tenant, Lambda revalidates it, and S3 prefix, DynamoDB PK, AOSS filter, and KMS
policy all enforce the same tenant. Do not add `tenant_id` to any of the 16 tool
schemas.
**Source layer:** Gateway + Lambda + CDK.
## 15. Re-check rights immediately before every URL issue
**Symptom:** A preview or download URL remains available after an asset is marked
restricted, revoked, replaced, or a render job changes.
**Root cause:** Search projections and previously read asset objects become stale.
A presigned URL cannot be revoked once issued.
**Fix:** Perform a strongly consistent DynamoDB read when issuing preview or
download URLs. Require `READY` state, approved rights, matching asset version,
completed job, and expected proxy/derivative key before signing. Never use model
metadata or AOSS `rights_state` as authorization.
**Source layer:** Lambda.
## 16. Signed URL TTLs have fixed upper and lower bounds
**Symptom:** A generated URL lasts too long, expires during normal UI playback, or
is accidentally reused as a bearer credential in a log or chat transcript.
**Root cause:** Letting callers choose arbitrary expiry trades authorization away
for convenience.
**Fix:** Clamp preview URLs to 60–300 seconds, download URLs to 60–900 seconds,
and upload URLs to one hour. Set private/no-store cache controls on preview content
and return URLs only through the intended tool response or UI `_meta` path.
**Source layer:** Lambda + bridge.
## 17. Cognito client secrets never belong in code or CloudFormation exports
**Symptom:** A secret appears in a repository, generated plugin configuration,
CloudFormation output, or copied client setup instructions.
**Root cause:** Cognito client-secret values are sensitive runtime credentials and
CloudFormation/CDK does not safely expose them as ordinary outputs.
**Fix:** Generate a client with a secret, retrieve it at deployment/runtime through
an approved custom-resource or Cognito API flow, store it in Secrets Manager, and
have the local bridge fetch it using the active AWS profile. Output only the secret
ARN or an installation step, never the secret value.
**Source layer:** CDK + bridge.
## 18. Gateway names tools as `target___tool`
**Symptom:** A Lambda receives `media-catalog___search_assets` and rejects it as an
unknown handler, or a bridge invokes a prefixed name not present in its local API.
**Root cause:** AgentCore Gateway prefixes target tool names to avoid collisions.
The 16-tool skill contract names the underlying catalog/control operations.
**Fix:** Strip only the final unambiguous prefix at the Lambda boundary and retain
the canonical name in schemas and client UX. Do not globally split arbitrary tool
strings or permit a caller to choose a target.
```python
def canonical_tool_name(context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    gateway_name = str(custom.get("bedrockAgentCoreToolName", ""))
    return gateway_name.rsplit("___", 1)[-1]
```
**Source layer:** Gateway + Lambda + bridge.
## 19. Audit Gateway IAM because CDK L2 grants can be incomplete
**Symptom:** Gateway deployment succeeds, but runtime returns authorization errors
when invoking a Lambda target or resolving an external OAuth identity.
**Root cause:** An L2 construct can create the Gateway without knowing every target
or secret access path required by the final architecture.
**Fix:** Grant target invocation explicitly and add the narrowest documented
identity-secret permission when that feature is enabled. Inspect synthesized IAM
before deployment; do not mask missing permissions with broad administrator grants.
```ts
catalogFunction.grantInvoke(gateway.role);
controlFunction.grantInvoke(gateway.role);
gateway.role.addToPrincipalPolicy(new iam.PolicyStatement({
  actions: ["secretsmanager:GetSecretValue"],
  resources: [identitySecret.secretArn],
}));
```
**Source layer:** CDK + Gateway.
## 20. State-machine timeout and model polling are different bounds
**Symptom:** A long Marengo job outlives the state machine, or a polling loop waits
forever and incurs transitions after the model cannot reasonably complete.
**Root cause:** A workflow-wide timeout and a provider-specific polling deadline
solve different failure modes.
**Fix:** Make both configurable: default state-machine timeout is 12 hours and
default per-model poll bound is 6 hours. Persist poll start time, stop with a
terminal failure when the model bound is crossed, and allow the outer timeout to
protect other workflow stages. Do not encode either duration as an undocumented
constant.
**Source layer:** CDK + Step Functions + Lambda.
## 21. MCP Apps UI must isolate bearer URLs and preserve published resource URIs
**Symptom:** A signed playback URL is exposed in model-visible text, browser CSP
blocks playback, or existing conversations cannot render a UI after a URI rename.
**Root cause:** UI metadata and model content have different exposure boundaries;
MCP Apps clients may cache an older resource URI.
**Fix:** Return preview/download bearer URLs only in `_meta`, not rendered text.
Set CSP `media-src` and `connect-src` to the approved playback origin. Publish
`ui://media-archive/media-results.v1.html` as the current resource and keep every
previously published resource URI as an alias to the current UI until client-cache
migration is complete.
**Source layer:** bridge + client UI.
## 22. ChatGPT desktop needs a full restart after plugin or UI changes
**Symptom:** Updated plugin metadata, tool allowlist, or MCP Apps resource HTML is
not visible even after local files and service configuration were changed.
**Root cause:** The desktop client can cache plugin discovery and UI resources for
the process lifetime.
**Fix:** After changing a plugin, MCP configuration, or UI resource, fully quit the
ChatGPT desktop application and start it again before verification. Re-run the
16-tool discovery check and a UI render smoke test; do not diagnose the stale client
as a gateway deployment failure first.
**Source layer:** bridge + client UI.
## 23. Quick Desktop cannot refresh client-credentials tokens
**Symptom:** The desktop client initially connects but later loses access after an
OAuth client-credentials token expires.
**Root cause:** The desktop integration does not perform the required refresh flow
for this service-to-service authorization pattern.
**Fix:** Document Quick Web service-to-service OAuth as the supported path, or put
a controlled token broker/bridge in front of the Gateway where policy permits. Do
not distribute a long-lived token, relax URL TTLs, or claim that Quick Desktop will
refresh client-credentials tokens itself.
**Source layer:** bridge + client configuration.
## Quick checklist (before code generation)
- [ ] Model discovery commands and AWS Knowledge MCP checks ran in `<region>`.
- [ ] User selected one available embedding model and one understanding model.
- [ ] Model IDs, verified dimension, invocation modes, limits, and quota evidence
      are recorded in `config/media-archive.yaml`.
- [ ] Originals are versioned and immutable; all render output uses `derivatives/`.
- [ ] KMS policy uses the account-scoped EventBridge `rule/*` ARN and covers SQS.
- [ ] DynamoDB boundary serialization handles `Decimal`, sets, and floats safely.
- [ ] AOSS uses `aoss` SigV4, payload hashing, frozen credentials, server IDs, and
      deterministic `document_key` fields.
- [ ] Vector indexes are model-versioned; migration is reindex → dual-write → alias
      cutover, with no mixed semantic spaces.
- [ ] Proxy chunk, byte, segment-count, Map concurrency, 12-hour workflow timeout,
      and 6-hour model-poll bounds are configurable and quota-backed.
- [ ] Proxy jobs use one clip, zero-based timecode, explicit audio/video selectors,
      and CBR; highlight jobs validate ranges and use 30-fps rounding/QVBR.
- [ ] ETag/version idempotency, conditional writes, and `ExecutionAlreadyExists`
      handling make ingest at-least-once safe.
- [ ] Low-confidence retrieval is rendered as "no confident match", never evidence.
- [ ] Tenant, rights, and URL TTL controls are enforced server-side on every issue.
- [ ] OAuth secrets are retrieved at runtime; no secret or bearer URL is emitted in
      source, stack outputs, tool text, or plugin configuration.
- [ ] Gateway tool prefixing, explicit IAM grants, UI CSP/_meta/URI aliases, and
      desktop-client restart/token limitations are documented and tested.
