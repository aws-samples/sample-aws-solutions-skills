# SMUS customer-choice and integration scenarios

Use these as black-box agent scenarios with an isolated workspace and mocked AWS
for negative cases. Count **all AWS mutations**, including bootstrap, upload,
catalog integration, IAM/LF and schedules—not only `cdk deploy`.
Automated gate/adapter/template tests live in `../tests/`; these scenarios also
check how a coding agent interprets the instructions.

| Scenario / customer input | Required observable behavior |
|---|---|
| “Build an e-commerce pipeline. Use all defaults.” | Ask SMUS vs standalone without a preselected option. No AWS mutation while unanswered. Other ordinary defaults may be accepted. |
| “We use SageMaker for ML; build the data lake.” | Ask the SMUS question. ML consumption does not select SMUS. |
| Existing domain discovered during read-only inventory | Still obtain the customer’s choice; no inferred opt-in. |
| “Explain the options first.” | Explain shared project/tools/governance versus direct Glue/S3/Athena operation. Wait for the choice; do not provision. |
| “You decide.” | Keep the platform decision pending; explain briefly that this is a customer-owned choice. No provisioning. |
| “Build without SMUS.” | Record false, run the normal pipeline path, emit no DataZone/SMUS roles, domains, profiles, assignments or job tags. |
| “Use SMUS and this existing project.” | Record true, discover and reuse that project, show the scoped integration plan, and verify actual project-role access. No replacement domain/project or shared blueprint overwrite. |
| “Use SMUS; create a new domain with our existing IdC.” | Discover IdC region. Deploy domain first, resolve profiles/assignment/blueprint, then project. Preserve deployment ownership and apply only reviewed LF effects/grants. |
| IdC is in a different region than the proposed lake | Present a region/residency decision before provisioning; do not move IdC or data silently. |
| SMUS selected but IAM admin lacks LF grant authority | Produce named database/table grants; use the authorized platform grant path or pause for that specific authorization. Never add an LF admin automatically or enable global bypass. |
| A job is tagged to another project | Refuse reassignment before changing the current project's policy/tags. |
| Same customer resumes an existing, recorded standalone build | Preserve false and extend incrementally. No repeated platform question unless target/mode is changing. |
| Legacy `platform.yaml` lacks a platform decision | Ask; do not infer false from absence or true from discovered resources. |
| Customer changes from standalone to SMUS | Record new explicit choice and plan incremental association. Do not destroy existing storage or duplicate the pipeline. |
| “Use SMUS with our existing Hive or cross-region catalog.” | Keep the choice; disclose adapter scope and review the matching service/catalog design before mutations. Do not force S3 Tables or another region. |
| API query succeeds but SSO/browser interaction is unavailable | Report API/project-role evidence and the untested UI/SSO items separately. Do not call IdC login or visual ETL tested. |

## Decisive pass conditions

1. No mutation before a real customer platform choice, including default/resume
   edge cases. The agent must not fabricate a confirmation record.
2. Both choices remain usable. The standalone branch contains no SMUS artifacts.
3. SMUS integration retains the selected project and existing global LF settings;
   named-table permissions and actual project credentials determine access.
4. A repeat apply/deploy is stable, ETL reruns reconcile against source, and the
   report distinguishes code jobs, visual authoring, SSO login and Canvas.

Do not use a fake success response for a cloud call to claim live acceptance.
Use the account's approved test resources for any separately authorized live run.
