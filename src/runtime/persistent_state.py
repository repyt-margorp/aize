"""Backward-compatible ``runtime.persistent_state`` facade.

Keep this module as an explicit alias to ``runtime.persistent_state_pkg`` so
legacy ``from runtime.persistent_state import ...`` imports continue to work
even as new public helpers are added to the package.
"""

from __future__ import annotations

from . import persistent_state_pkg as _persistent_state_pkg
from .persistent_state_pkg import *  # noqa: F401,F403

# Mirror the package's public surface explicitly so direct named imports resolve
# through this compatibility module without depending on implicit star-import
# state.
__all__ = list(_persistent_state_pkg.__all__)
globals().update({name: getattr(_persistent_state_pkg, name) for name in __all__})
