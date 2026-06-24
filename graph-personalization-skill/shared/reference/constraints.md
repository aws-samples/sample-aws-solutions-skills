# Constraints & Gotchas

> This skill's 25 gotchas. Detailed explanation of the SKILL.md Hard Constraints.

## 1. Schema changes are hard — design carefully from the start

Neptune itself is schemaless, but because the code and queries **hardcode vertex/edge labels**, they effectively become a schema.

```python
# ❌ Trying to rename 'BOUGHT' → 'PURCHASED' while in production
# requires changing the label in every Cypher query, ingestion code, and GNN training
# plus a multi-thousand-row migration

# ✅ Decide the production schema up front
# Vertex labels: User, Item, Category, Brand, Segment
# Edge labels: BOUGHT, VIEWED, CART, IN_CATEGORY, HAS_BRAND, IN_SEGMENT
```

→ **Always finalize the schema in Phase 2 Design** before generating code in Phase 3. Plan a separate migration for later changes.

## 2. Neptune Serverless v2 — no auto-pause

Different from Aurora SLv2. v2 always keeps the min NCU (0.5) on even when idle = $44/mo minimum.

```
v1: auto-pause possible (0 NCU when idle) — but not GA / many limitations
v2: always maintains min NCU — more stable but has a cost floor
```

→ Even dev environments have a $44/mo floor. For a very small PoC, consider a single Provisioned instance (a small burst class such as db.t3.medium).

## 3. Cold start — handling 0-edge users

A new user has no or very few edges in the graph → traversal returns empty.

```python
# ❌ Wrong — graph traversal only; 0 results → empty recommendations
result = neptune.run_query("MATCH (u:User {id:$id})-[:BOUGHT]->(:Item)<-...")
return result  # empty list

# ✅ Correct — fallback chain
def recommend(user_id):
    edge_count = count_user_edges(user_id)
    if edge_count < 3:
        # Cold start — popular items in user's segment
        segment = get_user_segment(user_id)
        return popular_in_segment(segment, top_n=10)
    elif edge_count < 10:
        # Hybrid — graph + popular
        graph_recs = graph_traversal(user_id, top_n=5)
        popular = popular_in_segment(segment, top_n=5)
        return merge_dedupe(graph_recs, popular)
    else:
        # Full graph traversal
        return graph_traversal(user_id, top_n=10)
```

→ The skill **always generates a popular fallback Lambda**, regardless of the Discovery answer.

## 4. Privacy — never expose other users' IDs

```python
# ❌ Wrong — exposes other user IDs in the Bedrock prompt
prompt = f"""Products bought by users similar to u-123: u-456, u-789, u-101:
- Product A
- Product B
Explain this in natural language."""

# ✅ Correct — aggregation only, no ID exposure
prompt = f"""Products frequently bought together by 5+ users with purchase patterns similar to yours:
- Product A (purchase frequency 80%)
- Product B (purchase frequency 65%)
Explain this in natural language."""
```

→ Every Cypher query in the skill enforces an **aggregation threshold (count >= 3 or 5)** and the Lambda never injects user IDs into the prompt.

## 5. Real-time edge upsert throughput

The Neptune writer has a limit on how many mutations it can process within a single transaction.

```python
# ❌ Wrong — does not reuse the connection per event
for event in kinesis_records:
    neptune.run("CREATE (e:Event)-[r:BOUGHT]->(...)", event)
    # 10,000 events → 10,000 round-trips

# ✅ Correct — batch upsert (UNWIND)
events_batch = [parse(r) for r in kinesis_records]  # max 100 per batch
neptune.run("""
    UNWIND $events AS e
    MATCH (u:User {id: e.user_id})
    MATCH (i:Item {id: e.item_id})
    MERGE (u)-[r:BOUGHT {at: e.timestamp}]->(i)
    SET r.weight = coalesce(r.weight, 0) + e.weight
""", events=events_batch)
```

→ The skill's ingestion Lambda enforces a **batch_size=100 + UNWIND pattern**. A DLQ is added automatically too.

## 6. IAM database authentication — SigV4 SDK compatibility

Neptune IAM auth = SigV4-signing the HTTPS request. The library must support a SigV4 plug-in.

```python
# Python — gremlin-python + tornado SigV4 plug-in, or
# manual SigV4 with 'requests-aws4auth'

# Node.js — neo4j-driver does not support SigV4 natively → instead:
# 'aws-sdk-neptune' or sign SigV4 + HTTP directly

# JVM — the SigV4 plugin Neptune provides (gremlin-driver-aws-sigv4)
```

→ The skill's Lambda uses **Python + gremlin-python + boto3 SigV4** (most stable). Verify the plug-in if you use another language.

## 7. Bedrock context size — top-N trim

If graph traversal returns many results, they won't all fit in the Bedrock prompt. Korean uses 2x the tokens of English.

