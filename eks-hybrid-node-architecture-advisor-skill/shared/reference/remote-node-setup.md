# Remote Hybrid Node setup and lifecycle

Use this guide after the architecture, networking, and compatibility gates pass. It defines an ordered host lifecycle, not a universal shell script. Adapt package names, services, paths, and hardware steps to the selected supported operating system and existing configuration-management owner.

A node is workload-ready only after every selected gate passes:

```text
planned → image-ready → network-ready → credential-ready → dependencies-installed
→ config-validated → initialized → runtime-configured → CNI-ready
→ hardware-ready → workload-ready → maintained → drained → removed
```

Do not skip a failed state. Capture evidence and fix the failed layer before advancing.

## Required output: remote node build and bootstrap plan

Produce this table for each node class:

| State | CPU/GPU branch | Golden-image action | First-boot action | Secret input | Owner | Success evidence | Rollback |
|---|---|---|---|---|---|---|---|

Also record the pinned operating-system image, architecture, kernel, Kubernetes minor version, Hybrid Nodes `nodeadm` version and checksum, credential provider, containerd version, CNI version, driver/runtime versions, labels, taints, proxy behavior, egress dependencies, and replacement policy.

## The ordering rule that prevents runtime loss

`nodeadm init` writes and starts the final kubelet and containerd configuration. Apply host runtime customizations that modify containerd, including `nvidia-ctk runtime configure`, **after** `nodeadm init`. Applying them earlier can let initialization overwrite them.

| Before `nodeadm init` | After successful `nodeadm init` |
|---|---|
| Install the supported OS, kernel, firmware, and host drivers | Reapply required containerd runtime customizations |
| Install or pin Hybrid Nodes `nodeadm` and verify its checksum | Restart containerd only when the customization requires it |
| Run `nodeadm install`, unless the image already contains its dependencies | Install the cluster CNI and wait for the Node to become Ready |
| Configure host proxy, CA trust, time sync, DNS, and firewall behavior | Install workload discovery and device plug-ins |
| Prepare SSM or IAM Roles Anywhere credentials | Validate allocatable resources and run a bounded smoke workload |
| Render NodeConfig with mode `0600` | Verify reboot persistence and monitoring |
| Run `nodeadm config check` | Remove short-lived NodeConfig secrets after successful persistence |

Do not place SSM activation codes, IAM Roles Anywhere private keys, certificates, kubeconfigs, or rendered secret NodeConfig in source control, images, logs, or unprotected IaC state.

## 1. Satisfy external prerequisites

Complete cluster and network changes before joining a host:

- Add every intended host address to the cluster's complete `RemoteNodeNetwork` list.
- Add `RemotePodNetwork` when selected traffic flows require routable pod addresses.
- Create the Hybrid Nodes IAM role and its `HYBRID_LINUX` access entry.
- Prove endpoint DNS and node-to-API TCP 443 connectivity.
- Prove the private control-plane ENI return route to node TCP 10250.
- Prepare Cilium values, pod CIDRs, scheduling rules, and an installation owner.
- Approve labels, taints, capacity, maintenance, replacement, and decommissioning behavior.

A public EKS API path does not satisfy the control-plane return route. Read [hybrid-networking-guide.md](hybrid-networking-guide.md) before approving this gate.

## 2. Build or select the host image

Use a current operating system and architecture supported by EKS Hybrid Nodes. Validate the kernel, cgroups, required modules, time synchronization, DNS, CA trust, filesystem capacity, and container runtime prerequisites against the pinned component versions.

For repeatable production nodes, prefer an immutable golden image:

- Bake OS updates, approved CA roots, diagnostic tooling, pinned `nodeadm`, and dependencies that do not identify one node.
- Use the official EKS Hybrid Nodes Packer path when it fits the image pipeline.
- Pin artifacts and verify checksums. Do not download `latest` during production bootstrap.
- Keep node identity, activation material, private keys, certificates, and cluster-specific NodeConfig out of the image.
- Run first-boot join logic through an owned service such as systemd, cloud-init, or the site's configuration manager. Define retries, timeouts, and terminal failure behavior.

First-boot installation can fit a disposable experiment, but it increases bootstrap time and depends on package and artifact services remaining reachable.

### CPU branch

A CPU image needs the supported OS, kernel, and `nodeadm` dependencies. Do not fail the bootstrap because GPU tooling is absent.

### GPU or accelerator branch

Prepare hardware before Kubernetes registration:

