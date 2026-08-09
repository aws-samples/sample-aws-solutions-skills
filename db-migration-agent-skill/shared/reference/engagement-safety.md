# Engagement Modes & Production Safety

> Read this at **Phase 0** — before the first customer question. It defines the single
> decision that shapes the whole engagement (**which mode**), the per-engagement
> parameters chosen at GATE 1, the Mode-2 handover contract, the waiver protocol, and the
> IAM guardrails. The failure this file prevents: an agent doing more to a customer's
> production database than the customer actually asked for.

## The three modes (Mode question is asked FIRST, at Phase 0)

An engagement is exactly one mode. The number says how far the agent goes; **Mode 2 is
the default and the recommended posture for customer engagements.**

| | **Mode 1 — analysis-only** | **Mode 2 — migration-ready** (default) | **Mode 3 — full-migration** |
|---|---|---|---|
| **The agent does** | Read-only assessment: preflight, discovery, compatibility/blocker scan, sizing, client discovery, third-party sweep, method options, cost | Everything in Mode 1, **plus**: provision the target (CDK), migrate the data, validate with evidence, keep the target current, parallel-run with daily reports, prepare + rehearse the cutover runbook, arm the rollback path | Everything in Mode 2, **plus** executing the production cutover itself |
| **The customer does** | Reads the report and decides whether to proceed | **Executes the cutover** — their window, their tests, their on-call, their go/no-go | Approves the window, is present during it, accepts the risk |
| **What limits it** | Read-only DB user + read-only IAM session. Cannot write: a failing trigger is a *finding*, not an action | The **mode boundary** below + IAM guardrail denies the freeze-capable actions | Cutover authorization signed (A4) + approver reachable during the window |
| **Deliverable** | Assessment report + draft `migration-plan.md` + risk register + cost estimate | A **migrated, validated, soaked target** + the handover package (below) — the customer holds the last step | Migrated database in production + full audit record |

Mode rules:
- Ask the mode question **before anything else**; record it in the `migration-plan.md`
  header and `authorizations.md` §1.
- **Mode 2 is what you propose by default.** When a customer says "just migrate it,"
  present Mode 2 first and explain the split: the agent does the whole migration and hands
  them a rehearsed cutover they run themselves. Only offer Mode 3 if they explicitly want
  the agent to perform the cutover.
- Mode 3 is **locked unless an assessment exists** (this engagement's Phases 1–3 or a
  prior Mode-1 report) and requires the Mode-3 warnings below to be stated and recorded.
- Mode changes are **new approvals, not silent upgrades** — moving 1→2 or 2→3 mid-
  engagement needs its own authorization row.
- A **clone/staging rehearsal** is not a separate mode any more: it is a step available
  inside Mode 2 and Mode 3 (see engagement parameters), because measuring the cutover
  window on a clone is useful regardless of who ultimately executes it.

## 🔴 The Mode 2 boundary (what the agent must NOT do)

In Mode 2 the agent stops at the handover. Specifically, the agent does **not**:

1. **Freeze the source** — no `SET GLOBAL read_only/super_read_only`, no
   `default_transaction_read_only`, no stopping the customer's write clients.
2. **Repoint any client** — no edits to app config, systemd units, secrets, task
   definitions, ConfigMaps, or DNS records that would move traffic to the target.
3. **Stop the forward replication** or start the reverse channel as part of a cutover
   sequence (it may *create and connection-test* the reverse task so it is ready).
4. **Execute the cutover runbook** — the runbook is a deliverable, not something the
   agent runs.

If the customer asks for any of these inside a Mode-2 engagement, the answer is: "that's
the cutover, which is Mode 3 — I can prepare everything and hand it over, or we can change
mode with a new authorization." Say it plainly and record the exchange.

## Mode 2 handover contract

Before handover, ask the customer **how deep the preparation should go** — both are valid,
the difference is who operates the replication and how the runbook's timings are derived:

| | **(a) Full preparation** (recommended) | **(b) Light preparation** |
|---|---|---|
| What the agent does | Target built + data migrated + **CDC kept current** through the parallel-run period + daily evidence reports + **cutover rehearsed on a clone** | Target built + data loaded + validated once; **no ongoing replication** |
| Runbook timings | **Measured** on the rehearsal | **Estimated** — clearly labeled as such |
| Customer operates | The cutover only | Replication start, parallel-run, and the cutover |
| Trade-off | Agent touches the source read-path continuously (replication user, binlog reads) | Minimal ongoing agent involvement, but the customer loses the near-zero-downtime path and operates CDC unaided |

