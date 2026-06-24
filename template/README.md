# <Solution Name> Skill — TEMPLATE

> Replace this README with skill-specific overview after copying. See [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Trigger phrases

```
"<Trigger phrase 1>"
"<Trigger phrase 2 in Korean if applicable>"
```

## Install

### Claude Code
```bash
mkdir -p ~/.claude/skills
ln -sf "$(pwd)/claude-code/skills/<skill-name>" ~/.claude/skills/<skill-name>
ln -sf "$(pwd)/shared" ~/.claude/skills/<skill-name>/shared
```

### Kiro
```bash
mkdir -p ~/.kiro/skills
ln -sf "$(pwd)/kiro/skills/<skill-name>" ~/.kiro/skills/<skill-name>
ln -sf "$(pwd)/shared" ~/.kiro/skills/<skill-name>/shared
```

### Amazon Quick
```bash
mkdir -p ~/.quickwork/skills
ln -sf "$(pwd)/quick/skills/<skill-name>" ~/.quickwork/skills/<skill-name>
ln -sf "$(pwd)/shared" ~/.quickwork/skills/<skill-name>/shared
```

(For Windows or if symlinks unavailable, use the `cp -r` form documented in CONTRIBUTING.md.)

## Outputs

This skill generates:
- `bin/app.ts`, `cdk.json`, `package.json`
- `lib/*-stack.ts` — CDK stacks
- `backend/lambdas/*/handler.ts` — Lambda handlers
- `frontend/src/` — React + Tailwind + shadcn/ui app
- `scripts/{deploy,destroy,check-prerequisites}.sh`
- `config/schema.yaml`

## MCP requirements

| MCP | Purpose | Required? |
|---|---|---|
| AWS Knowledge MCP | Service docs, regional availability, model IDs | Recommended |
| CloudFormation MCP | Stack validation | Optional |

## Knowledge sources

All real knowledge lives in [`shared/`](./shared/):
- `shared/reference/` — architecture, decision tree, AWS services catalog, constraints
- `shared/patterns/` — CDK / Lambda / Frontend code patterns
- `shared/examples/` — industry-specific instantiations

## Layout

```
<solution>-skill/
├── README.md                                       (this file)
├── claude-code/skills/<skill-name>/SKILL.md        (md5-identical to the other two)
├── kiro/skills/<skill-name>/SKILL.md               (md5-identical)
├── quick/skills/<skill-name>/SKILL.md              (md5-identical)
├── shared/{reference,patterns,examples}/
└── evals/<scenario>.md
```
