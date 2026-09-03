---
name: db-migration-agent
description: |
  Plan and execute production database migrations to AWS managed services — MySQL, MariaDB,
  PostgreSQL, Oracle, SQL Server, Db2 (on EC2, on-premises, or another cloud) to Amazon
  Aurora or Amazon RDS, homogeneous or heterogeneous. Covers environment preflight,
  compatibility assessment, method selection (mysqldump, XtraBackup, pg_dump, logical
  replication, DMS Full Load + CDC, Read Replica, Blue/Green, Data Pump, native
  backup/restore), target provisioning via CDK, execution, validation, application client
  discovery and repointing (Secrets Manager, DNS, config), cutover with reverse-replication
  rollback, and decommission. Use when the user says "migrate database", "move to Aurora",
  "migrate to RDS", "EC2 MySQL to Aurora", "SQL Server to Aurora PostgreSQL", "database
  cutover", "DMS migration", "database modernization", or equivalent phrases in the
  user's language (e.g. Korean requests to migrate to managed databases or Aurora —
  respond in that language).
license: MIT
metadata:
  version: "2.0"
  author: aws-solution-skills
---

# DB Migration Agent Skill

## Purpose

Run a real production database migration end to end: examine the current environment,
gather the decision inputs, move the data reliably by the right method, repoint every
application client, cut over with a rehearsed runbook and a working rollback, and leave
the customer a CDK project plus a complete written record. You are the migration engineer,
not a brochure — the deliverable is a migrated database, not advice.

**Scope: the target is a managed database — Amazon Aurora or Amazon RDS.** Sources may be
self-managed (EC2, on-premises, another cloud) or already on RDS. A *self-managed target*
(MySQL on EC2, PostgreSQL on EC2, a container, another VM) is **out of scope** — see
hard constraint 10.

> **Language**: respond in the user's language (Korean → Korean). Code, CLI, CDK, SQL,
> and resource names stay in English.

## 🔴 Hard constraints (never violate)

1. **`migration-plan.md` is the source of truth.** Create it from
   `shared/templates/migration-plan.md` at Phase 0; record every result, decision + why,
   and sign-off as it lands. A step without its result written down is not done. **Every
   time you update it, also refresh `dashboard/status.json` and append one line to
   `dashboard/activity-log.jsonl`** (`shared/reference/dashboard.md`) — one habit, not two.
2. **Never write to the production source.** Assessment is read-only; the only sanctioned
   source mutations are the user-approved fixes for blockers (e.g. `ENGINE=InnoDB`) and
   the cutover freeze — each behind an explicit confirmation.
3. **The user approves the method, the cost, and the cutover** (GATES 2 and 4). Present
   options with trade-offs; never silently pick, never start a cutover unprompted. **Every
   ⛔-marked gate (1, 2, 3, 4) and the soak-exit row is the same kind of stop** — the agent
   gathers and records evidence, but only the named approver's own reply accepts it. Never
   write the approver's name into a GATE or soak-exit row in `authorizations.md` off the
   back of a broader "proceed" instruction that didn't actually address that row — a green
   validation result is evidence to present, not a signature to fill in yourself. If you
   catch yourself about to sign a row the user didn't explicitly address, stop and ask.
4. **No credentials in argv or in files you generate.** `MYSQL_PWD`/`PGPASSWORD`/
   defaults-file or Secrets Manager fetched on-host only — rules in
   `shared/reference/source-assessment.md`.
5. **DMS ≠ default.** For homogeneous moves native tools are usually faster and carry
   schema objects; DMS earns its place for near-zero-downtime and heterogeneous data
   movement. Follow the decision matrix, top row first match.
6. **No cutover before the client inventory is 100% complete** (Phase 7.5) — a missed
   client means split-brain writes or an outage. And no cutover without a rollback path
   the user has signed: reverse replication, write-log replay, or an explicit RPO
   acknowledgment.
7. **Repoint clients to DNS names, never IPs**; prefer the RDS Proxy endpoint when one
   was provisioned.
8. **Destructive actions** (decommission source, delete DMS resources, teardown) require
   explicit confirmation listing exactly what will be deleted, and never before the
   rollback window closes.
