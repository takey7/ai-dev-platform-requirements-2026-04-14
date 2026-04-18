# 07. 導入ロードマップ

## Phase 0: 設計とガバナンス
### 目的
- 役割分担、権限、標準フロー、緊急フローを決める

### 成果物
- 本パックの承認
- GitHub App 設計
- required checks 名の確定
- protected path / risk class の確定
- spec テンプレート確定
- 外部ログ送り先の決定

### Exit criteria
- 標準経路と break-glass 経路が文書化されている
- org admin / platform team / repo owner の責任境界が明確

## Phase 1: Pilot（ローカル + PR まで）
### 目的
- Atlassian Rovo MCP + Claude hooks + Codex review + PR 作成まで固める

### 実施項目
- Rovo MCP 接続
- `.claude/settings.json` 導入
- `codex-plugin-cc` 導入
- `AGENTS.md` 導入
- spec 運用開始
- 1〜2 repo で pilot

### Exit criteria
- issue → spec → implementation → PR の流れが定着
- 1 ticket = 1 worktree = 1 PR が守られる

## Phase 2: 機械承認（merge まで）
### 目的
- 人手 Approve なしで PR を queue に入れられるようにする

### 実施項目
- rulesets / merge queue
- CI / spec gate / risk gate / security gate
- `codex-action` による AI gate
- auto-merge

### Exit criteria
- 標準変更が required checks のみで merge される
- merge queue で busy branch が安定運用できる

## Phase 3: 自動 release / staging deploy
### 目的
- merge 後に release と staging まで自動化する

### 実施項目
- release workflow
- artifact attestation
- deployment metadata
- staging smoke tests

### Exit criteria
- merge → release → staging が自動
- 失敗時の証跡が GitHub と外部ログに残る

## Phase 4: production 自動 deploy
### 目的
- 機械 gate だけで production まで流せるようにする

### 実施項目
- rollout policy
- canary / blue-green
- prod gate
- rollback artifact / rollback workflow
- SLO / error budget 連動

### Exit criteria
- 高リスク変更の gate が機械的に機能
- rollback が検証済み

## Phase 5: 並行開発の全社横展開
### 目的
- 複数チーム・複数 repo に展開する

### 実施項目
- reusable workflows
- org-level rulesets
- plugin marketplace / managed settings
- repo bootstrap テンプレート
- metrics / audit dashboard

## 推奨 KPI
- issue → PR までの lead time
- PR open → merge までの時間
- merge queue 待ち時間
- staging failure rate
- rollback 発生率
- high risk change の自動停止率
- bypass 使用回数
- Atlassian MCP call volume
