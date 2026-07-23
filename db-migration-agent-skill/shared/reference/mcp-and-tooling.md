# MCP Servers & AWS Agent Toolkit Integration

> How this skill uses MCP tools vs the AWS CLI, which servers to recommend, and how to
> chain the official AWS skills. Convention follows the Agent Toolkit for AWS:
> **MCP-first for volatile facts and sandboxed execution; AWS CLI fallback always works.**
> Verified against live AWS docs/GitHub as of 2026-07. Re-verify before publishing —
> this area moves fast.

## The rule (tiered, based on live test evidence)

- **Homogeneous engagements: the Agent Toolkit is recommended, not required.** Every
  instruction in this skill is executable with the plain AWS CLI + a DB client, and the
  full workflow works end-to-end without any MCP connected.
  When the toolkit IS connected, prefer it for the stated purposes (volatile-fact
  verification, `aws___call_aws` audited execution, DB queries without local clients).
  When absent, record **"MCP: not connected — CLI fallback"** in the Phase 0 preflight
  table plus the one real degradation: engine-version/regional facts were verified by
  CLI queries rather than live docs, so re-check anything version-sensitive against the
  console at GATE 2.
- **Heterogeneous engagements: the Agent Toolkit is REQUIRED.** Schema/code conversion
  chains to AWS's official `dms-schema-conversion` skill via `retrieve_skill`, and
  assessing conversion coverage without it means hand-driving DMS Schema Conversion
  through the CLI — slower and error-prone enough that the engagement should not
  proceed on the fallback. If the customer environment can't connect the toolkit,
  treat that as a Phase 0 blocker for the heterogeneous path (fix connectivity, or run
  the conversion workstream in an environment that has it).

## Primary: the managed AWS MCP Server (Agent Toolkit for AWS)

The official successor to the awslabs local servers. Remote, IAM-gated, free.
Repo: `github.com/aws/agent-toolkit-for-aws` · Docs: `docs.aws.amazon.com/agent-toolkit/`

**Install (Claude Code):**
```
/plugin install aws-core@claude-plugins-official
```
or manual `.mcp.json` (SigV4 via proxy; requires AWS CLI ≥ 2.32, `aws login`, uv):
```json
{"mcpServers": {"aws-mcp": {"command": "uvx", "args": [
  "mcp-proxy-for-aws@latest", "https://aws-mcp.us-east-1.api.aws/mcp",
  "--metadata", "AWS_REGION=<operating-region>"]}}}
```

**Tools this skill uses:**

| When | Tool | Why |
|------|------|-----|
| Verify engine-version availability, DMS source/target support, regional availability | `aws___get_regional_availability`, `aws___search_documentation` | These facts churn; never answer from memory |
| Read exact RDS/DMS API or procedure detail | `aws___read_documentation` | e.g. current `rds_restore_database` constraints |
| Run any AWS API call (DMS, RDS, EC2, Route 53, Secrets Manager) | `aws___call_aws` | Audited alternative to `aws` CLI; adds `aws:CalledViaAWSMCP` condition key |
| Long-running operations (restore-db-cluster-from-s3, DMS task) | `aws___get_tasks` | Poll instead of blocking |
| Load official AWS skills at runtime | `aws___retrieve_skill` | See "Chaining official skills" below |

## Chaining official AWS skills (do NOT duplicate them)

| Situation | Delegate to | How |
|-----------|-------------|-----|
| **Heterogeneous** schema conversion (Oracle/SQL Server → Aurora/PG/MySQL) | **`dms-schema-conversion`** skill (Agent Toolkit, `skills/specialized-skills/migration-and-modernization-skills/`) | `Load "dms-schema-conversion" skill using the retrieve_skill tool.` — covers DMS SC projects, data providers, metadata import/conversion, action items. Then return here for data movement + cutover. |
| Target-engine operational questions post-migration | `amazon-aurora-mysql`, `amazon-aurora-postgresql`, `rds-oracle`, `rds-sqlserver`, `rds-oss`, `rds-db2` skills | Same `retrieve_skill` mechanism, or the `aws-database` router skill in the `aws-core` plugin |

This skill's own ground is what none of those cover: end-to-end **data migration
orchestration** — assessment, method selection, DMS replication config, native paths,
client discovery, cutover, reverse replication, rollback.

## Companion local MCP servers (optional, per engagement)

All are `uvx awslabs.<name>@latest`, env `AWS_PROFILE`/`AWS_REGION`. Credentials via
Secrets Manager ARN — consistent with this skill's no-passwords-in-argv rule.

| Server | Use in this skill | Notes |
|--------|-------------------|-------|
| `awslabs.mysql-mcp-server` | Query source EC2/on-prem MySQL/MariaDB (assessment SQL) and target Aurora MySQL (validation SQL) without a local `mysql` client | `mysqlwire` for self-hosted sources; read-only by default — keep it that way for the source |
| `awslabs.postgres-mcp-server` | Same for PostgreSQL / Aurora PostgreSQL | `pgwire` / `pgwire_iam` / `rdsapi` connection methods |
| `awslabs.oracle-mcp-server` | Source/target Oracle inventory queries | Read-only via `SET TRANSACTION READ ONLY` |
| `awslabs.mssql-mcp-server` | Source/target SQL Server inventory queries | Secrets Manager auth |
| `awslabs.cloudwatch-mcp-server` | Watch DMS task metrics (`CDCLatencySource/Target`), alarms, log-insights during Phases 6–8 | `get_metric_data`, `get_active_alarms`, `execute_log_insights_query` |
| `awslabs.aws-pricing-mcp-server` | Cost estimate at GATE 2 (RDS/Aurora instance, storage, DMS instance, data transfer) | `get_pricing`, `generate_cost_report`; IAM `pricing:*` only |

## ⚠️ Known trap

**Do not install `awslabs.aws-dms-mcp-server` from PyPI.** As of 2026-07 that package
name is squatted by a third party ("security research") — it is **not AWS**. There is no
official DMS MCP server; drive DMS through `aws___call_aws` or the AWS CLI.

Also superseded (don't recommend for new setups): `awslabs.aws-api-mcp-server` (replaced
by the managed AWS MCP Server; AWS docs advise removing it to avoid tool conflicts) and
`awslabs.cost-explorer-mcp-server` (folded into `awslabs.billing-cost-management-mcp-server`).

## Fallback matrix (no MCP connected)

| Need | Fallback |
|------|----------|
| AWS API calls | `aws` CLI (the skill's snippets are already CLI-form) |
| Source DB queries with no local client | SSM Send-Command / port-forwarding paths in [source-assessment.md](source-assessment.md) |
| Doc verification | Ask the user to confirm against the console, and mark the assumption in `migration-plan.md` |
| Pricing | AWS Pricing Calculator link with the parameters filled into `migration-plan.md` |
