# Discovery Questions — {engagement} ({source} → {target})

> Check a lettered option and/or write your own answer on the **Answer:** line under each
> question, save, and let me know — or just answer any of these here in chat instead,
> either works, I'll re-read this file either way. Short on time? Reply "go with
> recommendations" and I'll use the called-out default for everything below, and flag
> every one I did that for when we lock this in.
>
> Already covered in chat: **#1** (source engine/location, and how you already connect to
> it) and **#2** (target service/version, network placement). Mode was chosen even
> earlier. This file picks up from **#3**.
>
> {Agent: generate this file after #1/#2 land in chat, tailored to that branch — e.g. drop
> the OS-access-dependent framing in #7/#9 if the source is a managed DB product. Fill
> every {bracketed} value with the real, engagement-specific default before writing this
> file — never ship a literal placeholder to the customer.}

---

### 3. Database size

Total data + index size, and the count/size of your largest tables. This is the single
biggest input to which migration method even qualifies (matrix rows are keyed on size
bands) and to the throughput-vs-window math in Phase 2 — a 2 TB database over a 100 Mbps
link is a multi-day copy no matter which tool moves it.

**Answer:** {total GB/TB, table count, largest table(s) and their size — or "not sure yet,
help me measure it"}

---

### 4. Downtime tolerance

How long can the application be unable to write during the actual cutover? Seconds,
minutes, or a maintenance window measured in hours? This is the other half of the
method-matrix lookup, and it sets the bar the cutover rehearsal has to clear before anyone
quotes a real number.

**Answer:** {a duration, or "we don't know yet — help us figure out what's reasonable"}

---

### 5. RPO on rollback

If a rollback ever happened, how much recently-written data could you tolerate losing?
Zero (nothing) shapes the rollback strategy toward reverse replication; a nonzero window
(minutes/hours) opens up simpler snapshot/PITR-based rollback instead.

**Answer:** {zero / a stated window / "need to ask the business"}

---

### 6. Usable network bandwidth, source → AWS

The *actual* sustained throughput on the path the data will travel (VPN/Direct
Connect/internet), not the link's rated speed — those are often very different. This
feeds directly into "how long will the initial copy take," which is what decides whether
a multi-TB migration needs the offline-seed (Snow Family/DataSync) branch instead of
copying over the wire.

**Answer:** {Mbps, or "not measured — we'll run an iperf3 test"}

---

### 7. Schema-object needs

Do you rely on stored procedures, triggers, views, or events living in the database
itself? Some migration methods (notably DMS) don't carry these — they need separate
handling — so this changes what has to happen alongside the data copy, not just the data
copy itself.

- [ ] A. None of these — plain tables only
- [ ] B. Yes, a few — willing to list them
- [ ] C. Yes, extensively — this is a real part of the application logic
- [ ] D. Not sure — check for me

**Answer:** {A/B/C/D, or your own answer}

---

### 8. Application modifiability

Can your application's code be changed as part of this migration, or does it need to work
completely unmodified against the new database? This matters most for heterogeneous moves
(different engine families), where some amount of query/driver-level difference is
sometimes unavoidable.

**Answer:** {yes, can modify / no, must be unmodified / n/a — same engine family}

---

### 9. How each application finds the database today

For every application/service that connects: where does it get the host — an environment
variable, a systemd unit file, a config file, a secret, or (worth knowing now, not at
cutover) a hardcoded IP? This is what makes the difference, at cutover, between "update
one secret" and "we have to find and edit a script on a server with no deployment
pipeline."

**Answer:** {list what you know now — the agent will also independently verify this
during Phase 7.5's client discovery, so a partial answer here is fine}

---

### 10. Downstream CDC/replication consumers

Does anything else read this database's change stream today — a Debezium connector, a
replica, an ELT/analytics pipeline? These can't simply be "repointed" at cutover the way
an application can; they need to restart from the target's own coordinates, which is a
different, separate plan.

**Answer:** {list any, or "none that we know of"}

---

### 11. Compliance mandates

Any regulatory requirements that shape this migration — data residency, encryption
standards, retention rules, audit requirements (e.g. Korean PIPA, ISMS-P, network
separation)? These can constrain target region, encryption configuration, or network
architecture before anything gets built.

**Answer:** {list any, or "none that apply"}

---

### 12. Korean security appliances and their mode

Do any Korean-market DB security/encryption products sit in front of or inside this
database (e.g. plug-in-mode encryption, an agent-based access-control tool)? Some of
these run in a mode that's fundamentally incompatible with a managed database (no
OS-level agent access on RDS/Aurora) — knowing this now avoids discovering it as a
late-stage blocker.

**Answer:** {product name(s) + mode, or "none"}

---

### 13. Multiple databases on the same host

Is this the only database on this server, or are there others — and if others, do any of
them share cross-database queries with the one being migrated? Cross-database joins don't
survive a move to a single-database managed instance without a redesign.

**Answer:** {single DB / multiple, no cross-queries / multiple, with cross-queries — describe}

---

### 14. Cross-region or cross-account target

Does the target need to live in a different AWS region or a different AWS account than
where this migration is being planned from? This changes networking (VPC peering/transit
gateway), KMS key policies, and snapshot-sharing mechanics — worth knowing before
architecture is drafted, not after.

**Answer:** {same region/account / different region / different account / both}

---

### 15. KMS key type

Encrypt the target with an AWS-managed key (simplest, no extra cost) or a customer-managed
key/CMK (you control rotation and access policy, needed if compliance requires it)?

- [ ] A. AWS-managed key — simplest default
- [ ] B. Customer-managed key (CMK) — we need control over rotation/access policy
- [ ] C. Not sure — recommend one for us

**Answer:** {A/B/C, or your own answer}

---

### 16. Engagement parameters

Four related decisions that together set how much ceremony this engagement gets. None of
these are inferred from a label — each is its own explicit choice.

**16a. Rehearsal.** Should the cutover be rehearsed against a clone before the real thing?

- [ ] A. One clone rehearsal (**recommended default**)
- [ ] B. Repeat rehearsals until the measured time budget converges (< 20% delta between
      runs) — worth it if the write-pause budget is tight (≤ 60s) or the cutover is
      business-critical
- [ ] C. None

**Answer:** {A/B/C, or your own answer}

**16b. Validation depth.** How thorough should post-migration validation be?

- [ ] A. Row counts + checksums + a smoke test
- [ ] B. All of A, plus app-level checks (collation, timezone, auto-increment high-water
      marks) and a version-gap battery if crossing a major version (**recommended
      default**)
- [ ] C. All of B, plus domain-specific reconciliation aggregates

**Answer:** {A/B/C, or your own answer — note: your own test suite, if you have one (see
#18), runs regardless of which level you pick here}

**16c. Parallel-run (soak) length.** A parallel run means: your old database keeps
running production traffic exactly as it does today — nothing is at risk yet. The new
database sits alongside it, receiving every change in real time, and gets watched for
some number of days before the actual cutover is recommended.

A one-time check can only see what's true right now — it can't see a job that runs once a
week at 2 AM, and that's not hypothetical: exactly this kind of thing has been caught by a
soak window before. A multi-day window turns "looked fine when we checked" into "actually
keeps working."

For your case the signal is: **{tier signal, e.g. "non-production, no live write traffic,
hours of downtime tolerance"}** — so the proposed length is **{N} day(s)** ({tier} risk
tier). If you keep it, checks run automatically on AWS-managed infrastructure (not your
own machine — that's not reliable left running for days), and you'll get one link, sent
once at the start, showing the dashboard live for the whole window.

- [ ] A. Keep the proposed {N} day(s)
- [ ] B. Shorten it — say how many days/hours below
- [ ] C. Skip it entirely — this is a waiver; the risk that moves into the cutover window
      is {state plainly, per `engagement-safety.md` §Waiver protocol}
- [ ] D. Not sure — give me one line of advice for a case like mine

**Answer:** {A/B/C/D — if B or C, say why: that reasoning is what gets recorded alongside
the choice}

**16d. Rollback strategy.**

- [ ] A. Reverse replication (zero RPO) — **recommended default when the engines support it**
- [ ] B. Snapshot/PITR restore — requires acknowledging a nonzero RPO (see #5)
- [ ] C. Write-log replay

**Answer:** {A/B/C, or your own answer}

**Mode 2 only — 16e. Handover depth.**

- [ ] A. Full preparation — CDC kept current through the parallel run, cutover rehearsed
      on a clone, measured timings (**recommended default**)
- [ ] B. Light preparation — target built and loaded, validated once, no ongoing
      replication; you operate CDC and the parallel run yourselves; runbook timings are
      estimated, not measured

**Answer:** {A/B, or your own answer}

---

### 17. Third-party tools on or in front of the database

Any security, backup, monitoring, HA, or proxy tools attached to this database that
you're aware of? (The agent runs a detection sweep regardless of your answer here — this
question exists because customers usually remember one or two that a pure technical scan
might miss context for, like "that's just for the Tuesday-night backup job.")

**Answer:** {list any, or "none that we know of"}

---

### 18. Your own test suite

Do you have your own regression, UAT, or load tests that already run against this
database? If so, these become real acceptance gates — run against the target during
rehearsal and the parallel run, in *your* CI/QA systems pointed at the target endpoint
(never pasted into chat).

**Answer:** {yes — describe briefly / no}

---

### 19. Named operational contact, source side

Not who *decides* things (that's the approver on each authorization block) — who would
you actually call if something on the source host looked odd during this migration? A
DBA/ops handle, not a manager.

**Answer:** {a role/contact description is fine — e.g. "on-call DBA rotation," a Slack
channel, whatever's real for your team}

---

### 20. Post-migration ownership

Once this migration is complete, who operates the target going forward? Distinct from
#16e's handover depth (which is about how much prep work happens now) — this is about who
owns it afterward, and feeds the Phase 9 decommission/handoff step.

**Answer:** {a role/team description}

---

### GATE 1 — Lock in discovery, mode, and engagement parameters

Once #1–20 above (across this file and chat) are answered, this is the actual GATE 1
sign-off — not a separate step in `authorizations.md`. Locking this in means the chosen
mode and engagement parameters are binding from here on; any later deviation is a recorded
waiver, not a quiet change.

**Confirmed:** {date — filled in once you're satisfied with the summary above; the agent
never fills this in itself}

---

*End of file. Answer as many as you'd like, in any order — reply here in chat when you're
done, or just start talking and I'll re-read this file on my next turn either way.*
