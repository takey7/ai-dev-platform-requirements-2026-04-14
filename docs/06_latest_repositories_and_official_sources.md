# 06. 最新リポジトリ・公式ソース調査メモ

調査日: 2026-04-14 JST

## 1. 採用対象

| コンポーネント | 公式ソース | 採用理由 | 備考 |
|---|---|---|---|
| Atlassian Rovo MCP | `atlassian/atlassian-mcp-server` / Atlassian Support | Jira / Confluence / Compass 文脈取得の公式経路 | `/v1/mcp` を使う |
| Claude plugins | Claude Code Plugins reference | plugin / skill / agent / hook を project scope で配布できる | `.claude/settings.json` で共有 |
| Claude hooks | Claude Code Hooks guide | 危険コマンド抑止・format・review gate の決定的制御 | LLM 任せにしない |
| Codex local bridge | `openai/codex-plugin-cc` | Claude から Codex review / rescue を呼ぶ最短経路 | `/codex:review` 等 |
| Codex CI bridge | `openai/codex-action` | GitHub required check として AI gate を作りやすい | sandbox / safety-strategy がある |
| Codex GitHub integration | OpenAI Developers: Codex in GitHub | PR review / automatic reviews の公式経路 | GitHub review は P0/P1 中心 |
| Codex Cloud env | OpenAI Developers: Cloud environments | 長時間タスク・setup/maintenance・secrets・cache の設計指針 | secrets は setup のみ |
| GitHub rulesets | GitHub Docs | branch protection より組織横断で扱いやすい | layering あり |
| GitHub merge queue | GitHub Docs | busy branch の安全な自動マージ | `merge_group` 対応必須 |
| GitHub environments | GitHub Docs | deploy gate / deployment history / secrets 制御 | required reviewers は通常系では使わない |
| Artifact attestations | GitHub Docs | release artifact の provenance | `actions/attest@v4` 推奨 |

## 2. 今回の採用判断
### 採用
- Atlassian Rovo MCP
- Claude plugin/hooks
- `codex-plugin-cc`
- `codex-action`
- GitHub rulesets / merge queue / Actions / environments / attestations
- Codex Cloud（長時間処理・バックグラウンド用）

### 条件付き採用
- GitHub 上の Codex automatic reviews
- Codex Cloud を使う background rescue
- optional な tmux 可視化

### MVP から外す
- preview / experimental 機能を主制御面に置くこと
- ruleset の silent exempt bypass を通常運用で使うこと
- Atlassian API token を人間の通常利用のデフォルトにすること

## 3. 今回の調査から重要だった事実
- Atlassian Rovo MCP の endpoint は `/v1/mcp`
- `/v1/sse` は 2026-06-30 以降非推奨
- OAuth 2.1 が default / recommended
- API token 認証は domain allowlist の扱いが異なる
- Atlassian 側に MCP invocation の audit log が残る
- Claude plugin は project scope で配布できる
- hooks は deterministic control として使える
- `codex-plugin-cc` は review / adversarial review / rescue / status / result / cancel を提供
- `codex-action` は secure proxy + sandbox + safety strategy を持つ
- GitHub rulesets は layered で最も厳しいルールが効く
- merge queue は busy branch を壊しにくい
- GitHub review 上の Codex は P0/P1 中心
- Codex Cloud の secrets は setup script のみ
- artifact attestation には workflow link / repo / org / env / SHA が含まれる

## 4. 監視すべき更新点
- `codex-plugin-cc` の background rescue 安定性
- Claude Code plugin / hooks の schema 変更
- GitHub custom deployment protection rules の preview → GA 化
- Atlassian Rovo MCP の supported tools / rate limits / admin controls の変更
- Codex Cloud の environment / security / feature maturity の変更

## 5. 実装前の再確認チェック
実装着手の直前に以下だけ再確認すること。

1. `codex-plugin-cc` README
2. `openai/codex-action` README
3. Claude Code plugin / hooks docs
4. GitHub rulesets / merge queue / environments docs
5. Atlassian Rovo MCP getting started / control settings / monitoring docs
