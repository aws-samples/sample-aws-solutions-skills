# Eval: Data Analytics Scenario

## Input Prompt
```
Build me a natural-language sales-data analytics system.
- Data: 4 tables — customers, orders, products, order_items
- Data location: S3 + Athena (Glue catalog)
- Natural-language queries: queries like "Top customers", "Sales by product", "Pending orders"
- No KB (data analysis only)
- Memory: short-term (follow-up questions)
- Region: us-east-1
```

## Expected Behavior

### Phase 1 (Discovery)
- [ ] Domain identification: sales data (e-commerce schema)
- [ ] Integration pattern decision: **Specialized Strands Agent** (multi-step reasoning — schema → SQL → execution → retry)
- [ ] LLM model recommendation: Sonnet 4 by default (Sub-agent also Sonnet 4)
- [ ] Confirm whether sample data is auto-included (automated CSV upload to S3)

### Phase 2 (Design — GATE 2)
- [ ] Stack composition:
  ```
  | Stack                          | Selected? |
  | OrchestratorAgentCoreStack     | ✅        |
  | Text2SqlAgentStack             | ✅        |
  | KnowledgeBaseStack             | ❌ (skip) |
  | AgentCoreGatewayStack          | ❌ (skip — no MCP target) |
  ```
- [ ] Orchestrator local tools: `query_data` (sub-agent invoke wrapper) + `list_tables`/`describe_table` (direct ssm/athena calls — optional)
- [ ] Sub-agent invocation method clearly defined: HTTPS POST to AgentCore Runtime endpoint with Cognito Bearer
- [ ] Cost estimate ~$50/mo (no KB)

### Phase 3 (Generated Files)

**CDK Stack**:
- [ ] `cdk-infra/src/stacks/text2sql_agent_stack.py` — Runtime + Cognito (M2M for sub-agent invocation) + S3 buckets (table_bucket, results_bucket) + Glue database + Glue tables (customers/orders/products/order_items) + sample CSV upload (BucketDeployment)
- [ ] IAM policy explicitly grants Athena/Glue/S3 access
- [ ] `text2sql_agent_stack` env vars: `BEDROCK_MODEL_ID`, `TOOL_NAME`, `AWS_REGION`
- [ ] SSM parameters: `/text2sql_agent/runtime/agent_arn`, `/text2sql_agent/config/athena_workgroup`, `/text2sql_agent/config/athena_database`, `/text2sql_agent/config/s3_output_location`

**Sub-agent**:
- [ ] `agents/text2sql-agent/text2sql_agent.py` — `BedrockAgentCoreApp`, Strands `Agent`, 3 `@tool` (`query_database`, `list_available_tables`, `get_table_info`), schema discovery (SHOW TABLES + DESCRIBE)
- [ ] SQL generation: Bedrock invoke_model with cross-region inference profile ID
- [ ] Athena polling loop with state check (SUCCEEDED/FAILED/CANCELLED)
- [ ] Result dict format: `{natural_language_query, generated_sql, results, row_count, execution_time_ms}`
- [ ] 4 CSV files in `agents/text2sql-agent/sample_data/` (fixed sample)
- [ ] Dockerfile LINUX_ARM64 compatible

**Orchestrator**:
- [ ] `query_data` `@tool` — calls `invoke_specialized_agent("text2sql_agent", question, user_id)`
- [ ] `invoke_specialized_agent` extracts only contentBlockDelta from the SSE stream
- [ ] Adds the DATA intent to the routing table in the system prompt (excludes the KB intent)

**Frontend**:
- [ ] 5 example cards: "Top customers by sales", "Top 5 products by revenue", "Pending orders last 30 days", "Customers > $1000", "Average order size"
- [ ] Renders results as a markdown table (`react-markdown` + `remark-gfm`)

**Scripts**:
- [ ] `scripts/deploy.sh` — Orchestrator → Text2SqlAgent (no Gateway or KB stack)
- [ ] `scripts/destroy.sh` — reverse-order destroy (auto_delete_objects enabled on S3 buckets)

### Phase 4 (Validate — GATE 3)
- [ ] `cdk synth` passes
- [ ] Validate Athena IAM actions with AWS Knowledge MCP
- [ ] Sub-agent invocation integration test (`tests/test_text2sql_e2e.py`)

### Phase 5 (Deploy)
- [ ] Guidance confirming the CSV data is automatically uploaded to S3
- [ ] Confirm Glue data catalog visibility (`aws glue get-tables --database-name workshop_db`)
- [ ] Confirm the Athena query result location is auto-created on the first query

## Code Quality Checks

- [ ] The Strands Agent loop auto-retries on SQL failure (Bedrock sees the error and fixes the SQL)
- [ ] Schema discovery results are re-fetched on every query (caching optional — recommended for production)
- [ ] Model is swappable via the BEDROCK_MODEL_ID env (Sonnet 4 → Opus 4.7 swap possible)
- [ ] Athena query timeout (e.g., 60s) followed by a cancel call
- [ ] Cognito client_credentials used for Sub-agent invocation (Orchestrator → Sub-agent)
   - Or Cognito USER_PASSWORD_AUTH (Bearer) — auto-detect in `CognitoTokenManager`

## Functional Verification (Smoke tests)

```bash
# Invoke the Orchestrator endpoint
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name MultiAgentOrchestrator --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name MultiAgentOrchestrator --query 'Stacks[0].Outputs[?OutputKey==`ClientId`].OutputValue' --output text)

# Get JWT
TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=testuser,PASSWORD=YourSecurePassword123! \
  --query 'AuthenticationResult.AccessToken' --output text)

# Get runtime URL
RUNTIME_ARN=$(aws cloudformation describe-stacks --stack-name MultiAgentOrchestrator --query 'Stacks[0].Outputs[?OutputKey==`RuntimeArn`].OutputValue' --output text)
ENCODED=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$RUNTIME_ARN', safe=''))")

# Invoke
curl -X POST \
  "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/$ENCODED/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Show me total sales by customer","customer_id":"smoke","session_id":"s1"}'
```

Expected response: results rendered as a markdown table via the SSE stream, displayed together with generated_sql.

## Memory Continuity Verification

```
[Query 1] "Show top 5 customers by sales"
  → Response: "John Doe ($5000), Jane ($4200), ..."

[Query 2 — same session_id, same customer_id] "Tell me more about his orders"
  → Memory prepends the "John Doe" context
  → query_data("List John Doe's orders with details")
  → SQL: WHERE c.name = 'John Doe'
  → Response: list of orders
```

Verification checklist:
- [ ] `resolve_customer_id(payload, context, logger)` in `orchestrator_agent.py` falls back in the order `payload["customer_id"]` → JWT sub → loud-warn UUID
- [ ] `resolve_session_id(payload)` does not use a timestamp-format fallback — UUID only
- [ ] Frontend explicitly sends `cognito_${idToken.sub}` as customer_id
- [ ] On the second call after the first (same customer_id + session_id), confirm the first call's context resolves automatically

## Failure Modes

| Failure scenario | Expected behavior |
|---|---|
| User query references a table outside the schema | The Strands Agent calls `list_available_tables` and presents available options to the user |
| SQL syntax error (invalid column) | The Strands Agent loop receives the error message, fixes the SQL, and retries |
| Athena timeout | On exceeding 60s, cancel the query and respond "Query timed out, try simplifying" |
| Insufficient permissions (Glue table missing) | "Schema not yet ready. Glue catalog refresh in 1–2 minutes." |
