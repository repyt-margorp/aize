# Dispatch storage and policy boundary

AIze treats Dispatch persistence and Dispatch decisions as separate concerns.
This refactor preserves the current behavior while making the policy replaceable.

## Layers

1. Session facts

   `store/sessions/<session>/log-*.jsonl` and the role cursors are the durable
   source of truth. User posts, role reports, Goal transitions, and system
   signals are recorded here.

2. Dispatch projection

   `dispatch_projection.py` interprets the unconsumed SessionLog window for one
   role and returns a `RoleDispatchReadiness`. It does not read or write files.

3. Dispatch policy

   `dispatch_policy.py` filters available proposals and selects one by priority,
   queue time, and stable insertion order. It does not read or write files.

4. Store adapter

   `store_dispatch_readiness.py` loads SessionLog data, calls the projection and
   policy functions, and persists request lifecycle changes. The current
   derived runtime index is `store/runtime/dispatch_readiness.json`.

## Authority

`dispatch_readiness.json` is a rebuildable runtime index, not Session history.
An acquired request is linked to a DispatchRun, but the reason a role needs
attention comes from SessionLog plus its role cursor.

This boundary allows later changes to multi-Worker cursors, backoff, fairness,
aging, and priority rules without changing the SessionLog file format or the
atomic storage implementation.
