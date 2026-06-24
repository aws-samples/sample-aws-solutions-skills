# Example: B2B SaaS Cross-sell

> B2B SaaS sales recommendations / cross-sell. The core: **"features used by larger customers in the same industry"** + **"upgrade likelihood score"**.

## Discovery answers

| Question | Answer |
|---|---|
| Project name | acme-saas |
| Industry | B2B SaaS |
| Region | us-east-1 (US market) |
| Schema (vertices) | Account + Feature + Industry + Plan + Contact |
| Schema (edges) | USES (with freqPerWeek) + ON_PLAN + IN_INDUSTRY + REQUIRES_PLAN + UPGRADED_FROM |
| Data source | Aurora PostgreSQL (CRM) + product usage events (Kinesis) |
| Update mode | Hybrid (initial bulk load + real-time usage events) |
| Recommend endpoints | peer-adoption + upgrade-readiness + cross-sell |
| Explanation | Bedrock Sonnet 4 (English default, formal tone) |
| Latency | Standard (<2s) — sales dashboard |
| Neptune mode | Provisioned db.r6g.large (24/7 steady load, RI) |
| Neptune ML | Skip (few accounts, so graph traversal is sufficient) |
| Frontend | React Admin (used by sales) |
| Auth | Cognito + Okta SAML |

## Graph schema

```
Vertex labels:
  Account     {id, name, industry, size, planTier, mrrUsd, contractEndDate}
  Feature     {id, name, category, isPaidOnly, requiredPlanTier}
  Industry    {id, name}                       # FinTech, EdTech, HealthTech, ...
  Plan        {id, name, tier, monthlyPriceUsd}
  Contact     {id, role, isDecisionMaker}
  Region      {id, code}                       # US-CA, EU-DE, ...

Edge labels:
  (Account)-[USES {at, weight, freqPerWeek}]->(Feature)
  (Account)-[ON_PLAN {since, mrrUsd}]->(Plan)
  (Account)-[IN_INDUSTRY]->(Industry)
  (Account)-[IN_REGION]->(Region)
  (Feature)-[REQUIRES_PLAN]->(Plan)
  (Account)-[UPGRADED_FROM {at, mrrAtTime}]->(Plan)
  (Contact)-[WORKS_AT]->(Account)
  (Contact)-[REQUESTED_FEATURE]->(Feature)     # sales trigger
```

## Endpoints

```
POST /recommendations/peer-adoption    { account_id, limit }
   → "features used by larger customers in the same industry"

POST /recommendations/upgrade-readiness { account_id }
   → upgrade likelihood score + rationale

POST /recommendations/cross-sell        { account_id, current_feature, limit }
   → "features used together with this feature"

POST /recommendations/at-risk          { limit }
   → at-risk accounts (low usage + downgrade history)
```

## Demo scenarios

### 1. Peer-adoption (sales meeting prep)

```
A sales rep opens the dashboard before a customer meeting
   ↓
account_id = "acc-fintech-startup-12"
POST /recommendations/peer-adoption

Response:
{
  "items": [
    {"id": "feat-sso", "name": "SAML SSO", "score": 8.5, "adopterCount": 12, "avgUsage": 45},
    {"id": "feat-audit", "name": "Audit Log Export", "score": 7.2, "adopterCount": 8},
    ...
  ],
  "explanation": {
    "explanation": "Same industry (FinTech) clients with higher MRR are using these features extensively. SAML SSO is adopted by 12 peer accounts with average 45 weekly active uses. Recommend pitching during next renewal conversation.",
    "reason_tag": "peer-adoption",
    "suggested_action": "request-demo"
  },
  "scenario": "peer-adoption"
}
```

→ Sales can pitch: "12 peer FinTech customers are using SAML SSO → recommend that our customer adopt it too."

### 2. Upgrade readiness

```
account_id = "acc-edutech-co-5"  (currently on Starter plan)
   ↓
POST /recommendations/upgrade-readiness

Response:
{
  "account_id": "acc-edutech-co-5",
  "currentPlan": "Starter",
  "upgradeLikelihood": "HIGH",
  "proFeaturesUsed": 4,
  "details": {
    "proFeaturesUsed": ["SSO", "Audit Log", "Custom Reports", "API access"],
    "currentFeatureCount": 11,
    "industryMedianFeatures": 8
  },
  "explanation": {
    "explanation": "This account is using 4 features that require Pro plan ($X/mo). Their current usage exceeds the median for their industry. High likelihood of successful upgrade pitch — recommend booking call within 30 days.",
    "reason_tag": "upgrade-readiness",
    "suggested_action": "book-meeting"
  }
}
```

