# Eval — Enterprise Training Scenario

## Input Prompt
```text
Build an enterprise training-video archive for internal courses, certification recordings, and compliance briefings.
Retention is strict: retain originals according to policy, expire analysis on schedule, and require an explicit archive
confirmation for cold storage. Amazon Quick is the primary client, and the organization uses an enterprise IdP.
Users must search lessons in natural language, see timestamped evidence, and render a derivative training highlight.
Downloads of restricted material must be blocked. Discover the video-capable Bedrock models available in our selected
region before recommending models or implementation details.
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Treat enterprise training, strict retention, Amazon Quick, enterprise IdP, evidence-backed search, derivative rendering, and restricted downloads as answered inputs.
- [ ] Ask for the selected AWS region before claiming model or service availability.
- [ ] Ask whether the archive is a single-tenant pilot or requires multi-tenant isolation from its first deployment.
- [ ] Ask for rights classes, course-owner roles, learner roles, preview/download/render permissions, and policy exception handling.
- [ ] Ask for raw, proxy, analysis, derivative, noncurrent-version, legal-hold, and archive retention periods.
- [ ] Ask whether retention policies differ among certification recordings, compliance briefings, and general internal courses.
- [ ] Ask for proxy resolution, codec, bitrate, source-relative chunk size, maximum chunks, and maximum source duration.
- [ ] Ask whether the five-chunk default is acceptable or which configured maximum duration should be enforced.
- [ ] Ask which Amazon Quick surface is used, whether Web service-to-service OAuth is available, and whether the Quick Desktop caveat applies.
- [ ] Ask whether additional clients such as ChatGPT/Codex, Kiro, or Claude are required despite Quick being the primary client.
- [ ] Ask how the enterprise IdP maps users and groups to application authorization without placing identity secrets in source control.
- [ ] Run `aws bedrock list-foundation-models --region <region>` or an AWS Knowledge MCP equivalent as an explicit discovery action.
- [ ] Filter the discovered catalog for video embedding models with `VIDEO` input and `EMBEDDING` output modalities.
- [ ] Filter the catalog independently for video-understanding models with `VIDEO` input and `TEXT` output modalities.
- [ ] Present only the filtered, currently available video-capable choices and let the user select an embedding and an understanding model.
- [ ] Do not state a fixed model list, fixed dimension, or named model family as a permanent compatibility guarantee.
- [ ] Read embedding dimension from the selected model card or a probe and record it alongside model identifiers.
- [ ] Capture region, tenancy, IdP/auth approach, rights, retention, proxy, selected models, dimension, and clients in `config/media-archive.yaml`.
- [ ] Summarize the discovery decisions and require GATE 1 approval before design generation.

### Phase 2 (Design -- GATE 2)
- [ ] Present a decision-tree design table with columns `Decision point`, `Input or condition`, `Selected branch`, and `Generated consequence`.
- [ ] Include an enterprise-identity row that maps the IdP choice to authorization without serializing an OAuth client secret into client configuration.
- [ ] Include a tenant row that selects the pilot server-side resolution branch or the multi-tenant Gateway interceptor, Lambda revalidation, prefixes, KMS, and collection branch.
- [ ] Include a retention row that maps strict policy values to raw, analysis, version, derivative, and archive lifecycle rules.
- [ ] Include an archive row that makes `confirm_archive=true` mandatory and delegates physical transition to S3 lifecycle.
- [ ] Include a model row that ties both selected model IDs to runtime capability discovery evidence.
- [ ] Include an embedding row that writes `embedding_dimension` to configuration and drives vector mapping plus the extraction guard.
- [ ] Include a proxy row that makes ≤55-minute analysis chunks and the maximum chunk count configuration rather than implicit limits.
- [ ] Include an access row that requires server-side rights checks for preview and download even when Amazon Quick hides an action.
- [ ] Include a client row that maps Amazon Quick Web service-to-service OAuth and documents the Quick Desktop limitation.
- [ ] Include an optional-client row that describes generated ChatGPT/Codex, Kiro, and Claude configurations only if requested.
- [ ] State the embedding migration process: reindex, dual-write, and alias cutover; never mix embeddings from different models in one index.
- [ ] Provide a sanitized architecture diagram from ingest through analysis proxies, enrichment, search, Gateway, bridge, enterprise IdP, and Quick.
- [ ] Explain cost and retention trade-offs without substituting a stale static model catalog for discovery.
- [ ] Stop at GATE 2 and require approval of the decision table and architecture diagram.

### Phase 3 (Generated Files)
- [ ] Generate `<project-root>/config/media-archive.yaml` with every Discovery answer, selected models, and `embedding_dimension`.
- [ ] Generate `<project-root>/infra/bin/app.ts`.
- [ ] Generate `<project-root>/infra/lib/media-archive-stack.ts`.
- [ ] Generate `<project-root>/cdk.json`.
- [ ] Generate `<project-root>/package.json`.
- [ ] Generate `<project-root>/tsconfig.json`.
- [ ] Generate `<project-root>/lambdas/common.py`.
- [ ] Generate `<project-root>/lambdas/dispatch/index.py`.
- [ ] Generate `<project-root>/lambdas/workflow/index.py`.
- [ ] Generate `<project-root>/lambdas/catalog/index.py`.
- [ ] Generate `<project-root>/lambdas/catalog/schema.json`.
- [ ] Generate `<project-root>/lambdas/control/index.py`.
- [ ] Generate `<project-root>/lambdas/control/schema.json`.
- [ ] Generate `<project-root>/local_bridge/server.py`.
- [ ] Generate `<project-root>/local_bridge/remote_client.py`.
- [ ] Generate `<project-root>/local_bridge/ui/media-results.html` with `ui://media-archive/media-results.v1.html`.
- [ ] Generate `<project-root>/clients/codex-plugin/.codex-plugin/plugin.json`.
- [ ] Generate `<project-root>/clients/codex-plugin/.mcp.json`.
- [ ] Generate `<project-root>/clients/codex-plugin/skills/media-archive/SKILL.md`.
- [ ] Generate `<project-root>/clients/kiro/.kiro/settings/mcp.json`.
- [ ] Generate `<project-root>/clients/kiro/.kiro/skills/media-archive-operations/SKILL.md`.
- [ ] Generate `<project-root>/clients/claude/.mcp.json` and installation guidance for `~/.claude/skills/media-archive-operations/SKILL.md`.
- [ ] Generate `<project-root>/clients/quick/README.md` for Quick Web service-to-service OAuth and the Quick Desktop caveat.
- [ ] Generate `<project-root>/scripts/check-prerequisites.sh`.
- [ ] Generate `<project-root>/scripts/deploy.sh`.
- [ ] Generate `<project-root>/scripts/destroy.sh`.
- [ ] Generate `<project-root>/scripts/verify.py`.
- [ ] Generate `<project-root>/evaluation/golden.json`.
- [ ] Generate `<project-root>/evaluation/generate_fixtures.py`.
- [ ] Generate `<project-root>/evaluation/run_eval.py`.
- [ ] Generate `<project-root>/evaluation/live_test.py`.
- [ ] Generate `<project-root>/tests/` with unit and integration tests for retention, rights, evidence, and tool contracts.
- [ ] Define exactly 10 catalog schema tools: `list_assets`, `get_asset`, `search_assets`, `analyze_asset`, `get_job_status`, `get_preview_url`, `get_download_url`, `get_ontology`, `list_collections`, and `get_collection`.
- [ ] Define exactly 6 control schema tools: `create_upload_session`, `complete_upload`, `request_enrichment`, `render_highlight`, `request_archive`, and `create_collection`.
- [ ] Use `media-catalog` for read tools and `media-commands` for write tools; unique Gateway prefixes may be stripped only by the local bridge.
- [ ] Include `list_collections`, `get_collection`, and `create_collection` in the generated Codex plugin `.mcp.json` allowlist.
- [ ] Include only `upload_local_video`, `render_media_results`, and `resolve_media_playback` as local bridge additions.
- [ ] Use `<account-id>`, `<region>`, `<gateway-url>`, and `<user-pool-id>` in documentation and configuration examples.
- [ ] Run grep checks over all generated source, client configurations, scripts, documentation, evaluation fixtures, and tests for credentials, account IDs, OAuth secrets, and named AWS profiles.
- [ ] Fail the scenario if any `.mcp.json` carries a secret, any repository file carries a real account identifier, or any file hardcodes a named profile.

