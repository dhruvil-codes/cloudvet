from unittest.mock import MagicMock

import pytest
from agentbox_sdk import Execution

from cloudvet.config import Settings
from cloudvet.sandbox import ExecutionResult, Workspace, git


@pytest.fixture
def settings():
    return Settings(agentbox_api_key="compute-test", maas_api_key="model-test")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "-c", "init.templateDir=", "init", "-q")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )
    return tmp_path


@pytest.fixture
def workspace(repository):
    return Workspace.capture(repository)


@pytest.fixture
def sandbox():
    box = MagicMock()
    box.id = "sandbox-test"
    box.execute.return_value = Execution({"execution_id": "execution-test"}, accepted=True)
    box.get_execution.return_value = Execution({"exit_code": 0, "stdout": "ok\n", "stderr": ""})
    return box


def execution(exit_code=0, stdout="", stderr="", timed_out=False):
    return ExecutionResult(
        command="pytest -q",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        timed_out=timed_out,
    )


PATCH = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)
