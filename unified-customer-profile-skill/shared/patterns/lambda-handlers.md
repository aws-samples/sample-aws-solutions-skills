# Lambda Handler Patterns

Reusable Lambda handler patterns. Generate by changing only the fields/logic per domain.

## Common structure

The basic skeleton of every handler:

```typescript
import { APIGatewayProxyEvent, APIGatewayProxyResult } from 'aws-lambda';

const HEADERS = {
  'Content-Type': 'application/json',
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Content-Type,Authorization',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
};

function ok(body: unknown): APIGatewayProxyResult {
  return { statusCode: 200, headers: HEADERS, body: JSON.stringify(body) };
}

function error(statusCode: number, message: string): APIGatewayProxyResult {
  return { statusCode, headers: HEADERS, body: JSON.stringify({ error: message }) };
}

export async function handler(event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> {
  try {
    const path = event.path;
    const method = event.httpMethod;

    if (method === 'OPTIONS') return ok({});

    // Route to sub-handlers
    // ...

    return error(404, 'Not found');
  } catch (err: any) {
    console.error('Handler error:', err);
    return error(500, err.message || 'Internal server error');
  }
}
```

## Matching Handler

Run Entity Resolution workflows + retrieve results

```typescript
import { EntityResolutionClient, StartMatchingJobCommand, GetMatchingJobCommand, ListMatchingJobsCommand } from '@aws-sdk/client-entityresolution';
import { DynamoDBDocumentClient, PutCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { S3Client, GetObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';

const erClient = new EntityResolutionClient({});
const ddbClient = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const s3Client = new S3Client({});

const WORKFLOW_NAME = process.env.WORKFLOW_NAME!;
const DATA_BUCKET = process.env.DATA_BUCKET!;
const RESULTS_TABLE = process.env.RESULTS_TABLE!;
const GLUE_DB_NAME = process.env.GLUE_DB_NAME!;
const GLUE_TABLE_NAME = process.env.GLUE_TABLE_NAME!;

// POST /api/matching/run — run matching
async function runMatching(body: { matchingType: 'simple' | 'advanced' | 'ml' }) {
  const workflowName = `${WORKFLOW_NAME}-${body.matchingType}`;

  // 1. Start ER Job
  const { jobId } = await erClient.send(new StartMatchingJobCommand({
    workflowName,
  }));

  // 2. Polling (in practice, Step Functions or EventBridge is recommended)
  let status = 'RUNNING';
  while (status === 'RUNNING') {
    await new Promise(r => setTimeout(r, 5000));
    const job = await erClient.send(new GetMatchingJobCommand({
      workflowName,
      jobId: jobId!,
    }));
    status = job.status || 'UNKNOWN';
  }

  // 3. Parse results (S3 output)
  const results = await parseErOutput(workflowName, jobId!);

  // 4. Save to DynamoDB
  for (const result of results) {
    await ddbClient.send(new PutCommand({
      TableName: RESULTS_TABLE,
      Item: {
        pk: `MATCH#${body.matchingType}`,
        sk: `${result.matchId}#${result.variantId}`,
        ...result,
        timestamp: new Date().toISOString(),
      },
    }));
  }

  return { jobId, matchCount: results.length, matchingType: body.matchingType };
}

// GET /api/matching/results — retrieve results
async function getResults(matchingType: string) {
  const { Items } = await ddbClient.send(new QueryCommand({
    TableName: RESULTS_TABLE,
    KeyConditionExpression: 'pk = :pk',
    ExpressionAttributeValues: { ':pk': `MATCH#${matchingType}` },
  }));
  return Items || [];
}

