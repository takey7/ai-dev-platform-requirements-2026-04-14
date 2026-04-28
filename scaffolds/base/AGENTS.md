# AGENTS.md
> This file is the shared contract for Codex, Claude Code, GitHub review, and Cloud execution.
> Update it through the central platform source repo instead of editing it ad hoc.

## Repository overview
- Service name: {{SERVICE_NAME}}
- Runtime: {{ADAPTER}}
- Primary language: TypeScript
- Package manager: pnpm

## Atlassian defaults
- Jira project key: {{ISSUE_PROJECT_KEY}}
- Confluence space: {{CONFLUENCE_SPACE}}
- Default issue prefix in branches: {{ISSUE_PROJECT_KEY}}
- Orchestrator start label: `ai:auto`
- Orchestrator control commands: `/ai pause`, `/ai resume`, `/ai cancel`, `/ai retry`, `/ai status`

## Atlassian scope policy
- Only access Jira work in project `{{ISSUE_PROJECT_KEY}}`
- Only access Confluence content in space `{{CONFLUENCE_SPACE}}`
- Do not search, list, or summarize across all Jira projects or all Confluence spaces
- Start Jira searches with `project = {{ISSUE_PROJECT_KEY}}`
- Treat any other Jira project or Confluence space as out of scope unless the user explicitly names it and asks for cross-project work
- Create Jira issues only when the user explicitly instructs you to do so
- Any Jira issue you create must target project `{{ISSUE_PROJECT_KEY}}`
- Default Jira issue type for explicit issue creation is `Task`
- The resident orchestrator may only start work from Jira issues labeled `ai:auto` in project `{{ISSUE_PROJECT_KEY}}`
- The project control issue is reserved for `/ai pause-project`, `/ai resume-project`, and `/ai drain-project`

## Required commands
- Install: `{{INSTALL_COMMAND}}`
- Lint: `{{LINT_COMMAND}}`
- Typecheck: `{{TYPECHECK_COMMAND}}`
- Unit test: `{{UNIT_TEST_COMMAND}}`
- Integration test: `{{INTEGRATION_TEST_COMMAND}}`
- Build: `{{BUILD_COMMAND}}`

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

## Review guidelines
- Focus on changed files only
- For docs-only PRs, treat typos as P2 unless explicitly configured otherwise
- For API/schema/auth/infra changes, prioritize hidden breakage and rollback risk
- The repo default is automatic Codex review; `@codex review` is only a fallback if no Codex review artifact arrives
