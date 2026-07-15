# Graph Personalization Builder — AI Skill

> **Customer Similarity Graph + Bedrock Explainable Recommendation.** An AI Skill that stores a user/item/relationship graph in Amazon Neptune, traverses similar users with Cypher, and automatically generates natural-language explanations with Bedrock Claude — delivered as a production-ready CDK system.

**Anthropic Agent Skills standard** (`SKILL.md`) format — Claude Code · Kiro · Codex all share an identical SKILL.md (md5-identical).

## Triggers (invoke in natural language)

```
"build a graph-based personalization recommendation system"
"similar-user recommendations — with Neptune"
"build a graph-based personalization system"
"explainable recommendation engine"
"customer similarity graph"
"e-commerce graph recommendations"
```

## How it differs from other recommendation patterns — why this skill

| Approach | Limitation | Graph-based (this skill) |
|---|---|---|
| **Amazon Personalize** | Closed-box, cannot define custom relationships, cannot explain "why?" | ✅ Custom edge type, explainable |
| **Vector similarity (OpenSearch)** | Semantic similarity only, no multi-hop reasoning | ✅ Hop traversal such as "friend of a friend" |
| **Collaborative filtering (matrix)** | Weak cold start, struggles with sparse data | ✅ Knowledge edges compensate for cold start |
| **Plain SQL JOIN** | Multi-hop cost explodes | ✅ Native graph DB support |

→ A graph enables **explainable + multi-hop** recommendations such as **"Y, liked by users similar to X, because they show the same Z pattern"**.

## Directory structure

```
graph-personalization-skill/
├── README.md                                                       (this file)
├── claude-code/skills/graph-personalization/SKILL.md               ★ identical in 3 places (md5-identical)
├── kiro/skills/graph-personalization/SKILL.md                      ★
├── codex/skills/graph-personalization/SKILL.md                     ★
├── shared/                                                          ⭐ actual knowledge (~5,400 lines)
│   ├── reference/
│   │   ├── architecture.md                  (260L) Neptune+Bedrock+Kinesis+(ML)+Frontend
│   │   ├── decision-tree.md                 (166L) 12 decision branches
│   │   ├── aws-services.md                  (270L) catalog + cost scenarios
│   │   ├── constraints.md                   (398L) 25 pitfalls
│   │   └── graph-schemas.md                 (321L) ★ per-industry schema (the most specific value)
│   ├── patterns/
│   │   ├── cdk-stacks.md                    (640L) 7 stacks of code
│   │   ├── graph-queries.md                 (410L) Cypher templates
│   │   ├── bedrock-explanation.md           (416L) Privacy-preserving prompts
│   │   ├── ingestion-patterns.md            (455L) Kinesis → Lambda → Neptune
│   │   └── frontend-pages.md                (531L) React + vis-network + shadcn
│   └── examples/
│       ├── ecommerce.md                     (220L) cosmetics/fashion shopping mall (default)
│       ├── media.md                         (227L) video streaming + social graph
│       └── b2b-saas.md                      (236L) sales cross-sell + Okta SAML
└── evals/
    ├── ecommerce-scenario.md                (160L)
    └── media-scenario.md                    (149L)
```

## Architecture (the generated system)

```
                          Frontend (React + vis-network + Cognito)
                                       │
                                       ▼
                              ┌─────────────────┐
                              │  API Gateway     │  Cognito JWT
                              └─────────┬───────┘
                                        ▼
                            ┌──────────────────────┐
                            │   Lambdas:           │
                            │   - recommend        │
                            │   - explore (graph)  │
                            │   - admin (stats)    │
                            └────────┬─────────────┘
                                     ▼ openCypher (SigV4)
                          ┌─────────────────────────┐
                          │   Neptune Cluster       │
                          │   - Vertex labels       │
                          │   - Edge types + weights │
                          │   - IAM auth + KMS      │
                          └──────────┬──────────────┘
                                     │
                       ┌─────────────┴────────────┐
                       │                          │
                       ▼                          ▼ (optional)
                  ┌──────────┐               ┌──────────────┐
                  │ Kinesis  │               │ Neptune ML   │
                  │ (event)  │               │ (GNN training│
                  └──────────┘               │  monthly)    │
                       │                     └──────────────┘
                       ▼
                  ┌──────────┐
                  │ Lambda   │
                  │ ingest   │  Bedrock (Claude Sonnet 4)
                  └──────────┘  ── natural-language recommendation explanations
```

## Discovery — 12 user decisions

