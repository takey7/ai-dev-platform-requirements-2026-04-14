# Copy-Paste New Development Manual

この手順は、ユーザーが新しい開発を **GitHub private repo 作成、Jira Kanban project 作成、platform bootstrap、orchestrator 登録、Jira issue 起点の実装、PR 確認** まで進めるためのコピペ用マニュアルです。

既定は polling-first です。ローカル Mac から Jira / GitHub へ outbound 接続するだけなので、固定 HTTPS URL は不要です。

## 0. 使う場所

- platform source repo 直下で実行する:
  - `platform configure`
  - `platform create-project`
  - `platform setup-repo`
  - `platform orchestrator register`
  - `platform orchestrator run`
  - `platform orchestrator status / poll`
- consumer repo 直下で実行する:
  - Claude / Codex への実装指示
  - `platform doctor --target .`
  - 通常の git / pnpm / gh 操作

まず platform source repo へ移動します。

```bash
cd /Users/jin/Downloads/ai-dev-platform-requirements-2026-04-14
```

使い分け:

- 新規 local repo も含めてゼロから作る: `create-project`
- すでに対象 repo の直下にいるエンジニアへ渡す: `setup-repo`
- GitHub / Jira を作らず基盤ファイルだけ入れる: `bootstrap`

## 1. プロジェクト値を決める

このブロックだけ、毎回プロジェクトごとに変更します。

```bash
export PLATFORM_SOURCE="/Users/jin/Downloads/ai-dev-platform-requirements-2026-04-14"
export PROJECTS_ROOT="$HOME/workspaces"

export GITHUB_OWNER="takey7"
export PLATFORM_SOURCE_REPO="takey7/ai-dev-platform-requirements-2026-04-14"
export PLATFORM_VERSION="v0.2.0"

export JIRA_SITE_URL="https://ssbot.atlassian.net"
export JIRA_ADMIN_EMAIL="YOUR_ATLASSIAN_ADMIN_EMAIL@example.com"

export PROJECT_TITLE="My New Service"
export REPO_NAME="my-new-service"
export JIRA_KEY="MNS"
export CONFLUENCE_SPACE="MNS"

export CONSUMER_REPO="$PROJECTS_ROOT/$REPO_NAME"
```

命名ルール:

- `REPO_NAME`: kebab-case
- `JIRA_KEY`: 2から10文字程度の大文字英数字。一意にする
- `CONFLUENCE_SPACE`: 通常は `JIRA_KEY` と同じ
- 1 repo = 1 Jira project/space に固定する

## 2. 必須ログインを確認する

```bash
cd "$PLATFORM_SOURCE"

for tool in git gh python3 node pnpm claude codex tmux; do
  command -v "$tool" >/dev/null && echo "ok: $tool" || echo "missing: $tool"
done

gh auth status
claude auth status
codex --version
```

未ログインなら実行します。

```bash
gh auth login
claude auth login --claudeai
codex login
```

標準は login-based auth です。`OPENAI_API_KEY` を通常開発の標準経路にしません。

## 3. 初回だけ user config を保存する

```bash
cd "$PLATFORM_SOURCE"

./bin/platform configure \
  --github-owner "$GITHUB_OWNER" \
  --projects-root "$PROJECTS_ROOT" \
  --source-repo "$PLATFORM_SOURCE_REPO" \
  --source-ref "$PLATFORM_VERSION" \
  --adapter node-ts \
  --launch-mode none \
  --jira-site-url "$JIRA_SITE_URL" \
  --jira-admin-email "$JIRA_ADMIN_EMAIL"
```

Jira project 作成に使う admin API token は macOS Keychain に保存します。repo には保存しません。

```bash
read -rsp "Jira admin API token: " ATLASSIAN_API_TOKEN
echo

security add-generic-password \
  -a "$USER" \
  -s ai-dev-platform.atlassian-api-token \
  -w "$ATLASSIAN_API_TOKEN" \
  -U

unset ATLASSIAN_API_TOKEN
```

## 4. GitHub private repo と Jira Kanban project を作る

```bash
cd "$PLATFORM_SOURCE"

./bin/platform create-project "$PROJECT_TITLE" \
  --github-owner "$GITHUB_OWNER" \
  --root "$PROJECTS_ROOT" \
  --repo-name "$REPO_NAME" \
  --jira-key "$JIRA_KEY" \
  --jira-name "$PROJECT_TITLE" \
  --confluence-space "$CONFLUENCE_SPACE" \
  --source-repo "$PLATFORM_SOURCE_REPO" \
  --version "$PLATFORM_VERSION" \
  --adapter node-ts \
  --launch-mode none
```

作成結果を確認します。

```bash
gh repo view "$GITHUB_OWNER/$REPO_NAME"
test -f "$CONSUMER_REPO/.platform/platform.yaml" && echo "ok: manifest exists"
./bin/platform doctor --target "$CONSUMER_REPO"
```

