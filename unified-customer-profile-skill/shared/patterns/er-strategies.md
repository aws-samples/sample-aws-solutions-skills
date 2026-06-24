# Entity Resolution — Matching Strategies

## Overview

Entity Resolution offers three matching types, but the core workflow is
**not "the user directly selects rules"**, but rather:

```

## Bedrock-Based Automatic Initial Rule Generation (Core Implementation)

Whereas the "AI rule improvement" above improves existing rules,
this section is an implementation that **creates rules from scratch by looking only at the data**.

### Step 1: Sample Data Analysis Prompt

```typescript
// backend/lambdas/ai-agent/rule-generator.ts

interface DataProfile {
  totalRecords: number;
  fields: Array<{
    name: string;
    nullRate: number;        // 0–1: empty ratio
    uniqueRate: number;      // 0–1: unique-value ratio (closer to 1 = higher discriminating power)
    variationPatterns: string[];  // examples of detected variation patterns
    sampleValues: string[];  // 5–10 representative values
  }>;
  channelDistribution: Record<string, number>;  // record count per channel
}

async function analyzeDataAndGenerateRules(dataProfile: DataProfile): Promise<GeneratedRules> {
  const MODEL_ID = process.env.BEDROCK_MODEL_ID!;

  const prompt = `You are an expert in AWS Entity Resolution matching rules.

Analyze the customer data profile below and generate an optimal set of Entity Resolution fuzzy matching rules.

## Data Profile
- Total record count: ${dataProfile.totalRecords}
- Channel distribution: ${JSON.stringify(dataProfile.channelDistribution)}

## Per-Field Quality Analysis
${dataProfile.fields.map(f => `
### ${f.name}
- Null rate: ${(f.nullRate * 100).toFixed(1)}%
- Unique-value rate: ${(f.uniqueRate * 100).toFixed(1)}%
- Variation patterns: ${f.variationPatterns.join(', ') || 'none'}
- Sample values: ${f.sampleValues.join(' | ')}
`).join('\n')}

## Rule Generation Requirements
1. Generate a set of rules in ER Advanced Rule format as JSON
2. Each rule includes a matchingKeys array (field names identical to those above)
3. The relationship between rules is OR (if any one matches, same customer)
4. matchingKeys within a rule are AND (all keys must match)
5. For each rule:
   - The rationale for choosing this combination
   - Estimated precision
   - Estimated recall
   - Cautions (false positive risk, etc.)

## Output Format (JSON)
\`\`\`json
{
  "rules": [
    {
      "ruleName": "RuleName",
      "matchingKeys": ["field1", "field2"],
      "rationale": "Selection rationale",
      "estimatedPrecision": 0.95,
      "estimatedRecall": 0.8,
      "warnings": ["Caution"]
    }
  ],
  "overallStrategy": "Overall strategy explanation",
  "recommendedOrder": ["Order from the most precise rule"],
  "dataQualityNotes": "Notable points about data quality"
}
\`\`\``;

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
  return parseGeneratedRules(result.content[0].text);
}
```

### Step 2: Generate the Data Profile (from sample data)

```typescript
// backend/lambdas/ai-agent/data-profiler.ts

async function profileData(s3Path: string): Promise<DataProfile> {
  // 1. Load sample data from S3 (up to 1000 records)
  const records = await loadSampleRecords(s3Path, 1000);

  // 2. Per-field analysis
  const fields = Object.keys(records[0]).map(fieldName => {
    const values = records.map(r => r[fieldName]).filter(Boolean);
    const nullCount = records.length - values.length;
    const uniqueValues = new Set(values);

    // Detect variation patterns
    const patterns = detectVariationPatterns(fieldName, values);

    return {
      name: fieldName,
      nullRate: nullCount / records.length,
      uniqueRate: uniqueValues.size / values.length,
      variationPatterns: patterns,
      sampleValues: values.slice(0, 10),
    };
  });

  // 3. Channel distribution
  const channelDist: Record<string, number> = {};
  records.forEach(r => {
    const ch = r.sourcechannel || 'UNKNOWN';
    channelDist[ch] = (channelDist[ch] || 0) + 1;
  });

  return { totalRecords: records.length, fields, channelDistribution: channelDist };
}