```python
# ❌ Wrong — top 100 items + all edge info in the prompt
prompt = f"items: {json.dumps(items_100)}, edges: {json.dumps(edges_500)}"
# 50K+ tokens → cost spike, higher latency

# ✅ Correct — top 20 + summary
prompt = f"""Here are the top 20 recommendation results:
{format_items_summary(items[:20])}
Common pattern: {summarize_edges(edges)}"""
```

→ The skill's explanation Lambda auto-generates a **top-20 default + summary helper**.

## 8. Edge weight normalization + recency decay

```cypher
// ❌ Wrong — treats every BOUGHT with the same weight
MATCH (u)-[r:BOUGHT]->(i) RETURN i.id, count(r) AS score

// ✅ Correct — apply recency decay + weight
MATCH (u)-[r:BOUGHT]->(i)
WITH i, sum(r.weight * exp(-0.05 * (timestamp() - r.at) / 86400000)) AS score
ORDER BY score DESC
RETURN i.id, score
```

→ The skill's query template **includes a recency-decay function** (weight ~0.22 after 30 days). Per-behavior weights are decided per the Discovery answer.

## 9. Multi-AZ cost (Provisioned)

Adding reader replicas in Provisioned mode multiplies cost ×N.

```
db.r6g.large writer:    $254/mo
+ db.r6g.large reader:  +$254/mo = $508
+ db.r6g.large reader:  +$254/mo = $762 (HA 3-AZ)
```

→ With Serverless v2, readers are also NCU-based — auto-scale.

## 10. GNN training cadence + cost

```
Once per month (recommended default):  ~$30-50/mo
Once per week:                          ~$120-200/mo
Once per day:                           ~$1000+/mo (usually unnecessary)
```

→ Daily training is meaningful in very rare cases (only large e-commerce). This skill's default = monthly.

## 11. Choosing a frontend graph viz library

| Library | Pros | Cons |
|---|---|---|
| **vis.js / vis-network** | simple, lightweight | performance drops on large graphs (>1K nodes) |
| **Cytoscape.js** | built-in graph algorithms, handles large graphs | steeper learning curve |
| **G6 (AntV)** | docs, visualization templates | many options — decision overhead |
| **D3.js** | fully customizable | implement force layout etc. yourself |

→ Skill default = **vis-network** (simple, enough to demo 100 vertices). Use Cytoscape if you need to demo large graphs.

## 12. Applying Bedrock prompt caching

If the system prompt + few-shot exceed 100K tokens, enabling caching cuts cost by 90%.

```python
response = bedrock.invoke_model(
    modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": user_prompt}],
    }),
)
```

→ The skill's explanation Lambda **applies caching automatically**. It is a 5-minute ephemeral cache, but with active traffic the hit rate is 90%+.

## 13. Neptune connection pool

Lambda concurrent executions × Neptune connection limit can collide.

```
Lambda concurrent 1000 → attempts 1000 Neptune connections
→ Neptune writer rejects / latency spikes
```

→ The skill's Lambda sets a **gremlin-python connection pool** + **reserved concurrency**. Alternatively, create + clean up a connection per Lambda (cold-start burden).

## 14. Cypher injection (external input directly in the prompt)

```python
# ❌ Wrong — string-interpolating user input into Cypher
query = f"MATCH (u:User {{id: '{user_input}'}}) RETURN u"
# user_input = "x'}) DETACH DELETE (n) //" → injection

# ✅ Correct — parameterized query
query = "MATCH (u:User {id: $user_id}) RETURN u"
neptune.run(query, user_id=user_input)
```

→ All Cypher is parameterized.

## 15. Bulk Loader vs streaming initial load

Under 100K vertices → streaming is fine. 100K+ → the Bulk Loader API is 100x faster.

```python
# Bulk Loader API
neptune_client.start_loader_job(
    source="s3://bucket/vertices.csv",
    iam_role_arn="arn:aws:iam::...:role/neptune-loader",
    format="csv",
    failOnError=True,
)
```

→ The skill's ingestion stack **auto-creates the bulk loader bucket + IAM role**. It provides guidance for the initial load.

## 16. Bedrock throttling

Sonnet 4 default RPM = 50, TPM = 200K. Recommendation-call bursts can throttle.

```python
# Lambda retry + DLQ
@retry(exponential_backoff=True, max_attempts=3)
def invoke_bedrock(prompt):
    try:
        return bedrock.invoke_model(...)
    except ThrottlingException:
        time.sleep(2 ** attempt)
        raise
```

→ Or raise the service quota (production).

## 17. Multi-tenant isolation

Option comparison:

| Pattern | Isolation strength | Cost |
|---|---|---|
| Single cluster + label prefix (`User_TenantA_u123`) | medium (developer mistakes possible) | low |
| Single cluster + tenant property + access control | strong (IAM policy + Lambda validation) | low |
| Cluster per tenant | strongest | high (cluster $44+/mo) |

