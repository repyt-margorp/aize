# Role Dispatch Readiness Refactor

## Purpose

SessionLogを読む必要があるという事実と、システム全体でどのRoleを先に
DispatchするかというScheduling判断を分離する。

旧実装はLogごとの起動理由とPriorityを集約していた。
現在は次の二段階へ分離されている。

```text
SessionLog + RoleCursor
        |
        v
RoleDispatchReadiness
  - ready
  - from_log_seq
  - observed_to_seq
  - wake_reasons
        |
        v
Dispatch Scheduler Policy
  - Session/Role priority
  - waiting age
  - available_after
  - active lease constraints
        |
        v
DispatchRun
```

## Progress Legend

- `[ ]`: 未着手
- `[x]`: 完了
- `BLOCKED:`: 外部判断または別作業が必要

## Target Invariants

- [x] `wake_reasons`は起動理由だけを表し、Priorityを持たない。
- [x] `(session_id, role)`ごとの待機中Readinessは最大一つとする。
- [x] Dispatch取得前にRoleCursor以降を再監査し、最新Logまで範囲を広げる。
- [x] Agentへは`from_log_seq`から`observed_to_seq`までを一括して渡す。
- [x] Agent実行中に追加されたLogは現在のRunへ混ぜず、次のReadinessへ残す。
- [x] RoleCursorはRunが取得した`observed_to_seq`までだけ進める。
- [x] Dispatch順序はLog種別ではなく、Session/Role Scheduling Policyが決める。
- [x] SessionLogとRoleCursorを永続的な正本とする。
- [x] Dispatch Readinessの永続ファイルは再生成可能な索引とする。
- [x] 同じSessionの同じRoleへ同時に複数Leaseを与えない。
- [x] 後方互換用の旧Proposal分岐は残さない。

## Target Data Model

### RoleDispatchReadiness

```json
{
  "readiness_id": "ready-...",
  "session_id": "session-a",
  "goal_id": "goal-a",
  "role": "GoalManager",
  "status": "ready",
  "from_log_seq": 101,
  "observed_to_seq": 106,
  "wake_reasons": [
    {
      "seq": 101,
      "kind": "UserInput",
      "message_id": "msg-...",
      "log_id": "log-..."
    },
    {
      "seq": 106,
      "kind": "WorkerReport",
      "message_id": "msg-...",
      "log_id": "log-..."
    }
  ],
  "available_after": "",
  "first_ready_at": "2026-08-08T00:00:00Z",
  "refreshed_at": "2026-08-08T00:00:10Z"
}
```

`wake_reasons`は説明・監査・Prompt構築に使用する。Schedulerの順位値は
このデータへ埋め込まない。

### Dispatch Scheduling Input

Schedulerへ渡す入力はReadinessと実行時状態から構成する。

```json
{
  "readiness": {},
  "session_policy": {
    "scheduling_class": "normal",
    "base_priority": 0
  },
  "role_policy": {
    "role": "GoalManager",
    "base_priority": 0
  },
  "runtime": {
    "now": "2026-08-08T00:01:00Z",
    "role_already_acquired": false
  }
}
```

Schedulerの出力は選択結果と計算根拠を返す。

```json
{
  "selected_readiness_id": "ready-...",
  "scheduling_score": 12,
  "scheduling_reason": "normal class; waiting age 60 seconds"
}
```

`scheduling_score`は実行時に計算し、SessionLogの起動理由とは分離する。

## File Responsibilities

### `src/dispatch_projection.py`

- SessionLog範囲からRoleのReadinessを純粋関数で導出する。
- GM/Workerが反応するLog種別を判定する。
- `wake_reasons`を作る。
- Priority計算やファイルI/Oを行わない。

### `src/dispatch_policy.py`

- Session/Role policy、待機時間、Lease状態からScheduling scoreを計算する。
- 利用可能なReadinessから一つを選ぶ。
- SessionLogの内容解釈やファイルI/Oを行わない。

