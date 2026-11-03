import * as cdk from 'aws-cdk-lib';
import { aws_datazone as dz, aws_iam as iam, aws_s3 as s3 } from 'aws-cdk-lib';
import { Construct } from 'constructs';

// Copy into the generated app only for the customer's SMUS/new-domain path.
// Resolve these IDs from AWS; sample account IDs must never become defaults.
export interface SmusDomainProps extends cdk.StackProps {
  prefix: string;
  customerSelectedSmus: boolean;
  idcInstanceArn: string;
  idcRegion: string;
}

export interface SmusProjectProps extends cdk.StackProps {
  prefix: string;
  customerSelectedSmus: boolean;
  domainId: string;
  domainArn: string;
  rootDomainUnitId: string;
  toolingBlueprintId: string;
  ownerUserId: string;
  deploymentGroupId: string;
  operatorGroupId?: string;
  createToolingConfiguration: boolean;
  createBlueprintUseGrant: boolean;
  vpcId?: string;
  privateSubnetIds?: string[];
  // Tooling can alter account-level LF settings. Require the platform owner's
  // review before this stack is deployed; never silently bootstrap LF admins.
  toolingGovernanceReviewed: boolean;
}

export class SmusDomainStack extends cdk.Stack {
  readonly domain: dz.CfnDomain;
  constructor(scope: Construct, id: string, props: SmusDomainProps) {
    super(scope, id, props);
    if (props.customerSelectedSmus !== true) {
      throw new Error('SMUS requires an explicit customer choice');
    }
    if (!props.env?.account || !props.env.region || props.idcRegion !== props.env.region) {
      throw new Error('This new-domain pattern requires the discovered IdC region; do not move IdC');
    }
    if (!/^[a-z][a-z0-9-]{0,19}$/.test(props.prefix) || !props.idcInstanceArn) {
      throw new Error('Use a 1-20 character prefix and a discovered IdC instance ARN');
    }
    cdk.Tags.of(this).add('ManagedBy', 'data-platform-pipeline-skill');
    const servicePrincipal = new iam.ServicePrincipal('datazone.amazonaws.com', {
      conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
    });
    const executionRole = new iam.Role(this, 'DomainExecution', {
      assumedBy: servicePrincipal,
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(
        'service-role/SageMakerStudioDomainExecutionRolePolicy')],
    });
    executionRole.assumeRolePolicy!.addStatements(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW, actions: ['sts:TagSession', 'sts:SetContext'],
      principals: [new iam.ServicePrincipal('datazone.amazonaws.com')],
      conditions: { StringEquals: { 'aws:SourceAccount': this.account },
        'ForAllValues:StringLike': { 'aws:TagKeys': 'datazone*' } },
    }));
    const serviceRole = new iam.Role(this, 'DomainService', {
      assumedBy: servicePrincipal,
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(
        'service-role/SageMakerStudioDomainServiceRolePolicy')],
    });
    this.domain = new dz.CfnDomain(this, 'Domain', {
      name: `${props.prefix}-unified-studio`, domainVersion: 'V2',
      domainExecutionRole: executionRole.roleArn, serviceRole: serviceRole.roleArn,
      singleSignOn: { type: 'IAM_IDC', idcInstanceArn: props.idcInstanceArn,
        userAssignment: 'MANUAL' },
    });
    this.domain.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    new cdk.CfnOutput(this, 'DomainId', { value: this.domain.attrId });
    new cdk.CfnOutput(this, 'DomainArn', { value: this.domain.attrArn });
    new cdk.CfnOutput(this, 'RootDomainUnitId', { value: this.domain.attrRootDomainUnitId });
    new cdk.CfnOutput(this, 'PortalUrl', { value: this.domain.attrPortalUrl });
  }
}

