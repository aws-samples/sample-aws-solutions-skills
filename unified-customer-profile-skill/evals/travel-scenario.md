# Eval: Travel & Hospitality Scenario

## Input Prompt
```
Build a unified customer system for the travel industry (airline + hotel + travel agency).
- Manage independent profiles for each of the 3 domains (airline, hotel, travel agency)
- Identify the same customer across domains (Cross-Domain)
- Analyze customer relationships with a Knowledge Graph
- Automatically improve matching rules with AI
- Region: ap-northeast-2
```

## Expected Output Checklist

### Architecture decisions
- [ ] 3 Connect Instances + 3 CP Domains (airline, hotel, agency)
- [ ] Platform-level CP Domain (cross-domain integration)
- [ ] Neptune Graph (enabled)
- [ ] Present Kinesis or CSV choice
- [ ] Warn about ~$400-600/month cost

### Generated files
- [ ] `lib/main-stack.ts` — per-domain stacks + Platform stack + Graph stack
- [ ] `lib/domain-profiles-stack.ts` — generated repeatedly (airline, hotel, agency)
- [ ] `lib/cross-domain-matching-stack.ts`
- [ ] `lib/neptune-stack.ts`
- [ ] `backend/lambdas/matching/cross-domain-handler.ts`
- [ ] `backend/lambdas/graph-rag/handler.ts`
- [ ] `backend/lambdas/graph-sync/handler.ts`
- [ ] `frontend/src/pages/CrossDomainMatching.tsx`
- [ ] `frontend/src/pages/EcosystemView.tsx`
- [ ] `frontend/src/pages/GraphBuilder.tsx`

### Code quality
- [ ] Structure that can pass `cdk synth`
- [ ] Sequential Connect Instance creation (addDependency)
- [ ] Connect Instance quota warning (4 needed)
- [ ] `apac.` prefix on the Bedrock model ID
- [ ] KMS encryption applied
- [ ] DLQ configured

### ER strategy
- [ ] Simple: FFN/LoyaltyNumber exact match
- [ ] Advanced: NameAndEmail, NameAndPhone
- [ ] ML: presented as an option (including region check)
- [ ] Cross-Domain ER: matching across domains
