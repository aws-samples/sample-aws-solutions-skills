# Engagement Modes, Criticality Tiers & Production Safety

> Read this at **Phase 0** — before the first customer question. It defines the two
> decisions that shape the entire engagement (mode and tier), the ceremony each tier
> requires, the waiver protocol, and the IAM guardrails. The failure mode this file
> prevents: not teams doing too little ceremony on purpose, but teams **not realizing
> which tier they're actually in**.

## Engagement modes (chosen at Phase 0 — the FIRST question)

An engagement is exactly one of these; each is separately scoped and approved. The mode
determines what the session is even *allowed* to do.

| Mode | What happens | What physically limits it | Deliverable |
|------|--------------|---------------------------|-------------|
| **assessment-only** | Phases 0–3 as read-only analysis: preflight, discovery, full compatibility/blocker scan, sizing, client discovery, third-party sweep, method recommendation, cost | Read-only DB user granted by the customer + read-only IAM session (see §IAM guardrails). The mode **cannot write** — a blocker like a failing trigger becomes a *finding in the report*, not an action | Assessment report + draft `migration-plan.md` + risk register + cost estimate |
| **staging-rehearsal** | The full migration executed against a **clone restored from a snapshot** — never production. Measures the real cutover window | Operates only on clone resources tagged for the rehearsal; production endpoints never appear in cutover scripts | Rehearsal report with **measured** step timings → the time budget quoted for production |
| **production-migration** | The real thing, Phases 0–9 | **Locked unless an assessment report exists** (this engagement or a prior one). Rehearsal expectations set by tier (below). IAM guardrail policy active | Migrated database + full audit record |

Mode rules:
- Ask the mode question before anything else; record it in `migration-plan.md` header
  and `authorizations.md` §1.
- A customer asking to "just migrate it" without a prior assessment → run the assessment
  phases first inside the production engagement (Phases 1–3 are the assessment), and say
  so; the point is the *report exists and was approved*, not bureaucracy.
- Mode transitions (assessment → production) are new approvals, not silent upgrades.

## Criticality tiers (chosen at Phase 1 discovery, locked at GATE 1)

Ask, with the guidance below — customers habitually under-tier:

> "If this database is wrong or unavailable for an hour, what happens? Nobody notices
> until Monday → Tier 1. Revenue stops or customers see errors → Tier 2. Regulatory
> breach, financial reconciliation breaks, or the company makes the news → Tier 3."

| | **Tier 1 — Standard** | **Tier 2 — Business-critical** (default) | **Tier 3 — Mission-critical** |
|---|---|---|---|
| Typical workload | Internal tools, dev/stage DBs, small sites | Order systems, SaaS backends, the main app DB | Payments, ledgers, regulated data, tenant platforms |
| Rehearsal | Recommended; waivable with recorded waiver | **1 full rehearsal required** (staging-rehearsal mode or clone inside the engagement) | **Repeated timed rehearsals until the measured window converges** (< 20% delta between consecutive runs) |
| Soak (Phase 7.7) | Not required | **Required** — default 7 consecutive green days (customer picks N ≥ 3) | Required — N ≥ 7, **plus performance validation**: top-N plan diffs vs baseline AND customer load test or read-only production traffic against the target |
| Validation depth | Row counts + checksums + smoke test | + app-level checks, version-gap battery if applicable, **+ customer's own test suite against the target (Q18) when one exists** | + **daily reconciliation reports** (domain aggregates, e.g. financial totals, agreed with the customer) + customer test suite required — no suite = a recorded waiver |
| Rollback | Snapshot restore acceptable **with explicit RPO acknowledgment** | Reverse replication required (or write-log replay + RPO sign-off where impossible) | Reverse replication required; phased cutover option offered (reads first, then writes; or service-by-service) |
| Cutover approval | Named approver in `authorizations.md` | Named approver + agreed abort criteria | Named approver **present during the window** (war-room), executive sign-off row |
| Waivers | Recorded | Recorded + named approver | Recorded + named approver + rationale; agent must restate the risk in one plain sentence |

The agent **enforces this matrix**: a Tier-2 cutover with zero rehearsals and no waiver
row is a blocked action, not a judgment call.

## Waiver protocol

When the customer declines any tier requirement (rehearsal, soak length, reverse
replication):
1. State plainly, in one or two sentences, what risk moves into the production window.
2. Record the waiver in `authorizations.md` §Waivers — what was skipped, the risk as
   stated, who accepted it, date.
3. Recover what value you can (e.g. declined rehearsal → component-test every
   freeze-window command against the real target; see execution-runbooks.md §Rehearsal).
4. Never silently skip. A waiver the customer doesn't remember signing is a failure.

Why this matters: a declined rehearsal plus one untested engine-version-specific
command is all it takes to turn a 40-second freeze into a 5-minute write pause.

## Approvals of record

Chat approvals drift and scroll away. Every gate sign-off, action-class authorization,
and waiver lives in **`authorizations.md`** (template in `shared/templates/`), with a
named person and date. `migration-plan.md` gate rows point at the corresponding
authorization row. The customer can hand the file to an auditor.

Action classes requiring a row **before** first execution:
1. Read-only source access (assessment)
2. Source writes (blocker fixes, migration user creation — each listed individually)
3. Target/production infrastructure deploys
4. Cutover execution (window + runbook version)
5. Rollback execution (pre-authorized criteria vs ad-hoc)
6. Decommission (exact resource list)

## IAM guardrails

Generate a **session policy** for the operator role per engagement and record it in the
plan. Structure:

- **assessment-only**: allow only `Describe*`/`Get*`/`List*` on the relevant services +
  `ssm:StartSession`/`SendCommand` scoped to the source instances (needed to run
  read-only SQL). No `Create*`, no `Modify*`, no `secretsmanager:PutSecretValue`.
- **staging-rehearsal / production-migration**: the operator set from
  [preflight-iam-cost.md](preflight-iam-cost.md) §2, **plus explicit Denies that outlast
  any agent mistake** until the decommission stage is signed:

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
  trail **is a valid guardrail for assessment-only mode**. State which level is in force
  and move on — do not stop the engagement demanding IAM changes the customer already
  declined. (Production-migration mode should still push harder for a hard control.)

## How this maps onto the phases

| Phase | Addition |
|-------|----------|
| 0 | Mode question first; mode gates the whole session; guardrail policy generated |
| 1 / GATE 1 | Tier question (with the under-tiering guidance); mode + tier locked and signed in `authorizations.md` |
| 6.5 | Rehearsal per tier (see execution-runbooks.md §Rehearsal; Tier 3 repeats until convergence) |
| **7.7 (new)** | **Parallel-run soak** (Tier 2/3): target stays CDC-current; daily `soak-report.md`; customer may point read-only traffic/load tests at the target; **cutover stays locked until N consecutive green days + signed cutover authorization** |
| 8 / GATE 4 | Cutover authorization row required; Tier 3 approver present |
| 9 | Decommission authorization row; guardrail Deny lifted only after it |
