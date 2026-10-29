# Execute reviewed steps without rebuilding the supervisor

Read before the first long-running operation, or when resuming a failed/interrupted
session. Use `shared/scripts/execution_step.py` for local POSIX jobs on Python 3.9+.
It runs one operation and its verifier, publishes through `dashboard_update.py`,
and retains a unique attempt directory. It has no database/cloud dependencies.

## Prepare once, reuse the tested revision

Prepare method-specific operation and verification scripts before the execution window.
Exercise their real flags, result serialization, failure exits and rollback branches on
the approved rehearsal environment. Reuse that reviewed revision with explicit source,
target and client parameters. Reusing code grants no permission on newly bound resources;
their identities and actions must fit the existing accepted scope or a new authorization.
Do not write another polling loop merely to supervise these scripts.

The scripts must check the expected database/resource identities before acting, obtain
secrets on-host, and keep credentials out of argv, generated files and diagnostic logs. The one
exception is the narrow on-host application-config staging of `cutover-procedures.md`
§Credential delivery, under **all** of its conditions: an authorized step whose A-block names the
host, live file and staged file; the staged file only on the customer's application host (never
the workstation, the engagement repo, `evidence/`, the handoff bundle or any log — including this
helper's captured logs); the live file's owner, group and mode applied atomically; verification
only with `shared/scripts/config_diff.py` on that host (allow-listed keys, no values otherwise);
removal or revert after cutover is verified or when the rollback window closes, recorded in the
plan.
The helper captures stdout/stderr into **private local logs**, not automatically safe
handoff material. Shell pipelines need explicit `bash` and `set -o pipefail`, or checked
producer and consumer exits. An exit code alone never validates a migration.
Commands must remain in the foreground and wait for all work they start. The helper
observes the immediate child process, not daemonized descendants or remote jobs.

This helper is **not an authorization service or sandbox**. It binds a reviewed
authorization record and script files by SHA-256, checks the declared effect against the
mode, and checks predecessor outcomes. It cannot authenticate a chat approval, classify
arbitrary SQL, prevent direct tool use, or prove that an operator-supplied evidence file
is truthful. Preserve the existing human gates, IAM/DB enforcement and exact scope.
Hashes and mode are checked at reservation and immediately before each command launch,
after any starting-checkpoint wait. Keep reviewed files and the workspace stable: these
checks do not freeze script dependencies, authenticate executable lookup, or continuously
enforce permission changes against a running process.
No extra approval is needed for already-authorized routine reads or local record repair;
changed scope, new production writes and an uncertain prior mutation need the applicable
decision before execution. Never fabricate a confirmation to satisfy a file check.

## One spec per reviewed operation

Paths below are relative to the engagement workspace. Replace hashes with the actual
reviewed bytes. Bind script dependencies/configuration as well as the entrypoints.
`authorization.reference` identifies the existing accepted block; it creates no approval.
For live cutover it must be A4 (not A4b), and for live rollback A5.

```json
{
  "phase": "6",
  "step": "load-orders",
  "label": "Load and verify orders",
  "plan": "migration-plan.md",
  "plan_section": "Phase 6",
  "dashboard": "dashboard",
  "effect": "target_write",
  "scope": "Approved target/database and orders table, using the reviewed load method",
  "authorization": {
    "path": "authorizations.md",
    "sha256": "<reviewed-record-sha256>",
    "reference": "GATE 2 — approved target load"
  },
  "inputs": [
    {"path": "load-orders.sh", "sha256": "<reviewed-script-sha256>"},
    {"path": "verify-orders.py", "sha256": "<reviewed-script-sha256>"}
  ],
  "operation": ["bash", "load-orders.sh"],
  "verification": ["python3", "verify-orders.py"],
  "interval_seconds": 60,
  "attention_after_seconds": 3600
}
```

`effect` is `read_only`, `target_write`, `rehearsal` (isolated approved resources only),
`cutover`, or `rollback`. Mode 1 permits only `read_only`; live cutover and rollback
require Mode 3. Mode 2 rehearsal does not authorize changes to production clients/source.
Source writes requiring A2 and decommission actions requiring A6 are **unsupported**.
Do not relabel them as `read_only`, `target_write`, or another supported effect to pass
this helper. Use the separately reviewed procedure and applicable approval for those
actions; this helper's effect model does not cover them.
Scaffold the plan and dashboard first. Do not use this local publisher while the active
dashboard is owned by the S3 soak scheduler; use that reference's coordinated writer.

```bash
python3 /installed-skill/shared/scripts/execution_step.py start \
  --workspace "$PWD" --attempt load-orders-001 --spec load-orders.json

python3 /installed-skill/shared/scripts/execution_step.py status \
  --attempt-dir "$PWD/.execution/load-orders-001" --wait-seconds 60
```

