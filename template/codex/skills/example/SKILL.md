---
name: example
description: |
  TEMPLATE — Replace with one paragraph that describes what this skill builds and
  when it should activate. Include trigger keywords,
  e.g., "build me a customer profile system".
  This text drives skill activation matching across Claude Code, Kiro, and Quick.
license: MIT
metadata:
  version: "1.0"
  author: aws-solution-skills
---

# <Skill Name>

## Purpose
1-2 paragraphs on what this skill builds and for whom.

## Knowledge sources
- `shared/reference/architecture.md` — architecture decisions and rationale
- `shared/reference/decision-tree.md` — conditional logic
- `shared/reference/aws-services.md` — service / model catalog
- `shared/reference/constraints.md` — known limits and gotchas
- `shared/patterns/cdk-stacks.md` — full CDK stack source
- `shared/patterns/lambda-handlers.md` — full Lambda handler source
- `shared/patterns/frontend-pages.md` — React + Tailwind + shadcn/ui pages
- `shared/examples/` — industry-specific golden examples

## Workflow

### Phase 1: Discovery (conversational requirements)
List discovery questions the agent asks the user. Skip what is already known.

⛔ **GATE 1**: summarize requirements, await user confirmation.

### Phase 2: Architecture Design
- Apply `shared/reference/decision-tree.md`
- Cost estimate from `shared/reference/aws-services.md`
- Verify regional availability via AWS Knowledge MCP

⛔ **GATE 2**: present design table, await user confirmation.

### Phase 3: Code Generation
Reference `shared/patterns/*` to emit files in this order:
1. `package.json`, `tsconfig.json`, `cdk.json`
2. `config/schema.yaml`
3. `lib/*-stack.ts`
4. `backend/lambdas/*/handler.ts`
5. `frontend/src/...`
6. `scripts/*.sh`

### Phase 4: Validate
`cdk synth` must pass.

### Phase 5: Deploy
Provide step-by-step deployment guide.

## Generation rules
- TypeScript + aws-cdk-lib v2
- Node 20+ Lambda, esbuild
- React 18 + Tailwind v3 + shadcn/ui (NOT Cloudscape)
- AWS SDK v3 modular imports

## Hard Constraints
List 1-line items. Full detail lives in `shared/reference/constraints.md`. Examples:
1. **<Constraint title>** — see `shared/reference/constraints.md` #N
2. ...

## When to call MCP
| When | MCP | Call |
|---|---|---|
| Region availability check | AWS Knowledge | `aws_get_regional_availability` |
| Latest model ID lookup | AWS Knowledge | `aws_search_documentation` |
| CDK construct verification | AWS Knowledge | `aws_read_documentation` |