### 3. At-risk accounts (churn prevention)

```
A sales manager reviews the at-risk list during weekly review
   ↓
POST /recommendations/at-risk { limit: 20 }

Response:
{
  "items": [
    {
      "id": "acc-retail-co-99",
      "name": "Retail Co (name hashed)",
      "industry": "Retail",
      "mrrUsd": 5000,
      "contractEndDate": "2026-08-15",
      "atRiskScore": 0.82,
      "signals": ["usage_drop_60d", "no_active_contact_30d", "downgraded_recently"]
    },
    ...
  ],
  "explanation": {
    "explanation": "These accounts show signals of churn risk: declining feature usage, no recent contact engagement, or recent plan downgrades. Recommend proactive outreach.",
    "reason_tag": "churn-prevention",
    "suggested_action": "book-meeting"
  }
}
```

## Graph Explorer demo (sales)

```
"graph of acc-fintech-startup-12"

   (FinTech Industry)
        ↑
   IN_INDUSTRY
        │
   acc-fintech-startup-12 ──USES──▶ Feature: Dashboard
        │                  USES   Feature: API
        │
        └──ON_PLAN──▶ (Starter Plan)
                          │
                       REQUIRES_PLAN
                          ▲
   (peer accounts in same industry)
   acc-fintech-1 ──USES─▶ SAML SSO ──REQUIRES_PLAN─▶ (Pro Plan)
   acc-fintech-2 ──USES─▶ SAML SSO
   ...
```

## Cost estimate

```
Neptune Provisioned db.r6g.large + RI (1y) ~$178/mo
   (few accounts, so r6g.large is sufficient; RI saves 30%)
+ Reader replica                            +$178
Lambda (low QPS — sales only)                $5
Bedrock Sonnet 4 (formal English)           $50
Kinesis (1 shard)                           $11
S3 + CloudFront                             $20
Cognito + Okta SAML                          $20
KMS                                         $2
─────────────────────────────────────────────────
Total                                       ~$464/mo
```

## SAML federation (Okta)

```typescript
// AuthStack
import * as cognito from 'aws-cdk-lib/aws-cognito';

const oktaProvider = new cognito.UserPoolIdentityProviderSaml(this, 'Okta', {
  userPool,
  metadataUrl: 'https://acme.okta.com/app/.../sso/saml/metadata',
  attributeMapping: {
    email: cognito.ProviderAttribute.SAML_EMAIL,
    givenName: cognito.ProviderAttribute.SAML_GIVEN_NAME,
    familyName: cognito.ProviderAttribute.SAML_FAMILY_NAME,
    custom: { 'role': cognito.ProviderAttribute.other('Role') },
  },
});

userPoolClient.identityProviders = [
  cognito.UserPoolClientIdentityProvider.custom('Okta'),
];
```

## Sales workflow integration (Salesforce / HubSpot)

```python
# A Lambda runs via EventBridge every early morning → pushes to Salesforce
def sync_to_salesforce_lambda(event, context):
    # 1. Neptune query: at-risk accounts
    at_risk = neptune.run(QUERIES['at_risk'], limit=50)
    
    # 2. Salesforce REST API
    for acc in at_risk:
        sf_client.update_account(acc['id'], {
            'AtRiskScore__c': acc['atRiskScore'],
            'AtRiskSignals__c': '; '.join(acc['signals']),
            'NextRecommendedAction__c': 'Book Meeting',
        })
```

## A/B test (for the sales team)

| Treatment | Description |
|---|---|
| A | Graph-based recommendations (this skill) |
| B | Existing CRM scoring (rule-based) |

→ Measure: meeting booked rate, upgrade conversion, MRR growth.

## Recommended follow-ups

- Salesforce integration (lead scoring)
- Automated email outbound (Pinpoint + personalized content — combine later with `genai-personalization-skill`)
- Slack notification (high-signal alerts to the sales team channel)
- Forecast (churn prediction — SageMaker)
