# AI Development Platform Source Repo
作成日: 2026-04-14 JST  
更新日: 2026-04-16

このリポジトリは、要件定義パックから発展した **platform source repo** です。  
今後の案件で再利用する標準開発フローを、ここで一元管理します。

## 何を source-of-truth にするか
- `docs/`:
  方針、要件、導入ガイド
- `scaffolds/`:
  consuming repo にコピーする base scaffold と stack adapter
- `.github/workflows/`:
  GitHub reusable workflows
- `.github/actions/`:
  consuming repo にコピーする composite actions
- `ops/platform/`:
  local hook と CI で共有する共通チェックロジック
- `scripts/platform.py`:
  正本 CLI
- `plugins/claude-platform-bootstrap/`:
  Claude Code から同じ CLI を呼ぶ補助導線
- `examples/node-service/`:
  bootstrap 後の完成形サンプル

## 採用する配布モデル
- `hybrid`
  中央 repo を source-of-truth にしつつ、consuming repo には薄い wrapper と共通 helper を同期する
- `repo-first`
  最初は repo-level ruleset と workflow で運用し、安定後に org-level へ昇格する
- `mixed`
  cross-cutting な値だけ `.platform/platform.yaml` に集約し、lint/build/test の詳細は native file に残す
- `versioned releases`
  consuming repo は中央 repo の ref/tag に pin する

## Quick Start
### 1. source repo をローカルで使う
```bash
git clone <this-repo>
cd ai-dev-platform-requirements-2026-04-14
chmod +x bin/platform scripts/platform.py ops/platform/checks.py
```

### 1.5. Claude / Codex をログイン方式で使う
```bash
claude auth login --claudeai
codex login
```

- Claude Code は `claude.ai` ログインを前提にする
- Codex CLI は ChatGPT ログインを前提にする
- `OPENAI_API_KEY` の手動設定は標準経路にしない

### 2. 初回だけ user-level defaults を保存する
```bash
./bin/platform configure \
  --github-owner takey7 \
  --projects-root ~/workspaces \
  --jira-site-url https://<site>.atlassian.net \
  --jira-admin-email you@example.com
```

Store the Jira provisioning token in the local macOS Keychain, or export it only for the command that needs provisioning:
```bash
security add-generic-password \
  -a "$USER" \
  -s ai-dev-platform.atlassian-api-token \
  -w "<jira-admin-token>" \
  -U
```

Temporary env export is also supported:
```bash
export ATLASSIAN_API_TOKEN=<jira-admin-token>
```

### 3. greenfield なら 1 コマンドで作る
```bash
./bin/platform create-project "Billing API"
```

- creates a local repo under `~/workspaces/<repo-name>`
- creates a GitHub private repository
- bootstraps the baseline
- creates a Jira Software Kanban project
- starts `claude` and `codex` in tmux by default

### 4. 空 repo / 既存 repo に標準基盤だけ導入する
```bash
./bin/platform bootstrap \
  --target /path/to/new-repo \
  --adapter node-ts \
  --issue-project-key PROJ \
  --confluence-space SPACE \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --version main
```

`bootstrap` is the canonical platform primitive. `create-project` is a convenience workflow that orchestrates repo creation and then applies the same baseline.

### 5. 既存 repo を検査する
```bash
./bin/platform doctor --target /path/to/existing-repo
```

### 6. issue spec を作る
```bash
./bin/platform new-spec PROJ-123 \
  --target /path/to/repo \
  --title "stabilize release ready gate"
```

### 7. consuming repo を更新する
```bash
./bin/platform upgrade \
  --target /path/to/repo \
  --to <new-tag>
```

### 8. Jira 駆動の常駐オーケストレータを登録する
```bash
./bin/platform orchestrator register \
  --target /path/to/consumer-repo
```

`ATLASSIAN_API_TOKEN` can come from the environment or macOS Keychain service `ai-dev-platform.atlassian-api-token`.

