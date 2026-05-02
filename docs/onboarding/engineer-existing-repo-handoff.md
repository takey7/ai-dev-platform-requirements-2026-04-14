# Engineer Handoff: Existing Repo Setup

この文書は、別環境で **すでに対象 repo の直下にいるエンジニア** に渡すための指示です。

重要: `bootstrap` だけでは GitHub private repo や Jira project は作りません。既存 repo 直下から GitHub / Jira 作成まで行う場合は `setup-repo` を使います。新規 local repo も含めてゼロから作る場合は `create-project` を使います。

## 依頼文

以下をそのままエンジニアに渡してください。

```markdown
# 依頼: 今いる repo に標準 AI 開発体制を導入してください

## 前提
あなたの作業対象は「今いる repo」です。

platform source repo は外部から取得して使うだけで、編集対象ではありません。

## ゴール
この repo に、以下を導入してください。

- GitHub private repo
- Jira Kanban project/space
- platform baseline
- Claude Code 設定
- Codex 実装・review 導線
- polling-first orchestrator 登録
- repo 固定 Jira scope guardrail

`1 repo = 1 Jira project/space` を守ってください。

## 1. 必須ログイン

```bash
gh auth login
claude auth login --claudeai
codex login
```

Claude / Codex は通常運用では API key ではなく login を使います。

## 2. Jira provisioning token

Jira project を新規作成する場合だけ、管理者 API token が必要です。
repo には保存しないでください。

macOS では Keychain に保存します。

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

Linux / 一時環境では shell env でも構いません。

```bash
export ATLASSIAN_API_TOKEN="<jira-admin-api-token>"
```

## 3. platform source repo を取得

対象 repo の外に clone します。

```bash
mkdir -p ~/workspaces
cd ~/workspaces

git clone https://github.com/takey7/ai-dev-platform-requirements-2026-04-14.git
cd ai-dev-platform-requirements-2026-04-14
```

## 4. 初期設定

```bash
./bin/platform configure \
  --github-owner <GITHUB_OWNER> \
  --projects-root ~/workspaces \
  --source-repo takey7/ai-dev-platform-requirements-2026-04-14 \
  --source-ref v0.2.0 \
  --adapter node-ts \
  --launch-mode none \
  --jira-site-url https://<JIRA_SITE>.atlassian.net \
  --jira-admin-email <ATLASSIAN_ADMIN_EMAIL>

./bin/platform orchestrator configure \
  --project-root ~/workspaces \
  --jira-site-url https://<JIRA_SITE>.atlassian.net \
  --jira-admin-email <ATLASSIAN_ADMIN_EMAIL> \
  --codex-model "" \
  --codex-binary auto \
  --codex-ignore-user-config \
  --claude-model default \
  --claude-effort ""

./bin/platform toolchain doctor
```

`--codex-model ""` は Codex CLI の組み込み最新版デフォルト追従です。worker は `~/.config/ai-dev-platform/toolchain.json` の互換済み Codex binary を使い、既定で `~/.codex/config.toml` を読まないため、個人の PATH や model 設定に引きずられません。

## 5. 今いる repo を一括セットアップ

対象 repo の絶対パスを指定して実行します。

```bash
cd ~/workspaces/ai-dev-platform-requirements-2026-04-14

./bin/platform setup-repo \
  --target /absolute/path/to/target-repo \
  --github-owner <GITHUB_OWNER> \
  --repo-name <kebab-case-repo-name> \
  --project-name "<Project Display Name>" \
  --jira-key <JIRA_KEY> \
  --jira-name "<Project Display Name>" \
  --confluence-space <JIRA_KEY> \
  --adapter node-ts \
  --launch-mode none
