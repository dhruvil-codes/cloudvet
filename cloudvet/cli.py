"""cloudvet command-line entrypoint."""

import os
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from agentbox_sdk.errors import APIError as AgentBoxAPIError

from cloudvet import __version__
from cloudvet.audit import audit_workspace, write_report
from cloudvet.config import Settings
from cloudvet.detector import detect_stack
from cloudvet.formatters import (
    ERROR,
    Workflow,
    card,
    console,
    note,
    show_diff,
    show_execution,
    show_header,
    show_model_usage,
    stage,
)
from cloudvet.healer import REMOTE_DIFF, Healer, MaaS, apply_locally
from cloudvet.sandbox import SandboxManager, Workspace

app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="Verify tests and AI repairs in disposable GMI sandboxes.",
)
Root = Annotated[Path, typer.Option("--root", exists=True, file_okay=False, resolve_path=True)]


@contextmanager
def errors():
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception as exc:
        # SDK errors may contain request headers or URLs; only print details for local validation.
        message = (
            str(exc)
            if isinstance(exc, (ValueError, FileExistsError, TimeoutError))
            else (
                f"{type(exc).__name__}: operation failed; check GMI console, keys, image and quota"
            )
        )
        if isinstance(exc, AgentBoxAPIError):
            message = f"GMI compute HTTP {exc.status_code}: {exc}"
        for name in ("GMI_AGENTBOX_API_KEY", "GMI_MAAS_API_KEY", "GMI_API_KEY"):
            if os.environ.get(name):
                message = message.replace(os.environ[name], "[redacted]")
        console.print(card(message, "CLOUDVET VERDICT: BLOCKED", ERROR))
        raise typer.Exit(2) from None


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False):
    if version:
        console.print(__version__)
        raise typer.Exit()


def snapshot(root: Path) -> Workspace:
    with stage("Snapshot working tree"):
        workspace = Workspace.capture(root)
    note(f"  {len(workspace.files)} files bundled · {len(workspace.excluded)} excluded")
    return workspace


def prepare(manager, sandbox, workspace, stack, install):
    with stage("Upload workspace and establish baseline"):
        manager.upload_workspace(sandbox, workspace)
    if install:
        with stage(f"Prepare {stack.package_manager} dependencies"):
            result = manager.run_command(sandbox, stack.install_command)
        if not result.passed:
            show_execution(result)
            note("Dependency setup failed. Tests and repairs did not run.")
            raise typer.Exit(2)
    if stack.language == "Rust":
        with stage("Inspect Rust toolchain"):
            toolchain = manager.run_command(sandbox, "rustc --version")
        if toolchain.passed and toolchain.stdout.startswith("rustc "):
            return "Rust " + toolchain.stdout.split()[1][:40]
    return stack.language


@contextmanager
def cloud_session(manager: SandboxManager, workflow: Workflow):
    with ExitStack() as resources:
        with stage("Launch GMI AgentBox v2"):
            sandbox = resources.enter_context(manager.session())
        details = manager.launch_details.get(sandbox.id, {})
        if isinstance(details, dict):
            workflow.boot_seconds = details.get("boot_seconds")
            workflow.idc = details.get("idc", "")
        yield sandbox
        with stage("Delete sandbox and owned template"):
            resources.close()


@app.command()
def check(
    root: Root = Path("."),
    install: Annotated[bool, typer.Option("--install/--no-install")] = True,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
):
    """Run the current working tree's tests in GMI Cloud. Local files stay unchanged."""
    with errors():
        show_header()
        settings, stack = Settings.from_env(), detect_stack(root)
        workflow = Workflow(stack=stack.language, command=stack.test_command)
        workspace = snapshot(root)
        manager = SandboxManager(settings)
        with cloud_session(manager, workflow) as sandbox:
            workflow.stack = prepare(manager, sandbox, workspace, stack, install)
            with stage(f"Run {stack.test_command}"):
                result = manager.run_command(sandbox, stack.test_command)
        show_execution(result, stack=workflow.stack, boot_seconds=workflow.boot_seconds)
        workflow.summary("PASS" if result.passed else "FAIL", result)
        if json_output:
            json_output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        raise typer.Exit(0 if result.passed else 1)


