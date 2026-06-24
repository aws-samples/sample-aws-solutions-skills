# Pattern — Bedrock Mantle in us-east-1 via cross-region VPC peering

Bedrock **Mantle** (OpenAI GPT-5.x via the Responses route, `bedrock_mantle/`) is invoked at
`https://bedrock-mantle.<region>.api.aws`. Mantle is available in **us-east-1 (Virginia)**, so to keep
that traffic **private** and pin it to Virginia regardless of where the gateway platform runs
(`config.awsRegion`, e.g. us-east-2), we reach it over a **cross-region VPC peering** connection to a
small peer VPC in us-east-1 that holds the `bedrock-mantle` PrivateLink endpoint.

This is two stacks (CfnRoute is regional, so each region's routes live in a same-region stack):
- **MantleNetworkStack** (us-east-1): peer VPC + `bedrock-mantle` interface endpoint + the peering
  connection (requester) + acceptance (custom resource in the primary region) + peer-side routes +
  a **cross-region private hosted zone** so the gateway resolves the mantle hostname over the peering.
- **MantlePeeringRoutesStack** (primary region): the primary VPC's `peerCidr → pcx` routes.

> The `com.amazonaws.us-east-1.bedrock-mantle` PrivateLink service exists (verify:
> `aws ec2 describe-vpc-endpoint-services --region us-east-1 --filters Name=service-name,Values=com.amazonaws.us-east-1.bedrock-mantle`).
> LiteLLM's `bedrock_mantle` provider (#29788 `_resolve_region`) reads the region from, in order:
> `aws_region_name` → `BEDROCK_MANTLE_API_BASE` host → `BEDROCK_MANTLE_REGION` → `AWS_REGION`.
> So set the GPT models' `aws_region_name` to us-east-1 AND inject env
> `BEDROCK_MANTLE_REGION=us-east-1` + `BEDROCK_MANTLE_API_BASE=https://bedrock-mantle.us-east-1.api.aws`.
> ⚠️ `MANTLE_REGION` is a doc alias only — it is **NOT read** by the provider; relying on it leaves
> the endpoint at `AWS_REGION` (the gateway region) and the call fails with
> "Cannot connect to host bedrock-mantle.<gw-region>.api.aws". Auth is **SigV4 (Task Role)** — no bearer token.

## Config (`config/dev.json`)

```jsonc
"mantle": {
  "region": "us-east-1",            // Virginia (where Mantle lives)
  "peerVpcCidr": "10.1.0.0/16",     // must NOT overlap network.vpcCidr
  "enablePrivateEndpoint": true     // create the bedrock-mantle endpoint + PHZ; false = peering+routes only
}
```

Schema:
```ts
export interface MantleConfig {
  readonly region: string;            // us-east-1
  readonly peerVpcCidr: string;       // non-overlapping with network.vpcCidr
  readonly enablePrivateEndpoint: boolean;
}
```

## Stack: `lib/mantle-network-stack.ts` (us-east-1)

```ts
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { MantleNetworkExports, NetworkExports } from './interfaces';
import { MantleConfig } from './config/schema';
import { PORTS, ns } from './config/constants';

export interface MantleNetworkStackProps extends cdk.StackProps {
  readonly config: MantleConfig;
  readonly primaryNetwork: NetworkExports;  // peering requester's peer (the primary VPC)
  readonly primaryVpcCidr: string;
  readonly primaryRegion: string;           // accepter region
}

export class MantleNetworkStack extends cdk.Stack implements MantleNetworkExports {
  public readonly peerVpcId: string;
  public readonly peerVpcCidr: string;
  public readonly peeringConnectionId: string;
  public readonly mantleRegion: string;

  constructor(scope: Construct, id: string, props: MantleNetworkStackProps) {
    super(scope, id, props);
    const { config, primaryNetwork, primaryVpcCidr, primaryRegion } = props;
    this.mantleRegion = config.region;
    this.peerVpcCidr = config.peerVpcCidr;

    // Peer VPC — isolated only (reachable solely over the peering)
    const peerVpc = new ec2.Vpc(this, 'MantlePeerVpc', {
      ipAddresses: ec2.IpAddresses.cidr(config.peerVpcCidr),
      maxAzs: 2, natGateways: 0,
      subnetConfiguration: [{ name: 'mantle-isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 }],
    });
    this.peerVpcId = peerVpc.vpcId;

    // Cross-region peering (requester = peer VPC; peer = primary VPC in primaryRegion)
    const peering = new ec2.CfnVPCPeeringConnection(this, 'MantlePeering', {
      vpcId: peerVpc.vpcId,
      peerVpcId: primaryNetwork.vpc.vpcId,
      peerRegion: primaryRegion,
    });
    this.peeringConnectionId = peering.ref;

    // Cross-region peering is NOT auto-accepted → accept it IN the primary region.
    // AwsCustomResource supports a per-call `region`, so the accept runs against primaryRegion.
    const accept = new cr.AwsCustomResource(this, 'AcceptPeering', {
      onCreate: {
        service: 'EC2', action: 'acceptVpcPeeringConnection',
        parameters: { VpcPeeringConnectionId: peering.ref },
        region: primaryRegion,
        physicalResourceId: cr.PhysicalResourceId.of(peering.ref),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE }),
      installLatestAwsSdk: false,
    });
    accept.node.addDependency(peering);

    // Peer-side routes: primary CIDR -> pcx
    peerVpc.isolatedSubnets.forEach((subnet, i) => {
      const route = new ec2.CfnRoute(this, `PeerRoute${i}`, {
        routeTableId: subnet.routeTable.routeTableId,
        destinationCidrBlock: primaryVpcCidr,
        vpcPeeringConnectionId: peering.ref,
      });
      route.node.addDependency(accept);
    });

    // bedrock-mantle interface endpoint + cross-region private DNS
    if (config.enablePrivateEndpoint) {
      const endpointSg = new ec2.SecurityGroup(this, 'MantleEndpointSg', {
        vpc: peerVpc, securityGroupName: ns('mantle-endpoint-sg'),
        description: 'bedrock-mantle interface endpoint (443 from primary VPC over peering)',
        allowAllOutbound: false,
      });
      endpointSg.addIngressRule(ec2.Peer.ipv4(primaryVpcCidr), ec2.Port.tcp(PORTS.HTTPS), 'primary VPC over peering');
      endpointSg.addIngressRule(ec2.Peer.ipv4(config.peerVpcCidr), ec2.Port.tcp(PORTS.HTTPS), 'peer VPC');

      // privateDnsEnabled=false → we publish DNS via a cross-region PHZ so the
      // gateway VPC (different region) can resolve mantle across the peering.
      const mantleEndpoint = new ec2.InterfaceVpcEndpoint(this, 'MantleEndpoint', {
        vpc: peerVpc,
        service: new ec2.InterfaceVpcEndpointService(`com.amazonaws.${config.region}.bedrock-mantle`, PORTS.HTTPS),
        securityGroups: [endpointSg],
        subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
        privateDnsEnabled: false,
      });

      const zoneName = `bedrock-mantle.${config.region}.api.aws`;
      const phz = new route53.CfnHostedZone(this, 'MantlePhz', {
        name: zoneName,
        vpcs: [
          { vpcId: peerVpc.vpcId, vpcRegion: config.region },
          { vpcId: primaryNetwork.vpc.vpcId, vpcRegion: primaryRegion }, // cross-region association
        ],
      });

      // Apex ALIAS -> the endpoint's first regional DNS entry ("Z123:dnsName")
      const dnsEntry = cdk.Fn.select(0, mantleEndpoint.vpcEndpointDnsEntries);
      new route53.CfnRecordSet(this, 'MantleAlias', {
        hostedZoneId: phz.ref, name: zoneName, type: 'A',
        aliasTarget: {
          hostedZoneId: cdk.Fn.select(0, cdk.Fn.split(':', dnsEntry)),
          dnsName: cdk.Fn.select(1, cdk.Fn.split(':', dnsEntry)),
          evaluateTargetHealth: false,
        },
      });
    }

    new cdk.CfnOutput(this, 'MantlePeerVpcId', { value: peerVpc.vpcId });
    new cdk.CfnOutput(this, 'MantlePeeringConnectionId', { value: peering.ref });
    new cdk.CfnOutput(this, 'MantleRegion', { value: config.region });
  }
}
```

## Stack: `lib/mantle-peering-routes-stack.ts` (primary region)

```ts
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { NetworkExports } from './interfaces';

export interface MantlePeeringRoutesStackProps extends cdk.StackProps {
  readonly primaryNetwork: NetworkExports;
  readonly peerVpcCidr: string;
  readonly peeringConnectionId: string;   // cross-region ref from MantleNetworkStack
}

export class MantlePeeringRoutesStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MantlePeeringRoutesStackProps) {
    super(scope, id, props);
    const { primaryNetwork, peerVpcCidr, peeringConnectionId } = props;
    const subnets = [
      ...primaryNetwork.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnets,
      ...primaryNetwork.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnets,
    ];
    const seen = new Set<string>();
    subnets.forEach((subnet, i) => {
      const rtId = subnet.routeTable.routeTableId;
      if (seen.has(rtId)) return;
      seen.add(rtId);
      new ec2.CfnRoute(this, `PrimaryMantleRoute${i}`, {
        routeTableId: rtId,
        destinationCidrBlock: peerVpcCidr,
        vpcPeeringConnectionId: peeringConnectionId,
      });
    });
  }
}
```

Export (`lib/interfaces.ts`):
```ts
export interface MantleNetworkExports {
  readonly peerVpcId: string;
  readonly peerVpcCidr: string;
  readonly peeringConnectionId: string;
  readonly mantleRegion: string;   // us-east-1
}
```

## Wiring (`bin/app.ts`)

```ts
const mantleNetwork = new MantleNetworkStack(app, 'MantleNetworkStack', {
  env: { account, region: config.mantle.region }, stackName: ns('mantle-network'), tags,
  crossRegionReferences: true,
  config: config.mantle, primaryNetwork: network,
  primaryVpcCidr: config.network.vpcCidr, primaryRegion,
});
const mantleRoutes = new MantlePeeringRoutesStack(app, 'MantlePeeringRoutesStack', {
  ...stackProps('mantle-peering-routes'), crossRegionReferences: true,
  primaryNetwork: network, peerVpcCidr: mantleNetwork.peerVpcCidr,
  peeringConnectionId: mantleNetwork.peeringConnectionId,
});
mantleRoutes.addDependency(mantleNetwork);
```

And LiteLLM env (the vars the provider actually reads):
`BEDROCK_MANTLE_REGION: config.mantle.region` and
`BEDROCK_MANTLE_API_BASE: https://bedrock-mantle.${config.mantle.region}.api.aws`, plus each GPT
model's `aws_region_name` set to the same region. (`MANTLE_REGION` may be set as a human-readable
alias but is NOT consumed by `bedrock_mantle`.)

