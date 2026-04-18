# AGENTS.md
> このファイルは Codex / GitHub review / Cloud 環境 / ローカル開発者向けの共通契約です。
> 変更時は platform team に確認してください。

## Repository overview
- Service name: <service-name>
- Runtime: <node/python/go/...>
- Primary language: <lang>
- Package manager: <npm/pnpm/pip/...>

## Atlassian defaults
- Jira project key: <PROJ>
- Confluence space: <SPACE>
- Default issue prefix in branches: <PROJ>

## Required commands
- Install: `<install-command>`
- Lint: `<lint-command>`
- Typecheck: `<typecheck-command>`
- Unit test: `<unit-test-command>`
- Integration test: `<integration-test-command>`
- Build: `<build-command>`

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
