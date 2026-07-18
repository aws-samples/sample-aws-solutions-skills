#!/usr/bin/env bash
#
# launch-ec2-host.sh — Provision an EC2 "deploy host" for the llm-gateway-governance skill.
#
# Creates (idempotently, tagged llmgw-deploy-host):
#   - an IAM role + instance profile (SSM core + deploy policy — AdministratorAccess by
#     default for sandbox accounts; override with --policy-arn or reuse via --instance-profile)
#   - a security group with NO inbound rules (access is SSM Session Manager only)
#   - an Amazon Linux 2023 instance (Graviton/ARM64 by default → native ARM64 Docker
#     builds, no QEMU), IMDSv2 required, 50 GB gp3 root volume
#   - user-data = sibling bootstrap.sh (Docker/Node/CDK/AWS CLI/jq/Claude Code)
#
# Usage (AWS CloudShell is the ideal place to run this — CLI v2, SSM plugin,
# and your console credentials are all preinstalled):
#   ./launch-ec2-host.sh launch    [--region ap-northeast-2] [--instance-type t4g.xlarge]
#                                  [--subnet-id subnet-xxx] [--volume-gb 50]
#                                  [--instance-profile NAME] [--policy-arn ARN]
#   ./launch-ec2-host.sh status    [--region ...]
#   ./launch-ec2-host.sh terminate [--region ...] [--purge-iam]
#
# Requires: AWS CLI v2 with credentials able to create EC2 + IAM resources.
set -euo pipefail

NAME="llmgw-deploy-host"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
INSTANCE_TYPE="t4g.xlarge"          # 4 vCPU / 16 GiB Graviton; t4g.large (8 GiB) is the floor
VOLUME_GB=50
SUBNET_ID=""
INSTANCE_PROFILE=""                 # reuse an existing instance profile instead of creating one
POLICY_ARN="arn:aws:iam::aws:policy/AdministratorAccess"
PURGE_IAM=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP="$SCRIPT_DIR/bootstrap.sh"

CMD="${1:-}"; shift || true
case "$CMD" in launch|status|terminate) ;; *)
  awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 1 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --region)           REGION="$2"; shift 2 ;;
    --instance-type)    INSTANCE_TYPE="$2"; shift 2 ;;
    --subnet-id)        SUBNET_ID="$2"; shift 2 ;;
    --volume-gb)        VOLUME_GB="$2"; shift 2 ;;
    --instance-profile) INSTANCE_PROFILE="$2"; shift 2 ;;
    --policy-arn)       POLICY_ARN="$2"; shift 2 ;;
    --purge-iam)        PURGE_IAM=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

[ -n "$REGION" ] || { echo "Set --region (or AWS_REGION)" >&2; exit 1; }
AWS=(aws --region "$REGION")

find_instance() {
  "${AWS[@]}" ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

# ---------------------------------------------------------------- status ----
if [ "$CMD" = status ]; then
  IID="$(find_instance)"
  if [ -z "$IID" ] || [ "$IID" = "None" ]; then echo "No $NAME instance in $REGION."; exit 0; fi
  "${AWS[@]}" ec2 describe-instances --instance-ids $IID \
    --query 'Reservations[].Instances[].{Id:InstanceId,State:State.Name,Type:InstanceType,Az:Placement.AvailabilityZone,LaunchTime:LaunchTime}' \
    --output table
  echo "Connect: aws ssm start-session --region $REGION --target ${IID%%$'\t'*}"
  exit 0
fi

# ------------------------------------------------------------- terminate ----
if [ "$CMD" = terminate ]; then
  IID="$(find_instance)"
  if [ -n "$IID" ] && [ "$IID" != "None" ]; then
    echo "Terminating $IID ..."
    "${AWS[@]}" ec2 terminate-instances --instance-ids $IID >/dev/null
    "${AWS[@]}" ec2 wait instance-terminated --instance-ids $IID
  else
    echo "No instance to terminate."
  fi
  SG_ID="$("${AWS[@]}" ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
            --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
    echo "Deleting security group $SG_ID ..."
    "${AWS[@]}" ec2 delete-security-group --group-id "$SG_ID" || echo "  (leave it — retry later if still attached)"
  fi
  if [ "$PURGE_IAM" = 1 ]; then
    echo "Purging IAM role/profile $NAME ..."
    aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" 2>/dev/null || true
    aws iam delete-instance-profile --instance-profile-name "$NAME" 2>/dev/null || true
    for arn in $(aws iam list-attached-role-policies --role-name "$NAME" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      aws iam detach-role-policy --role-name "$NAME" --policy-arn "$arn"
    done
    aws iam delete-role --role-name "$NAME" 2>/dev/null || true
  fi
  echo "Done."
  exit 0
fi

# ---------------------------------------------------------------- launch ----
[ -f "$BOOTSTRAP" ] || { echo "bootstrap.sh not found next to this script" >&2; exit 1; }

EXISTING="$(find_instance)"
if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "An instance tagged $NAME already exists in $REGION: $EXISTING"
  echo "Use '$0 status' or '$0 terminate' first."
  exit 1
fi

# IAM role + instance profile
if [ -z "$INSTANCE_PROFILE" ]; then
  INSTANCE_PROFILE="$NAME"
  if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    echo "Creating IAM role $NAME (deploy policy: $POLICY_ARN)"
    [ "$POLICY_ARN" = "arn:aws:iam::aws:policy/AdministratorAccess" ] && \
      echo "⚠️  AdministratorAccess is the sandbox-account default — pass --policy-arn to scope it down on shared accounts."
    aws iam create-role --role-name "$NAME" --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }' >/dev/null
    aws iam attach-role-policy --role-name "$NAME" --policy-arn "$POLICY_ARN"
    aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  fi
  # Bedrock invoke policy — lets Claude Code on the host run against Bedrock via the
  # instance role (CLAUDE_CODE_USE_BEDROCK=1, wired by bootstrap.sh), no API key needed.
  # Idempotent; also covers the --policy-arn scoped-deploy-role case.
  aws iam put-role-policy --role-name "$NAME" --policy-name bedrock-invoke --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }]
  }'
  if ! aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
    aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
    aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
    echo "Waiting for instance-profile propagation ..."; sleep 12
  fi
