# Strands Agents Patterns

> Production-ready code patterns for the **Orchestrator agent (intent classification + routing)** + **specialized agent (domain reasoning)**. All of them run in a Runtime container based on `bedrock_agentcore.BedrockAgentCoreApp`.

## File layout (per agent)

```
agents/<agent-name>/
├── <agent_name>.py             ← entrypoint, BedrockAgentCoreApp + Strands Agent
├── main.py                     ← uvicorn entry (usually a single line)
├── common/
│   ├── __init__.py
│   ├── aws_config.py           ← region, account helpers
│   ├── cognito_token_manager.py ← M2M / user_password Bearer token
│   ├── sigv4_auth.py           ← httpx Auth for Gateway calls
│   └── prompts.py              ← system prompt builder
├── memory/                     (orchestrator only)
│   ├── __init__.py
│   └── short_term_memory.py    ← HookProvider
├── tests/
│   ├── interactive_chat_test.py
│   └── test_short_term_memory.py
├── Dockerfile                  ← LINUX_ARM64 compatible (uv + python 3.13)
├── requirements.txt
├── pyproject.toml
├── .dockerignore
└── README.md
```

## Pattern 1: Orchestrator Agent — full structure

### `agents/orchestrator-agent/orchestrator_agent.py`

```python
"""
Orchestrator Agent — Strands + AgentCore Runtime
- Intent classification + tool routing
- AgentCore Memory (short-term) hooks
- Calls: MCP servers via Gateway (SigV4) + Specialized Strands agents (Cognito Bearer) + Knowledge Base
"""
import boto3
import json
import logging
import requests
import traceback
import uuid
from datetime import datetime

from bedrock_agentcore import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

from common.aws_config import AWSConfig
from common.cognito_token_manager import CognitoTokenManager
from common.prompts import get_orchestrator_system_prompt
from common.sigv4_auth import get_sigv4_auth
from memory.short_term_memory import (
    ShortTermMemoryHooks,
    create_orchestrator_short_term_memory,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

aws_config = AWSConfig(logger)
AWS_REGION = aws_config.get_region()

bedrock_client = boto3.client("bedrock-runtime")
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)

# ── Initialize app with CORS
app = BedrockAgentCoreApp()
app = Starlette(app)
app = BedrockAgentCoreApp(CORSMiddleware(app=app, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"]))

# ── Memory globals (lazy init)
short_term_memory_id = None
short_term_memory_client = None
SHORT_TERM_MEMORY_NAME = "orchestrator_agent_stm"


def initialize_memory():
    global short_term_memory_id, short_term_memory_client
    try:
        if AWS_REGION:
            short_term_memory_id, short_term_memory_client = create_orchestrator_short_term_memory(
                logger, AWS_REGION, memory_name=SHORT_TERM_MEMORY_NAME
            )
            logger.info(f"✅ Short-term memory initialized: {short_term_memory_id}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to initialize memory: {e}")


initialize_memory()


# ── Local tools (called like any other Strands tool, but execute Python code)

def invoke_specialized_agent(agent_name: str, prompt: str, user_id: str) -> dict:
    """Generic Bearer-token POST to a specialized Strands agent runtime."""
    ssm = boto3.client("ssm", region_name=AWS_REGION)
    agent_arn = ssm.get_parameter(Name=f"/{agent_name}/runtime/agent_arn")["Parameter"]["Value"]

    token_manager = CognitoTokenManager(secret_name=f"{agent_name}/cognito/credentials")
    bearer_token = token_manager.get_fresh_token()

    encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
    agent_url = (
        f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )
    headers = {"authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "user_id": user_id}

    response = requests.post(agent_url, headers=headers, json=payload, timeout=180)
    response.raise_for_status()

    # ── Parse SSE response — extract only contentBlockDelta text
    full_text = ""
    for line in response.text.split("\n"):
        if line.startswith("data: ") and "contentBlockDelta" in line:
            try:
                data = json.loads(line[6:])
                if "event" in data and "contentBlockDelta" in data["event"]:
                    text = data["event"]["contentBlockDelta"]["delta"].get("text", "")
                    full_text += text
            except Exception:
                pass
    return {"response": full_text, "prompt": prompt}


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


@tool
def answer_general_questions(query: str) -> dict:
    """
    Answer general questions using the workshop/knowledge base.
    Use for queries like: "What is X?", "How do I Y?", "Explain Z".
    """
    try:
        ssm = boto3.client("ssm", region_name=AWS_REGION)
        kb_id = ssm.get_parameter(Name="/workshop/knowledge_base/kb_id")["Parameter"]["Value"]
        return bedrock_agent_runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 10}},
        )
    except Exception as e:
        logger.error(f"KB retrieve error: {e}")
        return {"error": str(e)}


# ── Identity resolution helpers (★ Constraint #25 — must be stable, never random/timestamp)

def resolve_customer_id(payload: dict, context, logger) -> str:
    """
    Resolve a stable `actor_id` for AgentCore Memory in priority order:
    1. Explicit `customer_id` from payload (frontend should send Cognito sub)
    2. JWT `sub` claim from the Bearer token (AgentCore JWT authorizer has already validated it)
    3. UUID fallback — but log loudly, because Memory continuity is DISABLED for this invocation.

    NEVER fall back to `f"customer_{uuid.uuid4()}"` silently — that defeats long-term Memory.
    """
    cid = payload.get("customer_id")
    if cid:
        return cid

    # Try JWT sub extraction (best-effort)
    try:
        import base64, json as _json
        auth = None
        if hasattr(context, "headers"):
            auth = context.headers.get("authorization") or context.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            payload_b64 = auth[7:].split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            sub = claims.get("sub")
            if sub:
                return f"cognito_{sub}"
    except Exception as e:
        logger.debug(f"JWT sub extraction failed: {e}")

    fallback = f"anon_{uuid.uuid4().hex[:8]}"
    logger.warning(
        f"⚠️ No stable customer_id available (frontend must pass Cognito sub). "
        f"Falling back to {fallback}. Memory continuity DISABLED for this invocation."
    )
    return fallback


def resolve_session_id(payload: dict) -> str:
    """
    Resolve `session_id`:
    1. Explicit `session_id` from payload (frontend manages session lifecycle)
    2. UUID fallback — NEVER use timestamp (collision = cross-user data leak).
    """
    return payload.get("session_id") or f"session_{uuid.uuid4()}"


# ── Entry point

@app.entrypoint
async def agent_invocation(payload, context):
    logger.info(f"Received payload: {payload}")
    prompt = payload.get("prompt", "No prompt found")
    customer_id = resolve_customer_id(payload, context, logger)   # ← stable ID
    session_id = resolve_session_id(payload)                       # ← UUID, never timestamp

    try:
        ssm = boto3.client("ssm", region_name=AWS_REGION)
        gateway_url = ssm.get_parameter(Name="/agentcore_gateway/gateway_url")["Parameter"]["Value"]

        global short_term_memory_id, short_term_memory_client
        if not short_term_memory_id:
            short_term_memory_id, short_term_memory_client = create_orchestrator_short_term_memory(
                logger, AWS_REGION, memory_name=SHORT_TERM_MEMORY_NAME
            )
    except Exception as e:
        logger.error(f"Setup error: {e}")
        yield {"type": "error", "error": f"Setup error: {e}"}
        return

    # ── Bedrock model — always use cross-region inference profile ID (us./eu./apac./global.)
    bedrock_model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        client=bedrock_client,
    )

    # ── Gateway MCP client (SigV4)
    sigv4_auth = get_sigv4_auth(region=AWS_REGION)
    gateway_mcp_client = MCPClient(lambda: streamablehttp_client(gateway_url, auth=sigv4_auth))

    with gateway_mcp_client:
        gateway_tools_all = gateway_mcp_client.list_tools_sync()
        # Filter Gateway internal tools and tools belonging to a sub-agent (handled directly)
        gateway_tools = [
            t for t in gateway_tools_all
            if t.tool_name != "x_amz_bedrock_agentcore_search"
            and not t.tool_name.startswith("text2sql")  # ← exclude sub-agent tools
        ]
        logger.info(f"Gateway tools: {[t.tool_name for t in gateway_tools]}")

        local_tools = [query_data, answer_general_questions]
        tools = gateway_tools + local_tools

        # ── Tool descriptions for system prompt
        tool_descriptions = []
        for t in gateway_tools:
            try:
                props = t.get_display_properties()
                tool_descriptions.append(f"{t.tool_name}: {json.dumps(props) if isinstance(props, dict) else props}")
            except Exception:
                tool_descriptions.append(f"{t.tool_name}: MCP tool")
        for t in local_tools:
            tool_descriptions.append(f"{t.__name__}: {t.__doc__ or 'No description'}")

        system_prompt = get_orchestrator_system_prompt(tool_descriptions)

        memory_hooks = ShortTermMemoryHooks(
            memory_client=short_term_memory_client,
            memory_id=short_term_memory_id,
            actor_id=customer_id,
            session_id=session_id,
            logger=logger,
            conversation_turns=20,
        )

        agent = Agent(
            model=bedrock_model,
            system_prompt=system_prompt,
            tools=tools,
            hooks=[memory_hooks],
        )

        try:
            stream = agent.stream_async(prompt)
            async for event in stream:
                yield event
        except Exception as e:
            logger.error(f"Agent error: {e}\n{traceback.format_exc()}")
            yield {"type": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
```

