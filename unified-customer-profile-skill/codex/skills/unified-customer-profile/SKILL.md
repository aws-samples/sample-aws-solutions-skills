---
name: unified-customer-profile
description: |
  Build a production-ready unified customer profile system on AWS using Connect Customer
  Profiles + Entity Resolution + Bedrock AI. Generates a full CDK TypeScript project
  with Lambda handlers, React + shadcn/ui frontend, ETL scripts, and Calculated
  Attributes. Use when the user asks for "customer 360", "unified profile",
  "unified customer profile", "customer profile integration", "customer profile system",
  "entity resolution", "customer matching", "ID resolution", or describes scenarios with
  multi-channel customer data needing identity resolution. Industry-agnostic: airline,
  hotel, retail, finance, etc.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---

# Unified Customer Profile Builder

## Purpose
Through conversation with the user, gather requirements and generate a custom customer
profile integration system based on AWS Connect Customer Profiles + Entity Resolution.
Industry/domain-agnostic.

## Knowledge sources
All the architecture knowledge, patterns, and examples needed to execute this Skill are in `shared/`:
- `shared/reference/architecture.md` — architecture decisions and rationale
- `shared/reference/decision-tree.md` — conditional selection logic
- `shared/reference/aws-services.md` — service/model catalog (Bedrock Claude model IDs)
- `shared/reference/constraints.md` — limitations and constraints
- `shared/reference/calculated-attributes.md` — Calc Attribute definition, behavior, debugging (must read)
- `shared/patterns/cdk-stacks.md` — CDK stack code
- `shared/patterns/lambda-handlers.md` — Lambda handlers (CP send, ObjectType pitfalls)
- `shared/patterns/frontend-pages.md` — React + Tailwind + shadcn/ui pages
- `shared/patterns/etl-transforms.md` — Raw → ER input pipeline
- `shared/patterns/bedrock-prompts.md` — model selection + prompts
- `shared/patterns/er-strategies.md` — ER matching strategies
- `shared/examples/{travel,hotel,retail}.md` — industry-specific golden examples

## Workflow

### Phase 1: Discovery (conversational requirements gathering)

Collect the following questions from the user in order. Skip information already known.

```
1. Industry/domain: airline/hotel/retail/finance/healthcare/other
2. Channels: web/app/call center/OTA/POS/corporate, etc.
3. Identity data: name/email/phone/date of birth/membership number, etc.
4. Transaction data: reservations/orders/visits/billing → CP child Object Types
5. KPIs: annual revenue/visit frequency/AOV/CLV → Calculated Attributes
6. Data sources: existing DB (Glue Connection)/CSV/Parquet/Kinesis
7. Matching strategy: Rule (highly structured) / ML (varied variations)
8. Additional features: whether Knowledge Graph, Cross-Domain are needed
9. Region/cost constraints
10. PII normalization: already clean / Inline ETL / Glue ETL Job
11. **LLM model** — see the catalog in `shared/reference/aws-services.md`
    - Accuracy (ER rule generation) → Claude Opus 4.7 (`us.anthropic.claude-opus-4-7`)
    - Balance → Claude Sonnet 4 (`anthropic.claude-sonnet-4-20250514-v1:0`)
    - Cost (personalization) → Claude Haiku 4.5 (`anthropic.claude-haiku-4-5-20251001`)
    - Recommended combination: ER rules = Opus 4.7, personalization = Haiku 4.5
    - Always re-confirm the latest ID with AWS Knowledge MCP `aws___search_documentation`
12. Guide on the CP send workflow (Send to CP page included automatically)
13. Calculated Attribute definition — `calculated_attributes` in `config/schema.yaml`
```

⛔ **GATE 1**: Summarize gathered requirements → user approval → Phase 2.

### Phase 2: Architecture Design

Based on `shared/reference/decision-tree.md`:

1. **Stack composition**: Foundation, Storage, Profiles, Matching, Ingestion, Auth, API, [Graph], [Cross-Domain]
2. **ER matching strategy** — `shared/patterns/er-strategies.md`
3. **Cost estimation** — `shared/reference/aws-services.md`
4. **Regional availability** — AWS Knowledge MCP verification

⛔ **GATE 2**: Present design diagram/tables → user approval.

### Phase 3: Code Generation

Based on the approved design, generate incrementally in the following order:

1. **Scaffolding**: `bin/app.ts`, `package.json`, `tsconfig.json`, `cdk.json`, `jest.config.js`
2. **`config/schema.yaml`** — reflects Discovery results. **Must include**:
   - `object_types[]` — never define a `_profileId` key. Use `GuestKey [PROFILE, UNIQUE]` on the Parent and `GuestKey [PROFILE]` on children
   - `calculated_attributes[]` — KPI question mapping
   - `features.ai.bedrock.model_id` + `personalization_model_id`
   - `features.etl.mode` — none / inline / glue
3. **CDK stacks** — see `shared/patterns/cdk-stacks.md`
   ```
   lib/{foundation,storage,profiles,matching,ingestion,auth,api,main}-stack.ts
   [optional] lib/{graph,cross-domain}-stack.ts
   ```
   **Required Custom Resources**: `upsert-object-type` + `create-calculated-attributes` (both include a `SchemaRev` cache-buster)
