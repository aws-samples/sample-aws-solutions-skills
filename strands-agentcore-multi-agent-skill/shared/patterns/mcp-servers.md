# MCP Server Patterns

> A pattern for packaging a **FastMCP-based MCP server** as an AgentCore Runtime. Since the Orchestrator selects tools via the Gateway's semantic search, **the tool docstring determines the semantic-matching quality**.

## File layout (per MCP server)

```
mcp-servers/<domain>-mcp/
├── <domain>_mcp.py        ← FastMCP server (entry)
├── common/                (optional, shared helpers)
│   ├── __init__.py
│   └── api_client.py      ← initializes the external API SDK
├── tests/
│   └── test_<domain>.py
├── Dockerfile             ← LINUX_ARM64, port 8000
├── requirements.txt
└── .dockerignore
```

## Skeleton (common to all MCP servers)

### `<domain>_mcp.py`

```python
#!/usr/bin/env python3
"""
<Domain> MCP Server — provides <domain> integration tools via FastMCP.

Tools are auto-discovered via @mcp.tool() decorator and routed through
AgentCore Gateway with semantic search.
"""
import json
import logging
import os
from typing import Any

import boto3
from mcp.server.fastmcp import FastMCP


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FastMCP server (host=0.0.0.0 for container, stateless_http for Gateway compatibility)
mcp = FastMCP(host="0.0.0.0", stateless_http=True)

# ── Configuration via SSM Parameter Store (NEVER use env vars for secrets)
ssm_client = boto3.client("ssm")


def get_config() -> dict:
    """Load configuration from Parameter Store under /<tool_name>/config/*."""
    tool_name = os.environ.get("TOOL_NAME", "<domain>_mcp")
    try:
        response = ssm_client.get_parameters(
            Names=[
                f"/{tool_name}/config/api_url",
                f"/{tool_name}/config/api_token",
            ],
            WithDecryption=True,
        )
        return {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return {}


config = get_config()


# ── Tool definitions
# IMPORTANT: docstring quality drives semantic search accuracy.
# Use natural-language phrasing the user is likely to say.

@mcp.tool()
def list_items(filter_text: str = "", limit: int = 50) -> dict[str, Any]:
    """
    List <domain> items, optionally filtered by text.

    Use this tool for queries like: "show me all <items>", "list <items>",
    "what <items> exist", "find <items> matching X".

    Args:
        filter_text: Optional substring filter
        limit: Max items to return (default: 50)
    """
    try:
        # ... call external API using config["api_url"] + config["api_token"]
        return {"status": "success", "items": [], "count": 0}
    except Exception as e:
        logger.error(f"list_items error: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_item(item_id: str) -> dict[str, Any]:
    """
    Get details about a specific <domain> item by ID.

    Use this tool for queries like: "show details for <item>", "get info about <item>",
    "what is <item> about".

    Args:
        item_id: Identifier of the item
    """
    try:
        return {"status": "success", "item": {}}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Run server (transport=streamable-http for Gateway)
mcp.run(transport="streamable-http")
```

### `requirements.txt`

```
mcp
boto3
httpx>=0.24.0
# ── Domain-specific SDK
# jira (for Jira MCP)
# requests + GitHub REST (for GitHub MCP)
# slack-sdk (for Slack MCP)
```

### `Dockerfile`

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

EXPOSE 8000   # ← FastMCP streamable-http transport (NOT 8080)

COPY . .

CMD ["opentelemetry-instrument", "python", "-m", "<domain>_mcp"]
```

## Pattern 1: Jira MCP (example using an external SDK)

```python
import boto3
import logging
import os
from typing import Any
from jira import JIRA
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(host="0.0.0.0", stateless_http=True)
ssm_client = boto3.client("ssm")


def get_jira_config() -> dict:
    tool_name = os.environ.get("TOOL_NAME", "jira_mcp")
    response = ssm_client.get_parameters(
        Names=[
            f"/{tool_name}/config/jira_base_url",
            f"/{tool_name}/config/jira_email",
            f"/{tool_name}/config/jira_api_token",
        ],
        WithDecryption=True,
    )
    return {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}


