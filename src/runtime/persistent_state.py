"""Backward-compatible persistent_state module facade.

The runtime now stores session persistence logic under the package
`runtime.persistent_state_pkg` to avoid historical import collisions with a file
and package sharing the same namespace.
"""

from __future__ import annotations

from .persistent_state_pkg import *  # noqa: F401,F403
