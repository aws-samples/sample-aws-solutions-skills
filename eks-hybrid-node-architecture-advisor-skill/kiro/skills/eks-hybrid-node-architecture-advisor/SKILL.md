---
name: eks-hybrid-node-architecture-advisor
description: |
  Design and operate remote capacity attached to an existing Amazon EKS cluster with
  EKS Hybrid Nodes, primarily EC2 in another AWS Region (cross-Region AWS-to-AWS), and
  also on-premises or edge hosts. Use for architecture decisions, training or inference
  placement, networking and endpoints, Terraform or CDK ownership, CPU or GPU host
  lifecycle, SRE guides and runbooks, troubleshooting, cordon and drain, node removal,
  host reset and reuse, identity cleanup, upgrades, or billing-stop verification without
  recreating the cluster. Use when the user says "add hybrid nodes to my EKS cluster",
  "cross-Region EKS nodes", "run inference on GPUs in another Region", "EKS Hybrid Nodes
  architecture", "remove a hybrid node", or equivalent phrases in the user's language.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---

# EKS Hybrid Node Architecture Advisor

## Purpose

Guide architecture and operations for adding Hybrid Nodes to an **existing** Amazon EKS cluster. Primary focus: cloud-hosted Hybrid Nodes — EC2 in another AWS Region attached to an existing cluster. The same framework covers documented on-premises and edge deployments. Start from the customer's current clusters, networks, infrastructure as code, hosts, workloads, and operating model. Produce architecture decisions, SRE guides, runbooks, or direct FAQ answers from the requested scope. Do not reproduce a fixed reference deployment.

This skill is advisory by default. It produces a decision, cost model, ownership plan, implementation sequence, validation gates, and rollback plan. It MUST NOT mutate AWS, Kubernetes, network, or source resources unless the user separately asks for implementation and approves the proposed changes.

## Non-goals

- Do not create a new EKS cluster unless the analysis concludes that a separate cluster is safer and the user approves that scope.
- Do not claim AWS Support coverage for cloud-hosted Hybrid Nodes (EC2 in another Region, Local Zones, or another cloud). AWS documents this placement as unsupported, but it works and is a primary use case for this skill: disclose the support posture once, then design the pattern to run reliably. Do not block or discourage the design on support status alone.
- Do not assume full pod routing is required. Derive it from workload flows.
- Do not copy CIDRs, endpoint modes, identity choices, or bootstrap code from a reference environment.

## Task routing

Load only the references needed for the current task.

- **Architecture or adding capacity:** follow the full required workflow below.
- **A direct operations question:** go to step 8 and read [sre-operations-and-faq.md](shared/reference/sre-operations-and-faq.md); also read [remote-node-setup.md](shared/reference/remote-node-setup.md) for removal, reset, or reuse. Do not require the full discovery questionnaire or architecture brief to answer a focused question.
- **A full SRE guide:** use step 8 and that reference's guide scope and runbook template; discover only the environment facts needed to make the guide specific.

Reuse facts already supplied in the conversation or verified from the workspace. Skip discovery questions that are already answered or irrelevant to the task.

## Required workflow

### 1. Discover the environment

Discovery is the first action of every architecture engagement. Read [discovery-questionnaire.md](shared/reference/discovery-questionnaire.md).

Open with its quick orientation questionnaire — four short questions in one message: direction (AWS-to-AWS, on-premises, another cloud), whether the EKS cluster exists, whether networking is in place, and the workload in one line. The answers scope all remaining discovery; for example, AWS-to-AWS removes every on-premises hardware, power, and facilities question.

Then run the auto-discovery map before asking anything else:

1. **AWS account, read-only:** when credentials are available, collect the cluster configuration, VPC CIDRs, subnets, route tables, security groups, existing connectivity (Transit Gateway, VPN, Direct Connect, peering), access entries, and add-ons.
2. **IaC context:** search the local repository for the cluster definition — owning tool, modules, states, provider versions, and native `RemoteNetworkConfig` support.
3. **Verify with the user:** present everything found as a verified-facts summary and ask the user to confirm or correct it.
4. **Ask only the gaps:** questions discovery cannot answer — intent, workload behavior, SLOs, budget and team ownership, maintenance windows, future scale — go to the user in one concise batch.

When access is missing, propose it before falling back to manual questions. Offer the user concrete options and explain that each one replaces a block of questions:

