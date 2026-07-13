<div align="center">

# AWS Solution Skills

**Multi-tool AI Skills for AWS solution patterns.**

Ship the *ability* to generate a solution — not a static template.
One source of knowledge, one `SKILL.md` per skill, deployed verbatim to **Kiro · Claude Code · Amazon Quick**.

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](./LICENSE)
[![Skills](https://img.shields.io/badge/skills-6-orange.svg)](#-skill-catalog)
[![Tools](https://img.shields.io/badge/tools-Kiro%20%C2%B7%20Claude%20Code%20%C2%B7%20Amazon%20Quick-232f3e.svg)](#-supported-tools)
[![Standard](https://img.shields.io/badge/spec-Anthropic%20Agent%20Skills-5A45FF.svg)](https://agentskills.io/specification)

</div>

---

## Table of contents

- [Why this exists](#-why-this-exists)
- [Skill catalog](#-skill-catalog)
- [How it works](#-how-it-works)
- [Supported tools](#-supported-tools)
- [Quickstart](#-quickstart)
- [Directory layout](#-directory-layout)
- [Cross-skill assets](#-cross-skill-assets)
- [Authoring workflow](#-authoring-workflow)
- [Adding a new skill](#-adding-a-new-skill)
- [MCP requirements](#-mcp-requirements)
- [Design principles](#-design-principles)
- [Contributing](#-contributing)
- [License](#-license)

---

## 💡 Why this exists

A template gives you one frozen architecture. A **skill** gives an AI agent the *knowledge* to design and generate the right architecture for **your** requirements, gathered in conversation.

Each top-level `*-skill/` folder packages one AWS solution use case as an **[Anthropic Agent Skill](https://agentskills.io/specification)**. You describe what you need; the skill produces tailored **CDK + Lambda + frontend** code, following a gated workflow (Discovery → Design → Generate → Validate → Deploy) with your confirmation between phases.

The same `SKILL.md` runs identically across three AI tools — the only difference is where each tool looks for skills on disk.

---

## 📦 Skill catalog

| Skill | Solution stack | Example trigger | Format |
|---|---|---|---|
| **[unified-customer-profile-skill](./unified-customer-profile-skill/)** | Amazon Connect Customer Profiles + Entity Resolution + Bedrock | *"Build me a unified customer profile system"* | `shared/` + thin SKILL.md |
| **[strands-agentcore-multi-agent-skill](./strands-agentcore-multi-agent-skill/)** | Strands Agents + Bedrock AgentCore (Runtime · Gateway · Memory) + MCP servers + Knowledge Bases | *"Build a Strands AgentCore multi-agent system"* | `shared/` + thin SKILL.md |
| **[graph-personalization-skill](./graph-personalization-skill/)** | Customer similarity graph (Neptune) + Bedrock explainable recommendations + Kinesis real-time | *"Graph-based personalized recommendations"* | `shared/` + thin SKILL.md |
| **[data-platform-pipeline-skill](./data-platform-pipeline-skill/)** | S3 (3-bucket) + Glue + Athena — *source → queryable data* | *"Build a data pipeline"* | Monolithic SKILL.md (~50 KB) |
| **[data-platform-consumption-skill](./data-platform-consumption-skill/)** | QuickSight + Amazon Quick chat agents — *queryable data → BI* | *"Set up a QuickSight dashboard"* | Monolithic SKILL.md (~60 KB) |
| **[llm-gateway-governance-skill](./llm-gateway-governance-skill/)** | LiteLLM gateway + Bedrock Guardrails + SSO/Cognito virtual keys + ECS Fargate + ALB edge (certMode: acm/http, SG CIDR-restricted) | *"Build an LLM gateway to govern Bedrock"* | `shared/` + thin SKILL.md |

> **Two authoring styles coexist** (both produce md5-identical `SKILL.md` across all three tools):
> - **`shared/` + thin `SKILL.md`** — deep knowledge lives in `shared/`; `SKILL.md` is a thin wrapper. *(unified-customer-profile, strands-agentcore, graph-personalization, llm-gateway-governance)*
> - **Monolithic `SKILL.md`** — everything self-contained in one ~50–60 KB file. *(the two data-platform skills)*
>
> See [CONTRIBUTING.md](./CONTRIBUTING.md) for when to choose which.

---

## ⚙️ How it works

Every skill ships **the same `SKILL.md` in three locations** — one per supported tool. The three copies are **byte-identical** (CI verifies md5). Tools differ only in install location.

```
                       canonical SKILL.md (single source of truth)
                                      │
                 ┌────────────────────┼────────────────────┐
                 ▼                    ▼                    ▼
          Kiro                 Claude Code          Amazon Quick
        ~/.kiro/skills/      ~/.claude/skills/   ~/.quickwork/skills/
                 │                    │                    │
                 └──────────── identical bytes ────────────┘
                                      │
                                      ▼
        Discovery → Design → Generate → Validate → Deploy   (gated workflow)
                                      │
                                      ▼
                  Tailored CDK + Lambda + frontend for your input
```

---

## 🛠 Supported tools

| Tool | Location in repo | Install path |
|---|---|---|
| **Kiro** | `<skill>/kiro/skills/<name>/SKILL.md` | `~/.kiro/skills/<name>/SKILL.md` |
| **Claude Code** | `<skill>/claude-code/skills/<name>/SKILL.md` | `~/.claude/skills/<name>/SKILL.md` |
| **Amazon Quick** | `<skill>/quick/skills/<name>/SKILL.md` | `~/.quickwork/skills/<name>/SKILL.md` |

---

## 🚀 Quickstart

Install any skill into your tool of choice. Replace `<skill>` with a folder from the [catalog](#-skill-catalog) and `<name>` with the skill name.

<details open>
<summary><b>Kiro</b></summary>

```bash
mkdir -p ~/.kiro/skills
ln -sf "$(pwd)/<skill>/kiro/skills/<name>" ~/.kiro/skills/<name>
ln -sf "$(pwd)/<skill>/shared" ~/.kiro/skills/<name>/shared   # only if the skill has shared/
```
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
mkdir -p ~/.claude/skills
ln -sf "$(pwd)/<skill>/claude-code/skills/<name>" ~/.claude/skills/<name>
ln -sf "$(pwd)/<skill>/shared" ~/.claude/skills/<name>/shared   # only if the skill has shared/
```
</details>

<details>
<summary><b>Amazon Quick</b></summary>

```bash
mkdir -p ~/.quickwork/skills
ln -sf "$(pwd)/<skill>/quick/skills/<name>" ~/.quickwork/skills/<name>
ln -sf "$(pwd)/<skill>/shared" ~/.quickwork/skills/<name>/shared   # only if the skill has shared/
```
</details>

> 💡 On Windows or environments without symlink support, replace `ln -sf` with `cp -r`. The destination path is unchanged — it's where each tool natively looks for skills.

Once installed, start your tool and use a trigger phrase from the catalog (e.g. *"Build an LLM gateway to govern Bedrock"*). The skill takes over from there.

---

## 📁 Directory layout

**Style 1 — `shared/` + thin SKILL.md** (deep-knowledge skills):

```
<solution-name>-skill/
├── README.md                                       ← skill overview
├── kiro/skills/<name>/SKILL.md                     ★ md5-identical to ↓
├── claude-code/skills/<name>/SKILL.md              ★ canonical source (Anthropic Skills format)
├── quick/skills/<name>/SKILL.md                    ★ md5-identical to ↑
├── shared/                                         ← single source of deep knowledge
│   ├── reference/
│   ├── patterns/
│   └── examples/
└── evals/<scenario>.md                             ← (optional) verification scenarios
```

**Style 2 — Monolithic SKILL.md** (self-contained skills):

```
<solution-name>-skill/
├── README.md
├── kiro/skills/<name>/SKILL.md                     ★ md5-identical
├── claude-code/skills/<name>/SKILL.md              ★ canonical source, ~50–60 KB self-contained
└── quick/skills/<name>/SKILL.md                    ★ md5-identical
```

Full specification: [`shared-spec/skill-structure.md`](./shared-spec/skill-structure.md).

---

## 🔗 Cross-skill assets

| Path | Purpose |
|---|---|
| [`sample-data/erp/`](./sample-data/) | Cosmetics-manufacturer ERP fixture (~40K rows) — shared by both data-platform skills |
| [`scripts/sync-skills.sh`](./scripts/) | Syncs the canonical `SKILL.md` to all three tool directories + md5 verification |
| [`shared-spec/`](./shared-spec/) | Authoring specs: skill structure, multi-agent format, shared-knowledge pattern |
| [`template/`](./template/) | Starting point for a new skill |

---

## 🔧 Authoring workflow

1. **Edit** the canonical `SKILL.md` (pick one tool dir, e.g. `<skill>/claude-code/skills/<name>/SKILL.md`).
2. **Sync** to the other two tools:
   ```bash
   scripts/sync-skills.sh <skill-dir>
   ```
3. **Verify** md5 parity across all three copies in every skill:
   ```bash
   scripts/sync-skills.sh verify
   ```

---

## ➕ Adding a new skill

Start from [`template/`](./template/) and read [`CONTRIBUTING.md`](./CONTRIBUTING.md). A new skill must satisfy:

1. The standard directory layout — [`shared-spec/skill-structure.md`](./shared-spec/skill-structure.md)
2. The `SKILL.md` format — [`shared-spec/multi-agent-format.md`](./shared-spec/multi-agent-format.md)
3. *(If using `shared/` style)* all real knowledge in `shared/`, with `SKILL.md` as a thin wrapper — [`shared-spec/shared-knowledge-pattern.md`](./shared-spec/shared-knowledge-pattern.md)
4. md5-identical `SKILL.md` across all three tool directories
5. *(Recommended)* at least one evaluation scenario under `evals/`

---

## 🔌 MCP requirements

Skills call **Model Context Protocol (MCP)** servers at runtime to verify volatile facts instead of hard-coding them.

| MCP server | Purpose | Required? |
|---|---|---|
| **AWS Knowledge MCP** | Service docs, regional availability, model IDs | Recommended (real-time verification) |
| **CloudFormation MCP** | Stack validation and deployment | Optional |
| **Bedrock MCP** | AI feature smoke-testing | Optional |

---

## 🧭 Design principles

1. **One SKILL.md, three tools** — the Anthropic Agent Skills standard is the lowest common denominator; per-tool format variation is needless drift surface.
2. **Shared knowledge** — `shared/` lets reference, patterns, and examples be written once and referenced from a thin `SKILL.md`.
3. **Gate pattern** — Discovery → Design → Generate → Validate → Deploy, with user confirmation between phases.
4. **MCP-first** — never hard-code volatile catalogs (model IDs, region availability); verify via MCP at runtime.
5. **Golden examples** — patterns are extracted from working reference projects, not idealized prose.
6. **No drift** — the three `SKILL.md` copies are byte-identical; CI enforces it.

---

## 🤝 Contributing

Contributions are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines, and [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) for community standards. To report a security issue, follow the [AWS vulnerability reporting](http://aws.amazon.com/security/vulnerability-reporting/) process — please do **not** open a public GitHub issue.

---

## 📄 License

This project is licensed under the **MIT-0 License** (MIT No Attribution). See [LICENSE](./LICENSE) for details.
