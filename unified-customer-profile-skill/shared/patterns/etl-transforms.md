# ETL Transform Patterns

The **Raw → ER input** pipeline pattern applied after data ingestion and before running Entity Resolution.

## End-to-End Pipeline Structure (Essential Understanding)

```
PostgreSQL/CSV/JDBC (Raw)
   │  guests / reservations / folios / loyalty_members / guest_preferences
   │  └─ Un-normalized PII (Korean-English mix, varied phone formats, OTA relay email...)
   ▼
[Glue Crawler] — Register Glue Catalog Tables (src_*)
   │
   ▼
[Glue ETL Job (PySpark)] — Key step: explode one guest into N channel variants
   │   1) Normalize PII (name/phone/email)
   │   2) Generate per-channel variants (HOTEL_WEB, HOTEL_OTA, WALK_IN…)
   │   3) variantId = `V-{guest_id}-{n}` (the unit ER will group on)
   │   4) Align columns to the ER input schema
   ▼
S3: er-input/unified/*.csv  ← input read directly by ER (registered as a Glue Table)
   │
   ▼
[Entity Resolution Workflow] — run Simple / Advanced / ML matching
   ▼
DynamoDB matching_results (matchId groups)
   ▼
[profile-import Lambda] — golden record → CP GuestProfile
   ▼
[cp-data-import Lambda] — PostgreSQL → CP Reservation/Folio (child instances)
   ▼
Calculated Attributes computed automatically
```

**Why variant expansion is needed**: if one guest_id has only one row, ER has nothing to group. By creating rows expressed slightly differently per channel (e.g., 3 phone formats, 2 name locales), the effect of ER matching becomes visible. For demo/PoC use. In production, you simply unify real data already spread across channels.

## Why ETL Is Needed

```
Raw Data (DB/CSV/Parquet)
  │
  │  Name:  "김민호" / "KIM MINHO" / "Kim, Min-Ho" / "MINHO KIM"
  │  Phone: "+82 10-1234-5678" / "010-1234-5678" / "01012345678"
  │  Email: "Minho.Kim@Gmail.COM" / "guest-abc123@booking.com"
  │
  ▼ [ETL Transform]
  │
  │  Name:  "MINHO" + "KIM" (normalized)
  │  Phone: "01012345678" (digits only)
  │  Email: "minho.kim@gmail.com" (lowercase, relay tagged)
  │
  ▼
Entity Resolution (improved accuracy with normalized data)
```

**If you run ER without ETL**: Advanced Rule fuzzy matching covers some cases,
but extreme variations (Korean-English mix, presence/absence of country code) may be missed.

## ETL Execution Strategy (Decision)

```
Q: What is the data quality / variation level?
│
├─ Already structured (exported from a single system, consistent format)
│   └─ ETL unnecessary → run ER directly
│
├─ Minor variation (case, whitespace, hyphens)
│   └─ Lightweight ETL (inline transform within the Lambda)
│       └─ Cost: nearly 0 (included in Lambda execution time)
│
├─ Severe variation (Korean-English mix, multilingual, unstructured address)
│   └─ Full ETL (Glue ETL Job)
│       └─ Cost: $0.44/DPU-hour (typically 2 DPU × 5–10 min = ~$0.07/run)
│
└─ Large volume + complex transforms (millions of records, joins needed)
    └─ Glue ETL Job (Spark) or Step Functions pipeline
        └─ Cost: proportional to DPU × time
```

## Lightweight ETL (Lambda Inline)

For small data and simple transforms, process directly inside the Lambda handler.

