# Eval: Media Scenario

## Input Prompt
```
A recommendation system for a video streaming platform.
- Movie/drama/variety content
- Viewing behavior (WATCHED, completion ratio) + social graph (FOLLOWS)
- "What your friends watched" + "new releases in your preferred genre" recommendations
- Bedrock natural-language explanations (no spoilers)
- Region: ap-northeast-2
- Neptune ML enable (content embedding)
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Industry → Media → schema applied automatically
- [ ] Edge: WATCHED (with completionRatio), FOLLOWS (social), FOLLOWED (creator), RATED, FAVORITE
- [ ] Recommend endpoints: follows-watched + genre-affinity + person-affinity + popular
- [ ] Neptune ML: ✅ enable (per user answer)
- [ ] Latency: standard
- [ ] Auth: Cognito + guide on Apple/Google federation option

### Phase 2 (Design — GATE 2)

Stack composition:
- [ ] NetworkStack ✅
- [ ] GraphStack ✅ Neptune SLv2 (1–32 NCU) — large dataset
- [ ] IngestionStack ✅ Kinesis 3 shards (high event volume)
- [ ] **MLStack ✅ Neptune ML enabled**
- [ ] AuthStack ✅ Cognito + external IdP possible
- [ ] ApiStack ✅
- [ ] FrontendStack ✅

Schema finalized:
- [ ] Vertices: User, Content, Genre, Person, Tag
- [ ] Edges: WATCHED, FOLLOWS (user-user), FOLLOWED (user-person), HAS_GENRE, BY (director/actor), RATED, FAVORITE, TAGGED
- [ ] WATCHED weight = base × completionRatio (i.e., 80% completion = 1.6, 20% = 0.4)

Cost estimate:
- [ ] ~$1,840/mo (Sonnet 4 + caching + Neptune ML monthly training)

### Phase 3 (Generated Files)

**CDK**:
- [ ] `cdk/lib/ml-stack.ts` — Neptune ML training Lambda + EventBridge schedule (monthly cron)
- [ ] `cdk/lib/api-stack.ts` — 4 Lambdas (recommend, explore, admin, GraphRAG-style optional)
- [ ] `cdk/lib/auth-stack.ts` — Cognito + Apple/Google IdP (federation option)

**Backend**:
- [ ] `backend/lambdas/train-gnn/handler.py` — SageMaker training trigger (Neptune ML SDK)
- [ ] `backend/lambdas/recommend/handler.py` — friend-watched / genre-affinity / person-affinity branching
- [ ] `backend/queries/media/{follows-watched,genre-affinity,person-affinity,popular}.cypher`
- [ ] Explicitly multiply by completionRatio when computing edge weight
- [ ] No-spoiler prompt — do not expose plot in the explanation

**Frontend**:
- [ ] Graph Explorer visualizes the social network (emphasize FOLLOWS edges)
- [ ] Recommendation Demo: 4 scenarios (friend-watched/genre/person/popular)
- [ ] Content cards show genre + cast (no availability flag — subscription check is separate)

### Phase 4 (Validate — GATE 3)
- [ ] `cdk synth` passes
- [ ] Verify Neptune ML available region (ap-northeast-2 — confirm via MCP)
- [ ] SageMaker training instance type (ml.g4dn.xlarge) availability via AWS Knowledge MCP

### Phase 5 (Deploy)
- [ ] Standard deploy order
- [ ] **Additional**: first Neptune ML training run (manual trigger — after enough data has accumulated)
- [ ] Initial bulk load: viewing history (S3 → Bulk Loader)
- [ ] Smoke test: `curl POST /recommendations/follows-watched`

## Code Quality Checks

- [ ] WATCHED edge weight = base × completionRatio (specified in queries)
- [ ] Friend-watched uses only completionRatio >= 0.8 (sufficiently watched)
- [ ] Genre-affinity uses top 3 genres (based on user viewing history)
- [ ] No spoilers — specified in the Bedrock prompt
- [ ] User-User edge (FOLLOWS) — do not expose other user IDs in the prompt (count only)
- [ ] Neptune ML training schedule (monthly) — EventBridge cron
- [ ] SageMaker IAM role permissions explicit (NeptuneFullAccess + S3 read/write)
- [ ] Training cost CloudWatch alarm (notify when limit exceeded)

## Privacy Verification (Media specific)

```python
# Test cases for explanation
def test_no_friend_id_exposure():
    items = [{'id': 'movie-1', 'name': '영화 A', 'score': 5.2}]
    explanation = generate_explanation(items, 'follows-watched', 'media')
    
    # Friend IDs must not appear in the explanation
    raw_friend_ids = ['u-friend-1', 'u-friend-2']
    assert all(fid not in explanation['explanation'] for fid in raw_friend_ids)
    # Use aggregation expressions such as "N people you follow"
    assert any(phrase in explanation['explanation'] for phrase in [
        'follow', '친구', '비슷한', '명 이상', '분들'
    ])

def test_no_spoiler():
    # Movie plot details must not appear in the explanation
    explanation = generate_explanation(...)
    spoiler_keywords = ['결말', '스포일러', '죽는다', '범인', '반전']
    assert not any(kw in explanation['explanation'] for kw in spoiler_keywords)
```

## Functional Verification

```bash
# 1. Friend-watched
curl -X POST "$API_URL/recommendations/follows-watched" \
  -H "Authorization: $TOKEN" -d '{"user_id":"u-1","limit":10}'

# 2. Genre affinity
curl -X POST "$API_URL/recommendations/genre-affinity" \
  -H "Authorization: $TOKEN" -d '{"user_id":"u-1","limit":10}'

# 3. Person affinity
curl -X POST "$API_URL/recommendations/person-affinity" \
  -H "Authorization: $TOKEN" -d '{"user_id":"u-1","person_id":"p-nolan","limit":5}'

# 4. Graph Explorer (social graph)
curl "$API_URL/graph/explore?user_id=u-1&mode=social&limit=20" \
  -H "Authorization: $TOKEN"

# 5. Neptune ML training trigger (manual)
aws lambda invoke \
  --function-name acme-stream-train-gnn \
  --payload '{}' \
  output.json
# Result: SageMaker training job ARN
```

## Hard Constraints Verification (Media-specific)

- [ ] WATCHED edge weight reflects completionRatio (see queries.md #6)
- [ ] FOLLOWS edge → privacy hardening (do not expose other friend IDs)
- [ ] No spoilers — specified in the Bedrock system prompt
- [ ] Multilingual (Korean + English auto-detect)
- [ ] Neptune ML training cost cap (alert at $200/month)

## Eval Pass Criteria

- [ ] cdk synth passes
- [ ] All endpoints return 200
- [ ] Privacy validation function passes
- [ ] Bedrock responses are natural in Korean / English
- [ ] Neptune ML training Lambda triggers correctly
- [ ] Frontend Graph Explorer can visualize the social graph
