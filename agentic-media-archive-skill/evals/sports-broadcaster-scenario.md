# Eval — Sports Broadcaster Scenario

## Input Prompt
```text
4시간짜리 경기 녹화물과 짧은 리플레이를 보관하는 스포츠 중계 미디어 아카이브를 구축해 주세요.
ChatGPT와 Kiro에서 자연어로 플레이와 하이라이트를 검색하고, 선택한 구간을 새 하이라이트 영상으로
렌더링할 수 있어야 합니다. 원본은 절대 편집하지 말고, 라이선스 상태에 따라 다운로드를 제한해 주세요.
단일 테넌트 파일럿부터 시작하되 나중에 다중 테넌트를 고려하고 싶습니다. 운영 리전과 모델은 현재
사용할 수 있는 옵션을 확인한 뒤 제안해 주세요.
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Treat the supplied sport, four-hour source duration, ChatGPT, Kiro, highlight rendering, and single-tenant pilot as answered inputs.
- [ ] Ask for the deployment region before making any availability, pricing, or model claim.
- [ ] Ask whether the tenant boundary remains a single-tenant pilot or must become multi-tenant now.
- [ ] Ask who owns each feed, which rights states are allowed, and which users may preview, download, or render derivatives.
- [ ] Ask for raw, proxy, analysis, derivative, and legal-hold retention periods, including the cold-archive policy.
- [ ] Ask for proxy resolution, codec, bitrate, source-relative chunk length, maximum chunks, and the maximum supported source duration.
- [ ] Confirm that the default five ≤55-minute chunks support a four-hour match, or record a different configured limit.
- [ ] Ask whether ChatGPT/Codex and Kiro are the only clients, plus the authentication method and interactive-user expectations.
- [ ] Ask which client installation targets are required and whether a local bridge is allowed for desktop clients.
- [ ] Run `aws bedrock list-foundation-models --region <region>` or an AWS Knowledge MCP equivalent during discovery.
- [ ] Filter discovery results for embedding candidates with `VIDEO` input and `EMBEDDING` output modalities.
- [ ] Filter discovery results separately for understanding candidates with `VIDEO` input and `TEXT` output modalities.
- [ ] Present the filtered, currently available video-capable models and let the user choose one embedding model and one understanding model.
- [ ] Do not assert a fixed model list, fixed model ID, or fixed model family as the only supported choice.
- [ ] Record the chosen model IDs, their discovery evidence, and the embedding dimension obtained from a model card or probe.
- [ ] Capture every answer in `config/media-archive.yaml`, including region, tenancy, rights, retention, proxy, auth, and clients.
- [ ] Summarize the discovery answers and obtain GATE 1 confirmation before architecture generation.

### Phase 2 (Design -- GATE 2)
- [ ] Present a decision-tree design table with exactly these columns: `Decision point`, `Input or condition`, `Selected branch`, and `Generated consequence`.
- [ ] Include a tenant row that selects the single-tenant pilot boundary or the multi-tenant interceptor, Lambda revalidation, per-tenant prefix, KMS, and collection branch.
- [ ] Include a model row that links both chosen model IDs and their discovered capabilities to generated configuration.
- [ ] Include an embedding row that stores `embedding_dimension` in `config/media-archive.yaml` and uses that value for index mapping and extraction validation.
- [ ] Include a source-duration row that makes the chunk count and source limit configurable rather than silently accepting every duration.
- [ ] Include a proxy row for 1280×720 H.264/AAC analysis proxies and source-relative chunks.
- [ ] Include a rights row that differentiates preview, download, render, and archive permissions.
- [ ] Include a retention row for raw, analysis, derivative, version, and tagged archive lifecycle behavior.
- [ ] Include a client row that maps ChatGPT/Codex and Kiro to their generated client configurations.
- [ ] Include a deployment-shape row that selects the default single stack or the decision-tree multi-stack variant.
- [ ] State the migration rule: embeddings from different models must never share an index; migration is reindex, dual-write, then alias cutover.
- [ ] Show a sanitized architecture diagram from upload and ingest through proxies, enrichment, index, Gateway, bridge, and clients.
- [ ] Explain the cost drivers and model constraints without treating a stale catalog as authoritative.
- [ ] Stop for GATE 2 approval of the design table and architecture diagram.

### Phase 3 (Generated Files)
- [ ] Generate `<project-root>/config/media-archive.yaml` with all Discovery answers, chosen models, and the configured embedding dimension.
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
- [ ] Generate `<project-root>/local_bridge/ui/media-results.html` with resource URI `ui://media-archive/media-results.v1.html`.
- [ ] Generate `<project-root>/clients/codex-plugin/.codex-plugin/plugin.json`.
- [ ] Generate `<project-root>/clients/codex-plugin/.mcp.json`.
- [ ] Generate `<project-root>/clients/codex-plugin/skills/media-archive/SKILL.md`.
- [ ] Generate `<project-root>/clients/kiro/.kiro/settings/mcp.json`.
- [ ] Generate `<project-root>/clients/kiro/.kiro/skills/media-archive-operations/SKILL.md`.
- [ ] Generate `<project-root>/clients/claude/.mcp.json` and installation guidance for `~/.claude/skills/media-archive-operations/SKILL.md`.
- [ ] Generate `<project-root>/clients/quick/README.md` with Web service-to-service OAuth guidance and the Quick Desktop caveat.
- [ ] Generate `<project-root>/scripts/check-prerequisites.sh`.
- [ ] Generate `<project-root>/scripts/deploy.sh`.
- [ ] Generate `<project-root>/scripts/destroy.sh`.
- [ ] Generate `<project-root>/scripts/verify.py`.
- [ ] Generate `<project-root>/evaluation/golden.json`.
- [ ] Generate `<project-root>/evaluation/generate_fixtures.py`.
- [ ] Generate `<project-root>/evaluation/run_eval.py`.
- [ ] Generate `<project-root>/evaluation/live_test.py`.
- [ ] Generate `<project-root>/tests/` with the generated unit and integration test suite.
- [ ] Define exactly 10 catalog schema tools: `list_assets`, `get_asset`, `search_assets`, `analyze_asset`, `get_job_status`, `get_preview_url`, `get_download_url`, `get_ontology`, `list_collections`, and `get_collection`.
- [ ] Define exactly 6 control schema tools: `create_upload_session`, `complete_upload`, `request_enrichment`, `render_highlight`, `request_archive`, and `create_collection`.
- [ ] Expose catalog tools through `media-catalog` and control tools through `media-commands`; bridge behavior strips a unique Gateway prefix only.
- [ ] Include `list_collections`, `get_collection`, and `create_collection` in the Codex plugin `.mcp.json` allowlist.
- [ ] Include local bridge tools `upload_local_video`, `render_media_results`, and `resolve_media_playback` only as local bridge additions.
- [ ] Use placeholders such as `<account-id>`, `<region>`, `<gateway-url>`, and `<user-pool-id>` where configuration examples need values.
- [ ] Run grep checks across generated source, client configuration, scripts, docs, and tests for access keys, 12-digit account IDs, OAuth client secrets, credential assignments, and named AWS profiles.
- [ ] Fail generation if grep finds a secret in any `.mcp.json`, an account ID, or a named profile; no exception is allowed for examples.

