import json
from unittest.mock import MagicMock

import pytest
from conftest import PATCH, execution

from cloudvet.healer import Healer, MaaS, apply_locally, validate_patch


def models_with_patch(patch=PATCH):
    models = MagicMock()
    models.complete.side_effect = [
        json.dumps({"root_cause": "Subtraction instead of addition", "files": ["app.py"]}),
        patch,
    ]
    models.usage = []
    return models


def test_verified_patch_and_model_routing(settings, workspace, sandbox):
    manager = MagicMock()
    manager.run_command.side_effect = [
        execution(),
        execution(stdout=PATCH),
        execution(),
        execution(stdout=PATCH),
    ]
    models = models_with_patch()
    result = Healer(settings, manager, models).repair(sandbox, workspace, "pytest -q", execution(1))
    assert result.verified and result.diff == PATCH and result.attempts == 1
    assert models.complete.call_args_list[0].args[0] == settings.triage_model
    assert models.complete.call_args_list[1].args[0] == settings.fix_model
    assert b"a + b" not in workspace.files["app.py"]


def test_already_passing_does_not_invoke_model(settings, workspace, sandbox):
    models = MagicMock()
    result = Healer(settings, MagicMock(), models).repair(sandbox, workspace, "pytest", execution())
    assert result.verified and result.attempts == 0
    models.complete.assert_not_called()


def test_retries_bounded_and_no_unverified_diff(settings, workspace, sandbox):
    models = models_with_patch("not a patch")
    models.complete.side_effect = [
        json.dumps({"root_cause": "bad", "files": ["app.py"]}),
        "bad patch",
    ] * 3
    result = Healer(settings, MagicMock(), models).repair(
        sandbox, workspace, "pytest", execution(1)
    )
    assert not result.verified and result.attempts == 3 and result.diff == ""
    assert models.complete.call_count == 6


def test_failed_tests_never_return_verified_patch(settings, workspace, sandbox):
    models = models_with_patch()
    manager = MagicMock()
    manager.run_command.side_effect = [
        execution(),
        execution(stdout=PATCH),
        execution(1),
        execution(stdout=PATCH),
    ]
    result = Healer(settings, manager, models).repair(
        sandbox,
        workspace,
        "pytest",
        execution(1),
        max_attempts=1,
    )
    assert not result.verified and not result.diff


def test_test_suite_mutation_aborts_verification(settings, workspace, sandbox):
    manager = MagicMock()
    manager.run_command.side_effect = [
        execution(),
        execution(stdout=PATCH),
        execution(),
        execution(stdout=PATCH + "# altered"),
    ]
    with pytest.raises(RuntimeError, match="modified tracked source"):
        Healer(settings, manager, models_with_patch()).repair(
            sandbox, workspace, "pytest", execution(1)
        )


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "tests/test_app.py",
        ".github/workflows/test.py",
        "setup.py",
        "new.py",
        "test_app.py",
    ],
)
def test_path_and_test_file_restrictions(workspace, path):
    if not path.startswith(".."):
        workspace.files[path] = b"original"
    if path == "new.py":
        workspace.files.pop(path)
    with pytest.raises(ValueError):
        validate_patch(PATCH.replace("app.py", path), workspace)


def test_mode_change_and_scope_rejected(workspace):
    with pytest.raises(ValueError, match="modes"):
        validate_patch(PATCH.replace("--- a", "old mode 100644\nnew mode 100755\n--- a"), workspace)
    with pytest.raises(ValueError, match="scope"):
        validate_patch(PATCH, workspace, "other")


def test_hidden_gnu_diff_is_rejected(workspace):
    hidden = "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-fail\n+pass\n"
    with pytest.raises(ValueError, match="outside the validated"):
        validate_patch(PATCH + hidden, workspace)


def test_local_application_preserves_git_index(workspace):
    apply_locally(PATCH, workspace)
    assert "return a + b" in (workspace.root / "app.py").read_text()
    from cloudvet.sandbox import git

    assert git(workspace.root, "diff", "--cached") == b""


def test_local_patch_targets_project_subdirectory(repository):
    from cloudvet.sandbox import Workspace

    project = repository / "service"
    project.mkdir()
    original = (repository / "app.py").read_bytes()
    (project / "app.py").write_bytes(original)
    workspace = Workspace.capture(project)
    apply_locally(PATCH, workspace)
    assert "return a + b" in (project / "app.py").read_text()
    assert (repository / "app.py").read_bytes() == original


def test_local_edits_are_never_overwritten(workspace):
    (workspace.root / "app.py").write_text("# newer local edits\n")
    with pytest.raises(ValueError, match="Local edits"):
        apply_locally(PATCH, workspace)
    assert "newer local edits" in (workspace.root / "app.py").read_text()


def test_inference_records_usage_and_uses_gmi(settings):
    client = MagicMock()
    response = client.chat.completions.create.return_value
    response.choices = [MagicMock(finish_reason="stop", message=MagicMock(content="reply"))]
    response.usage.prompt_tokens, response.usage.completion_tokens = 10, 5
    models = MaaS(settings, client)
    assert models.complete(settings.triage_model, "system", "context") == "reply"
    assert models.usage[0].prompt_tokens == 10
    assert client.chat.completions.create.call_args.kwargs["model"] == settings.triage_model
