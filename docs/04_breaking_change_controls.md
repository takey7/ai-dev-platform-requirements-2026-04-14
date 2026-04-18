# 04. 破壊的変更対策

## 1. 先に結論
**破壊的変更対策は GitHub だけでは足りない。**  
GitHub は merge / release / deploy の最終統制面だが、破壊的変更はその前段で設計・実装・レビュー・テスト・ロールアウト戦略として防ぐ必要がある。

## 2. 破壊的変更の分類

| 区分 | 例 | 標準扱い | 追加対策 |
|---|---|---|---|
| API 契約破壊 | 必須フィールド削除、レスポンス互換性破壊 | 高リスク | additive-first, versioning, compatibility test |
| DB 破壊 | column drop, enum 破壊, destructive migration | 高リスク | expand/contract, rollback SQL, backfill plan |
| イベント契約破壊 | topic schema 変更 | 高リスク | consumer compatibility test, dual publish |
| 認証/認可破壊 | scope 変更、認可ルール破壊 | 高リスク | adversarial review, security scan |
| Infra 破壊 | Terraform apply による再作成、 network policy 変更 | 高リスク | plan diff check, staged rollout |
| 実行時挙動破壊 | feature default ON, timeout/retry 変更 | 中〜高 | feature flag, canary, SLO checks |
| ドキュメントのみ | README 修正 | 低リスク | 通常フロー |

## 3. 多層防御の設計

### 3.1 設計段階
spec に必須で書く。

- Compatibility Impact
- Migration Plan
- Rollback
- Blast Radius
- Observability
- Feature Flag
- Consumer Impact

### 3.2 ローカル段階
Claude hooks で以下を抑止 / 強制する。

- 危険コマンド
- migration 追加時の spec 欄未記入
- protected path 変更時の追加レビュー要求
- 未実行テストのまま停止

### 3.3 PR 段階
PR に対して自動で以下を付与する。

- 高リスクラベル
- breaking_change フラグ
- 影響範囲サマリ
- rollback 要否
- deploy 戦略（direct/canary/blue-green）

### 3.4 Merge / Release 段階
required checks を追加する。

- compatibility-tests
- migration-safety
- contract-diff
- rollback-ready
- release-note-check
- deploy-strategy-check

### 3.5 Deploy 段階
- staging の smoke test
- canary 指標
- error budget check
- rollback artifact の存在確認
- feature flag rollback の自動化

## 4. 標準ルール
### 4.1 API
- additive first
- 既存フィールド削除は 1 段階でやらない
- deprecated period を設ける
- shared contract には schema diff チェックを必須化

### 4.2 DB
- expand / contract を原則とする
- destructive migration は標準フローでは禁止、または別 gate で管理
- backfill を分離ジョブ化する
- rollback SQL または restore 戦略を明記する

### 4.3 Runtime
- デフォルト値変更は feature flag 経由
- retry / timeout / cache 戦略変更は adversarial review 対象
- 共有ライブラリ変更は consumer test を走らせる

### 4.4 Infra
- `terraform plan` / `kubectl diff` / policy-as-code を check にする
- 本番向け IaC 変更は別 release lane も検討
- infra-prod は protected path 扱いにする

## 5. GitHub の役割
GitHub は以下を担う。

- rulesets
- required checks
- merge queue
- release workflow
- deployments
- artifact attestations

ただし、GitHub は**最後の門**であって、唯一の防御ではない。

## 6. protected path とリスククラス
推奨 path 分類:

- `packages/contracts/**` → `risk=high`
- `db/migrations/**` → `risk=high`
- `infra/prod/**` → `risk=high`
- `auth/**` → `risk=high`
- `api/public/**` → `risk=high`
- `docs/**` → `risk=low`

これをもとに AI gate や spec gate の条件分岐を行う。

## 7. 高リスク変更で必須にすること
- adversarial review
- compatibility test
- rollback-ready check
- release note draft
- canary / staged rollout 指定
- 監視項目の明記

## 8. ラベル戦略
推奨ラベル:

- `risk:low`
- `risk:medium`
- `risk:high`
- `breaking-change`
- `db-migration`
- `infra-prod`
- `security-sensitive`
- `rollback-ready`
- `needs-canary`

ラベルは手動付与でなく、自動分類を原則にする。

## 9. 緊急変更
hotfix / rollback では通常フローを短縮してよいが、以下は必須。

- 緊急理由
- rollback plan
- change window
- owner
- 事後レビュー issue
- 外部ログ基盤への明示的 event

## 10. 受け入れ基準
- high risk path を触ると PR に自動分類結果が付く
- spec に compatibility / migration / rollback 欄が空なら check fail
- rollout 戦略未指定なら prod deploy fail
- rollback artifact 不在なら prod deploy fail
