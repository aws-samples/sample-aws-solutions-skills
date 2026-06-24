# Decision Tree

> Discovery answers → component selection. After receiving the Phase 1 answers, decide using this table and build the Phase 2 Design table.

## 1. Industry/domain — determines the graph schema

This is the first and biggest branch. Vertex/edge labels, recommendation scenarios, and evaluation metrics all differ per industry.

| Answer | Schema applied | Default recommendation scenarios |
|---|---|---|
| **E-commerce** (default) | User-Item-Category-Brand + BOUGHT/VIEWED/CART | "items bought by similar buyers" + cross-sell |
| **Media** (music/video/news) | User-Content-Genre-Person + WATCHED/RATED/FOLLOWS | "similar viewing patterns" + new releases from favorite creators |
| **B2B SaaS** | Account-Feature-Industry-Plan + USES/UPGRADED_FROM | "features used by similar companies" cross-sell |
| **Recruiting / headhunting** | Candidate-Skill-Company-JobPosting + WORKED_AT/HAS_SKILL | "matching candidates with similar career history" |
| **Healthcare** | Patient-Diagnosis-Medication + HAS_DIAGNOSIS/TREATED_WITH | "treatment patterns of similar patients" (strict privacy) |
| **Other / custom** | user specifies vertex/edge directly | user-defined |

For detailed schemas, see `shared/reference/graph-schemas.md`.

## 2. Node types — schema depth

| Answer | Additional vertex types |
|---|---|
| "Basic" (default) | User + Item + first-level taxonomy (Category, etc.) |
| "+ User-User social graph" | + FOLLOWS / FRIEND_OF edges |
| "+ Knowledge graph (external ontology)" | + external entity integration (Wikidata, industry-standard taxonomies) |
| "+ Multi-tenant" | + Tenant vertex + isolation |

Default = User + Item + Category (3 types).

## 3. Edge types — behaviors that carry weight

| Answer | edge definitions |
|---|---|
| **Explicit behaviors only** | RATED (1-5), LIKED, FOLLOWED, PURCHASED |
| **Including implicit behaviors** (default) | + VIEWED (duration), CART (count), DWELL_TIME |
| **External industry standards** | + KCD code (medical), HS code (logistics), GICS (finance) |

Default = explicit + implicit. Weights differ per behavior:
```
PURCHASED  → 5.0 weight
CART       → 3.0
VIEWED     → 1.0 (× duration_seconds / 60)
RATED      → rating_value (1-5)
```

## 4. Data source

| Answer | Handling |
|---|---|
| **UCP (Unified Customer Profile) skill's golden record** | fetch endpoint from UCP secret/SSM + cross-skill integration |
| **DynamoDB table** | Streams → Kinesis → Lambda → Neptune |
| **Aurora / RDS** | DMS or batch export → S3 → Neptune Bulk Loader |
| **S3 export (CSV / Parquet)** | Bulk Loader API (initial load) + Kinesis (real-time) |
| **External API** | Lambda scheduled fetch → Kinesis |

Default = user chooses between "UCP integration" or "DynamoDB Streams".

## 5. Graph update mode

| Answer | Handling | Cost |
|---|---|---|
| **Real-time** (default production) | Kinesis Data Streams → Lambda batch upsert | + Kinesis $11/shard/mo |
| **Batch (once daily)** | EventBridge schedule → Step Functions → Bulk Loader | $1-3/mo |
| **Hybrid** | Initial bulk load + real-time afterwards | both |

Default = Real-time. Dev/PoC uses Batch.

## 6. Recommendation scenarios — Lambda function types

| Answer | Generated endpoint |
|---|---|
| **"What similar users bought"** (default) | `POST /recommendations/collaborative` |
| **"Category-preference based"** | `POST /recommendations/content-based` |
| **"Cross-sell / frequently bought together"** | `POST /recommendations/cross-sell` |
| **"Cold start (popular)"** (always included) | `POST /recommendations/popular?segment=X` |
| **"GraphRAG (natural-language queries)"** | `POST /chat` — Bedrock + graph context |