```typescript
// backend/lambdas/ingestion/transforms.ts

/**
 * PII normalization pipeline
 * Order: trim whitespace → normalize name → normalize phone → normalize email → normalize address
 */
export function normalizeRecord(record: Record<string, string>): Record<string, string> {
  const normalized = { ...record };

  // 1. Common: trim leading/trailing whitespace
  for (const key of Object.keys(normalized)) {
    if (typeof normalized[key] === 'string') {
      normalized[key] = normalized[key].trim();
    }
  }

  // 2. Normalize name
  if (normalized.firstname) normalized.firstname = normalizeName(normalized.firstname);
  if (normalized.lastname) normalized.lastname = normalizeName(normalized.lastname);

  // 3. Normalize phone
  if (normalized.phone) normalized.phone = normalizePhone(normalized.phone);

  // 4. Normalize email
  if (normalized.email) normalized.email = normalizeEmail(normalized.email);

  // 5. Normalize address
  if (normalized.postalcode) normalized.postalcode = normalizePostalCode(normalized.postalcode);

  return normalized;
}

// ─── Name normalization ──────────────────────────────────────

function normalizeName(input: string): string {
  // 1. Remove special characters (hyphens, dots, etc.)
  let name = input.replace(/[-.'·]/g, '');

  // 2. Determine whether it is Korean or English
  const isKorean = /[\uAC00-\uD7AF]/.test(name);

  if (isKorean) {
    // Korean name: remove whitespace and keep as-is
    name = name.replace(/\s/g, '');
  } else {
    // English name: uppercase, remove whitespace
    name = name.replace(/\s/g, '').toUpperCase();
  }

  return name;
}

/**
 * Split a Korean name into surname/given name
 * - Korean 2–4 chars: first char = surname, rest = given name
 * - English: "KIM MINHO" → { lastName: "KIM", firstName: "MINHO" }
 *            "MINHO KIM" → detected and swapped
 */
export function splitKoreanName(fullName: string): { firstName: string; lastName: string } {
  const trimmed = fullName.trim();
  const isKorean = /[\uAC00-\uD7AF]/.test(trimmed);

  if (isKorean) {
    // Korean: first char = surname (1–2 chars), rest = given name
    const noSpace = trimmed.replace(/\s/g, '');
    // Most surnames are 1 char (90%+); the two-char surnames are listed below
    const twoCharSurnames = ['남궁', '선우', '사공', '독고', '황보', '제갈', '하동'];
    const isTwoChar = twoCharSurnames.some(s => noSpace.startsWith(s));
    const lastNameLen = isTwoChar ? 2 : 1;
    return {
      lastName: noSpace.slice(0, lastNameLen),
      firstName: noSpace.slice(lastNameLen),
    };
  } else {
    // English: split on whitespace
    const parts = trimmed.toUpperCase().split(/\s+/);
    if (parts.length === 1) return { firstName: parts[0], lastName: '' };

    // Detect Korean romanization order: "KIM MINHO" vs "MINHO KIM"
    // Korean surname list (top 10)
    const koreanSurnames = ['KIM', 'LEE', 'PARK', 'CHOI', 'JUNG', 'KANG',
      'CHO', 'YUN', 'JANG', 'LIM', 'HAN', 'OH', 'SEO', 'SHIN', 'KWON',
      'HWANG', 'AHN', 'SONG', 'RYU', 'HONG', 'YOO', 'MOON', 'YANG', 'NOH'];

    if (koreanSurnames.includes(parts[0]) && !koreanSurnames.includes(parts[parts.length - 1])) {
      // "KIM MINHO" → surname first
      return { lastName: parts[0], firstName: parts.slice(1).join('') };
    } else {
      // "MINHO KIM" → Western style or surname last
      return { firstName: parts.slice(0, -1).join(''), lastName: parts[parts.length - 1] };
    }
  }
}

// ─── Phone number normalization ──────────────────────────────────────

function normalizePhone(input: string): string {
  // 1. Extract digits only
  let digits = input.replace(/[^\d+]/g, '');

  // 2. Remove country code
  if (digits.startsWith('+82')) digits = '0' + digits.slice(3);
  if (digits.startsWith('82') && digits.length > 10) digits = '0' + digits.slice(2);

  // 3. Add leading 0 if missing
  if (!digits.startsWith('0') && digits.length === 10) digits = '0' + digits;

  return digits; // result: "01012345678"
}

// ─── Email normalization ──────────────────────────────────────

interface NormalizedEmail {
  email: string;
  isRelay: boolean;
  relayProvider?: string;
}

function normalizeEmail(input: string): string {
  // Convert to lowercase
  return input.toLowerCase().trim();
}

export function detectRelayEmail(email: string): NormalizedEmail {
  const normalized = email.toLowerCase().trim();
  const relayPatterns: Record<string, RegExp> = {
    'booking.com': /guest-.*@booking\.com/,
    'expedia': /.*@guest\.expedia\.com/,
    'hotels.com': /.*@guest\.hotels\.com/,
    'coupang': /.*@buyer\.coupang\.com/,
    'naver': /.*@relay\.naver\.com/,
  };

  for (const [provider, pattern] of Object.entries(relayPatterns)) {
    if (pattern.test(normalized)) {
      return { email: normalized, isRelay: true, relayProvider: provider };
    }
  }

  return { email: normalized, isRelay: false };
}

// ─── Postal code normalization ──────────────────────────────────────

function normalizePostalCode(input: string): string {
  // Korea: 5-digit number (old 6-digit cannot be converted to 5-digit, so keep as-is)
  const digits = input.replace(/[^\d]/g, '');
  return digits;
}

// ─── Data quality score ───────────────────────────────────

export interface QualityScore {
  completeness: number;  // 0–1: ratio of required fields filled
  validity: number;      // 0–1: format validity ratio
  overall: number;       // weighted average
}

export function calculateQuality(record: Record<string, string>, requiredFields: string[]): QualityScore {
  // Completeness: non-empty among required fields
  const filled = requiredFields.filter(f => record[f] && record[f].trim() !== '');
  const completeness = filled.length / requiredFields.length;

  // Validity: format checks
  let validCount = 0;
  let checkCount = 0;
  if (record.email) { checkCount++; if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(record.email)) validCount++; }
  if (record.phone) { checkCount++; if (/^0\d{9,10}$/.test(normalizePhone(record.phone))) validCount++; }
  const validity = checkCount > 0 ? validCount / checkCount : 1;

  return { completeness, validity, overall: completeness * 0.7 + validity * 0.3 };
}
```

