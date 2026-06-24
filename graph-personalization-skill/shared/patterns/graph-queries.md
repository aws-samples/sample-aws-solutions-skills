# Graph Queries (Cypher Templates)

> openCypher query templates per recommendation scenario. Every query applies **parameterized + privacy threshold + recency decay**. Vertex/edge labels change according to the per-industry schema.

## Common patterns

### Privacy threshold

Every collaborative query enforces `count >= N` (usually 3-5). Recommending from 1-2 people's data can identify those users.

### Recency decay

`exp(-0.05 * (timestamp() - r.at) / 86400000)` — weight ~0.22 after 30 days, ~0.05 after 60 days.

```
day 0:   weight × 1.00
day 7:   weight × 0.70
day 30:  weight × 0.22
day 60:  weight × 0.05
day 90:  weight × 0.01
```

### Edge weight standard per behavior

```
PURCHASED / BOUGHT     5.0
LIKED / FAVORITED      4.0
RATED (1-5)            r.value (1-5)
CART / SAVED           3.0
WATCHED (long)         2.0  (completionRatio >= 0.8)
VIEWED                 1.0  × duration_factor
CLICKED                0.5
```

## E-commerce queries

### 1. Collaborative — "what similar buyers bought"

```cypher
// $userId, $limit (default 10)
MATCH (u:User {id: $userId})-[:BOUGHT]->(:Item)<-[:BOUGHT]-(other:User)
WHERE u <> other
WITH u, other, count(*) AS sharedItems
WHERE sharedItems >= 3                                    -- privacy threshold

MATCH (other)-[r:BOUGHT]->(rec:Item)
WHERE NOT (u)-[:BOUGHT]->(rec)
  AND rec.status = 'active'

WITH rec,
     sum(sharedItems * r.weight * exp(-0.05 * (timestamp() - r.at) / 86400000)) AS score,
     count(distinct other) AS recommenderCount
WHERE recommenderCount >= 3                               -- privacy

ORDER BY score DESC
LIMIT $limit
RETURN rec.id AS itemId,
       rec.name AS itemName,
       rec.price AS price,
       score,
       recommenderCount
```

### 2. Cross-sell — "what was bought together with this item"

```cypher
// $itemId, $limit
MATCH (item:Item {id: $itemId})<-[:BOUGHT]-(buyer:User)-[:BOUGHT]->(other:Item)
WHERE item <> other
  AND other.status = 'active'

WITH other, count(distinct buyer) AS coBuyCount
WHERE coBuyCount >= 5

RETURN other.id AS itemId,
       other.name AS itemName,
       other.price AS price,
       coBuyCount AS score
ORDER BY coBuyCount DESC
LIMIT $limit
```

### 3. Content-based — "new items in preferred categories"

```cypher
// $userId, $limit, $daysSince (default 30)
MATCH (u:User {id: $userId})-[:BOUGHT]->(:Item)-[:IN_CATEGORY]->(c:Category)
WITH u, c, count(*) AS catCount
ORDER BY catCount DESC
LIMIT 3                                                    -- top 3 categories

MATCH (c)<-[:IN_CATEGORY]-(rec:Item)
WHERE rec.createdAt > timestamp() - $daysSince * 86400000  -- recent
  AND NOT (u)-[:BOUGHT]->(rec)
  AND rec.status = 'active'

RETURN rec.id AS itemId,
       rec.name AS itemName,
       c.name AS category
ORDER BY rec.createdAt DESC
LIMIT $limit
```

### 4. Popular fallback — "popular items in this segment" (Cold start)

```cypher
// $segment, $limit
MATCH (u:User)-[:IN_SEGMENT]->(:Segment {id: $segment})
MATCH (u)-[r:BOUGHT]->(rec:Item)
WHERE r.at > timestamp() - 30 * 86400000                  -- last 30 days
  AND rec.status = 'active'

WITH rec, count(distinct u) AS buyerCount
WHERE buyerCount >= 10                                     -- popular threshold

RETURN rec.id AS itemId,
       rec.name AS itemName,
       buyerCount AS score
ORDER BY buyerCount DESC
LIMIT $limit
```

### 5. Similar items — "same category + similar purchase pattern"

```cypher
// $itemId, $limit
MATCH (item:Item {id: $itemId})-[:IN_CATEGORY]->(c:Category)<-[:IN_CATEGORY]-(rec:Item)
WHERE item <> rec
WITH item, rec, c

// count of users who bought both
OPTIONAL MATCH (item)<-[:BOUGHT]-(buyer:User)-[:BOUGHT]->(rec)
WITH rec, c.name AS category, count(distinct buyer) AS coBuyers

RETURN rec.id AS itemId, rec.name AS itemName, category, coBuyers AS score
ORDER BY coBuyers DESC LIMIT $limit
```

## Media queries

### 6. "content watched by people you follow"