### Phase 4 (Validate -- GATE 3)
- [ ] Run `cdk synth` from `<project-root>/infra` and require successful synthesis before deployment.
- [ ] Run `python scripts/verify.py` from `<project-root>` and require structural, schema, syntax, and secret-hygiene checks to pass.
- [ ] Run `pytest` from `<project-root>` and require all generated tests to pass.
- [ ] Verify that index dimension and embedding extraction guard both read `embedding_dimension` from generated configuration.
- [ ] Verify that all 16 schema tool names are unique and every input schema is an object.
- [ ] Verify that the plugin allowlist contains every required collection tool.
- [ ] Verify that the UI resource uses the versioned media-results URI and no legacy URI is removed when one was already published.
- [ ] Verify that source paths, account identifiers, gateway endpoints, pool identifiers, and named profiles are absent from generated files.
- [ ] Stop at GATE 3 on any failed synth, verifier, or test result; do not deploy a red build.

### Phase 5 (Deploy)
- [ ] Run the environment bootstrap required by the selected region and deployment shape before deploying infrastructure.
- [ ] Deploy only after all GATE 3 checks pass and capture stack outputs in a sanitized outputs file.
- [ ] Write ChatGPT/Codex and Kiro client configurations from stack outputs, never by copying fixed endpoint or identity values into templates.
- [ ] Generate three short synthetic fixtures and run `python evaluation/run_eval.py` against the deployed service.
- [ ] Require the three-fixture synthetic evaluation to pass all expected search cases with source evidence.
- [ ] Run a live smoke test after synthetic evaluation passes.
- [ ] Assert that `search_assets` returns source-relative evidence with `asset_id`, `start_sec`, `end_sec`, modality, score, confidence band, and `source_id`.
- [ ] Assert that a low-confidence search reports no confident match rather than inventing a highlight.
- [ ] Assert that a restricted asset download is rejected by server-side rights evaluation.
- [ ] Assert that `render_highlight` creates an object under `derivatives/`.
- [ ] Capture the source object's ETag before and after rendering and require it to be unchanged.
- [ ] Record deploy, synthetic-eval, and smoke-test evidence without storing signed URLs or credentials in the repository.

