# Manual Compact Command Failure

## Summary

`service.manual_compact_failed` が

```json
{
  "type": "service.manual_compact_failed",
  "threshold_left_percent": 101,
  "session_id": "019cf306-5cb1-7930-a2a1-4819b00ddefe",
  "left_percent": "30",
  "used_percent": "70",
  "compaction": "unconfirmed",
  "post_left_percent": "30",
  "post_used_percent": "70",
  "returncode": "5"
}
```

の形で出ていた問題は、待機 timeout ではなく、`/compact` の slash command が受理されていないのに、helper が「footer が変わらなかった」と誤診していたことが主因だった。

## What Was Wrong

対象 helper:

- [`.temp/compact_codex_session.sh`](../.temp/compact_codex_session.sh)

旧挙動:

1. `codex resume <session> --no-alt-screen` を `tmux` で開く
2. `tmux send-keys -l /compact` で command を送る
3. footer の `% left` が増えれば成功
4. 増えなければ `compaction: unconfirmed` として `exit 5`

この設計には 2 つ問題があった。

### 1. Command Acceptance を確認していなかった

`tmux send-keys -l` は paste 扱いなので、slash command UI に対しては plain text 扱いになりやすい。  
実際の pane を確認すると、`/compact` 送信後も Codex 側は compact 実行に入らず、prompt 候補として別の slash command を出していた。

再現時の観測:

- `post_prompt_line: › Use /skills to list available skills`
- `post_status_line: gpt-5.4 medium · 30% left · ~/workspace/aize`
- `post_left_percent: 30`

つまり `/compact` は compact command として受理されておらず、待ちフェーズ以前の問題だった。

### 2. Wait Failure と Send Failure が同じ失敗に潰れていた

旧 helper は

- `post_left_percent > left_percent`
- または `Context compacted`

だけを成功条件にしていた。  
そのため、

- command 自体が受理されていない
- slash prompt に留まっている
- ただ footer は読める

というケースでも `exit 5` になっていた。

これは実際には wait failure ではなく send failure に近い。

## Reproduction Findings

対象 session:

- `019cf306-5cb1-7930-a2a1-4819b00ddefe`

修正後 helper での再現結果:

```text
status_line:   gpt-5.4 medium · 30% left · ~/workspace/aize
left_percent: 30
used_percent: 70
pre_command_status: idle
command_status: accepted
prompt_line: › Use /skills to list available skills
compaction: triggered
wait_status: unknown
post_status_line:   gpt-5.4 medium · 30% left · ~/workspace/aize
post_prompt_line: › Use /skills to list available skills
post_left_percent: 30
post_used_percent: 70
command_status: unexpected_prompt_after_command
compaction: command_not_accepted
EXIT:6
```

重要な点:

- timeout ではない
- footer は取得できている
- `% left` も取得できている
- しかし prompt line が compact 実行状態になっていない

## Fix Applied

### Helper Changes

対象:

- [`.temp/compact_codex_session.sh`](../.temp/compact_codex_session.sh)

修正内容:

1. 送信前に `C-u` で prompt をクリア
2. `/compact` を `-l` paste ではなく key-by-key 送信
3. `pre_command_status` を追加
   - `idle`
   - `busy`
4. `command_status` を追加
   - `accepted`
   - `pending_in_prompt`
   - `unexpected_prompt_after_command`
   - `blocked_by_active_turn`
5. `wait_status` を追加
   - `context_compacted`
   - `left_percent_increased`
   - `footer_missing_after_command`
   - `footer_unchanged_after_command`
6. `post_prompt_line` を観測
7. `post_prompt_line` が残っている場合は wait failure ではなく `command_not_accepted` として `exit 6`

### Event Enrichment

対象:

- [`src/runtime/cli_service_adapter.py`](../src/runtime/cli_service_adapter.py)

`run_codex_compaction()` が event に追加で載せる値:

- `command_status`
- `prompt_line`
- `wait_status`

これで HTTPBridge / history event からも、「送信失敗なのか、待ち失敗なのか」を見分けられる。

## Current Interpretation

現時点の manual compact failure は、

- `TIMEOUT`
- context footer unreadable

ではない。

より正確には、

- `codex resume --no-alt-screen` 上で `/compact` が compact command として受理されていない
- もしくは slash command selection が別候補に化けている

という send-side failure である。

## Remaining Work

今回の修正は「失敗理由の誤診修正」であり、manual compact 自体を成功させたわけではない。  
今後の対応候補は次のとおり。

1. `tmux send-keys` ベースをやめて、pty を直接制御する helper に置き換える
2. Codex 側に compact 用の non-interactive / app-server 的 API があればそちらへ寄せる
3. slash command prompt の候補選択挙動を追加観測して、`/compact` の受理確認をより厳密にする

## Operational Note

今回の event では `threshold_left_percent: 101` になっているが、これは異常ではない。  
manual compact では「現在の `% left` に関係なく compact を試行する」ための強制値として使っている。