4. **Lambda handlers** — `shared/patterns/lambda-handlers.md`
   ```
   backend/lambdas/{matching,accuracy,ai-agent,profiles,ingestion}/handler.ts
   backend/lambdas/profile-import/handler.ts        ← Send to CP — Step 1
   backend/lambdas/cp-data-import/handler.ts        ← Send to CP — Step 2 (self-invoke worker)
   backend/lambdas/personalization/handler.ts       ← assembleProfile + assembleFromGolden
   backend/custom-resources/upsert-object-type/handler.ts
   backend/custom-resources/create-calculated-attributes/handler.ts
   backend/glue-scripts/build-er-input.py
   ```
5. **Frontend** — `shared/patterns/frontend-pages.md` (React + Vite + Tailwind + shadcn/ui, NO Cloudscape)
   ```
   frontend/src/pages/{Dashboard,Ingestion,MatchingComparison,Accuracy,AiRules,
                       ProfileImport,ProfileView}.tsx
   frontend/src/components/ui/    ← shadcn (Card, Button, Badge, Alert, Skeleton, Table, Select, Tabs, Dialog)
   frontend/src/{components,api,lib,hooks}/
   ```
6. **Scripts**: `scripts/{deploy,destroy,check-prerequisites,update-frontend-env}.sh`
7. **Docs**: `docs/{architecture,deployment,api-reference,calc-attr-guide}.md`

⛔ **GATE 3**: Verify `cdk synth` passes. Verify the APIs used with AWS Knowledge MCP.

### Phase 4: Validate

- `cdk synth` clean
- Re-confirm used IAM actions / model IDs / regional availability via MCP
- Map to eval scenarios (`evals/<industry>-scenario.md`)

### Phase 5: Deploy

Deployment guide + post-deploy verification steps:
1. Run ER matching (Matching Comparison page)
2. **Send to CP — Step 1**: golden profiles import
3. **Send to CP — Step 2**: Reservation/Folio import (3-10 min, background)
4. Wait Calculated Attribute Status → COMPLETED (a few minutes)
5. Verify calc attr values are populated on the Profile Detail page
6. If empty, follow the "debugging checklist" in `shared/reference/calculated-attributes.md`

## Generation rules

- **CDK**: TypeScript + aws-cdk-lib v2 + Constructs v10
- **Lambda**: Node 20+ TypeScript + esbuild + AWS SDK v3 modular imports
- **Frontend**: React 18 + Vite + **Tailwind v3 + shadcn/ui** (Cloudscape NOT used). Icons `lucide-react`, charts `recharts`, toasts `sonner`. Auth via `oidc-client-ts` + `react-oidc-context`.
- Domain terminology follows the language provided by the user (Korean/English)
- ER rule names are `{MatchKey1}And{MatchKey2}` (e.g. NameAndEmail)
- Resource prefix: `{projectName}-`
- Enforce KMS encryption (all data at rest)
- Enforce SQS DLQ (all async Lambdas)
- No hardcoding: account ID, region, and model ID must all be env-overridable

## Hard Constraints

For detailed explanations, see `shared/reference/constraints.md`. One-line summary:

1. **Connect Instance Quota**: Default 2/account, max 4–5 with quota request. Never > 4 without explicit user approval.
2. **ER ML Matching**: Supported in only some regions — always verify via AWS Knowledge MCP.
3. **Neptune cost**: db.r5.large = ~$300/mo, warning required. Serverless recommended (~$200/mo).
4. **CP Domain names**: unique per account × region.
5. **Bedrock model ID**: cross-region inference profile prefix (`us.`, `eu.`, `apac.`) required. Re-confirm the latest ID via MCP.
6. **EventBridge Pipes + Kinesis**: `pipes.amazonaws.com` required in the IAM trust.
7. **CP Object Type — no `_profileId`**: CP reserved key (auto-filled with a UUID). Use a custom PROFILE key like `GuestKey` instead. For details, see "CP Object Type definition" in `shared/patterns/lambda-handlers.md`.
8. **CP Object Type — Target only `_profile`**: AWS docs state "the only supported target object is `_profile`." Child instances (Reservation, Folio) must omit Target.
9. **CP Object Type — Keys immutable**: Keys/StandardIdentifiers cannot be changed via PutProfileObjectType. To change, delete-then-create + `SchemaRev` cache-buster.
10. **Calculated Attribute lifecycle**: values are populated only after Object Type instance ingestion + CP indexing (Status=COMPLETED, Readiness=100%). The UI explicitly guides through Send-to-CP step 2. For details, see `shared/reference/calculated-attributes.md`.
11. **Cognito OIDC redirect URI**: must exactly match the Hosted UI callback URL (down to the trailing slash).

## When to call MCP

| When | MCP | Call |
|---|---|---|
| Confirm regional availability (ER ML, Connect, etc.) | AWS Knowledge | `aws___get_regional_availability` |
| Look up service constraints / IAM actions | AWS Knowledge | `aws___search_documentation` |
| Confirm latest Bedrock model ID | AWS Knowledge | `aws___search_documentation` (e.g. "claude opus 4 inference profile id") |
| Verify CDK construct props | AWS Knowledge | `aws___read_documentation` |
| Verify generated code (optional) | CloudFormation | validate-template |
| Actual deployment (optional) | CloudFormation | create-stack |