- **AWS account:** a read-only credential profile or role for the account that owns the cluster.
- **EKS context:** a kubeconfig for the cluster, or an EKS/Kubernetes MCP server if one can be installed.
- **IaC:** add the cluster's Terraform or CDK repository to the workspace, or point to the directory that defines the cluster.
- **Documentation and pricing:** AWS documentation and pricing MCP servers to refresh requirements and rates at runtime.

Accept whatever subset the user grants and fill the rest through questions. Discovery MUST stay read-only. Never mutate AWS, Kubernetes, or repository state while gathering facts.

### 2. Classify the workload and placement

Analyze the use case before selecting a cluster, network, or implementation. Classify the workload as distributed training, independent training, inference, or another workload. Record data location, communication pattern, state, latency, streaming, failover, and client-entry requirements.

- A distributed training job MUST run entirely in one locality: either the cluster Region or one remote site. Its workers, coordinator, model partitions, and collective communication MUST NOT span the WAN. Independent jobs may run in different localities.
- Inference MAY use local and remote capacity when each locality runs complete, independently callable replicas. Tensor parallelism, pipeline parallelism, model state, or one request MUST NOT span the WAN.
- Keep data and compute together unless measured transfer, latency, and compliance tradeoffs justify movement.

### 3. Apply the six hard gates

The design MUST pass these gates before implementation:

1. **Workload topology:** Prove the placement supports one-locality training or complete inference replicas, required data flows, and endpoint behavior.
2. **Support posture:** Classify the hosts as documented on-premises/edge infrastructure or cloud-hosted (works, not covered by AWS Support). This gate is informational: record the classification and disclosure, then continue.
3. **Cluster compatibility:** Verify IPv4, authentication mode, public-only or private-only EKS API behavior, remote DNS resolution, Kubernetes version, add-ons, and control-plane ENI route-table ownership.
4. **Network correctness:** Prove non-overlapping node and pod CIDRs, node-to-API TCP 443, control-plane-to-kubelet TCP 10250, required DNS, and bidirectional Layer 3 routing for every workload flow.
5. **Identity and lifecycle:** Select SSM hybrid activations or IAM Roles Anywhere and define secure join, rotation, drain, uninstall, and deregistration.
6. **Economics:** Estimate Hybrid Nodes, workload data-transfer, connectivity, and compute costs, then obtain an explicit cost-owner decision.

If a gate fails, stop and explain what must change. Do not hide a failed gate behind an implementation workaround. Read [hybrid-networking-guide.md](shared/reference/hybrid-networking-guide.md) before passing the cluster compatibility or network correctness gates. Read [requirements-and-compatibility.md](shared/reference/requirements-and-compatibility.md) and produce its current-state delta before passing cluster compatibility.

### 4. Build the cost decision

Read [cost-model.md](shared/reference/cost-model.md). Always show the public Hybrid Nodes fee separately from host compute, storage, network connectivity, data transfer, NAT, gateway, observability, and support costs. Control-plane-to-node traffic is negligible; the cost risk is workload access to main-Region services (S3, service endpoints, data streams) crossing the interconnect per GiB, at a rate that depends on the interconnect type.

Ask whether the customer has negotiated private pricing. Never assume a discount. If private pricing is unknown, show the public-price estimate and label it as a decision risk. When any node class exceeds 32 vCPUs per machine (typical for GPU hosts), tell the customer to contact their AWS account team for a private pricing quote.

The public baseline at authoring time started at `$0.020 per vCPU-hour` for the first 576,000 monthly vCPU-hours aggregated by AWS Region. Refresh this value from the official EKS pricing page whenever the skill runs. If live lookup is unavailable, label the rate as unverified and name the pricing evidence still needed; never present the cached value as current.

### 5. Select the architecture

Read [decision-framework.md](shared/reference/decision-framework.md) and [hybrid-networking-guide.md](shared/reference/hybrid-networking-guide.md). Produce at least two viable options when the requirements allow them, including a simpler baseline and a scalable target design. Compare:

- workload placement and data locality
- support status and operational risk
- cluster and site blast radius
- connectivity and route ownership
- pod-routing behavior
- EKS API endpoint and application ingress
- availability and WAN failure behavior
- security and identity lifecycle
- implementation effort
- monthly cost

State one recommendation and explain why it wins. Explicitly identify assumptions and unresolved decisions. The skill MUST recommend an application endpoint architecture when requirements are sufficient. If facts are missing, it MUST compare viable endpoint options, identify the missing evidence, and assign a decision owner.

### 6. Align with existing IaC ownership

Do not introduce a second control plane for the same resource.