### `main.py`

```python
import orchestrator_agent  # noqa: F401
# Used by `python -m orchestrator_agent` invocation in Dockerfile CMD.
```

### `common/aws_config.py`

```python
import boto3
import os


class AWSConfig:
    """Resolve AWS region and account from env / boto3 / sts."""

    def __init__(self, logger):
        self.logger = logger
        self._region = None
        self._account = None

    def get_region(self) -> str:
        if self._region:
            return self._region
        self._region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or boto3.session.Session().region_name
            or "us-east-1"
        )
        self.logger.info(f"AWS Region: {self._region}")
        return self._region

    def get_account(self) -> str:
        if self._account:
            return self._account
        sts = boto3.client("sts", region_name=self.get_region())
        self._account = sts.get_caller_identity()["Account"]
        return self._account
```

### `common/prompts.py`

```python
"""
System prompt builder for the orchestrator agent.

Routing table is the core of orchestration — keep keywords up to date.
"""
from datetime import datetime


def get_formatted_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_orchestrator_system_prompt(tools_descriptions: list[str]) -> str:
    return """You are the Multi-Agent Orchestrator. Today: {today_date}

## 1. CONTEXT RESOLUTION (MANDATORY FIRST STEP)
Before processing ANY query, resolve contextual references from conversation history:
- ALL pronouns ("he", "she", "they", "it", "their", "the orders") → resolve to the LAST discussed entity
- Follow-ups ("tell me more", "what about", "which country") → apply to previous subject
- IGNORE pronoun gender mismatches — user may use wrong pronoun, still resolve to last entity
- Maintain entity focus: if last response was about "Jane Doe", ALL follow-ups are about Jane Doe

## 2. INTENT ROUTING
Route queries based on keywords:

| Intent     | Keywords                                       | Action                    |
|------------|------------------------------------------------|---------------------------|
| <DOMAIN_A> | issue, ticket, sprint, task, assign            | <domain_a>_* MCP tools    |
| <DOMAIN_B> | repo, commit, PR, branch, contributor          | <domain_b>_* MCP tools    |
| DATA       | customers, orders, sales, revenue, sql, query  | query_data tool           |
| KNOWLEDGE  | what is, how to, explain, guide, doc           | answer_general_questions  |
| MULTI      | overall progress, dashboard, summary           | Combine multiple tools    |

## 3. AVAILABLE TOOLS
{tools_list}

## 4. RULES
- Resolve context BEFORE making tool calls
- Use exact identifiers (owner/repo, project keys)
- For multi-service: sequence calls, then synthesize
- Cite which service provided information
- Never answer technical questions directly when a tool can — always use tools
""".format(today_date=get_formatted_date(), tools_list="\n".join(tools_descriptions))
```

