# Rollback Runbook — fail back {target} → {source}

> Generate with real values and only applicable branches. Strategy approved at GATE 2/
> GATE 4: **{supported reverse replication / tested write-log replay / acknowledged RPO}**.
> Valid until {rollback-window end, UTC}; void after source decommission.
> Follow [cutover-procedures.md §Rollback Procedures](../reference/cutover-procedures.md#rollback-procedures)
> for containment, write-state classification and release criteria.

**Executed by:** {customer team in Mode 2 / migration agent in Mode 3}.
**Trigger and authority:** {abort criterion / decision}; {accepted A5 block, exact scope
and conditions}. Reuse that approval within scope; changed scope needs acceptance first.
**Announce on:** {channel}. **Runbook/script revision:** {revision/hash}.
**Exact source / target / client identities and versions:** {plan evidence reference}.
**Step budgets:** {per-step measured duration + rehearsal evidence, or estimated basis}.

## Capture and contain

| ✅ | Step | Command / evidence to fill | Verification |
|---|------|----------------------------|--------------|
| ▢ | 1. Enter/retain maintenance and source writer fence | {commands; last verified switch step; attempted mutations/results} | source app/jobs cannot write or reconnect |
| ▢ | 2. Contain target writers | {stop/fence app/jobs and reconnects; drain sessions and transactions, including prepared transactions} | fence effective; no residual writers; unavailable evidence recorded as unknown |
| ▢ | 3. Classify target writes | {switch journal, reachable target/audit evidence, marker/checkpoint results and UTC interval coverage} | {proven none / confirmed writes / unknown}; failed or absent marker alone is not proof of no writes |

**Unknown target/write state:** keep fences and stop before source release. Use already
authorized reads to investigate; preserve errors and evidence. An unreachable target,
failed query or ambiguous write timeout cannot authorize unfencing or a zero-loss claim.

## Reconcile data — choose the evidenced branch

| Branch | Required procedure before client recovery/source release |
|---|---|
| Proven no target writes | {evidence covering all writers and the switch interval}; retain source as authoritative, continue below |
| Supported reverse replication | {final target checkpoint}; fresh post-freeze lag/error evidence + verify final changes applied on source → {stop reverse channel and verify stopped} |
| Tested write-log replay | {complete log and bounds} → {ordered/idempotent replay procedure} → {reconcile affected rows/aggregates and checkpoint} |
| Acknowledged RPO | {accepted loss terms} compared with {observed stranded writes and coverage}; record actual loss, preserve target evidence; outside terms or unknown scope blocks release |

**Source high-water marks:** {exact version-specific reset statements and verification,
or evidenced not applicable}. “Task running” alone is not lossless. Do not use native
MySQL 8.4 → external 8.0 as a lossless branch; use the approved supported alternative.

## Reconcile every client, then release source writes

Fill one row per client, including any client whose switch failed midway.

| Client | Persistent revert + upstream deploy/reconciler state | Runtime reload/restart + effective endpoint | Source AND target session evidence |
|---|---|---|---|
| {service/job} | {original unit/drop-in/env/config/secret/DNS revision; exact revert and read-back} | {daemon-reload/redeploy/pool refresh; PID/start identity, redacted effective args/env} | {source read/auth success; target writers/transactions absent or independently contained with write state reconciled} |

| ✅ | Step | Verification / command |
|---|------|------------------------|
| ▢ | 4. Verify persistent source config and deployment origin | {all changed layers match approved originals; automation cannot reapply target config} |
| ▢ | 5. Reload/redeploy and refresh processes/pools while writes remain fenced | {effective runtime uses source; only read-safe clients/probes started; startup writers remain stopped} |
| ▢ | 6. Verify data and both-side sessions | {data branch resolved, high-water marks checked, source read/auth healthy, target writers contained, inventory reconciled} |
| ▢ | 7. Release SOURCE application fence | {exact command}; only after steps 1–6 pass within accepted A5 scope |
| ▢ | 8. Verify authorized source write/read and clients; exit maintenance | {write probe and session evidence; target stays isolated} |

A source-connected old process with a target-pointing unit file is **not recovered**.
A reverted file with a target-connected live pool is **not recovered**. Failed checks
retain fences and maintenance; do not skip to step 7.

## Afterwards

Record times, actual write pause/loss (unknown where unverified), config/process/session
evidence, root cause and remaining work in the plan and dashboard. Retain target and
replication resources. Before another window, re-run the full validation phase and
refresh affected preparation/rehearsal and authorization. Decide reseed/resume from proven
direction, checkpoint and data history, never an unconditional `resume-processing`.