# ── Initialize SDK once at import (or per-call if creds rotate)
jira_config = get_jira_config()
jira_client = (
    JIRA(server=jira_config["jira_base_url"], basic_auth=(jira_config["jira_email"], jira_config["jira_api_token"]))
    if all(k in jira_config for k in ("jira_base_url", "jira_email", "jira_api_token"))
    else None
)


@mcp.tool()
def list_projects(limit: int = 50) -> dict[str, Any]:
    """
    List Jira projects accessible to the user.

    Use for queries like: "show me all projects", "what projects do we have",
    "list available projects".
    """
    if not jira_client:
        return {"status": "error", "message": "Jira client not initialized"}
    projects = jira_client.projects()
    return {
        "status": "success",
        "projects": [
            {"key": p.key, "name": p.name, "description": getattr(p, "description", "")}
            for p in projects[:limit]
        ],
        "count": min(len(projects), limit),
    }


@mcp.tool()
def search_issues(jql: str = "", limit: int = 50) -> dict[str, Any]:
    """
    Search Jira issues using JQL (Jira Query Language).

    Use for queries like: "show me open issues", "find bugs", "list tickets in project DEMO",
    "what tasks are assigned to John", "issues due this week".

    Args:
        jql: JQL query (e.g., 'status = Open AND assignee = currentUser()'). If empty, returns recent.
        limit: Max issues to return
    """
    if not jira_client:
        return {"status": "error", "message": "Jira client not initialized"}
    if not jql:
        jql = "ORDER BY created DESC"
    issues = jira_client.search_issues(jql, maxResults=limit)
    return {
        "status": "success",
        "jql": jql,
        "issues": [
            {
                "key": i.key,
                "summary": i.fields.summary,
                "status": i.fields.status.name,
                "assignee": i.fields.assignee.displayName if i.fields.assignee else "Unassigned",
                "updated": i.fields.updated,
            }
            for i in issues
        ],
        "count": len(issues),
    }


@mcp.tool()
def create_issue(project_key: str, summary: str, description: str = "", issue_type: str = "Task") -> dict[str, Any]:
    """
    Create a new Jira issue.

    Use for queries like: "create a task for X", "log a bug about Y",
    "add a story for Z to project DEMO".

    Args:
        project_key: Project key (e.g. "DEMO")
        summary: Brief title
        description: Detailed description
        issue_type: "Task", "Bug", "Story" etc.
    """
    if not jira_client:
        return {"status": "error", "message": "Jira client not initialized"}
    new_issue = jira_client.create_issue(fields={
        "project": {"key": project_key},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type},
    })
    return {"status": "success", "issue": {"key": new_issue.key, "summary": summary, "project": project_key}}


mcp.run(transport="streamable-http")
```

## Pattern 2: GitHub MCP (direct REST API calls)

```python
import boto3
import logging
import os
from typing import Any
import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(host="0.0.0.0", stateless_http=True)
ssm_client = boto3.client("ssm")


def get_github_config() -> dict:
    tool_name = os.environ.get("TOOL_NAME", "github_mcp")
    resp = ssm_client.get_parameters(
        Names=[f"/{tool_name}/config/github_token", f"/{tool_name}/config/github_username"],
        WithDecryption=True,
    )
    return {p["Name"].split("/")[-1]: p["Value"] for p in resp["Parameters"]}


github_config = get_github_config()
session = requests.Session()
if github_config.get("github_token"):
    session.headers.update({
        "Authorization": f"token {github_config['github_token']}",
        "Accept": "application/vnd.github.v3+json",
    })


@mcp.tool()
def get_repository_info(owner: str, repo: str) -> dict[str, Any]:
    """
    Get detailed information about a GitHub repository.

    Use for queries like: "tell me about repo owner/repo", "stats for owner/repo",
    "show repository details".
    """
    r = session.get(f"https://api.github.com/repos/{owner}/{repo}")
    if r.status_code != 200:
        return {"status": "error", "message": f"HTTP {r.status_code}"}
    d = r.json()
    return {
        "status": "success",
        "repository": {
            "full_name": d["full_name"],
            "description": d.get("description", ""),
            "stars": d["stargazers_count"],
            "forks": d["forks_count"],
            "open_issues": d["open_issues_count"],
            "language": d.get("language", ""),
            "default_branch": d["default_branch"],
        },
    }