@app.command()
def fix(
    path: Annotated[
        Path | None, typer.Argument(help="Limit repairs to this file or directory.")
    ] = None,
    root: Root = Path("."),
    install: Annotated[bool, typer.Option("--install/--no-install")] = True,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Apply verified patch without a prompt.")
    ] = False,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
):
    """Triage with DeepSeek, synthesize with Sonnet, verify tests, then offer a local patch."""
    with errors():
        show_header()
        settings, stack = Settings.from_env(), detect_stack(root)
        workflow = Workflow(stack=stack.language, command=stack.test_command)
        settings.require_inference()
        scope = ""
        if path:
            target = (root / path).resolve()
            scope = target.relative_to(root.resolve()).as_posix()
            if not target.exists():
                raise ValueError("Repair scope does not exist")
            if scope == ".":
                scope = ""
        workspace, manager = snapshot(root), SandboxManager(settings)
        with cloud_session(manager, workflow) as sandbox:
            workflow.stack = prepare(manager, sandbox, workspace, stack, install)
            with stage(f"Run {stack.test_command} · baseline"):
                initial = manager.run_command(sandbox, stack.test_command)
            show_execution(initial, stack=workflow.stack, boot_seconds=workflow.boot_seconds)
            if initial.passed:
                note("Tests already pass; no repair needed.")
            else:
                baseline = manager.run_command(sandbox, REMOTE_DIFF)
                if not baseline.passed or baseline.stdout:
                    raise ValueError(
                        "Setup or tests modified tracked files; cannot verify a repair"
                    )
                models = MaaS(settings)
                try:
                    with stage("Repair tests in AgentBox"):
                        result = Healer(settings, manager, models).repair(
                            sandbox,
                            workspace,
                            stack.test_command,
                            initial,
                            scope=scope,
                            on_stage=lambda label: note("    " + label),
                        )
                finally:
                    models.close()
        if initial.passed:
            workflow.summary("PASS", initial)
            if json_output:
                json_output.write_text(initial.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return
        if json_output:
            json_output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        show_execution(result.execution)
        show_model_usage(result.usage)
        if not result.verified:
            for error in result.errors:
                console.print(error, markup=False)
            note("No verified repair. Local files were not changed.")
            workflow.summary("FAIL", result.execution, result.usage, result.attempts)
            raise typer.Exit(1)
        show_diff(result.diff)
        workflow.summary(
            "PASS WITH VERIFIED REPAIR", result.execution, result.usage, result.attempts
        )
        if yes or typer.confirm("Apply this verified diff locally?", default=True):
            apply_locally(result.diff, workspace, scope)
            note("Patch applied. Review the diff before committing.")
        else:
            note("Patch not applied.")


@app.command()
def audit(
    root: Root = Path("."),
    full: Annotated[
        bool, typer.Option("--full/--quick", help="Add shell and TLS source checks.")
    ] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
):
    """Export a limited source security review. No target code or exploit probes execute."""
    with errors():
        show_header()
        workflow = Workflow(command="source review")
        output = output or root / ".cloudvet" / datetime.now(timezone.utc).strftime(
            "audit-%Y%m%d-%H%M%S-%f"
        )
        if output.exists():
            raise FileExistsError(f"Report directory already exists: {output}")
        workspace, manager = snapshot(root), SandboxManager(Settings.from_env())
        with cloud_session(manager, workflow) as sandbox:
            with stage("Review source in AgentBox"):
                report = audit_workspace(manager, sandbox, workspace, full=full)
        paths = write_report(report, output)
        workflow.summary(
            "REVIEW INCOMPLETE",
            extra=[
                ("Leads", str(len(report.needs_validation))),
                ("Validation", "Source review only; no exploitability claims"),
            ],
        )
        for path in paths:
            note(str(path.resolve()))
        raise typer.Exit(1 if report.needs_validation else 0)


@app.command()
def run(
    command: Annotated[str, typer.Argument(help="Bash command to run in AgentBox.")],
    root: Root = Path("."),
):
    """Execute an explicit command remotely; poll stdout and stderr until completion."""
    with errors():
        show_header()
        workflow = Workflow(command=command)
        workspace, manager = snapshot(root), SandboxManager(Settings.from_env())
        with cloud_session(manager, workflow) as sandbox:
            manager.upload_workspace(sandbox, workspace)

            def stream(name: str, text: str):
                console.print(text, end="", markup=False, highlight=False)

            result = manager.run_command(sandbox, command, on_output=stream)
        workflow.summary("PASS" if result.passed else "FAIL", result)
        raise typer.Exit(result.exit_code if 0 <= result.exit_code <= 255 else 1)
