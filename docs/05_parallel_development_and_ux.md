# 05. 複数並行開発と人にわかりやすい UX

## 1. 結論
複数の別開発は**十分に可能**。  
ただし、成立条件は **1 ticket = 1 worktree = 1 branch = 1 PR = 1 spec** を厳守すること。

## 2. 標準単位
各案件ごとに以下を 1 セットとする。

- Jira issue: `PROJ-123`
- spec: `docs/specs/PROJ-123.md`
- branch: `feat/PROJ-123-short-slug`
- worktree: `../worktrees/PROJ-123-short-slug`
- Claude session: `PROJ-123`
- Codex thread: `PROJ-123`
- PR: `PROJ-123: short title`

## 3. 人間から見た「正本」
| 見たいこと | 見る場所 |
|---|---|
| なぜやるか | Jira issue / Confluence |
| 何をやるか | spec |
| 何が変わるか | PR diff |
| 今どこまで進んだか | PR checks / merge queue |
| いつ出るか | Release |
| どこに出たか | Deployments / Environments |
| 問題が起きたら何を戻すか | rollback_ref / release / deployment records |

## 4. UX の原則
### 4.1 ローカル UI は作業面、GitHub と Jira は共有面
- Claude / Codex / tmux / IDE は作業者向け
- GitHub PR / Actions / Releases / Deployments はチーム共有向け
- Jira はビジネス文脈の共有向け

### 4.2 1件ごとの可視化を揃える
各 PR に最低限以下を載せる。

- issue key
- spec link
- risk class
- breaking change 有無
- rollout strategy
- rollback summary

### 4.3 検索可能な命名規則
- branch 先頭に issue key を入れる
- PR title 先頭に issue key を入れる
- release note に issue key を含める
- deployment metadata に pr_number / sha / issue_key を残す

## 5. Claude と Codex の使い分け
### Claude
- issue を読み、spec を作る
- 実装し、ローカルで直す
- PR を組み立てる

### Codex
- 通常レビュー
- adversarial review
- 長時間調査
- GitHub 上での P0/P1 最終クロスチェック
- Cloud 上でのバックグラウンド task

## 6. worktree と subagent の使い分け
### worktree を使う場面
- 別 issue を並行進行したい
- diff を独立させたい
- 自動化が未完了のローカル作業に干渉してほしくない

### subagent を使う場面
- 同一 issue 内で探索を分業したい
- 実装ではなく分析タスクを並列化したい

**別案件の単位は subagent ではなく worktree に切る。**

## 7. 推奨 UX パターン
### 7.1 IDE / Desktop
- Claude Code: 複数会話タブ / 複数ウィンドウ
- Codex app: thread pin / worktree / notifications
- tmux は optional

### 7.2 GitHub
- PR 一覧に label と status を揃える
- queue 状態が見える
- environment ごとの deploy 履歴が見える

### 7.3 Atlassian
- issue に PR link を自動反映
- 必要なら release note / deploy note を issue に返す

## 8. 進捗状態の標準語彙
各案件の状態を以下のどれかに正規化する。

- `drafting-spec`
- `implementing`
- `local-review`
- `pr-open`
- `waiting-checks`
- `queued-for-merge`
- `merged`
- `releasing`
- `deployed-staging`
- `deployed-production`
- `rollback`
- `blocked`

## 9. チーム運用のコツ
- 1 PR に複数 issue を混ぜない
- 1 worktree で複数 feature を進めない
- main へ直接 push しない
- PR を source of truth にして Slack/tmux を source of truth にしない

## 10. optional な拡張
- tmux オーバーレイで NOC 風に監視
- Jira dashboard / GitHub project で status 可視化
- Codex automations / scheduled tasks で日次 review