### `src/store_dispatch_readiness.py`

- 名前を`store_dispatch_readiness.py`へ変更する。
- `(session_id, role)`単位でReadinessを作成・更新・取得する。
- Readinessの`ready/acquired/resolved/stale` lifecycleを保存する。
- ProjectionやScheduling規則を実装しない。

### `src/store_dispatch.py`

- 選択されたReadinessをLeaseとして取得する。
- RunへLog範囲のSnapshotを記録する。
- Role終了時にSnapshot終端までCursorを進める。
- AgentがRun中に追記したLogを次回分として残す。

### Runtime files

```text
.aize-state/
  store/
    sessions/<session-id>/
      log-*.jsonl
    metadata/
      sessions/<session-id>.json
    runtime/
      dispatch_readiness.json
      dispatch_runs/
```

`dispatch_readiness.json`はSessionLogとRoleCursorから再生成可能でなければならない。

## Implementation Plan

### Phase 1: Characterization tests

- [x] 通常UserInputでGMだけがReadyになるテストを追加する。
- [x] GMのWorker要求でWorkerだけがReadyになるテストを追加する。
- [x] Worker報告でGMがReadyになるテストを追加する。
- [x] 複数の起動理由が一つのLog範囲へ集約されるテストを追加する。
- [x] ready中にLogが増えると同じReadinessの終端が更新されるテストを追加する。
- [x] acquired後に増えたLogが次回分へ残るテストを追加する。
- [x] 同一Session/RoleでReadinessが重複しないテストを追加する。

### Phase 2: Readiness model

- [x] 旧Proposalを`RoleDispatchReadiness`へ置き換える。
- [x] 起動理由を`wake_reasons`へ統一する。
- [x] `wake_reasons[].priority`を削除する。
- [x] Log範囲終端を`observed_to_seq`へ統一する。
- [x] 待機開始時刻を`first_ready_at`へ統一する。
- [x] ProjectionからPriority constantsへの依存を削除する。
- [x] Projectionが`None`を返す条件をdocstringとテストで固定する。

### Phase 3: One readiness per Session/Role

- [x] Readinessの一意キーを`(session_id, role)`にする。
- [x] 新規Log検出時は既存Ready entryを更新する。
- [x] Message単位で重複entryを生成する分岐を削除する。
- [x] ready中の更新で`first_ready_at`を保持する。
- [x] `observed_to_seq`と`wake_reasons`だけを最新化する。
- [x] acquired entryは変更せず、Run終了後に次のReadinessを再導出する。

### Phase 4: Scheduler policy

- [x] Log種別Priority constantsをDispatch Projectionから削除する。
- [x] Session scheduling classの既定値を定義する。
- [x] Session base priorityの既定値を定義する。
- [x] Role policyの既定値を定義する。
- [x] waiting ageによるaging計算を純粋関数として実装する。
- [x] `available_after`以前のReadinessを選択対象外にする。
- [x] acquired中の同一Session/Roleを選択対象外にする。
- [x] score同値時の安定した順序を定義する。
- [x] Scheduling計算根拠をDispatchRunへ記録する。

初期Policyは挙動を単純に保つ。

```text
scheduling_score = session.base_priority
                 + role.base_priority
                 + waiting_age_score
```

Logの`UserInput`や`WorkerReport`種別をscoreへ直接加算しない。

### Phase 5: Store adapter rename and persistence

- [x] Store adapterを`store_dispatch_readiness.py`へ変更する。
- [x] state keyを`dispatch_readiness`へ変更する。
- [x] runtime fileを`dispatch_readiness.json`へ変更する。
- [x] 識別子を`readiness_id`へ変更する。
- [x] Store mixin/import/package module一覧を更新する。
- [x] 旧runtime fileと旧state keyの互換読込みを残さない。
- [x] 初回起動時にSessionLogとCursorからReadinessを再生成する。
- [x] CLI表示を`dispatch-readiness`へ変更する。

