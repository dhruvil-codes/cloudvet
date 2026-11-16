from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from conftest import PATCH, execution
from typer.testing import CliRunner

from cloudvet.cli import app, cloud_session
from cloudvet.config import Settings
from cloudvet.formatters import Workflow
from cloudvet.healer import ModelUsage, RepairResult
from cloudvet.sandbox import SandboxManager

runner = CliRunner()


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert "audit" in runner.invoke(app, ["--help"]).stdout
    assert "0.1.0" in runner.invoke(app, ["--version"]).stdout


def test_missing_credentials_fail_without_traceback(repository, monkeypatch):
    monkeypatch.delenv("GMI_AGENTBOX_API_KEY", raising=False)
    result = runner.invoke(app, ["check", "--root", str(repository)])
    assert result.exit_code == 2 and "GMI_AGENTBOX_API_KEY" in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("exit_code", [0, 1])
def test_check_verdict_and_cleanup(repository, monkeypatch, tmp_path, exit_code):
    monkeypatch.setenv("GMI_AGENTBOX_API_KEY", "test-key")
    manager = MagicMock()
    manager.run_command.return_value = execution(exit_code)
    monkeypatch.setattr("cloudvet.cli.SandboxManager", lambda settings: manager)
    output = tmp_path / "verdict.json"
    result = runner.invoke(
        app, ["check", "--no-install", "--root", str(repository), "--json-output", str(output)]
    )
    assert result.exit_code == exit_code, result.stdout
    assert '"exit_code"' in output.read_text()
    manager.session.return_value.__exit__.assert_called_once()


def test_environment_fallbacks_and_redaction(monkeypatch):
    monkeypatch.delenv("GMI_MAAS_API_KEY", raising=False)
    monkeypatch.setenv("GMI_API_KEY", "secret-model-key")
    settings = Settings.from_env()
    assert settings.maas_api_key.get_secret_value() == "secret-model-key"
    assert "secret-model-key" not in repr(settings)
    assert settings.maas_base_url == "https://api.gmi-serving.com/v1"


def test_insecure_inference_url_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(maas_base_url="http://insecure/v1")


@pytest.mark.parametrize("answer,applied", [("n\n", False), ("y\n", True)])
def test_fix_prompt_and_local_consent(repository, monkeypatch, answer, applied):
    monkeypatch.setenv("GMI_AGENTBOX_API_KEY", "test-key")
    monkeypatch.setenv("GMI_MAAS_API_KEY", "test-model-key")
    manager, models, healer = MagicMock(), MagicMock(), MagicMock()
    manager.run_command.side_effect = [execution(1), execution()]
    healer.repair.return_value = RepairResult(
        verified=True,
        diff=PATCH,
        execution=execution(),
        confidence=1,
        attempts=1,
        usage=[ModelUsage(model="fixture-model", prompt_tokens=123, completion_tokens=456)],
    )
    monkeypatch.setattr("cloudvet.cli.SandboxManager", lambda settings: manager)
    monkeypatch.setattr("cloudvet.cli.MaaS", lambda settings: models)
    monkeypatch.setattr("cloudvet.cli.Healer", lambda *args: healer)
    result = runner.invoke(app, ["fix", "--root", str(repository), "--no-install"], input=answer)
    assert result.exit_code == 0, result.stdout
    assert "fixture-model" in result.stdout and "123" in result.stdout and "456" in result.stdout
    assert "FAIL" in result.stdout and "PASS" in result.stdout
    assert ("return a + b" in (repository / "app.py").read_text()) is applied
    models.close.assert_called_once()
    manager.session.return_value.__exit__.assert_called_once()


def test_setup_failure_never_invokes_repair(repository, monkeypatch):
    monkeypatch.setenv("GMI_AGENTBOX_API_KEY", "test-key")
    monkeypatch.setenv("GMI_MAAS_API_KEY", "test-model-key")
    manager, models = MagicMock(), MagicMock()
    manager.run_command.return_value = execution(1, stderr="install failed")
    monkeypatch.setattr("cloudvet.cli.SandboxManager", lambda settings: manager)
    monkeypatch.setattr("cloudvet.cli.MaaS", models)
    result = runner.invoke(app, ["fix", "--root", str(repository)])
    assert result.exit_code == 2 and "Dependency setup failed" in result.stdout
    models.assert_not_called()


def test_fix_rejects_initial_source_mutation(repository, monkeypatch):
    monkeypatch.setenv("GMI_AGENTBOX_API_KEY", "test-key")
    monkeypatch.setenv("GMI_MAAS_API_KEY", "test-model-key")
    manager = MagicMock()
    manager.run_command.side_effect = [execution(1), execution(stdout=PATCH)]
    monkeypatch.setattr("cloudvet.cli.SandboxManager", lambda settings: manager)
    result = runner.invoke(app, ["fix", "--root", str(repository), "--no-install"])
    assert result.exit_code == 2 and "modified tracked files" in result.stdout


def test_run_propagates_exit_code(repository, monkeypatch):
    monkeypatch.setenv("GMI_AGENTBOX_API_KEY", "test-key")
    manager = MagicMock()
    manager.run_command.return_value = execution(7)
    monkeypatch.setattr("cloudvet.cli.SandboxManager", lambda settings: manager)
    result = runner.invoke(app, ["run", "exit 7", "--root", str(repository)])
    assert result.exit_code == 7


def test_progress_output_failure_still_deletes_compute(settings, sandbox, monkeypatch):
    client = MagicMock()
    client.agents.create.return_value.template_build_status = "ready"
    client.agents.create.return_value.launch.return_value = sandbox
    manager = SandboxManager(settings, client)

    @contextmanager
    def disconnected_output(label):
        yield
        if label == "Launch GMI AgentBox v2":
            raise BrokenPipeError("terminal output closed")

    monkeypatch.setattr("cloudvet.cli.stage", disconnected_output)
    with pytest.raises(BrokenPipeError):
        with cloud_session(manager, Workflow()):
            pytest.fail("Progress output should fail before entering the workflow")
    sandbox.delete.assert_called_once()
    client.agents.create.return_value.delete.assert_called_once()
