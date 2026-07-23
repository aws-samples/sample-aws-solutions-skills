# Migration Plan — {source} → {target}

> **This file is the working artifact and single source of truth for the engagement.**
> The agent creates it at Phase 0 and updates it as every result lands. A step is "done"
> only when its result is recorded here. Decisions carry their *why*. Chat scrollback is
> not the record — this file is.

| | |
|---|---|
| Engagement | {customer / project} |
| **Engagement mode** | {assessment-only / staging-rehearsal / production-migration} (authorizations.md §1) |
| **Criticality tier** | {1 / 2 / 3} — *basis:* {what happens if this DB is wrong for an hour} |
| Source | {engine+version} on {EC2 instance-id / on-prem host} |
| Target | {aurora-mysql / rds-postgresql / …} {version} in {region} |
| Method (approved GATE 2) | {method} — *why:* {reason} |
| Soak requirement (Tier ≥ 2) | {N} consecutive green days — tracker in §Phase 7.7 |
| Cutover window | {date/time, TZ} |
| Downtime budget | {seconds/minutes/hours} · RPO on rollback: {zero / acknowledged loss} |
| Status | ⏳ Phase {n} |

> Approvals of record live in **`authorizations.md`** (named person + date); gate rows
> below reference it. IAM guardrail active: {session policy / boundary / simulate-proof}.

## Phase 0 — Preflight ▢
- Account/region verified: ▢ ({account-id}, {region})
- IAM simulation passed: ▢ (gaps: …)
- Quotas OK: ▢ · Engine version available: ▢ · CDK bootstrapped: ▢/n.a.
- MCP servers connected: {list or "none — CLI fallback"}

## Phase 1 — Discovery answers (GATE 1 sign-off: ▢ {date, by})
| # | Question | Answer |
|---|----------|--------|
| 1 | Source engine/version/location | |
| 2 | Target service/version | |
| 3 | DB size / table count | |
| 4 | Downtime tolerance | |
| 5 | RPO if rollback | |
| 6 | Bandwidth source→AWS (usable) | |
| 7 | Stored procs/triggers/views needed | |
| 8 | App code modifiable? | |
| 9 | How app resolves DB host (secret/DNS/config/hardcoded) **and where that config is deployed FROM** (repo/pipeline/IaC/GitOps — the source of truth that must carry the change) | |
| 10 | Downstream CDC consumers | |
| 11 | Compliance/encryption mandates | |
| 12 | Korean security appliances (access-control / encryption products + mode) | |
| 13 | Multi-DB on host? Cross-DB queries? | |
| 14 | Cross-region / cross-account? | |
| 15 | KMS key type (AWS-managed / CMK) | |
| 16 | **Criticality tier** (1/2/3 — "if wrong for an hour, what happens?") | |
| 17 | **Third-party tools on/in front of the DB** (security, backup, monitoring, HA, proxy) | |
| 18 | **Customer's own test suite / UAT scenarios** (regression tests, load tests, key business flows QA runs) — to be executed against the target during rehearsal and soak | |

## Phase 2 — Assessment results
- Blockers found & resolutions: …
- Adjustments: …
- Binlog/WAL state: `log_bin=…`, `binlog_format=…` / `wal_level=…`
- Sizing: {GB}, largest tables: …
- Throughput estimate: {hours} vs window {hours} → {fits / offline-seed branch}
- Access path to source: {direct / bastion / SSM port-fwd / SSM send-command}
- Performance baseline captured: ▢ (top-20 digests + EXPLAIN attached below)

## Phase 3 — Method decision (GATE 2 sign-off: ▢)
- Matrix row #: … · Alternatives rejected & why: …
- Cost estimate presented: steady ~${}/mo, one-time ~${} — approved ▢

## Phase 4/5 — Target provisioning
- Immutables confirmed BEFORE create (charset/collation/block-size/license/KMS): ▢
- Cluster: {id} · endpoint: … · RDS Proxy: {endpoint / n.a.}
- Parameter groups: migration={name} production={name} · TLS enforced: {ON/OFF}
- cdk synth ✅ ▢ · deployed ▢ · alarms live ▢

## Phase 6 — Execution log
| When (UTC) | Step | Result / evidence |
|------------|------|-------------------|
| | | |
- CDC start position recorded (binlog file+pos / LSN / SCN): …
- Rehearsal performed ▢ — measured durations: …

## Phase 7 — Validation evidence (GATE 3 sign-off: ▢)
- Row counts: {n}/{n} tables match ▢ · Checksums (critical tables): ▢
- Schema-object counts source vs target: ▢ · FK orphans: 0 ▢
- AUTO_INCREMENT/sequence high-water marks reset plan: ▢
- App smoke test (read-only): ▢ · Version-gap checks (if major upgrade): ▢

## Phase 7.5 — Client inventory (cutover blocked until every row is ✅✅)
| Client | How it finds the DB (highest-priority source) | Config deployed from (repo/pipeline) | Change merged upstream | Pool prep done | Repointed | Verified on new DB processlist |
|--------|-----------------------------------------------|--------------------------------------|:---:|:---:|:---:|:---:|
| | | | ▢ | ▢ | ▢ | ▢ |

Downstream replication/CDC consumers (Debezium/replicas/ELT — cutover-procedures.md §Step 4):
| Consumer | Cutover plan (restart-from-target strategy) | Executed | Verified |
|----------|---------------------------------------------|:---:|:---:|
| | | ▢ | ▢ |

## Phase 7.7 — Parallel-run soak (Tier ≥ 2; cutover locked until green)
- Soak length: {N} consecutive green days · counter: {k}/{N}
- Daily reports: {links to soak-report files}
| Day | Verdict | Lag max | Spot checks | Notes |
|-----|---------|---------|-------------|-------|
| | 🟢/🔴 | | | |
- Customer test traffic against target: {what they ran}
- Soak-exit sign-off (authorizations.md §3): ▢

## Phase 8 — Cutover (GATE 4 sign-off: ▢) 
- Runbook generated & rehearsed: ▢ · Reverse replication created+tested (or alternative + RPO ack): ▢
- Executed {timestamp} · write-pause measured: {s} · bidirectional verify ✅ ▢
- Rollback decision points reviewed at T+15m ▢ T+1h ▢ T+24h ▢

## Phase 9 — Post-migration
- ANALYZE/stats ▢ · production parameter group swapped ▢ · scaled down ▢
- Baseline vs new top-20 query comparison: ▢ (regressions: …)
- Source decommission date: {cutover + 7d} · reverse replication stopped ▢ · DMS deleted ▢

## Risk & assumption log
| # | Risk/assumption | Mitigation / verification | Status |
|---|-----------------|---------------------------|--------|

## Rollback record (only if executed)
- Trigger: … · executed runbook steps: … · data loss: {none / description}