### Phase 4 (Validate -- GATE 3)
- [ ] Run `cdk synth` from `<project-root>/infra` and require a successful synthesized template.
- [ ] Run `python scripts/verify.py` from `<project-root>` and require all structural, schema, syntax, and sanitization checks to pass.
- [ ] Run `pytest` from `<project-root>` and require all generated tests to pass.
- [ ] Verify that the selected `embedding_dimension` config value is the sole source for vector mapping and embedding extraction validation.
- [ ] Verify 10 catalog and 6 control schemas, unique tool names, and object-shaped input schemas.
- [ ] Verify all three collection tools remain present in the plugin allowlist.
- [ ] Verify retention, archive confirmation, rights recheck, and IdP boundary tests are part of the test suite.
- [ ] Verify the versioned UI resource remains reachable and a pre-existing published alias is preserved when compatibility requires it.
- [ ] Verify grep checks find no credentials, OAuth secrets, fixed account IDs, endpoint values, local source paths, or named profile values.
- [ ] Enforce GATE 3: no bootstrap or deployment occurs after any synth, verifier, or pytest failure.

### Phase 5 (Deploy)
- [ ] Bootstrap the selected environment before deployment and retain no bootstrap credentials in generated artifacts.
- [ ] Deploy only after GATE 3 is green and collect stack outputs into a sanitized outputs file.
- [ ] Write the Amazon Quick client configuration from deployment outputs, using the selected enterprise IdP approach without copying secrets into a README or configuration file.
- [ ] Write optional client configurations only for clients approved during Discovery, derived from outputs rather than fixed values.
- [ ] Generate three short synthetic training fixtures and run `python evaluation/run_eval.py` against the deployed service.
- [ ] Require all three synthetic fixture queries to return their expected asset with source evidence before a live smoke test.
- [ ] Run the live smoke test with a training query and require `search_assets` to return evidence and a confidence band.
- [ ] Require low-confidence results to state no confident match rather than claim a compliance or training conclusion.
- [ ] Attempt a download of a restricted training asset and require a server-side rejection.
- [ ] Render an approved training highlight and require a new object under `derivatives/`.
- [ ] Capture the original source ETag before and after render and require it to remain unchanged.
- [ ] Record retention and archive confirmation behavior in deploy evidence without persisting signed URLs, identity tokens, or credentials.