### `Dockerfile` (shared by orchestrator + sub-agents)

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1

COPY requirements.txt requirements.txt
RUN uv pip install -r requirements.txt
RUN uv pip install aws-opentelemetry-distro>=0.10.1

ENV AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
ENV DOCKER_CONTAINER=1

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080
EXPOSE 8000   # MCP server only (a Strands agent uses only 8080)

COPY . .

CMD ["opentelemetry-instrument", "python", "-m", "orchestrator_agent"]
```

### `requirements.txt`

```
mcp
httpx>=0.24.0
boto3
bedrock-agentcore
bedrock-agentcore-starter-toolkit
strands-agents
strands-agents-tools
rpds-py
jsonschema==4.17.3
pydantic>=2.5.3
pydantic-core>=2.14.6
botocore
starlette
requests
```

### `.dockerignore`

```
__pycache__/
*.pyc
.git/
.venv/
venv/
tests/
*.md
.pytest_cache/
.mypy_cache/
.python-version
```

## Pattern 2: Specialized Strands Agent (e.g., Text2SQL)

Key differences:
- The Orchestrator invokes it **like a tool** → the entry handler's `payload` is a single prompt + user_id.
- Multi-step reasoning with its own tool set (`@tool` functions).
- The Memory hook is usually **not used** (the orchestrator's memory already holds the per-conversation context). However, if the sub-agent needs its own learning, add a separate Memory.

### `agents/text2sql-agent/text2sql_agent.py`

```python
"""
Text2SQL Specialized Agent — Strands + AgentCore Runtime
Receives natural language → generates SQL → executes against Athena → returns rows.
"""
import boto3
import json
import logging
import os
import time

