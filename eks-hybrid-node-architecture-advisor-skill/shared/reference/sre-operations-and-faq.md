# SRE operations guide and FAQ

Use this guide to answer operational questions and author an environment-specific SRE runbook. Read [remote-node-setup.md](remote-node-setup.md) for lifecycle ordering and [hybrid-networking-guide.md](hybrid-networking-guide.md) for traffic-path diagnosis.

The skill remains advisory. It MUST distinguish observation, workload disruption, host mutation, and destructive cleanup. It MUST NOT execute a disruptive or destructive action without the user naming the target and approving the impact.

## Operations response contract

For a direct SRE question, answer in this order:

1. **Short answer** — state the conclusion and the exact lifecycle boundary.
2. **Impact** — identify workload disruption, data risk, billing effect, and reversibility.
3. **Preconditions** — name the cluster, node, node owner, credential provider, storage, workloads, and required access.
4. **Procedure** — use ordered commands with placeholders; do not guess identifiers.
5. **Success evidence** — list objective checks at Kubernetes, host, identity, network, and billing layers.
6. **Failure and rollback** — state where to stop, what evidence to collect, and how to restore service.
7. **Escalation** — identify the owning team and evidence bundle when the procedure cannot finish safely.

Never answer “clear the node” without resolving the intended outcome:

| Requested outcome | Meaning | Last safe action |
|---|---|---|
| Stop new scheduling | Keep workloads and node registration | Cordon |
| Evacuate workloads | Keep the node registered for repair | Cordon and drain |
| Stop Hybrid Nodes billing | Remove the Kubernetes Node object | Delete the Node after drain |
| Remove EKS host software | Stop and remove `nodeadm` artifacts | Run `nodeadm uninstall` after drain and Node deletion |
| Reuse the host in the same cluster | Remove stale identity and CNI state, then rejoin cleanly | Follow reset-for-reuse runbook |
| Repurpose or dispose of the host | Remove cluster, identity, CNI, workload, secrets, and local data | Follow decommission and sanitization policy |

## Runbook authoring template

Create one runbook per operation. Do not combine routine repair, emergency isolation, and decommissioning into one ambiguous procedure.

```markdown
# <Operation> — <environment and node class>

## Purpose and trigger
## Scope and exclusions
## Customer and workload impact
## Prerequisites and approvals
## Target identification
## Pre-change evidence
## Procedure
## Validation
## Rollback or recovery
## Escalation and evidence bundle
## Audit record and follow-up
```

Every runbook MUST contain placeholders for cluster, Region, kubeconfig context, node name, site, workload owner, maintenance window, identity provider, managed-instance or certificate identity, and ticket or change record. It MUST list which commands are read-only, disruptive, host-mutating, or destructive.
## Standard evidence bundle

Collect bounded, redacted evidence before mutation whenever the host remains reachable:

### Kubernetes

```text
kubectl get node <node> -o wide
kubectl describe node <node>
kubectl get pods -A --field-selector spec.nodeName=<node> -o wide
kubectl get events -A --field-selector involvedObject.kind=Node,involvedObject.name=<node> --sort-by=.lastTimestamp
kubectl get endpointslice -A -o wide
```

Also inspect Cilium, CoreDNS, kube-proxy, storage, security, observability, NFD, and device plug-in pods that target the node. Record conditions, taints, labels, allocatable resources, leases, pressure, and recent scheduling events.

### Host

```text
nodeadm debug --config-source file:///path/to/nodeConfig.yaml --no-color
systemctl status kubelet containerd --no-pager
journalctl -u kubelet -u containerd --since '<bounded time>' --no-pager
```

Collect credential-provider, cloud-init, proxy, CNI, kernel, disk, time-sync, and hardware logs only as required. Redact activation codes, private keys, certificates where policy requires it, bearer tokens, kubeconfigs, NodeConfig secrets, and rendered user data.

### AWS and network

Use read-only inspection to record cluster endpoint mode, `RemoteNetworkConfig`, access entry, cluster insights, control-plane ENI routes, security groups, DNS answers, SSM managed-instance state or IAM Roles Anywhere certificate state, and Hybrid Nodes usage data. Prove TCP 443 and TCP 10250 separately.

## Runbook: safely remove one Hybrid Node

