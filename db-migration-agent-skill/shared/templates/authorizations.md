# Authorizations of Record — {engagement} ({source} → {target})

> **This file is the audit anchor.** Every genuine authorization moment lives here as its
> own block — chat approvals are working conversation, not the record. Each block carries
> a **date/timestamp only** — no name, no role, no other identifying detail, by design. A
> block's `**Confirmed:**` line is filled in directly by whoever is present at that moment;
> the agent drafts the block and appends it, then stops — it never fills in that line
> itself. `migration-plan.md` gate rows point at the corresponding block here. Rules and
> action classes: `shared/reference/engagement-safety.md`.
>
> Not every item below needs its own block. Items that just restate a decision already
> made and dated elsewhere (§1) are pure reference — nothing to confirm twice.

## 1. Engagement scope (reference only — decided and dated in `discovery-questions.md`, not re-confirmed here)

| | Value | Source |
|---|---|---|
| Engagement mode | {1 analysis-only / 2 migration-ready — customer executes cutover / 3 full-migration — agent executes cutover} | Mode question, Phase 0 |
| Mode 2 handover depth | {(a) full preparation / (b) light preparation} — n/a for Modes 1, 3 | discovery-questions.md #16 |
| Engagement parameters | rehearsal {…} · parallel run {N} ({risk tier: Low/Moderate/High} — signal: {e.g. "production-serving, live write traffic, zero/seconds downtime tolerance"}) · validation depth {…} · rollback {…} | discovery-questions.md #16 |
| Prior assessment report | {path/date of report that unlocks Mode 3 / "this engagement, Phases 1–3"} | fact, agent-recorded |
| IAM guardrail in place | {session policy / permissions boundary / simulate-proof} — Deny list active on {source ARNs} | fact, agent-recorded |

## 2. GATE 1 — Discovery + mode + engagement parameters

**Locked in `discovery-questions.md`, not duplicated here.** See that file's closing
block for the mark and date. GATE 1 line in `migration-plan.md` points there.

## 3. Standalone authorization blocks (appended as each moment arises)

Nothing pre-listed below — a block appears here only once its moment actually happens.
A Mode 2 engagement, for example, never gets an A4 block at all.

Template for every new block:

```
### {tag} — {short description of exactly what's being authorized}

{2–3 sentences: what this touches, why it's needed now, and what happens if declined
— e.g. "the migration can't proceed past this blocker" / "we'd redesign X instead"}

**Confirmed:** {date — filled in by whoever is present, when they're ready to proceed}
```

Tags in use, appended as they arise:

- **Mode 3 warnings** — stated and accepted (only if Mode 3 chosen; see
  `engagement-safety.md` §Mode 3 warnings for the required content of this block).
- **A1** — Read-only source access (assessment).
- **A2** — Source write (each one individually — a blocker fix, migration-user creation
  — never bundled).
- **A3** — Target/production infrastructure deploy.
- **GATE 2** — Method, cost, architecture, rollback strategy approved.
- **GATE 3** — Validation evidence accepted.
- **Soak-exit** — N consecutive green periods reached; parallel run may end.
- **A4** — Cutover execution (**Mode 3 only** — window, runbook version).
- **A4b** — Handover acceptance (**Mode 2 only** — package received, cutover ownership
  transferred).
- **A5** — Rollback execution (pre-authorized on stated abort criteria, or ad hoc).
- **A6** — Decommission (exact resource list).
- **Approver present at cutover** (**Mode 3 only**) — confirmed present for the window,
  no name — just the confirmation + date.

## 4. Waivers (recommended parameters skipped)

Each waiver is its own block, appended when it happens:

```
### Waiver — {what was skipped}

{Risk, stated plainly — what moves into the cutover window as a result}

**Confirmed:** {date}
```

## 5. Extended-assurance records (only if the engagement chose these parameters)

| | Value |
|---|---|
| Rehearsal convergence (repeat-until-converged) | run 1: {s} → run 2: {s} → run N: {s} (< 20% delta reached: {date}) |
| Reconciliation report sign-offs | {daily rows or ref to soak reports} |