function detectVariationPatterns(fieldName: string, values: string[]): string[] {
  const patterns: string[] = [];
  if (fieldName.includes('name')) {
    const hasKorean = values.some(v => /[\uAC00-\uD7AF]/.test(v));
    const hasEnglish = values.some(v => /^[A-Za-z\s-]+$/.test(v));
    if (hasKorean && hasEnglish) patterns.push('Korean-English mix');
    if (values.some(v => v.includes('-'))) patterns.push('contains hyphen');
    if (values.some(v => v !== v.toUpperCase() && v !== v.toLowerCase())) patterns.push('mixed case');
  }
  if (fieldName.includes('phone')) {
    if (values.some(v => v.startsWith('+82'))) patterns.push('contains country code');
    if (values.some(v => v.includes('-'))) patterns.push('contains hyphen');
  }
  if (fieldName.includes('email')) {
    if (values.some(v => /guest-.*@booking/i.test(v))) patterns.push('relay email');
  }
  return patterns;
}
```

### Step 3: HITL Validation API

```typescript
// POST /api/ai/generate-rules — generate initial rules
async function generateInitialRules(body: { dataPath: string }) {
  // 1. Data profiling
  const profile = await profileData(body.dataPath);

  // 2. Generate rules with Bedrock
  const generated = await analyzeDataAndGenerateRules(profile);

  // 3. Save to DynamoDB with PENDING status
  const suggestionId = crypto.randomUUID();
  await saveSuggestion(suggestionId, { ...generated, status: 'PENDING_REVIEW', dataProfile: profile });

  return { suggestionId, rules: generated.rules, dataProfile: profile };
}

// POST /api/ai/test-rules — run test matching with the generated rules
async function testRules(body: { suggestionId: string }) {
  const suggestion = await getSuggestion(body.suggestionId);
  // 1. Create a test ER Workflow (temporary)
  // 2. Run an ER Job on a small dataset
  // 3. Parse matching results → extract sample matched pairs
  // 4. Attach results to the suggestion
  return { matchedPairs: [], stats: { totalMatched: 0, groups: 0 } };
}

// POST /api/ai/review-rules — submit HITL feedback
async function reviewRules(body: {
  suggestionId: string;
  action: 'APPROVE' | 'MODIFY' | 'REJECT';
  feedback?: string;
  modifications?: Array<{ ruleName: string; matchingKeys: string[] }>;
}) {
  if (body.action === 'APPROVE') {
    // → Apply rules to the production ER Workflow
    await applyRulesToWorkflow(body.suggestionId);
    await updateSuggestionStatus(body.suggestionId, 'APPROVED');
  } else if (body.action === 'MODIFY') {
    // → Request re-analysis from Bedrock with the modified rules (improvement loop)
    const improved = await requestImprovement(body.suggestionId, body.feedback!, body.modifications);
    return { newSuggestionId: improved.id, rules: improved.rules };
  } else {
    await updateSuggestionStatus(body.suggestionId, 'REJECTED');
  }
}
```

### Frontend HITL Flow

```
[AI Rules page]
  ├── "Generate Rules" button → POST /api/ai/generate-rules
  │     └── Display data profile + generated rules
  │
  ├── "Run Test" button → POST /api/ai/test-rules
  │     └── List of matched pairs (user marks correct/incorrect)
  │
  ├── "Approve" button → POST /api/ai/review-rules (APPROVE)
  │     └── Applied to the ER Workflow
  │
  ├── "Request Changes" → enter feedback → POST /api/ai/review-rules (MODIFY)
  │     └── Bedrock regenerates rules incorporating the feedback (loop)
  │
  └── "Reject" → POST /api/ai/review-rules (REJECT)
```
```

In other words, the AI looks at data quality and patterns to propose optimal matching rules,
which a human validates/modifies before applying — the **AI-assisted Rule Generation + HITL** pattern.

## Core Workflow: AI Rule Generation + HITL

