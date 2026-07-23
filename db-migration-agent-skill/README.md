# db-migration-agent-skill

An Agent Skill that turns your coding agent — **Claude Code, Kiro, or Codex** — into a
database migration engineer. You describe the database you want moved to AWS; the agent
plans and executes the migration end to end, and **you approve every decision that
matters** before it happens.

It moves self-managed databases (on EC2, on-premises, or another cloud) to **Amazon
Aurora or Amazon RDS**. Supported sources: MySQL, MariaDB, PostgreSQL, Oracle, SQL
Server, Db2 — plus playbooks for heterogeneous moves (e.g. SQL Server → Aurora
PostgreSQL) and Korean-market engines and security appliances.

## What using it feels like

You start by telling the agent something like *"we run MySQL on an EC2 instance and want
to move to Aurora — we can tolerate about a minute of downtime and can't lose data."*
From there:

1. **Two framing questions.** Before anything else, the agent asks what kind of
   engagement this is — a **report only** (it inspects and recommends, guaranteed
   read-only), a **practice run** against a clone of your database, or the **real
   migration** — and **how critical the database is** ("if it's wrong for an hour, what
   happens?"). Those two answers decide how much ceremony everything else gets: a dev
   database gets a fast path; an order system gets a required rehearsal and a soak
   period; a payments ledger gets timed dress rehearsals and daily reconciliation.

2. **A short interview.** ~17 questions, one at a time, each with a recommended default —
   say **"go with recommendations"** and it skips to the end. It asks the things teams
   forget until it's too late: who else reads this database? any backup/monitoring/
   security agents on the host? what happens to you if a rollback loses ten minutes of
   writes?

3. **It inspects your environment — read-only.** Connects via SSM (no SSH keys, no
   passwords typed anywhere), scans for the things that break managed databases
   (unsupported storage engines, encryption plugins, privileged code), measures the
   database, finds **every application that connects to it** (including the ones hiding
   in cron jobs and hardcoded IPs), and captures a performance baseline. Anything it
   finds becomes a written finding — nothing is "fixed" without your sign-off.

4. **You approve the plan.** It presents the migration method it recommends (and the
   ones it rejected, with reasons), the downtime forecast, the rollback strategy, the
   target architecture, and an **itemized monthly + one-time cost**. Nothing is built
   until you say yes.

5. **It builds and migrates.** The target (Aurora/RDS) is created from a CDK project you
   keep — encrypted, private, monitored, with alarms live before any data moves. Data
   moves by the fastest safe method for your case (often native tools, not DMS), while
   your application keeps running on the old database.

6. **It proves the copy is right.** Row counts on every table, checksums on critical
   ones, your stored procedures executed on the target, and — for business-critical
   databases — a **soak period**: the new database runs in parallel, kept current in
   real time, with a daily green/red report. The cutover stays locked until it has been
   green for N consecutive days (you pick N) and a named person signs off.

7. **Cutover night runs from a rehearsed script.** You approve the window. The agent
   freezes the old database, drains the last changes, flips your applications to the new
   endpoint, and verifies **in both directions** — your app reports healthy *and* the new
   database shows every expected client connected. Measured pause is reported to you
   straight, whatever it is. Reverse replication is armed **before** the flip, so for
   the whole rollback window (default 7 days) the old server stays a live, current
   standby — one command fails everything back with zero data loss.

8. **Nothing is deleted without written sign-off.** The old server is only decommissioned
   after the rollback window closes and you've signed the exact teardown list.

## What you'll be asked — and never asked

**You'll be asked** at four gates: to confirm the interview answers, to approve the
method + cost, to accept the validation evidence, and to green-light the cutover window.
Every approval is recorded in a generated `authorizations.md` with a name and date — an
audit record you can hand to your security team. Skipping a safety step (like the
rehearsal) is possible, but only as a **written waiver** with the risk spelled out.

**You'll never be asked** to paste a password into chat (credentials live in AWS Secrets
Manager and are fetched on-host), to approve vague actions ("can I fix some things?" —
every production write is itemized), or to trust "it worked" without evidence.

## What you get

| Artifact | What it is |
|---|---|
| `migration-plan.md` | The running record — every decision and why, every result, every gate |
| `authorizations.md` | Who approved what, when — including any waivers |
| `{prefix}-migration/` | A deployable CDK (TypeScript) project for the new database — yours to keep |
| `cutover-runbook.md` / `rollback-runbook.md` | The scripts as executed, with measured timings |
| Soak reports | Daily green/red evidence during the parallel-run period (Tier 2+) |

## What this skill is NOT — read before setting expectations

The skill is deliberately scoped. These are the things a customer might reasonably
assume it does, but it doesn't — each is a conscious boundary, not an oversight:

