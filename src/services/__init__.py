"""
Service plugin loader for Aize.

A service plugin is a Python module that exports a run_service(**kwargs) -> int
function. Builtins live under src/services/{kind}/ and repo-local plugin
services can live under ./plugins/**/services/{kind}/.

Example layout:
  src/services/
    myservice/
      __init__.py   # must export run_service(...)
"""
from __future__ import annotations

import importlib
from typing import Callable, Any

from services.svcmgr.loader import get_service_descriptor, list_service_descriptors


def load_service_handler(kind: str) -> Callable[..., int]:
    """Load and return the run_service function for the given service kind.

    Raises ValueError if the kind is unknown or the module lacks run_service.
    """
    descriptor = get_service_descriptor(kind)
    module_name = str(descriptor.get("module") or f"services.{kind}")
    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"Unknown service kind {kind!r}: {exc}") from exc
    run_service = getattr(mod, "run_service", None)
    if not callable(run_service):
        raise ValueError(f"services.{kind} must export a callable run_service")
    return run_service


def list_available_kinds() -> list[str]:
    """Return a list of service kinds available in the services package."""
    return sorted(str(descriptor.get("kind", "")).strip() for descriptor in list_service_descriptors(exclude_kinds=set()))