```
┌─────────────────────────────────────────────────────────────┐
│                                                               │
│  ① Upload sample data (extracted from CSV/Parquet/DB)         │
│       ↓                                                      │
│  ② Bedrock analysis                                           │
│     - Per-field quality (null rate, variation patterns)       │
│     - Field correlation (email+name combo uniqueness)         │
│     - Recommend optimal match key combinations                │
│       ↓                                                      │
│  ③ Auto-generate fuzzy rules                                  │
│     - Generate a rule set as ER Advanced Rules                │
│     - Estimate expected precision/recall per rule             │
│       ↓                                                      │
│  ④ HITL validation (frontend UI)                              │
│     - Display the list of generated rules                     │
│     - Preview sample matched pairs ("these two records match")│
│     - User: approve / modify / reject                         │
│       ↓                                                      │
│  ⑤ Run test matching (small dataset)                          │
│     - Run an actual ER Job (test mode)                        │
│     - Result: matched pairs + confidence                      │
│       ↓                                                      │
│  ⑥ Accuracy review                                            │
│     - Display False Positive/Negative samples                 │
│     - Collect user feedback                                   │
│       ↓                                                      │
│  ⑦ Rule improvement loop (iterative)                          │
│     - Bedrock proposes improvements from feedback             │
│     - HITL re-validation → repeat until satisfied             │
│       ↓                                                      │
│  ⑧ Final apply (to production ER Workflow)                    │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## ER Matching Types (Reference)

The target types of the rules Bedrock generates in the workflow above:
1. **Simple Rule** — exact match (when a unique identifier is certain)
2. **Advanced Rule** — fuzzy matching (generated in most cases)
3. **ML Matching** — used as a supplement when rules cannot cover the cases

## Simple Rule Matching

### Use cases
- When a unique identifier exists (membership number, national ID, employee number)
- When exact match alone is sufficient

### Pattern
```typescript
// Create ER Workflow (Lambda handler)
const createWorkflowParams = {
  workflowName: `${projectName}-simple`,
  roleArn: ER_ROLE_ARN,
  inputSourceConfig: [{
    inputSourceARN: `arn:aws:glue:${region}:${accountId}:table/${glueDbName}/${glueTableName}`,
    schemaName: `${projectName}-schema`,
  }],
  resolutionTechniques: {
    resolutionType: 'RULE_MATCHING',
    ruleBasedProperties: {
      attributeMatchingModel: 'ONE_TO_ONE',
      rules: [{
        ruleName: 'LoyaltyNumberMatch',
        matchingKeys: ['loyaltynumber'],
      }],
    },
  },
  outputSourceConfig: [{
    outputS3Path: `s3://${dataBucket}/er-output/simple/`,
    output: [
      { name: 'variantid', hashed: false },
      { name: 'firstname', hashed: false },
      { name: 'lastname', hashed: false },
      { name: 'email', hashed: false },
    ],
  }],
};
```

## Advanced Rule Matching

### Use cases (key difference: uses fuzzy comparison functions)
- Name similarity (Cosine), phone edit distance (Levenshtein), phonetic matching (Soundex), etc.
- Covers Korean-English mixing, typos, and format differences
- **Difference from Simple Rule**: Simple supports only exact match (Exact); Advanced uses fuzzy functions

### ⚠️ Important: API Format Difference

| Type | API property | Rule format |
|------|---------|-----------|
| Simple | `ruleBasedProperties` | `matchingKeys: ['field1', 'field2']` (exact match only) |
| **Advanced** | **`ruleConditionProperties`** | **`conditionString: 'Cosine(Name, 0.7) AND Exact(Email)'`** |

**Simple and Advanced use different API properties!** You cannot use fuzzy functions in `ruleBasedProperties`.

### Available Comparison Functions (conditionString)

| Function | Use | Example |
|------|------|------|
| `Exact(matchKey)` | exact match | `Exact(Email)` |
| `Cosine(matchKey, threshold)` | cosine similarity (0.0–1.0) | `Cosine(FirstName, 0.7)` |
| `Levenshtein(matchKey, threshold)` | edit distance (integer) | `Levenshtein(Phone, 3)` |
| `Soundex(matchKey)` | phonetic similarity (English) | `Soundex(LastName)` |
| `DateDifference(matchKey, days)` | date difference range | `DateDifference(DOB, 30)` |

### Condition Syntax
```
Exact(Email) AND Cosine(FirstName, 0.7) AND Cosine(LastName, 0.7)
(Exact(Phone) OR Levenshtein(Phone, 3)) AND Soundex(FirstName)
Cosine(FirstName, 0.6) AND Cosine(LastName, 0.6) AND Exact(DateOfBirth)
```

### Rule Combination Strategy (Fuzzy)

| Priority | Rule | Condition | Expected precision |
|---------|------|--------|------------|
| 1 | EmailAndNameFuzzy | `Exact(Email) AND Cosine(FirstName, 0.7) AND Cosine(LastName, 0.7)` | 95%+ |
| 2 | PhoneAndNameSoundex | `Exact(Phone) AND Soundex(FirstName) AND Soundex(LastName)` | 90%+ |
| 3 | NameFuzzyAndDOB | `Cosine(FirstName, 0.7) AND Cosine(LastName, 0.7) AND Exact(DateOfBirth)` | 85%+ |
| 4 | PhoneAndEmail | `Exact(Phone) AND Exact(Email)` | 95%+ |

### Pattern (Workflow creation in CDK/Lambda)
```typescript
const advancedWorkflowParams = {
  workflowName: `${projectName}-advanced`,
  roleArn: ER_ROLE_ARN,
  inputSourceConfig: [/* same as above */],
  resolutionTechniques: {
    resolutionType: 'RULE_MATCHING',
    // ⚠️ Advanced uses ruleConditionProperties! (not ruleBasedProperties)
    ruleConditionProperties: {
      matchPurpose: 'IDENTIFIER_GENERATION',
      rules: [
        {
          ruleName: 'EmailAndNameFuzzy',
          conditionString: 'Exact(Email) AND Cosine(FirstName, 0.7) AND Cosine(LastName, 0.7)',
        },
        {
          ruleName: 'PhoneAndNameSoundex',
          conditionString: 'Exact(Phone) AND Soundex(FirstName) AND Soundex(LastName)',
        },
        {
          ruleName: 'NameFuzzyAndDOB',
          conditionString: 'Cosine(FirstName, 0.7) AND Cosine(LastName, 0.7) AND Exact(DateOfBirth)',
        },
      ],
    },
  },
  outputSourceConfig: [/* ... */],
};
```

### Schema Cautions (When Using Advanced Fuzzy)

In `ruleConditionProperties`, the conditionString references **match key names**.
NAME-type fields cannot use `groupName`, so FirstName/LastName must be registered as separate match keys:

```typescript
// In the schema mapping:
schemaInputAttributes: [
  { fieldName: 'firstname', type: 'NAME_FIRST', matchKey: 'FirstName' },   // separate match key
  { fieldName: 'lastname', type: 'NAME_LAST', matchKey: 'LastName' },      // separate match key
  { fieldName: 'email', type: 'EMAIL_ADDRESS', matchKey: 'Email' },
  { fieldName: 'phone', type: 'PHONE_NUMBER', matchKey: 'Phone' },
  { fieldName: 'dateofbirth', type: 'DATE', matchKey: 'DateOfBirth' },
  // ...
]
// ❌ Wrong: groupName: 'FullName' (NAME type cannot be grouped)
// ✅ Correct: a separate matchKey for each
```

### Korean Name Handling Strategy

In a Korean environment there are many name variations:
- 김민호 / KIM MINHO / Kim Min-Ho / MINHO KIM
- 010-1234-5678 / +82 10-1234-5678 / 01012345678

**Recommended preprocessing**:
```typescript
function normalizeKoreanName(input: string): { firstName: string; lastName: string } {
  // 1. Remove spaces, hyphens
  // 2. Detect Korean vs English
  // 3. Korean: first char = surname, rest = given name
  // 4. English: last token = surname (considering Korean romanization order)
  // 5. Uppercase
}

