# Migration Plan — {source} → {target}

> **This file is the working artifact and single source of truth for the engagement.**
> The agent creates it at Phase 0 and updates it as every result lands. A step is "done"
> only when its result is recorded here. Decisions carry their *why*. Chat scrollback is
> not the record — this file is.
> For every phase, record the outcome, substantive findings, remaining work and evidence.
> Mirror these into the dashboard using `shared/reference/dashboard.md`; keep the current
> risk register, customer requests, estimates and rationale consistent with phase results.

| | |
|---|---|
| Engagement | {customer / project} |
| **Engagement mode** | {1 analysis-only / 2 migration-ready — customer executes cutover / 3 full-migration — agent executes cutover} (authorizations.md §1) |
| **Mode 2 handover depth** | {(a) full — CDC current + clone-rehearsed timings / (b) light — target built, customer starts replication} · n/a for Modes 1, 3 |
| Source | {engine+version} on {EC2 instance-id / on-prem host} |
| Target | {aurora-mysql / rds-postgresql / …} {version} in {region} |
| Method (approved GATE 2) | {method} — *why:* {reason} |
| Engagement parameters (GATE 1) | rehearsal: {none/one/repeat-until-converged} · parallel run: {N} consecutive green days ({risk tier: Low/Moderate/High} — signal: {…}; hourly compression: manual tracking + waiver only) · validation depth: {…} · rollback: {reverse replication / snapshot+RPO ack / write-log replay} |
| Cutover window | {date/time, TZ} |
| Downtime budget | {seconds/minutes/hours} · RPO on rollback: {zero / acknowledged loss} |
| Status | ⏳ Phase {n} |

> Approvals of record live in **`authorizations.md`** as a `**Confirmed:**` mark + date
> per block — no name captured, by design; gate rows below reference it. **The customer
> never edits either file themselves** — a clear, specific reply in chat addressing that
> exact block is what's required; the agent then fills in the date. GATE 1's content
> already lives in `discovery-questions.md`, so its own go-ahead is noted below rather
> than as a separate authorizations.md row — same mechanism, just no duplicate block. IAM
> guardrail active: {session policy / boundary / simulate-proof}.

## Customer actions — current requests and recorded resolutions

Mirror the pending ACTION NEEDED checklist from chat, in the same order. Keep resolved
requests with the recorded response; a request row never grants approval on its own.
Use existing gate/A-number IDs where available; owners are roles/teams, not approver names.

| ID | Exact response / input needed | Why it matters | Owner | Needed by | Status / resolution |
|---|---|---|---|---|---|

## Phase 0 — Preflight ▢
- Account/region verified: ▢ ({account-id}, {region})
- IAM simulation passed: ▢ (gaps: …)
- Quotas OK: ▢ · Engine version available: ▢ · CDK bootstrapped: ▢/n.a.
- MCP servers connected: {list or "none — CLI fallback"}

## Phase 1 — Discovery answers (GATE 1: ▢ confirmed in chat {date} — agent-noted, see `discovery-questions.md`)
| # | Question | Answer |
|---|----------|--------|
| — | *#1–2 answered in chat at Phase 0/1; #3–18 collected via `discovery-questions.md` (or chat, if preferred) and transcribed below.* | |
| 1 | Source engine/version/location **(if not EC2/plain on-prem VM: self-managed w/ OS access, or managed DB product?)** — **and how do you already connect to it?** (existing bastion/jump host, VPN, direct network access) — check this first: a working path the customer already trusts is reused as-is (run the session from wherever that access already is), not replaced by SSM by default | |
| 2 | Target service/version **and network placement** — existing VPC/subnet group/SGs/KMS key to reuse, or provision new networking? | |
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
| 16 | **Engagement parameters** (rehearsal · parallel-run N · validation depth · rollback strategy) + Mode-2 handover depth (a/b) | |
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
- Architecture rationale and rollback path: …
- Estimate basis: {currency, pricing date, region, incremental vs existing costs}
- Estimate scope: {complete / partial — identify unpriced items and exclusions}

| Cost item | Monthly / one-time | Amount or range | Assumptions / pricing evidence |
|---|---|---|---|

- Expected write pause: {duration or range} · budget: {duration} · basis: {estimated / measured / mixed}
- Estimated engagement completion: {window with timezone, or unknown} · dependencies: …
- Next milestone and what it depends on: …
- Timing evidence / rehearsal revision: …

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

## Phase 7.5 — Client inventory (every repoint/revert plan staged and pool prep complete)

Before cutover, all clients/consumers must be repoint-ready, not already repointed.
Keep Repointed/Verified and consumer Executed/Verified unchecked until Phase 8; Mode 2
hands those execution checks to the customer. Upstream changes must remain inactive.
| Client | How it finds the DB (highest-priority source) | Config deployed from (repo/pipeline) | Change merged upstream | Pool prep done | Repointed | Verified on new DB processlist |
|--------|-----------------------------------------------|--------------------------------------|:---:|:---:|:---:|:---:|
| | | | ▢ | ▢ | ▢ | ▢ |

Downstream replication/CDC consumers (Debezium/replicas/ELT — cutover-procedures.md §Step 4):
| Consumer | Cutover plan (restart-from-target strategy) | Executed | Verified |
|----------|---------------------------------------------|:---:|:---:|
| | | ▢ | ▢ |

## Phase 7.7 — Parallel-run soak (cutover readiness locked until green)
- Soak length: {N} consecutive green days · counter: {k}/{N}
- Daily reports: {links to soak-report files}
| Day | Verdict | Lag max | Spot checks | Notes |
|-----|---------|---------|-------------|-------|
| | 🟢/🔴 | | | |
- Customer test traffic against target: {what they ran}
- Soak-exit sign-off (authorizations.md §3): ▢

## Phase 8 — Cutover

### Mode 2 — handover (agent does NOT execute)
- Runbook + rollback runbook generated with real values: ▢ · timings {measured/estimated}: ▢
- Reverse replication created + connection-tested, NOT started: ▢ (or rollback alternative + RPO ack: ▢)
- Client-repoint list handed over (per-client exact change + deploy source): ▢
- Validation + soak evidence attached: ▢
- Runbook walkthrough with the customer completed: ▢
- **A4b handover acceptance signed** (authorizations.md): ▢ — customer owns the cutover from here
- Offered read-only observation / post-cutover verification: ▢
- Customer-reported cutover outcome: {date, result, measured pause if shared}

### Mode 3 — execution (GATE 4 sign-off: ▢)
- Runbook generated & rehearsed: ▢ · Reverse replication created+tested: ▢
- Executed {timestamp} · write-pause measured: {s} · bidirectional verify ✅ ▢
- Rollback decision points reviewed at T+15m ▢ T+1h ▢ T+24h ▢

## Phase 9 — Post-migration
- ANALYZE/stats ▢ · production parameter group swapped ▢ · scaled down ▢
- Baseline vs new top-20 query comparison: ▢ (regressions: …)
- Source decommission date: {cutover + 7d} · reverse replication stopped ▢ · DMS deleted ▢

## Risk & assumption log
Keep current when a result resolves or changes a risk. Accepted residual risks stay
distinct from closed ones; leave severity/owner unrecorded until assessed/assigned.

| # | Risk/assumption and customer impact | Mitigation / verification and evidence | Status (open / mitigating / accepted / closed) | Severity (if assessed) | Owner / next review |
|---|---|---|---|---|---|

## Rollback record (only if executed)
- Trigger: … · executed runbook steps: … · data loss: {none / description}
