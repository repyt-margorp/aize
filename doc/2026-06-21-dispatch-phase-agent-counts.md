# Dispatch Phase Agent Counts

## Changed

- Graph and agent-pool allocation counts now use dispatch `current_phase`.
- An acquired run in `GoalManagerPrecheck` or `GoalManagerCompletion` counts as `G:1,W:0`.
- An acquired run in `WorkerAgent` counts as `G:0,W:1`.
- Agent pool active-run output now includes the current phase.

## Rationale

The previous display counted every acquired dispatch run as both GoalManager and WorkerAgent allocated. That made a newly sent message look like it immediately started WorkerAgent even when the dispatch run was still in GoalManager.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/new_aize/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
- Local state inspection showed the recent `time` runs completed with `GoalManagerPrecheck` and `GoalManagerCompletion` only.