- If Terraform owns the cluster, prefer updating its native `remote_network_config` and access entries in the owning state or module. Keep remote site connectivity and repeatable host lifecycle in separate modules or states with explicit outputs and dependencies.
- If CloudFormation or CDK owns the cluster, update `AWS::EKS::Cluster.RemoteNetworkConfig` through the owning stack. In CDK, verify the exact `CfnCluster.RemoteNetworkConfigProperty` signature for the pinned `aws-cdk-lib` version before generating code.
- If the cluster is not managed by IaC, propose an idempotent CLI or console update as a transition step and create an ownership plan. Do not hide an imperative `update-cluster-config` inside IaC unless the user accepts the drift and rollback implications.
- If the customer uses another tool, preserve its ownership and update semantics rather than forcing Terraform or CDK.

When modifying an existing remote network list, include all desired existing and new CIDRs. EKS replaces the complete list that is supplied.

### 7. Define requirements, implementation, and validation gates

Read [requirements-and-compatibility.md](shared/reference/requirements-and-compatibility.md), [remote-node-setup.md](shared/reference/remote-node-setup.md), and [implementation-checklists.md](shared/reference/implementation-checklists.md). First produce a current-state delta for the existing cluster, add-ons, remote hosts, external dependencies, storage, accelerators, and upgrade compatibility. Then produce a remote node build and bootstrap plan for every node class.

Sequence work so each layer can be validated independently:

1. Workload classification, locality, and data-flow inventory
2. Cluster and site inventory plus CIDR allocation
3. Requirements delta and compatibility matrix
4. Cluster compatibility and `RemoteNetworkConfig`
5. Underlay routing, DNS, security groups, and firewalls
6. Hybrid Nodes IAM role and `HYBRID_LINUX` access entry
7. SSM or IAM Roles Anywhere credentials
8. Golden image, network preflight, and `nodeadm install`
9. Secure NodeConfig rendering and `nodeadm config check`
10. `nodeadm init`, then post-init runtime customization
11. Cilium readiness
12. Hardware discovery and device plugins when needed
13. Storage and workload readiness
14. Locality-specific scheduling, Services, ingress, reboot, and traffic tests
15. Upgrade rehearsal, drain, deregistration, rollback, and billing-stop verification

Do not proceed past a failed gate. A registered node remaining `NotReady` before Cilium installation is expected; it is not proof that the full design works. Do not apply containerd runtime customization before `nodeadm init`, because initialization can overwrite it.

### 8. Answer operations questions and author the SRE guide

Read [sre-operations-and-faq.md](shared/reference/sre-operations-and-faq.md) when the user asks how to operate, diagnose, isolate, drain, remove, clear, reset, reuse, upgrade, or decommission a Hybrid Node, or asks for an SRE guide.

For direct questions, answer with the short conclusion, impact, preconditions, ordered procedure, objective success evidence, rollback, and escalation. Resolve ambiguous words such as “remove” or “clear” into the intended lifecycle boundary: cordon, drain, Node deletion, `nodeadm uninstall`, CNI cleanup, identity revocation, host reuse, or complete sanitization.

Start with read-only evidence. Do not execute disruptive actions without a named target and user approval. Preparing a runbook or commands with placeholders does not authorize their execution. Node deletion stops Hybrid Nodes billing; cordon, drain, host shutdown, and `nodeadm uninstall` do not replace that step. Never recommend blind deletion of Kubernetes, CNI, credential, or mounted-data directories.

When producing an architecture brief, include an environment-specific SRE guide outline and required runbooks. When the user explicitly asks for the full SRE guide, generate routine operations, symptom-driven incident procedures, FAQ answers, evidence collection, escalation, game days, and removal/reset runbooks with named owners and success criteria.

### 9. Use MCP only where it improves evidence

Read [mcp-guidance.md](shared/reference/mcp-guidance.md). Use the official AWS MCP servers listed there when available, and recommend the missing ones when they would replace manual questions: AWS Knowledge, Amazon EKS, AWS Pricing, AWS IaC, and read-only AWS inventory.

MCP is a good idea for discovery and validation because it reduces guessed resource IDs, stale API signatures, and undocumented assumptions. It MUST remain optional and MUST start read-only. The architecture cannot depend on a particular MCP server being installed.