## Full ETL (Glue Job — PySpark)

For large data and complex transforms, process with a Glue ETL Job.

### CDK Resources

```typescript
// lib/etl-stack.ts (or include in ingestion-stack.ts)
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';

export class EtlConstruct extends Construct {
  constructor(scope: Construct, id: string, props: EtlProps) {
    super(scope, id);

    // Upload the ETL script to S3
    const etlScript = new s3assets.Asset(this, 'EtlScript', {
      path: 'backend/glue-scripts/normalize-pii.py',
    });

    // Glue ETL Job Role
    const etlRole = new iam.Role(this, 'EtlRole', {
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole'),
      ],
    });
    props.dataBucket.grantReadWrite(etlRole);

    // Glue ETL Job
    new glue.CfnJob(this, 'NormalizePiiJob', {
      name: `${props.projectName}-normalize-pii`,
      role: etlRole.roleArn,
      command: {
        name: 'glueetl',
        scriptLocation: etlScript.s3ObjectUrl,
        pythonVersion: '3',
      },
      defaultArguments: {
        '--TempDir': `s3://${props.dataBucket.bucketName}/glue-temp/`,
        '--job-bookmark-option': 'job-bookmark-enable', // incremental processing
        '--SOURCE_PATH': `s3://${props.dataBucket.bucketName}/er-input-raw/`,
        '--TARGET_PATH': `s3://${props.dataBucket.bucketName}/er-input/`, // normalized output
        '--DATABASE_NAME': props.glueDbName,
        '--TABLE_NAME': props.glueTableName,
      },
      glueVersion: '4.0',
      numberOfWorkers: 2,
      workerType: 'G.1X', // 4 vCPU, 16GB — sufficient for small scale
      timeout: 30, // 30 minutes
    });
  }
}
```

### Glue ETL Script (PySpark)

```python
# backend/glue-scripts/normalize-pii.py
import sys
import re
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import udf, col, lower, trim, regexp_replace, when, lit
from pyspark.sql.types import StringType, StructType, StructField, FloatType

