# EKS Hybrid Nodes networking guide

Use this guide to explain and validate the network before recommending implementation. Do not reduce Hybrid Nodes networking to “the node can reach the API.” Kubernetes requires distinct forward and return flows.

## Mental model: six separate networks and endpoints

1. **EKS API endpoint:** the DNS endpoint used by kubelet, kube-proxy, operators, and clients to call the Kubernetes API on TCP 443.
2. **Control-plane ENIs:** EKS-managed private addresses in the cluster VPC. EKS uses them to initiate traffic toward Hybrid Nodes and remote pods.
3. **Remote node network:** routable IPv4 RFC 1918 or CGNAT CIDRs containing Hybrid Node addresses. Configure these as `RemoteNodeNetwork`.
4. **Remote pod network:** CIDRs allocated by Cilium or Calico to pods on Hybrid Nodes. Configure these as `RemotePodNetwork` when the control plane, cloud pods, or AWS load balancers must reach pod IPs.
5. **Kubernetes Service CIDR:** virtual ClusterIP addresses implemented by Kubernetes networking. It must not overlap VPC, remote node, or remote pod CIDRs and is not an externally routed client network.
6. **Application ingress:** ALB, NLB, remote-site load balancer, ingress controller, DNS, or global traffic manager. This is independent from the EKS API endpoint.

`RemoteNetworkConfig` tells EKS which destinations are remote and enables control-plane behavior. It does not create VPC, Transit Gateway, VPN, Direct Connect, firewall, DNS, or on-premises routes. The network teams must build those paths separately.

## The two mandatory control-plane directions

```text
Hybrid Node ── TCP 443 ──> EKS Kubernetes API endpoint
EKS control-plane ENI ── TCP 10250 ──> Hybrid Node kubelet
```

The first flow registers and maintains the node. The second powers `kubectl logs`, `exec`, `port-forward`, and other API-server-to-kubelet operations. A public EKS API endpoint changes only the first path. It never removes the private routed return path from control-plane ENIs to remote node addresses.

If webhooks or metrics endpoints run on Hybrid Nodes, add this third direction:

```text
EKS control-plane ENI ── webhook port ──> remote pod IP
```

That path requires a configured and routable remote pod CIDR.

## Choose exactly one API endpoint pattern

### Public endpoint

```text
Hybrid Node
  └─ DNS resolves EKS endpoint to public IPs
      └─ TCP 443 through site internet egress, proxy, or NAT
          └─ EKS Kubernetes API

EKS control-plane ENI
  └─ private route through TGW, VGW, DX, VPN, or VPC peering (cloud-hosted design)
      └─ TCP 10250 to Hybrid Node private IP
```

Use public endpoint access when controlled internet egress is acceptable and the source addresses can satisfy the cluster's public access CIDR restrictions. Verify the actual egress address after NAT or proxying. The remote node itself does not need a public IP.

Public endpoint mode does not make the reverse control-plane path public. `RemoteNodeNetwork` addresses must remain privately routable from the cluster VPC. Remote pod routes remain necessary for webhooks, direct cloud-to-remote pod traffic, or IP-target load balancing.

Validate:

- Remote DNS resolves the cluster endpoint to public addresses.
- The Hybrid Node can reach the endpoint on TCP 443 through its approved egress path.
- Public endpoint CIDR restrictions include the translated source address.
- Control-plane ENI subnet route tables contain a private route to every remote node CIDR.
- Remote routers and firewalls return traffic to the cluster VPC CIDRs.
- TCP 10250 succeeds from the control plane path to each Hybrid Node.

### Private endpoint

```text
Hybrid Node
  └─ DNS resolves EKS endpoint to private control-plane ENI IPs
      └─ private route through DX/VPN and TGW or VGW
          └─ TCP 443 to EKS Kubernetes API

EKS control-plane ENI
  └─ same private underlay and return routes
      └─ TCP 10250 to Hybrid Node private IP
```

Use private endpoint access when all Kubernetes API traffic must stay on private connectivity. The remote network must resolve the EKS endpoint to private control-plane addresses. For on-premises DNS, commonly configure conditional forwarding to Route 53 Resolver inbound endpoints reachable through Direct Connect or VPN.