## 5. Codex GitHub review を有効化する

CLI から GitHub/Codex 側の repo toggle は強制変更できません。設定画面を開き、対象 repo を有効化します。

```bash
cd "$PLATFORM_SOURCE"
./bin/platform codex-review --target "$CONSUMER_REPO" --open-settings
```

ブラウザで確認すること:

- GitHub Connector が `takey7` に接続済み
- GitHub App repository access に `$GITHUB_OWNER/$REPO_NAME` が含まれる
- Code review settings の対象 repo で `自動コードレビュー` を有効化
- 推奨は `すべての PR をレビューする`

設定後に CLI で確認します。

```bash
./bin/platform codex-review --target "$CONSUMER_REPO"
```

まだ PR が無い段階では review artifact は見つからなくても正常です。

## 6. Orchestrator に repo を登録する

```bash
cd "$PLATFORM_SOURCE"

./bin/platform orchestrator register \
  --target "$CONSUMER_REPO"

./bin/platform orchestrator configure \
  --codex-model "" \
  --codex-binary auto \
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""

./bin/platform toolchain doctor
./bin/platform doctor --target "$CONSUMER_REPO"
```

polling mode では Jira Automation webhook rule と固定 HTTPS URL は不要です。

Codex は空設定で CLI の組み込み current default に追従します。worker は `~/.config/ai-dev-platform/toolchain.json` の互換済み binary を使うため、tmux / shell / LaunchAgent の PATH 差分や古い `/opt/homebrew/bin/codex` に引きずられません。

Codex binary を明示固定したい場合は `./bin/platform toolchain pin-codex --binary <path>` を使います。専用 worker の OS ユーザー設定をあえて継承したい場合だけ `--codex-use-user-config` を使います。

Claude を常に最上位モデルへ寄せたい専用 worker では、`--claude-model best --claude-effort xhigh` に変更します。

## 7. Worker を起動する

別 terminal を開く代わりに tmux で常駐させます。

```bash
cd "$PLATFORM_SOURCE"

tmux has-session -t "worker-$REPO_NAME" 2>/dev/null || \
  tmux new-session -d -s "worker-$REPO_NAME" \
  "cd '$PLATFORM_SOURCE' && ./bin/platform orchestrator run --poll-only"

tmux list-sessions | grep "worker-$REPO_NAME"
```

ログを見る場合:

```bash
tmux attach -t "worker-$REPO_NAME"
```

tmux から抜けるだけなら `Ctrl-b` の後に `d` を押します。

## 8. Jira issue を Claude から作る

consumer repo 直下で実行します。Claude は repo の `.platform/platform.yaml` を正本として、作成先 Jira project を固定します。

```bash
cd "$CONSUMER_REPO"

claude -p --permission-mode bypassPermissions "
この repo の manifest にある issue.project_key だけを対象に、Jira Task を 1 件作成してください。
別 project は参照しないでください。
summary: Add health endpoint
description:
- Add a simple health endpoint for the service.
- Include tests.
- Keep the change small.
labels:
- ai:auto
status:
- To Do にできる場合は To Do にしてください。
返答は issue key と URL だけにしてください。
"
```

返ってきた issue key を保存します。例では `MNS-1` とします。

```bash
export ISSUE_KEY="MNS-1"
```

既に Jira issue がある場合は、その issue に `ai:auto` label を付け、status を `To Do` または `Selected for Development` にします。

## 9. 実装を自動で進める

worker が polling で issue を拾うと、自動で次を進めます。

- Jira / Confluence 読み取り
- `docs/specs/<ISSUE>.md` 作成
- branch / worktree 作成
- Claude planning
- Codex understanding
- Claude coordinator approval
- Codex coding
- Codex local review
- PR 作成
- GitHub checks 待ち
- Codex review artifact 待ち
- ready 後は GitHub auto-merge / merge queue を有効化
- Jira sticky comment 更新
- 作業開始時に Jira status を `In Progress` / `進行中` / `作業中` へ移動
- PR merge 後だけ Jira status を `Done` / `完了` へ移動

状態を確認します。

```bash
cd "$PLATFORM_SOURCE"

./bin/platform orchestrator reconcile --project "$JIRA_KEY"
./bin/platform orchestrator status --issue "$ISSUE_KEY"
```

継続監視する場合:

```bash
cd "$PLATFORM_SOURCE"

while true; do
  date
  ./bin/platform orchestrator status --issue "$ISSUE_KEY"
  sleep 30
done
```

止めるときは `Ctrl-C` です。

## 10. PR と GitHub checks を確認する

```bash
export PR_NUMBER="$(gh pr list -R "$GITHUB_OWNER/$REPO_NAME" --state open --json number -q '.[0].number')"
echo "PR_NUMBER=$PR_NUMBER"

gh pr view "$PR_NUMBER" -R "$GITHUB_OWNER/$REPO_NAME" --web
gh pr checks "$PR_NUMBER" -R "$GITHUB_OWNER/$REPO_NAME"
gh pr view "$PR_NUMBER" -R "$GITHUB_OWNER/$REPO_NAME" \
  --json number,title,url,headRefName,reviews,comments,statusCheckRollup,mergeable
```

