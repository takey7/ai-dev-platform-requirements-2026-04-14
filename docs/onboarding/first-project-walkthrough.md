# First Project Walkthrough

この手順は、初回ユーザーが **1 つの学習用プロジェクト** を通して、Jira 起点の自動開発フローを確認するための最短ルートです。

恒久運用の詳細は [new-project-to-startup-manual.md](new-project-to-startup-manual.md) と [orchestrator-host.md](orchestrator-host.md) を参照してください。

## 0. 使い分けるディレクトリ

- platform source repo:
  - `platform configure`
  - `platform create-project`
  - `platform orchestrator register`
  - `platform orchestrator run`
  - `platform orchestrator poll`
- consumer repo:
  - Claude / Codex に実装指示を出す
  - `platform doctor --target .`
  - issue 固有の確認や通常開発

この walkthrough では、まず platform source repo 直下から始めます。

```bash
cd /path/to/ai-dev-platform-source
```

## 1. 学習用の既定値

学習用 project は disposable private repo として作ります。

- project name: `Learning AI Flow`
- repo name: `learning-ai-flow-<YYYYMMDD>`
- Jira key: `LEARN`
- GitHub visibility: private
- Jira template: Kanban

`<YYYYMMDD>` は実行日で置き換えます。

## 2. ログイン確認

```bash
git --version
gh auth status
claude auth status
codex --version
python3 --version
node --version
pnpm --version
```

`gh`, `claude`, `codex` は API key ではなくログイン済みのアカウントを使います。

## 3. User config を作る

```bash
./bin/platform configure \
  --github-owner <GITHUB_OWNER> \
  --projects-root ~/workspaces \
  --source-repo <GITHUB_OWNER>/ai-dev-platform-requirements-2026-04-14 \
  --source-ref v0.1.0 \
  --adapter node-ts \
  --launch-mode none \
  --jira-site-url https://<SITE>.atlassian.net \
  --jira-admin-email <ATLASSIAN_ADMIN_EMAIL>
```

User config は初期値の seed です。repo 作成後の正本は consumer repo の `.platform/platform.yaml` です。

## 4. 学習用 project を作る

Jira project 作成だけは provisioning plane なので、ローカルでは macOS Keychain に保存した token を使います。環境変数 `ATLASSIAN_API_TOKEN` を一時 export しても動きます。

```bash
security add-generic-password \
  -a "$USER" \
  -s ai-dev-platform.atlassian-api-token \
  -w "<jira-admin-token>" \
  -U

./bin/platform create-project "Learning AI Flow" \
  --repo-name learning-ai-flow-<YYYYMMDD> \
  --jira-key LEARN \
  --jira-name "Learning AI Flow" \
  --confluence-space LEARN \
  --launch-mode none
```

作成後に確認します。

```bash
./bin/platform doctor --target ~/workspaces/learning-ai-flow-<YYYYMMDD>
```

## 5. Worker に登録する

```bash
./bin/platform orchestrator register \
  --target ~/workspaces/learning-ai-flow-<YYYYMMDD>
```

登録後に doctor で operational warning を確認します。

```bash
./bin/platform doctor --target ~/workspaces/learning-ai-flow-<YYYYMMDD>
```

## 6. Worker を起動する

固定 host では systemd が起動します。手元で確認する場合:

```bash
./bin/platform orchestrator run --poll-only
```

別 terminal で `platform orchestrator status --project <PROJECT_KEY>` を確認します。

## 7. Jira issue を作る

通常運用では Claude + Atlassian MCP/OAuth で作ります。consumer repo を開いて、明示的に依頼します。

```bash
cd ~/workspaces/learning-ai-flow-<YYYYMMDD>
claude
```

Claude への指示例:

```text
この repo の Jira project に Task を 1 件作ってください。
summary は "Add queue health endpoint"。
作成先は repo manifest の issue.project_key だけに限定してください。
```

作成された issue key を控えます。以下では `LEARN-1` とします。

## 8. 自動着手させる

Jira issue に `ai:auto` label を付け、status を `To Do` または `Selected for Development` にします。

worker が polling で issue/comment を拾うと、次を進めます。

- spec 生成
- branch / worktree 作成
- Claude planning
- Codex exec
- Codex local review
- PR 作成
- GitHub checks 待ち
- Codex review artifact 待ち
- Jira sticky comment 更新

## 9. 状態確認

worker が常駐している場合:

```bash
./bin/platform orchestrator status --issue LEARN-1
```

worker が止まっていた、または GitHub 状態が古そうな場合:

```bash
./bin/platform orchestrator poll --issue LEARN-1
./bin/platform orchestrator status --issue LEARN-1
```

または:

```bash
./bin/platform orchestrator status --issue LEARN-1 --refresh
```

## 10. Codex review の確認

GitHub checks が成功しても、real Codex review artifact が無ければ完了扱いにしません。

```bash
gh pr list --repo <GITHUB_OWNER>/learning-ai-flow-<YYYYMMDD> --state open
gh pr view <PR_NUMBER> --repo <GITHUB_OWNER>/learning-ai-flow-<YYYYMMDD> --json reviews,statusCheckRollup,comments
```

期待結果:

- required checks が success
- `reviews` に `codex` または `codex[bot]` の review が存在する
- review が来ない場合、worker は `@codex review` fallback を投稿する
- fallback 後も review が来なければ Jira に `blocked` として設定不足を書き戻す

Codex review は CLI から強制有効化できません。repo ごとに GitHub/Codex 側で automatic review を有効化してください。

## 11. 完了条件

学習用 project の成功条件は次です。

- GitHub private repo が作成されている
- Jira Kanban project が作成されている
- `.platform/platform.yaml` の `issue.project_key` が `LEARN` になっている
- Jira issue から PR が作られている
- GitHub required checks が動いている
- Jira sticky comment に state / branch / PR URL / checks / review が表示されている
- Codex review が来ない場合でも、理由が Jira に `blocked` として返る

## 12. よくある停止操作

```bash
./bin/platform orchestrator pause --issue LEARN-1
./bin/platform orchestrator resume --issue LEARN-1
./bin/platform orchestrator cancel --issue LEARN-1
```

Jira comment からも操作できます。

```text
/ai pause
/ai resume
/ai cancel
/ai status
```
