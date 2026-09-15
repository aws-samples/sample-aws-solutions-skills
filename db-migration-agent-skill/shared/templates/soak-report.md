# Soak Report — Day {n} of {N} — {engagement} — {date}

> Daily report for the Phase 7.7 parallel-run soak. Target is live and
> CDC-current; production still runs on the source. Cutover unlocks only after
> **{N} consecutive GREEN days** and the separately confirmed **soak-exit block**.
> A4 (Mode 3 cutover) / A4b (Mode 2 handover) is a later, separate authorization.
> **Consecutive green counter: {k}/{N}** (any RED resets it to 0).

## Verdict: {🟢 GREEN / 🔴 RED — reason}

| Check | Threshold | Today | Pass |
|-------|-----------|-------|:---:|
| Replication lag (sample: DMS last 15 min / native at invocation) | ≤ {30}s (soft, tunable threshold, not an AWS SLA) | {…} | ▢ |
| Replication errors | 0 | {…} | ▢ |
| Row-count spot check ({3-5 tables}, at a freeze-consistent instant) | exact match | {…} | ▢ |
| Checksum spot check ({1-2 static tables}) | identical | {…} | ▢ |
| Target alarms | none firing | {…} | ▢ |
| Storage/connections headroom on target | > 30% | {…} | ▢ |
| Schema drift (DDL on source since yesterday) | none unreplicated | {…} | ▢ |
| **Customer test suite vs target** (if provided, Q18) | all pass | {suite: n pass / n fail} | ▢ |

**Full-period evidence review:** {UTC start/end; retained replication logs and
CloudWatch alarm/metric history links; scheduled-job coverage; reviewer verdict}.
The automated sample is not whole-day evidence. Missing full-period evidence, a required
unknown check, or any unresolved incident blocks this day's acceptance and soak exit.

## Deep-validation additions (when these parameters were chosen)

| Check | Baseline (source) | Target today | Pass |
|-------|-------------------|--------------|:---:|
| Top-{10} statement plans (EXPLAIN diff) | {captured Phase 2} | {regressions: none/list} | ▢ |
| Customer load test / read-only prod traffic result | {p95 baseline} | {p95 today} | ▢ |
| Reconciliation aggregate(s): {e.g. SUM(ledger.amount) by day} | {value} | {value} | ▢ |

## Notes / anomalies
- {…}

## Customer visibility
Sent to: {names} · Questions raised: {none/list} · Customer test activity against target
today: {none / what they ran}
