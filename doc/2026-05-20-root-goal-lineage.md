# Root Goal Lineage

This note records the intended AIze goal hierarchy and the architecture constraint that goes with it.

## Goal hierarchy

- The Root session is the top of the visible session tree.
- The Root session's active goal is the overall goal for that tree.
- Subordinate sessions must stay inside that lineage rather than behaving like sibling roots.
- AIze Development is one such subordinate session: it is launched under `default` and exists to refine the Root goal into narrower implementation subgoals.
- Delegated task sessions under AIze Development are subgoals of that parent, and therefore indirect subgoals of Root as well.

In project terms, the Root goal acts like a constitution and lower goals act like subordinate laws: each lower goal is valid only insofar as it serves the higher goal above it.

## AIze Development

The canonical AIze Development Unit is not an alternate top-level authority.

- It must remain a child of Root.
- The reusable parent is the canonical bug-hunting Unit session launched under `default`.
- Ad hoc sessions that merely look like AIze Development are not part of the supported lineage and must not be reused as compatibility parents.
- It should keep only the durable development workflow and audit trail.
- Concrete implementation should happen in finite child task sessions.
- Those child sessions should report results back upward rather than replacing the parent-child hierarchy.
- Verification should happen on a separate port or isolated runtime before touching the active runtime.
- Cutover should happen only after verification has been recorded and the old runtime can be stopped safely.

## Implementation philosophy

The goal hierarchy above is paired with a MINIX-style implementation philosophy:

- keep service boundaries explicit
- prefer small, observable units
- avoid hidden text heuristics for core routing or authority decisions
- persist explicit state when conditional behavior matters

This note captures a standing project-developer instruction: preserve both the Root-first goal hierarchy and the MINIX-style implementation philosophy when extending AIze.
