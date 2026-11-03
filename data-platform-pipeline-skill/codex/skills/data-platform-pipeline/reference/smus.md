# Optional SageMaker Unified Studio integration

Read this **only after the customer explicitly chooses SMUS**. The standalone
pipeline remains a supported choice; SMUS is never inferred from an ML use case,
an existing domain, “use the defaults,” silence, or available account permissions.

## 1. Scope and customer decision

SMUS provides a shared project, catalog access, SQL tools, and job execution UI.
Glue executes the ETL, S3 Tables stores the Iceberg data, and Athena runs SQL.
Use the same pipeline resources; do not create a second copy just for the console.

The included adapter supports **same-account, same-region Glue code jobs and
S3 Tables**, with either an existing SMUS project or a new IdC-backed domain and
SQL/ETL project. Existing IAM-authenticated V2 domains can use the project adapter.
Hive, cross-account, split-region, and other blueprints require a separately
reviewed design; do not silently change storage pattern or move data to fit this
adapter. Existing split-region/residency decisions still apply.

Visual ETL graph round-tripping, Canvas forecasting, model training, endpoints,
catalog publication/subscription, and actual SSO sign-in are separate acceptance
items. A code job appearing under Data processing jobs does not prove those.

Record the actual customer answer in `.platform-decision.json` before any
provisioning. For example, **only after the customer has said this**:

```json
{
  "account_id": "111122223333",
  "region": "us-east-1",
  "prefix": "acme",
  "use_smus": true,
  "customer_decision": {
    "source": "customer",
    "confirmed": true,
    "answer": "Use SageMaker Unified Studio for this pipeline."
  }
}
```

The account above is a documentation example, never a deployment default.
For standalone use `false` and the customer's actual standalone answer.
`scripts/deployment_gate.py` rejects missing/ambiguous choices, string booleans,
wrong account/region/prefix, and a command mode that conflicts with the choice.
It validates a record; it cannot authenticate who wrote it. Never manufacture
customer evidence to make the check pass.

Copy the bundled scripts into the generated project's `scripts/`. Wrap **every
provisioning entrypoint**, including bootstrap, uploads, CDK deploy, IAM/LF
changes, integration enablement and schedules:

The gate uses Python's standard library. The SMUS adapter also requires a current
`boto3` installation; use the generated project's virtual environment if needed.

```bash
python3 scripts/deployment_gate.py \
  --decision .platform-decision.json --account "$ACCOUNT_ID" \
  --region "$AWS_REGION" --prefix "$PREFIX" --mode smus \
  -- npx cdk deploy SmusDomain --require-approval never
```

Use `--mode standalone` for that branch. The same gate applies before SDK
mutations; the SMUS adapter checks it internally. Missing answers permit
read-only discovery and local drafting, but **zero AWS mutations**, even if
the user accepts all other defaults. Preserve the selected mode on resume.

## 2. Preflight: discover rather than copy identifiers

Read `platform.yaml` and `ARCHITECTURE.md`; identify what is owned by this build
and what is shared. Reuse existing projects without replacing their blueprints,
memberships, execution role, or workgroup.

Verify using current AWS documentation and live read-only calls:

- Active STS account, target region and partition; IdC instance/identity store
  **and its region** (`sso-admin list-instances` in relevant regions).
- Existing V2 domains (`datazone list-domains`, `get-domain`), selected project,
  ACTIVE environments and provisioned resources, connections, current members.
- Available Tooling blueprint ID (`list-environment-blueprints` with
  `managed=true`), configuration schema, and regional parameters. Do not copy an
  ID from a previous customer.
- Approved private subnets, endpoint/NAT reachability, available bucket names,
  current CDK/SDK resource support. Do not open public ingress to fix connectivity.
- S3 Tables federated Glue catalog and **catalog-specific LF permissions**, not
  just global `CreateDatabaseDefaultPermissions`. A global
  `IAM_ALLOWED_PRINCIPALS` default does not establish access to this catalog.
- Existing LF settings and authorized grant administrator/deployment role.
  An IAM Administrator role may still lack `GrantPermissions`.

**Preserve governance.** Do not apply the standalone `IAMAllowedPrincipals`
shortcut, clear LF settings, enable `AllowFullTableExternalDataAccess`, grant
`Super`, or add yourself as a data-lake administrator. Prefer the existing
authorized grant path with database DESCRIBE and named-table SELECT/DESCRIBE.
If none is available, finish the scoped plan and ask the customer's platform
owner to authorize that specific change. Do not deploy a knowingly inaccessible
pipeline and call it complete.

