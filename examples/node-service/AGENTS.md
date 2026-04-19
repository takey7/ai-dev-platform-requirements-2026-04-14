# AGENTS.md
> This file is the shared contract for Codex, Claude Code, GitHub review, and Cloud execution.
> Update it through the central platform source repo instead of editing it ad hoc.

## Repository overview
- Service name: node-service
- Runtime: node-ts
- Primary language: TypeScript
- Package manager: pnpm

## Atlassian defaults
- Jira project key: DEMO
- Confluence space: PLATFORM
- Default issue prefix in branches: DEMO

## Atlassian scope policy
- Only access Jira work in project `DEMO`
- Only access Confluence content in space `PLATFORM`
- Do not search, list, or summarize across all Jira projects or all Confluence spaces
- Start Jira searches with `project = DEMO`
- Treat any other Jira project or Confluence space as out of scope unless the user explicitly names it and asks for cross-project work

## Required commands
- Install: `pnpm install`
- Lint: `pnpm lint`
- Typecheck: `pnpm typecheck`
- Unit test: `pnpm test:unit`
- Integration test: `pnpm test:integration`
- Build: `pnpm build`

## Authentication
- Claude Code: use `claude auth login --claudeai`
- Codex CLI: use `codex login`
- Do not make `OPENAI_API_KEY` part of the default local workflow

## Review policy
### Always check
- correctness
- edge cases
- rollback impact
- logging/monitoring impact
- test coverage

### If these paths changed, treat as HIGH RISK
- `packages/contracts/**`
- `db/migrations/**`
- `infra/prod/**`
- `auth/**`
- `api/public/**`

### High-risk expectations
If HIGH RISK:
- challenge compatibility assumptions
- require explicit rollback plan
- require migration strategy
- require rollout strategy
- require observability notes

## PR expectations
Every PR must include:
- Issue key
- Spec link
- Risk class
- Breaking change yes/no
- Rollout strategy
- Rollback summary

## Spec requirement
Before implementation, create:
`docs/specs/<ISSUE_KEY>.md`

The spec must include:
- Objective
- Acceptance criteria
- Compatibility impact
- Migration plan
- Rollback
- Observability
- Test plan

## Local validation
Before considering work done, run:
1. lint
2. typecheck
3. unit tests
4. integration tests where relevant

## Notes for GitHub review
- Focus on changed files only
- For docs-only PRs, treat typos as P2 unless explicitly configured otherwise
- For API/schema/auth/infra changes, prioritize hidden breakage and rollback risk
