# Eval: Retail Scenario

## Input Prompt
```
Build a unified customer system for e-commerce.
- 5 channels: web, app, POS, call center, marketplace
- Order/item/membership/preference data
- Need to handle guest purchases (non-members)
- RFM segment analysis
- No Graph needed, Kinesis real-time ingestion needed
- Region: ap-northeast-2
```

## Expected Output Checklist

### Architecture decisions
- [ ] Single CP Domain
- [ ] Graph: disabled
- [ ] Kinesis ingestion mode (real-time requirement)
- [ ] ~$100-200/month cost range

### Generated files
- [ ] `config/schema.yaml` — tailored for retail
- [ ] `lib/ingestion-stack.ts` — includes Kinesis Stream + EventBridge Pipe
- [ ] `frontend/src/pages/` — includes RFM segment page

### ER strategy (retail-specific)
- [ ] EmailOnly rule included (e-commerce core)
- [ ] LoyaltyNumber rule
- [ ] NameAndPhone (POS support)
- [ ] Guest purchase handling logic (when only email is available)
- [ ] Marketplace relay email detection

### Object Types
- [ ] Order (order header)
- [ ] OrderLineItem (order details)
- [ ] LoyaltyMembership (membership)
- [ ] Preferences (preferences)

### Calculated Attributes
- [ ] TotalSpendInAYear
- [ ] OrderCountInAYear
- [ ] AverageOrderValue
- [ ] MostRecentOrderDate
- [ ] LifetimeItemCount

### Frontend-specific
- [ ] RFM segment visualization
- [ ] Purchase category analysis
- [ ] AOV trend chart
