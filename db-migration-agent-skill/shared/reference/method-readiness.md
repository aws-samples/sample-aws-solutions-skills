# Method-Specific Readiness

> Load after method selection and when preparing validation, rehearsal, Phase 7.7 or
> recovery. This maps existing requirements to the approved method; it adds no GATE or
> operation authorization. See [engagement-safety.md](engagement-safety.md) for the four
> human GATEs/authorization model and [dashboard.md](dashboard.md) for the six readiness checks.

## Keep the complete validation scope

Every method retains [validation-patterns.md](validation-patterns.md): counts for **all
tables**, content checksums/reconciliation for critical tables and the full agreed scope,
schema objects and constraints, FK/orphan checks, application semantics (collation,
timezone, aggregates, high-water marks), accounts/effective grants, and authentication
with the real driver/TLS settings through the intended endpoint. Add the applicable
version-gap battery and customer suite. Validate with effective production parameters,
backups/availability settings and alarms; keep target scheduled writers disabled.

Neither an offline method nor a lab label reduces this battery to counts or a health
check. Heterogeneous methods use per-engine conversion/reconciliation evidence.
Record mismatches and missing evidence as unresolved; GATE 3 still needs human acceptance.
Use existing authorization for reads; test writes need their existing action-specific
scope or an authorized isolated rehearsal environment.

## Choose the evidence by method

| Approved method | Preparation evidence | Required at the authorized switch |
|---|---|---|
| Offline logical copy (`mysqldump`, `pg_dump`, schema-mode Data Pump); full-load-only copy | Complete validation of an identified static copy; timed final-copy and recovery rehearsal | Continuous fence on all source writers → final complete copy/restore → final validation → repoint and verify |
| Full load + CDC, native binlog/logical replication, live replica | Complete initial validation + actual apply proof + observed replication/monitoring intervals | Fence source writers → verify final checkpoint applied with fresh lag/error evidence → stop forward channel → final checks and repoint |

A backup seed **with catch-up** follows the live-replication row. A backup/full-load
without catch-up follows the static-copy row. Service-managed switchover still needs its
own supported procedure and eligibility checks; a green service status supplies neither
application validation nor a supported reverse path.

### Offline logical copy

Record copy identity, source snapshot/export boundary, completion times and validation
results. The Phase 6 target remains isolated from application/job writes. If the source
keeps accepting writes, that copy becomes stale; its successful tests establish
**preparation/rehearsal readiness**, not cutover-current data.

Budget the final full export, replacement restore and complete planned validation
inside the continuous source-write fence. Recheck accounts, effective settings and
high-water marks after restore, then test the intended endpoint before opening writes.
A failed check or a copy/validation duration that cannot fit the approved outage blocks
the switch. Revisit method/window rather than dropping validation or accepting stale data.
Mode 2 hands this execution to the customer; Mode 3 needs the existing A4 scope.

There is no CDC lag or replication soak to measure. Use the **existing soak waiver**
to name the static alternative and freshness limitation, and the existing **soak-exit**
record to accept its evidence/remaining final-copy obligation. Describe this as static
readiness, never “N green days.” In the dashboard's `cutover_gates` entry keyed `soak`,
`detail` states the accepted alternative and outstanding conditions; `met:true` requires
those preparation conditions resolved. Use the existing `soak.waived`/`waived_reason`
display fields without fabricated daily samples. A readiness flag cannot discharge
the runbook's final frozen-copy checks.

### CDC and live replication

Keep the target current while the source serves production. Prove change application
under [execution-runbooks.md](execution-runbooks.md)'s CDC Proof Probe and retain the
planned full validation; task `running`, endpoint reachability and zero sampled lag do
not prove correctness.

Use the approved risk-tier duration and sampler from
[engagement-safety.md](engagement-safety.md). Record UTC start/end, sample timestamps,
metric dimensions, retained log/alarm coverage and gaps. Credit only observed intervals:
point samples prove their instants, not the nights/batch windows between them. Missing,
unknown or RED intervals cannot be retroactively green. Daily scripts remain UTC-day
based; compressed windows require the existing explicit waiver, manual interval evidence
and soak-exit acceptance. A timer expiring or several same-day invocations is not a soak.

At cutover, fresh lag/error evidence must agree with the saved source checkpoint being
applied. An unavailable target or missing marker is **unknown**, not “zero outstanding
writes”; use [cutover-procedures.md §Rollback Procedures](cutover-procedures.md#rollback-procedures).
Forward compatibility never establishes reverse compatibility. For an external MySQL
8.0 source and 8.4 target, do not claim native 8.4 → 8.0 lossless rollback; apply
[method-selection.md](method-selection.md)'s rollback-direction gate and
[version-upgrades.md](version-upgrades.md)'s exact topology/version rules.

## Reuse of lab preparation and rehearsal

Reuse verified work within the existing accepted scope instead of resetting the plan
or asking again for authorized routine reads. Record the following in the plan, with
evidence references; these are evidence limits, not an additional approval block.

| Bound | Required record and limit |
|---|---|
| Resources | Exact source/target identities and endpoint roles, app hosts/services, secret version references, network/Proxy path, and rehearsal pair mapped to the intended deployment. Names alone are insufficient; any substitution invalidates the affected evidence. Keep credentials out of the record. |
| Configuration | Hash/revision of staged cutover/rollback scripts and parsers, schema/mappings, effective parameters, accounts/grants/TLS, client overrides and deployment origin. No unassessed drift is allowed; rerun the affected checks after a change. |
| Versions | Exact source/target engine versions, dump/restore tools, replication runtime and app driver/build. A minor-version change also invalidates affected compatibility/command evidence. |
| Age | UTC observation time and an explicit maximum age/expiry for each reused prep or rehearsal result, agreed in the existing plan scope. Age must be within that limit at use; an absent limit, expired result or unknown timestamp cannot establish current readiness. |
| Coverage | Which steps actually ran, measured pause, write/replay proof and validation results; identify omitted steps and accepted waiver references. Component timing cannot become an end-to-end rehearsal measurement. |

For example, an explicitly agreed **lab-only** reuse rule might allow prep evidence up
to 24 hours old and a rehearsal up to 4 hours old, with zero resource/config/version
drift. Those are example limits, not defaults or automatic permission. At use, perform
fresh scoped reads of identity, effective config, process/session state and copy/CDC
freshness; they can invalidate evidence younger than the age limit.

A retained lab pair can supply rehearsal evidence only for the steps actually exercised,
including final-copy/drain, repoint, verification and rollback. For an acknowledged-RPO
strategy, rehearse containment and failback and state the data loss; do not relabel it
lossless. A partial-config failure or uncertain target write needs the recovery drill,
not a blanket “rehearsal passed.”

Shortened/omitted soak or rehearsal uses the existing **explicit waiver protocol**:
name the exact omitted coverage, risk, replacement evidence, scope and expiry. No lab
label, fixture restriction, prior success or reuse decision automatically waives work.
Production defaults remain unchanged; reused lab evidence cannot waive production
validation, live observation, mode limits or execution authorization. Stale or changed
scope means refresh affected evidence; an uncertain mutation stops before execution.
