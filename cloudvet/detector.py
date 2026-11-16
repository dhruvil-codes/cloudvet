"""Detect test commands without executing repository code."""

import json
from pathlib import Path

from pydantic import BaseModel


class Stack(BaseModel):
    language: str
    package_manager: str
    test_command: str
    install_command: str


def detect_stack(root: Path) -> Stack:
    root = Path(root)
    if any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        install = "python -m pip install pytest"
        if (root / "requirements.txt").is_file():
            install += " && python -m pip install -r requirements.txt"
        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
            install += " && python -m pip install -e ."
        return Stack(
            language="Python",
            package_manager="pip",
            test_command="pytest -q",
            install_command=install,
        )
    if (root / "package.json").is_file():
        package = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
        if not isinstance(package, dict):
            raise ValueError("package.json must contain a JSON object")
        declared = str(package.get("packageManager", "")).split("@", 1)[0]
        if declared == "pnpm" or (root / "pnpm-lock.yaml").is_file():
            manager, install = "pnpm", "corepack enable && pnpm install --frozen-lockfile"
        elif declared == "yarn" or (root / "yarn.lock").is_file():
            manager, install = "yarn", "corepack enable && yarn install --immutable"
        else:
            manager = "npm"
            install = "npm ci" if (root / "package-lock.json").is_file() else "npm install"
        scripts = package.get("scripts", {})
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if isinstance(scripts, dict) and scripts.get("test"):
            command = f"{manager} test"
        elif "vitest" in dependencies:
            command = (
                "npx --no-install vitest run" if manager == "npm" else f"{manager} exec vitest run"
            )
        else:
            raise ValueError("No test script or installed Vitest declared in package.json")
        return Stack(
            language="Node", package_manager=manager, test_command=command, install_command=install
        )
    if (root / "Cargo.toml").is_file():
        return Stack(
            language="Rust",
            package_manager="cargo",
            test_command="cargo test --quiet",
            install_command="cargo fetch",
        )
    if (root / "go.mod").is_file():
        return Stack(
            language="Go",
            package_manager="go",
            test_command="go test ./...",
            install_command="go mod download",
        )
    raise ValueError("No supported test stack detected; use cloudvet run for a custom command")
