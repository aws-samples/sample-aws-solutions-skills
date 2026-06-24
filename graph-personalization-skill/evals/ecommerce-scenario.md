# Eval: E-commerce Scenario

## Input Prompt
```
Build a recommendation system for an e-commerce shopping mall.
- Cosmetics/fashion category, B2C
- Per-user recommendations (similar users + cross-sell + cold start)
- Natural-language explanations with Bedrock
- Korean + English multilingual
- User/product data already exists in DynamoDB
- Region: ap-northeast-2
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Skip items already answered among the 12 questions
- [ ] Industry → E-commerce → schema applied automatically (`shared/reference/graph-schemas.md`)
- [ ] Recommend endpoints default = collaborative + cross-sell + popular
- [ ] Guide on DynamoDB Streams integration pattern
- [ ] Recommend Neptune Serverless v2 (variable load + cost efficiency)
- [ ] Neptune ML option — recommend reviewing after Phase 2
- [ ] Confirm Neptune Serverless v2 ap-northeast-2 availability via AWS Knowledge MCP
- [ ] Confirm Bedrock Sonnet 4 cross-region inference profile via AWS Knowledge MCP

### Phase 2 (Design — GATE 2)
- [ ] Stack composition table:
  ```
  | Stack                 | Selected? |
  | NetworkStack          | ✅ 3 AZ   |
  | GraphStack            | ✅ Neptune SLv2 0.5–16 NCU |
  | IngestionStack        | ✅ Kinesis 1 shard + DLQ |
  | MLStack               | ❌ skip (Phase 2) |
  | AuthStack             | ✅ Cognito |
  | ApiStack              | ✅ /recommendations/{collaborative,cross-sell,popular} |
  | FrontendStack         | ✅ Admin/Demo UI |
  ```
- [ ] Schema table accurate:
  - Vertices: User, Item, Category, Brand, Segment
  - Edges: BOUGHT (5.0), VIEWED (1.0×dur), CART (3.0), RATED (value), IN_CATEGORY, HAS_BRAND, IN_SEGMENT
- [ ] Cost estimate ~$2,200/mo (Sonnet 4) or ~$880/mo (Haiku 4.5)
- [ ] Guide on DynamoDB Streams → Lambda → Kinesis → Ingestion integration pattern

### Phase 3 (Generated Files)

**CDK**:
- [ ] `cdk/bin/app.ts` — 7 stacks (industry='ecommerce')
- [ ] `cdk/lib/network-stack.ts` — VPC 3 AZ, NAT GW HA, Flow Logs REJECT
- [ ] `cdk/lib/graph-stack.ts` — Neptune SLv2 cluster, IAM auth enabled, KMS CMK with RETAIN, Multi-AZ subnet group
- [ ] `cdk/lib/ingestion-stack.ts` — Kinesis 1 shard, Lambda + DLQ + reservedConcurrency 50, Bulk Loader bucket + IAM role
- [ ] `cdk/lib/auth-stack.ts` — Cognito User Pool
- [ ] `cdk/lib/api-stack.ts` — API Gateway + 3 Lambdas (recommend, explore, admin), Cognito authorizer, SG-to-SG (Lambda → Neptune)
- [ ] `cdk/lib/frontend-stack.ts` — S3 + CloudFront + OAC, SSM Parameter for frontend config

**Backend Lambdas (Python)**:
- [ ] `backend/lambdas/ingest/handler.py` — Kinesis → Neptune batch upsert (UNWIND, batch_size=100, partial retry via batchItemFailures)
- [ ] `backend/lambdas/recommend/handler.py` — query type branching (collaborative/cross-sell/popular), Cold start fallback (edge < 3 → popular)
- [ ] `backend/lambdas/explore/handler.py` — graph viz API (top 20 + neighborhood)
- [ ] `backend/lambdas/admin/handler.py` — graph stats query
- [ ] `backend/shared/neptune_client.py` — SigV4-signed openCypher client
- [ ] `backend/queries/ecommerce/{collaborative,cross-sell,popular,similar-items}.cypher` — parameterized

**Frontend (React)**:
- [ ] `frontend/package.json` — vis-network, vis-data, Amplify v6, shadcn deps
- [ ] `frontend/src/App.tsx` — Amplify Authenticator + 3 routes
- [ ] `frontend/src/api/client.ts` — typed API + JWT auth
- [ ] `frontend/src/pages/RecommendationDemo.tsx` — user_id input + scenario tab + card list + Bedrock explanation box
- [ ] `frontend/src/pages/GraphExplorer.tsx` — vis-network + 2 modes (neighborhood/similar-users) + legend
- [ ] `frontend/src/pages/AdminDashboard.tsx` — stats cards + edges table

**Configuration**:
- [ ] `cdk.json` context: industry=ecommerce, enableNeptuneML=false
- [ ] Bedrock model ID with `us.` prefix (cross-region inference profile)
- [ ] `frontend/public/config.json` placeholder

**Scripts**:
- [ ] `scripts/{deploy, destroy, check-prerequisites, generate-frontend-config}.sh`

### Phase 4 (Validate — GATE 3)
- [ ] `cdk synth` clean (all stacks)
- [ ] Verify the IAM actions used (`neptune-db:*`, `bedrock:InvokeModel`) via AWS Knowledge MCP
- [ ] Confirm the latest Bedrock Sonnet 4 inference profile ID via AWS Knowledge MCP

### Phase 5 (Deploy)
- [ ] CDK bootstrap command
- [ ] Stack deploy order (Network → Graph → Ingestion → Auth → API → Frontend)
- [ ] Initial bulk load procedure (CSV → S3 → Bulk Loader API)
- [ ] Guide on enabling Anthropic model access
- [ ] Create Cognito test user
- [ ] Smoke test: `curl -X POST .../recommendations/popular -H "Authorization: ..." -d '{"user_id":"u-1","limit":5}'`

## Code Quality Checks

- [ ] All Cypher queries parameterized (`$userId`, `$limit`)
- [ ] Privacy threshold (`count >= 3` or `5`) enforced on every collaborative query
- [ ] Recency decay (`exp(-0.05 * (timestamp() - r.at) / 86400000)`)
- [ ] Edge weight standard (BOUGHT 5.0, CART 3.0, VIEWED 1.0×dur)
- [ ] Lambda batchItemFailures partial retry
- [ ] Neptune writer connection pool (reservedConcurrency)
- [ ] Bedrock prompt caching (system prompt + few-shot, ephemeral 5min)
- [ ] Top 20 limit on every explanation prompt
- [ ] Cold start fallback (edge < 3 → popular)
- [ ] User ID not exposed in the Bedrock prompt (validate_explanation_privacy function)
- [ ] CDK Tags: Project / Environment / Component / DataClassification

## Hard Constraints Verification

- [ ] constraints.md #1: Proceed to Phase 3 after finalizing the schema
- [ ] constraints.md #2: Guide on the Neptune SLv2 min 0.5 NCU = $44/month floor
- [ ] constraints.md #3: Always include a Cold start fallback
- [ ] constraints.md #4: Privacy aggregation only
- [ ] constraints.md #5: Batch upsert (UNWIND, batch_size=100)
- [ ] constraints.md #6: SigV4-signed Neptune client
- [ ] constraints.md #7: Top 20 + summary helper
- [ ] constraints.md #8: Edge weight + recency decay
- [ ] constraints.md #12: Bedrock prompt caching
- [ ] constraints.md #14: Parameterized Cypher
- [ ] constraints.md #19: KMS CMK RETAIN
- [ ] constraints.md #20: Production deletion_protection + RETAIN

## Functional Verification (Smoke tests)

```bash
# 1. Cognito JWT
TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=test,PASSWORD=Test1234! \
  --query 'AuthenticationResult.IdToken' --output text)

# 2. Popular recommendation (Cold start)
curl -X POST "$API_URL/recommendations/popular" \
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":"u-newuser-1","limit":5}'

# 3. Collaborative
curl -X POST "$API_URL/recommendations/collaborative" \
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":"u-1","limit":10}'

# 4. Cross-sell
curl -X POST "$API_URL/recommendations/cross-sell" \
  -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '{"item_id":"i-1","limit":5}'

# 5. Graph explore
curl "$API_URL/graph/explore?user_id=u-1&limit=20" \
  -H "Authorization: $TOKEN"

# 6. Admin stats
curl "$API_URL/admin/stats" -H "Authorization: $TOKEN"
```

## Privacy Verification

Tests:
- [ ] The Bedrock response does not explicitly name other user IDs such as `u-2`, `u-3`
- [ ] Only aggregation expressions such as "N similar users", "5+ friends"
- [ ] Email / Phone / national-ID patterns do not appear in the response (when Guardrails applied)
- [ ] `validate_explanation_privacy()` function unit test
