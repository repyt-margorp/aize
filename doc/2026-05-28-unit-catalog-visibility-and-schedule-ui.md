## Behavior Changed

The Unit screen now has an explicit visibility toggle for `All Units` versus
`Public Only`. The authenticated default remains all Units so local private Units
such as AIze Development are visible, while the public-only view can still be used
to inspect what the public catalog contains.

Non-singleton Units now expose an interval schedule editor in the Unit detail panel.
Users can enable or disable scheduling, set an `every_hours` cadence, and see the
next scheduled run reported from the same schedule state used by the scheduler.
Singleton Units show a read-only note because their purpose is canonical session
existence/reuse rather than repeated interval launches.

AIze System Diagnostics now defaults to a 24-hour interval instead of hourly.

## Files Touched

- `src/runtime/http_handler.py`
- `src/runtime/html_renderer.py`
- `tests/test_unit_catalog.py`
- `unit_packages/aize-development/units/system-diagnostics/unit.json`

## Verification

- `PYTHONPATH=./src python3 -m py_compile src/runtime/http_handler.py src/runtime/html_renderer.py`
- `PYTHONPATH=./src python3 -m unittest tests.test_unit_catalog -q`
- Live `https://127.0.0.1:64123/units?include_private=0` returned only `entrance.service`.
- Live `https://127.0.0.1:64123/units?include_private=1` returned AIze Development, AIze MINIX Refactor, diagnostics/monitor Units, and Entrance.
- Live `POST /units/schedule` saved a 24-hour interval for AIze System Diagnostics and rejected the singleton AIze Development Unit schedule edit.
- Headless Chrome rendered the authenticated page and captured `.temp/unit-catalog-page.png`; the DOM included the Unit visibility buttons, schedule editor container, and `/units/schedule` client call.

## Remaining Risk

The schedule editor currently supports interval schedules in hours. Daily-at-time
schedules still need a separate UI if they become a required user-facing mode.
