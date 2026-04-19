# Onboarding Quickstart

## 1. Publish the source repo
1. Push this repository to GitHub personal/org.
2. Mark it as a template repository.
3. Create an initial semver release such as `v0.1.1`.

## 2. Sign in locally
```bash
claude auth login --claudeai
codex login
```

Use login-based access for both tools. Do not make `OPENAI_API_KEY` part of the default local setup.

## 3. Bootstrap a consuming repo
```bash
git clone <platform-source-repo>
cd ai-dev-platform-requirements-2026-04-14

./bin/platform bootstrap \
  --target /path/to/consumer-repo \
  --adapter node-ts \
  --issue-project-key PROJ \
  --confluence-space SPACE \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --version v0.1.1
```

## 4. Validate the result
```bash
./bin/platform doctor --target /path/to/consumer-repo
```

## 5. Start day-to-day flow
```bash
./bin/platform new-spec PROJ-123 --target /path/to/consumer-repo --title "add queue health alerts"
```

Then:
- implement in a dedicated worktree/branch
- let local hooks run
- open a PR
- let required checks drive merge readiness

## 6. Enable GitHub-side Codex reviews
- connect ChatGPT/Codex to GitHub
- enable automatic Codex reviews or use `@codex` on PRs
- the shared `ai-gate` workflow is informational and does not require `OPENAI_API_KEY`

## 7. Upgrade later
```bash
./bin/platform upgrade --target /path/to/consumer-repo --to v0.2.0
```
