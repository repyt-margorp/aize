# Entrance Development Routing Lineage

- User-visible behavior changed: Entrance's canonical development routing descriptor no longer scopes parent lookup to the current Entrance session origin. This preserves routing to the registered canonical `aize-development.bug-hunting` parent even from compatibility or secondary Entrance sessions.
- Files touched: `plugins/aize-entrance/units/entrance/unit.json`.
- Verification run: inspected live persisted session state for Entrance routing, canonical AIze Development parent `repyt/0ac1231110d2881f`, recent child sessions, and stale compatibility parent usage; validated edited JSON with `python3 -m json.tool`.
- Remaining risk: active persisted Entrance session state already had the working blank scope, so this source change affects future launches/reloads rather than mutating the current live session.
