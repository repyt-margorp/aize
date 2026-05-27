from __future__ import annotations

import inspect

from runtime import cli_service_adapter


def test_http_service_binds_before_startup_state_reconcile() -> None:
    source = inspect.getsource(cli_service_adapter.run_http_service)

    server_start = source.index("server_thread.start()")
    reconcile_start = source.index("threading.Thread(target=_run_startup_state_reconcile")

    assert server_start < reconcile_start
    assert "\n    ensure_state(runtime_root)\n" not in source
    assert "\n    for reconciled in reconcile_session_waiting_on_children(runtime_root):\n" not in source
    assert "\n    release_stale_session_bindings()\n\n    def enqueue_service_control" not in source
