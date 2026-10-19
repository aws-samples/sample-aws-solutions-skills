# Requirements and compatibility guide

Use this guide after workload classification and before architecture selection. Inventory the existing environment first. Do not install an add-on merely because it appears in this guide.

## Required output: current-state delta

Produce this table for every component:

| Layer | Component | Classification | Current version/state | Required version/config | Placement | Action | Owner | Validation |
|---|---|---|---|---|---|---|---|---|

Use one classification:

- **Required:** the Hybrid Nodes design cannot function without it.
- **Conditional:** required only by a selected capability or workload.
- **Workload-specific:** required by hardware, storage, security, or application behavior.
- **Cloud-only:** may exist in the cluster but MUST NOT schedule on Hybrid Nodes.
- **Unsupported or unvalidated:** no current AWS Hybrid Nodes validation; obtain support ownership before use.
- **Already satisfied:** the existing version and placement meet the requirement.

Refresh supported Kubernetes, operating-system, Cilium, and add-on versions from official AWS documentation. Never infer compatibility from a standard EKS installation.

## Layer 1: existing EKS cluster

| Requirement | Classification | Decision or validation |
|---|---|---|
| Supported EKS Kubernetes version | Required | Match node kubelet minor version to the cluster upgrade policy |
| IPv4 cluster | Required | Hybrid Nodes do not use an IPv6 cluster configuration |
| `API` or `API_AND_CONFIG_MAP` authentication | Required | Required for EKS access entries |
| Public-only or private-only EKS API access | Required | Validate remote DNS, TCP 443, and the separate private return path |
| Complete `RemoteNodeNetwork` CIDR list | Required | Non-overlapping RFC 1918 or CGNAT ranges containing all Hybrid Node IPs |
| `RemotePodNetwork` CIDRs | Conditional | Required when the control plane, cloud workloads, or IP-target load balancers reach remote pod IPs |
| `HYBRID_LINUX` access entry | Required | Principal is the Hybrid Nodes IAM role |
| Control-plane ENI routes and security rules | Required | Prove TCP 10250 to nodes and configured webhook ports to pods |
| EKS cluster insights review | Required | Resolve Hybrid Nodes misconfiguration and upgrade findings before rollout |

Updating remote network configuration replaces the supplied complete lists. Preserve every desired existing CIDR.

## Layer 2: components installed in the cluster

### Baseline components

| Component | Classification | Hybrid Nodes requirement |
|---|---|---|
| Cilium | Required | Use a current AWS-maintained supported build. Pin it to `eks.amazonaws.com/compute-type=hybrid`. Match Cluster Scope IPAM to `RemotePodNetwork` and validate host kernel compatibility. |
| CoreDNS | Required | Use a compatible version. Place a replica on Hybrid Nodes only when remote pod CIDRs are routable from the cluster VPC; otherwise keep every replica on cloud nodes. With routable pod CIDRs, place at least one replica on each side when each must retain local DNS during partial connectivity loss. Validate cross-locality DNS routes. |
| kube-proxy | Required unless deliberately replaced | Use a compatible version and run it on Hybrid Nodes, or enable and validate Cilium kube-proxy replacement. Do not run both on the same node. |
| Amazon VPC CNI | Cloud-only | It is not compatible with Hybrid Nodes. Use a version and scheduling rules that exclude `compute-type=hybrid`. |

A node may register as `NotReady` before Cilium starts. It becomes workload-ready only after the CNI, DNS, and selected service path pass validation.

### Capability-driven components