- worker config を `~/.config/ai-dev-platform/orchestrator.json` に作る
- `projects_roots[]` に repo の親ディレクトリを追加する
- Jira control issue を作るか再利用する
- 可能なら project-scoped Automation rule を 2 本作る
  - lifecycle -> `POST /jira/events/<PROJECT_KEY>`
  - comment control -> `POST /jira/events/<PROJECT_KEY>`
- project ごとに別の webhook secret を払い出す
- live rule 作成ができない場合でも、`.platform/orchestrator/automation-rules/` に rule payload blueprint を出力する

### 9. 常駐 worker を起動する
```bash
./bin/platform orchestrator run
```

- worker は single-process + SQLite WAL で state を保持する
- repo ごとに 1 issue だけ lease を取る
- `ai:auto` + ready status (`To Do`, `Selected for Development`) の issue を対象にする
- `ready_for_merge` で停止し、merge は人か既存 GitHub ルールに委ねる
- 恒久運用では `deploy/orchestrator/` の `systemd` unit と `Caddyfile` を使い、固定 URL で公開する

### 10. status / pause / resume / cancel
```bash
./bin/platform orchestrator status --project PROJ
./bin/platform orchestrator poll --issue PROJ-123
./bin/platform orchestrator status --issue PROJ-123 --refresh
./bin/platform orchestrator pause --issue PROJ-123
./bin/platform orchestrator resume --issue PROJ-123
./bin/platform orchestrator cancel --issue PROJ-123
```

`status` without `--refresh` reads the local SQLite state only. If the worker is not running, use `poll` or `status --refresh` to refresh GitHub checks/reviews and Jira reporting.

Jira 側でも次の comment commands を使えます。
- `/ai pause`
- `/ai resume`
- `/ai cancel`
- `/ai retry`
- `/ai status`
- project control issue 専用:
  - `/ai pause-project`
  - `/ai resume-project`
  - `/ai drain-project`

## Recommended tool versions
- Claude Code:
  `2.1.109` or newer is recommended as of April 15, 2026.
  Recent updates added plugin marketplace improvements and shipped a fix for a workspace-trust-related hook execution security issue in `2.1.108`.
- Codex:
  use the current CLI and sign in with ChatGPT. If you previously used API-key auth, run `codex logout` and then `codex login`.
- GitHub CLI:
  keep `gh` current so release and repository API support stays aligned with GitHub Cloud.
- Check your local versions with:
```bash
claude --version
gh --version
codex --version
```

## GitHub での使い方
### template repository
- この repo 自体を GitHub で template repository 化する
- `Use this template` は discovery と sample repo 作成に使う
- 標準更新は template 再生成ではなく `platform upgrade` を使う

### reusable workflows
- consuming repo の `.github/workflows/*.yml` は薄い wrapper にする
- required check 名は固定:
  - `ci`
  - `spec-gate`
  - `risk-classification`
  - `security-scan`
  - `ai-gate`
  - `release-ready`
- `ai-gate` は `OPENAI_API_KEY` を使う GitHub Action ではなく、ChatGPT/GitHub 連携済みの Codex review を使う運用ガイド check として動かす
- Codex review は GitHub 側で automatic reviews を有効化するのを既定にし、Worker は実 review artifact を待つ
- automatic review が来ない場合だけ、Worker が PR 上で `@codex review` を fallback として送る
- source repo を private にする場合は、GitHub Actions の repository settings で
  `Accessible from repositories owned by '<OWNER>'`
  を有効にしないと、他 repo から reusable workflows と actions を参照できない

### release source
- source repo 自身のリリースは `.github/workflows/platform-release.yml` で bundle asset を作る
- 他環境では `git clone` か GitHub release asset を使って CLI を取得する

