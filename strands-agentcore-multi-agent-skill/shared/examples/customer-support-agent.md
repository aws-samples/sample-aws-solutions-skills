# Example: Customer Support Agent

> Call-center/CS multi-agent — a combination of **CRM (Salesforce/Zendesk MCP) + Knowledge Base + Order Lookup (Specialized Agent)**. Dynamically selects tools based on the user's answers while leveraging per-user history (Memory).

## User Answers (Discovery)

| Question | Answer |
|---|---|
| 1. Which domain? | E-commerce customer support |
| 2. External systems | Zendesk (tickets), Shopify (orders) |
| 3. Specialized agent needed? | Yes — multiple steps when looking up orders (order validation → shipment tracking → ETA prediction) |
| 4. KB | Yes — product manuals, FAQ, refund policy |
| 5. Memory | semantic + user_preference (accumulates repeat-customer info) |
| 6. Region | us-east-1 |
| 7. Model | Orchestrator: Sonnet 4 / Sub-agent: Sonnet 4 |
| 8. Number of tools | ~10 (Zendesk 5, Order sub-agent 1, KB 1) → SEMANTIC search |

## Generated Stack Composition

```
generated-project/
├── cdk-infra/
│   └── src/stacks/
│       ├── orchestrator_agent_stack.py
│       ├── zendesk_mcp_stack.py
│       ├── order_lookup_agent_stack.py
│       ├── knowledge_base_stack.py        ← product manuals + FAQ + refund policy
│       └── agentcore_gateway_stack.py
├── agents/
│   ├── orchestrator-agent/
│   └── order-lookup-agent/
│       ├── order_lookup_agent.py          ← @tool: get_order, track_shipment, estimate_eta, validate_return
│       ├── Dockerfile
│       └── requirements.txt
├── mcp-servers/
│   └── zendesk-mcp/
│       └── zendesk_mcp.py                 ← @mcp.tool: search_tickets, get_ticket, create_ticket, add_comment, get_user
└── frontend/                              ← support agent UI (customer info in sidebar)
```

## Orchestrator routing table

```
| Intent     | Keywords                                             | Action                |
|------------|------------------------------------------------------|------------------------|
| TICKET     | ticket, support, complaint, issue, refund, request   | zendesk_* MCP tools   |
| ORDER      | order, shipment, tracking, delivery, status, eta     | lookup_order tool     |
| KNOWLEDGE  | how, what, manual, guide, policy, refund             | answer_general_questions |
| MULTI      | full picture, all info, escalate, summarize          | sequence: zendesk + order + kb |
```

## Memory Strategy

```python
# Memory definition in the Orchestrator stack
agentcore.Memory(
    self, "Memory",
    memory_name="cs_orchestrator_memory",
    expiration_duration=Duration.days(180),               # retain for half a year
    memory_strategies=[
        agentcore.MemoryStrategy.using_built_in_semantic(),       # search past similar cases
        agentcore.MemoryStrategy.using_built_in_user_preference(),# customer preferences (e.g., response language)
    ],
)
```

→ Even if the same customer returns days later, the **semantic context of the previous conversation** is automatically prepended.

## Demonstration Scenarios

### 1. Simple ticket lookup

```
User: "What's the status of my ticket #12345?"
→ Orchestrator: intent=TICKET → get_ticket(12345)
→ Zendesk MCP → Zendesk API → ticket details
→ "Your ticket #12345 is In Progress, last updated 2 hours ago by Sarah."
```

### 2. Shipment tracking (Specialized agent)

```
User: "Where is my order #ABC-456?"
→ Orchestrator: intent=ORDER → lookup_order("Where is my order #ABC-456?")
→ OrderLookup Strands Agent:
   1. get_order(ABC-456) → status, shipment_id
   2. track_shipment(shipment_id) → carrier, current_location, ETA from carrier API
   3. estimate_eta(...)            → in-house ML or external API
   4. synthesize: "Your order shipped via FedEx, currently in Memphis, ETA tomorrow 5 PM."
→ Orchestrator passes the result through as-is
```

### 3. KB + ticket combination

```
User: "Can I return a damaged item bought 30 days ago?"
→ Orchestrator: intent=MULTI →
   1) answer_general_questions("return policy") → KB result: "damaged items are refundable within 30 days"
   2) Optional: search_tickets(jql="customer=user_xyz AND created>=30d ago") → check related tickets
→ "Yes, our policy allows refund of damaged items within 30 days. ..."
```

### 4. Memory + escalation

```
[From a past conversation (2 weeks ago): customer "John Doe" complained about a shipping delay + escalated via ticket #1111]

User (today): "Has my issue been resolved?"
→ Memory semantic search finds the "shipping delay" context
→ Orchestrator: pronouns "my issue" → resolve to ticket #1111
→ get_ticket(1111) → status=Resolved
→ "Yes, ticket #1111 (shipping delay) is now Resolved. The replacement was delivered on Apr 21."
```

## Key Learning Points

1. **Memory semantic strategy is the key to long-term user context** — different from RAG: semantic search isolated per user/session.
2. **The sub-agent encapsulates multi-step external API calls** — the Orchestrator just calls "lookup_order" once and is done.
3. **MULTI intent generalizes from 1–2 examples in the system prompt** — the Strands LLM decides the tool sequence itself.
4. **KB + external-system integration** — naturally synthesizes static policy (KB) + dynamic data (external API).

## Cost Estimate (1 brand, 500 ticket-related queries/day, monthly)

| Item | $ |
|---|---|
| Runtime (Orchestrator + 1 sub-agent + 1 MCP) | ~$30 |
| Bedrock Sonnet 4 (500 query × avg 4K tokens) | ~$50 |
| Memory (semantic + user_pref, 180d expiry, 500 query/d × ~10 events/query) | ~$50 |
| KB (OpenSearch Serverless 2 OCU) | ~$345 |
| Cognito (1000 MAU agents) | ~$5 |
| Zendesk API | ($0 additional — covered by Zendesk's own plan cost) |
| **Total** | **~$480/mo** |

## Pitfalls / Cautions

| Pitfall | Mitigation |
|---|---|
| Memory prepends another customer's info | Correctly isolate `actor_id` as the customer ID |
| Sub-agent needs separate auth for each external API | Add `secretsmanager:GetSecretValue` to the sub-agent's IAM role, mapping ARN patterns per secret |
| KB results too generic | Reduce chunking_strategy to `max_tokens=300, overlap=30%` and use metadata filters |
| Responses too long for users to read | Specify "Keep responses < 200 words unless asked for details" in the system prompt |
| Sensitive PII (order address, etc.) exposure | Apply a Bedrock Guardrail before returning KB search results / DB rows |

## Additional Data-Protection Recommendations

- **Bedrock Guardrails** for PII redaction:
  ```python
  bedrock_model = BedrockModel(
      model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
      client=bedrock_client,
      guardrail_id="<guardrail-id>",
      guardrail_version="DRAFT",
  )
  ```
- **Memory event_expiry_days = 180**, but establish a procedure to call `delete_memory(memory_id, actor_id=customer)` for GDPR / CCPA deletion requests.
- **Zendesk API token rotation** — Secrets Manager + Lambda automatic rotation.
