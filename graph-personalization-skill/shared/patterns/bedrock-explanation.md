# Bedrock Explanation Patterns

> Explain graph results in natural language with Bedrock Claude. **Privacy-preserving** (no user ID exposure) + **prompt caching** (lower cost) + **multilingual** (Korean + English).

## Core principles

1. **Aggregation only** — phrasing like "liked by 5 similar users". Never expose IDs directly.
2. **Top-N limit** — only top 20 in the Bedrock context. The rest burdens the prompt.
3. **Caching** — when the system prompt + few-shot exceed 100K tokens, enable `cache_control: ephemeral` (90% cost reduction).
4. **Industry-aware** — the prompt differs per domain (e-commerce / media / B2B).
5. **Korean / English** — detect the user's language + respond in the same tone.
6. **Fallback** — on Bedrock throttle / error, return a simple score-based response (degrade gracefully).

> The example prompts below target a Korean-serving recommender, so they instruct the model to answer in Korean and the sample product/genre names are kept in Korean as data values. Adapt the response language to your audience.

## System prompt — E-commerce

```python
ECOMMERCE_SYSTEM_PROMPT = """You are an assistant that explains e-commerce recommendation results in natural language.

## Output rules
1. Never expose other users' IDs, names, or personal information.
2. Use aggregation phrasing only, e.g. "similar pattern", "N similar users".
3. Respond in 2-4 sentences, matching the user's language (natural Korean, or English if the user asks in English).
4. Reason for the recommendation + one call-to-action (CTA) sentence.
5. Avoid excessive ML/technical jargon (e.g. do not use "GNN", "embedding").
6. Do not hallucinate any information about the recommended products beyond the facts given.

## Tone
- Friendly but professional
- Address the user politely (between formal and casual)
- Not overly marketing-heavy

## Input format
Top 10 products recommended to the user + recommendation scenario (collaborative / cross-sell / content-based).

## Output format
{
  "explanation": "natural-language explanation",
  "reason_tag": "similar-buyers | category-preference | trending | new-arrival"
}

## Example
Input:
- scenario: collaborative
- products: ["Widget A (score 5.2)", "Widget B (score 4.8)", ...]
- user segment: VIP

Output:
{
  "explanation": "These are products frequently bought together by VIP members with purchase patterns similar to yours. Widget A in particular is a popular item bought 4.5x more often than average — take a look.",
  "reason_tag": "similar-buyers"
}
"""
```

## Bedrock invoke — with caching

```python
import os, json, boto3

bedrock = boto3.client('bedrock-runtime', region_name=os.environ['AWS_REGION_NAME'])
MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'us.anthropic.claude-sonnet-4-20250514-v1:0')


def generate_explanation(items: list[dict], scenario: str, industry: str, user_segment: str = None) -> dict:
    """
    Generate natural-language explanation for recommendation results.
    Privacy-preserving: never includes user IDs in prompt.
    """
    # 1. Top 20 limit
    top_items = items[:20]

    # 2. Format for prompt (privacy-aware)
    items_text = "\n".join([
        f"- {item['name']} (score {item['score']:.2f})"
        for item in top_items
    ])

    user_context = f"user segment: {user_segment}" if user_segment else "user: (no segment info)"

    user_prompt = f"""## Recommendation scenario
{scenario}

## {user_context}

## Top {len(top_items)} recommended products
{items_text}

Explain the results above in natural language. Respond in JSON format."""

    # 3. Load system prompt (industry-specific)
    system_prompt = INDUSTRY_PROMPTS[industry]

    # 4. Bedrock invoke with caching
    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},   # ★ 5min cache
                    },
                ],
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
            }),
        )
        result = json.loads(response['body'].read())
        text = result['content'][0]['text']

        # Parse JSON from response
        try:
            parsed = json.loads(text)
            return {
                'explanation': parsed['explanation'],
                'reason_tag': parsed.get('reason_tag', 'general'),
            }
        except json.JSONDecodeError:
            # When Bedrock responds with plain text
            return {'explanation': text, 'reason_tag': 'general'}

    except Exception as e:
        print(f"Bedrock error: {e}")
        # Fallback — simple score-based response
        return {
            'explanation': f"Products preferred by {user_segment or 'similar users'}.",
            'reason_tag': 'fallback',
        }
```

## System prompt — Media