Codex review の platform 判定を確認します。

```bash
cd "$PLATFORM_SOURCE"
./bin/platform codex-review --target "$CONSUMER_REPO"
```

期待値:

- required checks が success
- `chatgpt-codex-connector` の review、または `Codex Review:` artifact がある
- Jira sticky comment に state / branch / PR URL / checks / review が書き戻される
- Jira は `ready_for_merge` では Done にならず、PR merge 後だけ Done になる
- 既定では worker が `ready_for_merge` 後に GitHub auto-merge / merge queue を有効化する

merge は GitHub 側の branch protection / merge queue / auto-merge 設定を最終統制面にします。ローカル worker が直接 merge commit を作る運用は標準ではありません。

## 11. 自動実装が止まったとき

worker 停止中や stale 状態が疑われる場合:

```bash
cd "$PLATFORM_SOURCE"

./bin/platform orchestrator poll --issue "$ISSUE_KEY"
./bin/platform orchestrator status --issue "$ISSUE_KEY" --refresh
```

一時停止、再開、キャンセル:

```bash
./bin/platform orchestrator pause --issue "$ISSUE_KEY"
./bin/platform orchestrator resume --issue "$ISSUE_KEY"
./bin/platform orchestrator cancel --issue "$ISSUE_KEY"
./bin/platform orchestrator fail --issue "$ISSUE_KEY" --backlog --reason "manual failover"
```

Jira comment からも操作できます。

```text
/ai status
/ai pause
/ai resume
/ai retry
/ai cancel
```

## 12. 手動で追加実装を指示する場合

自動 worker ではなく、ユーザーが直接 Claude / Codex に指示する場合は consumer repo 直下で行います。

```bash
cd "$CONSUMER_REPO"

./bin/platform doctor --target . 2>/dev/null || "$PLATFORM_SOURCE/bin/platform" doctor --target .
```

Claude への指示例:

```bash
claude -p "
この repo の AGENTS.md と .platform/platform.yaml を守ってください。
Jira project は manifest の issue.project_key のみ対象です。
$ISSUE_KEY の spec を確認し、実装方針を短く出してから変更してください。
"
```

Codex への指示例:

```bash
codex exec "
Implement the scoped change for $ISSUE_KEY in this repository.
Respect AGENTS.md, .platform/platform.yaml, and docs/specs/$ISSUE_KEY.md.
Run the relevant validation commands before finishing.
"
```

## 13. 次の repo を作るとき

同じ手順を使い、次の値だけ変えます。

```bash
export PROJECT_TITLE="Another Service"
export REPO_NAME="another-service"
export JIRA_KEY="ANS"
export CONFLUENCE_SPACE="ANS"
export CONSUMER_REPO="$PROJECTS_ROOT/$REPO_NAME"
```

その後、手順 4 から繰り返します。複数 repo は `projects_roots[]` 配下で共存できますが、`JIRA_KEY` は repo ごとに必ず一意にします。

## 13.5. 複数 issue をまとめて並列実行する場合

同じ repo / Jira project 内で複数 issue を短期間に進める場合は batch を使います。

```bash
cd "$PLATFORM_SOURCE"

./bin/platform orchestrator batch create \
  --project "$JIRA_KEY" \
  --jql "project = $JIRA_KEY AND labels = \"ai:auto\" AND status in (\"To Do\", \"Selected for Development\")" \
  --max-parallel 3

./bin/platform orchestrator batch status
```

Claude coordinator が dependency / conflict group / task contract を作ります。同じ conflict group や依存関係がある issue は同時に lease されず、独立 issue だけが並列で Codex worker に渡ります。

停止・再開:

```bash
export BATCH_ID="<batch_id>"

./bin/platform orchestrator batch pause --batch "$BATCH_ID"
./bin/platform orchestrator batch resume --batch "$BATCH_ID"
./bin/platform orchestrator batch cancel --batch "$BATCH_ID"
```

## 14. 最終チェック

```bash
cd "$PLATFORM_SOURCE"

./bin/platform doctor --target "$CONSUMER_REPO"
./bin/platform codex-review --target "$CONSUMER_REPO"
./bin/platform orchestrator status --project "$JIRA_KEY"

cd "$CONSUMER_REPO"
git status --short
gh pr list -R "$GITHUB_OWNER/$REPO_NAME" --state open
```

成功条件:

- GitHub private repo が存在する
- Jira Kanban project が存在する
- `.platform/platform.yaml` の `issue.project_key` が `$JIRA_KEY`
- Worker が issue を拾っている
- PR が作られている
- GitHub checks と Codex review が確認できる
- Jira sticky comment に進捗が戻っている
