# Example: Data Analytics Agent

> A system that queries data in natural language. The **Specialized Strands agent (Text2SQL)** is the core — the Orchestrator delegates to a self-reasoning sub-agent rather than a simple function.

## User Answers (Discovery)

| Question | Answer |
|---|---|
| 1. Which domain? | Sales data analytics (customers, orders, products) |
| 2. Integration pattern | Specialized Strands agent (multi-step reasoning — schema lookup → SQL generation → execution → retry) |
| 3. Data location | S3 + Glue catalog + Athena (or Aurora PostgreSQL) |
| 4. Additional tools | answer_general_questions (analytics docs KB) |
| 5. Memory | short-term — follow-up questions ("What are that customer's orders?") |
| 6. Region | us-east-1 |
| 7. Model | Orchestrator: Sonnet 4 / Sub-agent: Sonnet 4 (Opus 4.7 option after reviewing SQL-generation accuracy) |

## Generated Stack Composition

```
generated-project/
├── cdk-infra/
│   ├── app.py
│   └── src/stacks/
│       ├── orchestrator_agent_stack.py
│       ├── text2sql_agent_stack.py        ← Specialized Strands agent + S3/Glue/Athena
│       ├── knowledge_base_stack.py
│       └── agentcore_gateway_stack.py     ← (Gateway can be skipped if there is no MCP target)
├── agents/
│   ├── orchestrator-agent/                ← invoke_specialized_agent("text2sql_agent", ...)
│   └── text2sql-agent/
│       ├── text2sql_agent.py              ← @tool: query_database, list_available_tables, get_table_info
│       ├── sample_data/
│       │   ├── customers.csv
│       │   ├── orders.csv
│       │   ├── products.csv
│       │   └── order_items.csv
│       ├── Dockerfile
│       └── requirements.txt
├── frontend/
└── scripts/
```

## Orchestrator local tool wrapper

```python
@tool
def query_data(question: str) -> dict:
    """
    Query the database using natural language. Converts questions to SQL and executes them.

    Use this tool for data queries like:
    - "Show me total sales by customer"
    - "What are the top 5 products by revenue?"
    - "List all pending orders"
    - "Which customers have spent more than $1000?"
    """
    return invoke_specialized_agent("text2sql_agent", question, f"orchestrator_{uuid.uuid4().hex[:8]}")
```

## Orchestrator routing table

```
| Intent     | Keywords                                                | Action               |
|------------|---------------------------------------------------------|----------------------|
| DATA       | customers, orders, sales, revenue, count, top, sql, query | query_data tool      |
| KNOWLEDGE  | what is, how to, explain, definition, doc                 | answer_general_questions |
```

## Text2SQL agent — multi-step reasoning

On a single `query_database` tool call, the sub-agent performs the following:

1. **Schema lookup**: `SHOW TABLES` + `DESCRIBE` per table → build schema context
2. **SQL generation**: Bedrock invoke_model (Sonnet 4) — schema + natural-language question prompt
3. **SQL execution**: Athena `start_query_execution` → polling → `get_query_results`
4. **Return**: `{natural_language_query, generated_sql, results, row_count, execution_time_ms}`

Additional tools:
- `list_available_tables()` — explore the DB
- `get_table_info(table_name)` — schema of a specific table

The Strands Agent loop auto-retries on SQL execution failure (Bedrock sees the error message and fixes the SQL).

## Demonstration Scenarios

### 1. Simple query

```
User: "Show me total sales by customer"
→ Orchestrator: intent=DATA → query_data(question)
→ Text2SQL Strands Agent:
   1. SHOW TABLES → [customers, orders, products, order_items]
   2. DESCRIBE customers, orders → schema
   3. Bedrock prompt → SQL:
      SELECT c.name, SUM(o.total_amount) AS total_sales
      FROM customers c JOIN orders o ON c.customer_id = o.customer_id
      GROUP BY c.name ORDER BY total_sales DESC
   4. Athena execution → 10 rows
→ Orchestrator formats the result as a markdown table and responds
```

### 2. Follow-up (Memory context)

```
[Previous response: "John Doe is the top customer with $5000"]

User: "Can you tell me more about the orders?"
→ Memory hook injects history into the system prompt
→ Orchestrator: pronouns "the orders" → resolve to "John Doe's orders"
→ query_data("List John Doe's orders with details")
→ Text2SQL: WHERE c.name = 'John Doe' …
→ Responds with a table of 10 orders
```

### 3. Automatic error recovery (Strands tool-call loop)

```
User: "Show top products by revnue" (typo)
→ Text2SQL Strands Agent:
   1. SQL generation: SELECT name, SUM(quantity * price) AS revnue ...   (typo included)
   2. Athena: ambiguous reference to column 'price' → returns an error
   3. Strands loop: passes the error to the LLM → regenerates SQL (price → unit_price)
   4. Success → returns the result
```

## Cost Estimate (us-east-1, monthly, 100 queries/day)

| Item | $ |
|---|---|
| Runtime (Orchestrator + Text2SQL) | ~$10 |
| Bedrock Sonnet 4 (2 LLM calls per Text2SQL tool) | ~$30 |
| Athena ($5 / TB scanned) | ~$5 (sample data is small) |
| Glue Catalog | <$1 |
| S3 (sample data) | <$1 |
| KB (optional) | $345 or 0 |
| Memory | $2 |
| **Total** | **~$48 (without KB) / ~$393 (with KB)** |

## Variant 1: Data source is Aurora PostgreSQL

Replace `execute_athena_query` in `text2sql_agent.py` with the following:

```python
import psycopg2

def execute_postgres_query(query: str) -> dict:
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])
    creds = json.loads(secret["SecretString"])
    conn = psycopg2.connect(host=creds["host"], dbname=creds["dbname"], user=creds["username"], password=creds["password"])
    cur = conn.cursor()
    cur.execute(query)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {"columns": cols, "results": [dict(zip(cols, r)) for r in rows]}
```

CDK changes:
- Add an Aurora cluster + Secrets Manager DB credentials to `Text2SqlAgentStack`
- Add `secretsmanager:GetSecretValue` on the cluster secret to the IAM policy + VPC config (when Aurora is in a private subnet)

## Variant 2: Multiple data sources (Athena + Aurora + DynamoDB)

Split sub-agents per data source so the Orchestrator routes by intent:

```
Orchestrator
   ├── query_athena_data       → text2sql-athena-agent
   ├── query_postgres_data     → text2sql-postgres-agent
   └── query_dynamodb_data     → ddb-query-agent
```

Or route internally within the Text2SQL agent (supports cross-source queries — higher complexity).

## Variant 3: Multi-step analytics workflow

"Analyze this week's sales trend and tell me the outliers" → executes the following in sequence:
1. SQL: aggregate daily sales
2. Bedrock: outlier detection analysis
3. SQL: look up detailed orders for the anomalous dates
4. Synthesis: natural language report

→ Specify the multi-step pattern in the Text2SQL agent's system prompt + the Strands tool loop handles it automatically.

## Learning Points

1. **A sub-agent is a "tool" with its own reasoning loop** — from the Orchestrator's perspective it's a single tool call.
2. **Bedrock calls inside a tool are a separate cost** — both the Strands tool's LLM calls and the Orchestrator's LLM calls are billed.
3. **Memory only at the Orchestrator level** — sub-agents are typically stateless. (Exception: add Memory to the sub-agent too when learning per-user query history)
4. **Schema discovery only once** — calling `SHOW TABLES` on every query adds cost/latency. Consider caching.