Do not assume that an on-premises resolver can query the VPC's built-in resolver address directly. Validate the Resolver endpoint security group, TCP and UDP 53, subnet return route, forwarding domain, and high availability.

Validate:

- A DNS query from the Hybrid Node resolves the cluster endpoint to private addresses.
- Those addresses route through the approved private connection, not internet egress.
- TCP 443 succeeds from remote nodes and required remote pods.
- Control-plane ENI subnet route tables return remote node and pod CIDRs through the correct gateway.
- DNS and connectivity survive the tested connection or resolver failure scenario.

### Why public and private together is a bad Hybrid Nodes default

With public and private endpoint access enabled, Hybrid Nodes outside the VPC normally resolve the cluster endpoint to public IPs. This can defeat the intended private API path and prevent joining when public egress or endpoint CIDR rules do not permit it. AWS recommends public-only or private-only endpoint connectivity for Hybrid Nodes. Do not approve dual mode without proving the exact remote DNS response and path.

## Route and CIDR ownership

Build a route matrix before implementation:

| Source | Destination | Purpose | Required route owner |
|---|---|---|---|
| Remote node CIDR | EKS API endpoint | kubelet and kube-proxy API access | Site router plus internet egress for public mode, or private underlay for private mode |
| Control-plane ENI subnets | Remote node CIDR | logs, exec, port-forward, kubelet health | Cluster subnet route tables, gateway, remote routers |
| Remote nodes and pods | DNS resolvers | cluster and service DNS | Site DNS, Resolver endpoints, firewalls |
| Control-plane ENI subnets | Remote pod CIDR | webhooks and metrics | Cluster subnet route tables, gateway, remote pod routing |
| Cloud node or pod network | Remote pod CIDR | east-west application traffic | Cloud node route tables, gateway, remote pod routing |
| ALB or NLB subnets | Remote pod CIDR | IP-target application traffic | Load-balancer subnet route tables and remote pod routing |
| Remote pod network | VPC and service dependencies | application return traffic | CNI, remote routers, gateway, VPC routes |

At minimum, inspect the route tables attached to the subnets where EKS places control-plane ENIs. Do not update every VPC route table blindly. Add routes only to subnets participating in a proven flow.

Remote node and pod CIDRs must not overlap each other, the cluster VPC and secondary CIDRs, Kubernetes Service CIDR, connected networks, or planned expansion. Pod CIDRs must remain unique per cluster. Updating `RemoteNetworkConfig` replaces the supplied complete lists, so preserve all existing desired entries.

## Pod routing choices

### Routable remote pod CIDRs

Use routable pod CIDRs when the control plane runs webhooks or metrics on Hybrid Nodes, cloud pods call remote pods directly, or ALB/NLB IP targets point to remote pods. The VPC needs a route for the aggregate remote pod CIDR. The remote network must deliver each pod block to its current Hybrid Node, commonly through Cilium or Calico BGP, static routes, or the EKS Hybrid Nodes gateway.

### Unroutable or translated pod networks

This limits cluster functionality. Keep webhooks on cloud nodes, avoid direct cloud-to-remote pod assumptions, and prove every Service translation path. Node-routable endpoints or `hostNetwork` can support narrow use cases but reduce isolation and do not replace a general pod network.

A `hostNetwork` workload publishes the Hybrid Node IP as its Service endpoint, so cluster-Region clients reach it over the node route without BGP or the Hybrid Nodes gateway. Set `dnsPolicy: Default` on that pod so it resolves through its node's resolver instead of an unreachable cluster DNS path.

Keep CoreDNS on cluster-Region nodes while remote pod CIDRs are unroutable. A CoreDNS replica on a Hybrid Node receives an overlay address that the cluster VPC cannot route, which makes cluster DNS intermittently unreachable from the cluster Region.

### Amazon VPC CNI source NAT toward remote networks

