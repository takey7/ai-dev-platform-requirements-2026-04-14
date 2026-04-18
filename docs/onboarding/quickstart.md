# Onboarding Quickstart

## 1. Publish the source repo
1. Push this repository to GitHub personal/org.
2. Mark it as a template repository.
3. Create an initial semver release such as `v0.1.0`.

## 2. Bootstrap a consuming repo
```bash
git clone <platform-source-repo>
cd ai-dev-platform-requirements-2026-04-14

./bin/platform bootstrap \
  --target /path/to/consumer-repo \
  --adapter node-ts \
  --issue-project-key PROJ \
  --confluence-space SPACE \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --version v0.1.0
```

## 3. Validate the result
```bash
./bin/platform doctor --target /path/to/consumer-repo
```

## 4. Start day-to-day flow
```bash
./bin/platform new-spec PROJ-123 --target /path/to/consumer-repo --title "add queue health alerts"
```

Then:
- implement in a dedicated worktree/branch
- let local hooks run
- open a PR
- let required checks drive merge readiness

## 5. Upgrade later
```bash
./bin/platform upgrade --target /path/to/consumer-repo --to v0.2.0
```
