# GoalManager — Agent/Session Decoupling

**Date**: 2026-03-19
**Scope**: `src/runtime/persistent_state.py`, `src/runtime/cli_service_adapter.py`

---

## 背景と目的

GoalManager の `audit_state` (all_clear / needs_compact / panic) は、従来セッション (talk レコード) に格納されていた。
エージェント（Codex・Claude など）のコンテキスト健全性はエージェント固有の状態であり、セッションに紐付けるべきではないため、エージェント側に移動した。

---

## 設計方針

### セッション (talk) が管理するもの
- `goal_text` — ゴール文字列
- `goal_active` / `goal_mode` — Active / Inactive
- `goal_progress_state` — `in_progress` / `complete`
- `goal_completed` — 完了フラグ
- `goal_reset_completed_on_prompt`, `goal_auto_compact_enabled`, `preferred_provider` など設定値

### エージェント (`agent_states`) が管理するもの
- `audit_state` — `all_clear` / `needs_compact` / `panic`
  - キー形式: `{service_id}::{username}::{talk_id}`
  - `persistent.json` の `agent_states` セクションに格納

---

## 新規 API (persistent_state.py)

```python
agent_state_key(service_id, username, talk_id) -> str
load_agent_audit_state(runtime_root, *, service_id, username, talk_id) -> str
save_agent_audit_state(runtime_root, *, service_id, username, talk_id, audit_state) -> None
reset_agent_audit_states_for_talk(runtime_root, *, username, talk_id) -> int
```

`reset_agent_audit_states_for_talk` は指定 talk に紐付く全エージェントの audit_state を `all_clear` に戻す。
ゴール更新時・ユーザープロンプト送信時に呼ばれる。

---

## audit_state がリセットされるタイミング

| トリガー | 処理 |
|---|---|
| 新しいゴールを設定 (POST `/talk/goal`) | `reset_agent_audit_states_for_talk` |
| ユーザーがプロンプトを送信 (SendPrompt) | `reset_agent_audit_states_for_talk` |
| GoalManager が `all_clear` 判定 | `save_agent_audit_state(audit_state="all_clear")` |
| コンパクション成功後 | `save_agent_audit_state(audit_state="all_clear")` |

---

## ブレティンボード通知 (agent.turn_started)

`dispatch_pending` を処理してエージェントが応答を開始した瞬間に
`agent.turn_started` イベントをセッション history に書き込む。
これにより UI (SSE /events) がエージェントの応答開始を即座に検知できる。

---

## FIFO の統一

ユーザー入力 (`kind="user_message"`) と GoalManager フィードバック (`kind="goal_feedback"`)
は同一の `pending_inputs` FIFO に格納される。
エージェントシステムは `dispatch_pending` を受け取ると FIFO を drain して処理する。

---

## TurnCompleted 後のフロー

```
TurnCompleted
  └─ auto-compact チェック (agent audit_state == all_clear の場合のみ)
       └─ コンテキスト残量 <= threshold → save_agent_audit_state("needs_compact")
  └─ goal_should_continue チェック
       └─ run_goal_manager() (非同期スレッド)
            ├─ audit_state == all_clear → run_goal_audit() → 結果を save_agent_audit_state
            ├─ audit_state == needs_compact → compact 実行
            ├─ goal_satisfied → update_talk_goal_flags(goal_completed=True) + return
            └─ goal_not_satisfied → append_pending_input("goal_feedback") + dispatch_pending
```

GoalManager のフィードバックは `dispatch_pending` で FIFO 経由で再びエージェントに渡る。
これはユーザー入力と同じパスを通るため、FIFO が統一されている。

---

## 後方互換性

- `update_talk_goal_flags` の `goal_audit_state` パラメータは残存するが no-op になった。
- セッション記録に古い `goal_audit_state` フィールドが残っていても害はない。
- `maybe_resume_after_restart` はエージェント側を優先し、エージェント記録がない場合はセッション shadow にフォールバックする。
