# Optional MCP guidance

## Recommended official AWS MCP servers

MCP improves discovery and validation, but it is not required. The servers below are published by AWS in [awslabs/mcp](https://github.com/awslabs/mcp). Installed names differ between environments, so match on capability rather than on a hard-coded server name.

| Server | Package or endpoint | Use in this skill | Safe default |
|---|---|---|---|
| AWS Knowledge MCP Server | Remote: `https://knowledge-mcp.global.api.aws` | Current Hybrid Nodes requirements, supported OS and Kubernetes versions, CNI guidance, regional support, What's New, and pricing pages (`aws___search_documentation`, `aws___read_documentation`, `aws___recommend`) | No AWS credentials required |
| Amazon EKS MCP Server | `awslabs.eks-mcp-server` | Cluster and VPC config including remote node and pod CIDRs (`get_eks_vpc_config`), EKS insights (`get_eks_insights`), troubleshooting guide search, nodes, DaemonSets, events, and logs after join | Read-only by default. Do not pass `--allow-write`. Pass `--allow-sensitive-data-access` only when the user approves log, event, or insight access |
| AWS Pricing MCP Server | `awslabs.aws-pricing-mcp-server` | Current Hybrid Nodes fee tiers, EC2, EBS, Transit Gateway, VPN, Direct Connect, NAT, and data-transfer rates | Read-only Pricing API |
| AWS IaC MCP Server | `awslabs.aws-iac-mcp-server` | `AWS::EKS::Cluster.RemoteNetworkConfig`, CDK construct signatures, template validation, and security checks | Documentation and validation |
| AWS MCP Server (preview) | Remote via `mcp-proxy-for-aws` | Read-only inventory of VPCs, route tables, security groups, Transit Gateway, VPN, Direct Connect, access entries, add-ons, and SSM managed instances | Run under a read-only IAM role |

If the AWS MCP Server is not available, `awslabs.aws-api-mcp-server` with `READ_OPERATIONS_ONLY=true` covers the same read-only inventory. For Terraform-owned clusters, the HashiCorp Terraform MCP server can verify current AWS provider resources and module inputs.

### Example configuration

The exact file and syntax depend on the tool (Claude Code, Kiro, or Codex). The servers and arguments stay the same:

```json
{
  "mcpServers": {
    "aws-knowledge-mcp-server": {
      "type": "http",
      "url": "https://knowledge-mcp.global.api.aws"
    },
    "awslabs.eks-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.eks-mcp-server@latest"],
      "env": { "AWS_PROFILE": "<read-only-profile>", "AWS_REGION": "<cluster-region>" }
    },
    "awslabs.aws-pricing-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.aws-pricing-mcp-server@latest"],
      "env": { "AWS_PROFILE": "<read-only-profile>", "AWS_REGION": "us-east-1" }
    },
    "awslabs.aws-iac-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.aws-iac-mcp-server@latest"]
    }
  }
}
```

Recommend installing a missing server only when it replaces a block of manual questions. Do not install servers or change tool configuration without the user's approval.

## Operating rules

1. Use repository search before external documentation when deciding IaC ownership.
2. Use current official AWS documentation for constraints and pricing.
3. Start with read-only calls and least-privilege credentials.
4. Confirm account, Region, cluster, and environment before any write.
5. Summarize the intended mutation and rollback, then request explicit approval.
6. Never expose SSM activation codes, private keys, certificates, kubeconfigs, or Terraform state in prompts or reports.
7. Do not let a tool's existence determine the architecture; tools validate a decision made from requirements.
8. If no MCP is installed, provide exact read-only CLI commands or console fields to collect the missing facts.

## Suggested discovery order

1. Workload class, data locality, client entry, and failure requirements.
2. Local IaC and runbooks.
3. EKS cluster configuration and access entries.
4. VPC CIDRs, subnets, control-plane route tables, and security groups.
5. Existing TGW, Direct Connect, VPN, peering, and DNS architecture.
6. Remote-site CIDRs, routers, firewalls, hosts, and PKI.
7. Kubernetes add-ons and workload traffic requirements.
8. Current public pricing and any customer-specific private pricing.