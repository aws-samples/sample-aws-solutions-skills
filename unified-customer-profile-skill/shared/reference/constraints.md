# Constraints — Known Limitations & Gotchas

## Manual configuration required at deployment

1. **Connect Instance → Customer Profiles activation**
   - Creating the Connect Instance + CP Domain via CDK is possible
   - But the **association** of the Instance and Domain is manual in the Console
   - Select `Profile creation: "Create inferred profiles only"`
   - Can be automated with a CDK Custom Resource (see associate-connect-domain.ts)

2. **Connect → Data Source Integration (Kinesis mode)**
   - The Kinesis → CP integration is configured in the Console
   - Create an EventBridge Pipe → select that Pipe in the CP integration → specify Object Type Mapping

3. **ER ML Matching Training**
   - Training data needed on the first run (minimum ~100 labeled pairs)
   - Rule-based is usable immediately

## CDK-related constraints

1. **Connect Instance creation order**
   - Creating multiple instances simultaneously fails in the "pending" state
   - Always serialize with `addDependency()`
   ```typescript
   domainStack.addDependency(previousDomainStack);
   ```

2. **CP Domain Custom Resources**
   - Object Type creation is not natively supported by CDK → a Custom Resource Lambda is needed
   - Same for Calculated Attributes
   - References: `custom-resources/upsert-object-type.ts`, `create-calculated-attributes.ts`

3. **ER Schema Mapping**
   - The Glue Table's column names must exactly match the InputSourceConfig of the ER Schema Mapping
   - Watch case sensitivity (all lowercase recommended)

## Entity Resolution constraints

1. **Asynchronous execution**
   - An ER Job is an asynchronous batch. Not real-time matching.
   - StartMatchingJob → GetMatchingJob (polling) → results stored in S3
   - In the demo, a Lambda polls + parses the results

2. **Input format**
   - Glue Table required (S3 direct reference not allowed)
   - Columns: `variantid` (UNIQUE_ID), PII fields, `sourcechannel`
   - Partitioning: not needed (ER scans the entire table)

3. **Rule combination**
   - Multiple Rules possible in one Workflow (OR relationship)
   - Match Keys within a Rule are an AND relationship
   - e.g. NameAndEmail = (Name AND Email match)

## Customer Profiles constraints

1. **PutProfileObject call rules**
   - JSON that exactly matches the Object Type is required
   - Fields defined in Keys must have values
   - The `_profileObjectType` header is required

2. **SearchProfiles limitations**
   - Only key-based search (no free-text search)
   - To list profiles: use ListProfileObjects

3. **Calculated Attributes**
   - Can only aggregate fields of an existing Object Type
   - Cannot be changed after creation → delete and recreate
   - 20 limit (per domain)

## Frontend constraints

1. **Cognito Hosted UI Callback URL**
   - Both localhost:3000 (development) + Amplify URL (production) must be registered
   - Recommended to register both URLs in advance in CDK

2. **CORS**
   - CORS configuration required at API Gateway
   - Consider preflight requests during Cognito token refresh

## Security considerations

1. PII data → KMS encryption + S3 bucket policy
2. API → Cognito token required (no public endpoint)
3. Lambda → placing it inside a VPC increases NAT Gateway cost
4. Neptune → VPC isolation required (no public access)
