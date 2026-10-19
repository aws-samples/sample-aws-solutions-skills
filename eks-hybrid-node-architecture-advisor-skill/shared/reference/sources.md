# Sources and reference evidence

Use current official AWS documentation as the source of truth. Recheck these pages when the skill runs because pricing, supported versions, and features change.

## EKS Hybrid Nodes

- [Amazon EKS Hybrid Nodes overview](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html)
- [Prerequisite setup](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-prereqs.html)
- [Enable Hybrid Nodes on an existing cluster](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-cluster-update.html)
- [Networking concepts and constraints](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-concepts-networking.html)
- [Kubernetes and EKS API endpoint concepts](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-concepts-kubernetes.html)
- [Network traffic flows](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-concepts-traffic-flows.html)
- [Prepare networking](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-networking.html)
- [Prepare cluster access](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-cluster-prep.html)
- [Prepare credentials](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-creds.html)
- [Connect Hybrid Nodes](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-join.html)
- [Hybrid Nodes nodeadm reference](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-nodeadm.html)
- [Prepare operating systems and images](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-os.html)
- [Configure a proxy](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-proxy.html)
- [Configure add-ons and application networking](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-configure.html)
- [Upgrade Hybrid Nodes](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-upgrade.html)
- [Remove Hybrid Nodes](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-remove.html)
- [Troubleshoot Hybrid Nodes](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-troubleshooting.html)
- [Amazon EKS troubleshooting](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html)
- [Kubernetes: Safely Drain a Node](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/)
- [Configure Cilium](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-cni.html)
- [Configure Cilium BGP](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-cilium-bgp.html)
- [Hybrid Nodes gateway](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-gateway-overview.html)
- [Route 53 Resolver for private EKS endpoint resolution](https://aws.amazon.com/blogs/compute/enabling-dns-resolution-for-amazon-eks-cluster-endpoints/)
- [Hybrid Nodes load balancing](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-load-balancing.html)
- [Hybrid Nodes ingress](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-ingress.html)
- [Amazon EKS pricing](https://aws.amazon.com/eks/pricing/)
- [CloudFormation RemoteNetworkConfig](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-eks-cluster-remotenetworkconfig.html)

## Workload placement and endpoints

- [ML cost best practice: data and compute proximity](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mlcost04-bp13.html)
- [Amazon EKS AI/ML networking guidance](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-networking.html)
- [Run EKS training with EFA](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)
- [Global Accelerator endpoint types](https://docs.aws.amazon.com/global-accelerator/latest/dg/about-endpoints.html)
- [CloudFront origin failover behavior](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/RequestAndResponseBehaviorOriginGroups.html)

## Network and transfer pricing

- [AWS Transit Gateway pricing](https://aws.amazon.com/transit-gateway/pricing/)
- [AWS Site-to-Site VPN pricing](https://aws.amazon.com/vpn/pricing/)
- [AWS Direct Connect pricing](https://aws.amazon.com/directconnect/pricing/)
- [Amazon VPC pricing, including NAT Gateway](https://aws.amazon.com/vpc/pricing/)
- [Amazon EC2 data transfer pricing](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer)