Default = collaborative + popular + cross-sell. GraphRAG is optional.

## 7. Explainability (Bedrock natural-language explanation)

| Answer | Handling |
|---|---|
| "Basic recommendation" (default) | Bedrock Sonnet 4 explanation generation, Korean + English |
| "Score only" | skip explanation generation — Bedrock cost 0 |
| "GraphRAG too" | both explanation + chat endpoint |

Default = explanation included (Sonnet 4).

## 8. Response latency requirement

| Answer | Handling |
|---|---|
| **Real-time API (<200ms)** | Lambda + Neptune reader endpoint + DAX-style cache (DynamoDB) |
| **Standard (<1s)** (default) | Lambda + Neptune writer/reader |
| **Batch (email campaign)** | Step Functions + Lambda Map + Bedrock batch |

Default = standard (<1s). Real-time adds a cache layer.

## 9. Neptune mode

| Answer | Selection | Cost |
|---|---|---|
| **"Basic recommendation"** (default) | **Serverless v2** (0.5–16 NCU auto-scale) | min 0.5 NCU = $44/mo |
| "Production steady load" | Provisioned db.r6g.large + Reader replica | ~$300+/mo |
| "Dev/PoC only" | Serverless v2 (auto-pause v1 is deprecated) | min 0.5 NCU = $44/mo |
| "Large dataset (TB+)" | Serverless v2 max 128 NCU or Provisioned db.r6g.4xlarge+ | $$ |

Default = Serverless v2 (0.5–16 NCU range).

## 10. Whether to use Neptune ML (GNN)

| Answer | Handling |
|---|---|
| **"Basic recommendation / skip"** (default) | Pure traversal-based (Cypher) — no Neptune ML |
| **"Use GNN embeddings"** | + MLStack: SageMaker training (monthly) + serving |
| **"Real-time GNN inference"** | + endpoint hosting (per-minute billing — production scale) |

Default = skip. Enabling adds significant cost + operational burden.

**Cases where Neptune ML is worth it**:
- Severe cold-start problem (many new users)
- Very large dataset (vertices 100M+)
- pure traversal recommendation quality has hit a ceiling

## 11. Frontend admin/demo UI

| Answer | Generated |
|---|---|
| "Basic recommendation (included)" (default) | React 18 + Vite + shadcn — Graph Explorer + Recommendation Demo |
| "API only" | skip Frontend |

Default = included.

## 12. Authentication

| Answer | Handling |
|---|---|
| "Basic" (default) | Cognito User Pool + Hosted UI |
| "+ external IdP" | + SAML / OIDC federation (Okta, Google Workspace) |
| "Public API" | skip Cognito — API key or IP allow list |

Default = Cognito.

## Phase 2 Design output (example)

```
| Component               | Selection                                              |
|------------------------|--------------------------------------------------------|
| Industry                | E-commerce                                            |
| Region                  | ap-northeast-2                                        |
| Schema (Vertices)       | User, Item, Category, Brand                           |
| Schema (Edges)          | BOUGHT, VIEWED, CART, IN_CATEGORY, HAS_BRAND          |
| Edge weights            | BOUGHT 5.0, CART 3.0, VIEWED 1.0× duration            |
| Data source             | DynamoDB Streams                                      |
| Update mode             | Real-time (Kinesis 1 shard)                           |
| Recommend endpoints     | collaborative + popular + cross-sell                  |
| Explanation             | Bedrock Sonnet 4 (us.anthropic.claude-sonnet-4-...)  |
| Latency                 | Standard (<1s)                                        |
| Neptune mode            | Serverless v2 (0.5–16 NCU)                            |
| Neptune ML              | Skip (review further in Phase 2)                      |
| Frontend                | React Admin/Demo UI                                   |
| Auth                    | Cognito User Pool                                     |
| Estimated cost          | ~$280/mo (medium prod)                                |
```