// Parse S3 ER output
async function parseErOutput(workflowName: string, jobId: string) {
  // ER output format: s3://{bucket}/er-output/{workflowName}/{jobId}/
  // JSON Lines: { matchId, variantId, confidenceScore? }
  // ... S3 list + get + parse
  return [];
}
```

## Ingestion Handler (Multi-Mode)

CSV/Parquet upload + Glue Crawler trigger

```typescript
import { S3Client, PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
import { GlueClient, StartCrawlerCommand, GetCrawlerCommand } from '@aws-sdk/client-glue';
import { parse } from 'csv-parse/sync';

const s3Client = new S3Client({});
const glueClient = new GlueClient({});
const DATA_BUCKET = process.env.DATA_BUCKET!;
const GLUE_DB_NAME = process.env.GLUE_DB_NAME!;
const CRAWLER_NAME = process.env.CRAWLER_NAME; // optional (glue_connection mode)

// POST /api/ingestion/upload-csv — CSV upload + validation
async function uploadCsv(body: { fileName: string; content: string; channel: string }) {
  const records = parse(body.content, { columns: true, skip_empty_lines: true });

  // Validate required fields
  for (const record of records) {
    if (!record.variantid) return error(400, 'variantid is required');
    if (!record.sourcechannel) record.sourcechannel = body.channel;
  }

  // Save to S3 as CSV
  const key = `er-input/${body.channel}/${body.fileName}`;
  const csvContent = [
    Object.keys(records[0]).join(','),
    ...records.map((r: any) => Object.values(r).map(v => `"${v}"`).join(',')),
  ].join('\n');

  await s3Client.send(new PutObjectCommand({
    Bucket: DATA_BUCKET, Key: key, Body: csvContent, ContentType: 'text/csv',
  }));

  return { recordCount: records.length, format: 'csv', path: key };
}

// POST /api/ingestion/upload-parquet — Parquet upload (binary)
async function uploadParquet(body: { fileName: string; channel: string }, fileBuffer: Buffer) {
  // Parquet has an embedded schema, so no separate parsing is needed
  // If the Glue Table is configured with the Parquet SerDe, it can be referenced directly
  const key = `er-input/${body.channel}/${body.fileName}`;

  await s3Client.send(new PutObjectCommand({
    Bucket: DATA_BUCKET,
    Key: key,
    Body: fileBuffer,
    ContentType: 'application/x-parquet',
  }));

  // After Parquet upload, refresh Glue Table partitions (optional)
  // → MSCK REPAIR TABLE or re-run the Crawler

  return { format: 'parquet', path: key, message: 'Schema auto-detected from Parquet metadata' };
}

// POST /api/ingestion/crawl — run Glue Crawler (pull from DB source)
async function triggerCrawler() {
  if (!CRAWLER_NAME) return error(400, 'Glue Crawler not configured (ingestion mode != glue_connection)');

  await glueClient.send(new StartCrawlerCommand({ Name: CRAWLER_NAME }));

  // Polling (or receive a completion event via EventBridge)
  let status = 'RUNNING';
  while (status === 'RUNNING') {
    await new Promise(r => setTimeout(r, 10000));
    const { Crawler } = await glueClient.send(new GetCrawlerCommand({ Name: CRAWLER_NAME }));
    status = Crawler?.State || 'UNKNOWN';
    if (status === 'READY') break; // done
  }

  return { crawlerName: CRAWLER_NAME, status: 'COMPLETED' };
}

// GET /api/ingestion/status — current data status
async function getIngestionStatus() {
  // S3 file list (er-input/ prefix)
  // Glue Table partition info
  // Last Crawler run time
  return {
    totalFiles: 0,
    totalRecords: 0,
    lastCrawlTime: null,
    formats: ['csv', 'parquet'], // currently existing formats
  };
}
```

## Accuracy Handler

Matching accuracy evaluation (Precision / Recall / F1)

```typescript
const ACCURACY_TABLE = process.env.ACCURACY_TABLE!;

interface AccuracyMetrics {
  matchingType: string;
  totalPairs: number;
  truePositives: number;
  falsePositives: number;
  falseNegatives: number;
  precision: number;
  recall: number;
  f1Score: number;
}

// POST /api/accuracy/evaluate — compute accuracy
async function evaluateAccuracy(body: {
  matchingType: string;
  samplePairs: Array<{ id1: string; id2: string; expectedMatch: boolean }>;
}) {
  const results = await getResults(body.matchingType);
  const matchedPairs = buildMatchedPairSet(results);

  let tp = 0, fp = 0, fn = 0;
  for (const pair of body.samplePairs) {
    const actuallyMatched = matchedPairs.has(`${pair.id1}|${pair.id2}`);
    if (pair.expectedMatch && actuallyMatched) tp++;
    else if (!pair.expectedMatch && actuallyMatched) fp++;
    else if (pair.expectedMatch && !actuallyMatched) fn++;
  }

  const precision = tp / (tp + fp) || 0;
  const recall = tp / (tp + fn) || 0;
  const f1Score = 2 * (precision * recall) / (precision + recall) || 0;

  const metrics: AccuracyMetrics = {
    matchingType: body.matchingType,
    totalPairs: body.samplePairs.length,
    truePositives: tp, falsePositives: fp, falseNegatives: fn,
    precision, recall, f1Score,
  };

  await ddbClient.send(new PutCommand({
    TableName: ACCURACY_TABLE,
    Item: { pk: 'ACCURACY', sk: `${body.matchingType}#${Date.now()}`, ...metrics },
  }));

  return metrics;
}
```

## AI Agent Handler (Bedrock)

Generate AI rule improvement proposals

```typescript
import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';

const bedrockClient = new BedrockRuntimeClient({});
const MODEL_ID = process.env.BEDROCK_MODEL_ID!; // e.g. apac.anthropic.claude-sonnet-4-20250514-v1:0
const SUGGESTIONS_TABLE = process.env.SUGGESTIONS_TABLE!;

// POST /api/ai/suggest — request AI rule improvement
async function suggestRuleImprovement(body: {
  currentRules: Array<{ ruleName: string; matchingKeys: string[] }>;
  falseNegatives: Array<{ pair: [any, any]; reason: string }>;
  falsePositives: Array<{ pair: [any, any]; reason: string }>;
}) {
  const prompt = buildPrompt(body);

  const response = await bedrockClient.send(new InvokeModelCommand({
    modelId: MODEL_ID,
    contentType: 'application/json',
    accept: 'application/json',
    body: JSON.stringify({
      anthropic_version: 'bedrock-2023-05-31',
      max_tokens: 4096,
      messages: [{ role: 'user', content: prompt }],
    }),
  }));

  const result = JSON.parse(new TextDecoder().decode(response.body));
  const suggestion = parseSuggestion(result.content[0].text);

  // Save (pending status)
  const suggestionId = crypto.randomUUID();
  await ddbClient.send(new PutCommand({
    TableName: SUGGESTIONS_TABLE,
    Item: {
      pk: 'SUGGESTION',
      sk: suggestionId,
      status: 'PENDING', // PENDING → APPROVED / REJECTED
      suggestion,
      createdAt: new Date().toISOString(),
    },
  }));

  return { suggestionId, suggestion };
}

// POST /api/ai/approve — approve a proposal
async function approveSuggestion(suggestionId: string) {
  // 1. Update status → APPROVED
  // 2. Apply rule changes to ER workflow
  // 3. Record in rule change history
}

// POST /api/ai/reject — reject a proposal
async function rejectSuggestion(suggestionId: string, reason: string) {
  // 1. Update status → REJECTED
  // 2. Record reason in history
}

function buildPrompt(body: any): string {
  return `You are an expert in Entity Resolution matching rules.

## Current rules
${JSON.stringify(body.currentRules, null, 2)}

## Match failure cases (False Negatives - same customer but not matched)
${JSON.stringify(body.falseNegatives, null, 2)}

## Incorrect matches (False Positives - different customers but matched)
${JSON.stringify(body.falsePositives, null, 2)}

Analyze the cases above and propose rule improvements in the following format:

1. Problem analysis (why the failure/mismatch occurred)
2. Proposed rules (in JSON format)
3. Expected effect (predicted precision/recall change)
4. Cautions`;
}
```

## Profiles Handler

Customer Profiles lookup/management

```typescript
import { CustomerProfilesClient, SearchProfilesCommand, GetProfileCommand, ListProfileObjectsCommand } from '@aws-sdk/client-customer-profiles';

const cpClient = new CustomerProfilesClient({});
const DOMAIN_NAME = process.env.CP_DOMAIN_NAME!;

// GET /api/profiles/search?key=email&value=test@example.com
async function searchProfiles(keyName: string, values: string[]) {
  const { Items } = await cpClient.send(new SearchProfilesCommand({
    DomainName: DOMAIN_NAME,
    KeyName: `_${keyName}`, // CP uses underscore prefix for key search
    Values: values,
  }));
  return Items || [];
}

// GET /api/profiles/:profileId
async function getProfile(profileId: string) {
  const profile = await cpClient.send(new GetProfileCommand({
    DomainName: DOMAIN_NAME,
    ProfileId: profileId,
  }));
  return profile;
}

// GET /api/profiles/:profileId/objects?objectType=Booking
async function getProfileObjects(profileId: string, objectTypeName: string) {
  const { Items } = await cpClient.send(new ListProfileObjectsCommand({
    DomainName: DOMAIN_NAME,
    ProfileId: profileId,
    ObjectTypeName: objectTypeName,
  }));
  return Items || [];
}
```

## ⭐ CP Object Type Definition — Exact Key/Field Mapping (Critical)

**The most commonly mistaken part.** The Object Type definition in AWS Customer Profiles is very strict. If defined incorrectly, even when PutProfileObject returns 200 OK the child instance is not attached to the profile, leaving ListProfileObjects/Calculated Attributes all empty. This is hard to debug.

### Absolute Rules (based on AWS official documentation)

1. **Target supports only `_profile`.** `_hotelReservation` / `_loyalty` / `_hotelStayRevenue`, etc. are **TemplateIds**, not Target namespaces.
   > "The format of this field is always a JSON accessor. The only supported target object is `_profile`." — [AWS docs](https://docs.aws.amazon.com/connect/latest/adminguide/object-type-mapping-definition-details.html)

2. **Target is optional.** Child instance objects (Reservation, Folio, etc.) **omit Target**. Providing a Target overwrites a standard profile field, so the instance is not created.

3. **Do not define a `_profileId` key.** `_profileId` is a CP system-reserved key — it is filled with a UUID auto-generated by CP, not the value we set. So even when a child object arrives with `GuestProfileId=golden-...`, no profile with `_profileId=golden-...` exists, matching fails, and inferred profiles explode.

4. **Cross-object links happen only via a Key with the same name + the same value.** The CP key namespace is domain-global.

### The Exact Pattern

```yaml
# config/schema.yaml
object_types:
  - name: GuestProfile           # parent (golden record)
    keys:
      # link key that every child ObjectType attaches to this profile with the same name & value
      - name: GuestKey
        fields: [GuestProfileId]
        standard_identifiers: [PROFILE, UNIQUE]   # ← PROFILE+UNIQUE
      - name: EmailKey
        fields: [EmailAddress]
        standard_identifiers: [LOOKUP_ONLY]       # for lookup
      - name: PhoneKey
        fields: [PhoneNumber]
        standard_identifiers: [LOOKUP_ONLY]
    fields:
      - { name: GuestProfileId, type: STRING, required: true }
      - { name: FirstName, type: STRING }
      - { name: LastName, type: STRING }
      - { name: EmailAddress, type: STRING }
      - { name: PhoneNumber, type: STRING }
      - { name: BirthDate, type: STRING }
      - { name: Address1, type: STRING }
      - { name: City, type: STRING }
      - { name: State, type: STRING }
      - { name: PostalCode, type: STRING }
      - { name: Country, type: STRING }
      # other custom metadata: SourceChannels, MatchId, ImportedAt, ...

  - name: Reservation             # child (instance object)
    keys:
      - name: ReservationKey
        fields: [ReservationId]
        standard_identifiers: [UNIQUE]            # same ReservationId → upsert
      - name: GuestKey                            # same name & field value as the parent
        fields: [GuestProfileId]
        standard_identifiers: [PROFILE]           # PROFILE only (remove UNIQUE)
    fields:
      # required + business fields
      - { name: ReservationId, type: STRING, required: true }
      - { name: GuestProfileId, type: STRING, required: true }
      - { name: TotalAmount, type: NUMBER }
      - { name: NumberOfNights, type: NUMBER }
      # ... declare all fields the CalculatedAttribute will reference
```

### Custom Resource (ObjectType upsert) — Exact Mapping Handler

```typescript
// backend/custom-resources/upsert-object-type/handler.ts
import { CustomerProfilesClient, PutProfileObjectTypeCommand, DeleteProfileObjectTypeCommand } from '@aws-sdk/client-customer-profiles';
import type { CdkCustomResourceEvent, CdkCustomResourceResponse } from 'aws-lambda';

const cp = new CustomerProfilesClient({});
const DOMAIN = process.env.CP_DOMAIN_NAME!;

export async function handler(event: CdkCustomResourceEvent): Promise<CdkCustomResourceResponse> {
  const props = event.ResourceProperties as any;
  const objectTypeName = props.ObjectTypeName as string;

  if (event.RequestType === 'Delete') {
    await cp.send(new DeleteProfileObjectTypeCommand({ DomainName: DOMAIN, ObjectTypeName: objectTypeName }))
      .catch(e => { if (e.name !== 'ResourceNotFoundException') console.error(e); });
    return { PhysicalResourceId: `objtype-${objectTypeName}` };
  }

  // Keys: pass through schema.yaml verbatim. DO NOT auto-add LOOKUP_ONLY.
  const keys: Record<string, any[]> = {};
  for (const k of props.Keys as any[]) {
    keys[k.name] = [{
      StandardIdentifiers: k.standard_identifiers ?? k.StandardIdentifiers ?? [],
      FieldNames: k.fields ?? k.FieldNames,
    }];
  }

  // Fields:
  //   - GuestProfile: standard fields → _profile.X, address subs → _profile.Address.X, custom → _profile.Attributes.X
  //   - Child types (Reservation/Folio/...): Target OMITTED — each PutProfileObject creates an instance
  const STANDARD = new Set(['FirstName','LastName','MiddleName','BirthDate','Gender',
    'EmailAddress','PersonalEmailAddress','BusinessEmailAddress',
    'PhoneNumber','HomePhoneNumber','MobilePhoneNumber','BusinessPhoneNumber',
    'AccountNumber','PartyType','BusinessName']);
  const ADDRESS: Record<string,string> = {
    Address1:'Address.Address1', Address2:'Address.Address2', Address3:'Address.Address3', Address4:'Address.Address4',
    City:'Address.City', State:'Address.State', County:'Address.County', Country:'Address.Country',
    Province:'Address.Province', PostalCode:'Address.PostalCode',
  };

  const fields: Record<string, any> = {};
  for (const f of props.Fields as any[]) {
    const entry: any = { Source: `_source.${f.name}`, ContentType: f.type === 'NUMBER' ? 'NUMBER' : 'STRING' };
    if (objectTypeName === 'GuestProfile') {
      if (STANDARD.has(f.name))      entry.Target = `_profile.${f.name}`;
      else if (ADDRESS[f.name])      entry.Target = `_profile.${ADDRESS[f.name]}`;
      else                           entry.Target = `_profile.Attributes.${f.name}`;
    }
    // child types: NO target
    fields[f.name] = entry;
  }

  // Only the parent type may create profiles. Children link or skip.
  const allowProfileCreation = objectTypeName === 'GuestProfile';

  // Object Type Keys are immutable after creation. PutProfileObjectType cannot
  // change Keys/StandardIdentifiers in place. Schema rev → delete-recreate.
  if (event.RequestType === 'Update') {
    await cp.send(new DeleteProfileObjectTypeCommand({ DomainName: DOMAIN, ObjectTypeName: objectTypeName }))
      .catch(e => { if (e.name !== 'ResourceNotFoundException') console.error(e); });
  }

  await cp.send(new PutProfileObjectTypeCommand({
    DomainName: DOMAIN, ObjectTypeName: objectTypeName,
    Description: props.Description, Keys: keys, Fields: fields,
    AllowProfileCreation: allowProfileCreation,
    ExpirationDays: 365,
  }));

  return { PhysicalResourceId: `objtype-${objectTypeName}`, Data: { ObjectTypeName: objectTypeName } };
}
```

On the CDK side, always include a cache-buster like `properties.SchemaRev` to force a CR re-run on schema bump:

```typescript
// lib/profiles-stack.ts
const SCHEMA_REV = 'rev3-guestkey-profile-link';   // bump on any Keys/Fields change
for (const objType of schemaConfig.object_types) {
  new cdk.CustomResource(this, `ObjType${objType.name}`, {
    serviceToken: provider.serviceToken,
    properties: {
      ObjectTypeName: objType.name,
      Description: objType.description,
      Keys: objType.keys,
      Fields: objType.fields,
      SchemaRev: SCHEMA_REV,
    },
  });
}
```

## Profile Import Handler — Golden Record → CP

PUT the ER matching results as GuestProfile objects.

```typescript
// backend/lambdas/profile-import/handler.ts
import { CustomerProfilesClient, PutProfileObjectCommand, SearchProfilesCommand, DeleteProfileCommand } from '@aws-sdk/client-customer-profiles';

const cp = new CustomerProfilesClient({});
const DOMAIN = process.env.CP_DOMAIN_NAME!;

async function run(matchingType: 'simple'|'advanced'|'ml', replaceExisting: boolean) {
  const variants = await loadVariants();              // S3 ER input CSV
  const groups = await loadGroupings(matchingType);   // DynamoDB matching results: matchId → [variantId,...]

  if (replaceExisting) await deleteExistingGoldenProfiles(groups);

  let imported = 0;
  for (const [matchId, members] of groups) {
    const golden = buildGolden(matchId, members, variants);
    if (!golden) continue;

    // ⚠️ FIELD NAMES MUST MATCH ObjectType.Fields exactly.
    //    Unknown fields are silently dropped.
    //    Do NOT include `ProfileId` (CP-reserved) — use `GuestProfileId` only.
    const objectPayload: Record<string, string> = {
      GuestProfileId: golden.goldenProfileId,         // ← link key (same name as Reservation.GuestKey)
      MatchId: matchId,
      MatchingType: matchingType,
      VariantCount: String(golden.variantCount),
      FirstName: golden.fields.firstname ?? '',
      LastName: golden.fields.lastname ?? '',
      EmailAddress: golden.fields.email ?? '',
      PhoneNumber: golden.fields.phone ?? '',
      BirthDate: golden.fields.dateofbirth ?? '',
      LoyaltyNumber: golden.fields.loyaltynumber ?? '',
      Address1: golden.fields.street ?? '',           // flat — NOT nested { Address: { ... } }
      City: golden.fields.city ?? '',
      State: golden.fields.state ?? '',
      PostalCode: golden.fields.postalcode ?? '',
      Country: golden.fields.country ?? '',
      SourceChannels: golden.fields.sourcechannel ?? '',
      SourceVariantIds: golden.sources.join(','),
      ImportedAt: new Date().toISOString(),
    };

    await cp.send(new PutProfileObjectCommand({
      DomainName: DOMAIN, ObjectTypeName: 'GuestProfile',
      Object: JSON.stringify(objectPayload),
    }));
    imported++;
  }
  return { importedCount: imported };
}
```

## CP Data Import Handler — Reservation/Folio → CP

PUT PostgreSQL transactional data as instances of child ObjectTypes. Because of **`AllowProfileCreation: false`** (on child types), attachment happens only when a matching parent with the GuestProfileId exists.

```typescript
// backend/lambdas/cp-data-import/handler.ts (gist)
for (const row of reservationRows) {
  const goldenId = guestToGolden.get(row.guest_id);
  if (!goldenId) { unmatchedGuestIds++; continue; }   // skip — no parent profile

  await cp.send(new PutProfileObjectCommand({
    DomainName: DOMAIN, ObjectTypeName: 'Reservation',
    Object: JSON.stringify({
      // GuestProfileId is the link key. NO `ProfileId` field.
      GuestProfileId: goldenId,
      ReservationId: row.reservation_id,
      PropertyCode: row.property_code,
      CheckInDate: row.check_in_date,
      CheckOutDate: row.check_out_date,
      NumberOfNights: Number(row.number_of_nights ?? 0),
      AverageDailyRate: Number(row.average_daily_rate ?? 0),
      TotalAmount: Number(row.total_amount ?? 0),
      // ... every field must be declared in schema.yaml ObjectType.fields
    }),
  }));
}
```

### Fire-and-forget Pattern (Large-Volume Import)

API Gateway has a 29-second timeout, so hundreds to thousands of PutProfileObject calls cannot be processed synchronously. Split them off with a Lambda self-invoke:

```typescript
// /api/cp-data-import/run (API path)
if (path.endsWith('/run') && method === 'POST') {
  await lambdaClient.send(new InvokeCommand({
    FunctionName: process.env.AWS_LAMBDA_FUNCTION_NAME!,
    InvocationType: 'Event',                             // async
    Payload: Buffer.from(JSON.stringify({ __mode: 'WORKER' })),
  }));
  return ok({ status: 'STARTED', message: 'Background import 3-10 min' });
}

// Worker mode in same handler
if ((event as any).__mode === 'WORKER') {
  await runImport();   // long-running PutProfileObject loop
  return;
}
```

A self-invoke IAM policy is required in CDK:

```typescript
fn.addToRolePolicy(new iam.PolicyStatement({
  actions: ['lambda:InvokeFunction'],
  resources: [`arn:aws:lambda:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:function:${projectName}-cp-data-import`],
}));
```

## Cleanup Helper (Clean Profiles Without Wiping the Domain)

When an ObjectType is delete-recreated, child instances disappear with it, but the profiles themselves remain in a stale state. Sweep and delete via SearchProfiles using known key values:

```typescript
async function cleanupAllProfiles() {
  const seen = new Set<string>();
  for (const goldenId of allGoldenIds) {
    const r = await cp.send(new SearchProfilesCommand({
      DomainName: DOMAIN, KeyName: 'GuestKey', Values: [goldenId], MaxResults: 10,
    })).catch(() => ({ Items: [] }));
    for (const item of r.Items ?? []) {
      const pid = item.ProfileId;
      if (!pid || seen.has(pid)) continue;
      seen.add(pid);
      await cp.send(new DeleteProfileCommand({ DomainName: DOMAIN, ProfileId: pid }))
        .catch(e => console.warn(e.message));
    }
  }
  return { deleted: seen.size };
}
```

## Ingestion Handler

Data ingestion (CSV upload + load into Glue table)

```typescript
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { parse } from 'csv-parse/sync';

const DATA_BUCKET = process.env.DATA_BUCKET!;

// POST /api/ingestion/upload — CSV upload
async function uploadCsv(body: { fileName: string; content: string; channel: string }) {
  const records = parse(body.content, { columns: true, skip_empty_lines: true });

  // Validate required fields (variantid must exist)
  for (const record of records) {
    if (!record.variantid) {
      return error(400, 'Each record must have a variantid field');
    }
    // Add source channel if not present
    if (!record.sourcechannel) {
      record.sourcechannel = body.channel;
    }
  }

  // Write to S3 in Glue-compatible format (CSV with headers)
  const csvContent = [
    Object.keys(records[0]).join(','),
    ...records.map(r => Object.values(r).join(',')),
  ].join('\n');

  await s3Client.send(new PutObjectCommand({
    Bucket: DATA_BUCKET,
    Key: `er-input/${body.channel}/${body.fileName}`,
    Body: csvContent,
    ContentType: 'text/csv',
  }));

  return { recordCount: records.length, channel: body.channel, path: `er-input/${body.channel}/${body.fileName}` };
}

// POST /api/ingestion/generate — generate sample data (for demos)
async function generateSampleData(body: { count: number; channels: string[] }) {
  // Use faker or custom logic to generate PII variants
  // Each "person" gets multiple variants across channels with realistic variations
  // → See data-generator patterns in travel demo
}
```

## Rule Management Handler

ER rule CRUD + change history

```typescript
const RULE_HISTORY_TABLE = process.env.RULE_HISTORY_TABLE!;

interface RuleChange {
  changeId: string;
  timestamp: string;
  action: 'CREATE' | 'UPDATE' | 'DELETE';
  ruleName: string;
  previousRule?: { matchingKeys: string[] };
  newRule?: { matchingKeys: string[] };
  source: 'MANUAL' | 'AI_APPROVED';
  approvedBy?: string;
}

// GET /api/rules — list current rules
async function listRules() {
  // ER GetMatchingWorkflow → extract rules
}

// POST /api/rules — add a rule
async function createRule(body: { ruleName: string; matchingKeys: string[] }) {
  // 1. Update ER Workflow with new rule
  // 2. Record change history
}

// PUT /api/rules/:ruleName — update a rule
async function updateRule(ruleName: string, body: { matchingKeys: string[] }) {
  // 1. Get current rule (for history)
  // 2. Update ER Workflow
  // 3. Record change history
}

// GET /api/rules/history — change history
async function getRuleHistory() {
  const { Items } = await ddbClient.send(new QueryCommand({
    TableName: RULE_HISTORY_TABLE,
    KeyConditionExpression: 'pk = :pk',
    ExpressionAttributeValues: { ':pk': 'HISTORY' },
    ScanIndexForward: false, // newest first
    Limit: 50,
  }));
  return Items || [];
}
```

## Graph RAG Handler (Optional)

Natural-language graph queries using Neptune + Bedrock

```typescript
import { NeptuneClient } from './shared/neptune-client';

const neptuneClient = new NeptuneClient(process.env.NEPTUNE_ENDPOINT!);

// POST /api/graph/query — natural-language query
async function graphQuery(body: { question: string; context?: string }) {
  // 1. Bedrock: question → openCypher query translation
  const cypherQuery = await generateCypher(body.question);

  // 2. Execute the query on Neptune
  const graphResult = await neptuneClient.executeOpenCypher(cypherQuery);

  // 3. Bedrock: results → natural-language answer generation
  const answer = await generateAnswer(body.question, graphResult);

  return { question: body.question, cypher: cypherQuery, answer, rawResult: graphResult };
}

async function generateCypher(question: string): Promise<string> {
  // Prompt includes graph schema (node labels, relationship types, properties)
  // Returns valid openCypher query
  const prompt = `Given this graph schema:
Nodes: Customer(id, name, email, segment), Booking(id, date, amount), Hotel(name, city)
Relationships: BOOKED(Customer→Booking), STAYED_AT(Booking→Hotel), KNOWS(Customer→Customer)

Convert this question to an openCypher query:
"${question}"`;

  // Call Bedrock...
  return ''; // parsed cypher
}
```

## Graph Sync Handler (Optional)

Customer Profiles → Neptune synchronization

```typescript
// POST /api/graph/sync — sync profiles into the graph
async function syncProfilesToGraph(body: { profileIds?: string[] }) {
  // 1. Look up profiles from CP
  // 2. Profiles → Neptune nodes (Customer, with properties)
  // 3. Object data → Neptune nodes + edges (Booking→Hotel, etc.)
  // 4. Matching results → SAME_AS relationships

  // Upsert pattern (idempotent):
  const upsertCypher = `
    MERGE (c:Customer {id: $profileId})
    SET c.name = $name, c.email = $email, c.segment = $segment, c.updatedAt = datetime()
    RETURN c
  `;
}
```