fi

# AMI: latest AL2023, arch matched to instance type (t4g/m7g/c7g/... => arm64)
case "$INSTANCE_TYPE" in
  a1*|t4g*|m6g*|m7g*|m8g*|c6g*|c7g*|c8g*|r6g*|r7g*|r8g*|x2g*|im4g*|is4g*) ARCH=arm64 ;;
  *) ARCH=x86_64 ;;
esac
AMI_ID="$("${AWS[@]}" ssm get-parameter \
  --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-$ARCH" \
  --query 'Parameter.Value' --output text)"
echo "AMI: $AMI_ID ($ARCH)"
[ "$ARCH" = x86_64 ] && echo "⚠️  x86_64 instance — ARM64 image builds will run under QEMU (bootstrap installs binfmt). Prefer a Graviton type (t4g/m7g)."

# Subnet: given, or a default-VPC subnet
if [ -z "$SUBNET_ID" ]; then
  SUBNET_ID="$("${AWS[@]}" ec2 describe-subnets --filters Name=default-for-az,Values=true \
    --query 'Subnets[0].SubnetId' --output text)"
  [ "$SUBNET_ID" != "None" ] || { echo "No default VPC — pass --subnet-id (needs outbound internet)" >&2; exit 1; }
fi
VPC_ID="$("${AWS[@]}" ec2 describe-subnets --subnet-ids "$SUBNET_ID" --query 'Subnets[0].VpcId' --output text)"

# Security group: no inbound at all (SSM only)
SG_ID="$("${AWS[@]}" ec2 describe-security-groups \
  --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
  SG_ID="$("${AWS[@]}" ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC_ID" \
    --description "llm-gateway deploy host - no inbound, SSM only" --query GroupId --output text)"
fi
echo "Security group: $SG_ID (no inbound rules)"

echo "Launching $INSTANCE_TYPE in $SUBNET_ID ..."
IID="$("${AWS[@]}" ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$INSTANCE_PROFILE" \
  --user-data "file://$BOOTSTRAP" \
  --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2' \
  --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$VOLUME_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=purpose,Value=llm-gateway-governance-skill}]" \
  --query 'Instances[0].InstanceId' --output text)"

echo "Instance: $IID — waiting for running state ..."
"${AWS[@]}" ec2 wait instance-running --instance-ids "$IID"

cat <<EOF

✅ Deploy host launched: $IID  (region $REGION)

Bootstrap (user-data) takes ~3-5 minutes. Then connect — no SSH key, no open ports:

  aws ssm start-session --region $REGION --target $IID

On first connect:
  sudo tail -f /var/log/cloud-init-output.log     # watch bootstrap finish
  sudo su - ec2-user                              # work as ec2-user (docker group applied)
  ./start-llmgw.sh                                # clone skill + preflight + Claude Code in tmux

Claude Code is pre-wired to Bedrock via the instance role (no API key). One manual step
remains: enable Anthropic model access in the Bedrock console for this region.

Next steps: shared/reference/ec2-deploy-host.md
Idle cost control:  aws ec2 stop-instances --region $REGION --instance-ids $IID
Cleanup:            $0 terminate --region $REGION [--purge-iam]
EOF