## Code Quality Checks
- [ ] The stack reads all selected model IDs, dimensions, retention values, proxy settings, and tenancy settings from generated configuration.
- [ ] Enterprise IdP integration conveys authorization claims without embedding OAuth secrets in source, client files, or test fixtures.
- [ ] Every preview and download invocation reaches a server-side rights check; client-side Quick behavior is not treated as a policy boundary.
- [ ] Index creation and extraction validation consume the configured embedding dimension and fail loudly on mismatch.
- [ ] Proxies and analysis results are segregated from originals, and all edits are stored as derivatives.
- [ ] The workflow is at-least-once safe using ETag/version identity, conditional writes, and tolerated duplicate execution.
- [ ] The archive operation only tags a raw object; lifecycle policies own the cold-storage transition.
- [ ] MCP tool descriptions remain concrete enough for semantic selection while preserving the fixed 16-tool contract.
- [ ] Client configurations use stack outputs and runtime credential retrieval, not environment snapshots or checked-in secrets.
- [ ] All user-facing artifacts are sanitized: no real endpoints, account values, pool values, local paths, or profile names.

## Evidence & Safety Verification
1. [ ] Ingest, proxy, enrichment, preview, and rendering tests prove originals are never modified or overwritten; every edit creates a `derivatives/` object.
2. [ ] Every clip result includes `asset_id`, `start_sec`, `end_sec`, modality, score, confidence band `high`/`medium`/`low`, and `source_id`; low confidence returns no confident match.
3. [ ] Pegasus descriptions and suggested ontology concepts are stored as advisory metadata and cannot become approved enterprise vocabulary without a separate approval process.
4. [ ] Rights tests prove server-side rechecks for each preview/download and signed URL lifetimes of 60–300 seconds for preview, 60–900 seconds for download, and one hour for upload.
5. [ ] `request_archive` rejects any call lacking `confirm_archive=true`; a successful call adds a tag only and S3 lifecycle performs movement.
6. [ ] The pilot resolves tenant server-side; a multi-tenant decision requires Gateway interceptor, Lambda revalidation, per-tenant S3 prefixes, KMS, and AOSS collection isolation.
7. [ ] Repeated ingest tests prove ETag/version workflow identity, conditional DynamoDB writes, and tolerated `ExecutionAlreadyExists` outcomes.
8. [ ] Repository scans prove no OAuth client secret exists in any file; the bridge retrieves credentials only at runtime from Secrets Manager or the Cognito API through the configured credential source.

## Failure Modes
- [ ] FAIL if index mapping or embedding extraction silently hardcodes `512` instead of reading `embedding_dimension` from `config/media-archive.yaml`.
- [ ] FAIL if the generated response asserts video models without `list-foundation-models` or AWS Knowledge MCP discovery.
- [ ] FAIL if `.mcp.json` includes an OAuth client secret, access key, real account value, or named AWS profile.
- [ ] FAIL if `get_asset` returns a raw storage key, signed URL, identity data, or unauthorized rendition metadata.
- [ ] FAIL if `request_archive` accepts archive requests without `confirm_archive=true`.
- [ ] FAIL if strict retention is described but no lifecycle configuration or archive-tag behavior is generated.
- [ ] FAIL if a restricted training asset receives a download URL because the Quick client rendered a download control.
- [ ] FAIL if the plugin allowlist omits `list_collections`, `get_collection`, or `create_collection`.
- [ ] FAIL if tool schemas do not contain exactly 10 catalog tools and 6 control tools.
- [ ] FAIL if a search hit lacks source-time evidence or turns a low-confidence result into a compliance assertion.
- [ ] FAIL if rendering mutates an original source, changes its ETag, or writes outside `derivatives/`.
- [ ] FAIL if deployment proceeds while `cdk synth`, `python scripts/verify.py`, or `pytest` is failing.
