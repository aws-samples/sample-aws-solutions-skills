# Architecture

> **Customer Similarity Graph + Bedrock Explainable Recommendation.** A system that stores a user-item-relationship graph in Neptune, traverses similar users/items with Gremlin/openCypher, and generates natural-language explanations of the results with Bedrock Claude.

## High-level diagram

```
                                     Frontend (React + Vite)
                                          │
                                          │ Cognito JWT
                                          ▼
                                ┌─────────────────────┐
                                │   API Gateway        │
                                │   + Cognito auth     │
                                └──────────┬──────────┘
                                           │
                          ┌────────────────┼─────────────────┐
                          ▼                ▼                 ▼
                  ┌──────────────┐  ┌───────────┐  ┌─────────────────┐
                  │ Recommend λ  │  │ Explore λ │  │ Admin/Ingest λ  │
                  │ (graph query │  │ (graph    │  │ (graph stats,    │
                  │  + Bedrock)  │  │  viz API) │  │  schema mgmt)    │
                  └──────┬───────┘  └─────┬─────┘  └────────┬─────────┘
                         │                │                  │
                         └────────────────┼──────────────────┘
                                          ▼
                          ┌──────────────────────────────────┐
                          │   Neptune Cluster (VPC, private) │
                          │                                   │
                          │   - Cluster endpoint (writer)     │
                          │   - Reader endpoint               │
                          │   - Gremlin / openCypher / SPARQL │
                          │   - IAM database authentication   │
                          │   - Encryption: KMS CMK           │
                          │                                   │
                          │   Vertex labels (industry-driven):│
                          │     User, Item, Category, Brand,  │
                          │     Segment, Tag, ...             │
                          │                                   │
                          │   Edge labels:                    │
                          │     BOUGHT, VIEWED, RATED,        │
                          │     LIKES, SIMILAR_TO, ...        │
                          └──────────────────────────────────┘
                                          ▲
                                          │ writes
                          ┌───────────────┴──────────────────┐
                          │                                   │
              ┌─────────────────────┐              ┌────────────────────┐
              │ Kinesis Data Stream │              │ S3 (bulk loader)   │
              │ (real-time events)  │              │ (initial / batch)  │
              └─────────┬───────────┘              └────────┬───────────┘
                        │                                   │
                        ▼                                   ▼
              ┌─────────────────────┐              ┌────────────────────┐
              │ Ingestion Lambda    │              │ Neptune Bulk       │
              │ (batch upsert,      │              │ Loader API         │
              │  DLQ, retries)      │              │                    │
              └─────────────────────┘              └────────────────────┘

   (Optional)
   Neptune ML (GNN) ──→ trains user/item embeddings → stored back as vertex properties
                       SageMaker training job (scheduled weekly/monthly)

   Bedrock Claude (Sonnet 4 default) ── explains graph results in natural language
   "These are products frequently watched together by users with purchase patterns similar to yours."
```

## Component decisions and WHY

### 1. Amazon Neptune (graph DB)

- **What**: stores a graph of Vertices (User, Item, Category, ...) + Edges (BOUGHT, VIEWED, ...). Multi-hop relationship traversal is native.
- **Why graph DB?**
  - Faster than a relational DB's multi-JOIN (even a 10-hop traversal is in the ms range)
  - Enables multi-hop recommendations like "what a friend of a friend bought" and "similar category + similar users"
  - Schema flexibility — diverse per-industry vertex/edge labels
- **Why Neptune (vs Neo4j, ArangoDB)?**
  - Fully-managed (automatic backup, Multi-AZ, patching, scaling)
  - IAM auth + KMS encryption + VPC isolation native
  - The same cluster supports **Gremlin + openCypher + SPARQL** → free choice of query pattern
  - Neptune ML (GNN) option — graph embedding training without separate SageMaker code
- **Mode**:
  - **Serverless v2**: 0.5–128 NCU auto-scale. Suited to dev/PoC + production with high load variability.
  - **Provisioned**: from db.r6g.large, RI available. Production with large steady load.

### 2. Gremlin vs openCypher (the same cluster supports both)

The same Neptune cluster can use **both query languages**. Choose per query:

| Language | Best for |
|---|---|
| **openCypher** | higher recommendation-query readability, SQL-friendly (familiar to Neo4j users) |
| **Gremlin** | graph algorithms (PageRank, shortest path), step-based expression |
| **SPARQL** | only when integrating external RDF knowledge graphs |