function normalizePhone(input: string): string {
  // 1. Remove all non-digits
  // 2. Remove country code (+82 → 0)
  // 3. Standard format: 01012345678
}
```

## ML Matching

### Use cases
- 5 or more data sources
- PII quality varies greatly across sources
- When rules miss many matches

### Region Constraints
⚠️ **ML Matching is available only in limited regions**
- Always verify via AWS Knowledge MCP:
  `aws_get_regional_availability("entityresolution", "MLMatchingWorkflow")`

### Pattern
```typescript
const mlWorkflowParams = {
  workflowName: `${projectName}-ml`,
  roleArn: ER_ROLE_ARN,
  inputSourceConfig: [/* ... */],
  resolutionTechniques: {
    resolutionType: 'ML_MATCHING',
    // ML needs no extra configuration — it learns automatically
  },
  outputSourceConfig: [/* ... */],
};
```

### ML Matching Cost Reference
- Rule: $0.25 / 1,000 records
- ML: $1.00 / 1,000 records (4x)
- For 100K records: Rule $25 vs ML $100

## Processing Matching Results

Result-processing flow after the ER run:
```
ER Job complete → S3 output (JSON Lines) → Lambda parsing → save to DynamoDB
                                                    → CP Import (Golden Record)
```

### Result Parsing Pattern
```typescript
interface ErOutputRecord {
  matchId: string;      // same-customer group ID
  variantId: string;    // original record ID
  confidenceScore?: number; // ML only
}

// Golden Record selection: choose the most complete record within the matchId group
function selectGoldenRecord(variants: ErOutputRecord[]): GoldenRecord {
  // 1. Prefer the one with the higher field-fill ratio
  // 2. Prefer the most recent data
  // 3. Prefer trusted channels (CRM > Web > OTA)
}
```

## AI Rule Improvement (Human-in-the-Loop)

Bedrock Claude analyzes matching results and proposes rule improvements:

```
[Current rules] + [Match failure cases] → Claude analysis → [Improvement proposal]
                                                    ↓
                                            Admin review (UI)
                                              ↙        ↘
                                         Approve → apply     Reject → discard
```

```typescript
// Invoke Bedrock from the AI Agent Lambda
const prompt = `
Current Entity Resolution rules:
${JSON.stringify(currentRules)}

Match failure cases (same customer but not matched):
${JSON.stringify(falsNegatives)}

Incorrect match cases (different customers but matched):
${JSON.stringify(falsePositives)}

Analyze the cases above and propose rule improvements.
`;
```
