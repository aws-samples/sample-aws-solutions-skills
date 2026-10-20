# Discovery and challenge questions

## Quick orientation — ask these four questions first

Before any deep discovery, ask the user four short direction questions in one message. Their answers scope everything that follows and remove entire question groups.

1. **Direction:** Where will the Hybrid Nodes run?
   - (a) EC2 in another AWS Region — AWS-to-AWS (this skill's primary pattern)
   - (b) On-premises or edge site
   - (c) Another cloud
   - (d) Not decided yet
2. **Cluster:** Does the EKS cluster that will own the nodes already exist?
3. **Networking:** Is connectivity between the cluster VPC and the remote location already in place (peering, Transit Gateway, Direct Connect, VPN)?
4. **Workload:** One line — training, inference, or something else?

Use the answers to scope discovery:

| Answer | Effect on discovery |
|---|---|
| Direction = AWS-to-AWS | Skip on-premises questions entirely: hardware, power, facilities, colocation, PXE, site routers, and firewall ownership. Host costs are EC2 and EBS. SSM hybrid activations are the usual identity path. |
| Direction = on-premises/edge | Include host, facilities, PKI, and site-network ownership questions. Skip EC2/EBS cost lines. Compare Direct Connect and VPN; VPC peering is not an option. |
| Direction = another cloud | Same as AWS-to-AWS mechanics plus cross-cloud egress rates and the other cloud's network constructs. |
| Cluster exists | Run the auto-discovery map against it. |
| No cluster yet | Stop: this skill targets existing clusters. Confirm whether the user wants cluster creation guidance elsewhere first. |
| Networking in place | Discover and validate the existing path (routes, return path, DNS, MTU) instead of designing a new one. |
| No networking yet | Include the connectivity design questions and the interconnect cost comparison. |

Fill answers yourself before asking anything else. When the agent has access to the cluster's IaC repository, AWS credentials, or a kubeconfig, most technical questions below are discoverable. Run read-only discovery first, present the results as verified facts for confirmation, and ask the user only what discovery cannot answer.

## Auto-discovery map

| Question group | Source | How |
|---|---|---|
| Cluster estate (Region, version, CIDRs, endpoint mode, auth mode, ENI subnets) | AWS API or IaC repo | `aws eks describe-cluster`; parse the Terraform/CDK/CloudFormation cluster definition |
| VPC and networking (VPC CIDRs, secondary CIDRs, subnets, route tables, security groups) | AWS API | `aws ec2 describe-vpcs`, `describe-subnets`, `describe-route-tables`, `describe-security-groups` |
| Existing connectivity (TGW, VPN, Direct Connect, peering) | AWS API | `aws ec2 describe-transit-gateways`, `describe-vpn-connections`, `describe-vpc-peering-connections`; `aws directconnect describe-connections` |
| Add-ons, DaemonSets, webhooks | Kubernetes API | `aws eks list-addons`; `kubectl get daemonsets,mutatingwebhookconfigurations -A` when a kubeconfig exists |
| IaC ownership (tool, repo, modules, native `RemoteNetworkConfig` support) | Local repository | Search the repo for the cluster resource, provider versions, and state configuration |
| Existing Hybrid Nodes usage and access entries | AWS API | `aws eks list-access-entries`; check for `HYBRID_LINUX` entries and `remoteNetworkConfig` |
| Current pricing | Public pricing page or pricing MCP | Refresh rates at runtime |

Questions that discovery cannot answer — intent, workload behavior, SLOs, budget ownership, team ownership, maintenance windows, future scale — go to the user. Group them into one concise batch.

If none of these sources is available, do not silently fall back to asking everything. First propose that the user grant access: a read-only AWS credential profile, a kubeconfig or EKS MCP server, the cluster's Terraform or CDK repository added to the workspace, and documentation/pricing MCP servers. State which question groups each access replaces, accept the subset the user grants, and ask only what remains.

Read [mcp-guidance.md](mcp-guidance.md) for the discovery order and MCP capabilities. Start read-only; never mutate during discovery.

## Workload and placement — ask first

1. Is the workload distributed training, independent training, batch processing, online inference, or asynchronous inference?
2. For training, does one job span nodes? What framework, collective protocol, model parallelism, NCCL, EFA, RDMA, bandwidth, and latency does it require?
3. Where are the training dataset, preprocessing output, checkpoints, and model artifacts? What volume moves for each job and checkpoint?
4. Can every worker and dependency for one distributed job run entirely in the cluster Region or entirely at one remote site?
5. For inference, can each locality host a complete replica, including every model shard, cache, tokenizer, adapter, and dependency?
6. Is inference stateless or stateful? Does it require streaming, session affinity, cache consistency, model-version synchronization, or request replay?
7. Where are clients, what latency SLO applies, and what are the required RTO, RPO, failover, and degraded-mode behaviors?
8. Should traffic enter through the cluster Region, the remote site, or both? Who owns the customer-facing or global endpoint decision?
9. Must routing consider available model capacity, accelerator type, queue depth, or model version rather than health and latency alone?

## Intent and support boundary

1. What problem requires remote capacity in this existing cluster: data locality, latency, hardware availability, migration, resilience, or a demonstration?
2. Where will hosts run: customer-owned data center, edge site, factory, colocation, AWS Region, Local Zone, Outposts, or another cloud?
3. If hosts are cloud-based, confirm the decision owner knows AWS Support does not cover this placement. The pattern works and is this skill's primary use case; record the acknowledgment once and move on.
4. Why is attaching nodes to this cluster better than operating a separate cluster nearer the hosts? Consider WAN failure, upgrade coupling, blast radius, data governance, and team ownership.

## Cluster estate

1. How many EKS clusters exist today, and how many are expected over the next 12–36 months?
2. Which exact cluster should own the nodes, and can one remote site serve more than one cluster?
3. What are the cluster Region, Kubernetes version, IPv4 service CIDR, VPC CIDRs including secondary CIDRs, API endpoint mode, authentication mode, control-plane ENI subnets, and their route tables?
4. From the remote network, what IPs does the EKS endpoint DNS name resolve to? For private mode, which DNS conditional forwarder and Route 53 Resolver inbound endpoints answer the query?
5. Who owns cluster changes, endpoint mode, DNS, and control-plane ENI routes? What maintenance window and rollback process apply?
6. Which add-ons, admission webhooks, service meshes, observability agents, and security agents run as DaemonSets or Deployments?

## Existing connectivity and endpoint paths

1. Do you already have Direct Connect, Site-to-Site VPN, Transit Gateway, Virtual Private Gateway, VPC peering, Cloud WAN, or another routed connection?
2. Is the EKS API public-only or private-only? For public mode, what NAT or proxy source address reaches TCP 443? For private mode, how do remote resolvers return private control-plane addresses?
3. Can control-plane ENI subnets route privately to every remote node CIDR for TCP 10250, independent of the node-to-API path?
4. Who owns each gateway, attachment, route table, firewall, DNS resolver, and BGP policy?
5. Is connectivity transitive? VPC peering is not transitive.
6. Can the path sustain a tested 100 Mbps baseline and stay below 200 ms round-trip latency? These are AWS recommendations, not hard limits.
7. What redundancy, failover time, MTU, asymmetric-routing, and DNS requirements apply?
8. Which cluster subnet route tables must reach remote nodes and pods? Include control-plane ENI subnets, load-balancer subnets, and cloud-node subnets only when their flows require routes.

## Addressing and workload flows

1. What RFC 1918 or CGNAT CIDRs are available for remote nodes and remote pods?
2. Do they overlap any cluster VPC, secondary VPC, service, connected-site, or future expansion CIDR?
3. Must the EKS control plane reach remote pod IPs for webhooks or metrics?
4. Must cloud pods, load balancers, or AWS services reach remote pod IPs?
5. Can the workload use node-routable endpoints or `hostNetwork`, or does it require normal pod networking?
6. Does the remote site need ingress, egress, east-west traffic, or service exposure during WAN degradation?

## Hosts and lifecycle

1. How many sites, hosts, and vCPUs per host are planned now and at peak?
2. How many hours per month will each host remain joined to the cluster?
3. Which operating system, architecture, GPU or accelerator, container runtime, and image lifecycle are required?
4. Will hosts use a golden image, PXE, cloud-init, configuration management, or manual installation?
5. Who owns patching, Kubernetes upgrades, certificates, activation rotation, draining, and failed-node replacement?
6. How long can workloads continue during loss of WAN connectivity?

## Identity and security

1. Does the organization operate PKI suitable for IAM Roles Anywhere?
2. If not, can it securely issue, store, rotate, and revoke SSM hybrid activations?
3. Where will Terraform state, rendered user data, NodeConfig, certificates, and activation material live?
4. Which team approves IAM roles, `HYBRID_LINUX` access entries, security groups, firewalls, and network routes?
5. Are SSH access or public IPs truly required, or can managed access and private nodes satisfy operations?

## Economics

1. What are the node count, reported vCPUs per node, and joined hours per month?
2. Does the account or AWS Organization already consume Hybrid Nodes vCPU-hours in the same Region?
3. Does the customer have negotiated private pricing for EKS Hybrid Nodes? If unknown, who can confirm it?
4. Who owns the incremental Hybrid Nodes fee and the budget approval?
5. Include host compute, power or colocation, EC2 and EBS for cloud-hosted nodes, public IPv4, Direct Connect or VPN, Transit Gateway processing, inter-Region or internet transfer, NAT, gateway compute, observability, and operational support.
6. Would a separate EKS cluster, EKS Anywhere, EKS on Outposts, or conventional EC2 nodes cost less for the requirement?

## IaC and delivery

1. What owns the existing cluster: Terraform, CDK, CloudFormation, eksctl, another system, or manual operations?
2. Which repository, state, stack, pipeline, and team may update it?
3. Can the current version express `RemoteNetworkConfig` and `HYBRID_LINUX` access entries natively?
4. Should cluster changes, site connectivity, and host lifecycle use separate modules or stacks?
5. What plan, change-set, policy, security, and rollback checks must pass before deployment?
