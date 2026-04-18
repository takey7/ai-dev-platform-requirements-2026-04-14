# 08. GitHub Ruleset / Environment チェックリスト

## 1. `main` ブランチ
- [ ] PR 必須
- [ ] direct push 禁止
- [ ] required status checks 設定
- [ ] merge queue 有効化
- [ ] auto-merge 許可
- [ ] force push 禁止
- [ ] branch deletion 禁止
- [ ] bypass actor は通常系で設定しない
- [ ] standard bypass は緊急用 actor のみ

## 2. `release/*`
- [ ] `main` より厳しい ruleset
- [ ] high risk checks を追加
- [ ] direct push 禁止
- [ ] tag / release workflow を限定

## 3. required checks（例）
- [ ] `ci`
- [ ] `spec-gate`
- [ ] `risk-classification`
- [ ] `security-scan`
- [ ] `ai-gate`
- [ ] `release-ready`

## 4. workflow トリガ
- [ ] required checks の workflow は `pull_request`
- [ ] merge queue を使うなら `merge_group` も追加
- [ ] skip される workflow を required check にしない

## 5. environments
### staging
- [ ] `staging` environment を作成
- [ ] environment secret を分離
- [ ] deployment history を残す

### production
- [ ] `production` environment を作成
- [ ] deploy branch 制限
- [ ] 機械 gate を配置
- [ ] 通常系で human reviewer を required にしない
- [ ] break-glass workflow は分離

## 6. Artifact attestation
- [ ] `attestations: write`
- [ ] `id-token: write`
- [ ] build artifact に対する attestation 生成
- [ ] release / deploy metadata と相互参照

## 7. GitHub App
- [ ] 専用 app を作成
- [ ] installation token を使用
- [ ] repo scope / permission を最小化
- [ ] PAT を標準経路から排除
- [ ] merge / release を app または actions からのみ実行

## 8. CODEOWNERS の扱い
- [ ] 通常系で required code owner review は使わない
- [ ] notification 用または緊急系 / 手動系用として使うなら可
- [ ] 機械承認モデルと矛盾しないように設定する

## 9. 監査
- [ ] Actions logs を保持
- [ ] Release / Deployments を追跡可能
- [ ] Webhook または audit stream を外部へ送る
- [ ] break-glass の event を明示的に残す