```cypher
// $userId, $limit
MATCH (u:User {id: $userId})-[:FOLLOWS]->(friend:User)-[w:WATCHED]->(c:Content)
WHERE NOT (u)-[:WATCHED]->(c)
  AND w.completionRatio >= 0.8                            -- only those watched 80%+
  AND w.at > timestamp() - 30 * 86400000

WITH c, count(distinct friend) AS friendCount, avg(w.completionRatio) AS avgCompletion
WHERE friendCount >= 3

RETURN c.id, c.title, friendCount, avgCompletion
ORDER BY friendCount DESC, avgCompletion DESC
LIMIT $limit
```

### 7. "new releases in preferred genres"

```cypher
// $userId, $limit
MATCH (u:User {id: $userId})-[:WATCHED]->(:Content)-[:HAS_GENRE]->(g:Genre)
WITH u, g, count(*) AS gCount
ORDER BY gCount DESC LIMIT 3

MATCH (g)<-[:HAS_GENRE]-(rec:Content)
WHERE rec.releasedAt > timestamp() - 30 * 86400000
  AND NOT (u)-[:WATCHED]->(rec)

RETURN rec.id, rec.title, g.name AS genre, rec.releasedAt
ORDER BY rec.releasedAt DESC
LIMIT $limit
```

### 8. "other works by this writer/director"

```cypher
// $personId, $userId, $limit
MATCH (p:Person {id: $personId})<-[:BY]-(c:Content)
WHERE NOT EXISTS { MATCH (:User {id: $userId})-[:WATCHED]->(c) }
RETURN c.id, c.title, c.releasedAt
ORDER BY c.releasedAt DESC LIMIT $limit
```

## B2B SaaS queries

### 9. "features used by larger customers in a similar industry"

```cypher
// $accountId, $limit
MATCH (a:Account {id: $accountId})-[:IN_INDUSTRY]->(ind:Industry)
MATCH (other:Account)-[:IN_INDUSTRY]->(ind)
WHERE a <> other AND other.mrrUsd > a.mrrUsd

MATCH (other)-[u:USES]->(f:Feature)
WHERE NOT (a)-[:USES]->(f)
  AND f.isPaidOnly = true

WITH f, count(distinct other) AS adopterCount, avg(u.freqPerWeek) AS avgUsage
WHERE adopterCount >= 3

RETURN f.id, f.name, adopterCount, avgUsage
ORDER BY adopterCount DESC, avgUsage DESC
LIMIT $limit
```

### 10. "upgrade likelihood score"

```cypher
// $accountId
MATCH (a:Account {id: $accountId})-[:ON_PLAN]->(currentPlan:Plan)
OPTIONAL MATCH (a)-[:USES]->(f:Feature)-[:REQUIRES_PLAN]->(p:Plan)
WHERE p.tier > currentPlan.tier                            -- using features that require a higher tier

WITH a, currentPlan, count(distinct f) AS proFeaturesUsed
RETURN a.id,
       currentPlan.name AS currentPlan,
       proFeaturesUsed,
       CASE WHEN proFeaturesUsed >= 3 THEN 'HIGH'
            WHEN proFeaturesUsed >= 1 THEN 'MEDIUM'
            ELSE 'LOW' END AS upgradeLikelihood
```

## Graph Explorer (Frontend) queries

### 11. "user-centric 1-hop neighborhood" (for visualization)

```cypher
// $userId
MATCH (u:User {id: $userId})
OPTIONAL MATCH (u)-[r:BOUGHT|VIEWED|CART]->(i:Item)
WITH u, collect({type: type(r), at: r.at, weight: r.weight, item: i})[0..20] AS interactions

RETURN u {.*} AS user,
       [x IN interactions | { item: x.item {.*}, edge: { type: x.type, at: x.at, weight: x.weight } }] AS neighborhood
```

### 12. "similar-users graph" (for visualization)

```cypher
// $userId, $maxNodes (default 20)
MATCH (u:User {id: $userId})-[:BOUGHT]->(:Item)<-[:BOUGHT]-(similar:User)
WHERE u <> similar
WITH u, similar, count(*) AS sharedItems
WHERE sharedItems >= 3
ORDER BY sharedItems DESC
LIMIT $maxNodes

WITH u, collect(similar) AS similars
UNWIND similars AS s
MATCH (u)-[:BOUGHT]->(item:Item)<-[:BOUGHT]-(s)
WITH u, s, collect(item)[0..5] AS sharedItems

RETURN u {.id, .segment} AS center,
       collect({user: s {.id}, sharedCount: size(sharedItems), sharedItems: [i IN sharedItems | i {.id, .name}]}) AS similars
```

## Admin / stats queries

### 13. "Graph statistics" (admin dashboard)

```cypher
MATCH (u:User) WITH count(u) AS userCount
MATCH (i:Item) WITH userCount, count(i) AS itemCount
MATCH ()-[r]->() WITH userCount, itemCount, type(r) AS edgeType, count(r) AS edgeCount
RETURN userCount, itemCount, collect({type: edgeType, count: edgeCount}) AS edges
```