Use this runbook for planned replacement or decommissioning. Removal disrupts workloads and deletion is not reversible as the same Kubernetes Node object.

### Preconditions

- Confirm the exact kubeconfig context, cluster, and node.
- Confirm the node is a Hybrid Node and identify its site and host.
- Identify workloads, PodDisruptionBudgets, local or attached storage, accelerator jobs, endpoint membership, and replacement capacity.
- Obtain workload-owner and change approval.
- Keep host access and identity records until validation finishes.

### Procedure

1. Remove or reduce application traffic to the node's endpoints. Confirm requests drain.
2. Cordon the node.
3. Drain it with flags selected from workload and storage evidence. Do not copy a forceful generic drain command.
4. Confirm that only expected DaemonSet or static pods remain.
5. Delete the Kubernetes Node object. This is the action that stops Hybrid Nodes charges for that node.
6. On the host, run `nodeadm uninstall` with root or sudo.
7. Remove host-local CNI and add-on artifacts through their documented owner procedures.
8. Verify SSM managed-instance deregistration, or revoke and inventory IAM Roles Anywhere credentials.
9. Remove unused activation material, certificates, NodeConfig secrets, monitoring, DNS, BGP, and inventory entries.
10. Remove host infrastructure or return it to the approved reuse process.
11. Change `RemoteNetworkConfig`, shared routes, or cluster-wide add-ons only when no remaining node or flow depends on them.

### Success evidence

- Kubernetes no longer returns the Node object.
- No EndpointSlice or workload references the removed endpoint.
- The host no longer runs kubelet or containerd artifacts owned by `nodeadm`.
- The SSM registration is absent, or IAM Roles Anywhere credentials are revoked as intended.
- CNI routes, interfaces, BGP advertisements, and local files meet the selected cleanup level.
- Monitoring and inventory show the host as removed.
- Hybrid Nodes metering no longer includes the node after the expected reporting delay.

### Failure handling

If drain fails, stop before deleting the Node. Resolve disruption budgets, finalizers, local data, unreachable pods, or replacement capacity with the workload owner. Do not use forced eviction until the owner accepts data and availability impact.

If `nodeadm uninstall` fails after Node deletion, billing has stopped, but host cleanup remains incomplete. Preserve logs, inspect mounts and running processes, and fix uninstall safely. Do not use `--force` until you verify that Kubernetes or CNI paths do not contain mounted host data.

## Runbook: reset a host for controlled reuse

A reset is more than `kubectl delete node`. Use it only when the same physical or virtual host will join again and the image owner does not prefer reprovisioning.

1. Complete the safe-removal runbook through Node deletion, uninstall, and identity cleanup.
2. Inventory mounted filesystems and workload data before removing files.
3. Follow the selected CNI's host cleanup procedure. Cilium interfaces and routes can remain after Helm removal or node uninstall.
4. Remove stale node-specific credential and Kubernetes configuration only through documented `nodeadm`, SSM, IAM Roles Anywhere, and CNI procedures.
5. Revoke the old activation or certificate identity. Create a fresh identity for the new registration.
6. Revalidate OS, kernel, packages, proxy, CA trust, DNS, time, driver, runtime, disk, and network state.
7. Prefer rebuilding from the approved golden image when cleanup cannot prove a known state.
8. Follow the complete bootstrap state machine in [remote-node-setup.md](remote-node-setup.md).

Do not provide a blind recursive deletion list. `nodeadm uninstall --force` and manual deletion under `/var/lib/kubelet`, `/var/lib/cni`, or `/etc/cni/net.d` can affect mounted data or components another owner manages.

## Runbook: isolate a failing node without removing it

Use this when diagnosis or repair may return the node to service.

1. Cordon the node to stop new scheduling.
2. Decide whether current workloads can remain during evidence collection. Drain when continued execution risks requests, data, or other nodes.
3. Remove the node from application traffic when health is uncertain.
4. Collect the standard evidence bundle.
5. Diagnose by layer: host pressure and clock; credentials; endpoint DNS and TCP 443; control-plane return TCP 10250; kubelet and containerd; Cilium; DNS and Services; storage; accelerator; workload.
6. Repair one layer and validate it before changing another.
7. Uncordon only after the node is Ready, required resources are allocatable, smoke tests pass, and endpoint health is stable.