1. Install compatible host firmware and the vendor driver.
2. Verify the device and driver from the host.
3. Install the matching container runtime toolkit and CLI, such as NVIDIA Container Toolkit and `nvidia-ctk`.
4. Validate NVML or the vendor management library.
5. Record driver, toolkit, firmware, kernel, and device-plugin compatibility.

Do not assume `nodeadm`, Cilium, NFD, or a Kubernetes device plug-in installs the host driver. A GPU taint controls placement; it does not discover or advertise a GPU.

## 3. Run network, DNS, clock, and egress preflight

Validate from the exact host subnet and proxy context:

- The selected EKS endpoint resolves to the intended public or private addresses.
- TCP 443 reaches that endpoint through the approved path.
- DNS reaches required resolvers over TCP and UDP 53.
- NTP or another approved time source keeps credentials and TLS within tolerance.
- The host can retrieve pinned `nodeadm` and every `nodeadm install` artifact.
- Package repositories, container registries, CNI images, identity endpoints, log sinks, model stores, and storage dependencies are reachable.
- MTU and asymmetric routing tests pass.
- The node address falls within `RemoteNodeNetwork`.

For restricted egress, build an explicit hostname, port, proxy, CA, and private-endpoint allowlist from current AWS documentation and every selected add-on. Do not replace this inventory with unrestricted outbound access.

### Proxy requirements

Apply OS and service proxy configuration before `nodeadm init`. Configure kubelet, containerd, SSM or IAM Roles Anywhere tooling, and package utilities as required. Include the EKS endpoint, cluster and remote CIDRs, Service CIDR, pod CIDRs, node-local addresses, DNS, and approved internal endpoints in `NO_PROXY` when direct routing is required. Validate the actual path rather than copying a generic `NO_PROXY` list.

## 4. Prepare one credential identity per node

Choose one provider and define revocation before issuance.

### SSM hybrid activation

- Use Hybrid Nodes `nodeadm` version `1.0.19` or later.
- Create the activation in the EKS cluster Region with deliberate expiration and registration limits.
- Deliver the activation ID and code through an approved short-lived secret channel.
- Never reuse a one-registration activation after a partial registration without checking SSM managed-instance state.
- Define stale managed-instance cleanup and activation revocation.

### IAM Roles Anywhere

- Issue a unique certificate and private key through the managed PKI process.
- Ensure the node name and certificate identity satisfy the selected NodeConfig rules.
- Protect key files with restrictive ownership and permissions.
- Validate trust anchor, profile, role, CRL or revocation behavior, renewal, expiry alerting, and replacement.
- Never bake a private key into a shared image.

Both branches use the same Hybrid Nodes IAM role as the EKS access-entry principal. Do not use an EC2 instance profile as a substitute for the documented on-premises credential provider.

## 5. Install Hybrid Nodes dependencies

Use the Hybrid Nodes `nodeadm`, not the different AL2023 EC2 bootstrap implementation. Run it with root or sudo access.

```text
nodeadm install <cluster-major.minor> --credential-provider <ssm|iam-ra>
```

`nodeadm install` installs or validates Kubernetes and host dependencies such as kubelet, containerd, kubectl, CNI binaries, and the selected AWS credential tooling. Skip runtime installation only when the image pipeline already installed the exact required artifacts and its validation proves they remain intact.

Match the installed kubelet minor version to the cluster upgrade plan and Kubernetes version-skew policy. Record package versions and installation logs. A successful install does not register the node.

## 6. Render and validate NodeConfig securely

Create a node-specific NodeConfig only after the host passes preflight. Include:

- Cluster name and Region.
- The selected SSM or IAM Roles Anywhere configuration.
- Stable locality, hardware-class, ownership, and lifecycle labels.
- Registration taints that prevent workloads from arriving before specialized readiness gates pass.
- Only deliberate kubelet configuration.

Store it in a root-owned file with mode `0600`. Avoid command-line secret arguments because process inspection and shell history can expose them. Keep rendered content out of Terraform plan output, cloud-init logs, CI artifacts, and support bundles.

Validate before initialization:

```text
nodeadm config check --config-source file:///path/to/nodeConfig.yaml
```

Stop on schema, identity, endpoint, cluster-access, CIDR, or permission errors. Do not use `--skip` flags as a routine workaround. If the IAM role deliberately omits discovery permissions, provide the required cluster data explicitly and document the tradeoff.

## 7. Initialize and register the node

Run initialization once the configuration passes:

```text
nodeadm init --config-source file:///path/to/nodeConfig.yaml
```