| When | Official AWS MCP server | Call |
|---|---|---|
| Hybrid Nodes requirements, supported versions, regional support | AWS Knowledge MCP Server | `aws___search_documentation`, `aws___read_documentation` |
| Current Hybrid Nodes, EC2, and data-transfer rates | AWS Pricing MCP Server or AWS Knowledge MCP Server | pricing lookup, or read the EKS pricing page |
| Cluster VPC config, remote node and pod CIDRs, EKS insights | Amazon EKS MCP Server (read-only default) | `get_eks_vpc_config`, `get_eks_insights` |
| VPC, route table, TGW/VPN/peering, and access-entry discovery | AWS MCP Server under a read-only role | describe/list calls only |
| `RemoteNetworkConfig` and CDK L1 signatures | AWS IaC MCP Server | resource and property lookup |
| Node, DaemonSet, events, and add-on state after join | Amazon EKS MCP Server | `list_k8s_resources`, `get_k8s_events` |

### 10. Produce the architecture brief

For an architecture request, return this exact structure. For a direct operations question, use the operations response contract in [sre-operations-and-faq.md](shared/reference/sre-operations-and-faq.md). For a full SRE guide request, use that reference's SRE guide scope and runbook template.

1. **Recommendation** — workload class, locality decision, architecture decision, and support classification.
2. **Known facts and assumptions** — separate verified facts from assumptions.
3. **Challenge answers** — workload, data, clusters, sites, connectivity, CIDRs, EKS API DNS and forward path, control-plane return path, pod routing, application ingress, identity, IaC, and pricing.
4. **Options considered** — concise comparison table with at least two options when possible.
5. **Target architecture** — Mermaid or text diagram plus traffic-flow and workload-placement tables.
6. **Endpoint and ingress** — explain the EKS API endpoint mode, control-plane return path, Kubernetes Services, cluster-Region ingress, remote-local ingress, and recommended global endpoint.
7. **Requirements and compatibility delta** — classify every cluster, add-on, host, external service, storage, accelerator, and upgrade dependency; include current state, required state, action, owner, and validation.
8. **Cost estimate** — vCPU-hours, public Hybrid Nodes fee, private-pricing status, directional transfer, connectivity, and adjacent costs. For any node class above 32 vCPUs per machine, include the recommendation to request a private pricing quote from the AWS account team.
9. **IaC ownership plan** — files, stacks, modules, states, and interfaces to change; do not generate a replacement cluster.
10. **Remote node build and bootstrap plan** — per node class, separate golden-image and first-boot actions; show SSM or IAM Roles Anywhere, CPU or accelerator, proxy, `nodeadm`, post-init runtime, CNI, storage, reboot, upgrade, and removal gates.
11. **SRE operations plan** — monitoring and alert coverage, routine operations, required incident runbooks, safe removal and reset levels, evidence bundle, escalation owners, game days, and FAQ topics.
12. **Implementation sequence** — ordered, reversible phases with approval points.
13. **Validation and rollback** — objective workload, locality, DNS, route, endpoint, network, host, storage, accelerator, reboot, operations, and billing-stop checks.
14. **Open decisions** — named owner, options, and required evidence for every blocker.
15. **Sources** — current official AWS links used for constraints, compatibility, lifecycle, operations, and pricing.

## Stop conditions

Stop and request a decision when any of these conditions applies:

- The workload type, one-locality training boundary, or complete-replica inference boundary remains unknown.
- For a cloud-hosted design, the support-posture disclosure has not been made. One acknowledgment is enough; do not repeat it as a blocker.
- CIDRs overlap or the complete connected network inventory is unknown.
- No team owns bidirectional routes, firewalls, and DNS.
- Expected latency, bandwidth, reliability, or workload transfer volume has not been measured.
- The existing cluster owner has not approved `RemoteNetworkConfig` and access changes.
- Public Hybrid Nodes cost is material and the budget owner or private-pricing status is unknown.
- The proposed IaC change would create duplicate ownership or replace the existing cluster.
- A production design stores activation codes, certificates, private keys, or rendered NodeConfig in source control or unprotected state.

## Knowledge sources

- [Discovery questions](shared/reference/discovery-questionnaire.md)
- [Architecture decision framework](shared/reference/decision-framework.md)
- [Hybrid networking guide](shared/reference/hybrid-networking-guide.md)
- [Requirements and compatibility](shared/reference/requirements-and-compatibility.md)
- [Remote node setup and lifecycle](shared/reference/remote-node-setup.md)
- [SRE operations guide and FAQ](shared/reference/sre-operations-and-faq.md)
- [Cost model](shared/reference/cost-model.md)
- [Implementation and validation](shared/reference/implementation-checklists.md)
- [Optional MCP guidance](shared/reference/mcp-guidance.md)
- [Sources and reference evidence](shared/reference/sources.md)