## Code Quality Checks
- [ ] The generated CDK stack uses configuration, not silent literals, for selected model IDs, embedding dimension, retention, proxy limits, and tenancy.
- [ ] Lambda code validates tenant and rights server-side; UI state is never treated as authorization.
- [ ] Vector mapping uses cosine similarity and the configured dimension; a mismatched embedding fails loudly.
- [ ] Workflow identity is ETag/version-derived, conditional writes are used, and duplicate execution is tolerated.
- [ ] Analysis proxies are separate from originals and use the selected configured chunk policy.
- [ ] Highlight rendering uses source-relative timecodes rounded to 30 fps and writes only derivatives.
- [ ] Tool descriptions are specific enough for semantic tool selection and retain the fixed catalog/control contract.
- [ ] Client configurations are generated from outputs and use runtime credential retrieval rather than repository secrets.
- [ ] The local bridge retains the versioned UI resource alias when compatibility requires it.
- [ ] All customer-facing prose is sanitized and contains no real endpoints, account identifiers, user-pool values, or profile names.

## Evidence & Safety Verification
1. [ ] Upload, enrichment, preview, and render tests prove originals are never modified or overwritten; every edit is a new `derivatives/` object.
2. [ ] Each returned clip claim includes `asset_id`, `start_sec`, `end_sec`, modality, score, `high`/`medium`/`low` confidence, and `source_id`; low confidence returns no confident match.
3. [ ] Tests show that Pegasus descriptions and suggested ontology concepts are advisory metadata, never automatically approved vocabulary.
4. [ ] Preview and download tests show server-side rights rechecks and enforce signed-URL lifetimes of 60–300 seconds, 60–900 seconds, and one hour for upload respectively.
5. [ ] `request_archive` fails unless `confirm_archive=true`; a successful request only tags the raw object and lifecycle performs the transition.
6. [ ] The selected tenancy test proves server-side tenant resolution for the pilot; a multi-tenant selection proves Gateway interceptor, Lambda revalidation, tenant prefixes, KMS, and collection isolation.
7. [ ] Duplicate ingest tests prove ETag/version workflow identity, conditional DynamoDB writes, and tolerated `ExecutionAlreadyExists` behavior.
8. [ ] Repository scans prove no OAuth client secret is committed; the bridge obtains client credentials only at runtime from Secrets Manager or the Cognito API through the configured credential source.

## Failure Modes
- [ ] FAIL if `512` is hardcoded in index mapping or extraction logic without being read from `config/media-archive.yaml`.
- [ ] FAIL if a fixed model list is asserted without running `list-foundation-models` or AWS Knowledge MCP discovery.
- [ ] FAIL if `.mcp.json` contains an OAuth client secret, access key, fixed account identifier, or named profile.
- [ ] FAIL if `get_asset` leaks a raw storage key, signed URL, credential value, or unauthorized rendition metadata.
- [ ] FAIL if `request_archive` lacks an explicit `confirm_archive=true` gate.
- [ ] FAIL if a four-hour source silently exceeds the configured proxy/chunk maximum instead of producing a clear validation error.
- [ ] FAIL if plugin allowlists omit any collection tool.
- [ ] FAIL if generated tool schemas contain fewer or more than 10 catalog tools and 6 control tools.
- [ ] FAIL if search results omit source-time evidence or label a low-confidence hit as a confirmed match.
- [ ] FAIL if rendering alters the original object, changes its ETag, or writes outside `derivatives/`.
- [ ] FAIL if preview or download authorization is based only on a client-provided rights flag.
- [ ] FAIL if deployment proceeds after `cdk synth`, `python scripts/verify.py`, or `pytest` fails.
