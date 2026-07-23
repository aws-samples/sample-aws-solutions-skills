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

> **Language**: respond in the user's language (Korean → Korean). Code, CLI, CDK, SQL,
> and resource names stay in English.

## 🔴 Hard constraints (never violate)

1. **`migration-plan.md` is the source of truth.** Create it from
   `shared/templates/migration-plan.md` at Phase 0; record every result, decision + why,
   and sign-off as it lands. A step without its result written down is not done.
2. **Never write to the production source.** Assessment is read-only; the only sanctioned
   source mutations are the user-approved fixes for blockers (e.g. `ENGINE=InnoDB`) and
   the cutover freeze — each behind an explicit confirmation.
3. **The user approves the method, the cost, and the cutover** (GATES 2 and 4). Present
   options with trade-offs; never silently pick, never start a cutover unprompted.
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
9. **The engagement mode and criticality tier govern the ceremony** (see
   `shared/reference/engagement-safety.md`): assessment-only sessions are physically
   read-only; production cutover requires the tier's requirements satisfied (rehearsal,
   soak green-days) **or a recorded waiver** — and approvals of record live in
   `authorizations.md` (named person + date), not in chat scrollback.

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
| `shared/reference/engagement-safety.md` | Phase 0 — engagement modes, criticality tiers + ceremony matrix, waiver protocol, IAM guardrails |
| `shared/reference/preflight-iam-cost.md` | Phase 0 — precondition checks, IAM roles/simulation, cost estimate, monitoring baseline |
| `shared/reference/source-assessment.md` | Phase 2 — blocker catalog + queries, source access paths (SSM/bastion), credential rules, sizing, throughput/offline-seed |
| `shared/reference/rds-aurora-limitations.md` | Phase 2 — full per-limitation detail behind the blocker tables |
| `shared/reference/method-selection.md` | Phase 3 — the 18-row decision matrix, binlog gate, multi-DB/cross-region/cross-account edges |
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
| `shared/reference/post-migration.md` | Phase 9 |
| `shared/reference/troubleshooting.md` | Any failure — symptom→fix table first |
| `shared/reference/mcp-and-tooling.md` | Session start if MCP available; anytime tooling questions arise |

## Workflow

### Phase 0: Preflight

1. Ask the **engagement-mode question first** (`shared/reference/engagement-safety.md`):
   **assessment-only** (read-only, ends with a report), **staging-rehearsal** (migrate a
   clone, never production), or **production-migration** (locked unless an assessment
   report exists). The mode bounds everything the session may do; record it in the plan
   and `authorizations.md` §1, and generate the mode's IAM guardrail policy.
2. Create `migration-plan.md` and `authorizations.md` from the templates in the working
   directory.
3. Ask the **current-state question**: fresh engagement / plan exists, resume at phase N
   / migration failed midway, triage? Resume from the plan file if it exists.
4. Run the precondition checks (`shared/reference/preflight-iam-cost.md` §1) — identity,
   account, region, source reachability, engine-version availability, quotas, IAM
   simulation. Report ✅/❌ table. **STOP on ❌ and wait.**
5. Note which MCP servers are connected (`shared/reference/mcp-and-tooling.md`).
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

Collect the 17 inputs in the plan template §Phase 1 — source engine/location, target,
size, **downtime tolerance**, **RPO on rollback**, usable bandwidth, schema-object needs,
app modifiability, **how each app finds the DB today**, downstream CDC consumers,
compliance mandates, **Korean security appliances and their mode**, multi-DB,
cross-region/account, KMS key type, **criticality tier** (#16 — use the "if this DB is
wrong for an hour, what happens?" guidance in engagement-safety.md; customers habitually
under-tier), **third-party tools on or in front of the DB** (#17 — security, backup,
monitoring, HA, proxy agents; customers usually forget these until asked), and the
**customer's own test suite** (#18 — regression/UAT/load tests their QA already runs;
these become acceptance gates executed against the target during rehearsal and soak —
integration mechanics in `shared/reference/customer-test-integration.md`: their tests
run in *their* CI/QA systems pointed at the target endpoint, never pasted into chat). "Go with recommendations" accepts all remaining defaults. Skip what's
already known.

⛔ **GATE 1** — summarize the inputs in the plan; user confirms before any assessment.
**Mode + tier are locked here** and signed in `authorizations.md` §3; the tier's ceremony
matrix (engagement-safety.md) is binding from this point.

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
rollback strategy, itemized cost, target architecture (Mermaid). User approves.

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

⛔ **GATE 3** — all validation green, recorded. No cutover date before this.

### Phase 7.5: Discover every DB client (mandatory)

Per `shared/reference/cutover-procedures.md` §client discovery: SG-ingress trace → each
client's connection config in **override order** (process args → env → systemd → config →
secret → hardcoded IPs; ECS task defs / K8s ConfigMaps / Lambda env for containerized
clients) → cross-check against the live processlist → plan for **downstream
replication/CDC consumers** (Debezium, replicas, ELT tools — they can't be repointed,
they restart from the target's coordinates). Pre-tune connection pools; disable ORM
auto-DDL. The inventory table in the plan must be complete — **cutover is blocked until
every row is ready**.

### Phase 7.7: Parallel-run soak (Tier 2/3 — cutover stays locked until it passes)

The target runs live and CDC-current while production stays on the source, for the
customer-chosen soak length (Tier 2 default: 7 consecutive green days; Tier 3 adds
performance validation and reconciliation — `shared/reference/engagement-safety.md`).
Each day: generate a report from `shared/templates/soak-report.md` (lag, spot
counts/checksums, alarms, drift) and send it to the customer; any RED day resets the
consecutive-green counter. Client discovery (7.5) runs alongside the soak. Invite the
customer to point read-only test traffic or load tests at the target during this window.
Cutover scheduling unlocks only at **N consecutive greens + the signed soak-exit row** in
`authorizations.md`. Shortening or skipping the soak is a waiver (engagement-safety.md
§Waiver protocol).

### Phase 8: Cut over

Instantiate `shared/templates/cutover-runbook.md` and `rollback-runbook.md` with real
values (zero placeholders). Reverse replication created and tested **before** the window
— or the alternative rollback strategy signed (RPO acknowledgment in the plan).

⛔ **GATE 4** — walk the user through the runbook; they approve the window and the
rollback strategy. Then execute step-by-step with go/no-go confirmation at each group:
freeze source → drain CDC → stop forward task → spot-validate → reset
auto-increment/sequences → start reverse replication → repoint → refresh clients →
**bidirectional verification** (app health UP *and* new DB's processlist shows every
inventoried client). Watch the abort criteria at T+15m/T+1h/T+24h.

### Phase 9: Post-migration

Per `shared/reference/post-migration.md`: refresh statistics, swap to the production
parameter group, scale down, compare against the Phase 2 baseline, keep the source +
reverse replication through the 7-day window, then decommission (with constraint 8's
confirmation). Hand over the CDK project + plan as the customer's operational record.

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
2. **`authorizations.md`** — mode/tier sign-offs, action-class authorizations, waivers —
   the customer's audit record.
3. **`{prefix}-migration/`** — the deployed CDK project (`shared/patterns/cdk-stacks.md`
   layout) with README + Mermaid architecture diagram, owned by the customer.
4. **`cutover-runbook.md` + `rollback-runbook.md`** — as executed, with measured timings
   — and, for Tier 2/3, the daily **soak reports**.
(Assessment-only mode delivers items 1–2 plus the assessment report; no infrastructure.)

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