This skill's default = **openCypher** (readability + Bedrock understands Cypher well).

### 3. Bedrock for Explanation

- **What**: feed the graph traversal results (top-N items + relationships) to Bedrock Claude as context and generate a natural-language recommendation rationale.
- **Why?**
  - A recommendation alone does not say "why it was recommended" → lower user trust
  - Bedrock produces natural-language reasoning like "similar purchase pattern", "same category preference", "related to recently viewed items"
- **Privacy pattern**:
  - Never include other users' IDs in the prompt
  - "liked by N people similar to you" — aggregation only (count > threshold)
  - Additional validation with Bedrock Guardrails
- **Model**:
  - Default: **Claude Sonnet 4** (us.anthropic.claude-sonnet-4-20250514-v1:0) — balanced
  - Cost-conscious: **Claude Haiku 4.5** — recommendation text is simple
  - Re-confirm the latest ID with AWS Knowledge MCP each time

### 4. Kinesis → Lambda → Neptune (real-time updates)

- **What**: when user behavior (view, click, purchase) arrives via Kinesis, a Lambda adds Neptune edges via batch upsert.
- **Why?**
  - New user behavior must be reflected in recommendations **immediately** (fast cold-start recovery)
  - Kinesis acts as a throughput buffer (the Neptune writer has an instantaneous TPS limit)
  - The DLQ preserves failed messages (retry / analysis)
- **Trade-off**:
  - For batch (once daily) instead of real-time, use S3 + EventBridge schedule rather than Kinesis
  - High-throughput production uses Kinesis Data Streams (provisioned shards)

### 5. (Optional) Neptune ML — Graph Neural Network

- **What**: SageMaker automatically trains a GNN and stores user/item embeddings as vertex properties.
- **Why?**
  - Pure traversal-based (Cypher) only uses explicit edges. A GNN learns latent features.
  - Compresses the definition of "similarity" — synthesizing multi-hop + properties — into a single vector → vector index search
  - Mitigates cold start — embeddings can be generated even for a new user from a partial graph
- **Trade-off**:
  - SageMaker training cost (per-minute billing) + additional operational burden
  - Default = Discovery option (user enables/disables)
  - Dev/PoC = skip recommended

### 6. Frontend (React + Vite + shadcn)

- **What**: Admin/demo UI. Two pages:
  - **Graph Explorer**: enter a user ID → visualize the graph centered on that user node (vis.js / cytoscape)
  - **Recommendation Demo**: select a user → recommendation results + Bedrock explanation + edge weight breakdown
- **Why?**
  - Visuals are key when demoing the skill — graph patterns are intuitive
  - Lets an admin validate the schema + sanity-check recommendation quality
- **Tech**: React 18 + Vite + Tailwind v3 + shadcn/ui + Cognito Authenticator (same pattern as UCP/Strands)

### 7. Authentication & Authorization

| Flow | Authentication |
|---|---|
| Frontend → API Gateway | Cognito User Pool (USER_PASSWORD_AUTH) |
| API Gateway → Lambda | API Gateway automatic (IAM role) |
| Lambda → Neptune | **IAM database authentication** (Neptune verifies SigV4) |
| Lambda → Bedrock | Lambda execution role |
| Kinesis → Lambda | event source mapping |

→ Neptune does not use password-based auth (IAM only). More secure + integrates with IAM Identity Center.

## Stack separation (CDK)

| Stack | Responsibility | Dependency |
|---|---|---|
| `NetworkStack` | VPC (Neptune requires VPC private), subnets, security groups | (root) |
| `GraphStack` | Neptune cluster + IAM auth + KMS | NetworkStack |
| `IngestionStack` | Kinesis Data Streams + Lambda + DLQ + S3 bulk loader bucket | GraphStack |
| `MLStack` (optional) | Neptune ML training + SageMaker role + scheduled trigger | GraphStack |
| `AuthStack` | Cognito User Pool + Hosted UI | (independent) |
| `ApiStack` | API Gateway + Lambdas (recommend, explore, admin) | GraphStack, AuthStack |
| `FrontendStack` | S3 + CloudFront + WAF (optional) | (independent — config injects API endpoint) |