@mcp.tool()
def get_recent_commits(owner: str, repo: str, limit: int = 10) -> dict[str, Any]:
    """
    Get recent commits from a GitHub repository.

    Use for queries like: "show recent commits", "what was committed lately",
    "list latest changes in owner/repo".
    """
    r = session.get(f"https://api.github.com/repos/{owner}/{repo}/commits", params={"per_page": limit})
    if r.status_code != 200:
        return {"status": "error", "message": f"HTTP {r.status_code}"}
    return {
        "status": "success",
        "commits": [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in r.json()
        ],
    }


@mcp.tool()
def get_pull_requests(owner: str, repo: str, state: str = "open", limit: int = 10) -> dict[str, Any]:
    """
    Get pull requests from a GitHub repository.

    Use for queries like: "show open PRs", "list pull requests", "which PRs are pending review".

    Args:
        state: "open", "closed", or "all"
    """
    r = session.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        params={"state": state, "per_page": limit},
    )
    if r.status_code != 200:
        return {"status": "error", "message": f"HTTP {r.status_code}"}
    return {
        "status": "success",
        "pull_requests": [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "author": p["user"]["login"],
                "url": p["html_url"],
            }
            for p in r.json()
        ],
    }


mcp.run(transport="streamable-http")
```

## Pattern 3: Custom domain MCP (scaffolding)

Checklist when building an MCP server for a new domain:

1. **Tool taxonomy**: List the 5–10 questions users ask most often, and map each to one tool.
2. **Docstring**: Include the **natural-language verbs/nouns** of user queries in the first line of the docstring and the "Use this tool for queries like:" section.
3. **Args**: Use explicit type hints. FastMCP auto-generates the JSON schema.
4. **Unified return format**: `{"status": "success" | "error", ...}`. This keeps the Orchestrator's response-synthesis code simple.
5. **External API auth**: SSM Parameter Store (plaintext) or Secrets Manager (sensitive). **Never use env vars**.
6. **Error handling**: On external API failure, return `{"status": "error", "message": ...}` — do not raise exceptions (if FastMCP returns a 5xx, the Orchestrator cannot retry).

## Pattern 4: Improving tool description quality — semantic-search friendly

A good docstring:
```python
@mcp.tool()
def search_issues(jql: str = "", limit: int = 50) -> dict:
    """
    Search Jira issues using JQL (Jira Query Language).
    Use for queries like: "show me bugs", "list open tickets",
    "find issues assigned to John", "what tasks are in sprint 5",
    "issues due this week", "tickets I created".
    """
```

A bad docstring:
```python
@mcp.tool()
def search_issues(jql, limit):
    """Search issues."""        # ← lacks keywords, semantic match fails
```

## Pattern 5: Streaming response (optional)

Most MCP tools return synchronously. However, long-running tasks (e.g., file downloads) can return chunked responses over streamable-http:

```python
@mcp.tool()
async def download_large_file(url: str) -> dict:
    """Download and process a large file."""
    # ── streamable-http supports yield
    async for chunk in fetch_chunks(url):
        yield {"type": "progress", "bytes": len(chunk)}
    yield {"type": "complete", "result": ...}
```

> However, how the Gateway delivers a streaming response to the LLM must be verified in production.

## Pattern 6: Local testing

```bash
cd mcp-servers/<domain>-mcp
TOOL_NAME=<domain>_mcp python <domain>_mcp.py
# → exposes http://localhost:8000/sse or /messages

# in another terminal, mcp inspector
npx @modelcontextprotocol/inspector http://localhost:8000
```

If the tool list and schema appear in the inspector UI, it works. It must pass before Gateway registration.

## Avoiding key pitfalls (summary)

| Pitfall | Avoidance |
|---|---|
| Poor tool docstring | Add 5+ natural-language keywords in the "Use for queries like:" section |
| Injecting secrets via env vars | Use SSM Parameter Store + `WithDecryption=True` |
| Raising exceptions | Return gracefully with `{"status":"error","message":...}` |
| Exposing 8080 instead of 8000 | An MCP server uses 8000 (Gateway expects it); only a Strands agent uses 8080 |
| Using `mcp.run(transport="stdio")` | Only streamable-http is Gateway-compatible |
| No default on a tool argument | If a query omits some args, the LLM tool call fails |
