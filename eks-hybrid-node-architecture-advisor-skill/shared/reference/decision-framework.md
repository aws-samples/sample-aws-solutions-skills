# Architecture decision framework

## 1. Workload and locality decision

Classify the workload before choosing connectivity or cluster scope.

| Workload | Placement rule | Required evidence |
|---|---|---|
| Distributed training | Run one job entirely in the cluster Region or entirely at one remote site | Dataset and checkpoint location, framework, NCCL/EFA/RDMA needs, measured bandwidth and latency |
| Independent training jobs | Different jobs may use different localities; each job remains local | Scheduler isolation, data staging, checkpoint ownership, transfer estimate |
| Inference | Run complete, independently callable replicas in each selected locality | Full model fit, state and cache behavior, model synchronization, streaming, latency, failover |
| Other workload | Derive placement from communication, state, data gravity, and failure behavior | Explicit traffic and dependency map |

A distributed job MUST NOT place workers, coordinators, parameter servers, model partitions, or collective communication across the WAN. Inference MAY run locally and remotely, but tensor parallelism, pipeline parallelism, shared model state, and one request MUST NOT span localities. Split requests among complete replicas instead.

Enforce placement with locality labels and required node affinity. Use separate Deployments, Jobs, and Services when local and remote behavior differs. Do not rely on preferred affinity or one selector that can silently mix localities.

## 2. Support classification

| Host location | Classification | Required response |
|---|---|---|
| Customer-managed on-premises or edge physical/virtual machines | Documented Hybrid Nodes use | Continue through all gates |
| EC2 in another Region or Local Zone | Cloud-hosted; works, not covered by AWS | Primary pattern for this skill. Disclose the support posture once, then design it to production standards: same gates, same rigor |
| AWS Outposts | Do not assume Hybrid Nodes | Compare EKS on Outposts and documented options |
| Another cloud | Cloud-hosted; works, not covered by AWS Support | Same mechanics as cross-Region EC2. Disclose the support posture and account for cross-cloud egress cost and latency |
| Disconnected or intermittently connected site | Poor fit | Evaluate EKS Anywhere or a separate local cluster |

## 3. Cluster scope

Prefer an existing cluster only when shared lifecycle, policy, and workload placement outweigh WAN and blast-radius coupling. Recommend a separate cluster when sites need independent upgrades, tolerate long disconnections, have conflicting compliance boundaries, or cannot meet routing and latency requirements.

For multiple clusters or sites, assign unique pod CIDRs, identify which cluster owns each node, and avoid one-off connection meshes. Separate cluster configuration ownership from reusable site and node modules.

## 4. Connectivity decision

Use this order:

1. Reuse an existing Transit Gateway when it already provides governed routing to the cluster VPC and remote site or VPC. Verify attachments, route propagation, segmentation, return routes, failure domains, and data-processing charges.
2. For a VPC-to-VPC cloud-hosted design without a suitable Transit Gateway, use VPC peering as the simplest fast path. Peering is non-transitive, becomes difficult at scale, and cannot connect an on-premises network.
3. For a real on-premises or edge site, use Direct Connect or Site-to-Site VPN through the existing Transit Gateway when appropriate, or through the approved Virtual Private Gateway design. Do not present VPC peering as an on-premises option.
4. If no connection exists, compare Direct Connect and VPN by bandwidth, latency, resilience, lead time, ownership, and cost. Do not add a Transit Gateway unless scale or governance justifies it.

Choose the pod-routing method separately from the underlay:

| Need | Option | Challenge |
|---|---|---|
| Dynamic remote pod routing | Cilium BGP | Requires BGP-capable routers and governed advertisements |
| VPC-to-pod reachability without BGP | EKS Hybrid Nodes gateway | Requires AWS Cilium, gateway compute, routes, and HA |
| Node-routable application only | `hostNetwork` or node endpoint, by exception | Reduces isolation and does not solve general pod routing |

A valid design proves bidirectional Layer 3 reachability for every required flow among control-plane ENIs, cloud nodes and pods, Hybrid Nodes, remote pods, DNS, registries, identity endpoints, storage, and clients.

## 5. EKS API endpoint decision

AWS recommends public or private EKS API endpoint access for Hybrid Nodes, not both. For private access, prove private DNS resolution and routing through Direct Connect, VPN, Transit Gateway, or Virtual Private Gateway. For public access, prove controlled egress and endpoint CIDR restrictions. Do not infer endpoint behavior from a demonstration.

## 6. Kubernetes Service and application ingress

A Kubernetes Service is cluster-scoped, not AWS-Region-scoped. It can select cloud or Hybrid Node pods. A `ClusterIP` Service does not create a client-facing endpoint, and an EndpointSlice proves endpoint selection rather than end-to-end reachability.

Keep local and remote Services separate when their routing, health, capacity, or failover policies differ. AWS Load Balancer Controller can create ALB or NLB resources in the EKS cluster's AWS Region. IP targets can route to Hybrid Node pods only when the remote pod CIDRs are routable. This does not create ingress local to the remote site.

When remote pod CIDRs are unroutable, or the remote hosts sit in a peered VPC, prefer a gateway on cluster-Region nodes behind the load balancer over targeting remote addresses directly. See the gateway pattern in [hybrid-networking-guide.md](hybrid-networking-guide.md#application-endpoint-is-a-separate-decision).

If clients need remote-local entry, evaluate the site's existing technology and options such as Cilium, Calico, MetalLB, NGINX, or HAProxy. Treat its lifecycle, address advertisement, TLS, and availability as a separate design.

Read [hybrid-networking-guide.md](hybrid-networking-guide.md). Explain the selected API mode, DNS result, forward path, return path, route-table owners, and required ports. Then recommend the application endpoint from client location, protocol, latency, health, capacity, and failover requirements.

Global Accelerator can front supported AWS endpoints but cannot directly register an arbitrary on-premises endpoint. CloudFront origin failover covers only `GET`, `HEAD`, and `OPTIONS`, so it does not solve POST-heavy inference failover. Route 53 or an application gateway may fit. Select one when evidence supports it; otherwise compare options and state the evidence needed for a decision.

## 7. Availability and failure behavior

Define behavior for WAN loss, cluster API loss, remote-site loss, cluster-Region ingress loss, model unavailability, and version mismatch. Inference failover must select another complete compatible replica. Training must restart or resume within one locality; it must not continue as a partial cross-WAN job.

When a cluster-Region gateway fronts remote replicas, load-balancer health checks test only the gateway. During a remote outage the gateway stays in service and returns served errors. Alarm on remote backend health separately, and configure the gateway's fallback or failover to another complete replica explicitly.

## 8. Decision rule

Recommend the simplest architecture that passes all six gates. Present a scalable option when cluster or site growth warrants it. Do not let an existing demonstration, IaC tool, or optional MCP server determine the architecture.