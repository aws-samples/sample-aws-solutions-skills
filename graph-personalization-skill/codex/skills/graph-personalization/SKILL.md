---
name: graph-personalization
description: |
  Build a Customer Similarity Graph + Bedrock-explainable recommendation system on AWS
  using Amazon Neptune (graph DB) + Bedrock Claude + Kinesis (real-time updates) + React
  Admin UI. Generates production-ready CDK with Graph schema per industry (e-commerce /
  media / B2B SaaS / recruiting / healthcare), parameterized Cypher queries with privacy
  thresholds, recency-decayed weights, Cold start fallback, and natural-language
  recommendation explanations. Use when user asks for "graph-based personalization",
  "similar user recommendations", "knowledge graph", "Neptune recommendation",
  "graph-based personalization", "explainable recommendation", "customer similarity
  graph", "e-commerce recommendations", or describes scenarios needing
  relationship-driven recommendations beyond simple collaborative filtering / vector
  search.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---

# Graph Personalization Builder

## Purpose
Model user behavior/attribute data as a graph (Amazon Neptune) to generate a production-ready CDK system that delivers relationship-based personalization recommendations — such as **"what users similar to you liked"**, **"content your friends watched"**, and **"features used by larger customers in the same industry"** — plus Bedrock natural-language explanations. The per-industry graph schema difference is the biggest specific value.

## Knowledge sources
- `shared/reference/architecture.md` — full architecture + WHY each component
- `shared/reference/decision-tree.md` — 12 decision branches (industry/node/edge/data source/update/scenario/explainability/latency/Neptune mode/ML/Frontend/Auth)
- `shared/reference/aws-services.md` — Neptune mode comparison, Bedrock models, cost scenarios
- `shared/reference/constraints.md` — 25 pitfalls (must read)
- `shared/reference/graph-schemas.md` — **per-industry vertex/edge schema (E-commerce / Media / B2B SaaS / Recruiting / Healthcare)**
- `shared/patterns/cdk-stacks.md` — 7 stacks (Network/Graph/Ingestion/ML/Auth/API/Frontend)
- `shared/patterns/graph-queries.md` — Cypher templates (collaborative / cross-sell / popular / similar-items / friends-watched / peer-adoption / upgrade-readiness)
- `shared/patterns/bedrock-explanation.md` — Privacy-preserving prompts + caching + multilingual
- `shared/patterns/ingestion-patterns.md` — Kinesis → Lambda → Neptune (real-time) + Bulk Loader (batch)
- `shared/patterns/frontend-pages.md` — React + Vite + vis-network + shadcn (Graph Explorer + Recommendation Demo + Admin)
- `shared/examples/{ecommerce,media,b2b-saas}.md` — 3 industry instantiations

## Workflow

### Phase 1: Discovery (interactive requirements gathering)

```
1. Industry/domain — determines schema (E-commerce default / Media / B2B SaaS / Recruiting / Healthcare / other)
2. Node types — basic (User+Item+Category) / + User-User social / + Knowledge graph / + Multi-tenant
3. Edge types — explicit only (BOUGHT/RATED) / including implicit (VIEWED/CART/DWELL_TIME) / + industry standard
4. Data source — UCP / DynamoDB Streams / Aurora-RDS / S3 export / external API
5. Update mode — Real-time (Kinesis, default prod) / Batch (once daily) / Hybrid
6. Recommendation scenario — collaborative + cross-sell + popular (default) / + content-based / + GraphRAG
7. Explainability — Bedrock natural-language explanation (default) / simple score / + GraphRAG chat
8. Response latency — Real-time <200ms (DAX cache) / Standard <1s (default) / Batch
9. Neptune mode — Serverless v2 (default) / Provisioned RI / Dev t3.medium
10. Neptune ML (GNN) — Skip (default) / Enable (monthly training)
11. Frontend admin/demo UI — Yes (default) / API only
12. Auth — Cognito (default) / + external IdP / public API
```

⛔ **GATE 1**: Summarize gathered requirements + **graph schema preview** → user approval.

### Phase 2: Architecture Design

Apply `shared/reference/decision-tree.md`:

1. **Finalize schema** — industry answer → vertex/edge label + property + weight table → display explicitly to the user
2. Decide **stack composition** (7 stacks)
3. **Neptune mode** + NCU range
4. **Update mode** (number of Kinesis shards)
5. **Explanation cost trade-off** (Sonnet 4 vs Haiku 4.5)
6. **Cost estimate** (`shared/reference/aws-services.md`)

⛔ **GATE 2**: Design table + schema diagram + cost estimate → user approval.

### Phase 3: Code Generation

Reference order for `shared/patterns/*`:

1. **Project scaffolding**:
   ```
   cdk/{bin/app.ts, cdk.json, package.json, tsconfig.json}
   backend/{lambdas/, queries/, shared/}
   frontend/{package.json, vite.config.ts, src/}
   ```
