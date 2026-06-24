# Eval: Hotel Scenario

## Input Prompt
```
Build a unified customer system for a hotel chain.
- 6 channels: web, app, OTA, walk-in, call center, corporate
- Manage reservation/folio/preference/loyalty data
- Solve the OTA relay email problem
- No Graph needed
- Region: ap-northeast-2
```

## Expected Output Checklist

### Architecture decisions
- [ ] Single CP Domain (not multi-domain)
- [ ] Graph: disabled
- [ ] CSV ingestion (default)
- [ ] ~$50-100/month cost range

### Generated files
- [ ] `config/schema.yaml` — tailored for the hotel industry
- [ ] `lib/main-stack.ts` — no Graph/CrossDomain
- [ ] Only the 7 core stacks (Foundation, Storage, Profiles, Matching, Ingestion, Auth, API)
- [ ] `backend/lambdas/` — no graph-rag, graph-sync

### ER strategy (hotel-specific)
- [ ] LoyaltyNumber first
- [ ] NameAndPhone (works around OTA relay email)
- [ ] OTA relay email detection logic included
- [ ] NameAndEmail (direct booking channels)

### Object Types
- [ ] Reservation
- [ ] Folio (ancillary facility usage)
- [ ] GuestPreferences (preferences)
- [ ] Loyalty (membership)

### Calculated Attributes
- [ ] TotalHotelRevenueInAYear
- [ ] TotalNightsInAYear
- [ ] AverageDailyRate
- [ ] TotalFolioSpendInAYear
- [ ] MostRecentStayDate
