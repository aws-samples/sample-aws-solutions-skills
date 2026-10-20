# Eval — Cross-Region GPU Inference Scenario (AWS-to-AWS Hybrid Nodes)

A black-box checklist that verifies the `eks-hybrid-node-architecture-advisor` skill produces a correct
architecture brief for attaching GPU capacity in a second AWS Region to an **existing** EKS cluster.
The evaluator judges solely on **user input → expected outputs**.

Decision rationale: `shared/reference/decision-framework.md`, `shared/reference/hybrid-networking-guide.md`,
`shared/reference/cost-model.md`.

---

## User input (simulated prompt)

```
We run an EKS 1.33 cluster in us-east-1, managed with Terraform. GPU capacity is scarce there,
but we can get g6e instances in us-west-2. We want to serve an 8B model from both Regions.
The two VPCs are already connected with a Transit Gateway peering attachment.
Cluster VPC is 10.0.0.0/16, remote VPC is 10.0.0.0/16 as well.
```

Expected: the skill opens with the quick orientation questions (already mostly answered), runs or proposes
read-only discovery, discloses the support posture once, and **stops on the CIDR overlap** before producing
an implementation sequence.

---

## Expected output checklist

### A. Discovery
- [ ] Classifies direction as AWS-to-AWS and skips on-premises hardware, power, and facilities questions
- [ ] Proposes read-only access (AWS profile, kubeconfig or EKS MCP, the Terraform repository) before asking manual questions
- [ ] Does not mutate AWS, Kubernetes, or repository state during discovery

### B. Workload and placement
- [ ] Classifies the workload as inference
- [ ] Requires each Region to host a **complete, independently callable replica** — no tensor or pipeline parallelism across Regions
- [ ] Asks where model weights, images, and request data live, and flags cross-Region transfer for each

### C. Hard gates
- [ ] Support posture: states once that EC2 in another Region is cloud-hosted and not covered by AWS Support, then continues without blocking
- [ ] Network correctness **fails**: the cluster and remote VPC CIDRs overlap (`10.0.0.0/16`), so the skill stops and explains what must change: remote node and pod CIDRs must not overlap the cluster VPC, its secondary CIDRs, or the Service CIDR
- [ ] Requires node-to-API TCP 443 **and** control-plane-to-kubelet TCP 10250 as separate, proven paths
- [ ] Identity: recommends SSM hybrid activations or IAM Roles Anywhere and does not store activation codes in Terraform state or source control

### D. Cost
- [ ] Shows the public Hybrid Nodes fee separately from EC2, EBS, Transit Gateway, and data-transfer costs
- [ ] Refreshes the per-vCPU-hour rate from the EKS pricing page rather than assuming it
- [ ] Asks whether private pricing exists and never assumes a discount; when the selected g6e size exceeds 32 vCPUs per machine (for example `g6e.12xlarge`), tells the user to contact their AWS account team for a private pricing quote
- [ ] Identifies workload access to main-Region services (S3, model registry) as the transfer-cost risk, not control-plane traffic

### E. IaC ownership
- [ ] Updates `remote_network_config` and access entries in the **existing** Terraform state or module
- [ ] Does not generate a replacement cluster or a second owner for the cluster resource
- [ ] Notes that the supplied remote network list replaces the existing one in full

### F. Endpoint and ingress
- [ ] Separates the EKS API endpoint from application ingress, and compares global endpoint options (Route 53, Global Accelerator, CloudFront, application gateway) that target complete replicas
- [ ] Notes that CloudFront origin failover does not fail over POST requests, so it does not solve POST-heavy inference failover
- [ ] With unroutable remote pod CIDRs, recommends a cluster-Region gateway whose pod IPs are the load-balancer targets, rather than registering remote instances or IPs across the peering
- [ ] Sets the load-balancer idle timeout above the longest expected generation and alarms on remote backend health separately from gateway health

### G. Cross-Region data path
- [ ] Requires `AWS_VPC_K8S_CNI_EXCLUDE_SNAT_CIDRS` for the remote node and pod CIDRs so cluster-Region calls to remote endpoints do not fail intermittently
- [ ] Keeps CoreDNS on cluster-Region nodes while remote pod CIDRs are unroutable
- [ ] Explains that a public EKS API endpoint does not remove the private TCP 10250 return path

---

## Pass criteria

All of A–G satisfied, and:

1. **The CIDR overlap stops the design** — no implementation sequence is produced until it is resolved (C).
2. **Support posture is disclosed once and does not block** the design (C).
3. **No replacement cluster** is proposed (E).