from bedrock_agentcore import BedrockAgentCoreApp
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from strands import Agent, tool
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ssm = boto3.client("ssm")
athena = boto3.client("athena")
bedrock_client = boto3.client("bedrock-runtime")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

app = BedrockAgentCoreApp()
app = Starlette(app)
app = BedrockAgentCoreApp(CORSMiddleware(app=app, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"]))


def get_config() -> dict:
    """Read config from SSM Parameter Store."""
    tool_name = os.environ.get("TOOL_NAME", "text2sql_agent")
    response = ssm.get_parameters(
        Names=[
            f"/{tool_name}/config/athena_workgroup",
            f"/{tool_name}/config/athena_database",
            f"/{tool_name}/config/s3_output_location",
        ],
        WithDecryption=True,
    )
    return {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}


def execute_athena_query(query: str, database: str, output_location: str, workgroup: str, skip_header: bool = True) -> dict:
    start = time.time()
    resp = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
        WorkGroup=workgroup,
    )
    qid = resp["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)
        state = status["QueryExecution"]["Status"]["State"]
        if state in ["SUCCEEDED", "FAILED", "CANCELLED"]:
            break
        time.sleep(0.5)
    if state != "SUCCEEDED":
        reason = status["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
        raise Exception(f"Query failed: {state} - {reason}")
    results = athena.get_query_results(QueryExecutionId=qid)
    start_idx = 1 if skip_header else 0
    rows = [[col.get("VarCharValue", "") for col in row["Data"]] for row in results["ResultSet"]["Rows"][start_idx:]]
    return {"results": rows, "row_count": len(rows), "execution_time_ms": int((time.time() - start) * 1000)}


@tool
def query_database(natural_language_query: str) -> dict:
    """
    Convert natural language to SQL and execute against the database.
    Use for any data analytics query.
    """
    config = get_config()
    db = config["athena_database"]

    # ── Build schema context (single SHOW TABLES + DESCRIBE per table)
    tables = execute_athena_query("SHOW TABLES", db, config["s3_output_location"], config["athena_workgroup"], skip_header=False)
    schema_parts = []
    for row in tables["results"]:
        tname = row[0]
        desc = execute_athena_query(f"DESCRIBE {tname}", db, config["s3_output_location"], config["athena_workgroup"], skip_header=False)
        cols = []
        for r in desc["results"]:
            if r and r[0]:
                parts = r[0].split("\t")
                if len(parts) >= 2:
                    cols.append(f"{parts[0].strip()} {parts[1].strip()}")
        schema_parts.append(f"Table: {tname}\nColumns: {', '.join(cols)}")
    schema = "\n\n".join(schema_parts)

    # ── Generate SQL with Bedrock (single shot — Strands Agent loop will retry on failure)
    prompt = f"""Convert this natural language query to SQL.

Database Schema:
{schema}

Natural Language Query: {natural_language_query}

Generate only the SQL query without explanation. Use standard SQL compatible with Athena/Presto."""

    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
    resp = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    result = json.loads(resp["body"].read())
    sql = result["content"][0]["text"].strip().replace("```sql", "").replace("```", "").strip()

    query_result = execute_athena_query(sql, db, config["s3_output_location"], config["athena_workgroup"])
    return {
        "natural_language_query": natural_language_query,
        "generated_sql": sql,
        "results": query_result["results"],
        "row_count": query_result["row_count"],
        "execution_time_ms": query_result["execution_time_ms"],
    }


@tool
def list_available_tables() -> dict:
    """List all available tables in the database."""
    cfg = get_config()
    res = execute_athena_query("SHOW TABLES", cfg["athena_database"], cfg["s3_output_location"], cfg["athena_workgroup"], skip_header=False)
    return {"database": cfg["athena_database"], "tables": [r[0] for r in res["results"]]}


@tool
def get_table_info(table_name: str) -> dict:
    """Get schema for a specific table."""
    cfg = get_config()
    res = execute_athena_query(f"DESCRIBE {table_name}", cfg["athena_database"], cfg["s3_output_location"], cfg["athena_workgroup"], skip_header=False)
    cols = []
    for r in res["results"]:
        if r and r[0]:
            p = r[0].split("\t")
            if len(p) >= 2:
                cols.append({"name": p[0].strip(), "type": p[1].strip()})
    return {"table": table_name, "columns": cols}


SYSTEM_PROMPT = """You are a Text2SQL Agent that helps users query databases using natural language.

Capabilities:
1. Convert natural language to SQL
2. Execute SQL against Athena
3. List tables and schemas
4. Return results clearly

When a user asks a data question:
1. Use query_database tool — it converts and executes
2. Present results in a clear format
3. If query fails, explain the error and retry with a corrected SQL"""


@app.entrypoint
async def agent_invocation(payload, context):
    prompt = payload.get("prompt", payload.get("question", ""))
    user_id = payload.get("user_id", "unknown")
    if not prompt:
        yield {"type": "error", "error": "No prompt provided"}
        return

    bedrock_model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        client=bedrock_client,
    )
    agent = Agent(
        model=bedrock_model,
        system_prompt=SYSTEM_PROMPT,
        tools=[query_database, list_available_tables, get_table_info],
    )
    try:
        async for event in agent.stream_async(prompt):
            yield event
    except Exception as e:
        logger.error(f"Error: {e}")
        yield {"type": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
```

## Pattern 3: Tool-as-thin-wrapper for sub-agent invocation

Like the Orchestrator's `query_data` tool, a thin wrapper that encapsulates **the external call + SSE parsing**. Add another sub-agent (e.g., code review agent, browser agent) with the same pattern:

```python
@tool
def review_code(repo_url: str, pr_number: int) -> dict:
    """
    Review a pull request — performs static analysis and security scan.
    Use for queries like: "Review PR #123 in owner/repo".
    """
    return invoke_specialized_agent(
        "code_review_agent",
        f"Review PR #{pr_number} in {repo_url}",
        f"orchestrator_{uuid.uuid4().hex[:8]}",
    )
```

The `invoke_specialized_agent` helper is reused for every sub-agent call (see Pattern 1).

## Pattern 4: Multi-tool synthesis (multiple tools in one response)

The LLM calls multiple tools automatically, but accuracy improves when you guide it explicitly via the **MULTI item in the system prompt**:

```python
def get_orchestrator_system_prompt(tools_descriptions):
    return f"""...

## MULTI-SERVICE PATTERNS
When a query spans multiple domains:

1. "Team performance this sprint" →
   a. Get sprint info: search_issues with JQL "sprint = openSprints()"
   b. Get commit activity: get_repository_statistics
   c. Synthesize: produce table joining issue counts with commit counts per assignee

2. "Project health overview" →
   a. Open issues: search_issues with JQL "status != Done"
   b. Open PRs: get_pull_requests state=open
   c. Recent commits: get_recent_commits limit=20
   d. Synthesize a one-paragraph summary
"""
```

## Pattern 5: Local testing (before deploy)

### `tests/interactive_chat_test.py`

```python
"""
Interactive local chat test — bypass Cognito by directly invoking entry handler.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from orchestrator_agent import agent_invocation


async def main():
    print("Local chat (Ctrl+C to exit)")
    customer_id = "test_customer"
    session_id = "test_session"
    while True:
        prompt = input("> ").strip()
        if not prompt:
            continue
        async for event in agent_invocation(
            {"prompt": prompt, "customer_id": customer_id, "session_id": session_id}, None
        ):
            if "event" in event and "contentBlockDelta" in event["event"]:
                text = event["event"]["contentBlockDelta"]["delta"].get("text", "")
                print(text, end="", flush=True)
            elif "type" in event and event["type"] == "error":
                print(f"\nERROR: {event['error']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
```

If this script passes before deploy, production will behave almost identically.

## Pattern 6: Scaling guide when adding N more agents

Procedure to add a new sub-agent:
1. Create the `agents/<name>-agent/` folder, copying Pattern 2
2. Create `cdk-infra/src/stacks/<name>_agent_stack.py` (use the specialized agent stack pattern in `shared/patterns/cdk-stacks.md`)
3. Add a row to the routing table in the Orchestrator's `prompts.py`
4. Add an `@tool` wrapper function to the Orchestrator's `orchestrator_agent.py`
5. Add the stack instance + dependency in `app.py`
6. Redeploy

Procedure to add a new MCP server:
1. Create the `mcp-servers/<name>-mcp/` folder and write the FastMCP server (`shared/patterns/mcp-servers.md`)
2. Create `cdk-infra/src/stacks/<name>_mcp_stack.py` (copy the Jira MCP stack pattern)
3. Pass `<name>_mcp_stack` to the Gateway stack in `app.py`
4. Call `_add_mcp_target("<name>", <name>_mcp_stack)` in the Gateway stack's `__init__`
5. The Orchestrator code does **not** need changes — the Gateway exposes it automatically (semantic search)
