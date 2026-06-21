# Dispatch Role Agent Counts

## Changed

- Graph and agent-pool allocation counts now use dispatch `current_phase`.
- An acquired run in `GoalManager` counts as `G:1,W:0`.
- An acquired run in `WorkerAgent` counts as `G:0,W:1`.
- Agent pool active-run output includes the current phase.

## Rationale

The previous display counted every acquired dispatch run as both GoalManager and WorkerAgent allocated. That made a newly sent message look like it immediately started WorkerAgent even when the dispatch run was still in GoalManager review.

## Verification

- `PYTHONPATH=src python3 -m py_compile src/*.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -q`