args = getResolvedOptions(sys.argv, ['JOB_NAME', 'SOURCE_PATH', 'TARGET_PATH', 'DATABASE_NAME', 'TABLE_NAME'])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# ─── UDFs ─────────────────────────────────────────────

@udf(StringType())
def normalize_phone(phone):
    """Normalize phone: extract digits only + remove country code"""
    if not phone:
        return None
    digits = re.sub(r'[^\d]', '', phone)
    # remove +82
    if digits.startswith('82') and len(digits) > 10:
        digits = '0' + digits[2:]
    # add leading 0 if missing
    if not digits.startswith('0') and len(digits) == 10:
        digits = '0' + digits
    return digits

@udf(StringType())
def normalize_name(name):
    """Normalize name: remove special chars, remove whitespace, uppercase"""
    if not name:
        return None
    # detect Korean
    if re.search(r'[\uAC00-\uD7AF]', name):
        return re.sub(r'\s', '', name)
    else:
        return re.sub(r'[-.\'\s]', '', name).upper()

@udf(StringType())
def detect_relay_email(email):
    """Detect relay email → return a tag (relay:provider or None)"""
    if not email:
        return None
    e = email.lower().strip()
    patterns = {
        'booking.com': r'guest-.*@booking\.com',
        'expedia': r'.*@guest\.expedia\.com',
        'hotels.com': r'.*@guest\.hotels\.com',
    }
    for provider, pattern in patterns.items():
        if re.match(pattern, e):
            return f'relay:{provider}'
    return None

@udf(FloatType())
def quality_score(firstname, lastname, email, phone):
    """Data quality score (0–1)"""
    fields = [firstname, lastname, email, phone]
    filled = sum(1 for f in fields if f and f.strip())
    return filled / len(fields)

# ─── Main Transform ───────────────────────────────────

# 1. Read source (auto-detect CSV or Parquet)
source_path = args['SOURCE_PATH']
if source_path.endswith('.parquet') or '/parquet/' in source_path:
    df = spark.read.parquet(source_path)
else:
    df = spark.read.option('header', 'true').csv(source_path)

# 2. Apply normalization
df_normalized = df \
    .withColumn('firstname', normalize_name(col('firstname'))) \
    .withColumn('lastname', normalize_name(col('lastname'))) \
    .withColumn('phone', normalize_phone(col('phone'))) \
    .withColumn('email', lower(trim(col('email')))) \
    .withColumn('_relay_tag', detect_relay_email(col('email'))) \
    .withColumn('_quality_score', quality_score(col('firstname'), col('lastname'), col('email'), col('phone')))

# 3. Relay email handling: tag so the email match key can be excluded in ER
# (keep the actual email value; post-processing possible via the _relay_tag column)

# 4. Quality filter (optional): warn on records below the minimum quality
low_quality = df_normalized.filter(col('_quality_score') < 0.25)
if low_quality.count() > 0:
    print(f"⚠️ Low quality records: {low_quality.count()}")
    # Save to DLQ or a separate path
    low_quality.write.mode('append').parquet(f"{args['TARGET_PATH']}_low_quality/")

# 5. Save normalized output (Parquet — for ER input)
df_clean = df_normalized.filter(col('_quality_score') >= 0.25) \
    .drop('_relay_tag', '_quality_score')  # remove internal columns

df_clean.write.mode('overwrite').parquet(args['TARGET_PATH'])

# 6. Update the Glue Data Catalog (register Parquet partitions)
# → Either the Crawler does this automatically, or register directly via the Catalog API

job.commit()
```

## ETL Pipeline Integration (Step Functions)

For complex scenarios, orchestrate with Step Functions:

```
┌─────────────────────────────────────────────────────┐
│ Step Functions: UCP Data Pipeline                     │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ① Ingest ──→ ② ETL Normalize ──→ ③ ER Matching     │
│  (S3/DB)       (Glue Job)          (ER Job)          │
│                     │                    │            │
│                     ▼                    ▼            │
│              [Quality report]      ④ CP Import        │
│                                         │            │
│                                         ▼            │
│                                   ⑤ Graph Sync       │
│                                   (optional)          │
│                                                       │
└─────────────────────────────────────────────────────┘
```

### CDK (Step Functions)

```typescript
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';

