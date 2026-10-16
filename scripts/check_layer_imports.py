#!/usr/bin/env python3
"""Layer-boundary audit: static import-direction conformance for the AIze runtime."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

LAYER_OF_PACKAGE = {"kernel": "kernel", "goal": "goal", "cli": "cli"}
LAYER_ORDER = {"kernel": 0, "goal": 1, "cli": 2}
ALLOWED_IMPORTS = {
    "kernel": {"kernel"},
    "goal": {"goal", "kernel"},
    "cli": {"cli", "goal", "kernel"},
}
SURFACE_ALLOWED = ALLOWED_IMPORTS["cli"]

KERNEL_FORBIDDEN_TOKENS = re.compile(
    r"\bGoalManager\b|\bWorkerAgent\b|\bgoal\w*\b|completion_state|\bAGENT_ROLES\b|agent_endpoint"
)


def module_layer(module: str) -> str | None:
    package = module.split(".", 1)[0]
    return LAYER_OF_PACKAGE.get(package)


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    modules: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and module_layer(node.module):
                modules.append((node.module, node.lineno))
            elif node.level > 0:
                raise SystemExit(f"relative imports are not used in this tree: {node.lineno}")
    return modules


def audit_layered_source(path: Path, layer: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for module, line in imported_modules(tree):
        target = module_layer(module)
        if target is None:
            continue
        if target not in ALLOWED_IMPORTS[layer]:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{line}: {layer} must not import {module} ({target} layer)")
    return violations


def audit_surface_module(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for module, line in imported_modules(tree):
        target = module_layer(module)
        if target is not None and target not in SURFACE_ALLOWED:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{line}: surface module must not import {module} ({target} layer)")
    return violations


def audit_kernel_vocabulary() -> list[str]:
    violations: list[str] = []
    for path in sorted((SRC / "kernel").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if KERNEL_FORBIDDEN_TOKENS.search(line):
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{number}: goal vocabulary inside kernel: {line.strip()}")
    return violations


def main() -> int:
    violations: list[str] = []
    packages = sorted(
        path for path in SRC.iterdir() if path.is_dir() and path.name in LAYER_OF_PACKAGE
    )
    for package in packages:
        layer = LAYER_OF_PACKAGE[package.name]
        for path in sorted(package.rglob("*.py")):
            violations.extend(audit_layered_source(path, layer))
    for path in sorted(SRC.glob("*.py")):
        violations.extend(audit_surface_module(path))
    violations.extend(audit_kernel_vocabulary())

    if violations:
        print("layer-boundary audit failed:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("layer-boundary audit ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
