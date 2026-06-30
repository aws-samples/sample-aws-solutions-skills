# AI Commentary Integration

> **Verified against AWS docs (2026-06-08):**
> - `UpdateDashboard` accepts a full `Definition` (Sheets → Visuals → InsightVisual → InsightConfiguration → CustomNarrative). Updating creates a new version but does **not** publish it.
> - `CustomNarrativeOptions.Narrative` is a required String, **max length 150000** chars.
> - `UpdateDashboardPublishedVersion` is the correct API to publish a version (takes `VersionNumber`).
> - No external database is required — the narrative is stored in the dashboard definition; version history is automatic.

## AI Commentary Integration (Bedrock → Quick Sight InsightVisual)

### When to use — decision tree (follow this exactly)

```
고객이 대시보드에 텍스트 코멘트를 원하는가?
├── NO → skip (차트/KPI만)
└── YES → 어떤 종류?
    ├── 영어 OK + 정형 수치 요약만 (최대/최소/증감률)
    │   → Classic Narratives (InsightVisual + Computations)
    │   → reference/dashboard-definitions.md의 InsightVisual 참조
    │
    └── 한국어 필요 OR 자유형 분석 (원인 추론, 비교, 권장 조치)
        → Bedrock Lambda (이 파일의 Architecture 따름)
        → Korean explicitly NOT supported by Classic Narratives (AWS docs)
```

> **This is a USER-facing decision — present the choice and get approval before building** (see the escalation table in `SKILL.md`). The customer sees the result inside the dashboard, so the agent does not pick the approach silently.

In one line: when the customer needs auto-generated Korean AI commentary (e.g., 전일대비 GAP 분석, 매체별 비교 코멘트) or free-form analysis that goes beyond Quick Sight's native Narratives capability, take the Bedrock branch.

### Why not Classic Narratives alone?
- Classic Narratives = **template-based, NOT LLM-generated** (no free-form reasoning)
- **Korean text generation: ❌ explicitly NOT supported** — AWS docs state the language feature "does not translate ML Insights, suggested insights, or computations in narratives"
- Free-form analysis: ❌ only formulaic stats (max, min, top-N) — no cause inference, comparison, or recommended actions

### Architecture
```
EventBridge (daily cron, e.g., 08:00 KST)
    ↓
Lambda function
    ↓
① Athena query: fetch yesterday vs today KPIs
② Bedrock (Claude Sonnet): generate Korean commentary
③ QuickSight UpdateDashboard API: inject text into InsightVisual.CustomNarrative.Narrative
④ QuickSight UpdateDashboardPublishedVersion: publish new version
    ↓
User opens dashboard → sees fresh AI commentary natively inside Quick Sight
```

### No external database needed
- Commentary is stored IN the Quick Sight dashboard definition (Narrative field, 150K char limit)
- Version history is automatic (Quick Sight tracks dashboard versions)
- Optional: Lambda can also save to S3 (`{date}.json`) for archival

### CDK for the Lambda + EventBridge trigger
```typescript
const commentaryLambda = new lambda.Function(this, 'AiCommentary', {
  runtime: lambda.Runtime.PYTHON_3_12,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/ai-commentary'),
  timeout: cdk.Duration.minutes(5),
  environment: {
    DASHBOARD_ID: `${prefix}-dashboard`,
    WORKGROUP: `${prefix}-workgroup`,
    MODEL_ID: 'anthropic.claude-sonnet-4-20250514',
    INSIGHT_VISUAL_ID: 'ai-commentary-widget',
  },
});

// Grant permissions
commentaryLambda.addToRolePolicy(new iam.PolicyStatement({
  actions: ['quicksight:UpdateDashboard', 'quicksight:UpdateDashboardPublishedVersion', 'quicksight:DescribeDashboardDefinition'],
  resources: [`arn:aws:quicksight:*:*:dashboard/${prefix}-*`],
}));
commentaryLambda.addToRolePolicy(new iam.PolicyStatement({
  actions: ['bedrock:InvokeModel'],
  resources: ['arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-20250514'],
}));
commentaryLambda.addToRolePolicy(new iam.PolicyStatement({
  actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults'],
  resources: ['*'],
}));

// Daily trigger
new events.Rule(this, 'DailyCommentary', {
  schedule: events.Schedule.cron({ hour: '23', minute: '0' }), // 08:00 KST
  targets: [new targets.LambdaFunction(commentaryLambda)],
});
```

### Lambda handler sketch (Python)
```python
import boto3, json, os

def handler(event, context):
    athena = boto3.client('athena')
    bedrock = boto3.client('bedrock-runtime')
    qs = boto3.client('quicksight')
    account_id = context.invoked_function_arn.split(':')[4]

    # 1. Query yesterday vs today KPIs
    kpi_data = run_athena_query(athena, "SELECT ...")  # your KPI comparison query

    # 2. Generate Korean commentary via Bedrock
    prompt = f"""다음 광고 성과 데이터를 분석하여 한국어로 전일대비 GAP 분석 코멘트를 작성해주세요.
    데이터: {json.dumps(kpi_data, ensure_ascii=False)}
    규칙: 추측하지 말고 데이터에 기반한 분석만 작성. 마케팅 전문 용어 사용."""

    response = bedrock.invoke_model(
        modelId='anthropic.claude-sonnet-4-20250514',
        body=json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 2000})
    )
    commentary = json.loads(response['body'].read())['content'][0]['text']

    # 3. Get current dashboard definition
    definition = qs.describe_dashboard_definition(
        AwsAccountId=account_id,
        DashboardId=os.environ['DASHBOARD_ID']
    )['Definition']

    # 4. Find and update the InsightVisual's Narrative
    for sheet in definition['Sheets']:
        for visual in sheet.get('Visuals', []):
            if 'InsightVisual' in visual:
                insight = visual['InsightVisual']
                if insight['VisualId'] == os.environ['INSIGHT_VISUAL_ID']:
                    insight['InsightConfiguration']['CustomNarrative'] = {
                        'Narrative': commentary
                    }

    # 5. Update dashboard + publish
    qs.update_dashboard(AwsAccountId=account_id, DashboardId=os.environ['DASHBOARD_ID'], Definition=definition, Name=...)
    # Get latest version number and publish
    versions = qs.list_dashboard_versions(AwsAccountId=account_id, DashboardId=os.environ['DASHBOARD_ID'])
    latest = max(v['VersionNumber'] for v in versions['DashboardVersionSummaryList'])
    qs.update_dashboard_published_version(AwsAccountId=account_id, DashboardId=os.environ['DASHBOARD_ID'], VersionNumber=latest)
```

> **Note:** `Narrative` is required and capped at 150000 chars — truncate Bedrock output before injecting. `UpdateDashboard` requires `Name` (max 2048 chars), so pass the existing dashboard name. The narrative-injection update is the same 3-call flow documented in `dashboard-patterns.md` §6 (update → permissions → publish); permissions are unchanged here so only update + publish are needed.

### Prompt design tips (for Korean marketing commentary)
- Include the customer's 마케팅 용어집 in the system prompt
- Specify tone: "광고주 보고서 스타일, 객관적, 데이터 기반"
- Include period context: "2026년 6월 7일 기준 전일(6/6) 대비 분석"
- Request structured output: "요약 → GAP 원인 분석 → 권장 조치" format

### Limitations
- InsightVisual cannot render HTML/markdown — plain text only
- No real-time updates (batch, cron-based)
- Bedrock per-token cost applies (~$0.003 per 1K output tokens for Sonnet)
- If dashboard STRICT validation fails after update, the publish step will error — catch and log
