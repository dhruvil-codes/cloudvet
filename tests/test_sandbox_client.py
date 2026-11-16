import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest
from agentbox_sdk import Execution

from cloudvet.sandbox import SandboxManager, Workspace, git, safe_workspace_path


def configured_client(sandbox):
    client = MagicMock()
    client.agents.create.return_value.template_build_status = "ready"
    client.agents.create.return_value.launch.return_value = sandbox
    return client


def test_lifecycle_sets_server_ttl_and_cleans_up(settings, sandbox):
    client = configured_client(sandbox)
    manager = SandboxManager(settings, client)
    with manager.session() as box:
        assert box is sandbox
        client.agents.create.return_value.launch.assert_called_once()
        assert client.agents.create.return_value.launch.call_args.kwargs["timeout_seconds"] == 300
        assert manager.launch_details[box.id]["boot_seconds"] >= 0
    sandbox.delete.assert_called_once()
    client.agents.create.return_value.delete.assert_called_once()


def test_reused_template_not_deleted(settings, sandbox):
    settings.agent_slug = "prepared"
    client = MagicMock()
    client.agents.get.return_value.launch.return_value = sandbox
    with SandboxManager(settings, client).session():
        pass
    client.agents.create.assert_not_called()
    client.agents.get.return_value.delete.assert_not_called()


def test_wait_failure_deletes_resources(settings, sandbox):
    sandbox.wait_until_running.side_effect = TimeoutError("not ready")
    client = configured_client(sandbox)
    with pytest.raises(TimeoutError):
        SandboxManager(settings, client).launch_ephemeral_sandbox()
    sandbox.delete.assert_called_once()
    client.agents.create.return_value.delete.assert_called_once()


def test_template_failure_deletes_template(settings, sandbox):
    client = configured_client(sandbox)
    client.agents.create.return_value.template_build_status = "error"
    with pytest.raises(RuntimeError, match="image build"):
        SandboxManager(settings, client).launch_ephemeral_sandbox()
    client.agents.create.return_value.delete.assert_called_once()
    client.agents.create.return_value.launch.assert_not_called()


def test_cleanup_retries(settings, sandbox):
    sandbox.delete.side_effect = [OSError("transient"), None]
    SandboxManager(settings, MagicMock()).cleanup(sandbox)
    assert sandbox.delete.call_count == 2


def test_cleanup_does_not_mask_original_exception(settings, sandbox):
    sandbox.delete.side_effect = OSError("offline")
    manager = SandboxManager(settings, configured_client(sandbox))
    with pytest.warns(RuntimeWarning, match="TTL remains active"):
        with pytest.raises(ValueError, match="original"):
            with manager.session():
                raise ValueError("original")
    assert sandbox.delete.call_count == 3


def test_polls_real_execution_not_http_acceptance(settings, sandbox):
    sandbox.get_execution.side_effect = [
        Execution({"id": "execution-test", "status": "running", "stdout": "first\n"}),
        Execution({"id": "execution-test", "exit_code": 7, "stdout": "first\nsecond\n"}),
    ]
    output = []
    with patch("cloudvet.sandbox.time.sleep"):
        result = SandboxManager(settings, MagicMock()).run_command(
            sandbox,
            "echo 'hello'; exit 7",
            on_output=lambda stream, data: output.append(data),
        )
    assert result.exit_code == 7
    assert "".join(output) == "first\nsecond\n"
    sandbox.execute.assert_called_once()
    assert sandbox.execute.call_args.kwargs == {"wait": False}
    assert "timeout --kill-after=5s" in sandbox.execute.call_args.args[0]


def test_timeout_cancels_execution(settings, sandbox):
    settings.command_timeout = 1
    sandbox.get_execution.return_value = Execution({"status": "running"})
    with patch("cloudvet.sandbox.time.monotonic", side_effect=[0, 0, 2, 2]):
        result = SandboxManager(settings, MagicMock()).run_command(sandbox, "sleep 100")
    assert result.timed_out and result.exit_code == 124
    sandbox.cancel_execution.assert_called_once_with("execution-test")


def test_missing_execution_id_is_error(settings, sandbox):
    sandbox.execute.return_value = Execution({})
    with pytest.raises(RuntimeError, match="no execution ID"):
        SandboxManager(settings, MagicMock()).run_command(sandbox, "true")


def test_upload_snapshot_filters_ignored_and_tracked_secrets(repository):
    (repository / ".gitignore").write_text("ignored.txt\nnested/private.txt\n")
    (repository / "ignored.txt").write_text("private")
    (repository / ".env").write_text("SECRET=private")
    (repository / "key.pem").write_text("private")
    (repository / "new.py").write_text("# new file")
    (repository / "nested").mkdir()
    (repository / "nested/private.txt").write_text("private")
    git(repository, "add", "-f", ".env", "key.pem")
    workspace = Workspace.capture(repository)
    assert {"new.py", "app.py"} <= workspace.files.keys()
    assert not {".env", "key.pem", "ignored.txt", "nested/private.txt"} & workspace.files.keys()
    assert "SECRET" not in workspace.changed_diff
    with tarfile.open(fileobj=io.BytesIO(workspace.archive())) as archive:
        assert archive.getnames() == list(workspace.files)
        assert all(member.isfile() for member in archive.getmembers())


def test_changes_include_staged_and_unstaged(repository):
    (repository / "app.py").write_text("# staged\n")
    git(repository, "add", "app.py")
    (repository / "requirements.txt").write_text("pytest>=8\n")
    diff = Workspace.capture(repository).changed_diff
    assert "+# staged" in diff and "+pytest>=8" in diff


def test_workspace_limit(repository, monkeypatch):
    monkeypatch.setattr("cloudvet.sandbox.MAX_WORKSPACE_BYTES", 1)
    with pytest.raises(ValueError, match="upload limit"):
        Workspace.capture(repository)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/root",
        "C:/file",
        "a\\b",
        ".git/config",
        ".env.local",
        "nested/.aws/config",
        "file\nname",
        "a.key",
    ],
)
def test_unsafe_workspace_paths(name):
    assert not safe_workspace_path(name)


def test_upload_and_baseline(settings, sandbox, workspace):
    manager = SandboxManager(settings, MagicMock())
    manager.upload_workspace(sandbox, workspace)
    assert sandbox.upload_file.call_args.kwargs["path"].startswith("/tmp/cloudvet-")
    command = sandbox.execute.call_args.args[0]
    assert "commit -qm baseline" in command and "core.hooksPath=/dev/null" in command