| Capability | Candidate component | Classification | Key requirement |
|---|---|---|---|
| AWS IAM credentials for pods | EKS Pod Identity Agent or IRSA | Conditional | For Pod Identity on Ubuntu, RHEL, or AL2023, enable the node credentials file and deploy the hybrid DaemonSet. Use a compatible agent version. Bottlerocket has separate requirements. |
| AWS Region ingress | AWS Load Balancer Controller | Conditional | Use IP targets for Hybrid Node pods. Remote pod CIDRs must be routable from load-balancer subnets. The controller uses webhooks and calls AWS APIs, so schedule it on cloud nodes. Delete its load balancers while it still runs, before destroying the cluster. |
| Remote-local ingress | Cilium Ingress/Gateway, MetalLB, NGINX, HAProxy, or existing local load balancer | Conditional | Select one based on local routing, BGP/L2, TLS, HA, and operations. Do not assume the AWS controller creates local ingress. |
| Pod CIDR advertisement | Cilium BGP or approved routing system | Conditional | Required when pod IPs must be directly routable. Validate router support, advertisements, aggregation, withdrawal, and failure behavior. |
| VPC-to-pod routing without site BGP | EKS Hybrid Nodes gateway | Conditional | Requires supported Cilium/VTEP versions, gateway compute, route ownership, HA, and cost. |
| Metrics API | Kubernetes Metrics Server | Conditional | The control plane must reach its pod IP, or use a validated host-network design. |
| Telemetry | ADOT, CloudWatch Observability, AMP collector | Workload-specific | Check minimum compatible versions, webhooks, pod routability, private endpoint constraints, and unsupported node metrics that depend on EC2 IMDS. |
| Certificates | cert-manager or AWS Private CA Connector | Workload-specific | Webhooks on Hybrid Nodes require routable pod CIDRs. |
| Secrets | AWS Secrets Store CSI provider or another approved system | Workload-specific | Validate supported version, pod identity, endpoint reachability, rotation, and offline behavior. |
| Snapshots | CSI snapshot controller | Workload-specific | Prefer controller placement near the control plane and validate the storage driver separately. |

Only add-ons listed in the current AWS Hybrid Nodes compatibility documentation are validated by AWS. For every other DaemonSet, controller, webhook, security agent, or service mesh, classify it as cloud-only or unsupported until its owner proves compatibility.

## Layer 3: remote host, operating system, and runtime

Build one requirement row for each node class. Read [remote-node-setup.md](remote-node-setup.md) for the ordered implementation gates.

| Component | Classification | Required decision or validation |
|---|---|---|
| Supported OS and architecture | Required | Select a current EKS Hybrid Nodes-supported combination. Record image owner, patch source, kernel, cgroups, and replacement policy. |
| Hybrid Nodes `nodeadm` | Required | Use the Hybrid Nodes binary, not the EC2 AL2023 bootstrap version. Pin its version and checksum. SSM requires `1.0.19` or later. |
| kubelet and containerd | Required | Install through `nodeadm` or a validated image. Match Kubernetes skew policy and revalidate runtime customization after init and upgrade. |
| Host DNS, time, and CA trust | Required | Prove endpoint resolution, clock synchronization, certificate trust, and renewal behavior. |
| Proxy configuration | Conditional | Configure OS, kubelet, containerd, and credential tooling before `nodeadm init`; validate `NO_PROXY` for direct cluster and internal routes. |
| Host firewall and kernel modules | Required | Permit selected CNI, kubelet, DNS, storage, workload, and observability flows. Validate forwarding, MTU, and required modules. |
| SSM or IAM Roles Anywhere tooling | Required | Select one provider. Define secure issuance, renewal, revocation, stale-registration cleanup, and reboot persistence. |
| Configuration management | Required | Own image creation, first boot, retries, terminal failure, logging, patching, drift, and replacement. Manual setup is not a production lifecycle. |
| Local filesystem capacity | Required | Size image, ephemeral container, log, model, checkpoint, and upgrade working space. Define pressure and cleanup thresholds. |

### Accelerator requirements

| Component | Classification | Required decision or validation |
|---|---|---|
| Host firmware and driver | Workload-specific | Pin a compatible vendor driver and firmware. Verify the device before Kubernetes registration. |
| Vendor management library | Workload-specific | Validate NVML or the selected accelerator equivalent from the host and container runtime. |
| Container runtime toolkit | Workload-specific | Install before join, but apply containerd customization after `nodeadm init` because initialization can overwrite runtime configuration. |
| Hardware discovery | Workload-specific | Select NFD, vendor operator discovery, or another single owner. Validate the expected hardware label before deploying the device plug-in. |
| Device plug-in or operator | Workload-specific | Pin and validate it against OS, kernel, driver, runtime, Kubernetes, and hardware. Verify a non-zero allocatable resource. |
| Health and telemetry | Workload-specific | Select DCGM or the appropriate vendor health path. Define alerting, quarantine, drain, and repair behavior. |
| Labels and taints | Workload-specific | Register topology and accelerator taints before workloads can schedule. Taints do not advertise devices. |