> **Order**: Network → Auth (parallel) → Graph → Ingestion + (ML) → API → Frontend.

## Request lifecycle (example: "recommend for User X")

```
1. Frontend → POST /recommendations { user_id: "u-123" }
   - Accompanied by the Cognito JWT header

2. API Gateway validates the JWT → invokes the Recommend Lambda

3. Recommend Lambda:
   a. Neptune connection (IAM SigV4)
   b. openCypher query:
      MATCH (u:User {id: "u-123"})-[:BOUGHT]->(:Item)<-[:BOUGHT]-(other:User)
      WHERE u <> other
      WITH other, count(*) AS shared
      WHERE shared >= 3                          // privacy threshold
      MATCH (other)-[:BOUGHT]->(rec:Item)
      WHERE NOT (u)-[:BOUGHT]->(rec)
      WITH rec, sum(shared) AS score
      ORDER BY score DESC LIMIT 10
      RETURN rec.name, rec.id, score
   c. Result: 10 items + score
   d. Bedrock invoke:
      - prompt: "Explain the following recommendation rationale in natural language: items=[...], user segment=VIP"
      - response: "These are products frequently bought together by users with purchase patterns similar to yours..."

4. API Gateway → Frontend: { items, explanation }

5. Frontend renders as cards + edge weight breakdown
```

## Cost scenarios (us-east-1, monthly)

### Dev / PoC (Serverless v2 min ACU, small data)

```
Neptune Serverless v2 (0.5 NCU min, idle 70%)    ~$50
Lambda (recommend, 100 invocations/day)          $1
Bedrock Sonnet 4 (100 calls × 1K tokens)         ~$5
Kinesis (1 shard) — or $0 if batch              $11
S3 + CloudFront (Frontend)                       $5
Cognito (under 50K MAU)                          $0
KMS                                              $1
─────────────────────────────────────────────────────
Total                                            ~$73/mo
```

### Production (medium, 1M events/day, 10K rec/day)

```
Neptune Serverless v2 (avg 4 NCU)                ~$280
Lambda (recommend + ingest)                      $20
Bedrock Sonnet 4 (10K rec/day × avg)             ~$200
Bedrock Haiku 4.5 (caching applied)              + cost ↓ 70%
Kinesis (3 shards)                               $33
S3 + CloudFront                                  $20
Cognito                                          $5 (low MAU)
KMS                                              $2
─────────────────────────────────────────────────────
Total                                            ~$560-700/mo
```

### + Neptune ML (optional, monthly training)

```
+ SageMaker training (monthly, 4 hours, ml.m5.xlarge)  ~$50
+ Neptune ML processing (data export + serving)        ~$30
                                                       ~$80 added
```

## Gotchas (summary — the detailed 25 items are in `shared/reference/constraints.md`)

1. **Schema changes are hard** — Neptune is schemaless, but vertex/edge labels are a de facto contract. Design carefully from the start.
2. **Neptune minimum cost fence** — even Serverless v2 has a min 0.5 NCU = $44/mo (no auto-pause).
3. **Cold start** — graph traversal alone cannot recommend for a 0-edge user → fallback (popular + segment).
4. **Privacy** — never expose other user IDs explicitly. Aggregation only (count > N).
5. **Real-time edge throughput** — batch upsert pattern from Kinesis → Neptune writer + DLQ.
6. **IAM auth** — the Lambda calls Neptune via SigV4-signed HTTP requests. Confirm the library (gremlin-python, neo4j-driver) supports the SigV4 plug-in.
7. **Bedrock context size** — graph results of 100+ items → only top 20 in the prompt (account for 2x Korean token usage).
8. **Edge weight normalization** — per behavior (purchase 5, cart 3, view 1) + recency decay (0.5x after 30 days).
9. **Multi-AZ cost** — adding a Provisioned reader instance doubles the cost.
10. **GNN training cadence** — monthly vs weekly. Cost vs freshness.

## Volatile catalog (always verify with MCP)

The `shared/reference/aws-services.md` catalog + always re-confirm the following with AWS Knowledge MCP:
- Neptune Serverless v2 available regions (Seoul, Tokyo, Virginia, etc.)
- Bedrock Claude Sonnet 4 / Haiku 4.5 cross-region inference profile prefix
- Neptune ML available regions (limited)
- Kinesis shard limit per region