// Glue ETL → ER → CP Import pipeline
const etlJob = new tasks.GlueStartJobRun(this, 'RunEtl', {
  glueJobName: `${projectName}-normalize-pii`,
  integrationPattern: sfn.IntegrationPattern.RUN_JOB, // wait until completion
});

const erJob = new tasks.LambdaInvoke(this, 'RunMatching', {
  lambdaFunction: matchingLambda,
  payload: sfn.TaskInput.fromObject({ matchingType: 'advanced' }),
});

const cpImport = new tasks.LambdaInvoke(this, 'ImportToCP', {
  lambdaFunction: profilesLambda,
  payload: sfn.TaskInput.fromObject({ action: 'import-golden-records' }),
});

const pipeline = etlJob
  .next(erJob)
  .next(cpImport);

new sfn.StateMachine(this, 'DataPipeline', {
  stateMachineName: `${projectName}-data-pipeline`,
  definitionBody: sfn.DefinitionBody.fromChainable(pipeline),
  timeout: cdk.Duration.hours(1),
});
```

## Transform Rule Configuration (Schema-Driven)

Add transform rules declaratively in `config/schema.yaml`:

```yaml
# config/schema.yaml (extended)
features:
  ingestion:
    mode: glue_connection  # csv | parquet | glue_connection | kinesis | hybrid
  etl:
    enabled: true
    mode: glue_job         # inline (Lambda) | glue_job (PySpark)
    transforms:
      - field: firstname
        operations: [trim, remove_special, uppercase, remove_spaces]
      - field: lastname
        operations: [trim, remove_special, uppercase, remove_spaces]
      - field: phone
        operations: [digits_only, remove_country_code_kr, ensure_leading_zero]
      - field: email
        operations: [lowercase, trim, tag_relay]
      - field: postalcode
        operations: [digits_only]
    quality_filter:
      min_score: 0.25      # below this → DLQ
      required_fields: [firstname, lastname]  # reject if any is missing
    relay_email:
      action: tag          # tag | exclude | replace_with_null
      providers: [booking.com, expedia, hotels.com, coupang]
```

## Discovery Questions (ETL-Related)

Additional questions for the AI to ask the user:

```
"Do you need data normalization?"
├─ Already clean → etl.enabled: false
├─ Name/phone format inconsistencies → etl.mode: inline (handled within Lambda)
└─ Large volume + complex transforms → etl.mode: glue_job

"Are Korean names included?"
├─ YES → add Korean-English name normalization + surname/given-name splitting logic
└─ NO → standard English normalization only

"How to handle relay emails (OTA/marketplace)?"
├─ Tag only (lower the email match weight in ER) → tag
├─ Exclude entirely from ER matching → exclude
└─ Replace with null (fall back to phone/name) → replace_with_null
```

## Raw → ER Input — Full PySpark Script Pattern (`backend/glue-scripts/build-er-input.py`)

This script implements the entire **[Glue ETL Job]** stage of the pipeline diagram above. It reads the `src_*` tables in the Glue Catalog (registered from PostgreSQL by the Crawler) and converts them into a single CSV that ER can use.

```python
import sys, re, random
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql.functions import udf, col, lit, concat, explode, when
from pyspark.sql.types import StringType, ArrayType, StructType, StructField

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'TARGET_S3_PATH', 'GLUE_DATABASE', 'GUESTS_TABLE',
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext); job.init(args['JOB_NAME'], args)

# ─── Normalization UDFs ──────────────────────────────
@udf(StringType())
def normalize_name(name):
    if not name: return None
    if re.search(r'[가-힯]', name): return re.sub(r'\s', '', name)
    return re.sub(r"[-.'\s]", '', name).upper()

@udf(StringType())
def normalize_phone(phone):
    if not phone: return None
    digits = re.sub(r'[^\d]', '', phone)
    if digits.startswith('82') and len(digits) > 10: digits = '0' + digits[2:]
    if not digits.startswith('0') and len(digits) == 10: digits = '0' + digits
    return digits

