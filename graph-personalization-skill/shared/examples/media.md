# Example: Media Recommendation

> A recommendation system for content platforms such as music / video / news. The core is **the social graph (FOLLOWS) + viewing behavior (WATCHED)**.

## Discovery answers

| Question | Answer |
|---|---|
| Project name | acme-stream |
| Industry | Media (video streaming) |
| Region | ap-northeast-2 |
| Schema (vertices) | User + Content + Genre + Person + Tag |
| Schema (edges) | WATCHED (with completionRatio) + FOLLOWED + RATED + FAVORITE + BY (director) |
| Data source | Kinesis Data Streams (app viewing events) |
| Update mode | Real-time (3 shards — high viewing event volume) |
| Recommend endpoints | follows-watched + genre-affinity + person-affinity |
| Explanation | Bedrock Sonnet 4 (Korean + English, no spoilers) |
| Latency | Standard (<1s) — recommendation page load |
| Neptune mode | Serverless v2 (1–32 NCU, large dataset) |
| Neptune ML | **Enable** (content embedding) |
| Frontend | React Admin/Demo |
| Auth | Cognito + Apple/Google federation |

## Graph schema

```
Vertex labels:
  User      {id, ageGroup, region, language}
  Content   {id, title, type, durationSec, releasedAt, status}
  Genre     {id, name}
  Person    {id, name, role}             # director, actor, artist
  Tag       {id, name}                   # user tags / keywords

Edge labels:
  (User)-[WATCHED {at, weight, completionRatio, devicId}]->(Content)
  (User)-[RATED {at, value=1-5}]->(Content)
  (User)-[FAVORITE {at}]->(Content)
  (User)-[FOLLOWED {at}]->(Person)
  (User)-[FOLLOWS {at}]->(User)          # ★ social graph
  (Content)-[HAS_GENRE]->(Genre)
  (Content)-[BY {role: 'director'|'actor'}]->(Person)
  (Content)-[TAGGED {weight}]->(Tag)
```

## Endpoints

```
POST /recommendations/follows-watched   { user_id, limit }
   → "content watched by people you follow"

POST /recommendations/genre-affinity    { user_id, limit }
   → "new releases in your preferred genres"

POST /recommendations/person-affinity   { person_id, user_id, limit }
   → "other works by this writer/director"

POST /recommendations/popular           { user_id, limit }
   → Cold start (popular content by region + age group)
```

## Demo scenarios

### 1. Friend-watched

```
User: "u-001 (30s, follows 12 people)"
   ↓
POST /recommendations/follows-watched

Response:
{
  "items": [
    {"id": "movie-noir-2024", "name": "그림자의 도시", "score": 5.2, "genre": "스릴러"},
    {"id": "movie-drama-2024", "name": "바다의 여인", "score": 4.5, "genre": "드라마"},
    ...
  ],
  "explanation": {
    "explanation": "당신이 follow 하는 분들 중 5명 이상이 최근 시청을 마친 작품들입니다. 특히 '그림자의 도시'는 80% 이상 완주율을 기록한 인기작이에요.",
    "reason_tag": "friend-watched"
  }
}
```

→ The "what your friends watched" pattern increases **engagement** (social proof).

### 2. Person-affinity (following a creator)

```
The user follows "Christopher Nolan"
   ↓
POST /recommendations/person-affinity { person_id: "p-nolan", user_id: "u-001" }

Response:
{
  "items": [
    {"id": "movie-tenet", "name": "Tenet", ...},
    {"id": "movie-dunkirk", "name": "Dunkirk", ...}
  ],
  "explanation": {
    "explanation": "당신이 follow 하시는 Christopher Nolan 감독의 다른 작품입니다. 아직 시청하지 않으신 영화 위주로 추천드려요.",
    "reason_tag": "person-affinity"
  }
}
```

### 3. Genre-affinity new releases

```
User: top 3 genres of content watched in the last 30 days = [SF, 스릴러, 미스터리]
   ↓
POST /recommendations/genre-affinity { user_id: "u-001" }

Response:
{
  "items": [
    {"id": "new-sci-fi-2024", "name": "..., 새 SF 시리즈", "releasedAt": "2024-12-01", ...}
  ],
  "explanation": {
    "explanation": "최근 즐겨보시는 SF, 스릴러 장르의 신작입니다. 평론가들이 호평한 작품 위주로 골랐어요.",
    "reason_tag": "genre-affinity"
  }
}
```

## Graph Explorer (social graph visualization)

```
"the social graph of User u-001 + their recent viewing"

       (Friend A)──WATCHED──(Movie X)
           │
       FOLLOWS
           │
        u-001 ◀──FOLLOWS──── (Friend B)──WATCHED──(Movie Y)
           │
       FOLLOWED
           │
       (Christopher Nolan)
           │
           BY (director)
           │
       (Tenet) (Dunkirk) (Inception)
```

## Using Neptune ML

Generate user/content embeddings via GNN training:

```python
# train-gnn Lambda (monthly EventBridge schedule)
import boto3
neptune = boto3.client('neptune-data')

# 1. Data export
export_job = neptune.start_export({
    'targetBucketUri': 's3://acme-stream-ml/exports',
    'graphFilter': {'edgeLabels': ['WATCHED', 'FOLLOWED', 'HAS_GENRE']},
})

# 2. Training (SageMaker)
sagemaker.create_training_job(
    TrainingJobName=f'acme-stream-gnn-{date}',
    AlgorithmSpecification={'TrainingImage': 'neptune-ml-cpu:latest'},
    InstanceType='ml.g4dn.xlarge',
    InputDataConfig=[{'DataSource': {...}}],
    OutputDataConfig={'S3OutputPath': 's3://acme-stream-ml/models/'},
)

# 3. Transform — embeddings back to Neptune
neptune.start_transform({
    'modelId': model_id,
    'inputDataS3Location': '...',
    'outputDataS3Location': '...',
})
```

Afterwards, vector similarity search via `User.embedding` (vector) becomes possible → compensates for cold start.

## Cost estimate

```
Neptune Serverless v2 (avg 8 NCU)        $928/mo
   (large cluster needed due to high viewing event volume)
Lambda (high QPS — called on every content page) $200
Bedrock Sonnet 4 (with caching)          $500
Kinesis (3 shards)                       $33
S3 + CloudFront                          $50 (large viewing history)
Cognito                                  $30
DynamoDB cache                           $50
Neptune ML (monthly training)             $50
KMS                                      $2
─────────────────────────────────────────────
Total                                    ~$1,840/mo
```

## A/B test

| Treatment | Description |
|---|---|
| A | Graph friends-watched (social proof) |
| B | Pure collaborative filtering (behavior-based) |
| C | Hybrid (both + ranking) |

→ Measure: CTR, watch_completion_rate, session_length, retention.

## Additional privacy patterns

### Even "what your friends watched" must not name friends

```python
# ❌ Wrong
explanation = f"u-friend-1 님과 u-friend-2 님이 본 영화"

# ✅ Correct
explanation = "당신이 follow 하시는 분들 중 5명 이상이 본 영화"
```

### Only the user themselves may be named in their viewing history

Do not expose other users' viewing records in the explanation. **Aggregation (count) only**.

## Recommended follow-ups

- Hybrid recommendation (graph + content-based + popularity)
- Real-time ranking (recent watch boost)
- Cross-content recommendation (movie → music cross-domain)
- Recommendation result caching (DynamoDB 1h TTL)
