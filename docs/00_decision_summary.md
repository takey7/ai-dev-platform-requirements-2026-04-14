# 00. 意思決定サマリ

## 採用する中核構成
**Atlassian Rovo MCP + Claude Code plugin/hooks + OpenAI `codex-plugin-cc` + GitHub/Codex Cloud** を標準構成とする。

## 主要判断
### 1. Atlassian は「文脈取得面」
- Jira / Confluence / Compass を Rovo MCP 経由で Claude に渡す
- 実装前に `docs/specs/<ISSUE>.md` へ凍結し、そこから先の主文脈は repo 内ファイルに寄せる
- Codex Cloud に Jira/Confluence の live な文脈を直接依存させない

### 2. Claude Code は「ローカル制御面」
- 仕様化、実装、ローカル検証、作業オーケストレーションの司令塔
- hooks を使って `lint / typecheck / test / 危険コマンド抑止 / 必須レビュー有無確認` を決定的に enforce する
- plugin は **project scope** で配布し、`.claude/settings.json` でチーム共有する

### 3. Codex は「セカンドオピニオン + 長時間ワーク面」
- ローカルでは `codex-plugin-cc` で `/codex:review`・`/codex:adversarial-review`・`/codex:rescue`
- GitHub 上では Codex review を最終クロスチェックとして活用
- 長時間タスク・バックグラウンドタスク・Cloud 環境実行は Codex Cloud へ寄せる

### 4. GitHub は「最終統制面」
- required status checks / rulesets / merge queue / release workflow / environments を承認の実体にする
- 通常系では **人間の Approve を必須にしない**
- 最終的な `merge / release / deploy` は **GitHub App または GitHub Actions** のみが実行する

### 5. tmux は主制御面にしない
- tmux は optional な観測 UI / 補助 UI に留める
- 正式な状態遷移は Jira / PR / Actions / Releases / Deployments に残す
- 人間から見た真実のソースは GitHub と Atlassian に寄せる

## 採用しない・MVP から外すもの
### Claude channels / agent teams
- 面白いが、MVP の必須には入れない
- 将来の可視化強化やマルチエージェント拡張として扱う

### GitHub ruleset の `exempt` bypass
- 通常運用では採用しない
- 標準経路で silent bypass が起きる設計は監査上よくない

### Atlassian API token を通常系の既定にする設計
- 日常の対話型利用は OAuth 2.1 を既定にする
- API token は service-style / non-interactive 用に限定する

## 重要な設計原則
1. **1チケット = 1 worktree = 1 branch = 1 PR**
2. **GitHub が最終状態遷移の唯一の正本**
3. **breaking change 対策は GitHub “だけ” ではなく、多層防御**
4. **監査証跡は GitHub / Atlassian / 外部ログ基盤に二重化**
5. **通常系では bypass なし、緊急系だけ明示的 break-glass**
