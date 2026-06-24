# Example: Retail & E-commerce

Customer unification reference for the retail/e-commerce industry.

## Industry Characteristics

- Unifying online/offline channels is central (O2O)
- High proportion of guest (non-member) purchases → harder to identify
- Email is the strongest identifier (one-account-per-person pattern)
- Shipping address is useful as a secondary identifier
- RFM segmentation is the core analysis

## Channels

```yaml
channels:
  - WEB            # Official web store
  - MOBILE_APP     # Mobile app
  - POS            # Offline store POS
  - CALL_CENTER    # Call-center phone orders
  - MARKETPLACE    # External marketplaces (Coupang, Naver, etc.)
```

## PII Fields

```yaml
pii_fields:
  - name: variantid
    type: UNIQUE_ID
    required: true
  - name: firstname
    type: NAME_FIRST
    match_key: Name
    group: FullName
    required: true
  - name: lastname
    type: NAME_LAST
    match_key: Name
    group: FullName
    required: true
  - name: email
    type: EMAIL_ADDRESS
    match_key: Email
  - name: phone
    type: PHONE_NUMBER
    match_key: Phone
  - name: dateofbirth
    type: DATE
    match_key: DateOfBirth
  - name: loyaltynumber
    type: STRING
    match_key: LoyaltyNumber
  - name: postalcode
    type: ADDRESS_POSTALCODE
    match_key: Address
    group: ShippingAddress
  - name: country
    type: ADDRESS_COUNTRY
    match_key: Address
    group: ShippingAddress
  - name: sourcechannel
    type: STRING
```

## Object Types

### Order
Order header
- OrderId, CustomerId, OrderDate, OrderStatus
- TotalAmount, Currency, Channel, PaymentMethod
- ShippingCity, ShippingCountry

### OrderLineItem
Order details (per item)
- LineItemId, OrderId, ProductId, ProductName
- Category, Quantity, UnitPrice, LineAmount

### LoyaltyMembership
Membership program
- LoyaltyId, CustomerId, Tier (Bronze/Silver/Gold/VIP)
- PointsBalance, LifetimeSpend, EnrollDate

### Preferences
Marketing/product preferences
- PreferredCategories, PreferredBrands
- MarketingOptIn, PreferredLanguage

## Calculated Attributes

| Name | Aggregation | Period |
|------|------|------|
| TotalSpendInAYear | SUM(Order.TotalAmount) | 365 days |
| OrderCountInAYear | COUNT(Order) | 365 days |
| AverageOrderValue | AVG(Order.TotalAmount) | 365 days |
| MostRecentOrderDate | LAST_OCCURRENCE(OrderDate) | — |
| LifetimeItemCount | SUM(OrderLineItem.Quantity) | All-time |

## ER Strategy (Retail-Specific)

**Key challenges**:
- Guest purchases: only email collected, name may be missing
- POS cash payment: almost no identifying information
- Marketplace: relay address used instead of the real email
- Family members share the same account

**Recommended rule combination**:
1. `LoyaltyNumber` — definitive for members
2. `EmailOnly` — strongest in e-commerce (assumes one email per person)
3. `NameAndPhone` — for POS membership enrollment
4. `NameAndEmail` — marketplace (after excluding relay emails)
5. `PhoneOnly` — based on mobile-app authentication

**Guest purchase handling**:
```typescript
// When only email is present and the name is missing
if (!record.firstname && record.email) {
  // Attempt to match using the EmailOnly rule
  // Merge the profile later when the customer signs up
}
```

**RFM segment calculation** (in the frontend):
```typescript
interface RfmSegment {
  recency: number;    // days since last order
  frequency: number;  // orders per year
  monetary: number;   // total spend per year
  segment: 'Champion' | 'Loyal' | 'AtRisk' | 'Lost' | 'New';
}

function calculateRfm(profile: any): RfmSegment {
  const recency = daysSince(profile.MostRecentOrderDate);
  const frequency = profile.OrderCountInAYear;
  const monetary = profile.TotalSpendInAYear;

  // Simple scoring (1-5)
  const rScore = recency < 30 ? 5 : recency < 90 ? 4 : recency < 180 ? 3 : recency < 365 ? 2 : 1;
  const fScore = frequency > 12 ? 5 : frequency > 6 ? 4 : frequency > 3 ? 3 : frequency > 1 ? 2 : 1;
  const mScore = monetary > 1000000 ? 5 : monetary > 500000 ? 4 : monetary > 200000 ? 3 : monetary > 50000 ? 2 : 1;

  // Segment mapping
  if (rScore >= 4 && fScore >= 4) return { ...base, segment: 'Champion' };
  if (fScore >= 3) return { ...base, segment: 'Loyal' };
  if (rScore <= 2 && fScore >= 3) return { ...base, segment: 'AtRisk' };
  if (rScore <= 1) return { ...base, segment: 'Lost' };
  return { ...base, segment: 'New' };
}
```

## Frontend-Specific

- **Purchase funnel**: visualize cart → order → shipping → completion
- **RFM segment map**: 3D scatter or heatmap
- **Category preference**: treemap of purchased categories
- **Repurchase rate**: cohort analysis chart
- **AOV trend**: monthly average order value line chart
