# Authorizations of Record — {engagement} ({source} → {target})

> **This file is the audit anchor.** Every genuine authorization moment lives here as its
> own block — what was authorized and when. Each block carries a **date/timestamp only** —
> no name, no role, no other identifying detail, by design. **The customer never edits
> this file.** The agent drafts each block and, once the customer's reply **affirmatively
> accepts that exact block in full** — not merely a reply that mentions or partially
> addresses it — fills in its `**Confirmed:**` date itself, recording what was actually
> presented. A broader instruction like "proceed with execution" authorizes only the work
> it names — it is not advance confirmation of a block the customer hasn't actually seen
> yet; the agent waits for full acceptance of that specific block before writing anything.
> `migration-plan.md`
> gate rows point at the corresponding block here. Rules and action classes:
> `shared/reference/engagement-safety.md`.
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

Confirmed the same way as every block in §3 — a clear go-ahead in chat, agent fills in the
date — except the mark lives in `migration-plan.md`'s GATE 1 row instead of a block here,
since this content already lives in `discovery-questions.md`; no need to duplicate it.
Once you're satisfied with that file's summary, just say so in chat.

## 3. Standalone authorization blocks (appended as each moment arises)

Nothing pre-listed below — a block appears here only once its moment actually happens.
A Mode 2 engagement, for example, never gets an A4 block at all.

Template for every new block:

```
### {tag} — {short description of exactly what's being authorized}

{2–3 sentences: what this touches, why it's needed now, and what happens if declined
— e.g. "the migration can't proceed past this blocker" / "we'd redesign X instead"}

**Confirmed:** {date — the agent fills this in once a clear, specific reply addressing
this exact block lands in chat; never inferred from a broader "proceed" instruction}
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
