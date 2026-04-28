# New Project To Startup Manual

この手順は、**新しい repo を作るところから resident orchestrator を起動して Jira issue を流し始めるところまで**を対象にした運用マニュアルです。

## 0. 前提
- source repo: `/Users/jin/Downloads/ai-dev-platform-requirements-2026-04-14`
- formal entrypoint: `./bin/platform`
- Jira 既定テンプレート: `Kanban`
- Claude / Codex は login-based auth
- Jira project 作成と control issue 作成だけ Jira admin token を使う

## 1. ローカル認証
```bash
gh auth status
claude auth status
codex login status
```

未ログインなら:
```bash
gh auth login
claude auth login --claudeai
codex login
```

## 2. user config を 1 回だけ保存する
```bash
cd /Users/jin/Downloads/ai-dev-platform-requirements-2026-04-14

./bin/platform configure \
  --github-owner <github-owner> \
  --projects-root ~/workspaces \
  --jira-site-url https://<site>.atlassian.net \
  --jira-admin-email <jira-admin-email> \
  --launch-mode tmux
```

ローカル macOS では token を Keychain に保存します:
```bash
security add-generic-password \
  -a "$USER" \
  -s ai-dev-platform.atlassian-api-token \
  -w "<jira-admin-token>" \
  -U
```

Linux host や一時実行では環境変数も使えます:
```bash
export ATLASSIAN_API_TOKEN=<jira-admin-token>
```

## 3. 新規 repo / Jira project を一気に作る
```bash
./bin/platform create-project "Billing API" \
  --repo-name billing-api \
  --jira-key BILL \
  --jira-name "Billing API" \
  --confluence-space BILL
```

このコマンドで行われること:
- GitHub private repo を作る
- local repo を clone する
- baseline を bootstrap する
- Node/TypeScript adapter を入れる
- `pnpm install` と `platform doctor` を通す
- 初回 commit / push を行う
- Jira Software Kanban project を作る
- `tmux` で `dev / claude / codex` を起動する

確認ポイント:
```bash
gh repo view <owner>/<repo>
tmux list-sessions
cat ~/workspaces/<repo-name>/.platform/platform.yaml
```

## 4. worker を用意する
既定は polling-first です。ローカル Mac から Jira REST / GitHub CLI へ outbound 接続するだけなので、public HTTPS URL は不要です。

常時稼働させたい場合だけ Linux VM + systemd を使います。詳細は [orchestrator-host.md](orchestrator-host.md) を参照してください。

worker config の確認:
```bash
cat ~/.config/ai-dev-platform/orchestrator.json
```

最低限必要な項目:
- `event_mode`: 既定は `polling`
- `bind_host`
- `bind_port`
- `projects_roots`
- `jira_site_url`
- `jira_admin_email`
- `ai.codex_model`: 既定は空文字。Codex CLI の組み込み current default に追従する
- `ai.codex_ignore_user_config`: 既定は `true`。worker は個人の `~/.codex/config.toml` を読まない
- `ai.claude_model`: 既定 `default`
- `ai.claude_effort`: 既定は空文字で Claude Code の既定に委ねる

明示的に最新モデル方針を保存する場合:
```bash
./bin/platform orchestrator configure \
  --codex-model "" \
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""
```

Codex を固定したい専用 worker では `--codex-model gpt-5.5 --codex-ignore-user-config` を使います。

専用 worker の OS ユーザー設定をあえて継承したい場合だけ `--codex-use-user-config` を使います。

Claude 側を最も強い利用可能モデルに寄せる専用 worker では:
```bash
./bin/platform orchestrator configure \
  --claude-model best \
  --claude-effort xhigh
```

## 5. consuming repo を worker に登録する
```bash
./bin/platform orchestrator register \
  --target ~/workspaces/<repo-name>
```

このコマンドで行われること:
- `projects_roots[]` に repo 親ディレクトリを追加
- Jira control issue を作成または再利用
- worker DB に repo と Jira project key を登録
- polling mode では Jira Automation rule は作らない
- 旧 webhook mode の rule ID が DB に残っている場合は、可能な範囲で Jira 側 rule を disabled にする

webhook mode が必要な場合だけ、明示的に opt-in します。

```bash
./bin/platform orchestrator register \
  --target ~/workspaces/<repo-name> \
  --webhook \
  --public-base-url https://orchestrator.<domain>
```

