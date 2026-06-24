# Example: DevOps Assistant

> Jira + GitHub integrated multi-agent. A direct instantiation of the reference project (`agentcore-multi-agent-workshop`). This example is the baseline for a **2 MCP servers + KB** configuration.

## User Answers (Discovery)

| Question | Answer |
|---|---|
| 1. Which domains to integrate? | Jira + GitHub |
| 2. External-system integration method | Jira REST API, GitHub REST API |
| 3. Specialized agent needed? | No (simple function calls only) |
| 4. Knowledge Base needed? | Yes — internal dev playbook site |
| 5. Memory strategy | short-term (raw) — same-session follow-up only |
| 6. Region | us-east-1 |
| 7. Model | Sonnet 4 (orchestrator) |
| 8. Number of tools | ~12 (Jira 6, GitHub 6) → SEMANTIC search |

## Generated Stack Composition

```
generated-project/
├── cdk-infra/
│   ├── app.py
│   └── src/stacks/
│       ├── orchestrator_agent_stack.py   ← MultiAgentOrchestrator
│       ├── jira_mcp_stack.py             ← JiraMcp
│       ├── github_mcp_stack.py           ← GitHubMcp
│       ├── knowledge_base_stack.py       ← DevPlaybookKnowledgeBase
│       └── agentcore_gateway_stack.py    ← AgentCoreGateway
├── agents/
│   └── orchestrator-agent/
│       ├── orchestrator_agent.py
│       ├── common/{aws_config, prompts, sigv4_auth, cognito_token_manager}.py
│       ├── memory/short_term_memory.py
│       ├── Dockerfile
│       └── requirements.txt
├── mcp-servers/
│   ├── jira-mcp/
│   │   ├── jira_mcp.py                   ← list_projects, search_issues, get_issue, create_issue, transition_issue, add_comment
│   │   ├── Dockerfile
│   │   └── requirements.txt              (jira, mcp, boto3)
│   └── github-mcp/
│       ├── github_mcp.py                 ← get_repository_info, get_recent_commits, get_pull_requests, get_repository_issues, search_repositories, get_repository_statistics
│       ├── Dockerfile
│       └── requirements.txt              (requests, mcp, boto3)
├── frontend/
│   └── src/pages/chat.tsx                ← example cards: Jira / GitHub / Knowledge
└── scripts/
    ├── deploy.sh
    ├── destroy.sh
    └── generate-frontend-config.sh
```

## Orchestrator system prompt — routing table

```
| Intent     | Keywords                                       | Action                 |
|------------|------------------------------------------------|------------------------|
| JIRA       | issue, project, sprint, task, ticket, assign   | jira_* MCP tools       |
| GITHUB     | repo, commit, PR, branch, contributor, fork    | github_* MCP tools     |
| KNOWLEDGE  | what is, how to, explain, playbook, doc, guide | answer_general_questions|
| MULTI      | team performance, project health, sprint progress | sequence: jira + github + synthesize |
```

## Demonstration Scenarios

### 1. Single domain — Jira

```
User: "Show me open issues in project DEMO"
→ Orchestrator: intent=JIRA → Gateway tool semantic match → search_issues(jql="project=DEMO AND status=Open")
→ JiraMcp Runtime: jira.search_issues → JIRA SDK → Atlassian API
→ Result: 12 issues, responded as a key/summary/assignee table
```

### 2. Single domain — GitHub

```
User: "What pull requests are open in awslabs/aws-cdk?"
→ Orchestrator: intent=GITHUB → get_pull_requests(owner="awslabs", repo="aws-cdk", state="open")
→ GitHubMcp: GitHub REST → list of PRs
→ Result: a table of PR number/title/author/URL
```

### 3. Multi-service — Sprint health

```
User: "How is our team performing this sprint?"
→ Orchestrator: intent=MULTI →
   1) search_issues(jql="sprint in openSprints()")  ─→ 25 issues, status distribution
   2) get_repository_statistics(owner, repo)         ─→ commit activity, contributors
   3) synthesize: "Of the 25 issues in the current sprint, 18 Done, 5 In Progress, 2 Blocked.
                   This week's commit activity: 47 commits by 5 contributors. ..."
```

### 4. Knowledge Base

```
User: "What's our incident response process?"
→ Orchestrator: intent=KNOWLEDGE → answer_general_questions(query)
→ bedrock-agent-runtime.retrieve(kb_id, query)
→ Returns 6 chunks from the "Incident Response" page of the internal dev playbook site
→ Sonnet 4 formats it into natural language and responds
```

## Key Learning Points

1. **Adding an MCP server requires no code changes** — the Gateway automatically exposes the new tools. Only update the Orchestrator routing table.
2. **Semantic search automates tool selection** — whether the user says "issue" or "ticket", it matches `search_issues`.
3. **For multi-service, the LLM decides the sequence** — providing just 1–2 MULTI-pattern examples in the system prompt generalizes.

## CDK app.py (for this example)

```python
import os
from pathlib import Path
import aws_cdk as cdk
from dotenv import load_dotenv

from src.stacks.orchestrator_agent_stack import OrchestratorAgentCoreStack
from src.stacks.jira_mcp_stack import JiraMcpAgentCoreStack
from src.stacks.github_mcp_stack import GitHubMcpAgentCoreStack
from src.stacks.knowledge_base_stack import KnowledgeBaseStack
from src.stacks.agentcore_gateway_stack import AgentCoreGatewayStack

load_dotenv(Path(__file__).parent / ".env")
app = cdk.App()

orchestrator = OrchestratorAgentCoreStack(app, "MultiAgentOrchestrator")
jira = JiraMcpAgentCoreStack(app, "JiraMcp")
github = GitHubMcpAgentCoreStack(app, "GitHubMcp")
kb = KnowledgeBaseStack(app, "DevPlaybookKnowledgeBase")
gateway = AgentCoreGatewayStack(app, "AgentCoreGateway", mcp_stacks={"jira": jira, "github": github})

jira.add_dependency(orchestrator)
github.add_dependency(orchestrator)
gateway.add_dependency(jira)
gateway.add_dependency(github)

app.synth()
```

## Cost Estimate (us-east-1, monthly, 1 dev team ≈ 50 queries/day)

| Item | $ |
|---|---|
| Runtime invocation (Orchestrator + 2 MCP) | ~$8 |
| Gateway request | ~$2 |
| Memory (short-term, 7d expiry) | ~$1 |
| Bedrock Sonnet 4 (50 query/d, 3K avg tokens) | ~$15 |
| KB OpenSearch Serverless (2 OCU minimum) | ~$345 ★ |
| Cognito (small team) | $0 |
| **Total** | **~$371/mo** |

> KB cost is dominant. With Pinecone Free or no KB, **~$26/mo**.

## Variant: Lightweight version without KB

When there is no KB, replace `answer_general_questions` with the following:

```python
@tool
def search_aws_docs(query: str) -> dict:
    """Search AWS documentation via AWS Knowledge MCP server (Gateway target)."""
    # → Add AWS Knowledge MCP as another Gateway target and call recursively
```

Or replace the KB itself with **AWS Knowledge MCP** — a retrieval-as-a-service already hosted by AWS.

## .env.example

```
JIRA_BASE_URL=https://your-instance.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-jira-api-token
GITHUB_TOKEN=your-github-personal-access-token
GITHUB_USERNAME=your-github-username
COGNITO_TEST_USERNAME=testuser
COGNITO_TEST_PASSWORD=YourSecurePassword123!
```
