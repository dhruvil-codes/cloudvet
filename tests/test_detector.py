import json

import pytest

from cloudvet.detector import detect_stack


@pytest.mark.parametrize(
    ("manifest", "language", "command"),
    [
        ("pyproject.toml", "Python", "pytest -q"),
        ("requirements.txt", "Python", "pytest -q"),
        ("setup.py", "Python", "pytest -q"),
        ("Cargo.toml", "Rust", "cargo test --quiet"),
        ("go.mod", "Go", "go test ./..."),
    ],
)
def test_detects_languages(tmp_path, manifest, language, command):
    (tmp_path / manifest).touch()
    stack = detect_stack(tmp_path)
    assert (stack.language, stack.test_command) == (language, command)


@pytest.mark.parametrize(
    ("lock", "manager"),
    [
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
    ],
)
def test_node_managers(tmp_path, lock, manager):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    (tmp_path / lock).touch()
    assert detect_stack(tmp_path).test_command == f"{manager} test"


def test_vitest_without_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies":{"vitest":"^3"}}')
    assert detect_stack(tmp_path).test_command == "npx --no-install vitest run"


def test_declared_pnpm_takes_precedence(tmp_path):
    (tmp_path / "package.json").write_text('{"packageManager":"pnpm@10","scripts":{"test":"test"}}')
    (tmp_path / "package-lock.json").touch()
    assert detect_stack(tmp_path).package_manager == "pnpm"


@pytest.mark.parametrize("content", ["[]", "bad json", "{}"])
def test_invalid_or_missing_node_tests(tmp_path, content):
    (tmp_path / "package.json").write_text(content)
    with pytest.raises(ValueError):
        detect_stack(tmp_path)


def test_missing_stack(tmp_path):
    with pytest.raises(ValueError, match="No supported"):
        detect_stack(tmp_path)
