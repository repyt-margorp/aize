# AIze MINIX Unit Model

AIze names runtime-managed components with OS-style terms. The system follows a MINIX-like split: a small kernel routes messages and enforces capabilities, while userland units provide sessions, managers, agents, interfaces, storage, and peer gateways.

## Vocabulary

- `Unit`: any AIze kernel-managed execution target.
- `UnitFile`: a declarative unit definition discovered from a unit package.
- `Server`: a long-lived userland unit that owns a responsibility and receives messages.
- `Process`: a running instance of a unit.
- `Endpoint`: a message-addressable port exposed by a unit.
- `Message`: the only primitive communication form between units.
- `SessionUnit`: a unit that owns goal, history, participants, and execution state.
- `ServiceUnit`: a singleton or daemon-like unit.
- `ManagerUnit`: a control unit such as GoalManager.
- `AgentUnit`: an agent unit that joins a SessionUnit.
- `InterfaceUnit`: a UI, CLI, WS, or HTTP entrypoint.
- `DeviceUnit`: an adapter for an external node, provider, filesystem, browser, or peer link.

## Compatibility

The old terms remain as compatibility inputs only:

- `plugin` means a unit package directory under `./plugins/`.
- `session template` means a legacy UnitFile descriptor.
- `app` means an older launcher record for a UnitFile.

New UI and API surfaces should prefer `Unit`, `UnitFile`, and `Interface`.

## Descriptor Direction

New descriptors should use `units/<name>/unit.json`:

```json
{
  "unit_id": "entrance.service",
  "unit_kind": "interface",
  "unit_class": "service",
  "instance_policy": "singleton",
  "lifecycle": "on_demand",
  "restart_policy": "on_failure",
  "interfaces": {
    "web": "/units/entrance"
  },
  "endpoints": {
    "ipc": "unit://entrance.service/inbox"
  },
  "launcher": {
    "goal_text": "Route user feedback to the appropriate SessionUnit."
  }
}
```

Legacy `session-templates/*/session-template.json` and `apps/*/app.json` files are still accepted while the implementation is migrated.
