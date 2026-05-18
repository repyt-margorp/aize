# Runtime Journal and Status Gateway

AIze runtime state should be driven by runtime events first, not by independent
status files as the source of truth.

The intended model is:

- runtime code emits a state-change event;
- subscribers receive the event through the existing history/SSE broadcast path;
- the same event is appended to `runtime_journal.jsonl`;
- restart/resume reconstructs runtime projections from the journal plus compact
  snapshots where available.

`session.json`, GoalManager state files, and service state files remain useful as
snapshots and indexes, but they should not be the only place where a state
transition is represented.

This matters for communication sessions such as Entrance. A single user prompt
has a turn-level lifecycle, while the Entrance Unit has a continuous service
goal. These must not share one `GoalCompleted` flag. Interactive replies can
complete a communication turn, but only GoalManager should mark the durable goal
complete.

The first implementation step records every appended history event into the
session runtime journal:

```text
.aize-state/sessions/<user>/<session_id>/runtime_journal.jsonl
```

Further migration should move direct status writes behind a small gateway API
and keep status files as projections of the journal rather than independent
authorities.
