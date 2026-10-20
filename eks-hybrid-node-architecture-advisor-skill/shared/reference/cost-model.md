# Hybrid Nodes cost model

Refresh all public rates from [Amazon EKS pricing](https://aws.amazon.com/eks/pricing/) when the skill runs. Ask the customer whether private pricing supersedes public rates.

## Cost structure

A Hybrid Nodes design has three direct cost components:

1. **Hybrid Nodes fee** — the per-vCPU-hour charge below.
2. **The node itself** — host compute: EC2 and EBS for cloud-hosted nodes, or hardware, power, and facilities on premises.
3. **Control-plane data transfer** — kubelet, API, and telemetry traffic between the control plane and the Hybrid Nodes. This volume is fairly negligible; note it but do not build the cost case on it.

The cost attention belongs elsewhere: **workload access to services in the main Region**. Every time a pod on a Hybrid Node reads an S3 bucket, calls a service endpoint, or receives a data stream from the cluster Region, that traffic crosses the interconnect and is billed per GiB. A video stream sent continuously from the main Region to the second Region can cost far more than the Hybrid Nodes fee and the hosts combined. Surface this to the user explicitly in every cost estimate.

## Public Hybrid Nodes fee

AWS bills Hybrid Nodes by the vCPUs reported to Kubernetes while each node remains joined to the cluster. Billing starts when the node joins and stops when the Node object is removed from the cluster. Hyperthreaded bare-metal cores can report two vCPUs per physical core.

The fee applies wherever Hybrid Nodes run. AWS states that Hybrid Nodes on Amazon EC2 instances still incur the Hybrid Nodes fee, so a cloud-hosted design pays both EC2 compute and the Hybrid Nodes vCPU-hour charge.

Current public monthly tiers per AWS Region are:

| Aggregate monthly vCPU-hours | Public rate |
|---:|---:|
| First 576,000 | $0.020/vCPU-hour |
| Next 576,000 | $0.014/vCPU-hour |
| Next 4,608,000 | $0.010/vCPU-hour |
| Next 5,760,000 | $0.008/vCPU-hour |
| Above 11,520,000 | $0.006/vCPU-hour |

Tiers aggregate by Region and account. With consolidated billing, AWS applies tiers across accounts in the AWS Organization for the same Region.

For machines with more than 32 vCPUs per node — common for GPU hosts, where a single instance can report 96 to 192 vCPUs — the public per-vCPU rate scales poorly. Direct the customer to contact their AWS account team for a private pricing quote before committing to the design.

## Required calculation

For each node class or site:

```text
monthly vCPU-hours = node count × reported vCPUs per node × joined hours/month
```

Apply progressive tiers to the total Regional usage. Do not multiply all usage by the lowest reached tier.

For a quick estimate that remains in the first tier:

```text
Hybrid Nodes fee = monthly vCPU-hours × $0.020
```

Example: 10 nodes × 8 vCPU × 730 hours = 58,400 vCPU-hours, or $1,168/month at the first public tier.

## Pricing challenge

The architecture brief MUST answer:

- What number of vCPUs does Kubernetes report?
- How many hours do nodes remain joined, including idle time?
- What existing Regional Hybrid Nodes usage affects the tier?
- Is private pricing confirmed, unavailable, or unknown?
- Does any node class exceed 32 vCPUs per machine? If so, has the customer requested a private pricing quote from their AWS account team?
- Who accepts the public-price exposure if private pricing is unknown?
- How does cost change at expected and peak scale?
- What process removes idle nodes from the cluster so billing stops? Use the safe-removal runbook in [sre-operations-and-faq.md](sre-operations-and-faq.md); cordon, drain, host shutdown, and `nodeadm uninstall` alone do not stop the fee.

## Workload data-transfer model

Calculate traffic before choosing a connection or endpoint. The per-GiB rate depends on the interconnect carrying the flow, so the same GiB volume prices differently on Direct Connect, Site-to-Site VPN, VPC peering, or Transit Gateway. Identify the interconnect for each flow first, look up its current rate, and calculate each path separately:

| Interconnect | What drives the per-GiB cost |
|---|---|
| Inter-Region VPC peering | Inter-Region data-transfer rate in each direction, billed per Region pair |
| Transit Gateway | Data-processing charge per GiB on top of the transfer rate, plus attachment hours |
| Site-to-Site VPN | Connection hours plus standard data-transfer-out rates |
| Direct Connect | Port hours, cross-connect, and DX-specific data-transfer-out rates (usually the lowest per-GiB) |

Create one row per direction, locality, and traffic class:

| Traffic class | Source → destination | Events/month | Average GiB/event | Monthly GiB | Path | Payer and rate | Monthly cost |
|---|---|---:|---:|---:|---|---|---:|
| Training dataset or preprocessing output |  |  |  |  |  |  |  |
| Training checkpoints |  |  |  |  |  |  |  |
| Model, adapter, and container distribution |  |  |  |  |  |  |  |
| Inference requests |  |  |  |  |  |  |  |
| Inference responses or streams |  |  |  |  |  |  |  |
| Control plane, logs, metrics, and traces |  |  |  |  |  |  |  |
| Storage, cache, or replication traffic |  |  |  |  |  |  |  |

```text
monthly GiB = events/month × average GiB/event
monthly path cost = Σ(monthly GiB × current path-specific $/GiB) + hourly, port, cross-connect, and attachment charges
```

Refresh every rate for the actual Regions and connection type. Identify the charging side and payer, model both directions when AWS bills them separately, and avoid counting the same transfer twice. Include expected and peak scenarios.

For training, include initial dataset staging, repeated reads across the WAN, checkpoint frequency and size, and artifact return. Repeated cross-Region dataset reads are the dominant avoidable cost: the best option is often to copy the dataset once to the machine's local storage or to an S3 bucket in the second Region, then train against the local copy. Pay the one-time staging transfer instead of per-epoch WAN reads, and compare the added storage cost against the recurring transfer it eliminates.

For inference, include request and response size, streaming duration, requests per second, model synchronization, cache/state replication, and failover traffic.

## Adjacent costs

Show these separately because the Hybrid Nodes fee does not include them:

- EKS cluster standard or extended support fee
- physical or virtual host cost, power, facilities, and support
- EC2 and EBS cost for cloud-hosted nodes
- Direct Connect ports, cross-connects, gateway, and data transfer
- Site-to-Site VPN connections and data transfer
- Transit Gateway attachments and data processing
- inter-Region VPC peering transfer
- NAT Gateway hours and processing
- public IPv4 addresses
- EKS Hybrid Nodes gateway compute
- observability, security, image registry, storage, backup, and operations

## Decision rule

If the public Hybrid Nodes fee changes the preferred architecture, do not proceed until a budget owner confirms private pricing or accepts the public estimate. Present alternatives such as a separate EKS cluster, conventional EC2 nodes, EKS Anywhere, EKS on Outposts, or a smaller joined footprint when they satisfy the requirement.