`start` reserves the name and starts a detached worker. A successful `start` means
**accepted for supervision**, not completed. `status` returns 0 only for verified,
persisted completion whose plan, dashboard step and activity checkpoint still agree;
1 for blocked/stale/attention-needed or unconfirmed completion; 3 for a pending worker
launch or continuing supervision. It checks the worker lock rather than inferring
liveness from a recent timestamp. Each status polling wait is finite (0–60 seconds);
local file access and scheduling can add latency.
The same worker publishes between turns, including during verification. The configured
sleep interval is at most 60 seconds; publication time and OS scheduling add overhead.
This is not a real-time heartbeat guarantee.
It never launches the next operation or automatically retries a mutation.

The verifier checks the actual outcome and writes
`$MIGRATION_ATTEMPT_DIR/verification.json` only after those checks:

```json
{
  "verified": true,
  "detail": "orders: full content comparison matches the recorded source snapshot",
  "evidence": [
    {"path": "evidence/orders-check.json", "sha256": "<observed-evidence-sha256>"}
  ],
  "dashboard_patch": {
    "migration_objects": {
      "tables": {"items": [{"name": "orders", "checksum_match": true, "status": "validated"}]}
    }
  }
}
```

The optional patch accepts only `migration_objects`, for observed object counts/status.
Follow the phase-specific checkpoint table in `SKILL.md`; a load-only check does not
establish checksum validation. Verifiers cannot approve a gate or set cutover readiness.
Keep evidence queries in the verifier under supervision, not after a premature `done`.

## Continue or recover

- **Next sequential step:** set `previous` to the latest attempt name in the same
  `.execution` directory. Its successful result, plan, dashboard step and log checkpoint
  must still agree. One chain is sequential; do not launch overlapping writers through
  separate state roots as a workaround.
- **Active job or finite status-wait expiry:** observe that same directory again.
  Neither a vanished terminal session nor a missing PID proves database completion.
- **Execution budget exceeded:** the worker requests attention and keeps observing the
  current process until it exits. It does not kill a database operation, proceed to the
  verifier, or turn a late exit into success. The operator decides how to stop/reconcile
  it under the approved abort plan. Budget expiry is checked before launching and at
  process observations; it is not an interrupt or a wall-clock deadline for file I/O.
- **Failed operation:** the result remains `blocked`. A recovery spec uses a different
  `step`, `previous: "<failed-attempt>"`, and `recovery_for: "<failed-attempt>"`.
  Recovery investigates/fixes/verifies within its approved scope; it is not a disguised
  retry. Only after its completion may a *new* attempt repeat the original operation.
- **Lost worker, failed publication, or unknown mutation outcome:** stop advancement.
  Inspect the local process **and any remote task/DB effects** using already-authorized
  reads. Never resubmit a write just because its response was lost.
- **Completion checkpoint missing or changed:** status returns 1 and preserves the
  original result. Inspect and repair the local records before advancing. A later
  attempt can supersede the same dashboard step; use the latest attempt for advancement,
  and the retained result for historical reporting.

After that inspection establishes there is no active local or remote operation, record
the observed outcome, remaining uncertainty and recovery decision in nonsecret evidence.
`reconcile --attempt-dir ... --evidence-file ...` accepts:

```json
{
  "no_active_job": true,
  "detail": "Task ended; target contains a partial load; approved recovery is target cleanup",
  "evidence": [{"path": "evidence/reconciliation.json", "sha256": "<observed-evidence-sha256>"}]
}
```

Reconciliation publishes a **blocked** checkpoint and a separate `reconciliation.json`;
it never overwrites the failed result or claims success. Only the latest attempt can be
reconciled. A held worker lock or a recorded job PID whose exit is unobserved and which
still exists prevents reconciliation;
neither a missing PID nor an unrecorded launch proves that all work has stopped.
The evidence must cover those uncertainties and any remote effects.
Reconciliation seals an unstarted reservation before publication, so a delayed worker
cannot launch it. If publication fails, repair local publication and repeat reconciliation
with valid inspection evidence. `status` then reports the separate reconciliation record.
Recovery and a fresh attempt still follow the preceding rule.
If the helper itself cannot read/persist these records, repair local publication first.
Keep original records; do not hand-edit a failure into success.

## Evidence and limits

Each attempt retains the reviewed spec, start record, current state, raw local logs,
verifier evidence references, and terminal result when known. Build reports from those
observations, preserving failed attempts and unresolved findings; use
[handoff-packaging.md](handoff-packaging.md) for a reviewed export. Result hashes prove
which bytes were reviewed, not production correctness or an atomic database snapshot.

These controls assume a trusted local workspace and writers using the helpers. They do
not implement remote task reattachment, distributed locks, Windows process supervision,
process-tree supervision, host/power-loss durability guarantees, automatic rollback,
or a universal MySQL dump/restore procedure. If a worker or publisher loses access to
storage, supervision can stop while the operation continues; no next command is launched.
For unsupported environments,
adapt the transport and test equivalent checkpoint/recovery behavior before live use.
Prepare and rehearse that adapter outside the cutover window.