// Deploy after domain creation and identity profile discovery. Keeping this
// separate allows recovery without recreating the retained domain or data.
export class SmusProjectStack extends cdk.Stack {
  readonly project: dz.CfnProject;
  constructor(scope: Construct, id: string, props: SmusProjectProps) {
    super(scope, id, props);
    if (props.customerSelectedSmus !== true || props.toolingGovernanceReviewed !== true) {
      throw new Error('Explicit SMUS selection and Tooling governance review are required');
    }
    if (!/^[a-z][a-z0-9-]{0,19}$/.test(props.prefix)) {
      throw new Error('Use a 1-20 character prefix for this project template');
    }
    if (typeof props.createToolingConfiguration !== 'boolean'
      || typeof props.createBlueprintUseGrant !== 'boolean') {
      throw new Error('Explicitly choose whether existing Tooling configuration and grants are reused');
    }
    if (props.createToolingConfiguration && (!props.vpcId
      || !Array.isArray(props.privateSubnetIds) || props.privateSubnetIds.length < 2)) {
      throw new Error('New Tooling configuration requires at least two approved private subnets');
    }
    for (const value of [props.domainId, props.domainArn, props.rootDomainUnitId,
      props.toolingBlueprintId, props.ownerUserId, props.deploymentGroupId]) {
      if (!value) throw new Error('Resolve domain, identity, blueprint and network prerequisites first');
    }
    if (!props.env?.account || !props.env.region
      || !props.domainArn.includes(`:datazone:${props.env.region}:${props.env.account}:domain/${props.domainId}`)) {
      throw new Error('The selected domain must match the target account and region');
    }
    cdk.Tags.of(this).add('ManagedBy', 'data-platform-pipeline-skill');
    let assets: s3.Bucket | undefined;
    let tooling: dz.CfnEnvironmentBlueprintConfiguration | undefined;
    if (props.createToolingConfiguration) {
      // AWS-managed Tooling permissions expect an amazon-sagemaker-* project bucket.
      assets = new s3.Bucket(this, 'ProjectAssets', {
        bucketName: `amazon-sagemaker-${props.prefix}-${this.account}-${this.region.replace(/-/g, '')}`,
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        enforceSSL: true, versioned: true, removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      const projectPrincipal = new iam.ServicePrincipal('datazone.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account },
          ArnEquals: { 'aws:SourceArn': props.domainArn } },
      });
      const provision = new iam.Role(this, 'Provisioning', {
        assumedBy: projectPrincipal,
        managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/SageMakerStudioProjectProvisioningRolePolicy')],
      });
      const manage = new iam.Role(this, 'ManageAccess', {
        assumedBy: projectPrincipal,
        managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonDataZoneGlueManageAccessRolePolicy')],
      });
      tooling = new dz.CfnEnvironmentBlueprintConfiguration(this, 'Tooling', {
        domainIdentifier: props.domainId, environmentBlueprintIdentifier: 'Tooling',
        enabledRegions: [this.region], provisioningRoleArn: provision.roleArn,
        manageAccessRoleArn: manage.roleArn,
        regionalParameters: [{ region: this.region, parameters: {
          S3Location: `s3://${assets.bucketName}`, VpcId: props.vpcId!,
          Subnets: props.privateSubnetIds!.join(','),
        } }],
      });
    }
    const profile = new dz.CfnProjectProfile(this, 'Profile', {
      name: `${props.prefix}-sql-etl`, domainIdentifier: props.domainId,
      domainUnitIdentifier: props.rootDomainUnitId, status: 'ENABLED',
      environmentConfigurations: [{
        name: 'Tooling', environmentBlueprintId: props.toolingBlueprintId,
        deploymentMode: 'ON_CREATE', deploymentOrder: 0,
        awsAccount: { awsAccountId: this.account }, awsRegion: { regionName: this.region },
        configurationParameters: { parameterOverrides: [
          { name: 'enableSpaces', value: 'false', isEditable: false },
          { name: 'enableAmazonBedrockPermissions', value: 'false', isEditable: false },
          { name: 'enableSageMakerMLWorkloadsPermissions', value: 'false', isEditable: false },
          { name: 'enableGlueWorkloadsPermissions', value: 'true', isEditable: false },
          { name: 'enableAthena', value: 'true', isEditable: false },
          { name: 'workgroupName', value: `${props.prefix}-project`, isEditable: false },
          { name: 'logGroupRetention', value: '14', isEditable: false },
          { name: 'enableS3AccessGrantsForTools', value: 'false', isEditable: false },
        ] },
      }],
    });
    if (tooling) profile.addResourceDependency(tooling);
    let grant: dz.CfnPolicyGrant | undefined;
    if (props.createBlueprintUseGrant) {
      grant = new dz.CfnPolicyGrant(this, 'UseToolingBlueprint', {
        domainIdentifier: props.domainId, entityType: 'ENVIRONMENT_BLUEPRINT_CONFIGURATION',
        entityIdentifier: `${this.account}:${props.toolingBlueprintId}`,
        policyType: 'CREATE_ENVIRONMENT_FROM_BLUEPRINT',
        detail: { createEnvironmentFromBlueprint: {} },
        principal: { project: { projectDesignation: 'CONTRIBUTOR',
          projectGrantFilter: { domainUnitFilter: {
            domainUnit: props.rootDomainUnitId, includeChildDomainUnits: false,
          } } } },
      });
      if (tooling) grant.addResourceDependency(tooling);
    }
    const members: dz.CfnProject.ProjectMembershipAssignmentProperty[] = [{
      designation: 'PROJECT_OWNER', member: { userIdentifier: props.ownerUserId },
    }, {
      designation: 'PROJECT_OWNER', member: { groupIdentifier: props.deploymentGroupId },
    }];
    if (props.operatorGroupId && props.operatorGroupId !== props.deploymentGroupId) {
      members.push({ designation: 'PROJECT_OWNER',
        member: { groupIdentifier: props.operatorGroupId } });
    }
    this.project = new dz.CfnProject(this, 'Project', {
      domainIdentifier: props.domainId, name: `${props.prefix}-pipeline`,
      projectProfileId: profile.attrId, membershipAssignments: members,
    });
    this.project.addResourceDependency(profile);
    if (grant) this.project.addResourceDependency(grant);
    new cdk.CfnOutput(this, 'DomainId', { value: props.domainId });
    new cdk.CfnOutput(this, 'ProjectId', { value: this.project.attrId });
    if (assets) new cdk.CfnOutput(this, 'ProjectAssetBucket', { value: assets.bucketName });
  }
}