**1. It does not edit your application code.** Connection *config* changes (endpoint in
a secret, systemd unit, ConfigMap) are in scope — delivered as an exact change spec your
team commits to your own repo. Application *logic* is not: for heterogeneous moves it
emits `app-remediation-findings.md` (file:line, offending construct, replacement) as a
handoff for your dev team — or a second agent session opened in your repo with your
tests. It never asks for access to your source control.

**2. Oracle RAC (and other multi-writer/clustered sources) are NOT supported as
like-for-like migrations.** There is no RAC on RDS or Aurora — a RAC estate cannot be
"migrated" by this skill; it requires an architecture redesign (single writer + readers)
that is its own project, owned by your architects. The skill will detect a RAC source
during assessment, say exactly this, and stop short of promising a migration. What IS
supported for Oracle: single-instance Oracle → RDS for Oracle (lift-and-shift), and
single-instance Oracle → Aurora PostgreSQL as a conversion project where the skill runs
the data workstream and hands your team the code-conversion findings. For any
heterogeneous move, remember the app/code conversion — not the data — is usually the
schedule.

**3. Tight downtime numbers are earned, not promised.** Generous windows (hours) are
routinely achievable; tight budgets (≤ 60–120 s) depend on details that only a rehearsal
surfaces — engine-version syntax quirks, connection-pool behavior, per-step dispatch
latency. The skill's rule: quote a cutover window only from a **measured rehearsal**,
and treat rehearsal × 2 as the honest budget. If someone needs a guaranteed sub-minute
cutover with no rehearsal, this skill will refuse to promise it — that's a feature.

**4. It is not unattended.** A named human approves the mode, tier, method, cost, and
cutover, and is reachable during the window. The skill is designed to *stop and ask*
when an abort criterion trips mid-cutover rather than decide on its own — which only
works if someone is there to answer.

**5. Large-scale migrations are planned honestly, not waved through.** The decision
matrix has explicit > 1 TB paths — XtraBackup + S3 physical seed with CDC catch-up
(MySQL-family), Oracle transportable tablespaces (EE-only, with real preconditions),
SQL Server full+diff+log chains (≤ 64 TiB per native restore), and an offline-seed
branch (Snow Family / DataSync) with the record-the-replication-position discipline that
keeps an offline seed lossless. But hard bounds remain that no tool changes: moving N
terabytes takes `N / usable-bandwidth` — the skill computes this in Phase 2 and routes
to Snow (adding days–weeks of device shipping) rather than pretending; Aurora storage
caps at 128 TiB; a single S3 dump object at 5 TiB. For any multi-TB engagement treat the
rehearsal as mandatory regardless of tier, and note that multi-TB + near-zero-downtime +
heterogeneous *combined* is a phased program, not one engagement. The skill will give
you an honest plan and run the data workstream — expect it to tell you things (shipping
time, multi-week CDC catch-up, phased cutover) that no tool can make disappear.

**6. Some sources have no tooling fast-path.** Tibero, CUBRID, Altibase (no AWS
DMS/SCT support): the skill plans a PoC + JDBC extraction path and says so plainly
rather than implying DMS will "just work".

**7. Not covered at all**: sharded/multi-master topologies as such (Galera,
Group Replication — flagged as redesigns), NoSQL sources, data-warehouse migrations
(Redshift territory), ongoing post-migration DBA operations (that's the `aws-database`
skills' ground), and BYO-license procurement decisions (it surfaces the LI/BYOL choice;
your licensing team owns it).

## Layout