2. **CDK stacks** — 7 stacks (`shared/patterns/cdk-stacks.md`)
3. **Backend Lambdas** — Python (`shared/patterns/ingestion-patterns.md` + `graph-queries.md`)
4. **Cypher templates** — per-industry (`shared/reference/graph-schemas.md` + `graph-queries.md`)
5. **Bedrock prompts** — per-industry (`shared/patterns/bedrock-explanation.md`)
6. **Frontend** — React + vis-network (`shared/patterns/frontend-pages.md`)
7. **Scripts** — deploy/destroy/check-prereq/generate-frontend-config

⛔ **GATE 3**: `cdk synth` passes + verify IAM action / region availability with AWS Knowledge MCP.

### Phase 4: Validate
- `cdk synth` clean
- AWS Knowledge MCP — Neptune Serverless v2 region, Bedrock model ID, Neptune ML availability
- Eval scenario mapping (`evals/<scenario>.md`)

### Phase 5: Deploy
1. CDK bootstrap
2. Stack deploy order (Network → Graph → Ingestion → (ML) → Auth → API → Frontend)
3. **Initial bulk load**: existing data → S3 → Neptune Bulk Loader API
4. Enable Anthropic model access
5. Configure Cognito test user / SAML federation
6. (When Neptune ML enabled) manually trigger the first training
7. Frontend `s3 sync` + CloudFront invalidate
8. Smoke test (Cognito JWT → /recommendations/popular → /graph/explore)

## Generation rules

- **CDK**: TypeScript + `aws-cdk-lib` v2.150+ + Constructs v10
- **Backend**: Python 3.13 + Lambda + gremlin-python + boto3
- **Frontend**: React 18 + Vite + Tailwind v3 + shadcn/ui + vis-network + Amplify (Cloudscape ❌)
- All resource prefix: `{projectName}-{environment}-`
- 1 KMS CMK + alias, used by Neptune/S3/Kinesis alike
- Cypher: parameterized + privacy threshold (count >= 3) + recency decay enforced
- Bedrock: prompt caching + privacy validation + multilingual (Korean + English)

## Hard Constraints

The full 25 items are in `shared/reference/constraints.md`. One-line summary:

1. **Schema is hard to change** — finalize the schema in Phase 2 before proceeding to Phase 3. Migration burden is high if changed later.
2. **Neptune Serverless v2 cost floor** — min 0.5 NCU = $44/month (no auto-pause). Applies to Dev too.
3. **Cold start fallback required** — graph traversal alone cannot recommend for users with fewer than 3 edges. Always include a popular fallback.
4. **Privacy** — do not expose other user IDs in the Bedrock prompt. Aggregation only (count >= 3).
5. **Real-time edge upsert** — batch_size=100 + UNWIND + DLQ + reservedConcurrency 50.
6. **IAM database auth** — gremlin-python + SigV4 plug-in. Do not use password auth.
7. **Bedrock context size** — top 20 + summary helper. Account for 2x Korean token usage.
8. **Edge weight standard** — per behavior (BOUGHT 5.0, CART 3.0, VIEWED 1.0×dur) + recency decay (`exp(-0.05 × days)`)
9. **Multi-AZ** — enforced for production (writer + reader replica)
10. **GNN training cadence** — monthly by default (cost savings). Weekly/daily for large production datasets.
11. **Frontend graph viz** — top 20 + click-to-expand. Do not render a large graph all at once.
12. **Bedrock prompt caching** — system prompt + few-shot ephemeral 5min cache (90% cost reduction).
13. **Connection pool** — Lambda reservedConcurrency 50 (reduces Neptune writer load).
14. **Cypher injection** — parameterized only ($userId, $limit). String interpolation prohibited.
15. **Bulk Loader** — 100x faster than streaming for 100K+ vertices. Recommended for initial load.
16. **Bedrock throttling** — Lambda retry + DLQ + service quota increase (production).
17. **Multi-tenant** — single cluster + tenant property (default) / cluster-per-tenant (strong isolation).
18. **Edge property timestamp** — all in unix milliseconds (compatible with Neptune `datetime()`).
19. **KMS CMK RETAIN** — preserve the key even on stack delete (mistake prevention).
20. **Production deletion_protection** — Neptune cluster RETAIN + `deletionProtection: true`.
21. **Backup** — automated 7-30 days + (optional) AWS Backup vault.
22. **Frontend graph viz top-N** — vis-network freezes at 1000+ nodes. Use the drill-down pattern.
23. **Cypher result format** — use a human-readable format for the Bedrock prompt (top 20 list + score).
24. **Neptune health check** — verify `RETURN 1` on Lambda init.
25. **Tags** — Project / Environment / Component / DataClassification on every stack.

## When to call MCP

| When | MCP | Call |
|---|---|---|
| Confirm Neptune Serverless v2 region | AWS Knowledge | `aws___get_regional_availability(filters=["Amazon Neptune"])` |
| Neptune ML available region | AWS Knowledge | `aws___search_documentation` ("Neptune ML availability region") |
| Bedrock model ID (Sonnet 4 / Haiku 4.5) | AWS Knowledge | `aws___search_documentation` ("claude sonnet 4 inference profile id") |
| Neptune engine version | AWS Knowledge | `aws___search_documentation` ("Neptune engine version") |
| CDK construct prop | AWS Knowledge | `aws___read_documentation` |
| Validate generated code (optional) | CloudFormation | validate-template |
