# Calculated Attributes — From Definition to On-Screen Display

A Customer Profiles Calculated Attribute is a **dynamic aggregate value computed based on Object Type instances** (e.g. annual revenue, cumulative nights, most recent visit date). Creating the definition alone does not produce a value — it is **populated only after the correct child instances are attached**. If you do not guide the user through this flow, they get confused with "why does everything show 0?"

## Behavior flow (must guide the user)

```
1) Define ObjectType (schema.yaml + Custom Resource)
2) Define Calculated Attribute (CreateCalculatedAttributeDefinition)
3) GuestProfile import (profile-import handler)              ← parent profile
4) Reservation/Folio import (cp-data-import handler)         ← child instances
5) CP indexing in progress (Status: PREPARING)
6) Status becomes COMPLETED + Readiness.ProgressPercentage=100 → usable
7) Call GetCalculatedAttributeForProfile → returns Value
```

**If you skip Step 4, Step 7 always returns 0/null/empty.** This is where users get stuck most often.

## Definition — `config/schema.yaml`

```yaml
calculated_attributes:
  - name: TotalHotelRevenueInAYear
    object_type: Reservation              # ← can reference only one ObjectType
    field: TotalAmount                     # ← the key name in that ObjectType's Fields map
    aggregation: SUM                       # SUM | COUNT | AVERAGE | MIN | MAX | FIRST_OCCURRENCE | LAST_OCCURRENCE | MAX_OCCURRENCE
    threshold_days: 365                    # time window (0 = entire period)
  - name: TotalNightsInAYear
    object_type: Reservation
    field: NumberOfNights
    aggregation: SUM
    threshold_days: 365
  - name: AverageDailyRateLast365
    object_type: Reservation
    field: AverageDailyRate
    aggregation: AVERAGE
    threshold_days: 365
  - name: TotalFolioSpendInAYear
    object_type: Folio
    field: Amount
    aggregation: SUM
    threshold_days: 365
  - name: MostRecentStayDate
    object_type: Reservation
    field: CheckInDate
    aggregation: MAX_OCCURRENCE            # the field value of the most recently ingested instance
    threshold_days: 0
```

### Constraints (AWS official)

- Can reference only **one ObjectType + up to 2 fields** (expression: `{Type.Field1} + {Type.Field2}`)
- **Expression format: `{ObjectTypeName.AttributeName}`** — not a Target path. Reference by the **key name** in the Fields map
- **AttributeName** must be declared in the Object Type's Fields map. Undeclared fields cannot be referenced
- **Up to 20 per domain** (quota increase can be requested)
- **Conditions.Range.Unit** supports only `DAYS`
- **UseHistoricalData=true**: instances already ingested before the definition was created are also included in the calculation

## Custom Resource — definition create/update

```typescript
// backend/custom-resources/create-calculated-attributes/handler.ts
import {
  CustomerProfilesClient,
  CreateCalculatedAttributeDefinitionCommand,
  UpdateCalculatedAttributeDefinitionCommand,
  DeleteCalculatedAttributeDefinitionCommand,
} from '@aws-sdk/client-customer-profiles';

const cp = new CustomerProfilesClient({});
const DOMAIN = process.env.CP_DOMAIN_NAME!;

export async function handler(event) {
  const props = event.ResourceProperties;
  const name = props.CalculatedAttributeName;

  if (event.RequestType === 'Delete') {
    await cp.send(new DeleteCalculatedAttributeDefinitionCommand({
      DomainName: DOMAIN, CalculatedAttributeName: name,
    })).catch(e => { if (e.name !== 'ResourceNotFoundException') console.error(e); });
    return { PhysicalResourceId: `calc-${name}` };
  }

  const conditions = props.ThresholdDays && Number(props.ThresholdDays) > 0
    ? { Range: { Value: Number(props.ThresholdDays), Unit: 'DAYS' } }
    : undefined;

  const objectTypeName = props.ObjectTypeName;
  const attrs = (props.AttributeDetails.Attributes as any[]).map(a => ({ Name: a.Name }));
  const expression = attrs.map(a => `{${objectTypeName}.${a.Name}}`).join(' + ');

  const createParams = {
    DomainName: DOMAIN,
    CalculatedAttributeName: name,
    DisplayName: name,
    Description: `Calculated attribute: ${name}`,
    AttributeDetails: { Attributes: attrs, Expression: expression },
    Statistic: props.Statistic,
    UseHistoricalData: true,                       // ← important
    ...(conditions && { Conditions: conditions }),
  };

  // After a Domain wipe + redeploy, even if an Update event arrives the definition may not exist — safety net
  if (event.RequestType === 'Create') {
    try {
      await cp.send(new CreateCalculatedAttributeDefinitionCommand(createParams));
    } catch (e: any) {
      if (e.name !== 'ConflictException' && e.name !== 'BadRequestException') throw e;
      await updateOnly(name, conditions);
    }
  } else {
    try {
      await updateOnly(name, conditions);
    } catch (e: any) {
      if (e.name !== 'ResourceNotFoundException') throw e;
      await cp.send(new CreateCalculatedAttributeDefinitionCommand(createParams));
    }
  }
  return { PhysicalResourceId: `calc-${name}`, Data: { Name: name } };
}

async function updateOnly(name: string, conditions: any) {
  await cp.send(new UpdateCalculatedAttributeDefinitionCommand({
    DomainName: DOMAIN, CalculatedAttributeName: name,
    DisplayName: name, Description: `Calculated attribute: ${name}`,
    ...(conditions && { Conditions: conditions }),
  }));
}
```

