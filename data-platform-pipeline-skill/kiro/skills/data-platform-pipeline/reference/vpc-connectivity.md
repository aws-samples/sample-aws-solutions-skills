# On-prem / Private VPC Connectivity (JDBC sources)

Applies to **both** patterns (Iceberg and Hive) whenever the JDBC source is on-premises or in a private subnet (not a public RDS endpoint). Glue reaches it through **ENIs that Glue creates in a VPC subnet**, and traffic flows out over a Site-to-Site VPN or Direct Connect. Get the network right *before* building the Glue Connection or the ETL job — connectivity failures here are the most common reason an on-prem Data Lab build stalls, and they surface as opaque timeouts deep inside a job run.

For public RDS / a reachable endpoint, you can skip the network prerequisites and create the Glue Connection directly.

---

## Network prerequisites — verify each before creating the Glue Connection

Walk these checks with the user one at a time; for each, run the probe where one exists and only continue once it passes. Do NOT generate the Glue Connection or ETL job until all four pass.

1. **Hybrid link established?** — Is a Site-to-Site VPN or Direct Connect up between the VPC and the on-prem network?
   ```bash
   # VPN tunnels should show State=UP (at least one of the two)
   aws ec2 describe-vpn-connections --region {region} \
     --query 'VpnConnections[].{Id:VpnConnectionId,State:State,Tunnels:VgwTelemetry[].Status}'
   # If using Direct Connect instead:
   aws directconnect describe-connections --region {region} \
     --query 'connections[].{Id:connectionId,State:connectionState}'
   ```
   If neither exists, STOP — the customer's network team must establish the link first. This is not something the skill can provision.

2. **Route propagation confirmed?** — Does the route table for the Glue subnet have a route to the on-prem CIDR, pointing at the VGW (VPN) or TGW (Transit Gateway)?
   ```bash
   aws ec2 describe-route-tables --region {region} \
     --filters "Name=association.subnet-id,Values={glue_subnet_id}" \
     --query 'RouteTables[].Routes[].{Dest:DestinationCidrBlock,GW:GatewayId,TGW:TransitGatewayId}'
   # Expect a row whose Dest matches the on-prem DB CIDR (e.g. 10.10.0.0/16).
   ```

3. **DNS resolution of the on-prem DB hostname?** — Can a host *in the Glue subnet* resolve the DB hostname? Test from a bastion EC2 or a Lambda attached to the same subnet (do NOT test from your laptop — it resolves via a different path):
   ```bash
   # From a bastion/EC2 in the Glue subnet:
   nslookup {onprem_db_hostname}
   # Returns the on-prem private IP → OK. NXDOMAIN/timeout → DNS not wired.
   ```
   If the hostname only resolves via an on-prem DNS server, the VPC needs custom DNS (Route 53 Resolver inbound/outbound endpoints, or a DHCP option set pointing at the on-prem resolver) — and the Glue security group must allow outbound 53/tcp + 53/udp to that resolver. Prefer the JDBC URL by **IP** only as a last resort (breaks on DB failover).

4. **On-prem firewall allows inbound from AWS?** — The on-prem firewall must permit inbound on the DB port from the source AWS sees. With a VPN/DX and no NAT, that source is the **Glue ENIs' private IPs** (i.e. the Glue subnet CIDR). If egress is via a NAT gateway, it's the **NAT GW's IP/EIP**. Confirm with the customer's network team which applies, and give them the exact CIDR/IP and port to allowlist. The skill cannot verify this from the AWS side — it's a question to the on-prem team.

> ⚠ **Two requirements that are easy to miss and break the build:**
> - **Self-referencing security group rule.** The SG on the Glue Connection must allow **all-TCP inbound from itself** (self-reference). Glue's ENIs/DPUs talk to each other over this; without it, jobs hang or fail with no useful error even when the on-prem path is perfect.
> - **S3 reachability from the private subnet.** Glue reads its scripts from S3 and writes the raw zone to S3. A private subnet has no implicit S3 path. Add an **S3 Gateway VPC endpoint** (free, preferred) on the subnet's route table, or route egress through a **NAT gateway**. Without one, the ingest job fails to start or fails on write even though the on-prem DB is reachable.

---

## Glue Connection — VPC configuration (CDK)

```typescript
import * as glue from 'aws-cdk-lib/aws-glue';

new glue.CfnConnection(this, 'JdbcConnection', {
  catalogId: cdk.Stack.of(this).account,
  connectionInput: {
    name: `${prefix}-jdbc-connection`,
    connectionType: 'JDBC',
    connectionProperties: {
      // The URL encodes host/port/database; credentials come from Secrets Manager via SECRET_ID.
      JDBC_CONNECTION_URL: `jdbc:${engine}://${host}:${port}/${database}`,
      SECRET_ID: secretArn, // Secrets Manager secret holding { username, password }
    },
    physicalConnectionRequirements: {
      availabilityZone: 'ap-northeast-2a',          // must match the subnet's AZ
      subnetId: 'subnet-xxx',                        // PRIVATE subnet with a route to the on-prem CIDR
      securityGroupIdList: ['sg-xxx'],               // SG with (a) self-referencing all-TCP rule and
                                                     //          (b) outbound to the on-prem DB IP:port
    },
  },
});
```

How the pieces connect:
- Glue creates **ENIs in the specified subnet** → their traffic egresses through the VGW/TGW over the VPN/DX to the on-prem DB.
- The **security group** must allow outbound to the on-prem DB `IP:port` *and* carry the self-referencing all-TCP rule above.
- The **subnet's route table** must have a route to the on-prem CIDR (via VGW or TGW) plus an S3 path (gateway endpoint or NAT) — per the prerequisites.
- `availabilityZone` must be the AZ that `subnetId` lives in, or connection creation fails validation.

---

## Connectivity test — run before building the ETL job

```bash
# Validates the connection end-to-end (network + DNS + credentials) without running a full job.
aws glue test-connection \
  --connection-name {prefix}-jdbc-connection \
  --region {region}
# HTTP 200 / Success → proceed to the ingest job.
# Failure → walk back through the prerequisites: SG (self-ref + outbound port),
#   route table (on-prem CIDR + S3 path), DNS (hostname resolves in-subnet),
#   on-prem firewall (inbound from Glue/NAT source), and the secret's credentials.
```

Only once `test-connection` succeeds should you create and run the ingest job (`{prefix}-ingest-jdbc` for Hive, or `{prefix}-ingest-iceberg` for the Iceberg JDBC case).