Cordon does not stop Hybrid Nodes billing. Drain does not stop billing. Deleting the Kubernetes Node object stops billing.
## Symptom-driven troubleshooting map

| Symptom | First distinction | Evidence and next layer |
|---|---|---|
| Node never appears | Install versus initialization versus identity | `nodeadm config check`, `nodeadm debug`, init output, SSM or IAM Roles Anywhere identity, access entry, node CIDR |
| Node appears `NotReady` immediately after join | Expected pre-CNI state versus CNI failure | Node conditions, Cilium scheduling and logs, IPAM pool, kernel, CNI files |
| Node changed from Ready to `NotReady` | Host pressure, kubelet/runtime, WAN, credentials, or CNI | Conditions/events, bounded journals, disk/memory/PID, clock, TCP 443, Cilium |
| `kubectl logs`, exec, or port-forward fails | Node-to-API works but reverse path may not | Control-plane ENI subnet routes, security rules, TCP 10250, kubelet certificate |
| Pods cannot resolve DNS | CoreDNS endpoint, service path, or upstream DNS | Pod resolv.conf, CoreDNS placement/logs, Service/EndpointSlice, TCP/UDP 53 |
| Calls from cloud pods to a Service with a remote endpoint succeed or time out intermittently | VPC CNI source NAT versus routing | `AWS_VPC_K8S_CNI_EXCLUDE_SNAT_CIDRS` on the `aws-node` DaemonSet, calling pod's interface, direct node-IP test |
| Long inference responses are cut off mid-stream | Load-balancer idle timeout versus backend failure | Load-balancer idle timeout, gateway request timeout, backend logs for completed requests |
| Gateway healthy but every request fails | Gateway health versus remote backend health | Gateway logs, upstream Service EndpointSlice, remote node and pod state |
| Cloud pods cannot reach remote pods | Service translation versus direct pod routing | Remote pod CIDR config, VPC routes, BGP/gateway, return route, policy, MTU |
| GPU label exists but `nvidia.com/gpu` is absent | Discovery works but runtime/device plug-in does not | Driver, NVML, containerd runtime, device plug-in pod, allocatable resources |
| GPU disappears after reboot or upgrade | Post-init runtime customization did not persist | containerd config, `nvidia-ctk`, service ordering, driver state, plug-in logs |
| Drain is blocked | Disruption budget, finalizer, unmanaged pod, local data, or dead host | Pods on node, owners, PDBs, volumes, termination events |
| Node was deleted but host still runs Kubernetes | Cluster removal and host uninstall are separate | kubelet/containerd status, `nodeadm uninstall`, CNI state, identity registration |
| Host was uninstalled but Node remains | `nodeadm uninstall` does not delete the Node | Drain and delete Node; verify workload and billing impact |
| Billing appears after host shutdown | Shutdown does not remove cluster membership | Check whether the Kubernetes Node object still exists; delete it safely |
| Same host cannot rejoin | Stale Node, managed instance, certificate, or host files | Compare Node name and identity; complete reset-for-reuse rather than retrying blindly |

## FAQ

### How do I stop workloads from scheduling on a node?

Cordon it. Existing pods remain. This action does not stop Hybrid Nodes billing. Validate that the node shows `SchedulingDisabled`, then decide whether to drain it.

### How do I empty a node but keep it for repair?

Cordon and drain it. Select drain behavior from PodDisruptionBudgets, local data, accelerator jobs, static pods, and replacement capacity. Keep the Node object while diagnosing. Billing continues.

### How do I remove a node and stop Hybrid Nodes charges?

Drain it, then delete the Kubernetes Node object. AWS states that Hybrid Nodes charges stop when the Node object leaves the EKS cluster. Host shutdown, cordon, drain, and `nodeadm uninstall` alone do not replace this deletion step.

### Should I run `nodeadm uninstall` before deleting the Node?

Use the planned order: remove traffic, cordon, drain, delete the Node, then run `nodeadm uninstall`. `nodeadm uninstall` removes host artifacts but does not drain or delete the Kubernetes Node.

### Does `nodeadm uninstall` remove everything?

No. It removes artifacts installed by `nodeadm`, including kubelet and containerd, and it deregisters an SSM-managed host. It does not remove the Kubernetes Node or necessarily remove CNI and other add-on files. Validate each cleanup layer separately.

### Can I use `nodeadm uninstall --force` to clear the host?

