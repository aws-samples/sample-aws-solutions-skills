const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const cdk = require('aws-cdk-lib');
const { Template } = require('aws-cdk-lib/assertions');
const generated = path.join(__dirname, '.generated');
fs.mkdirSync(generated, { recursive: true });
fs.cpSync(path.join(__dirname, '../claude-code/skills/data-platform-pipeline/assets/smus'),
  path.join(generated, 'src'), { recursive: true });
const tscPackage = JSON.parse(fs.readFileSync(path.join(__dirname, 'node_modules/typescript/package.json'), 'utf8'));
fs.writeFileSync(path.join(generated, 'tsconfig.json'), JSON.stringify({
  compilerOptions: { target: 'ES2020', module: 'Node16', moduleResolution: 'Node16',
    skipLibCheck: true, outDir: 'compiled', rootDir: 'src' },
  include: ['src/*.ts'],
}));
const compile = spawnSync(process.execPath, [path.join(__dirname, 'node_modules/typescript', tscPackage.bin.tsc),
  '--project', path.join(generated, 'tsconfig.json')], { encoding: 'utf8' });
assert.equal(compile.status, 0, compile.stdout + compile.stderr);
const { SmusDomainStack, SmusProjectStack } = require('./.generated/compiled/smus-foundation.js');
const { SmusTableGrantsStack } = require('./.generated/compiled/smus-table-grants.js');
const env = { account: '111122223333', region: 'us-east-1' };
const domainProps = {
  env, prefix: 'acme', customerSelectedSmus: true, idcRegion: env.region,
  idcInstanceArn: 'arn:aws:sso:::instance/ssoins-1234567890abcdef',
};
const projectProps = {
  env, prefix: 'acme', customerSelectedSmus: true, toolingGovernanceReviewed: true,
  createToolingConfiguration: true, createBlueprintUseGrant: true,
  domainId: 'dzd-discovered', domainArn: `arn:aws:datazone:${env.region}:${env.account}:domain/dzd-discovered`,
  rootDomainUnitId: 'root-discovered', toolingBlueprintId: 'blueprint-discovered',
  ownerUserId: 'user-discovered', deploymentGroupId: 'deployment-discovered',
  vpcId: 'vpc-example', privateSubnetIds: ['subnet-one', 'subnet-two'],
};
const grantProps = {
  env, customerSelectedSmus: true, projectRoleArn: `arn:aws:iam::${env.account}:role/project-role`,
  tableBucketName: 'acme-table-bucket', namespace: 'acme_db', tables: ['orders_clean', 'order_mart'],
};

test('new IdC domain retains its data identity and correct service policies', () => {
  const stack = new SmusDomainStack(new cdk.App(), 'Domain', domainProps);
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::DataZone::Domain', {
    DomainVersion: 'V2', SingleSignOn: { Type: 'IAM_IDC',
      IdcInstanceArn: domainProps.idcInstanceArn, UserAssignment: 'MANUAL' },
  });
  const raw = template.toJSON();
  const domain = Object.values(raw.Resources).find(r => r.Type === 'AWS::DataZone::Domain');
  assert.equal(domain.DeletionPolicy, 'Retain');
  assert.match(JSON.stringify(raw), /service-role\/SageMakerStudioDomainExecutionRolePolicy/);
  assert.match(JSON.stringify(raw), /service-role\/SageMakerStudioDomainServiceRolePolicy/);
  template.resourceCountIs('AWS::LakeFormation::DataLakeSettings', 0);
  template.resourceCountIs('AWS::DataZone::Project', 0); // Identity discovery is a separate phase.
});

test('project preserves deployer ownership and orders blueprint grants before creation', () => {
  const stack = new SmusProjectStack(new cdk.App(), 'Project', projectProps);
  const template = Template.fromStack(stack);
  const raw = template.toJSON();
  const entry = Object.entries(raw.Resources).find(([, r]) => r.Type === 'AWS::DataZone::Project');
  const project = entry[1];
  assert.deepEqual(project.Properties.MembershipAssignments.map(x => x.Member), [
    { UserIdentifier: 'user-discovered' }, { GroupIdentifier: 'deployment-discovered' },
  ]);
  const grantId = Object.entries(raw.Resources).find(([, r]) => r.Type === 'AWS::DataZone::PolicyGrant')[0];
  assert.ok(project.DependsOn.includes(grantId));
  const blueprint = Object.values(raw.Resources).find(r => r.Type === 'AWS::DataZone::EnvironmentBlueprintConfiguration');
  assert.ok(JSON.stringify(blueprint.Properties.RegionalParameters).includes('s3://'));
  const bucket = Object.values(raw.Resources).find(r => r.Type === 'AWS::S3::Bucket');
  assert.ok(bucket.Properties.BucketName.startsWith('amazon-sagemaker-'));
  assert.ok(bucket.Properties.BucketName.length <= 63);
  assert.deepEqual(Object.values(bucket.Properties.PublicAccessBlockConfiguration), [true, true, true, true]);
  assert.equal(bucket.DeletionPolicy, 'Retain');
  template.resourceCountIs('AWS::LakeFormation::DataLakeSettings', 0);
  template.resourceCountIs('AWS::DataZone::Domain', 0); // Supports existing domain/new project.
  const profile = Object.values(raw.Resources).find(r => r.Type === 'AWS::DataZone::ProjectProfile');
  const flags = profile.Properties.EnvironmentConfigurations[0].ConfigurationParameters.ParameterOverrides;
  assert.equal(flags.find(x => x.Name === 'enableSpaces').Value, 'false');
  assert.equal(flags.find(x => x.Name === 'enableSageMakerMLWorkloadsPermissions').Value, 'false');
});

