# Example: Hotel & Hospitality

Customer unification reference for the hotel/resort industry.

## Industry Characteristics

- The same guest books through multiple channels: OTA, direct booking, corporate, etc.
- Name/email variations are especially common (OTA relay email: guest-abc123@booking.com)
- The loyalty program is a strong identifier
- Personalization data such as room preferences and dietary restrictions is the core value

## Channels

```yaml
channels:
  - HOTEL_WEB       # Official hotel website
  - HOTEL_APP       # Official hotel app
  - HOTEL_OTA       # OTA (Booking.com, Expedia, etc.)
  - WALK_IN         # On-site walk-in check-in
  - CALL_CENTER     # Phone reservation
  - CORPORATE       # Corporate contract booking
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
  - name: street / city / state / postalcode / country
    type: ADDRESS_*
    match_key: Address
    group: FullAddress
  - name: sourcechannel
    type: STRING
```

## Object Types

### Reservation
Hotel reservations + stay records
- ReservationId, GuestProfileId, PropertyCode, PropertyName
- CheckInDate, CheckOutDate, RoomType, RateCode
- AverageDailyRate, TotalAmount, NumberOfNights
- BookingChannel, Status

### Folio
Ancillary facility usage during the stay
- FolioId, ReservationId, ItemType (ROOM/FNB/SPA/MINIBAR/LAUNDRY)
- Description, Amount, Currency, ChargeDate

### GuestPreferences
Guest personalization data
- RoomPreference (high floor, ocean view, quiet)
- PillowType, TemperaturePreference, AmenityPreferences
- DietaryRestrictions, SmokingPreference

### Loyalty
Membership program
- LoyaltyId, MembershipTier (Silver/Gold/Platinum/Diamond)
- PointsBalance, LifetimeNights, LifetimeSpend, EnrollDate

## Calculated Attributes

| Name | Aggregation | Period |
|------|------|------|
| TotalHotelRevenueInAYear | SUM(TotalAmount) | 365 days |
| TotalNightsInAYear | SUM(NumberOfNights) | 365 days |
| AverageDailyRate | AVG(AverageDailyRate) | 365 days |
| TotalFolioSpendInAYear | SUM(Folio.Amount) | 365 days |
| MostRecentStayDate | LAST_OCCURRENCE(CheckInDate) | — |

## ER Strategy (Hotel-Specific)

**Key challenges**:
- OTA relay email (guest-xxx@booking.com) → cannot match on Email alone
- For corporate bookings, an assistant books on behalf → the name may differ
- Walk-in collects only minimal information

**Recommended rule combination**:
1. `LoyaltyNumber` — highest confidence (definitive when present)
2. `NameAndPhone` — bypasses OTA relay email
3. `NameAndEmail` — strong for direct bookings
4. `NameAndDOB` — fallback when phone/email are absent

**OTA relay email preprocessing**:
```typescript
function isRelayEmail(email: string): boolean {
  return /guest-.*@(booking|expedia|hotels)\.com/i.test(email);
}
// exclude relay email from matching and fall back to phone/name
```

## Frontend-Specific

- **Stay timeline**: visualize check-in/out history as a timeline
- **Preference card**: room/dietary/temperature preferences at a glance
- **Revenue per Guest**: room charges + ancillary facilities combined
- **Property distribution**: pie chart of which hotels are used most