### Phase 6: Lease and Cursor semantics

- [x] Lease取得直前にReadinessを最新SessionLogまでrefreshする。
- [x] 取得時の`from_log_seq`と`observed_to_seq`をRunへ固定する。
- [x] Agent Promptへ固定範囲内のLogを一括提供する。
- [x] GM終了時にGM CursorをRun終端まで進める。
- [x] Worker終了時にWorker CursorをRun終端まで進める。
- [x] Run中の追記がCursorの先に残ることを確認する。
- [x] Agent失敗時も取得済み終端までCursorを進める。
- [x] Daemon再起動時にacquired Readinessを復旧する。

### Phase 7: CLI and observability

- [x] `dispatch-readiness [SESSION]`で現在のReadinessを表示する。
- [x] Session、Role、Log範囲、待機時間を表示する。
- [x] `wake_reasons`をPriorityなしで表示する。
- [x] Scheduler scoreと選択理由はDispatchRun側に表示する。
- [x] `graph`のready/running表示を新しいReadinessへ接続する。
- [x] CLI helpと設計文書の旧コマンド表記を削除する。

### Phase 8: Cleanup

- [x] 旧Proposal型を削除する。
- [x] 旧Request Store mixinを削除する。
- [x] 旧起動理由フィールドを削除する。
- [x] Log種別Priority constantsの未使用定義を削除する。
- [x] Message ID依存のDispatch分岐を削除する。
- [x] 旧名称を`rg`で全コード・テスト・文書から検査する。
- [x] Dead codeと後方互換分岐が残っていないことを確認する。

## Required Tests

- [x] Projection unit tests
- [x] Scheduler policy unit tests
- [x] Store readiness lifecycle tests
- [x] GM -> Worker -> GM integration test
- [x] UserInput while Worker is running test
- [x] Multiple accumulated wake reasons test
- [x] Multiple Sessions scheduling order test
- [x] Same Session/Role lease exclusion test
- [x] Log appended during acquired Run test
- [x] Daemon restart recovery test
- [x] CLI rendering test
- [x] Full regression suite

Verification command:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Decisions Required Before Phase 4

- [x] Session scheduling classはUnitから継承せず、Session固有値とする。
- [x] Session priorityは`set-session-policy`で実行中にも変更可能とする。
- [x] GoalManagerとWorkerAgentの既定Role priorityは同値の0とする。
- [x] waiting ageは60秒ごとに+1、上限100とする。
- [x] Singleton Sessionにも通常Sessionと同じ既定Policyを適用する。
- [x] 将来の複数WorkerではAgent membership単位のCursorを追加し、現在のRole Cursorを暗黙の単一membershipとして扱う。

これらはReadiness導出と独立しているため、Phase 1からPhase 3までは先に実装できる。

## Definition of Done

- [x] 一つのSession/Roleに複数の待機Readinessが存在しない。
- [x] 一回のDispatchでCursor以降から取得時点の最新Logまでを処理する。
- [x] `wake_reasons`にPriorityが存在しない。
- [x] Scheduling順序が`dispatch_policy.py`だけで決まる。
- [x] ProjectionとPolicyの単体テストがStoreなしで実行できる。
- [x] SessionLogからReadinessを再生成できる。
- [x] 旧Dispatch Request/Proposal実装と互換コードが削除されている。
- [x] 全テストが成功する。
- [x] Daemon再起動後もActive/Incomplete Sessionの処理が継続する。

## Current Progress

- [x] 永続化、Projection、Policy、Store adapterの境界を分離した。
- [x] 現行Dispatchの挙動を維持する純粋関数テストを追加した。
- [x] 本リファクタリングの手順と完了条件を文書化した。
- [x] Phase 1のcharacterization testsを完成させた。
- [x] Phase 2からPhase 8を実装した。