AWS-managed Tooling provisioning can itself modify LF administrators/external
access settings. Before creating a project, capture the original settings,
inspect the selected blueprint's planned effects with the platform owner, and
settle authorization. Diff afterwards. Do not blindly overwrite settings with an
old snapshot; preserve concurrent unrelated changes and verify project access
after any agreed restoration. The templates deliberately contain **no**
`AWS::LakeFormation::DataLakeSettings` resource or administrator bootstrap.

If IdC is in a different region, present a region choice and its data-residency
consequences. Never silently relocate IdC or the customer's pipeline.

## 3. Provision only the missing foundation

### Existing domain and project

Skip foundation creation. Use the adapter in §4 to discover the existing execution
role and Athena workgroup. Changing a customer-selected project is not a fallback
for a permissions failure.

### New domain or project

Copy `assets/smus/smus-foundation.ts` into the generated CDK app. It exposes two
stacks to support the necessary discovery between deployments:

1. **SmusDomainStack:** create a V2 domain using the existing IdC instance and
   correctly named `service-role/SageMakerStudioDomainExecutionRolePolicy` /
   `service-role/SageMakerStudioDomainServiceRolePolicy` policies.
2. After the domain is available, resolve or create its user/group profiles using
   `get-user-profile`/`create-user-profile` and `get-group-profile`/
   `create-group-profile`. Resolve the **CloudFormation execution role's group**
   as `deploymentGroupId`; it may differ from the caller's role. Use the intended
   IdC user's ID as `ownerUserId`. Resolve the domain's SSO application ARN and
   create the intended user/group assignment with SSO Admin if missing. Do not
   invent users, credentials, domain-owner grants, or application ARNs.
3. Discover the Tooling blueprint ID and approve private subnet inputs.
   **SmusProjectStack** then creates its configuration, profile, use-blueprint
   policy grant, and project. It requires the domain ID/ARN/root-unit ID from
   phase 1. For a new domain, set `createToolingConfiguration: true` and
   `createBlueprintUseGrant: true` after verifying those resources are absent.
   Preserve the deploying role as project owner so rollback/update
   remains possible. Optional operator ownership must match the customer's
   authorized operators.
4. Wait for both project and environment to become ACTIVE. On failure, inspect
   service/CloudFormation events. Recover only resources from this build and
   retain the domain/data. Do not repeatedly create replacement projects.
5. Instantiate the normal storage/pipeline stacks. Keep the SMUS foundation,
   storage, ETL and scoped LF grants in separate stacks.

The template uses a dedicated private, encrypted `amazon-sagemaker-*` project
asset bucket. Tooling `S3Location` must be a **full `s3://bucket` URI**. It enables
SQL/Glue and disables automatic Spaces, Bedrock and ML workload permissions.
This does not prevent users from later starting a Query Editor kernel; inspect
and stop test compute after verification.

For an existing domain/new project, skip step 1 and use its discovered outputs.
Set `createToolingConfiguration: false` when reusing its enabled regional
Tooling configuration; this omits the shared bucket/role/configuration resources.
Set `createBlueprintUseGrant: false` when an existing grant already authorizes
the project path. Verify both prerequisites before using the reuse flags.
Never overwrite an existing shared Tooling configuration or create duplicate
grants; have the domain owner review the compatible project profile.

## 4. Associate the pipeline and grant project access

Create `smus-config.json` from **live outputs**, not naming guesses:

```json
{
  "account_id": "111122223333",
  "region": "us-east-1",
  "prefix": "acme",
  "domain_id": "dzd-discovered",
  "project_id": "discovered-project",
  "environment_id": "discovered-environment",
  "table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-table-bucket",
  "namespace": "acme_db",
  "tables": ["orders_clean"],
  "jobs": ["acme-ingest-iceberg"],
  "checks": [
    {"table": "orders_clean", "expected_count": 6,
     "sum_column": "line_total", "expected_sum": "100.00"}
  ]
}
```

The numbers are a synthetic example. Calculate customer expectations independently
from source data and transformations. Do not derive “expected” from the target
table or copy sample values into a real build.

On extension, merge the existing managed table/job list from `platform.yaml`.
The dedicated inline policy represents that complete list; omitting an existing
table can remove its access. Do not reuse an unrelated inline policy's name or
remove existing access unless that change is part of the customer's plan.

Run `inspect` and review its plan before `apply`:

