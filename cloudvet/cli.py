"""cloudvet command-line entrypoint."""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from agentbox_sdk.errors import APIError as AgentBoxAPIError

from cloudvet import __version__
from cloudvet.audit import audit_workspace, write_report
from cloudvet.config import Settings
from cloudvet.detector import detect_stack
from cloudvet.formatters import console, show_diff, show_execution, show_model_usage
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
        console.print(f"Error: {message}", style="red", markup=False)
        raise typer.Exit(2) from None


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False):
    if version:
        console.print(__version__)
        raise typer.Exit()


def snapshot(root: Path) -> Workspace:
    workspace = Workspace.capture(root)
    console.print(f"Snapshot: {len(workspace.files)} files; {len(workspace.excluded)} excluded.")
    return workspace


def prepare(manager, sandbox, workspace, stack, install):
    with console.status("Uploading workspace..."):
        manager.upload_workspace(sandbox, workspace)
    if install:
        with console.status("Installing dependencies in AgentBox..."):
            result = manager.run_command(sandbox, stack.install_command)
        if not result.passed:
            show_execution(result)
            console.print("Dependency setup failed. Tests and repairs did not run.")
            raise typer.Exit(2)


@app.command()
def check(
    root: Root = Path("."),
    install: Annotated[bool, typer.Option("--install/--no-install")] = True,
    json_output: Annotated[Path | None, typer.Option("--json-output")] = None,
):
    """Run the current working tree's tests in GMI Cloud. Local files stay unchanged."""
    with errors():
        settings, stack = Settings.from_env(), detect_stack(root)
        workspace = snapshot(root)
        manager = SandboxManager(settings)
        with manager.session() as sandbox:
            prepare(manager, sandbox, workspace, stack, install)
            with console.status(f"Running {stack.test_command}..."):
                result = manager.run_command(sandbox, stack.test_command)
        show_execution(result)
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
        settings, stack = Settings.from_env(), detect_stack(root)
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
        with manager.session() as sandbox:
            prepare(manager, sandbox, workspace, stack, install)
            with console.status("Running tests in AgentBox..."):
                initial = manager.run_command(sandbox, stack.test_command)
            show_execution(initial)
            if initial.passed:
                console.print("Tests already pass; no repair needed.")
                return
            baseline = manager.run_command(sandbox, REMOTE_DIFF)
            if not baseline.passed or baseline.stdout:
                raise ValueError("Setup or tests modified tracked files; cannot verify a repair")
            models = MaaS(settings)
            try:
                with console.status("Triaging, patching and verifying in AgentBox..."):
                    result = Healer(settings, manager, models).repair(
                        sandbox,
                        workspace,
                        stack.test_command,
                        initial,
                        scope=scope,
                    )
            finally:
                models.close()
        if json_output:
            json_output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        show_execution(result.execution)
        show_model_usage(result.usage)
        if not result.verified:
            for error in result.errors:
                console.print(error, markup=False)
            console.print("No verified repair. Local files were not changed.")
            raise typer.Exit(1)
        show_diff(result.diff)
        if yes or typer.confirm("Apply this verified diff locally?", default=True):
            apply_locally(result.diff, workspace, scope)
            console.print("Patch applied. Review the diff before committing.")
        else:
            console.print("Patch not applied.")


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
        output = output or root / ".cloudvet" / datetime.now(timezone.utc).strftime(
            "audit-%Y%m%d-%H%M%S-%f"
        )
        if output.exists():
            raise FileExistsError(f"Report directory already exists: {output}")
        workspace, manager = snapshot(root), SandboxManager(Settings.from_env())
        with manager.session() as sandbox:
            with console.status("Reviewing source in AgentBox..."):
                report = audit_workspace(manager, sandbox, workspace, full=full)
        paths = write_report(report, output)
        console.print(
            f"{len(report.needs_validation)} leads. Review incomplete; no exploitability claims."
        )
        for path in paths:
            console.print(str(path.resolve()), markup=False)
        raise typer.Exit(1 if report.needs_validation else 0)


@app.command()
def run(
    command: Annotated[str, typer.Argument(help="Bash command to run in AgentBox.")],
    root: Root = Path("."),
):
    """Execute an explicit command remotely; poll stdout and stderr until completion."""
    with errors():
        workspace, manager = snapshot(root), SandboxManager(Settings.from_env())
        with manager.session() as sandbox:
            manager.upload_workspace(sandbox, workspace)

            def stream(name: str, text: str):
                console.print(text, end="", markup=False, highlight=False)

            result = manager.run_command(sandbox, command, on_output=stream)
        console.print(f"\nExit {result.exit_code} in {result.duration_seconds:.2f}s")
        raise typer.Exit(result.exit_code if 0 <= result.exit_code <= 255 else 1)