Do not combine standalone NFD and device plug-ins with an operator that already owns the same components unless the design assigns non-overlapping responsibilities.

## Layer 4: external services and site dependencies

| Dependency | Classification | Required decision or validation |
|---|---|---|
| Private underlay and return routes | Required | Prove node-to-API and control-plane-to-node paths separately. Add pod routes only for selected flows. |
| DNS resolver path | Required | Validate public or private endpoint responses, conditional forwarding, redundancy, TCP/UDP 53, and failure behavior. |
| Artifact and package sources | Required | Provide pinned `nodeadm`, OS packages, Kubernetes artifacts, CNI, and update sources through approved egress, mirrors, or private repositories. |
| Container and model registry | Workload-specific | Validate authentication, bandwidth, cache, image and model pinning, outage behavior, and pull-time SLO. |
| Credential endpoints | Required | Ensure SSM or IAM Roles Anywhere endpoints, PKI, CRL, and time sources remain reachable through the selected proxy or private path. |
| Observability destinations | Workload-specific | Validate buffering, backpressure, sensitive-data handling, bandwidth, and behavior during WAN loss. |
| Secrets and key management | Workload-specific | Define boot and runtime secret delivery, rotation, revocation, offline behavior, audit, and redaction. |
| Site ingress and load balancing | Conditional | Assign address advertisement, TLS, health, capacity, failover, and lifecycle ownership separately from Kubernetes Services. |
| BGP or Hybrid Nodes gateway | Conditional | Select one governed pod-routing owner. Validate HA, convergence, route limits, security, and cleanup. |

### Restricted egress

Create a current domain-and-port inventory from AWS documentation and every selected component. Include package sources, EKS artifacts, API endpoints, credential services, registries, object or model storage, DNS, time, telemetry, and certificate services. A generic internet path or unrestricted proxy is not an acceptable production requirement.

## Storage locality and CSI compatibility

Storage support follows the node's physical locality. A cluster-wide StorageClass does not make an AWS Region block volume attachable to an on-premises host.

For every storage class and CSI driver, record:

- Controller and node plug-in placement.
- Supported Hybrid Node OS, kernel, mount utilities, and architecture.
- Backend network path, identity, encryption, topology, and capacity.
- Volume binding, attach or mount, rescheduling, expansion, snapshot, restore, and cleanup behavior.
- WAN-loss behavior and whether state can fail over without violating consistency or data-residency requirements.
- Dataset, checkpoint, model, cache, and local ephemeral-data ownership.

Classify Amazon EBS node attachment as cloud-only. Treat Amazon EFS, FSx, S3-mounted filesystems, third-party CSI drivers, and local storage as workload-specific until their exact network path and Hybrid Node compatibility pass tests. Keep state and compute in one locality unless a measured and owned replication design proves otherwise.

## Upgrade and version compatibility

Build a compatibility matrix before initial rollout and every upgrade:

| Component | Current | Target | Supported combination source | Upgrade order | Rollback or replacement | Owner |
|---|---|---|---|---|---|---|

Include EKS Kubernetes, kubelet, `nodeadm`, OS, kernel, containerd, Cilium, kube-proxy, CoreDNS, CSI drivers, webhooks, security agents, observability, GPU drivers, runtime toolkits, discovery, device plug-ins, and workload frameworks.

Prefer node replacement through a new validated image. Treat `nodeadm upgrade` as disruptive: cordon, drain, upgrade one node, reapply post-init runtime customization, validate, then uncordon. Do not change credential-provider type during an in-place upgrade.