@udf(StringType())
def mask_relay_email(email):
    if not email: return None
    e = email.lower().strip()
    for p in [r'guest-.*@booking\.com', r'.*@guest\.expedia\.com',
              r'.*@guest\.hotels\.com', r'.*@agoda\.com', r'.*@trip\.com']:
        if re.match(p, e): return ''
    return e

# ─── Variant expansion (KEY DEMO TRICK) ──────────────
# Each guest becomes 2-3 ER input rows representing different channels
# with realistic variations. This is what makes ER matching demonstrably useful.
variant_struct = ArrayType(StructType([
    StructField('suffix', StringType()),
    StructField('channel', StringType()),
    StructField('phone_format', StringType()),     # 'digits' | 'hyphen' | 'plus82'
    StructField('email_strategy', StringType()),   # 'normal' | 'relay' | 'empty'
    StructField('name_locale', StringType()),      # 'asis' | 'en' | 'ko'
]))

@udf(variant_struct)
def make_variants(seed):
    rng = random.Random(seed)
    n = rng.choice([2, 2, 3])
    channels = ['HOTEL_WEB', 'HOTEL_APP', 'HOTEL_OTA', 'WALK_IN', 'CALL_CENTER', 'CORPORATE']
    rng.shuffle(channels)
    out = []
    for i in range(n):
        ch = channels[i]
        phone_fmt = rng.choice(['digits', 'hyphen', 'plus82'])
        email_strat = (rng.choice(['relay', 'relay', 'normal']) if ch == 'HOTEL_OTA' else
                       rng.choice(['empty', 'empty', 'normal']) if ch == 'WALK_IN' else 'normal')
        name_loc = rng.choice(['asis', 'asis', 'asis', 'ko' if i == 1 else 'en'])
        out.append((str(i + 1), ch, phone_fmt, email_strat, name_loc))
    return out

@udf(StringType())
def reformat_phone(phone, fmt):
    if not phone: return None
    digits = re.sub(r'[^\d]', '', phone)
    if digits.startswith('82') and len(digits) > 10: digits = '0' + digits[2:]
    if fmt == 'digits': return digits
    if fmt == 'hyphen' and len(digits) == 11:
        return f'{digits[0:3]}-{digits[3:7]}-{digits[7:]}'
    if fmt == 'plus82' and digits.startswith('0'):
        return '+82 ' + digits[1:3] + '-' + digits[3:7] + '-' + digits[7:]
    return digits

# ─── Main ────────────────────────────────────────────
guests = glueContext.create_dynamic_frame.from_catalog(
    database=args['GLUE_DATABASE'], table_name=args['GUESTS_TABLE'],
).toDF()

expanded = guests.withColumn('_variants', make_variants(col('guest_id'))) \
    .withColumn('_v', explode(col('_variants'))).drop('_variants')

out = expanded.select(
    concat(lit('V-'), col('guest_id'), lit('-'), col('_v.suffix')).alias('variantid'),  # ⭐ ER unit
    normalize_name(col('firstname')).alias('firstname'),
    normalize_name(col('lastname')).alias('lastname'),
    mask_relay_email(col('email')).alias('email'),
    reformat_phone(col('phone'), col('_v.phone_format')).alias('phone'),
    col('dateofbirth').cast('string').alias('dateofbirth'),
    when(col('loyaltynumber').isNull(), lit('')).otherwise(col('loyaltynumber')).alias('loyaltynumber'),
    when(col('street').isNull(), lit('')).otherwise(col('street')).alias('street'),
    when(col('city').isNull(), lit('')).otherwise(col('city')).alias('city'),
    when(col('state').isNull(), lit('')).otherwise(col('state')).alias('state'),
    when(col('postalcode').isNull(), lit('')).otherwise(col('postalcode')).alias('postalcode'),
    when(col('country').isNull(), lit('')).otherwise(col('country')).alias('country'),
    col('_v.channel').alias('sourcechannel'),
)

# Output as a single CSV (ER does not recognize partitions)
out.coalesce(1).write.mode('overwrite').option('header', 'true') \
   .csv(args['TARGET_S3_PATH'])