Record the choice in the plan. The handover package the customer receives:

1. **`cutover-runbook.md`** — step-by-step with an owner, expected duration, verification,
   and abort action per step; timings marked *measured* (a) or *estimated* (b).
2. **`rollback-runbook.md`** — the exact failback procedure, with the reverse-replication
   path armed-but-not-started (a) or the snapshot/PITR path documented (b).
3. **The client repoint list** — every client discovered in Phase 7.5 with the *exact*
   change each one needs (file, key, old → new value) and where that config is deployed
   from, so their team can land it in their own repo/pipeline.
4. **Validation evidence** — row counts, checksums, schema-object comparison, plus the
   parallel-run reports (a) and any customer-test-suite results.
5. **A named handover acceptance** (`authorizations.md` A4b): the customer confirms
   receipt and accepts ownership of the cutover step.
6. **A standing offer**: the agent stays available to *observe* and verify during the
   customer's cutover (read-only checks, bidirectional verification afterwards) without
   executing any of it.

## ⚠️ Mode 3 warnings (state these plainly before accepting Mode 3)

- The agent will **freeze the production source, repoint live clients, and switch the
  application to a new database.** During the freeze window the application cannot write.
- **A named approver must be reachable for the whole window** and pre-agree the abort
  criteria; the agent stops and asks whenever a criterion trips rather than deciding.
- **Timing is earned, not promised:** quote a write-pause budget only from a measured
  rehearsal, and treat rehearsal × 2 as the honest number.
- **A rollback path must be armed before the flip** — reverse replication, or write-log
  replay, or an explicitly acknowledged RPO loss window.
- Mode 3 is **never the default recommendation.** If the customer hasn't asked for
  agent-executed cutover specifically, propose Mode 2.

## Engagement parameters (chosen at Phase 1 discovery, locked at GATE 1)

How much ceremony an engagement gets is an **explicit choice per parameter**, never
inferred from a label — a customer's real constraints (window length, test assets,
appetite for rehearsal) don't collapse into a single number. Ask each one with the
recommended default; every deviation from a default is a recorded waiver.

| Parameter | Options | Default |
|-----------|---------|---------|
| **Rehearsal** | none / one clone rehearsal / repeat until the measured window converges (< 20% delta between runs) | **One clone rehearsal.** Repeat-until-converged when the write-pause budget is tight (≤ 60 s) or the estimate is business-critical |
| **Parallel-run length** | not run / N consecutive green periods (days, or hours for compressed engagements) | **7 consecutive green days** for a production-serving database; N ≥ 3 if the customer needs to move faster |
| **Validation depth** | counts + checksums + smoke test / + app-level & version-gap battery / + domain reconciliation aggregates | **counts + checksums + app-level checks**, plus the **customer's own test suite** whenever one exists (discovery Q18) |
| **Rollback strategy** | snapshot/PITR restore (with acknowledged RPO) / reverse replication (zero RPO) / write-log replay | **Reverse replication** when the engines support it; otherwise state the RPO plainly and get it acknowledged |
| **Approver(s)** | named person per action class; for Mode 3 also "present during the window" | Named in `authorizations.md` before any production-touching step |

Guidance to offer when the customer is unsure: *"If this database being wrong or down for
an hour would stop revenue or reach customers, take the full rehearsal, a 7-day parallel
run, and reverse replication. If nobody would notice until Monday, a single rehearsal and
a short parallel run is proportionate."* — that's advice, not a classification the agent
enforces.

## Waiver protocol

When the customer declines a recommended parameter (rehearsal, parallel-run length,
reverse replication):
1. State plainly, in one or two sentences, what risk moves into the cutover window.
2. Record the waiver in `authorizations.md` §Waivers — what was skipped, the risk as
   stated, who accepted it, date.
3. Recover what value you can (e.g. declined rehearsal → component-test every
   freeze-window command against the real target; see execution-runbooks.md §Rehearsal).
4. Never silently skip. A waiver the customer doesn't remember signing is a failure.

Why this matters: a declined rehearsal plus one untested engine-version-specific command
is all it takes to turn a 40-second freeze into a 5-minute write pause.

