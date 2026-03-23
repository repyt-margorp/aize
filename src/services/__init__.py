"""
Service plugin loader for Aize.

A service plugin is a Python package under src/services/{kind}/ that exports
a run_service(**kwargs) -> int function. New service types can be added by
creating a new subdirectory with __init__.py.

Example layout:
  src/services/
    myservice/
      __init__.py   # must export run_service(...)
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Callable, Any


def load_service_handler(kind: str) -> Callable[..., int]:
    """Load and return the run_service function for the given service kind.

    Raises ValueError if the kind is unknown or the module lacks run_service.
    """
    module_name = f"services.{kind}"
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
    pkg_dir = Path(__file__).parent
    return sorted(
        p.name
        for p in pkg_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "__init__.py").exists()
    )
