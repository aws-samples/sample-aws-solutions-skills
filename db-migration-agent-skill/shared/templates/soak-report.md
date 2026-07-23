# Soak Report — Day {n} of {N} — {engagement} — {date}

> Daily report for the Phase 7.7 parallel-run soak (Tier 2/3). Target is live and
> CDC-current; production still runs on the source. Cutover unlocks only after
> **{N} consecutive GREEN days** and the signed cutover authorization.
> **Consecutive green counter: {k}/{N}** (any RED resets it to 0).

## Verdict: {🟢 GREEN / 🔴 RED — reason}

| Check | Threshold | Today | Pass |
|-------|-----------|-------|:---:|
| Replication lag (max over day) | < {5}s | {…} | ▢ |
| Replication errors | 0 | {…} | ▢ |
| Row-count spot check ({3-5 tables}, at a freeze-consistent instant) | exact match | {…} | ▢ |
| Checksum spot check ({1-2 static tables}) | identical | {…} | ▢ |
| Target alarms | none firing | {…} | ▢ |
| Storage/connections headroom on target | > 30% | {…} | ▢ |
| Schema drift (DDL on source since yesterday) | none unreplicated | {…} | ▢ |
| **Customer test suite vs target** (if provided, Q18) | all pass | {suite: n pass / n fail} | ▢ |

## Tier 3 additions

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