## Approvals of record

Chat approvals drift and scroll away. Every gate sign-off, action-class authorization,
and waiver lives in **`authorizations.md`** (template in `shared/templates/`), with a
named person and date. `migration-plan.md` gate rows point at the corresponding
authorization row. The customer can hand the file to an auditor.

Action classes requiring a row **before** first execution:
1. Read-only source access (assessment)
2. Source writes (blocker fixes, migration user creation — each listed individually)
3. Target/production infrastructure deploys
4. **Cutover execution — Mode 3 only** (window + runbook version)
   **4b. Handover acceptance — Mode 2** (customer received the package and owns the cutover)
5. Rollback execution (pre-authorized criteria vs ad-hoc)
6. Decommission (exact resource list)

## IAM guardrails

Generate a **session policy** for the operator role per engagement and record it in the
plan:

- **Mode 1**: allow only `Describe*`/`Get*`/`List*` on the relevant services +
  `ssm:StartSession`/`SendCommand` scoped to the source instances (needed to run read-only
  SQL). No `Create*`, no `Modify*`, no `secretsmanager:PutSecretValue`.
- **Mode 2**: the operator set from [preflight-iam-cost.md](preflight-iam-cost.md) §2 —
  minus the actions that could perform a cutover. Add these denies as the technical
  backstop for the mode boundary (belt to the procedural rule above):
  `secretsmanager:UpdateSecret`/`PutSecretValue` on the **application's** secret,
  `route53:ChangeResourceRecordSets` on the app's zone, and `ssm:SendCommand`/
  `StartSession` on the **application hosts** (source-DB host access stays, for read-only
  checks). The agent therefore *cannot* repoint clients even by mistake.
- **Mode 3**: the full operator set, still with the source-protection denies below.

Both Mode 2 and Mode 3 keep explicit denies that outlast any agent mistake until the
decommission stage is signed:

```json
{
  "Sid": "ProtectSourceUntilDecommission",
  "Effect": "Deny",
  "Action": [
    "ec2:TerminateInstances", "ec2:StopInstances",
    "rds:DeleteDBCluster", "rds:DeleteDBInstance",
    "rds:DeleteDBSnapshot", "ec2:DeleteVolume"
  ],
  "Resource": ["<source-instance-arn>", "<source-volume-arns>", "<target-cluster-arn>"]
}
```

- At decommission (Phase 9, after the signed authorization), the session policy is
  re-issued without the Deny — the *policy change itself* is the two-person control.
- Where session policies aren't practical (customer-provided credentials), fall back to
  an SCP-style permissions boundary or, minimally, `aws iam simulate-principal-policy`
  proof that the destructive actions are denied — and record which control is in place.
- **The fallback chain is acceptable, not a blocker.** When the customer cannot provision
  roles or attach policies on the engagement's timeline (common), the combination of
  (a) simulate-proof of what *would* be denied, (b) a customer-recorded procedural
  constraint ("read-only user only, no mutating calls"), and (c) the CloudTrail audit
  trail **is a valid guardrail for Mode 1**. State which level is in force and move on —
  do not stop the engagement demanding IAM changes the customer already declined. (Modes 2
  and 3 should still push harder for a hard control.)

## How this maps onto the phases

| Phase | Addition |
|-------|----------|
| 0 | **Mode question first** (1/2/3, Mode 2 recommended); mode gates the whole session; guardrail policy generated for that mode |
| 1 / GATE 1 | Engagement parameters chosen (rehearsal, parallel-run N, validation depth, rollback, approvers) + Mode-2 handover depth (a/b); mode + parameters signed in `authorizations.md` |
| 6.5 | Rehearsal per the chosen parameter (see execution-runbooks.md §Rehearsal) |
| 7.7 | **Parallel-run soak**: target stays current; daily `soak-report.md`; customer may point read-only traffic/load tests at the target; cutover readiness requires N consecutive green periods |
| 8 | **Mode 2** → assemble + verify the handover package, walk the customer through the runbook, sign A4b, then stop (offer read-only observation during their cutover). **Mode 3** → execute step-by-step with go/no-go per group, A4 signed first |
| 9 | Post-migration + decommission authorization row; guardrail Deny lifted only after it. In Mode 2, Phase 9 runs *after the customer reports a completed cutover* |
