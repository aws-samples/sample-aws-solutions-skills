# Implementation and validation checklists

Do not execute this checklist until the architecture gates pass and the user approves implementation. Complete the current-state delta in [requirements-and-compatibility.md](requirements-and-compatibility.md), use [remote-node-setup.md](remote-node-setup.md) as the authoritative host ordering, and author operational procedures from [sre-operations-and-faq.md](sre-operations-and-faq.md).

## Existing cluster and API endpoint

- Confirm the cluster uses IPv4.
- Confirm authentication mode is `API` or `API_AND_CONFIG_MAP`.
- Select and document public-only or private-only EKS API endpoint access.
- From the remote node network, resolve the EKS endpoint and record whether it returns expected public or private addresses.
- For public mode, verify approved egress, translated source address, TCP 443, and endpoint CIDR restrictions.
- For private mode, verify conditional forwarding, Route 53 Resolver inbound endpoints, TCP and UDP 53, private TCP 443, and DNS/path redundancy.
- Separately verify private control-plane ENI routes to remote node CIDRs for TCP 10250.
- Inventory all VPC and service CIDRs before selecting remote CIDRs.
- Add the final complete `RemoteNodeNetworks` list and optional `RemotePodNetworks` list.
- Remember that updating either list replaces that complete list; preserve existing entries.
- Wait for cluster status `ACTIVE` before continuing.
- Add a `HYBRID_LINUX` access entry for the Hybrid Nodes IAM role.
- Prevent incompatible add-ons from scheduling on Hybrid Nodes.

## Connectivity

- Build a directional flow matrix before adding routes or firewall rules.
- Add routes for remote node CIDRs to the route tables attached to control-plane ENI subnets.
- Add routes to cloud-node or load-balancer subnet route tables only when their workloads must reach remote nodes or pods.
- Add remote pod routes when webhooks, direct pod traffic, or load-balancer IP targets require them.
- Configure matching return routes on the remote network.
- Allow TCP 443 from required remote node and pod CIDRs to the EKS API path.
- Allow TCP 10250 from the control-plane path to Hybrid Nodes.
- Allow TCP and UDP 53 along every required DNS path and configured webhook ports to remote pods.
- Configure security groups, firewalls, network ACLs, BGP, and DNS.
- Test MTU, asymmetric routing, bandwidth, latency, and redundant path failover.
- Do not use `0.0.0.0/0` inbound rules as a substitute for a flow matrix.

## Identity

- Create a least-privilege Hybrid Nodes IAM role.
- Use SSM hybrid activations when no managed PKI exists.
- Use IAM Roles Anywhere when the organization operates a suitable CA and certificate lifecycle.
- Treat activation IDs, activation codes, certificates, and rendered node configuration as secrets.
- Use `nodeadm` 1.0.19 or later with the SSM credential provider.
- Set activation expiration and registration limits deliberately.
- Define certificate or activation rotation, `nodeadm uninstall`, and SSM deregistration.

## Host lifecycle

Follow [remote-node-setup.md](remote-node-setup.md); this checklist does not replace its ordered state machine.

- Select a validated operating system and architecture.
- For production, bake and pin `nodeadm` plus checksums in a golden image and run first-boot join logic through an owned service.
- Encrypt storage, require IMDSv2 where applicable, and avoid SSH keys when managed access exists.
- Configure proxy, CA trust, DNS, time, and restricted egress before `nodeadm init`.
- Render NodeConfig with restrictive permissions and run `nodeadm config check` before initialization.
- Apply containerd runtime customization after `nodeadm init`; initialization can overwrite earlier changes.
- Remove short-lived NodeConfig secrets only after successful join, persisted credentials, and reboot validation.
- Apply workload labels and taints deliberately; taints do not advertise hardware resources.
- For GPU nodes, validate host driver and runtime, then install NFD before a standalone NVIDIA device plug-in.
- Rehearse cordon, drain, upgrade or replacement, Node deletion, `nodeadm uninstall`, identity revocation, and billing-stop verification.