```

この 1 コマンドで以下を実行します。

- 対象 repo が git repo でなければ `git init`
- origin が無ければ GitHub private repo を作成して origin を追加
- Jira Software Kanban project/space を作成
- `.platform/platform.yaml` を作成
- `AGENTS.md` を作成
- `.claude/settings.json` を作成
- `.mcp.json` を作成
- GitHub workflow wrapper を作成
- `platform doctor` を実行
- setup commit を作成して push
- polling-first orchestrator に登録

既存 origin がある場合は、その origin を使います。別 repo を指している場合は停止します。

既存 commit がある repo で未コミット変更がある場合は、安全のため停止します。先に commit/stash するか、意図して含める場合だけ `--allow-dirty` を付けてください。

## 6. 検証

```bash
./bin/platform doctor --target /absolute/path/to/target-repo
./bin/platform orchestrator status --project <JIRA_KEY>
./bin/platform codex-review --target /absolute/path/to/target-repo
```

## 7. GitHub / Codex review 設定

Codex review の automatic review は CLI から強制有効化できません。

```bash
./bin/platform codex-review \
  --target /absolute/path/to/target-repo \
  --open-settings
```

ブラウザで以下を確認してください。

- GitHub Connector が有効
- 対象 repo が connector access に含まれる
- Code review settings で対象 repo の automatic review が有効

## 7.5. 登録チェックリスト

この導入で登録・確認すべきものは次です。

- local login: `gh`, `claude`, `codex`
- platform user config: GitHub owner, projects root, Jira site URL, Jira admin email
- orchestrator config: projects root, Jira site URL/email, Codex/Claude model policy
- GitHub: private repo, origin remote, Actions permission, Codex connector repo access, automatic Codex review
- Jira: Kanban project/space, project key, control issue, `ai:auto` 運用
- Claude: Atlassian MCP OAuth consent
- repo files: `.platform/platform.yaml`, `AGENTS.md`, `.claude/settings.json`, `.mcp.json`, workflow wrappers
- secrets: Jira admin API token is local-only; no token in repo

## 8. 実装開始

Jira で対象 project に issue を作り、label に `ai:auto` を付けます。

その後、platform source repo 側で worker を起動します。

```bash
cd ~/workspaces/ai-dev-platform-requirements-2026-04-14
./bin/platform orchestrator run --poll-only
```

これで Claude が Jira issue を読み、Codex が実装し、GitHub PR 作成まで進みます。
実装前には Codex の理解内容を Claude coordinator が承認する mediated baton を通します。
既定では PR が `ready_for_merge` になった後、GitHub auto-merge / merge queue を有効化します。ローカル worker が直接 merge commit を作る運用は標準ではありません。

複数 issue をまとめて流す場合:

```bash
./bin/platform orchestrator batch create \
  --project <JIRA_KEY> \
  --jql 'project = <JIRA_KEY> AND labels = "ai:auto" AND status in ("To Do", "Selected for Development")' \
  --max-parallel 3
```

## 完了条件

- 対象 repo に `.platform/platform.yaml` がある
- 対象 repo に `AGENTS.md`, `.claude/settings.json`, `.mcp.json` がある
- GitHub private repo に push 済み
- Jira Kanban project/space が作成済み
- `platform doctor` が重大エラーなし
- orchestrator 登録済み
- Codex review 設定状況を確認済み
- API token / secret が repo に保存されていない
```

## 直接 curl で実行する場合

platform source repo を手動 clone せず、対象 repo 直下から実行したい場合は次を使えます。

```bash
curl -fsSL https://raw.githubusercontent.com/takey7/ai-dev-platform-requirements-2026-04-14/main/install.sh | bash -s -- \
  --target "$PWD" \
  --github-owner <GITHUB_OWNER> \
  --repo-name <kebab-case-repo-name> \
  --project-name "<Project Display Name>" \
  --jira-key <JIRA_KEY> \
  --jira-name "<Project Display Name>" \
  --confluence-space <JIRA_KEY> \
  --adapter node-ts \
  --launch-mode none
```

この script は platform source repo を `~/.cache/ai-dev-platform/source` に clone / update してから `platform setup-repo` を呼びます。

## 使い分け

- 既存 repo に導入する: `setup-repo`
- 完全な新規 repo を local から作る: `create-project`
- GitHub / Jira を作らず基盤ファイルだけ入れる: `bootstrap`
