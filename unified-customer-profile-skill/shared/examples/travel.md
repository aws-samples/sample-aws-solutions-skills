# Example: Travel & Hospitality (Golden Reference)

A complete reference implementation of customer unification for the travel industry (airline, hotel, agency).
Source code: `demo-for-customer-profile-travel-hospitality/`

## Highlights

- **3 domains**: Airline, Hotel, Agency — each an independent CP Domain
- **Cross-Domain unification**: a platform-level CP identifies the same customer across domains
- **Knowledge Graph**: Neptune + GraphRAG for relationship analysis + an AI assistant
- **Ecosystem CLV**: aggregated customer lifetime value across domains

## Channel Configuration

| Domain | Channels |
|--------|------|
| Airline | WEB, MOBILE, OTA, CORPORATE, CALL_CENTER |
| Hotel | HOTEL_WEB, HOTEL_APP, HOTEL_OTA, WALK_IN, CORPORATE |
| Agency | AGENCY_WEB, AGENCY_APP, B2B |

## PII Fields

```yaml
- variantid (UNIQUE_ID)
- firstname (NAME_FIRST) → match_key: Name, group: FullName
- lastname (NAME_LAST) → match_key: Name, group: FullName
- email (EMAIL_ADDRESS) → match_key: Email
- phone (PHONE_NUMBER) → match_key: Phone
- dateofbirth (DATE) → match_key: DateOfBirth
- passportnumber (STRING) → match_key: PassportNumber  # Airline-specific
- frequentflyernumber (STRING) → match_key: FFN  # Airline-specific
- loyaltynumber (STRING) → match_key: LoyaltyNumber
- sourcechannel (STRING)
```

## Object Types

### Airline Domain
- **Booking**: PNR, segments, class, amount, seat
- **FFP** (Frequent Flyer Program): tier, mileage, status
- **Ancillary**: ancillary service purchases (baggage, seat upgrade, lounge)
- **ServiceCase**: customer service history

### Hotel Domain
- **Reservation**: check-in/out, room type, ADR
- **Folio**: ancillary facility usage (F&B, spa, minibar)
- **GuestPreferences**: room preference, pillow, temperature
- **Loyalty**: membership tier, points

### Agency Domain
- **Package**: package product bookings (flight + hotel combined)
- **Inquiry**: consultation history
- **Preferences**: travel style, preferred destinations

## ER Strategy

Use all three types:
1. **Simple**: exact match on FFN/LoyaltyNumber
2. **Advanced**: NameAndEmail, NameAndPhone (fuzzy)
3. **ML**: when variation across sources is large

## Calculated Attributes Examples

- TotalAirlineRevenueInAYear (Booking → SUM Amount)
- FlightCountInAYear (Booking → COUNT)
- TotalHotelNightsInAYear (Reservation → SUM NumberOfNights)
- AverageDailyRate (Reservation → AVG ADR)
- LastFlightDate (Booking → LAST_OCCURRENCE DepartureDate)
- EcosystemCLV (Cross-Domain aggregation — computed by a separate Lambda)

## Cross-Domain Ecosystem CLV Calculation

```typescript
interface EcosystemCLV {
  airlineCLV: number;   // airline 1-year revenue × retention rate
  hotelCLV: number;     // hotel 1-year revenue × retention rate
  agencyCLV: number;    // agency 1-year revenue × retention rate
  ecosystemCLV: number; // aggregate + synergy coefficient
  synergyBonus: number; // weighting applied for multi-domain usage
}

// Synergy coefficient: 2 domains = 1.2x, 3 domains = 1.5x
```

## Key Design Decisions

| Decision | Rationale |
|------|------|
| One Connect Instance per domain | Data isolation + independent Object Type management |
| Separate platform-level Domain | Store Cross-Domain matching results separately |
| Sequential Instance creation | Avoid Connect "pending" state conflicts |
| Neptune for Graph | Optimal for relationship queries of depth 3+ hops |
| GraphRAG (Bedrock + Cypher) | Natural language → graph query translation |