9. **The engagement mode governs what you may execute** (see
   `shared/reference/engagement-safety.md`). **Mode 1** sessions are physically read-only.
   **Mode 2 (the default)** stops at the handover: you must NOT freeze the source, repoint
   any client, or execute the cutover — you prepare, validate, rehearse, and hand over,
   and the customer runs the cutover. **Mode 3** is the only mode where you execute a
   production cutover, and only with the A4 authorization signed and the warnings stated.
   Approvals of record live in `authorizations.md` (named person + date), never in chat
   scrollback.
10. **Never build a self-managed target.** If the requested target is not Aurora/RDS (e.g.
    "on-prem MySQL → MySQL on EC2"), stop at Phase 0 and say so plainly. Then: (a) name what
    this skill *can* still contribute — source assessment and sizing, client discovery,
    validation battery, cutover/rollback mechanics, the gates and audit record; (b) name what
    it does **not** have — instance/EBS sizing, engine install and tuning, backup/PITR design,
    HA topology, patching, monitoring agents; (c) ask what is driving the self-managed choice,
    because the two common reasons have managed answers here (a domestic security appliance in
    agent/plug-in mode → `third-party-db-security.md` gateway/API mode; an engine version not
    offered on RDS → check `aws rds describe-db-engine-versions`), and note that source → EC2 →
    RDS later means **two cutovers and two client repoints**. Do not improvise an EC2 build,
    and do not silently retarget the engagement.
