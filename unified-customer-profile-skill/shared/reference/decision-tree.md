# Decision Tree — Feature & Strategy Selection

This document describes the decision logic for which features/strategies to choose based on user requirements.

## 1. Choosing the Ingestion Mode

```
Q: Where does the customer data currently live?
│
├─ In an existing DB (RDS, Aurora, Redshift, DynamoDB, on-prem DB)
│   └─ mode: glue_connection
│       └─ Glue Connection + Crawler → Glue Table → ER input
│       └─ JDBC (MySQL, PostgreSQL, Oracle, SQL Server) or DynamoDB
│
├─ Can be extracted as files (batch)
│   ├─ Small data size (<100MB), low frequency
│   │   └─ mode: csv (default)
│   │       └─ S3 upload (CSV) + Lambda processing + Glue Table
│   │
│   └─ Large data size (>100MB), columnar optimization needed
│       └─ mode: parquet
│           └─ S3 upload (Parquet) + Glue Table direct reference (no transformation needed)
│           └─ Benefits: high compression, good Athena/ER query performance, schema built in
│
├─ Real-time event stream
│   └─ mode: kinesis
│       └─ Kinesis Stream + EventBridge Pipe + load directly into CP
│
└─ Hybrid (batch + real-time)
    └─ mode: hybrid
        └─ Glue Connection (initial migration) + Kinesis (new events)
```

**Decision criteria**:
- Demo/PoC, small volume → CSV (simple, fast setup)
- Large batch, with parallel analytics → Parquet (Athena queryable, compression efficiency)
- Existing DB integration, periodic sync → Glue Connection + Crawler
- Production real-time → Kinesis
- Initial migration + later real-time → Hybrid (Glue + Kinesis)

## 2. Entity Resolution matching strategy

### Core principles

1. **Always include all 3 matching types**: Simple Rule + Advanced Rule + ML Matching
   Do not ask the user "which one to use." Make all of them runnable.

2. **Comparison dashboard**: a page comparing the 3 matching results side by side must be included
   - Number of match groups, unmatched count, suspicious group count per type
   - Precision/Recall comparison chart
   - Sample match pair comparison (how each type judged the same data)

3. **Bedrock auto-generation**: AI looks at the data and generates rules, validated via HITL

```

### Automatic matching type determination (decided by AI)

```
Q: Does the data have a unique identifier? (Bedrock decides after data profiling)
│
├─ Unique identifier exists (loyaltyNumber, membershipId, etc., uniqueRate > 0.95)
│   └─ Bedrock auto-generates 1 Simple Rule (exact match)
│       + also generates an Advanced Rule (for records without a unique ID)
│
├─ No unique identifier, PII field quality is good (nullRate < 0.3)
│   └─ Bedrock auto-generates 2-4 Advanced Rules
│       - Field combinations: optimal combinations based on the data profile
│       - When Korean is detected: recommend adding an ETL normalization step
│
└─ Low data quality (nullRate > 0.5, many variation patterns)
    └─ Bedrock decides:
        ├─ Recommend re-analyzing after ETL normalization (improve data quality first)
        └─ Or suggest using ML Matching as a supplement (must confirm regional availability)
```

### Mandatory HITL validation points

After Bedrock generates rules, the user must confirm the following:
1. **Match pair preview**: "These two records are judged to be the same customer" — is that correct?
2. **Expected precision/recall**: show expected accuracy per rule
3. **Cautions**: highlight rules with high false-positive risk
4. **Test run**: actual matching on a small dataset → review results → feedback → improvement loop

### Advanced Rule combination strategy

| Rule name | Match Keys | Confidence | Usage condition |
|-----------|-----------|--------|-----------|
| NameAndEmail | Name + Email | High | When email is available |
| NameAndPhone | Name + Phone | High | When phone number is available |
| NameAndAddress | Name + Address | Medium | When address data is well-structured |
| EmailOnly | Email | Medium | When one email per person is guaranteed |
| PhoneOnly | Phone | Medium | When phone number uniqueness is guaranteed |
| LoyaltyNumber | LoyaltyNumber | Highest | When a membership system exists |

## 3. Whether a Knowledge Graph is needed

```
Q: Is customer relationship analysis needed?
├─ YES → graph.enabled: true
│   │
│   ├─ Need to identify family/companion relationships
│   ├─ Link corporate customers to individuals
│   ├─ Influence/network analysis
│   └─ AI assistant (GraphRAG) needed
│   │
│   └─ ⚠️ Cost warning: Neptune db.r5.large + NAT = ~$300/month minimum
│
└─ NO → graph.enabled: false (default)
    └─ When individual customer profiles alone are sufficient
```

## 4. Whether Cross-Domain is needed

```
Q: Is customer integration across different business units/brands needed?
├─ YES → add the Cross-Domain stack
│   │
│   ├─ Multiple Connect Instances (per domain)
│   ├─ Platform-level CP Domain (unified view)
│   └─ Cross-Domain ER Workflow
│   │
│   └─ ⚠️ Connect Instance quota: default 2 → quota increase needed
│
└─ NO → single domain (default)
    └─ Integrate all channels into one CP Domain
```

## 5. Frontend page selection

| Core (always) | Matching-related | Enrichment/Integration | Advanced |
|------------|----------|----------|------|
| Dashboard | Matching Results | Profile Enrichment | Cross-Domain View |
| Data Ingestion | Matching Dashboard | Unified Profile View | Graph Builder |
| | AI Rule Improvement | | GraphRAG Chat |
| | Rule History | | Ecosystem View |

**Selection logic**:
- Core pages (always): Dashboard, Ingestion, Matching, AI Rules, Profile View
- Cross-Domain pages: Cross-Domain View, Ecosystem View → only when multi-domain
- Graph pages: Graph Builder, GraphRAG → only when graph.enabled
- Domain-specific: industry-tailored visualizations (journey map, purchase funnel, etc.)

## 6. Cost tiers

| Tier | Composition | Estimated monthly cost |
|------|------|------------|
| Minimal | CP + ER (Rule) + CSV + Cognito | ~$50-100 |
| Standard | + Kinesis + ML Matching | ~$100-200 |
| Full | + Neptune + Cross-Domain | ~$400-600 |

*Cost varies with the number of profiles, matching frequency, and region*