## Gotchas

- **Acyclic design**: NetworkStack only *exports* the VPC. MantleNetworkStack (us-east-1) creates the
  peering + acceptance + peer-side routes + endpoint + PHZ; MantlePeeringRoutesStack (primary region)
  adds the primary-side routes. Don't add peer routes inside NetworkStack — that creates a cycle.
- **Cross-region peering is not auto-accepted.** Use an `AwsCustomResource` with a per-call `region`
  set to the **primary** region to call `acceptVpcPeeringConnection`.
- **Cross-region private DNS**: an interface endpoint's `privateDnsEnabled` only resolves in its own
  VPC/region. To resolve `bedrock-mantle.us-east-1.api.aws` from the gateway VPC, set
  `privateDnsEnabled:false` and publish a `CfnHostedZone` (PHZ) associated with **both** VPCs
  (cross-region association via the `VpcRegion` field), aliased to the endpoint's regional DNS entry.
- **CIDRs must not overlap** (`mantle.peerVpcCidr` ≠ `network.vpcCidr`) — schema-validate it.
- **Bootstrap** both us-east-1 and the gateway region; both LiteLLM and the routes stack need
  `crossRegionReferences: true`.
- If `enablePrivateEndpoint:false`, the peering + routes still deploy; mantle then resolves via public
  DNS over NAT (less private) — keep `true` for the private path.