## consuming repo に入る最小契約
- `.platform/platform.yaml`
- `AGENTS.md`
- `.claude/settings.json`
- `.mcp.json`
- `docs/specs/ISSUE_SPEC_TEMPLATE.md`
- `.github/workflows/*.yml`
- `.github/actions/*`
- `ops/platform/checks.py`
- `ops/ai/*.sh`

## Node/TypeScript v1 adapter
- Node 20
- pnpm
- TypeScript
- ESLint
- Vitest

base 層は stack-neutral を維持し、Node 前提の実装は `scaffolds/adapters/node-ts/` に閉じ込めています。

## Claude plugin
- marketplace catalog: `.claude-plugin/marketplace.json`
- plugin: `plugins/claude-platform-bootstrap/`
- 正式入口は CLI のまま
- plugin は `platform configure / create-project / bootstrap / doctor / new-spec / upgrade` を呼ぶ補助導線のみを提供する

## Atlassian / MCP
- project-scoped `.mcp.json` を consuming repo に配置する
- 標準 endpoint は `/v1/mcp`
- 認証は OAuth 2.1 前提
- API token は opt-in の補助用途のみ
- 各 consuming repo は 1 つの Jira project key を `issue.project_key` に固定する
- AI はその Jira project key と既定の Confluence space だけを扱い、他 project / space は明示的なユーザー指示がない限り対象外にする
- 通常運用の標準経路は `MCP + OAuth`
- `create-project` の Jira project 新規作成だけは provisioning plane として `REST + API token` を使う
- admin credential は generated repo に残さない
- Claude は明示的なユーザー指示がある場合にだけ、repo 固定の Jira project key に issue を作成してよい
- Jira issue 作成の標準 UI は CLI 追加ではなく Claude + MCP に固定する

### resident orchestrator v1
- 人間と Claude/Codex の通常開発では、引き続き repo-scoped MCP + OAuth を標準にする
- resident orchestrator は Jira event / control / sticky comment のために Jira REST を使う
- `Claude` と `Codex` は直接会話させず、worker が structured payload で baton pass する
- Jira の主経路は project-scoped Automation rule + `Send web request`
- `jira:issue_updated` に comment が載らないため、comment control は別 rule に分ける
- 固定 URL の単一 Worker で複数 project を同居させるが、event endpoint と webhook secret は project 単位で分離する
- GitHub 連携の inbound は webhook ではなく polling に固定する
- v1 の自動化は `PR まで自動`

## Jira template recommendation
- この標準開発フローの既定は `カンバン` が最適
- 理由: repo 単位の継続 delivery と相性がよく、sprint ceremony を前提にしないため導入コストが低い
- `スクラム` は、すでに固定長 sprint を運用しているチームだけに限定して選ぶ
- `Jira Product Discovery` のテンプレートは delivery 管理ではなく discovery / prioritization 用なので、repo ごとの開発 space の既定にはしない

## 今回の source repo で追加した主な実装
- `scripts/platform.py`
  bootstrap / configure / create-project / doctor / upgrade / new-spec
- `ops/platform/checks.py`
  local hook と CI で共有する spec/risk/security/release-ready logic
- `scaffolds/base/`
  共通契約のコピー元
- `scaffolds/adapters/node-ts/`
  v1 adapter
- `.github/workflows/`
  reusable workflows
- `.github/actions/`
  composite actions
- `.claude-plugin/` と `plugins/claude-platform-bootstrap/`
  Claude 補助導線

## 関連ドキュメント
- [導入 quickstart](docs/onboarding/quickstart.md)
- [初回プロジェクト walkthrough](docs/onboarding/first-project-walkthrough.md)
- [GitHub / 配布方針](docs/onboarding/distribution.md)
- [固定 URL worker host](docs/onboarding/orchestrator-host.md)
- [新規 project から起動までの manual](docs/onboarding/new-project-to-startup-manual.md)
- [意思決定サマリ](docs/00_decision_summary.md)
- [要件定義](docs/02_requirements_definition.md)
- [破壊的変更対策](docs/04_breaking_change_controls.md)
