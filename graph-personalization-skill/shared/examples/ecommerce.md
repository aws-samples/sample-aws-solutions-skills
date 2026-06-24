# Example: E-commerce Personalization

> **The default industry**. A recommendation system for B2C shopping malls such as cosmetics/fashion/food.

## Discovery answers

| Question | Answer |
|---|---|
| Project name | acme-shop |
| Industry | E-commerce |
| Region | ap-northeast-2 |
| Schema (vertices) | User + Item + Category + Brand + Segment |
| Schema (edges) | BOUGHT (5.0), VIEWED (1.0×dur), CART (3.0), RATED (value) |
| Data source | DynamoDB Streams (existing e-commerce DB) |
| Update mode | Real-time (Kinesis 1 shard) |
| Recommend endpoints | collaborative + cross-sell + popular |
| Explanation | Bedrock Sonnet 4 (Korean + English multilingual) |
| Latency | Standard (<1s) |
| Neptune mode | Serverless v2 (0.5–16 NCU) |
| Neptune ML | Skip (review adoption after Phase 2) |
| Frontend | React Admin/Demo |
| Auth | Cognito User Pool |

## Generated graph schema

```
Vertex labels:
  User      {id, segment, country, registeredAt, language}
  Item      {id, name, price, stockQuantity, status}
  Category  {id, name, parentId}
  Brand     {id, name, country}
  Segment   {id, name}                # VIP, Regular, NewUser, AtRisk

Edge labels:
  (User)-[BOUGHT {at, weight=5.0×qty, qty}]->(Item)
  (User)-[VIEWED {at, weight, durationSec}]->(Item)
  (User)-[CART {at, weight=3.0×qty}]->(Item)
  (User)-[RATED {at, value=1-5}]->(Item)
  (User)-[IN_SEGMENT {at}]->(Segment)
  (Item)-[IN_CATEGORY {at}]->(Category)
  (Item)-[HAS_BRAND {at}]->(Brand)
  (Category)-[PARENT_OF]->(Category)
```

## Endpoints

```
POST /recommendations/collaborative   { user_id, limit }
   → "what similar buyers bought" + Bedrock explanation

POST /recommendations/cross-sell      { item_id, limit }
   → "what was bought together with this item"

POST /recommendations/popular         { user_id, limit }
   → popular items in the user's segment (Cold start)

GET  /graph/explore?user_id=X         → User neighborhood (visualization)
GET  /admin/stats                     → Graph statistics
```

## Demo scenarios

### 1. VIP customer recommendation

```
User input: "u-vip-001 (VIP segment)"
   ↓
GET /recommendations/collaborative

Response:
{
  "items": [
    {"id": "p-skin-1", "name": "프리미엄 토너 200ml", "score": 5.4},
    {"id": "p-skin-2", "name": "히알루론산 세럼", "score": 4.8},
    ...
  ],
  "explanation": {
    "explanation": "당신과 비슷한 스킨케어 구매 패턴의 VIP 회원 7명이 자주 함께 구매한 프리미엄 상품들이에요. 특히 프리미엄 토너는 재구매율이 80% 이상인 베스트셀러입니다.",
    "reason_tag": "similar-buyers"
  },
  "scenario": "collaborative"
}
```

Frontend (Recommendation Demo):
```
┌────────────────────────────────────────────────────────────┐
│ Recommendation Demo                                        │
├────────────────────────────────────────────────────────────┤
│ User ID: [u-vip-001          ] [Get Recommendations]       │
│ [Similar Users] [Cross-sell] [Popular]                     │
│                                                            │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ similar-buyers                                          │ │
│ │ 당신과 비슷한 스킨케어 구매 패턴의 VIP 회원 7명이 자주 │ │
│ │ 함께 구매한 프리미엄 상품들이에요. ...                  │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                            │
│ #1 프리미엄 토너 200ml         Score: 5.4  ████████████   │
│ #2 히알루론산 세럼              Score: 4.8  ██████████     │
│ #3 ...                                                     │
└────────────────────────────────────────────────────────────┘
```

### 2. Cross-sell (PDP page)

```
User input: item_id = "p-skin-1"
   ↓
GET /recommendations/cross-sell

Response:
{
  "items": [
    {"id": "p-skin-cleanser", "name": "클렌징 폼", "score": 12.0},
    {"id": "p-skin-cream", "name": "수분 크림", "score": 8.0},
    ...
  ],
  "explanation": {
    "explanation": "이 상품을 구매하신 분들 12명 이상이 함께 구매하신 상품입니다. 스킨케어 라인 완성에 추천드려요.",
    "reason_tag": "frequently-bought-together"
  }
}
```

### 3. Cold start new user

```
User input: "u-new-999" (signed up 1 day ago, edge < 3)
   ↓
GET /recommendations/collaborative
   → Lambda detects edge_count < 3 → fallback to popular

Response:
{
  "items": [...],
  "explanation": {
    "explanation": "처음 방문하신 분들에게 인기 있는 베스트셀러 상품입니다. 신규 고객 200명 이상이 첫 구매로 선택한 상품들이에요.",
    "reason_tag": "popular"
  },
  "scenario": "popular"          // ★ collaborative was requested but it fell back
}
```

## Graph Explorer demo

```
User input: "u-vip-001"
   ↓
1-hop neighborhood:

         (Premium Toner)
                ↑ BOUGHT (w=5.4)
                │
   (Cleansing Foam)──VIEWED──┐
                ↑ BOUGHT     │
                │            ▼
       u-vip-001 ◀──────── (Hyaluronic Serum)
                │
                ▼ CART
       (Sunscreen SPF50)
```

## Cost estimate

```
Neptune Serverless v2 (avg 4 NCU)        $464/mo
Lambda                                    $30
Bedrock Sonnet 4 (caching, 10K rec/d)    $1,650
   (or with Haiku 4.5)                   ($330)
Kinesis (1 shard)                        $11
S3 + CloudFront                          $20
Cognito                                  $5
DynamoDB cache                           $20
KMS                                      $2
─────────────────────────────────────────────
Total (Sonnet 4)                         ~$2,200/mo
Total (Haiku 4.5)                        ~$880/mo
```

## Migration from existing system

Existing e-commerce DB → Neptune:

```bash
# 1. Initial bulk load
# DynamoDB / RDS → S3 (Glue ETL)
aws s3 cp users.csv s3://${BULK_BUCKET}/vertices.csv
aws s3 cp items.csv s3://${BULK_BUCKET}/vertices.csv --append
aws s3 cp purchases.csv s3://${BULK_BUCKET}/edges.csv

# 2. Neptune Bulk Loader
curl -X POST https://${NEPTUNE_ENDPOINT}:8182/loader -d '{...}'

# 3. Real-time subscription
# DynamoDB Streams → Lambda → Kinesis → Ingestion Lambda → Neptune
```

## A/B test pattern

```
Treatment A: Graph collaborative recommendations
Treatment B: Existing system (matrix factorization)

CloudWatch metric:
  - CTR (click-through rate)
  - conversion rate
  - revenue per user

Treatment branching:
  Lambda decides by user_id hash % 2
  Results go EventBridge → Kinesis Firehose → S3 → Athena analysis
```

## Recommended follow-ups

- ElastiCache Redis (recommendation cache, 1h TTL → reduces Neptune query load)
- Bedrock Guardrails (additional PII validation)
- Hybrid with Personalize (ensemble graph + matrix factorization results)
- Adopt Neptune ML (Phase 2)
