# UI Browser Verification Requirement

- Added an explicit AIze Development rule that UI work is not complete when only the code has changed.
- Required browser-based verification for UI fixes so the changed screen is checked in rendered form and confirmed visible to a human.
- Applied the same rule in both the canonical AIze Development parent guidance and the Entrance-spawned child-session guidance so routed implementation sessions inherit it consistently.

Files touched:
- `plugins/aize-development/units/bug-hunting/unit.json`
- `plugins/aize-entrance/units/entrance/unit.json`

Verification:
- Reviewed the effective skill text in both unit definitions after the edit to confirm the new requirement appears in `usage`, `prompt`, and the embedded AIze Development guidance files.

Residual risk:
- This updates the written operating contract only. Existing sessions already in memory may still be following older guidance until they are restarted or re-materialized from the updated unit definitions.