```bash
python3 scripts/smus_project.py inspect --decision .platform-decision.json \
  --config smus-config.json --output smus-plan.json
python3 scripts/smus_project.py apply --decision .platform-decision.json \
  --config smus-config.json --output smus-apply.json
```

Both commands verify the recorded customer mode and actual STS account.
`inspect` makes no mutations. `apply` changes only:

- A dedicated project-role inline policy (`PipelineSmusDatasetAccess` by default):
  Glue catalog metadata, named S3 Tables reads, and named Glue job run/status
  actions. ETL writes continue under the ETL service role.
- Glue tags `AmazonDataZoneDomain` and `AmazonDataZoneProject`, retaining unrelated
  tags. If the job is already assigned to another domain/project, stop.

The adapter discovers table ARNs and the actual project role/workgroup, refuses
ambiguous environments, and never stores temporary credentials.

**Also put the same job tags in the generated CDK job definition.** Otherwise a
later deployment can remove the association. Use project outputs as tokens or
required context/config; do not make association silently optional on resynthesis.

Deploy `assets/smus/smus-table-grants.ts` with the existing authorized LF grant
role. The catalog ID is
`{account_id}:s3tablescatalog/{table_bucket_name}`. Grant the project role database
DESCRIBE plus SELECT/DESCRIBE on each named table, with no grant option. Import or
reuse existing permissions for the same principal/resource pair rather than
creating a duplicate `AWS::LakeFormation::PrincipalPermissions` resource.
LF grant deletion may revoke permissions on that pair; treat ownership and
teardown carefully. `apply` does **not** apply LF grants.

For ordinary Hive, federated sources, or cross-account access, stop using this
S3 Tables adapter and generate the reviewed permission/catalog pattern for that
case. The customer still owns the SMUS choice.

## 5. Acceptance and operational handoff

After permissions propagate, run:

```bash
python3 scripts/smus_project.py verify --decision .platform-decision.json \
  --config smus-config.json --output smus-verification.json \
  --run-job acme-ingest-iceberg
```

This vends credentials through DataZone, checks the actual project identity,
waits for the job to succeed, and compares project-role query counts/sums against
the independently supplied expectations. It exits nonzero on mismatch. An IAM
owner calling `GetEnvironmentCredentials` **does not prove IdC sign-in**.

Complete these separately:

1. Reconcile all source/base/mart counts, monetary totals and declared grains.
   Include invalid records and duplicates in a bounded synthetic test; explain
   intentional exclusions. For incremental jobs rerun the same input and prove
   no duplicate keys or changed totals.
2. Use the intended login path when available. Show the job under SMUS **Data
   processing jobs → Code**, run it, and query the same table via project Athena.
   If interactive sign-in cannot be completed, record that limitation.
3. Repeat the deployment and adapter apply. Confirm stable resources, no duplicate
   projects, and no IAM/tag changes on the second identical apply.
4. Compare LF global settings to the approved baseline. Verify no unauthorized
   administrators or broad access remain. Stop only test-created interactive
   compute; preserve requested resources and customer schedules.

Record `use_smus`, customer decision reference, domain/project/environment IDs,
actual role ARN, connection/workgroup, catalog/table names, ownership, LF grant
owner, test query/job IDs, and separate API/UI/SSO/visual/Canvas results in
`platform.yaml` and `ARCHITECTURE.md`. Standalone builds record `use_smus: false`
and do not emit SMUS stacks, profiles, roles, assignments, or tags.

## Sources and validation boundary

Check current API/blueprint schemas before adapting:

- [AWS SMUS CloudFormation examples](https://github.com/aws/Unified-Studio-for-Amazon-Sagemaker/tree/main/cloudformation/domain)
- [DataZone Domain CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-datazone-domain.html)
- [DataZone Project CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-datazone-project.html)
- [GetEnvironmentCredentials](https://docs.aws.amazon.com/datazone/latest/APIReference/API_GetEnvironmentCredentials.html)
- [Lake Formation PrincipalPermissions](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lakeformation-principalpermissions.html)

The underlying same-account pattern was live-tested with a new IdC-configured V2
domain, Glue 5.0, S3 Tables, four successful ETL runs, project-role Athena queries,
SMUS job/query UI, and a no-change CDK redeployment. Actual IdC sign-in, Visual ETL
round-tripping and Canvas were not part of that test. The bundled parameterized
templates and adapter require their own validation for the selected account;
the historical test is not evidence that every branch or customer will work.
