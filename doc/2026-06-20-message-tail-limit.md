# Message Tail Limit

## User-visible behavior

- `messages` now shows the latest 10 messages by default.
- Non-interactive CLI supports `messages [SESSION] --limit N` and `-n N`.
- `--limit 0` shows all messages.
- Interactive console supports `messages [N]`.

## Files touched

- `src/new_aize/cli.py`
- `tests/test_cli.py`

## Verification

```bash
python3 -m py_compile src/new_aize/*.py
python3 -m unittest discover -s tests -q
```

Both commands passed.
