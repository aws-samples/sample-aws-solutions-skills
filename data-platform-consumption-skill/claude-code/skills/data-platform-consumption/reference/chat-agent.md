# Amazon Quick Chat Agent (Dataset Q&A) — Topics, Semantic Model, Test Cases

Amazon Quick chat agents (Dataset Q&A on top of Quick Sight Topics) provide natural-language Q&A grounded in topics (semantic models). The chat agent's quality depends almost entirely on:
1. The persona / system prompt
2. Synonym coverage in the topic
3. Whether speculation guardrails are enforced

> **🔴 The Topic `Description` field has a 256-character limit** (`TopicDetails.Description`, max 256). Do **NOT** paste the full persona + rules block into it — the `create-topic` call will be rejected. The verbose rules block is a **design reference for you**, not topic input. The agent's quality lives in `Columns` / `CalculatedFields` / `NamedEntities`, not the description.

---

## 1. Persona

Pick a domain expert role and write it as if briefing a new analyst:

| Domain | Persona |
|---|---|
| Manufacturing | "You are a quality analytics expert for cosmetics manufacturing. You help quality managers understand defect trends, vendor performance, and inspection outcomes." |
| Retail | "You are a sales and inventory analyst for an online bookstore. You help merchandisers understand product performance, regional trends, and customer behavior." |
| Finance | "You are a financial analyst supporting a corporate planning team. You answer questions about revenue, expenses, and budget variance using only verified ledger data." |

---

## 2. Guardrails — where they actually live

Chat-agent quality comes from the **semantic model** — column→business-term mappings, synonyms, calculated fields, named entities (below) — **not** from a long description string. Spend your effort there. Condense the guardrail that *does* go in the `Description` to one short sentence:

```
데이터 기반 답변만 제공; 예측·추측 불가. 출처(테이블/기간) 명시.
```

Keep the full rules below as the **principles to encode** — through synonym coverage, `visible: false` on raw IDs, calculated fields, and (for the speculation refusal) the one-sentence description above plus the test-case validation:

```
RULES (design reference — encode via the semantic model, do NOT paste verbatim):
1. Answer ONLY based on data available in the topic. Never speculate, estimate,
   or extrapolate beyond what the data shows.
2. If the data is insufficient to answer the question, explicitly state what
   additional data would be needed. Do not guess.
3. Always cite which dataset or table your answer comes from (e.g., "from
   mart_quality_summary, inspection_month=2025-11").
4. When showing numbers, always include the time period and any filters
   applied (e.g., "FY 2025 Q3, suppliers in 'packaging' segment").
5. If asked to predict, forecast, or estimate future values, refuse with:
   "I cannot make predictions — I can only summarize historical data. To
    forecast, please use a forecasting tool with this data as input."
6. If the user's terminology doesn't match the data dictionary, ask a
   clarifying question rather than guessing.
```

---

## 3. Topic creation

> Verify against the latest Quick Sight CLI reference before running — the topic API structure (`Name`, `DataSets`, `NamedEntities`, etc.) evolves. Run `aws quicksight create-topic help` (CLI namespace is still `quicksight`) and confirm the `--topic` JSON shape matches the current shape.

> ⚠️ `Description` must be **≤ 256 characters**. Keep it to the one-sentence guardrail — do NOT paste the persona/rules block.

```bash
aws quicksight create-topic \
  --aws-account-id {account_id} \
  --topic-id "{prefix}-quality" \
  --topic '{
    "Name": "품질 관리 토픽",
    "Description": "데이터 기반 답변만 제공; 예측·추측 불가. 출처(테이블/기간) 명시.",
    "DataSets": [{
      "DatasetArn": "arn:aws:quicksight:{region}:{account}:dataset/{prefix}-quality-inspections",
      "DatasetName": "Quality Inspections",
      "DatasetDescription": "월별 검사 결과 및 불량 데이터",
      "Filters": [],
      "Columns": [/* see Semantic Model below */],
      "CalculatedFields": [/* see Semantic Model below */],
      "NamedEntities": [/* see Semantic Model below */]
    }]
  }' \
  --region {aws_region}
```

---

## 4. Test questions (generated from `business_questions`)

For each customer domain, generate at least 5 test questions covering these categories:

| Category | Example (Manufacturing) | Expected behavior |
|---|---|---|
| Simple lookup | "2024년 검사 건수는?" | Returns single number with period |
| Trend analysis | "월별 불량률 추세를 보여줘" | Returns time series, cites table |
| Comparison | "거래처별 불량 TOP 5" | Returns top-N with sort and limit |
| Filter combination | "2025년 1분기에 포장검사 불량률" | Multi-condition filter |
| Refusal — speculation | "다음 달 불량률을 예측해줘" | Refuses per rule 5, suggests forecasting tool |
| Refusal — out-of-scope | "이 거래처의 신용등급은?" | States data not in topic, suggests where to look |

Document these as a `test-cases.md` alongside the topic so the customer can re-validate after schema changes.