test('named-table LF permissions have no wildcard or grant option', () => {
  const stack = new SmusTableGrantsStack(new cdk.App(), 'Grants', grantProps);
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::LakeFormation::PrincipalPermissions', 3);
  const raw = template.toJSON();
  for (const resource of Object.values(raw.Resources)) {
    assert.deepEqual(resource.Properties.PermissionsWithGrantOption, []);
    assert.ok(!JSON.stringify(resource).includes('TableWildcard'));
    assert.ok(!resource.Properties.Permissions.includes('ALL'));
  }
  assert.match(JSON.stringify(raw), /111122223333:s3tablescatalog\/acme-table-bucket/);
  template.resourceCountIs('AWS::LakeFormation::DataLakeSettings', 0);
});

test('existing-domain project reuses shared Tooling configuration and permissions', () => {
  const stack = new SmusProjectStack(new cdk.App(), 'NewProject', {
    ...projectProps, createToolingConfiguration: false, createBlueprintUseGrant: false,
    vpcId: undefined, privateSubnetIds: undefined,
  });
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::DataZone::Project', 1);
  template.resourceCountIs('AWS::DataZone::ProjectProfile', 1);
  for (const type of ['AWS::DataZone::Domain', 'AWS::DataZone::EnvironmentBlueprintConfiguration',
    'AWS::DataZone::PolicyGrant', 'AWS::IAM::Role', 'AWS::S3::Bucket',
    'AWS::LakeFormation::DataLakeSettings']) {
    template.resourceCountIs(type, 0);
  }
});

test('standalone generated branch contains pipeline resources and zero SMUS resources', () => {
  const app = new cdk.App();
  const choice = false; // Explicit customer's standalone answer.
  const stack = new cdk.Stack(app, 'Pipeline', { env });
  new cdk.aws_s3tables.CfnTableBucket(stack, 'Tables', { tableBucketName: 'acme-table-bucket' });
  new cdk.aws_glue.CfnJob(stack, 'Etl', {
    name: 'acme-etl', role: `arn:aws:iam::${env.account}:role/etl`,
    command: { name: 'glueetl', scriptLocation: 's3://acme/scripts/etl.py' },
  });
  if (choice) new SmusDomainStack(app, 'Smus', domainProps);
  const assembly = app.synth();
  assert.equal(assembly.stacks.length, 1);
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::S3Tables::TableBucket', 1);
  template.resourceCountIs('AWS::Glue::Job', 1);
  assert.ok(!JSON.stringify(template.toJSON()).includes('AmazonDataZone'));
  assert.ok(!JSON.stringify(template.toJSON()).includes('AWS::DataZone'));
});

test('constructs reject unselected SMUS, unresolved IdC region and governance review', () => {
  assert.throws(() => new SmusDomainStack(new cdk.App(), 'A',
    { ...domainProps, customerSelectedSmus: false }), /explicit customer choice/);
  assert.throws(() => new SmusDomainStack(new cdk.App(), 'B',
    { ...domainProps, idcRegion: 'eu-west-1' }), /IdC region/);
  assert.throws(() => new SmusProjectStack(new cdk.App(), 'C',
    { ...projectProps, toolingGovernanceReviewed: false }), /governance review/);
  assert.throws(() => new SmusTableGrantsStack(new cdk.App(), 'D',
    { ...grantProps, customerSelectedSmus: false }), /Explicit SMUS choice/);
  assert.throws(() => new SmusProjectStack(new cdk.App(), 'E',
    { ...projectProps, toolingGovernanceReviewed: 'false' }), /governance review/);
  assert.throws(() => new SmusTableGrantsStack(new cdk.App(), 'F',
    { ...grantProps, tables: ['orders_clean', 'orders_clean'] }), /unique/);
  assert.throws(() => new SmusProjectStack(new cdk.App(), 'G',
    { ...projectProps, domainArn: projectProps.domainArn.replace(env.account, '999988887777') }),
  /target account/);
});

test('stable input produces identical templates on repeat synthesis', () => {
  const first = Template.fromStack(new SmusProjectStack(new cdk.App(), 'Project', projectProps)).toJSON();
  const second = Template.fromStack(new SmusProjectStack(new cdk.App(), 'Project', projectProps)).toJSON();
  assert.deepEqual(first, second);
});