複数 project を同じ worker に載せる場合も同じです。
```bash
./bin/platform orchestrator register --target ~/workspaces/repo-a
./bin/platform orchestrator register --target ~/workspaces/repo-b
```

## 6. worker を起動する
```bash
./bin/platform orchestrator run --poll-only
```

別ターミナルで確認:
```bash
./bin/platform orchestrator status --project <PROJECT_KEY>
```

## 7. Jira issue を作る
通常運用では **Claude + MCP** を使います。CLI 追加コマンドは使いません。

ポリシー:
- Jira issue 作成は **明示指示のときだけ**
- 作成先は repo 固定 `issue.project_key`
- 既定 issue type は `Task`

例:
```bash
cd ~/workspaces/<repo-name>

claude -p --permission-mode bypassPermissions \
  "In this repository, create a Jira Task in the repo's fixed Jira project. Title: 'Add queue health note'. Return only the created issue key and title."
```

## 8. orchestrator に着手させる
対象 issue に `ai:auto` を付けます。status は `To Do` または `Selected for Development` を使います。

その後 worker は:
1. Jira issue を読む
2. `docs/specs/<ISSUE>.md` を生成する
3. worktree と branch を作る
4. Claude planning -> Codex coding/review -> Claude integrate を回す
5. PR を作る
6. GitHub checks を待つ
7. Codex review artifact を待つ
8. `ready_for_merge` か `blocked` を Jira sticky comment に返す

## 9. 日常の監視と操作
status:
```bash
./bin/platform orchestrator status --project <PROJECT_KEY>
./bin/platform orchestrator status --issue <ISSUE_KEY>
```

CLI pause/resume/cancel:
```bash
./bin/platform orchestrator pause --issue <ISSUE_KEY>
./bin/platform orchestrator resume --issue <ISSUE_KEY>
./bin/platform orchestrator cancel --issue <ISSUE_KEY>
```

Jira comment control:
- `/ai pause`
- `/ai resume`
- `/ai cancel`
- `/ai retry`
- `/ai status`

project-wide:
- `/ai pause-project`
- `/ai resume-project`
- `/ai drain-project`

## 10. GitHub review の確認
repo 側では automatic Codex review を有効化します。

worker の動作:
1. PR 作成
2. required checks 待ち
3. Codex review artifact 待ち
4. 来なければ `@codex review` fallback
5. それでも来なければ Jira に `blocked` を返す

確認コマンド:
```bash
gh pr list -R <owner>/<repo> --state all --json number,title,reviews,reviewDecision,statusCheckRollup
./bin/platform doctor --target ~/workspaces/<repo-name>
```

worker が止まっていた場合は、GitHub 状態を手動で再取得します。

```bash
./bin/platform orchestrator poll --issue <ISSUE_KEY>
./bin/platform orchestrator status --issue <ISSUE_KEY>
```

`@codex review` の comment だけでは完了扱いにしません。GitHub 上の review、または `chatgpt-codex-connector` の `Codex Review:` comment が無い場合は、一定時間後に Jira へ `blocked` として書き戻します。

## 11. 複数 project で混ざらないことの確認
以下を project ごとに確認します。
- `.platform/platform.yaml` の `issue.project_key` が repo ごとに一意
- branch 名がその issue key を含む
- worktree path が `.../worktrees/<PROJECT_KEY>/.../<ISSUE_KEY>` になっている
- sticky comment の branch / PR URL がその repo だけを指す
- 他 repo の PR や Jira issue key が混ざらない

## 12. 失敗時の切り分け
- Jira issue が拾われない:
  - issue に `ai:auto` label があるか確認
  - status が `To Do` または `Selected for Development` か確認
  - `platform orchestrator reconcile --project <PROJECT_KEY>` を実行
- Jira issue は読めるが PR が出ない:
  - `platform orchestrator status`
  - worktree の `git status`
  - `gh pr list`
- review が返らない:
  - repo 側の automatic Codex review 設定
  - fallback `@codex review` comment の有無

## 13. 実運用の既定
- 1 repo = 1 Jira project/space
- Jira は Kanban
- Claude の Jira issue 作成は explicit-only
- Codex review は automatic review を正道、`@codex review` は fallback
- Worker は `ready_for_merge` で止まり、merge は人または GitHub rules に委ねる
