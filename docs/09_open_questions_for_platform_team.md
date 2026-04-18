# 09. プラットフォームチームへの未決事項

## 1. GitHub
1. private/internal repo で使う GitHub plan は何か
2. GitHub App は org 所有か team 所有か
3. rulesets は org-level と repo-level のどちらを主にするか
4. release branch 戦略を採るか、trunk-based に寄せるか
5. self-hosted runner は使うか

## 2. デプロイ
1. デプロイ先はどこか
2. staging / production 以外に preview 環境は必要か
3. canary / blue-green / progressive delivery のどれを使うか
4. rollback artifact は何を正本にするか
5. デプロイ失敗時にどこまで自動 rollback するか

## 3. Atlassian
1. OAuth 2.1 を標準化できるか
2. API token は誰に許可するか
3. 許可ドメイン / IP allowlist はどう管理するか
4. audit log の保管先はどこか
5. Rovo MCP のレート制限に対する運用ルールは何か

## 4. AI gate
1. AI gate はどこまで required check にするか
2. high risk だけ gate を強くするか、全 PR で同一 gate にするか
3. JSON schema をどう固定するか
4. false positive / false negative の扱いをどうするか

## 5. 監査
1. 外部ログ基盤は何を使うか
2. 保存期間は何日か
3. 監査担当は誰か
4. break-glass の承認 / 記録 / 事後レビューは誰が持つか

## 6. 例外運用
1. 本当に人手承認ゼロにするのか
2. もし一部 repo だけ manual lane を残すなら、その条件は何か
3. hotfix の SLA と通常系の SLA をどう分けるか
4. どの変更種類を AI-only 禁止にするか（もしあれば）

## 7. 導入順
1. pilot 対象 repo はどれか
2. 最初に high risk path を持たない repo から始めるか
3. who owns template updates
4. reusable workflows を誰が保守するか
