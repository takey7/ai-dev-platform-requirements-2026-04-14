# Parallel Batch Orchestrator

v0.2.1 の標準は、Claude と Codex を直接フリーフォーム会話させず、orchestrator が structured contract / trace / approval state を管理する方式です。

## Why

直接会話は文脈の漂流、監査欠落、無限往復、権限境界の曖昧化が起きやすいです。v0.2.1 は mediated baton に固定します。

1. Claude coordinator が `task_contract` を作る
2. Codex worker が編集前に `task_understanding` を返す
3. Claude coordinator が `approved`, `revise_contract`, `split_task`, `block` を返す
4. `approved` の場合だけ Codex が編集する
5. 結果、diff、validation、review を orchestrator DB に trace として残す

## Batch Execution

```bash
./bin/platform orchestrator batch create \
  --project PROJ \
  --jql 'project = PROJ AND labels = "ai:auto" AND status in ("To Do", "Selected for Development")' \
  --max-parallel 3

./bin/platform orchestrator batch status
```

Claude coordinator は JQL の issue 群から batch plan を作ります。

- `dependencies`: 先に merge すべき issue
- `conflict_group`: 同時編集を避ける共有面
- `task_contract`: Codex に渡す実装契約
- `design_memo`: batch 全体の整合メモ

Codex worker は `1 issue = 1 worktree = 1 branch = 1 PR` を守ります。

## Scheduler Rules

既定値:

```json
{
  "scheduler": {
    "max_parallel_per_repo": 3,
    "max_parallel_per_project": 5,
    "contract_handshake": "required",
    "max_baton_rounds": 2
  },
  "failure": {
    "max_attempts": 2,
    "backlog_statuses": ["To Do", "Backlog"]
  },
  "github": {
    "merge_policy": "merge_queue"
  }
}
```

同じ repo の独立 issue は最大 3 並列です。同じ `conflict_group`、未完了 dependency、同じ protected path を含む issue は同時に lease しません。

## Failure Policy

一時的な DNS / timeout / 429 / 5xx は bounded retry します。上限を超えた場合、validation failure、contract 不整合、Codex が理解段階で編集した場合は terminal failure として扱います。

Quality gate failure は terminal failure と同じ扱いにしません。required checks / Codex review / spec-gate / risk gate で止まった issue は、その issue だけを隔離し、batch は `degraded` として継続します。

- `gate_waiting_human`: intentional gate、risk approval、changes requested、manual approval 待ち
- `gate_failed`: required check、spec-gate、security-scan、merge queue removal
- `waiting_dependency`: 依存先が gate / failed / backlog のため待機。並列枠は消費しない
- `backlog`: operator が fail/backlog し、Jira を `To Do` / `Backlog` 相当に戻した terminal state

Gate 状態の確認と解除:

```bash
./bin/platform orchestrator gate status --batch PROJ-YYYYMMDDHHMMSS
./bin/platform orchestrator gate status --project PROJ
./bin/platform orchestrator gate unblock --issue PROJ-123 --reason "required check fixed"
```

Jira comment でも明示解除できます。

```text
/ai unblock
```

Terminal failure の処理:

- job state を `failed` にする。operator が `--backlog` を指定した場合は `backlog` まで進める
- Jira sticky comment に原因、attempt、再実行コマンドを書く
- Jira status を `To Do` / `Backlog` 相当に best-effort transition する
- lease を解放し、scheduler は次の executable issue へ進む

再実行:

```bash
./bin/platform orchestrator retry --issue PROJ-123
```

手動 fail/backlog:

```bash
./bin/platform orchestrator fail \
  --issue PROJ-123 \
  --backlog \
  --reason "validation failed after operator review"
```

## Merge Policy

GitHub が最終統制面です。v0.2.1 の既定は `github.merge_policy = "merge_queue"` です。

Worker は PR が `ready_for_merge` になった後、`gh pr merge --auto --merge` で GitHub auto-merge / merge queue を有効化します。branch protection、required checks、merge queue の実際の判定は GitHub 側に委ねます。

Jira は `ready_for_merge` では `Done` にしません。GitHub PR が `MERGED` になったことを polling で確認した後だけ `Done` / `完了` に移動します。

手動 merge 運用に戻す場合:

```bash
./bin/platform orchestrator configure --github-merge-policy manual
```

## Pause / Resume

Batch:

```bash
./bin/platform orchestrator batch pause --batch PROJ-YYYYMMDDHHMMSS
./bin/platform orchestrator batch resume --batch PROJ-YYYYMMDDHHMMSS
./bin/platform orchestrator batch cancel --batch PROJ-YYYYMMDDHHMMSS
```

Issue:

```bash
./bin/platform orchestrator pause --issue PROJ-123
./bin/platform orchestrator resume --issue PROJ-123
./bin/platform orchestrator cancel --issue PROJ-123
```

Jira comment commands:

```text
/ai pause
/ai resume
/ai retry
/ai cancel
/ai status
```