Not by default. Force cleanup can delete contents under standard Kubernetes and CNI directories. Mounted paths may expose host or workload data. Inspect mounts and ownership, collect evidence, and obtain explicit approval before using force.

### Can I delete `/var/lib/kubelet` or `/var/lib/cni` manually?

Do not provide that as a generic fix. These paths can contain mounted pod volumes, runtime state, or data managed by another component. Prefer reprovisioning from a golden image. If manual cleanup is unavoidable, derive it from current official `nodeadm` and CNI procedures after inspecting mounts and preserving required data.

### The host is powered off. Can I delete the Node object?

Yes, after the workload owner accepts that graceful drain may be impossible and verifies data and endpoint impact. Cordon and delete the stale Node using the approved emergency procedure. Then clean or revoke SSM or IAM Roles Anywhere identity before reusing the host.

### The Node is `NotReady` after `nodeadm init`. Is initialization broken?

Not necessarily. A newly registered Hybrid Node remains `NotReady` until Cilium runs. Check whether Cilium has been installed and scheduled for `eks.amazonaws.com/compute-type=hybrid` before repeating initialization.

### What should I run first when a node cannot join or becomes unhealthy?

Start with read-only evidence and `nodeadm debug --config-source file:///path/to/nodeConfig.yaml --no-color`. It checks credential endpoints, credentials, EKS endpoint access and certificate validity, cluster authentication, and VPC access. Then inspect Node conditions and bounded kubelet/containerd logs.

### Why does the node reach the API but logs and exec fail?

Those directions are different. The node reaches the EKS API on TCP 443. Logs, exec, and port-forward require the EKS control plane to reach the kubelet on TCP 10250 through control-plane ENI routes and security controls.

### Can I reuse the same SSM activation after a failed join?

Do not assume so. Check whether the activation consumed a registration and whether an SSM managed-instance identity exists. Remove stale identity and issue a fresh activation when needed. Blind reuse can create mismatched or duplicate state.

### How do I move a node to another EKS cluster?

Treat it as decommissioning from the old cluster followed by a fresh join. Drain and delete the old Node, uninstall host artifacts, clean CNI state, revoke old identity, validate the host, create credentials for the target cluster, and run the complete bootstrap sequence. Do not reuse cluster-specific NodeConfig or identity.

### How do I clean Cilium from one host?

First remove workloads and the Node safely. Then use the Cilium version's documented host cleanup procedure. Removing the Helm release affects the cluster and can disrupt every Hybrid Node; do not use cluster-wide uninstall to clean one host. Cilium interfaces and routes can remain on disk after removal, so validate host routes, links, BPF state, and CNI files against the selected procedure.

### When should I rebuild instead of repair?

Rebuild when host identity, CNI state, runtime configuration, storage mounts, or package provenance cannot be proven clean; when repair takes longer than replacement; or when immutable-image policy requires it. Preserve an evidence bundle before replacement.

### What confirms complete decommissioning?

The Node object is absent; endpoints no longer reference it; workloads and local data have owners; `nodeadm` artifacts and CNI state meet the cleanup target; credentials are deregistered or revoked; routes, BGP, DNS, monitoring, and inventory are clean; host infrastructure is removed or sanitized; and metering no longer includes the node.

## SRE guide scope to generate for an architecture

When the user asks for an SRE guide, tailor these sections to the selected design:

1. Service and workload overview, owners, SLOs, and locality constraints.
2. Node inventory, labels, taints, identity, image, hardware, storage, and lifecycle owner.
3. Dashboards and alerts for node Ready state, WAN, credentials, CNI, DNS, storage, GPU, workload, and joined vCPU-hours.
4. Routine operations: add, cordon, drain, reboot, certificate or activation rotation, patch, upgrade, and replace.
5. Incident runbooks: cannot join, `NotReady`, API direction failure, DNS, pod routing, storage, GPU, ingress, and WAN loss.
6. Removal, reset for reuse, emergency stale-node deletion, and complete decommissioning.
7. Evidence collection, redaction, escalation, support case inputs, and post-incident review.
8. Game days for WAN loss, resolver loss, credential expiry, node loss, CNI failure, accelerator failure, and billing-stop verification.

Every generated guide MUST name owners and objective success checks. It MUST preserve the architecture's existing IaC and operations ownership.