## CNI

- Install Cilium configured only for `eks.amazonaws.com/compute-type=hybrid`.
- Do not schedule Amazon VPC CNI on Hybrid Nodes.
- Make the CNI pod pool match the configured remote pod network.
- Keep the node in `NotReady` until CNI installation succeeds; this is expected.
- Validate CoreDNS placement and mixed-mode cluster behavior; keep CoreDNS on cloud nodes while remote pod CIDRs are unroutable.
- Size the remote pod CIDRs and the Cilium per-node mask for the eventual pods per node; they cannot change after Cilium is installed without rebuilding.
- Set `AWS_VPC_K8S_CNI_EXCLUDE_SNAT_CIDRS` to the remote node and pod CIDRs when cluster-Region pods call remote endpoints, and verify it after VPC CNI add-on upgrades.

## Workload placement

- Label each locality and enforce it with required node affinity or selectors.
- Verify every distributed training job runs entirely in one locality.
- Verify each inference locality has at least one complete Ready replica.
- Use separate local and remote Services when traffic policy, health, or failover differs.
- Inspect EndpointSlices and prove each Service contains only its intended locality.
- Confirm loss of one locality cannot create a partial-model or cross-WAN execution path.
- Measure dataset, checkpoint, model, request, response, cache, and telemetry transfer.

## Application exposure

- Distinguish ClusterIP and EndpointSlice reachability from customer-facing ingress.
- For AWS ALB or NLB IP targets, prove remote pod CIDRs are routable from the load balancer.
- Treat an AWS load balancer created by the cluster's controller as a cluster-Region endpoint, even when it targets remote pods.
- If clients must enter at the remote site, validate a separate local ingress or load-balancing design.
- Test health, TLS, authentication, streaming, rate limits, model-version consistency, capacity-aware routing, and failover.
- Do not treat `kubectl port-forward` as production ingress.
- Raise the load-balancer idle timeout above the longest expected generation, and keep the gateway's request timeout below it; the ALB default of 60 seconds truncates long responses mid-stream.
- Verify streaming responses arrive incrementally through every hop rather than buffered.
- Default the load-balancer scheme to internal. Internet-facing access requires named source CIDRs, TLS, and per-caller credentials.
- Recommend the global or distant endpoint when the evidence supports one option.
- When evidence is incomplete, compare viable options and name the owner and measurements needed to decide.

## Workload and traffic validation

- Validate webhooks, metrics, logs, exec, port-forward, east-west traffic, and every required service flow.
- Test local and remote endpoints independently before testing failover.
- Record measured latency, bandwidth, error rate, recovery time, and transfer volume.

## Storage and accelerator readiness

- Classify every StorageClass and CSI driver by locality; do not schedule Amazon EBS-backed claims to on-premises hosts.
- Test provision, mount or attach, read/write, reschedule, detach, snapshot, restore, and cleanup as applicable.
- Define dataset, checkpoint, model, cache, and local ephemeral-data ownership during node and WAN failure.
- For accelerators, validate firmware, host driver, management library, runtime toolkit, discovery, device plug-in, and health telemetry as one compatibility chain.
- Verify labels and taints before scheduling, then verify a non-zero allocatable resource and a bounded device smoke workload.

## Reboot, upgrade, and removal

- Reboot one non-critical node and verify credentials, runtime customization, CNI, DNS, hardware resources, and workload gates recover.
- Build a version matrix for EKS, kubelet, `nodeadm`, OS, kernel, containerd, Cilium, add-ons, storage, drivers, runtime, and device plug-ins.
- Prefer validated image replacement; treat `nodeadm upgrade` as disruptive and operate one cordoned, drained node at a time.
- Remove traffic, cordon, drain, delete the Kubernetes Node, run `nodeadm uninstall`, clean CNI artifacts, revoke identity, and remove infrastructure in that order.
- Delete load balancers created by the cluster's controller before destroying the cluster; IaC does not own them and they outlive the controller.
- Verify the node no longer appears attached and Hybrid Nodes billing stops.