Capture `nodeadm` output and inspect kubelet, containerd, and credential-provider services. Confirm:

- The command completed without skipped validation.
- Kubernetes shows exactly one expected Node identity.
- The Node has `eks.amazonaws.com/compute-type=hybrid` and the intended labels and taints.
- The internal IP matches the approved remote node CIDR.
- SSM or IAM Roles Anywhere provides renewable credentials.
- API-to-kubelet operations can reach TCP 10250.

A registered node remains `NotReady` until Cilium runs. Treat this as the expected intermediate state, not a reason to repeat `nodeadm init`.

### Failure handling

On failure, preserve restrictive NodeConfig access until you understand whether identity persisted. Collect bounded logs from cloud-init or systemd, `nodeadm`, kubelet, containerd, and SSM or IAM Roles Anywhere. Redact secrets before sharing them.

Do not blindly rerun initialization. First check for a Kubernetes Node with the same name, an SSM managed-instance registration, credential files, running services, and partially written configuration. Revoke or remove stale identity through the documented lifecycle before retrying with a fresh identity.

## 8. Apply post-initialization runtime customization

Apply configuration that `nodeadm init` could overwrite only now.

For an NVIDIA node, the tested ordering is:

```text
nodeadm install
nodeadm config check
nodeadm init
nvidia-ctk runtime configure --runtime=containerd --set-as-default
systemctl restart containerd
```

Run the NVIDIA commands only when `nvidia-ctk` exists and this node class requires that runtime. Validate the generated containerd configuration and service health before continuing. Other accelerators need their vendor-specific post-initialization runtime gate.

Persist the customization through reboot and future `nodeadm upgrade` operations. An in-place upgrade can replace runtime artifacts, so rerun and revalidate this phase after every upgrade.

## 9. Install cluster networking and reach Ready

Install the selected supported Cilium build with scheduling restricted to Hybrid Nodes. Align its IPAM pool with the cluster's `RemotePodNetwork`. Keep Amazon VPC CNI off Hybrid Nodes. The pod CIDRs and per-node mask size cannot change after Cilium is installed, so size them for the eventual pods per node; widening them means rebuilding.

Wait for these gates:

1. Cilium DaemonSet is Ready on the node.
2. The Kubernetes Node becomes Ready.
3. CoreDNS resolution works from a pod on that node.
4. Pod-to-API, required pod-to-pod, pod-to-service, and egress flows pass.
5. Routable pod CIDRs advertise or traverse the selected gateway correctly.
6. Logs, exec, port-forward, webhooks, and metrics pass where the design requires them.

Do not install workloads merely because the Node condition changed to Ready.

## 10. Discover and advertise hardware

### CPU nodes

Validate CPU architecture, capacity, huge pages, NUMA behavior, local disks, and any workload-specific kernel feature. Remove a bootstrap taint only after these checks pass.

### GPU nodes

Use one approved discovery and management approach. If the design selects standalone NFD plus a device plug-in, preserve this order:

1. Install NFD.
2. Verify the expected vendor PCI label, such as `feature.node.kubernetes.io/pci-10de.present=true` for NVIDIA.
3. Install the pinned device plug-in.
4. Verify its DaemonSet pod initializes the vendor library.
5. Verify a non-zero allocatable resource such as `nvidia.com/gpu`.
6. Run a bounded device smoke workload.

A GPU Operator may own drivers, toolkit, NFD, device plug-in, health, and telemetry in another design. Do not combine overlapping ownership. Classify its exact version and host OS as supported or explicitly unvalidated before selecting it.

For EC2 Capacity Blocks, match the subnet Availability Zone, instance type, and AMI to the reservation. Instances terminate near the reservation end time, so keep durable model and checkpoint data off the instance and plan Node deletion before the end time.

Register GPU taints before general scheduling begins. Workloads must request the advertised resource and tolerate the taint. Labels and taints alone never prove device usability.

## 11. Validate storage and workload readiness

Select storage by locality and failure behavior, not by cluster scope.

- A cloud-block CSI driver such as Amazon EBS cannot attach a Region volume to an on-premises host.
- Validate every CSI node plug-in on the Hybrid Node OS, kernel, mount tools, identity path, network, topology labels, and upgrade path.
- Keep state and compute in one locality unless replication and WAN behavior have explicit owners and tests.
- Define what happens to volumes, checkpoints, caches, and local model data when a node or WAN path fails.
- Test provision, attach or mount, read/write, detach, reschedule, snapshot, restore, expansion, and cleanup as applicable.