By default the Amazon VPC CNI source-NATs pod traffic that leaves the VPC to the node's primary interface address, while policy routing binds each pod to its own interface. A pod on a secondary interface then leaves through that interface with the primary interface's address, and EC2 source/destination checking drops the packet. The CNI picks its routing mark from the destination before kube-proxy DNAT, so a `ClusterIP` Service whose endpoint is a remote node IP bypasses the exclusion logic.

The symptom is intermittent: calls from cluster-Region pods to a Service backed by a remote endpoint succeed or time out depending on which interface the calling pod landed on, while direct calls to the remote node IP keep working. Set `AWS_VPC_K8S_CNI_EXCLUDE_SNAT_CIDRS` on the VPC CNI add-on to the remote node and remote pod CIDRs. Prefer this over `AWS_VPC_K8S_CNI_EXTERNALSNAT=true`, which disables source NAT for every destination outside the VPC. Verify the setting survives add-on upgrades.

## Required ports and policy

Use the current AWS table as the source of truth. At minimum, reason about:

| Direction | Protocol/port | Purpose |
|---|---|---|
| Remote node/pod → EKS API | TCP 443 | Kubernetes API access |
| Control plane → remote node | TCP 10250 | kubelet logs, exec, port-forward, and operations |
| Control plane → remote pod | configured webhook port | admission webhooks or metrics endpoints |
| Nodes/pods ↔ DNS | TCP and UDP 53 | service and endpoint resolution |
| Node ↔ node and pod ↔ pod | workload/CNI-specific | east-west traffic and CNI health |
| Remote node → registries, S3, SSM, package endpoints | service-specific | bootstrap, credentials, images, and updates |

EKS may add inbound cluster security-group rules when remote networks are configured, but custom outbound restrictions still require explicit rules. Security-group rule limits can prevent automatic additions. Removing remote networks does not automatically remove corresponding security-group rules; include cleanup in lifecycle ownership.

## Application endpoint is a separate decision

A Kubernetes Service is cluster-scoped. `ClusterIP` does not create a remote-Region, on-premises, or internet endpoint. EndpointSlices show selected backends, not client routability.

- **Cluster-Region ingress:** AWS Load Balancer Controller can create ALB or NLB resources in the cluster Region. IP targets can reach Hybrid Node pods only when remote pod CIDRs are routable from the load-balancer subnets.
- **Cluster-Region gateway in front of remote replicas:** when remote pod CIDRs are unroutable or the remote hosts sit in a peered VPC, run a gateway (for example an OpenAI-compatible LLM proxy) on cluster-Region nodes and let the load balancer target the gateway pod IPs. A load balancer cannot register instances by ID from a peered VPC. IP targets in the remote VPC make every health check depend on the interconnect, and public subnets often lack the route to the remote network. The gateway makes the remote hop through a `ClusterIP` Service and holds keepalive connections upstream.
- **Remote-local ingress:** use a site-operated option such as Cilium, Calico, MetalLB, NGINX, or HAProxy when clients must enter and remain local to the remote site.
- **Global ingress:** choose among Route 53, Global Accelerator, CloudFront, or an application-aware gateway based on protocol, client location, health, capacity, streaming, and failover. Global Accelerator accepts AWS ALB, NLB, EC2, or Elastic IP endpoints, not arbitrary on-premises endpoints. CloudFront origin failover does not fail over POST requests.

The skill should recommend one option when evidence is sufficient. Otherwise, it should compare viable options and request the specific measurements needed to decide.

## First-time troubleshooting order

1. Resolve the EKS endpoint from the Hybrid Node and record public or private IPs.
2. Trace the node-to-API TCP 443 path.
3. Identify control-plane ENI subnets and inspect their routes to remote node CIDRs.
4. Test control-plane-to-kubelet TCP 10250 through `logs`, `exec`, or `port-forward`.
5. Resolve and test DNS in both directions.
6. If remote pod reachability is required, inspect VPC routes and per-node pod route advertisement.
7. Inspect security groups, firewalls, network ACLs, NAT or proxy translation, MTU, and asymmetric routes.
8. Only then debug CNI, Services, ingress, or workload policy.