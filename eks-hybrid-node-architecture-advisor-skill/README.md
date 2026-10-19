# eks-hybrid-node-architecture-advisor-skill

An Agent Skill that guides architecture and operations decisions for attaching remote capacity to an **existing** Amazon EKS cluster with [EKS Hybrid Nodes](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html). Primary focus: cloud-hosted Hybrid Nodes — EC2 in another AWS Region (cross-Region AWS-to-AWS). The same framework covers documented on-premises and edge deployments. It starts from your current clusters, networks, infrastructure as code, hosts, and operating model. It does not replay a fixed reference deployment.

> **Support posture:** AWS documents Hybrid Nodes running on cloud-hosted infrastructure (EC2 in another Region, Local Zones, or another cloud) as not covered by AWS Support. The pattern works, and making it run reliably is this skill's primary purpose. The skill discloses this once, then applies the same design rigor as any production architecture.

## Trigger phrases

```
"Add hybrid nodes to my EKS cluster"
"Run inference on GPUs in another Region from my existing EKS cluster"
"Design an EKS Hybrid Nodes architecture"
"How do I safely remove a hybrid node and stop billing?"
```

## Install

### Claude Code
```bash
mkdir -p ~/.claude/skills
ln -sf "$(pwd)/claude-code/skills/eks-hybrid-node-architecture-advisor" ~/.claude/skills/eks-hybrid-node-architecture-advisor
ln -sf "$(pwd)/shared" ~/.claude/skills/eks-hybrid-node-architecture-advisor/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
ln -sf "$(pwd)/kiro/skills/eks-hybrid-node-architecture-advisor" ~/.kiro/skills/eks-hybrid-node-architecture-advisor
ln -sf "$(pwd)/shared" ~/.kiro/skills/eks-hybrid-node-architecture-advisor/shared
```

### Codex
```bash
mkdir -p ~/.agents/skills
ln -sf "$(pwd)/codex/skills/eks-hybrid-node-architecture-advisor" ~/.agents/skills/eks-hybrid-node-architecture-advisor
ln -sf "$(pwd)/shared" ~/.agents/skills/eks-hybrid-node-architecture-advisor/shared
```

(For Windows or if symlinks are unavailable, use the `cp -r` form documented in [CONTRIBUTING.md](../CONTRIBUTING.md).)

## Outputs

This skill is advisory. It produces documents, not deployed infrastructure:

- **Architecture briefs** — workload placement, six hard gates, endpoint and ingress decisions, cost model, IaC ownership plan, implementation sequence, and validation gates.
- **Requirements deltas** — what the existing cluster, add-ons, hosts, external services, storage, and accelerators must change before rollout.
- **Remote node build and bootstrap plans** — the ordered host lifecycle from image build through `nodeadm`, post-init runtime customization, CNI, GPU discovery, and billing-stop cleanup.
- **SRE guides and runbooks** — safe node removal, host reset for reuse, failure isolation, symptom-driven troubleshooting, and direct FAQ answers.

## Design rules the skill enforces

- Distributed training stays within one locality; inference uses complete, independently callable replicas.
- A public EKS API endpoint never removes the private control-plane return path (TCP 10250).
- Containerd runtime customization such as `nvidia-ctk` runs after `nodeadm init`, never before.
- Deleting the Kubernetes Node object is what stops Hybrid Nodes billing; cordon, drain, shutdown, and `nodeadm uninstall` do not.
- For machines above 32 vCPUs, typical for GPU hosts, the skill tells you to contact your AWS account team for a private pricing quote instead of relying on the public per-vCPU rate.
- Existing IaC ownership is preserved; the skill never generates a replacement cluster.
- Discovery and MCP usage stay read-only; no AWS, Kubernetes, network, or source mutation without explicit approval.

## MCP requirements

Official AWS MCP servers from [awslabs/mcp](https://github.com/awslabs/mcp). All are optional and used read-only; the skill falls back to the AWS CLI, `kubectl`, or questions when a server is missing. Setup details and an example configuration: [`shared/reference/mcp-guidance.md`](./shared/reference/mcp-guidance.md).

| MCP server | Purpose | Required? |
|---|---|---|
| AWS Knowledge MCP Server (`https://knowledge-mcp.global.api.aws`) | Current Hybrid Nodes requirements, supported versions, pricing pages | Recommended |
| Amazon EKS MCP Server (`awslabs.eks-mcp-server`) | Cluster VPC config with remote node and pod CIDRs, EKS insights, node and add-on inspection | Recommended |
| AWS Pricing MCP Server (`awslabs.aws-pricing-mcp-server`) | Hybrid Nodes fee tiers, EC2, interconnect, and data-transfer rates | Optional |
| AWS IaC MCP Server (`awslabs.aws-iac-mcp-server`) | `RemoteNetworkConfig` and CDK signature verification | Optional |
| AWS MCP Server (preview) | Read-only VPC, routing, and access-entry inventory under a read-only IAM role | Optional |

## Knowledge sources

All real knowledge lives in [`shared/reference/`](./shared/reference/):

| File | Contents |
|---|---|
| `discovery-questionnaire.md` | Quick orientation, auto-discovery map, workload, cluster, network, identity, and cost questions |
| `decision-framework.md` | Placement, support, cluster scope, connectivity, endpoints |
| `hybrid-networking-guide.md` | Six-network mental model, endpoint modes, routes, ports |
| `requirements-and-compatibility.md` | Cluster, add-on, host, external, storage, upgrade deltas |
| `remote-node-setup.md` | Ordered host lifecycle state machine (bootstrap → removal) |
| `sre-operations-and-faq.md` | Runbooks, troubleshooting map, FAQ, SRE guide scope |
| `implementation-checklists.md` | Per-layer implementation and validation gates |
| `cost-model.md` | Hybrid Nodes fee tiers, transfer model, adjacent costs |
| `mcp-guidance.md` | Optional read-only MCP discovery and validation |
| `sources.md` | Official AWS documentation links |

## Layout

```
eks-hybrid-node-architecture-advisor-skill/
├── README.md                                                        (this file)
├── claude-code/skills/eks-hybrid-node-architecture-advisor/SKILL.md (md5-identical to the other two)
├── kiro/skills/eks-hybrid-node-architecture-advisor/SKILL.md        (md5-identical)
├── codex/skills/eks-hybrid-node-architecture-advisor/SKILL.md       (md5-identical)
├── shared/reference/
└── evals/
    ├── cross-region-gpu-inference-scenario.md
    └── node-removal-billing-stop-scenario.md
```

## Related skills

- [llm-gateway-governance-skill](../llm-gateway-governance-skill/) — a governed LiteLLM gateway with virtual keys, budgets, and Bedrock Guardrails. Use it when the cluster-Region gateway in front of remote inference replicas needs per-caller keys and spend controls.

## Disclaimer

This is sample content for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory, and compliance requirements before deployment. The skill reads current official AWS documentation at runtime for pricing, supported versions, and compatibility; verify its recommendations before applying them.
