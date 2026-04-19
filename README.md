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

### 2. 空 repo に標準基盤を導入する
```bash
./bin/platform bootstrap \
  --target /path/to/new-repo \
  --adapter node-ts \
  --issue-project-key PROJ \
  --confluence-space SPACE \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --version main
```

### 3. 既存 repo を検査する
```bash
./bin/platform doctor --target /path/to/existing-repo
```

### 4. issue spec を作る
```bash
./bin/platform new-spec PROJ-123 \
  --target /path/to/repo \
  --title "stabilize release ready gate"
```

### 5. consuming repo を更新する
```bash
./bin/platform upgrade \
  --target /path/to/repo \
  --to v0.1.1
```

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
- Codex review は GitHub 側で automatic reviews を有効化するか、PR 上で `@codex` を使って呼ぶ
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
- plugin は `platform bootstrap / doctor / new-spec / upgrade` を呼ぶ補助導線のみを提供する

## Atlassian / MCP
- project-scoped `.mcp.json` を consuming repo に配置する
- 標準 endpoint は `/v1/mcp`
- 認証は OAuth 2.1 前提
- API token は opt-in の補助用途のみ
- 各 consuming repo は 1 つの Jira project key を `issue.project_key` に固定する
- AI はその Jira project key と既定の Confluence space だけを扱い、他 project / space は明示的なユーザー指示がない限り対象外にする

## Jira template recommendation
- この標準開発フローの既定は `カンバン` が最適
- 理由: repo 単位の継続 delivery と相性がよく、sprint ceremony を前提にしないため導入コストが低い
- `スクラム` は、すでに固定長 sprint を運用しているチームだけに限定して選ぶ
- `Jira Product Discovery` のテンプレートは delivery 管理ではなく discovery / prioritization 用なので、repo ごとの開発 space の既定にはしない

## 今回の source repo で追加した主な実装
- `scripts/platform.py`
  bootstrap / doctor / upgrade / new-spec
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
- [GitHub / 配布方針](docs/onboarding/distribution.md)
- [意思決定サマリ](docs/00_decision_summary.md)
- [要件定義](docs/02_requirements_definition.md)
- [破壊的変更対策](docs/04_breaking_change_controls.md)