job.commit()
```

### CDK — Crawler + ETL Job + Glue Table for ER input

```typescript
// lib/ingestion-stack.ts (gist)
const etlScript = new s3assets.Asset(this, 'BuildErInputScript', {
  path: 'backend/glue-scripts/build-er-input.py',
});

const etlJob = new glue.CfnJob(this, 'BuildErInput', {
  name: `${projectName}-build-er-input`,
  role: glueRole.roleArn,
  glueVersion: '4.0', numberOfWorkers: 2, workerType: 'G.1X',
  command: { name: 'glueetl', scriptLocation: etlScript.s3ObjectUrl, pythonVersion: '3' },
  defaultArguments: {
    '--JOB_NAME': `${projectName}-build-er-input`,
    '--TARGET_S3_PATH': `s3://${dataBucket.bucketName}/er-input/unified/`,
    '--GLUE_DATABASE': glueDb.databaseName,
    '--GUESTS_TABLE': 'src_<schema>_public_guests',
    '--enable-glue-datacatalog': 'true',
    '--TempDir': `s3://${dataBucket.bucketName}/glue-temp/`,
  },
});

// Statically define the Glue Table for ER to read (S3 prefix)
new glue.CfnTable(this, 'ErInputTable', {
  catalogId: cdk.Aws.ACCOUNT_ID,
  databaseName: glueDb.databaseName,
  tableInput: {
    name: `${projectName.replace(/-/g, '_')}_variants`,
    tableType: 'EXTERNAL_TABLE',
    parameters: { classification: 'csv', skipHeaderLineCount: '1' },
    storageDescriptor: {
      location: `s3://${dataBucket.bucketName}/er-input/unified/`,
      inputFormat: 'org.apache.hadoop.mapred.TextInputFormat',
      outputFormat: 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
      serdeInfo: {
        serializationLibrary: 'org.apache.hadoop.hive.serde2.OpenCSVSerde',
        parameters: { 'separatorChar': ',', 'quoteChar': '"' },
      },
      columns: [
        { name: 'variantid', type: 'string' },
        { name: 'firstname', type: 'string' },
        { name: 'lastname', type: 'string' },
        { name: 'email', type: 'string' },
        { name: 'phone', type: 'string' },
        { name: 'dateofbirth', type: 'string' },
        { name: 'loyaltynumber', type: 'string' },
        { name: 'street', type: 'string' },
        { name: 'city', type: 'string' },
        { name: 'state', type: 'string' },
        { name: 'postalcode', type: 'string' },
        { name: 'country', type: 'string' },
        { name: 'sourcechannel', type: 'string' },
      ],
    },
  },
});
```

### Lambda Trigger (`/api/ingestion/build-er-input`)

To run the ETL with a single click of the frontend IngestionPage button, add a `glue:StartJobRun` call to the ingestion handler:

```typescript
// backend/lambdas/ingestion/handler.ts
if (path.endsWith('/build-er-input') && method === 'POST') {
  const r = await glue.send(new StartJobRunCommand({
    JobName: process.env.ETL_JOB_NAME!,
    Arguments: { '--SOURCE_REFRESH': new Date().toISOString() }, // bookmark force
  }));
  return ok({ jobRunId: r.JobRunId });
}
```

### ER Input Schema — Domain-Agnostic Standard

For ER to work most efficiently, align the input schema to the following 12 columns:

| Column | Meaning | match_key possible |
|---|---|---|
| `variantid` | unique ID for ER grouping (`V-{entity_id}-{n}`) | UNIQUE_ID (required) |
| `firstname` / `lastname` | normalized name | NAME_FIRST / NAME_LAST |
| `email` | lower + relay masking | EMAIL_ADDRESS |
| `phone` | normalized number | PHONE_NUMBER |
| `dateofbirth` | YYYY-MM-DD | DATE |
| `loyaltynumber` | membership number | STRING |
| `street/city/state/postalcode/country` | 5 address elements | ADDRESS_* (group: FullAddress) |
| `sourcechannel` | for tracking (not used in matching) | — |