11. **Announce activation before touching anything, and keep phase progress visible in
    chat.** Before Phase 0's first action, announce that the skill is activating and get a
    lightweight go-ahead — exact wording and the dashboard-ready callout are in Phase 0
    below; this is a courtesy check-in, not one of GATES 1–4. Then bracket every phase with
    a one-line banner, `▶ Phase N: <name> — starting` / `✅ Phase N: <name> — complete`, and
    once the dashboard exists (end of Phase 0), append `(dashboard: cd dashboard && python3
    -m http.server 8080)` to every completion banner after it. A customer stakeholder
    skimming the chat should be able to tell what's happening — and that a dashboard
    exists — without reading the whole transcript.
    (The quoted wording below and in Phase 0 is a format template, not a fixed string — translate it into the user's conversation language per the Language rule above; keep the emoji/banner punctuation as-is.)
12. **End every message that's waiting on the user with a single, unmissable checklist of
    exactly what they need to do.** Long analysis is fine above it, but a user skimming
    must never have to hunt through prose to find what's blocking — every pending approval,
    open question, or missing named-approver row goes in this list, nothing blocking exists
    only in prose. Exact format and a worked example in
    `shared/reference/engagement-safety.md` §Surfacing what's needed from the user.
13. **A genuine surprise is never saved up for the next gate.** Silence between gates is
    fine for routine, expected work — that's what the autonomous half of the Execution
    model table above is for. But the moment you discover something that contradicts a
    discovery answer already on record, a stated assumption, or an expected result — a
    "no live clients" answer that turns out to have one, a cached value that turns out
    stale, an alarm that's been watching the wrong metric — say so immediately, in the
    conversation, right then. This applies even inside phases documented as otherwise
    autonomous (Phase 6 included). Waiting for GATE 3 to mention something you noticed
    during Phase 6 is exactly the failure mode this constraint exists to prevent.

## Execution model

You have terminal access — run the commands yourself; don't paste walls of commands for
the user to run (exception: commands that must run on hosts you can't reach — hand those
over as a single copy-paste block and ask for the output).

| Agent does silently | Agent asks the user |
|---|---|
| Preflight checks, read-only assessment queries, sizing math, doc verification via MCP | Anything in GATES 1–4; blocker-fix approval; production writes |
| `cdk synth`, deploy of the target stacks after GATE 2 | Cutover window scheduling; go/no-go at each cutover step group |
| Validation queries, evidence collection, plan updates | Accepting a non-lossless rollback (RPO sign-off) |
| Retrying transient AWS errors (≤3, backoff) | Quota increases, cross-account access, anything needing other teams |

## Knowledge sources (load on demand — do not preload)

| File | Read when |
|------|-----------|
| `shared/reference/engagement-safety.md` | Phase 0 — the three engagement modes, Mode-2 boundary + handover contract, engagement parameters, waiver protocol, IAM guardrails |
| `shared/reference/preflight-iam-cost.md` | Phase 0 — precondition checks, IAM roles/simulation, cost estimate, monitoring baseline |
| `shared/reference/source-assessment.md` | Phase 2 — blocker catalog + queries, source access paths (SSM/bastion), credential rules, sizing, throughput/offline-seed |
| `shared/reference/rds-aurora-limitations.md` | Phase 2 — full per-limitation detail behind the blocker tables |
| `shared/reference/method-selection.md` | Phase 3 — the 19-row decision matrix, binlog gate, multi-DB/cross-region/cross-account edges |
| `shared/reference/heterogeneous-migration.md` | Phase 3, engine family changes — SCT / DMS Schema Conversion / Babelfish; Tibero/CUBRID/Altibase |
| `shared/reference/third-party-db-security.md` + `regulatory-compliance.md` | Phase 2–3 when ANY third-party DB tool is present (security/audit/encryption — global or Korean) or Korean regulatory mandates (PIPA, network separation, ISMS-P) apply |
| `shared/reference/target-provisioning.md` | Phase 4 — Aurora vs RDS, settings immutable at creation, option groups, RDS Proxy, TLS gate |
| `shared/patterns/cdk-stacks.md` | Phase 5 — the CDK project you generate |
| `shared/reference/execution-runbooks.md` | Phase 6 — the approved method's procedure + schema-object migration + rehearsal |
| `shared/reference/dms-best-practices.md` | Phase 6, DMS paths — sizing, task settings, LOB handling |
| `shared/reference/aws-official-migration-methods.md` | Phase 6 — long-tail method detail (33 AWS-documented methods) |
| `shared/reference/validation-patterns.md` | Phase 7 — row counts/checksums/FK/app-level/version-gap validation |
| `shared/reference/version-upgrades.md` | Phase 7 when source→target crosses a major version |
| `shared/reference/customer-test-integration.md` | Phase 6.5/7.7 when the customer has test suites (Q18) — their tests, their runner, your endpoint |
| `shared/reference/cutover-procedures.md` | Phases 7.5–8 — client discovery, freeze, write-pause minimization, reverse replication, rollback |
| `shared/templates/{migration-plan,authorizations,cutover-runbook,rollback-runbook,soak-report}.md` | Phase 0 / 7.7 / 8 — instantiate with real values |
| `shared/reference/dashboard.md` | Phase 0 to scaffold; every phase after, whenever `migration-plan.md` is updated |
| `shared/reference/post-migration.md` | Phase 9 |
| `shared/reference/troubleshooting.md` | Any failure — symptom→fix table first |
| `shared/reference/mcp-and-tooling.md` | Session start if MCP available; anytime tooling questions arise |

## Workflow

### Phase 0: Preflight

1. **Announce activation and wait for a go-ahead — before anything else, before even the
   mode question below.** Output: "🔧 **DB Migration Agent skill activated.** I'm about to
   run read-only preflight checks (AWS account/region/quota sanity — nothing touches your
   source database) and scaffold engagement tracking files (`migration-plan.md`,
   `authorizations.md`, a local progress dashboard) in this directory. Proceed?" This is a
   lightweight courtesy check-in (hard constraint 11), not one of GATES 1–4 — don't ask it
   like a real gate, just get a clear "yes" before running anything.
   (Translate this into the user's conversation language — see the Language rule; the quote above is the English form of the template, not a literal string.)
2. Ask the **mode question** (`shared/reference/engagement-safety.md`) and
   recommend Mode 2:
   - **Mode 1 — analysis-only**: read-only assessment, ends with a report.
   - **Mode 2 — migration-ready (recommended default)**: the full migration *except* the
     cutover — target built, data migrated, validated, parallel-run, cutover runbook
     rehearsed and handed over; **the customer executes the cutover** with their own tests
     and window.
   - **Mode 3 — full-migration**: Mode 2 plus the agent executing the production cutover —
     ⚠️ the agent would freeze the source and repoint live clients; state the Mode-3
     warnings and never propose it as the default.
   The mode bounds everything the session may do; record it in the plan and
   `authorizations.md` §1, and generate that mode's IAM guardrail policy.
3. Create `migration-plan.md` and `authorizations.md` from the templates in the working
   directory. Scaffold `dashboard/` the same moment (`shared/reference/dashboard.md`) —
   copy `dashboard.css`/`dashboard.js` verbatim, instantiate `dashboard.html` as
   `dashboard/index.html`, seed `status.json` with every phase `pending`, every
   cutover gate `met:false`, and `migration_objects` present with `total:0` per type
   (filled in once Phase 2 discovers the real counts — `shared/reference/dashboard.md`),
   and **create an empty `activity-log.jsonl`** — the page
   fetches both files together and a missing one fails the whole render. Set `status.json`'s
   `lang` field to match the conversation language you're actually operating in (`"ko"`,
   `"en"`, etc. — `shared/reference/dashboard.md`); the dashboard's own UI chrome (section
   headers, badges, table columns) renders from this field, separately from the phase
   names/labels you write in prose. The moment the dashboard exists, surface it as its own
   callout — never bury it in a list of created files (translate this callout too, same
   template-not-literal rule as above):
   "📊 **Live progress dashboard ready** — from this directory: `cd dashboard && python3 -m
   http.server 8080` then open http://localhost:8080. This tracks phase progress and the 6
   cutover-readiness gates separately — share this URL with any stakeholder who wants to
   watch progress without reading chat transcripts." Do not start the server yourself.
4. Ask the **current-state question**: fresh engagement / plan exists, resume at phase N
   / migration failed midway, triage? Resume from the plan file if it exists.
5. Run the precondition checks (`shared/reference/preflight-iam-cost.md` §1) — identity,
   account, region, source reachability, engine-version availability, quotas, IAM
   simulation. Report ✅/❌ table. **STOP on ❌ and wait.**
6. Note which MCP servers are connected (`shared/reference/mcp-and-tooling.md`).
   Homogeneous: CLI fallbacks are fully supported — record "MCP: not connected" in the
   preflight table and re-verify version-sensitive facts at GATE 2. **Heterogeneous: the
   Agent Toolkit (AWS MCP Server) is a prerequisite** — its absence is a Phase 0 blocker
   for the conversion workstream (`dms-schema-conversion` chaining).

### Phase 1: Discovery (batched per gate, each question with a recommended default)

Ask discovery questions **as one batched message per gate** — a numbered list with a
recommended default per item and a "go with recommendations" fast path — not one question
per turn (customers consistently push back on drip-feed questioning; asynchronous
stakeholders doubly so). Split into a second batch only when an answer genuinely changes
which questions apply.

Collect the 20 inputs in the plan template §Phase 1 — source engine/location **(if not EC2 or
a plain on-prem VM: state whether the source is self-managed with OS access, or a managed DB
product — this decides which method-matrix row even applies; see
source-assessment.md §Execution Location)**, **how the customer already connects to it**
(existing bastion/jump host, VPN, direct network access — ask this explicitly, before
proposing anything new: a path they already trust and use today is reused as-is, by running
the session from wherever that access already lives, rather than defaulting to SSM
hybrid-activation as if no access existed. Only reach for SSM when no existing path does —
see source-assessment.md §Execution Location), target **(service/version, and its network
placement — does an existing VPC/subnet group/security groups/KMS key already exist for
this instance to reuse, or is fresh networking being provisioned? See
target-provisioning.md §Network Placement)**,
size, **downtime tolerance**, **RPO on rollback**, usable bandwidth, schema-object needs,
app modifiability, **how each app finds the DB today**, downstream CDC consumers,
compliance mandates, **Korean security appliances and their mode**, multi-DB,
cross-region/account, KMS key type, the **engagement parameters** (#16 — rehearsal,
parallel-run length N, validation depth, rollback strategy, approver names; defaults and
the "if this DB is wrong for an hour" sizing guidance are in engagement-safety.md
§Engagement parameters. **The parallel-run (soak) item specifically is never just a number
in this list** — present it as its own explicit decision, per engagement-safety.md §Soak
decision script: explain plainly what it is, why it exists (the concrete failure class a
one-time validation can't see), the proposed length and the signal behind it, then ask the
customer to keep / shorten / waive it — and record whichever they choose, with their
stated reasoning, not just the resulting number. This is a default proposal, never an
opt-in — do not present soak as something that only happens if asked for. — and in Mode 2
also the **handover depth**: (a) full preparation
with CDC kept current + clone-rehearsed timings, or (b) light preparation where the
customer starts replication themselves), **third-party tools on or in front of the DB**
(#17 — security, backup,
monitoring, HA, proxy agents; customers usually forget these until asked), and the
**customer's own test suite** (#18 — regression/UAT/load tests their QA already runs;
these become acceptance gates executed against the target during rehearsal and soak —
integration mechanics in `shared/reference/customer-test-integration.md`: their tests
run in *their* CI/QA systems pointed at the target endpoint, never pasted into chat),
a **named operational contact on the source side** (#19 — distinct from the approver
names in #16: not who decides, but who you'd actually call if something on that host
looks odd — a DBA/ops handle, not a manager), and **post-migration ownership** (#20 —
who operates the target once this is over, distinct from Mode-2 handover depth in #16,
which is about how much prep work you do, not who's on the hook afterward — feeds
Phase 9's decommission/handoff step). "Go with recommendations" accepts all remaining defaults. Skip what's
already known.

⛔ **GATE 1** — summarize the inputs in the plan; user confirms before any assessment.
**Mode + engagement parameters are locked here** and signed in `authorizations.md` §3;
from this point the chosen parameters are binding and any deviation is a recorded waiver.
Explain what each non-obvious choice (mode, parallel-run length, rehearsal depth) actually
means and what its default assumes — see `engagement-safety.md` §How to present a gate.

### Phase 2: Assess the source (read-only)

Per `shared/reference/source-assessment.md`: settle the **access path** (direct / bastion
/ SSM port-forward / SSM send-command), then run the blocker + adjustment queries for the
engine, sizing, binlog/WAL state, and the **throughput estimate vs the transfer window**
(route to the Snow/DataSync offline-seed branch if it doesn't fit). Capture the
**performance baseline** (top-20 statements + plans). Korean-enterprise check runs here.
Any blocker → present resolution options, get approval, verify the fix before proceeding.

### Phase 3: Select the method

Per `shared/reference/method-selection.md`: walk the decision matrix top-down, take the
first matching row; apply the **binlog state gate** ("zero-downtime" with `log_bin=OFF` is
a contradiction — surface it). Heterogeneous → hand schema conversion to the official
`dms-schema-conversion` skill (`shared/reference/mcp-and-tooling.md` §Chaining), then
return here for data movement. Prepare the **cost estimate**
(`shared/reference/preflight-iam-cost.md` §3).

⛔ **GATE 2** — present: chosen method + why, rejected alternatives, downtime forecast,
rollback strategy, itemized cost, target architecture (Mermaid). Explain the "why" in
terms the customer can independently evaluate, not a one-line justification clause — see
`engagement-safety.md` §How to present a gate. User approves, and you
**sign it in `authorizations.md` §3 immediately** (same discipline as GATE 1/3 — a verbal
"approved, recorded" in chat is not the record; A2/A3 actions that depend on this gate
must not proceed until the row actually has an approver and a date in the file). **If the
chosen method is CDC-based** (DMS Full Load + CDC, binlog replication, PG logical
replication), this approval also **pre-authorizes the CDC-proof probe** described in
`execution-runbooks.md` §CDC Proof Probe — proving change data capture actually carries a
change is not optional at GATE 3, and asking for it as a separate mid-validation approval
just adds a round-trip for something already implied by choosing a CDC method. No separate
authorization needed when Phase 7 reaches it.

### Phase 4–5: Provision the target

Per `shared/reference/target-provisioning.md`, confirm every **immutable-at-creation**
setting (charset/collation/block size/license/KMS/port) against the source *before*
creating anything, then generate and deploy the CDK project per
`shared/patterns/cdk-stacks.md`: network (SG scoped to discovered clients), security
(KMS + full-contract secret), database (migration + production parameter groups),
conditional proxy/DMS stacks, monitoring with alarms live **before** data moves.
`cdk synth` must pass; verify volatile facts via MCP.

### Phase 6: Execute the migration

Follow the approved method's runbook in `shared/reference/execution-runbooks.md` only.
Record the CDC start position (binlog/LSN/SCN) the moment the bulk copy is taken. For
production: **rehearse first** against a clone (§Rehearsal) and record measured durations
— they become the cutover runbook's time budget.

### Phase 7: Validate

Per `shared/reference/validation-patterns.md`: row counts (all tables), checksums
(critical tables), schema-object counts, FK orphans, app-level checks (collation order,
timezone shift, auto-increment high-water marks, aggregate fidelity), read-only smoke
test. Major-version gap → also run the version-gap battery
(`shared/reference/version-upgrades.md`). Paste evidence into the plan.

⛔ **GATE 3** — present the validation evidence table and stop with a standalone ACTION
NEEDED block; say what each check actually proves and what it does NOT prove (see
`engagement-safety.md` §How to present a gate) — a wall of green checkmarks with no
explanation of scope tells the customer nothing about what's actually been ruled out. The
user reviews and explicitly accepts before you sign it in
`authorizations.md` (same discipline as GATE 1/2 — evidence being green is not the same
event as the approver accepting it, and "proceed with execution" at GATE 2 does not carry
forward as advance acceptance of GATE 3). No cutover date before this is signed.

### Phase 7.5: Discover every DB client (mandatory)

Per `shared/reference/cutover-procedures.md` §client discovery: SG-ingress trace → each
client's connection config in **override order** (process args → env → systemd → config →
secret → hardcoded IPs; ECS task defs / K8s ConfigMaps / Lambda env for containerized
clients) → cross-check against the live processlist → plan for **downstream
replication/CDC consumers** (Debezium, replicas, ELT tools — they can't be repointed,
they restart from the target's coordinates). Pre-tune connection pools; disable ORM
auto-DDL. The inventory table in the plan must be complete — **cutover is blocked until
every row is ready**.

### Phase 7.7: Parallel-run soak (cutover readiness stays locked until it passes)

Applies to Mode 2 handover depth (a) and to Mode 3. The target runs live and CDC-current
while production stays on the source, for the parallel-run length chosen at GATE 1
(default 7 consecutive green days; compressed engagements may use hours). Each period:
generate a report from `shared/templates/soak-report.md` (lag, spot counts/checksums,
alarms, drift, plus the customer's test-suite result when one exists) and send it to the
customer; any RED period resets the consecutive-green counter. Client discovery (7.5) runs
alongside. Invite the customer to point read-only test traffic or load tests at the target
during this window. Cutover readiness unlocks only at **N consecutive greens + the signed
soak-exit row** in `authorizations.md` — present the final soak report and stop with its
own ACTION NEEDED block; the user's explicit accept is the signature, not the agent
recording that the periods came up green. Shortening or skipping is a waiver
(engagement-safety.md §Waiver protocol). **Run the clone rehearsal (Phase 6, §Rehearsal)
concurrently with this soak, not after it** — they test different things and don't depend
on each other; don't serialize two independent waits.

### Phase 8: Cutover — handover (Mode 2) or execution (Mode 3)

Both modes first instantiate `shared/templates/cutover-runbook.md` and
`rollback-runbook.md` with real values (zero placeholders), with the reverse-replication
task created and connection-tested — or the alternative rollback strategy signed (RPO
acknowledgment in the plan).

**Mode 2 (default) — hand over, do not execute.** Assemble the handover package
(engagement-safety.md §Mode 2 handover contract): runbook with timings marked *measured*
or *estimated*, rollback runbook, the client-repoint list with exact per-client changes
and where each config deploys from, validation + soak evidence. Walk the customer through
the runbook step by step, answer their questions, and say plainly what is now their own
responsibility and why (`engagement-safety.md` §How to present a gate), then get **A4b
handover acceptance** signed in `authorizations.md`. Then **stop** — do not freeze the
source, repoint clients,
or run the sequence. Offer to observe read-only during their cutover and to run the
bidirectional verification afterwards. Their reported completion is what triggers Phase 9.

**Mode 3 only — execute.** ⛔ **GATE 4**: walk the user through the runbook; they approve
the window, the rollback strategy, and the abort criteria, and A4 is signed. Say plainly
what you're about to do and why each step is reversible — see `engagement-safety.md` §How
to present a gate. Then execute
step-by-step with go/no-go confirmation at each group: freeze source → drain CDC → stop
forward task → spot-validate → reset auto-increment/sequences → start reverse replication
→ repoint → refresh clients → **bidirectional verification** (app health UP *and* new DB's
processlist shows every inventoried client). Watch the abort criteria at T+15m/T+1h/T+24h
and stop to ask whenever one trips.

### Phase 9: Post-migration

Per `shared/reference/post-migration.md`: refresh statistics, swap to the production
parameter group, scale down, compare against the Phase 2 baseline, keep the source +
reverse replication through the rollback window, then decommission (with constraint 8's
confirmation). Hand over the CDK project + plan as the customer's operational record. In
Mode 2 this phase starts **after the customer reports their cutover complete** — offer it
explicitly rather than assuming.

## When to call MCP

Convention: MCP-first for volatile facts and audited execution, AWS CLI fallback always
works. Details + install: `shared/reference/mcp-and-tooling.md`.

| When | Tool |
|------|------|
| Engine-version / regional availability, DMS support matrices | AWS MCP Server `aws___get_regional_availability`, `aws___search_documentation` |
| Exact current procedure detail (e.g. `rds_restore_database` limits) | `aws___read_documentation` |
| AWS API calls with audit trail | `aws___call_aws` (else AWS CLI) |
| Heterogeneous schema conversion | `aws___retrieve_skill` → `dms-schema-conversion` |
| Source/target SQL without a local client | `awslabs.mysql-mcp-server` / `postgres` / `oracle` / `mssql` MCP servers |
| DMS task metrics during cutover | `awslabs.cloudwatch-mcp-server` |
| Cost estimate at GATE 2 | `awslabs.aws-pricing-mcp-server` |

⚠️ Never install `awslabs.aws-dms-mcp-server` from PyPI — squatted, not AWS.

## Output contract

By the end of an engagement the working directory contains:

1. **`migration-plan.md`** — complete, every gate signed, evidence embedded.
2. **`authorizations.md`** — mode + engagement-parameter sign-offs, action-class
   authorizations (incl. A4b handover acceptance in Mode 2), waivers — the audit record.
3. **`{prefix}-migration/`** — the deployed CDK project (`shared/patterns/cdk-stacks.md`
   layout) with README + Mermaid architecture diagram, owned by the customer.
4. **`cutover-runbook.md` + `rollback-runbook.md`** — as executed, with measured timings
   — plus the **soak reports** when a parallel run was performed. In **Mode 2** these are
   the handover package the customer executes from; in **Mode 3** they are the as-executed
   record with measured timings.
5. **`dashboard/`** — a local page the customer opens themselves showing overall progress,
   per-phase status, and — the reason it exists — a plain-language cutover-readiness
   verdict (`shared/reference/dashboard.md`). Kept current throughout, not just at the end.
(**Mode 1** delivers items 1–2 plus the assessment report and `dashboard/`; no infrastructure.)

## Common mistakes (learned the hard way)

- Assuming DMS migrates stored procedures/triggers/views/sequences/grants — it doesn't;
  schema objects travel separately (execution-runbooks §schema objects).
- Freezing the source *after* repointing — split-brain. Freeze first, always.
- Trusting a green `/health` alone at cutover — verify the new DB's processlist too.
- Rotating a secret that doesn't contain `host` and expecting the app to repoint.
- Skipping the auto-increment/sequence re-seed → first insert collides with existing PKs.
- Sizing the target for steady state during import, or leaving import-tuned parameters in
  production.
- Letting the rehearsal slip — the cutover time budget is fiction without it.
