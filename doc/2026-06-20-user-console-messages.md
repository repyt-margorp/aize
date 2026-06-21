# UserConsole Messages

## User-visible behavior

Dispatch now records text output from both `GoalManager` and `WorkerAgent` as
first-class Messages addressed to `UserConsole`.

This means CLI users can inspect Agent output through the same Session message
surface used for user input, Session capabilities, WorkerAgent handoff, and
GoalManager completion.

## Files touched

- `src/store.py`
  - Added the `UserConsole` recipient.
  - Added `send_user_console_message` to the default Session capabilities for
    `GoalManager` and `WorkerAgent`.
  - Added paired `*ConsoleOutput` Messages for dispatch step output emitted by
    either Agent role.
  - Updated existing `SessionCapabilities` payloads when defaults change.
- `tests/test_cli.py`
  - Updated dispatch and console expectations to include `UserConsole` output
    Messages.
- `README.md`
- `docs/minimal-architecture.md`

## Verification

- `python3 -m py_compile src/*.py`
- `python3 -m unittest discover -s tests -q`
- CLI smoke test with external Agent execution disabled:
  `session console-check`, `update-goal Reply`, `send hello`, `dispatch`,
  `messages`.

The smoke test showed `UserInput` delivered to `Session`, ordinary Agent step
Messages, and paired `GoalManager` / `WorkerAgent` Messages addressed to
`UserConsole`.

## Remaining risk

`UserConsole` Messages are currently visible in the general `messages` view.
A later CLI pass can add a focused `console-output` command if the full message
timeline becomes too noisy.
