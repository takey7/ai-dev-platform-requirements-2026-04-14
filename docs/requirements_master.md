# requirements_master.md
このファイルは配布用の**単一版要件定義**です。詳細は各分割ドキュメントを参照してください。

## 1. 採用アーキテクチャ
標準構成は以下です。

- Atlassian Rovo MCP: Jira / Confluence / Compass の文脈取得
- Claude Code plugin/hooks: ローカル制御面
- OpenAI `codex-plugin-cc`: ローカルのクロスレビューと rescue
- GitHub / Codex Cloud: PR / 非同期タスク / cloud execution
- GitHub App / GitHub Actions: 最終的な merge / release / deploy 実行主体

## 2. 基本原則
1. 1 ticket = 1 worktree = 1 branch = 1 PR = 1 spec  
2. Jira / Confluence の live 文脈は一度 spec に凍結してから実装する  
3. hooks / checks / rulesets による決定的制御を優先する  
4. GitHub は最終統制面であり、最終状態遷移の正本  
5. 監査証跡は GitHub / Atlassian / 外部ログ基盤へ二重化する  
6. 通常系では bypass を使わない  
7. 緊急系は break-glass として標準系から分離する

## 3. FR-7 の最終方針
- 人手承認を標準系の必須条件にしない
- required checks / merge queue / release workflow / deploy workflow を「承認の実体」にする
- GitHub App または GitHub Actions が merge / release / deploy を実行する
- ローカル端末から直接 `main` 反映・release・prod deploy はしない
- 監査証跡は PR / Checks / Actions / Releases / Deployments / attestations に残す

## 4. 破壊的変更対策
破壊的変更は GitHub だけでは防げない。  
設計・実装・PR・merge・release・deploy の多層で対策する。

### 必須の設計項目
- Compatibility Impact
- Migration Plan
- Rollback
- Blast Radius
- Observability
- Rollout Strategy

### 高リスク扱いの例
- 共有 API
- public contract
- DB migration
- infra/prod
- auth / permission
- shared library behavior change

### 標準ルール
- additive first
- expand / contract
- feature flag
- compatibility tests
- rollback artifact 必須

## 5. 複数並行開発
複数案件の同時実行は可能。  
ただし、案件の分離単位は subagent ではなく **worktree / branch / PR** に置く。

### 人間から見た追跡面
- なぜやるか: Jira / Confluence
- 何をやるか: spec
- 何が変わるか: PR diff
- どこまで進んだか: checks / queue
- いつ出たか: Release
- どこに出たか: Deployments

## 6. テンプレートの使い方
- `templates/AGENTS.md` を repo root に置く
- `templates/.claude/settings.example.json` を `.claude/settings.json` の叩き台にする
- `templates/.codex/config.example.toml` を `.codex/config.toml` の叩き台にする
- `templates/.github/workflows/*.yml` を repo 用に調整する
- `templates/docs/specs/ISSUE_SPEC_TEMPLATE.md` を spec テンプレートにする

## 7. 実施順
1. Pilot repo を選ぶ
2. Rovo MCP / Claude hooks / Codex plugin を入れる
3. spec 運用を開始する
4. PR まで固める
5. required checks / merge queue を有効化する
6. release / deploy / attestations を有効化する
7. 外部ログ基盤へイベント送信する

## 8. すぐ確認すべきファイル
- `docs/02_requirements_definition.md`
- `docs/03_machine_approval_and_audit.md`
- `docs/04_breaking_change_controls.md`
- `docs/08_github_ruleset_checklist.md`
- `references/sources.md`