Before admitting production traffic, prove scheduling locality, complete-model availability, image and model retrieval, secrets, observability, ingress, health checks, disruption budgets, and rollback. Distributed training must remain within one locality. Each inference endpoint must target complete independently callable replicas.

## 12. Prove reboot and credential persistence

Reboot one non-critical node before scaling the class. Verify:

- Credential tooling renews access without activation reuse.
- kubelet, containerd, proxy configuration, and post-init runtime settings start correctly.
- Cilium and required DaemonSets recover.
- The node returns Ready with the same intended identity.
- Hardware resources reappear.
- No workload schedules before taints and readiness gates apply.

Remove short-lived activation material and rendered NodeConfig secrets only after successful initialization and persisted credential validation. Keep the minimum non-secret configuration needed for reboot, upgrade, and support.

## 13. Upgrade safely

Prefer image replacement when the environment supports immutable hosts. For an in-place Kubernetes upgrade:

1. Confirm the target cluster and node version sequence satisfies EKS and Kubernetes skew policy.
2. Validate the target OS, Cilium, kube-proxy, CoreDNS, CSI, security, observability, driver, runtime, and device plug-in compatibility.
3. Upgrade one node at a time.
4. Cordon and drain the node while respecting disruption budgets and local state.
5. Run `nodeadm upgrade <target-major.minor> --config-source file:///path/to/nodeConfig.yaml`.
6. Reapply post-init runtime customization.
7. Validate Ready, networking, storage, hardware, and a smoke workload.
8. Uncordon only after every gate passes.

`nodeadm upgrade` stops, removes, installs, and restarts Kubernetes components. Treat it as disruptive. It cannot change the credential-provider type. Never run it on an active workload without a validated evacuation plan.

## 14. Drain, remove, uninstall, and stop billing

Use this order for replacement or decommissioning:

1. Remove the node from application endpoints and confirm traffic drains.
2. Cordon the Kubernetes Node.
3. Drain workloads, respecting state, disruption budgets, local volumes, and accelerator jobs.
4. Delete the Kubernetes Node object.
5. Run `nodeadm uninstall` with root or sudo.
6. Remove CNI and other add-on files according to their owners' procedures.
7. Verify SSM managed-instance deregistration or revoke IAM Roles Anywhere credentials. Terminating an EC2 host does not deregister its `mi-*` record; it moves to `ConnectionLost` and must be deregistered explicitly.
8. Revoke unused activation material and remove secrets.
9. Remove host, DNS, BGP, firewall, route, monitoring, and inventory entries.
10. Confirm the Node no longer appears attached to the cluster and Hybrid Nodes metering stops.

`nodeadm uninstall` does not drain or delete the Kubernetes Node. It does not necessarily remove every CNI or add-on artifact. Its force mode can delete default Kubernetes and CNI directory contents; never use it without inspecting mounts and data first.

Remove a remote CIDR from cluster configuration only after no desired node or pod uses it. Preserve all other desired CIDRs because EKS replaces the complete supplied list.

## Automation and IaC catches

- Preserve the existing host provisioning owner. Keep image build, host creation, identity issuance, cluster changes, and add-on lifecycle as explicit interfaces.
- Replacing first-boot data can recreate a host. For example, Terraform `user_data_replace_on_change = true` replaces the instance when rendered bootstrap changes.
- Sensitive Terraform inputs remain present in state and may appear in rendered user data even when variables are marked sensitive. Use encrypted restricted state and avoid passing durable credentials through IaC.
- A generic OS image is not a GPU-ready image. Select an approved hardware image or build one with drivers and runtime tooling.
- Make accelerator customization conditional so CPU nodes do not fail.
- Delete NodeConfig only after successful initialization and persisted identity checks. A command sequence that deletes it unconditionally can erase evidence after partial failure.
- Use terminal failure markers and bounded retries. Do not create duplicate Kubernetes or SSM identities.
- Store bootstrap logs without secret values and retain enough evidence to diagnose the failed state.

## Minimum acceptance evidence

A rollout gate is complete only when evidence records:

- Pinned image and component versions with checksums.
- Successful endpoint DNS, TCP 443, TCP 10250, and required pod-route tests.
- Successful `nodeadm config check` and one clean initialization.
- Ready Cilium, DNS, and service networking.
- Correct labels, taints, allocatable CPU or accelerator resources, and locality.
- Successful storage and workload smoke tests.
- Successful reboot recovery.
- Successful drain, uninstall, identity revocation, and billing-stop rehearsal on a disposable node.