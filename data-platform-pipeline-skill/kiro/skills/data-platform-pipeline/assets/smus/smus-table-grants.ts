import * as cdk from 'aws-cdk-lib';
import { aws_lakeformation as lf } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface SmusTableGrantsProps extends cdk.StackProps {
  customerSelectedSmus: boolean;
  projectRoleArn: string;
  tableBucketName: string;
  namespace: string;
  tables: string[];
}

// Deploy using an already authorized LF grant administrator. This stack does
// not make the deployment role an administrator or alter global LF defaults.
export class SmusTableGrantsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SmusTableGrantsProps) {
    super(scope, id, props);
    if (props.customerSelectedSmus !== true || !props.tables.length) {
      throw new Error('Explicit SMUS choice and named tables are required');
    }
    if (!props.env?.account || !props.env.region
      || !props.projectRoleArn.includes(`:iam::${props.env.account}:role/`)) {
      throw new Error('Use a project role in the selected account');
    }
    if (new Set(props.tables).size !== props.tables.length
      || props.tables.some(name => !/^[a-z0-9_]+$/.test(name))
      || !/^[a-z0-9_]+$/.test(props.namespace)
      || !/^[a-z0-9][a-z0-9-]*$/.test(props.tableBucketName)) {
      throw new Error('Use unique, explicit S3 Tables names; wildcards are not permitted');
    }
    const catalogId = `${this.account}:s3tablescatalog/${props.tableBucketName}`;
    const principal = { dataLakePrincipalIdentifier: props.projectRoleArn };
    new lf.CfnPrincipalPermissions(this, 'DescribeNamespace', {
      principal, resource: { database: { catalogId, name: props.namespace } },
      permissions: ['DESCRIBE'], permissionsWithGrantOption: [],
    });
    for (const [index, name] of props.tables.entries()) {
      new lf.CfnPrincipalPermissions(this, `ReadTable${index}`, {
        principal, resource: { table: { catalogId, databaseName: props.namespace, name } },
        permissions: ['DESCRIBE', 'SELECT'], permissionsWithGrantOption: [],
      });
    }
  }
}