### Response validation checklist

After topic creation, validate **in the Quick Sight console** (or hand the test cases to the customer) — there is no API to pose an NL question to a Topic and grade the answer. For each test question verify:
- [ ] Agent uses data from the cited table
- [ ] Numbers include time period and filters
- [ ] Refusal questions are refused (not answered with speculation)
- [ ] Synonyms work (Korean and English variations)
- [ ] Out-of-scope questions explain what's missing rather than guessing

If any check fails, iterate on synonyms (below) before iterating on the persona.

> **Why not via the API:** there is no public API to pose an NL question to a Topic and score the response. `create-topic-refresh-schedule` exists (Topics auto-index from SPICE on a schedule), but there is no ad-hoc `create-topic-refresh` trigger and no NL-query/grade API. Do not claim to have "run the chat tests" — produce the test cases and validate in-console or hand them off.

---

## 5. Semantic model / Topic definition

The semantic model bridges raw column names and business questions. Three pieces.

### A. Column → business term mapping

```yaml
# Saved to semantic-model.yaml alongside the topic
columns:
  supplier_id:
    business_name: "공급업체 ID"
    description: "Unique identifier for supplier"
    visible: false  # hide IDs from natural language, expose names
  supplier_name:
    business_name: "공급업체"
    synonyms: ["거래처", "supplier", "vendor"]
    description: "Supplier name"
  product_code:
    business_name: "제품 코드"
    synonyms: ["제품코드", "product code", "품번"]
    visible: false
  product_name:
    business_name: "제품명"
    synonyms: ["제품", "product", "품목"]
  inspection_type_name:
    business_name: "검사 유형"
    synonyms: ["검사종류", "inspection type", "검사 유형명"]
  defect_count:
    business_name: "불량 건수"
    synonyms: ["불량", "defect count", "결함 수"]
    aggregation: SUM
  total_count:
    business_name: "검사 건수"
    synonyms: ["전체 검사", "inspection count", "총 검사"]
    aggregation: SUM
  defect_rate_pct:
    business_name: "불량률"
    synonyms: ["불량률", "defect rate", "결함률"]
    aggregation: AVERAGE
    format: "percent_2_decimal"
  inspection_month:
    business_name: "검사월"
    synonyms: ["검사 시간", "inspection month", "검사월자"]
    semantic_type: DATE
```

### B. Calculated fields

Push business logic into the dataset, not into each visual:

```sql
-- defect_rate_pct already in the view, but topic adds derived metrics:
"YoY 불량률 변화" = ({defect_rate_pct} - LAG({defect_rate_pct}, 12) OVER (ORDER BY {inspection_month})) / LAG({defect_rate_pct}, 12) OVER (ORDER BY {inspection_month})

"Pass Rate" = 100 - {defect_rate_pct}

"Pass Count" = {total_count} - {defect_count}
```

### C. Named entities & metrics

Metrics are columns the chat agent treats as first-class:

```yaml
metrics:
  total_defects:
    expression: "SUM({defect_count})"
    name: "총 불량 건수"
    synonyms: ["전체 불량", "불량 합계"]
    filterable_by: [supplier_name, inspection_month, inspection_type_name]
  avg_defect_rate:
    expression: "AVG({defect_rate_pct})"
    name: "평균 불량률"
    synonyms: ["평균 불량 비율"]

named_entities:
  supplier:
    columns: [supplier_id, supplier_name]
    primary_column: supplier_name
    synonyms: ["거래처", "공급업체", "supplier", "vendor"]
  product:
    columns: [product_code, product_name]
    primary_column: product_name
    synonyms: ["제품", "품목", "product"]
  inspection:
    columns: [inspection_month, inspection_type_name]
    primary_column: inspection_type_name
    synonyms: ["검사", "inspection"]
```

### Synonym coverage rule

For every business question in the input, identify the key terms and ensure each has at least 2 synonyms in the topic:
- Original business term ("불량률")
- English equivalent ("defect rate")
- Common variant ("결함률")

Insufficient synonyms is the **#1 cause of poor chat agent quality**. Spend time here.

---

## 6. Per-Space chat persona

The chat agent persona can differ per Space:
- Quality team Space: "quality analytics expert" persona
- Sales team Space: "sales analyst" persona
- Leadership Space: "executive briefing analyst — concise, headline-oriented"

Set by creating **one Topic per Space**, each Topic with its own description (carrying that Space's persona + condensed guardrail). Space layout + group CDK → `iam-permissions.md` §9.

---

## 7. Extending chat coverage (from real user feedback)

1. Identify the gap from real user feedback (questions the agent failed to answer).
2. For each failed question, decide:
   - **Missing synonym** → add to topic's `NamedEntities`
   - **Missing calculated field** → add to dataset
   - **Missing data** → goes back to the pipeline layer (out of scope here)
   - **Speculation drift** → strengthen the rules encoded via the semantic model + description
3. Re-test the failure case + a regression set of previously-working questions.