## CDK — Custom Resource registration + cache buster

```typescript
// lib/profiles-stack.ts (key points)
const SCHEMA_REV = 'rev3-guestkey-profile-link';
let prevCalc: cdk.CustomResource | undefined;
for (const calc of schemaConfig.calculated_attributes) {
  const calcResource = new cdk.CustomResource(this, `CalcAttr${calc.name}`, {
    serviceToken: new cr.Provider(this, `CalcAttrProvider${calc.name}`, {
      onEventHandler: calcAttrFn,
    }).serviceToken,
    properties: {
      CalculatedAttributeName: calc.name,
      ObjectTypeName: calc.object_type,
      AttributeDetails: { Attributes: [{ Name: calc.field }] },
      Statistic: calc.aggregation,
      ThresholdDays: calc.threshold_days,
      SchemaRev: SCHEMA_REV,                       // bump triggers re-run
    },
  });
  // The ObjectType must be created first before creating the calc
  calcResource.node.addDependency(prevCalc ?? cpDomain);
  prevCalc = calcResource;
}
```

## Usage — `GetCalculatedAttributeForProfile`

```typescript
import { GetCalculatedAttributeForProfileCommand, ListCalculatedAttributeDefinitionsCommand } from '@aws-sdk/client-customer-profiles';

async function loadCalcAttrs(profileId: string) {
  const { Items: defs } = await cp.send(new ListCalculatedAttributeDefinitionsCommand({
    DomainName: DOMAIN, MaxResults: 30,
  }));

  const calculated: Record<string, string | undefined> = {};
  for (const ca of defs ?? []) {
    if (!ca.CalculatedAttributeName) continue;
    if (ca.CalculatedAttributeName.startsWith('_')) continue;     // skip system-defined
    try {
      const r = await cp.send(new GetCalculatedAttributeForProfileCommand({
        DomainName: DOMAIN, ProfileId: profileId,
        CalculatedAttributeName: ca.CalculatedAttributeName,
      }));
      calculated[ca.CalculatedAttributeName] = r.Value;
    } catch { /* skip */ }
  }
  return calculated;
}
```

Example response (READY state):
```json
{
  "CalculatedAttributeName": "TotalHotelRevenueInAYear",
  "Value": "1123950",
  "IsDataPartial": "true",                 // uses only instances within the window
  "LastObjectTimestamp": "2026-05-15T00:23:09Z"
}
```

## Status tracking

```bash
aws customer-profiles get-calculated-attribute-definition \
  --domain-name <domain> --calculated-attribute-name TotalHotelRevenueInAYear \
  --query '[Status,Readiness]'
```

```
PREPARING → COMPLETED (Readiness.ProgressPercentage = 100)
```

If `PREPARING`, it is indexing right after instance ingestion, so **instead of telling the user "wait a few minutes," guide them to "first check whether Step 4 (Send to CP) is done"** (skipping Step 4 is the most common mistake).

## UI guidance — steps to show the user

Always display the following guidance in the Calculated Attributes section of `ProfileViewPage`:

> 📌 Calculated Attributes are populated only after all 4 steps below are complete:
>
> 1. Run ER matching (Matching page)
> 2. **Send to CP — Step 1: Golden Profile Import** (Profile Import page)
> 3. **Send to CP — Step 2: Reservation/Folio Import** ← always 0/null if skipped
> 4. CP finishes indexing (a few minutes to tens of minutes, until the definition's Status becomes `COMPLETED`)

If the value is empty, it improves the user experience to surface the Step 4 guidance + a Status lookup button together.

## Commonly mistaken patterns (debugging checklist)

| Symptom | Cause | Fix |
|---|---|---|
| All calc attrs are 0/empty | Step 4 (cp-data-import) not run, or child instances not attached to the parent | Call `ListProfileObjects(profileId, ObjectTypeName=Reservation)` to first confirm instances exist |
| Reservation came in but calc is 0 | `field` not in ObjectType.Fields map, or ContentType is STRING (NUMBER aggregation not possible) | Specify `type: NUMBER` on the ObjectType field in schema.yaml |
| ProfileCount surge (inferred profiles) | Child Object Type has a `_profileId` key, or `AllowProfileCreation: true` | Remove `_profileId` + set `AllowProfileCreation: false` on children (see `shared/patterns/lambda-handlers.md`) |
| Status stays PREPARING | Still indexing, or a domain with empty ObjectType and Calc Attr definitions | Re-check after 1-3 minutes. It will never reach COMPLETED if there are 0 ObjectType instances |
| Expression error | Case mismatch like `{Reservation.totalAmount}` | Fields map keys are case-sensitive. `{Reservation.TotalAmount}` must match exactly |
| MAX_OCCURRENCE but empty value | Conditions.Range too short so no instances exist | `threshold_days: 0` (entire period) or increase it |
