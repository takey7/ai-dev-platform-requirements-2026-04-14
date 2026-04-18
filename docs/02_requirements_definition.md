# 02. 要件定義書

## 1. 目的
本システムは、**自然言語のみ**で以下の流れを完了できることを目的とする。

`Jira/Confluence 取得 → 仕様化 → 実装 → ローカル検証 → Codex クロスレビュー → PR → 自動マージ → 自動リリース → 自動デプロイ → 証跡保管`

## 2. スコープ
### 対象
- Atlassian Cloud
- GitHub Cloud-hosted repository
- Claude Code
- Codex plugin / Codex Cloud / Codex GitHub integration
- GitHub Actions / rulesets / merge queue / environments

### 非対象
- GitHub 以外の SCM を主経路にすること
- Data Center / on-prem 前提の Atlassian 運用
- 人手承認を必須とした従来フロー
- tmux を正本 UI にすること

## 3. 機能要件（Functional Requirements）

| ID | 優先度 | 要件 | 受け入れ証跡 |
|---|---|---|---|
| FR-1 | MUST | Claude Code は Atlassian Rovo MCP から Jira / Confluence / Compass 文脈を取得できること | issue / page 参照ログ、spec への転記 |
| FR-2 | MUST | 実装前に `docs/specs/<ISSUE>.md` を生成し、目的・受け入れ条件・互換性影響・ロールバック方針を凍結すること | spec ファイル |
| FR-3 | MUST | 1チケットごとに worktree / branch / conversation / PR を分離できること | worktree / branch / PR の対応 |
| FR-4 | MUST | Claude hooks により、危険コマンド抑止・format・lint・typecheck・test・review 必須確認を決定的に enforce できること | hook 設定、Actions / local log |
| FR-5 | MUST | すべての変更で Codex 通常レビューを実行できること | `/codex:review` 結果 |
| FR-6 | MUST | 共有契約・schema・infra・security-sensitive 変更では Codex adversarial review を追加で実行できること | `/codex:adversarial-review` 結果 |
| FR-7 | MUST | PR は AI により作成できること。PR には issue key、spec、リスク区分、breaking change 有無を含めること | PR テンプレート |
| FR-8 | MUST | required checks が揃った PR は、人手 Approve なしで merge queue に投入できること | PR timeline / queue 状態 |
| FR-9 | MUST | merge 後に release workflow が tag / release / artifact attestation を自動生成できること | GitHub Release / attestation |
| FR-10 | MUST | deploy workflow は staging / production を自動実行できること。environment gate は機械判定を前提とすること | Deployments / environment records |
| FR-11 | MUST | merge / release / deploy の最終実行主体は GitHub App または GitHub Actions とし、ローカル端末から直接実行しないこと | actor 情報、workflow logs |
| FR-12 | MUST | GitHub 上に required checks, merge queue, release, deployments, workflow logs を一貫した証跡として残せること | GitHub UI / API |
| FR-13 | MUST | Atlassian 側にも MCP tool invocation の監査ログが残ること | Atlassian audit log |
| FR-14 | MUST | 破壊的変更は path / label / spec に基づき高リスクとして分類し、追加ゲートを課せること | labels / gate job / spec |
| FR-15 | MUST | rollback / hotfix の緊急経路を標準経路と分離して提供できること | emergency workflow / logs |
| FR-16 | SHOULD | Codex Cloud を使って長時間調査・修正をバックグラウンド実行できること | Codex Cloud task records |
| FR-17 | SHOULD | GitHub 上の Codex automatic reviews または `@codex review` を併用できること | PR review comment |
| FR-18 | SHOULD | 外部ログ基盤に GitHub / Atlassian のイベントを集約できること | SIEM / data lake records |

## 4. 非機能要件（Non-Functional Requirements）

| ID | 優先度 | 要件 | 指標 / 判断基準 |
|---|---|---|---|
| NFR-1 | MUST | 自然言語主体で運用できること | Slash command 依存が補助レベル |
| NFR-2 | MUST | 決定的制御は hooks / rulesets / checks で行うこと | LLM の気分に依存しない |
| NFR-3 | MUST | 監査証跡は GitHub / Atlassian / 外部ログの少なくとも2系統で保全すること | 欠損時の復元可能性 |
| NFR-4 | MUST | secrets は最小権限・短命トークン・setup 限定で扱うこと | App token / environment secret 設計 |
| NFR-5 | MUST | Rovo MCP の rate limit に依存しないこと | spec 凍結後の再問い合わせ抑制 |
| NFR-6 | MUST | 複数案件を平行実行しても UI が混線しないこと | 1 ticket = 1 worktree = 1 PR |
| NFR-7 | MUST | 人間が Jira issue と PR と Deployments を見れば現状が追えること | 運用レビュー |
| NFR-8 | MUST | breaking change は additive-first / expand-contract / feature flag / rollback artifact を原則とすること | spec / CI / deploy gate |
| NFR-9 | SHOULD | Cloud / local の環境差分を小さく保てること | setup script / AGENTS.md / universal image |
| NFR-10 | SHOULD | 高リスク変更のみ追加ゲートを有効化し、通常変更のスループットを下げすぎないこと | lead time / queue time |
| NFR-11 | SHOULD | ローカルで異常停止しても GitHub 上の状態から再開できること | PR・workflow 中心の運用 |
| NFR-12 | SHOULD | 導入後に repo 単位・組織単位で横展開しやすいこと | project-scoped plugin / reusable workflows |

## 5. 受け入れ基準
### AC-1 標準変更
- 開発者が日本語でチケット番号を指示する
- Jira / Confluence 文脈が spec に固定される
- Claude が実装し、Codex がレビューする
- PR が作成される
- required checks が通る
- merge queue から自動マージされる
- release と deploy が自動実行される
- GitHub / Atlassian / 外部ログに証跡が残る

### AC-2 高リスク変更
- 共有契約や migration を含む PR に高リスクラベルが自動付与される
- adversarial review と追加 gate が走る
- deploy 前に rollout / rollback 条件が検証される
- conditions を満たさなければ自動マージ・自動デプロイは進まない

### AC-3 複数並行開発
- 3件以上の別 issue を並行に回しても、branch / worktree / PR / spec が混ざらない
- 人間が Jira issue と PR 一覧から各案件の状態を判別できる

## 6. 依存関係
- Atlassian Cloud site
- GitHub repository / organization settings
- GitHub App
- GitHub Actions secrets
- Claude Code plugin/hook configuration
- Codex auth / Codex Cloud environment
- 外部ログ基盤（任意だが推奨）