→ Skill default = **single cluster + tenant_id property**. Applied when the answer indicates multi-tenant.

## 18. Edge property type consistency

```cypher
// ❌ Same edge label but inconsistent property types
CREATE (u)-[:BOUGHT {at: 1700000000}]->(i)        // unix sec
CREATE (u)-[:BOUGHT {at: "2024-01-01"}]->(i)     // ISO string
CREATE (u)-[:BOUGHT {timestamp: 123}]->(i)        // different property name

// ✅ Consistent — Neptune is schemaless so it won't enforce this; keep it consistent in code
```

→ The skill's ingestion Lambda enforces **all timestamps = unix milliseconds (compatible with Neptune's datetime() function)**.

## 19. Cluster lock when a KMS key is revoked

If a Neptune cluster is encrypted with a KMS key and the key is later disabled, the cluster becomes unusable.

```python
# Use a customer-managed CMK + RemovalPolicy.RETAIN from the start
cmk = kms.Key(self, 'Cmk',
    enable_key_rotation=True,
    removal_policy=RemovalPolicy.RETAIN,         # ★ preserve the key even on stack delete
    pending_window=Duration.days(30),
)
```

## 20. Neptune data loss on CloudFormation rollback

```python
# If the Neptune cluster is inside the stack, deleting the stack deletes the cluster
neptune_cluster = neptune.DatabaseCluster(self, 'Graph',
    deletion_protection=True,                    # ★ production
    removal_policy=RemovalPolicy.RETAIN,         # ★ production
    ...
)
```

→ The skill's GraphStack **auto-applies RETAIN when environment=='prod'**.

## 21. Neptune backup — automated snapshot vs export to S3

| Method | Use |
|---|---|
| Automated snapshot (1-35 days) | PITR within the same cluster |
| Manual snapshot | permanent retention or cross-region |
| Export to S3 (CSV) | migration to another system, BI analysis |

→ The skill's cluster auto-backs up for 7-30 days (30 in production).

## 22. Frontend graph viz — limit the data volume

```typescript
// ❌ Wrong — visualizing 1000+ vertices at once
graph.setData({ nodes: 1500_nodes, edges: 5000_edges });
// browser freeze

// ✅ Correct — top-N + drill-down
const top20 = await fetchUserNeighborhood(userId, { hops: 1, limit: 20 });
graph.setData(top20);
// fetch more when the user clicks a node
```

→ The skill's Graph Explorer page uses a **default 20-node limit + click-to-expand** pattern.

## 23. Cypher result → Bedrock context format

A Cypher result is a dict/object → dumping it straight into the prompt is noisy.

```python
# ❌ Wrong
prompt = f"data: {json.dumps(cypher_result)}"
# result: {"records":[{"_fields":[...]}]} → the LLM wastes tokens parsing the schema

# ✅ Correct — human-readable format
items_text = "\n".join([
    f"- {r['name']} (score {r['score']:.2f})"
    for r in cypher_result
])
prompt = f"Recommended products:\n{items_text}\n\nNatural-language explanation:"
```

→ The skill auto-generates a format_for_prompt() helper.

## 24. Health check — Neptune endpoint

```python
# The Lambda needs a health check that it can reach the Neptune endpoint (especially on init)
def lambda_handler(event, context):
    if not _neptune_ready:
        try:
            neptune.run("RETURN 1")
            _neptune_ready = True
        except Exception:
            return {"statusCode": 503, "body": "Neptune not ready"}
    # ... recommendation logic
```

→ Monitor ALB / API Gateway throttling + Neptune cluster availability separately.

## 25. Tags + cost allocation

```python
cdk.Tags.of(stack).add('Project', project_name)
cdk.Tags.of(stack).add('Environment', environment)
cdk.Tags.of(stack).add('Component', 'graph-personalization')
cdk.Tags.of(stack).add('DataClassification', 'confidential')  # recommendations = user behavior data
```

→ In Cost Explorer, track graph cluster cost vs Bedrock cost separately.

## Quick checklist (before code generation)

```
[ ] Schema (vertex/edge labels) finalized in Phase 2
[ ] Vertex/edge weight and recency decay calculation specified
[ ] Aggregation threshold (count >= 3 or 5) enforced in every query
[ ] No user IDs included directly in the Bedrock prompt
[ ] Cold-start fallback (popular + segment) always included
[ ] Cypher parameterized queries (no string interpolation)
[ ] Batch upsert (UNWIND, batch_size=100) for ingestion Lambda
[ ] DLQ + retry policy
[ ] IAM database auth (gremlin-python + SigV4)
[ ] Neptune cluster CMK encryption + RETAIN
[ ] Frontend top-20 + click-to-expand
[ ] Bedrock prompt caching (system prompt 100K+)
[ ] Tags: Project / Environment / Component / DataClassification
```