```python
MEDIA_SYSTEM_PROMPT = """You are an assistant that explains media content recommendations in natural language.

## Output rules
1. Do not expose other users' IDs or names.
2. Use aggregation phrasing only, e.g. "N people you follow", "similar viewing patterns".
3. State only content titles, genres, and cast (objective facts).
4. Respond in 2-4 sentences, in natural Korean or English.
5. No content spoilers.

## Tone
- Friendly, polite address, curator-like trustworthiness

## Recommendation scenarios
- friend-watched: content watched by people the user follows
- genre-affinity: new releases in preferred genres
- person-affinity: other works by a preferred author/director

## Output format
{
  "explanation": "natural-language explanation",
  "reason_tag": "friend-watched | genre-affinity | person-affinity"
}

## Example
Input:
- scenario: friend-watched
- content: ["Movie A (genre: 스릴러)", "Movie B (genre: 드라마)"]
- user ageGroup: 30s

Output:
{
  "explanation": "These are titles recently finished by 5+ people you follow. Movie A in particular is a hit with an 80%+ completion rate.",
  "reason_tag": "friend-watched"
}
"""
```

## System prompt — B2B SaaS

```python
B2B_SYSTEM_PROMPT = """You are an assistant that explains B2B SaaS sales / cross-sell recommendations.

## Output rules
1. Never expose other company names or user IDs.
2. Use aggregation phrasing only, e.g. "N larger companies in the same industry", "customers of similar size".
3. Emphasize business value — outcomes such as ROI, productivity, security.
4. Respond in 3-5 sentences, in a business (formal) tone.
5. Make the CTA clear ("upgrade to the Pro plan", "request a demo").

## Tone
- Professional, with the clarity of a sales document
- English is fine (English is common in B2B)

## Scenarios
- peer-adoption: used by larger customers in the same industry
- upgrade-readiness: already using advanced features relative to the current plan
- cross-sell: used-together patterns

## Output format
{
  "explanation": "...",
  "reason_tag": "peer-adoption | upgrade-readiness | cross-sell",
  "suggested_action": "upgrade-plan | request-demo | book-meeting"
}
"""
```

## Few-shot examples (boosts caching benefit)

Adding 5-10 few-shot examples to the system prompt improves the LLM's tone/format consistency. With caching applied, there is no extra cost burden.

```python
ECOMMERCE_FEW_SHOTS = """
## Example 1 — Collaborative
Input:
- scenario: collaborative
- products: ["스킨 토너 (5.2)", "선크림 SPF50 (4.8)", "립밤 (3.5)"]
- segment: VIP

Output:
{
  "explanation": "These are items frequently bought together by 7 VIP members with skincare purchase patterns similar to yours. 스킨 토너 in particular is a popular item with a high repurchase rate.",
  "reason_tag": "similar-buyers"
}

## Example 2 — Cross-sell
Input:
- scenario: cross-sell
- base product: 카메라 렌즈
- recommended products: ["렌즈 케이스 (12)", "필터 (8)", "마이크 (5)"]

Output:
{
  "explanation": "These accessories were bought together by 12+ people who purchased this lens. They help protect the lens and improve video quality.",
  "reason_tag": "frequently-bought-together"
}

## Example 3 — Cold start (Popular)
Input:
- scenario: popular
- products: ["베스트셀러 토너 (200)", "히트 세럼 (150)"]
- segment: NewUser

Output:
{
  "explanation": "These are bestsellers popular with first-time visitors. 200+ new customers chose these as their first purchase.",
  "reason_tag": "popular"
}
"""

ECOMMERCE_SYSTEM_PROMPT_FULL = ECOMMERCE_SYSTEM_PROMPT + "\n" + ECOMMERCE_FEW_SHOTS
```

## Multilingual handling

```python
def detect_language(user_prompt: str) -> str:
    """Simple detection: if Korean char ratio is 30%+, return ko."""
    korean_chars = sum(1 for c in user_prompt if '가' <= c <= '힣')
    if korean_chars / max(len(user_prompt), 1) > 0.3:
        return 'ko'
    return 'en'


def generate_explanation_multilingual(items, scenario, industry, user_lang='auto'):
    if user_lang == 'auto':
        # Decide from the event's user prompt or the lang header sent by the frontend
        user_lang = 'ko'   # default

    system_prompt = INDUSTRY_PROMPTS[industry]
    if user_lang == 'en':
        system_prompt += "\n\n## Language: Respond in English."
    else:
        system_prompt += "\n\n## Language: Respond in Korean."

    return generate_explanation(items, scenario, industry, system_prompt=system_prompt)
```

## GraphRAG — using graph context in chat

Optional endpoint (`POST /chat`):