### 14. "Cold start users (edge < 3)"

```cypher
MATCH (u:User)
OPTIONAL MATCH (u)-[r]->()
WITH u, count(r) AS edgeCount
WHERE edgeCount < 3
RETURN count(u) AS coldStartUserCount
```

### 15. "Top viewed items (last 7 days)"

```cypher
MATCH (:User)-[v:VIEWED]->(i:Item)
WHERE v.at > timestamp() - 7 * 86400000
WITH i, count(v) AS viewCount
ORDER BY viewCount DESC
LIMIT 20
RETURN i.id, i.name, viewCount
```

## Bulk loader CSV format

CSV format for initial load:

### vertices.csv

```csv
~id,~label,name:String,segment:String,registeredAt:Long
u-1,User,"Alice","VIP",1700000000000
u-2,User,"Bob","Regular",1700100000000
i-1,Item,"Widget A",,
i-2,Item,"Widget B",,
c-1,Category,"Electronics",,
c-2,Category,"Books",,
```

### edges.csv

```csv
~id,~from,~to,~label,weight:Double,at:Long
e-1,u-1,i-1,BOUGHT,5.0,1700000000000
e-2,u-1,i-2,VIEWED,1.0,1700001000000
e-3,i-1,c-1,IN_CATEGORY,1.0,1700000000000
```

### Bulk Loader API call

```bash
curl -X POST https://${NEPTUNE_ENDPOINT}:8182/loader \
  -H "Content-Type: application/json" \
  -d '{
    "source": "s3://my-bucket/vertices.csv",
    "format": "csv",
    "iamRoleArn": "arn:aws:iam::123456789012:role/neptune-bulk-loader",
    "region": "ap-northeast-2",
    "failOnError": true,
    "parallelism": "MEDIUM",
    "updateSingleCardinalityProperties": "FALSE"
  }'
```

> **Recommended**: streaming is OK below 100K vertices. For 100K+, the bulk loader is 100x faster.

## Calling Cypher from Lambda (Python)

```python
# backend/lambdas/recommend/handler.py
import os
import json
from gremlin_python.driver import client, serializer
from neptune_client import NeptuneOpenCypherClient   # custom wrapper

NEPTUNE_ENDPOINT = os.environ['NEPTUNE_ENDPOINT']
NEPTUNE_PORT = os.environ['NEPTUNE_PORT']
INDUSTRY = os.environ['INDUSTRY']                    # ecommerce, media, b2b-saas

# IAM auth — SigV4 sign HTTP request
neptune = NeptuneOpenCypherClient(NEPTUNE_ENDPOINT, NEPTUNE_PORT, region=os.environ['AWS_REGION_NAME'])

# load per-industry query templates
QUERIES = {
    'ecommerce': {
        'collaborative': open('queries/ecommerce/collaborative.cypher').read(),
        'cross-sell': open('queries/ecommerce/cross-sell.cypher').read(),
        'popular': open('queries/ecommerce/popular.cypher').read(),
    },
    'media': { ... },
    'b2b-saas': { ... },
}


def handler(event, context):
    body = json.loads(event['body'])
    user_id = body['user_id']
    scenario = event['pathParameters']['scenario']    # collaborative / cross-sell / popular
    limit = body.get('limit', 10)

    # Cold start check
    edge_count = count_user_edges(user_id)
    if edge_count < 3 and scenario == 'collaborative':
        scenario = 'popular'
        # popular needs a segment
        body['segment'] = get_user_segment(user_id) or 'NewUser'

    query = QUERIES[INDUSTRY][scenario]
    params = {'userId': user_id, 'limit': limit, **body}

    result = neptune.run(query, **params)

    # reformat results for Bedrock prompt
    items = [
        {'id': r['itemId'], 'name': r['itemName'], 'score': r['score']}
        for r in result
    ]

    # Bedrock invoke for explanation
    explanation = generate_explanation(items, scenario, INDUSTRY)

    return {
        'statusCode': 200,
        'body': json.dumps({
            'items': items,
            'explanation': explanation,
            'scenario': scenario,
            'count': len(items),
        }),
    }
```

Detailed Bedrock prompt patterns are in `shared/patterns/bedrock-explanation.md`.

## Pitfall avoidance (see constraints #4, #7, #8)

| Pitfall | Handling in the query |
|---|---|
| Privacy (exposing other user IDs) | enforce `WHERE count >= 3`, do not include user IDs in RETURN |
| Recency decay | `exp(-0.05 * (timestamp() - r.at) / 86400000)` |
| Cypher injection | parameterized (`$userId`, `$limit`) |
| Bedrock context size | top 20 limit + summary helper |
| Edge weight consistency | standard weight per behavior × recency |
| Cold start | edge_count < 3 → popular fallback |