| # | Item | Default |
|---|---|---|
| 1 | Industry | E-commerce |
| 2 | Node types | User+Item+Category |
| 3 | Edge | explicit + implicit (BOUGHT/VIEWED/CART/RATED) |
| 4 | Data source | UCP / DynamoDB / Aurora / S3 |
| 5 | Update mode | Real-time (Kinesis) |
| 6 | Recommendation scenario | collaborative + cross-sell + popular |
| 7 | Explainability | Bedrock Sonnet 4 |
| 8 | Latency | Standard (<1s) |
| 9 | Neptune mode | Serverless v2 |
| 10 | Neptune ML | Skip (optional) |
| 11 | Frontend | Yes (Admin + Demo) |
| 12 | Auth | Cognito |

## Per-industry graph schema (the biggest specific value)

```
E-commerce:   User -[BOUGHT/VIEWED/CART/RATED]→ Item -[IN_CATEGORY]→ Category
              User -[IN_SEGMENT]→ Segment

Media:        User -[WATCHED w/ completionRatio]→ Content -[BY]→ Person
              User -[FOLLOWS]→ User    (social graph)
              User -[FOLLOWED]→ Person (creator)

B2B SaaS:     Account -[USES]→ Feature -[REQUIRES_PLAN]→ Plan
              Account -[IN_INDUSTRY]→ Industry
              Account -[UPGRADED_FROM]→ Plan

Recruiting:   Candidate -[HAS_SKILL/WORKED_AT]→ Skill/Company
              JobPosting -[REQUIRES_SKILL]→ Skill

Healthcare:   Patient -[HAS_DIAGNOSIS]→ Disease (very strict privacy, hash ID)
              Patient -[PRESCRIBED]→ Medication
```

## Cost estimates

| Scenario | Monthly cost |
|---|---|
| Dev / PoC (small) | ~$73 (Neptune SLv2 floor + Bedrock low) |
| Production medium (1M event/d, 10K rec/d) | ~$717 (Sonnet 4) / ~$300 (Haiku 4.5) |
| Production large (10M event/d, 100K rec/d) | ~$2,500-3,000 |
| + Neptune ML monthly training | +$30-80 |

## Installation

### Claude Code
```bash
mkdir -p ~/.claude/skills
cp -r claude-code/skills/graph-personalization ~/.claude/skills/
cp -r shared ~/.claude/skills/graph-personalization/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
cp -r kiro/skills/graph-personalization ~/.kiro/skills/
cp -r shared ~/.kiro/skills/graph-personalization/shared
```

### Codex
```bash
mkdir -p ~/.agents/skills
cp -r codex/skills/graph-personalization ~/.agents/skills/
cp -r shared ~/.agents/skills/graph-personalization/shared
```

## MCP requirements

| MCP | Purpose | Required |
|-----|------|-----------|
| AWS Knowledge MCP | Neptune region availability, Bedrock model IDs, Neptune ML region verification | Recommended |
| CloudFormation MCP | Stack validation/deployment | Optional |

## Core design principles

1. **Single SKILL.md** — Anthropic Agent Skills standard, identical across 3 tools (md5-verified)
2. **Shared knowledge** — ~5,400 lines live in one place under `shared/`; SKILL.md is a thin wrapper (~155L)
3. **Industry-driven schema** — the graph schema differs completely per industry. This is the biggest specific value of this skill
4. **Privacy-first** — other user IDs are never exposed in the Bedrock prompt (aggregation only, count >= 3)
5. **Cold start fallback** — for users with fewer than 3 edges → always include popular + segment-based fallback
6. **Bedrock prompt caching** — 90% cost reduction
7. **Cypher parameterized** — injection prevention + recency decay + standardized edge weights

## Editing workflow

```bash
# 1. Edit only the canonical copy (claude-code/)
$EDITOR claude-code/skills/graph-personalization/SKILL.md

# 2. Sync to the other two locations
../scripts/sync-skills.sh graph-personalization-skill

# 3. Verify
../scripts/sync-skills.sh verify
```

## Reference statistics

- shared/ total: ~5,400 lines
- SKILL.md: 155 lines (thin wrapper, md5 `709b35e5...`)
- evals: 309 lines
- examples: 683 lines (3 industry instantiations)
- 25 hard constraints (pitfall avoidance)
- 7 CDK stacks (Network/Graph/Ingestion/ML/Auth/API/Frontend)
- 12 Discovery decision points
- 5 industry schema templates (E-commerce/Media/B2B SaaS/Recruiting/Healthcare)