```python
GRAPH_RAG_SYSTEM_PROMPT = """You are a graph-based recommendation chatbot. Use the graph results to answer the user's question.

## Available graph context
user ID = $userId
- products the user viewed in the last 30 days (top 10): ...
- products bought together by 5 users with purchase patterns similar to the user (top 10): ...
- the user's top 3 preferred categories: ...

## Rules
1. Never expose other users' IDs.
2. For information not in the graph, answer "no information" or "hard to confirm".
3. Answer in 2-5 sentences, in friendly Korean.

## User question
{user_question}
"""

def handle_chat(user_id, question):
    # 1. Graph context fetch (parallel)
    recent_views = neptune.run(QUERIES['recent_views'], userId=user_id, limit=10)
    similar_recs = neptune.run(QUERIES['collaborative'], userId=user_id, limit=10)
    pref_cats = neptune.run(QUERIES['top_categories'], userId=user_id, limit=3)

    # 2. Format
    context = format_graph_context(recent_views, similar_recs, pref_cats)

    # 3. Bedrock invoke
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": [{"type": "text", "text": GRAPH_RAG_SYSTEM_PROMPT.format(userId=user_id), "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": f"## Graph context\n{context}\n\n## Question\n{question}"},
            ],
        }),
    )
    return json.loads(response['body'].read())['content'][0]['text']
```

## Cost estimation per request

```
Sonnet 4 Standard:
  Input:  ~2K tokens (system + user prompt)  × $3/M = $0.006
  Output: ~500 tokens (explanation)           × $15/M = $0.0075
  Total: ~$0.014 per request

With caching (system prompt 5K cached):
  Input cache hit:  5K × $3/M × 10% = $0.0015
  Input non-cache:  ~500 tokens × $3/M = $0.0015
  Output: ~500 tokens × $15/M = $0.0075
  Total: ~$0.011 per request (20% saved)

Haiku 4.5 (cost-optimized):
  Total: ~$0.003 per request (5x cheaper than Sonnet 4)
```

→ 10K rec/day = monthly cost:
- Sonnet 4 (caching): ~$3,300/mo (★ ~$1,650 with caching applied)
- Haiku 4.5: ~$900/mo

## Privacy validation checklist

```python
def validate_explanation_privacy(text: str, raw_user_ids: list[str]) -> bool:
    """Verify the LLM response does not expose any user ID."""
    text_lower = text.lower()
    for user_id in raw_user_ids:
        if user_id.lower() in text_lower:
            return False  # ★ ID leak
    # Additional validation for email pattern, phone pattern, etc.
    if re.search(r'\b\w+@\w+\.\w+\b', text):
        return False
    return True


def safe_generate_explanation(items, scenario, raw_user_ids):
    explanation = generate_explanation(items, scenario, ...)
    if not validate_explanation_privacy(explanation['explanation'], raw_user_ids):
        # Privacy violation — fallback
        return {'explanation': 'Products preferred by similar users.', 'reason_tag': 'fallback'}
    return explanation
```

## Bedrock Guardrails (hardening)

```typescript
// CDK
const guardrail = new bedrock.CfnGuardrail(this, 'RecGuardrail', {
  name: `${projectName}-rec-guardrail`,
  blockedInputMessaging: 'Your input contains inappropriate content.',
  blockedOutputsMessaging: 'A response could not be generated.',
  contentPolicyConfig: {
    filtersConfig: [
      { type: 'SEXUAL',       inputStrength: 'HIGH', outputStrength: 'HIGH' },
      { type: 'VIOLENCE',     inputStrength: 'HIGH', outputStrength: 'HIGH' },
      { type: 'HATE',         inputStrength: 'HIGH', outputStrength: 'HIGH' },
      { type: 'INSULTS',      inputStrength: 'MEDIUM', outputStrength: 'HIGH' },
      { type: 'MISCONDUCT',   inputStrength: 'HIGH', outputStrength: 'HIGH' },
      { type: 'PROMPT_ATTACK', inputStrength: 'HIGH' },
    ],
  },
  sensitiveInformationPolicyConfig: {
    piiEntitiesConfig: [
      { type: 'EMAIL',           action: 'BLOCK' },
      { type: 'PHONE',           action: 'BLOCK' },
      { type: 'CREDIT_CARD_NUMBER', action: 'BLOCK' },
      // Korean resident registration numbers, etc. via regex pattern
    ],
    regexesConfig: [
      { name: 'KoreanRRN', pattern: '\\d{6}-[1-4]\\d{6}', action: 'BLOCK' },
    ],
  },
});

// Apply the guardrail when invoking the Lambda
bedrock.invoke_model({
  modelId: ...,
  guardrailIdentifier: guardrail.attrGuardrailId,
  guardrailVersion: 'DRAFT',
  body: ...,
});
```

## Pitfall avoidance (see constraints #4, #7, #16)

| Pitfall | Handling |
|---|---|
| User ID exposure | aggregation only, validate_explanation_privacy check |
| Context size | top 20 + summary |
| Token cost | Prompt caching (5min ephemeral) |
| Throttling | Lambda retry + DLQ |
| Hallucination | only facts from items in the prompt, ratings stated explicitly |
| Korean token 2x | top 20 in Korean still uses more tokens — Haiku 4.5 fallback |