Follows the [aws-samples/sample-aws-solutions-skills](https://github.com/aws-samples/sample-aws-solutions-skills)
conventions — one canonical `SKILL.md`, byte-identical per tool, all knowledge in `shared/`:

```
db-migration-agent-skill/
├── README.md
├── claude-code/skills/db-migration-agent/SKILL.md   ★ canonical source — edit THIS one
├── kiro/skills/db-migration-agent/SKILL.md          ★ md5-identical copy
├── codex/skills/db-migration-agent/SKILL.md         ★ md5-identical copy
├── shared/
│   ├── reference/
│   │   ├── preflight-iam-cost.md           Phase 0 — preconditions, IAM roles, cost, monitoring baseline
│   │   ├── source-assessment.md            Phase 2 — blockers, access paths, credential rules, sizing, Snow branch
│   │   ├── rds-aurora-limitations.md       Full blocker/adjustment catalog with queries
│   │   ├── method-selection.md             Phase 3 — 18-row decision matrix, binlog gate, edge cases
│   │   ├── heterogeneous-migration.md      SCT / DMS SC / Babelfish; Tibero/CUBRID/Altibase
│   │   ├── target-provisioning.md          Phase 4 — Aurora vs RDS, immutable settings, RDS Proxy, TLS gate
│   │   ├── execution-runbooks.md           Phase 6 — per-method procedures, schema objects, rehearsal
│   │   ├── dms-best-practices.md           DMS sizing, task settings, LOB handling
│   │   ├── aws-official-migration-methods.md  All 33 AWS-documented methods
│   │   ├── validation-patterns.md          Phase 7 — counts/checksums/app-level/version-gap
│   │   ├── version-upgrades.md             Major-version-gap behavioral changes
│   │   ├── cutover-procedures.md           Phases 7.5–8 — client discovery, freeze, reverse replication, rollback
│   │   ├── post-migration.md               Phase 9
│   │   ├── troubleshooting.md              Symptom → fix
│   │   ├── mcp-and-tooling.md              AWS MCP Server / Agent Toolkit / companion MCP servers
│   │   ├── engagement-safety.md            Modes, criticality tiers, waivers, IAM guardrails
│   │   ├── third-party-db-security.md      Third-party DB security tools (global + Korean deep-dive)
│   │   └── regulatory-compliance.md        Korean regulatory mandates (PIPA, network separation, ISMS-P)
│   ├── patterns/
│   │   └── cdk-stacks.md                   The CDK project the skill generates
│   └── templates/
│       ├── migration-plan.md               Working artifact (source of truth per engagement)
│       ├── authorizations.md               Named-approver audit record
│       ├── soak-report.md                  Daily green/red gate during the parallel run
│       ├── cutover-runbook.md              Instantiated with real values at Phase 8
│       └── rollback-runbook.md
├── evals/                                  Black-box eval scenarios (expected-behavior checklists)
│   ├── ec2-mysql-to-aurora-scenario.md
│   └── sqlserver-to-aurora-pg-scenario.md
```

## Install

`shared/` must be installed alongside `SKILL.md` so relative references resolve.

```bash
# Claude Code
mkdir -p ~/.claude/skills/db-migration-agent
cp claude-code/skills/db-migration-agent/SKILL.md ~/.claude/skills/db-migration-agent/
cp -r shared evals ~/.claude/skills/db-migration-agent/

# Kiro
mkdir -p ~/.kiro/skills/db-migration-agent
cp kiro/skills/db-migration-agent/SKILL.md ~/.kiro/skills/db-migration-agent/
cp -r shared evals ~/.kiro/skills/db-migration-agent/

# Codex
mkdir -p ~/.agents/skills/db-migration-agent
cp codex/skills/db-migration-agent/SKILL.md ~/.agents/skills/db-migration-agent/
cp -r shared evals ~/.agents/skills/db-migration-agent/
```

## MCP requirements (optional — CLI fallback always works)

Recommended: the **AWS MCP Server** via the Agent Toolkit for AWS
(`/plugin install aws-core@claude-plugins-official` in Claude Code), which also provides
`retrieve_skill` for chaining the official `dms-schema-conversion` skill on heterogeneous
migrations. Optional companions: `awslabs.mysql-mcp-server` / `postgres` / `oracle` /
`mssql` (query source/target without local clients), `awslabs.cloudwatch-mcp-server`
(DMS metrics), `awslabs.aws-pricing-mcp-server` (GATE 2 cost estimate). Details:
`shared/reference/mcp-and-tooling.md`.

## Trigger phrases

**English**: "migrate database", "move to Aurora", "migrate to RDS", "EC2 MySQL to
Aurora", "SQL Server to Aurora PostgreSQL", "database cutover", "DMS migration",
"database modernization".
**Other languages**: the skill answers in the customer's language (Korean fully
supported) — trigger it with the equivalent phrases in that language.

## For operators: modes & tiers in one line each

Details in `shared/reference/engagement-safety.md`. Modes: `assessment-only` (physically
read-only), `staging-rehearsal` (clone only), `production-migration` (requires a prior
assessment). Tiers: 1 Standard (fast path), 2 Business-critical (default: rehearsal +
soak required), 3 Mission-critical (timed rehearsals to convergence, reconciliation,
war-room cutover). An IAM guardrail policy denies source-destructive actions until the
decommission sign-off.

## The opinionated choices

- **DMS is not the default.** Native tools (mysqldump, XtraBackup+S3, pg_dump, Data Pump,
  native .bak restore) are faster for homogeneous moves and carry schema objects; the
  matrix in `shared/reference/method-selection.md` is walked top-down, first match wins.
- **Four user gates** (inputs → method+cost → validation → cutover). Everything else the
  agent executes itself.
- **No cutover without a complete client inventory and a signed rollback path** —
  reverse replication where possible, write-log replay or explicit RPO acknowledgment
  where not.

## Contributing / editing

Edit **only** `claude-code/skills/db-migration-agent/SKILL.md` and `shared/`, then sync
the three copies from the repo root:

```bash
scripts/sync-skills.sh db-migration-agent-skill
scripts/sync-skills.sh verify
```

## License

MIT.
