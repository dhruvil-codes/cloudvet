"""AgentBox lifecycle, bounded execution and secret-filtered workspace snapshots."""

import io
import os
import shlex
import stat
import subprocess
import tarfile
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from agentbox_sdk import AgentBoxClient
from pydantic import BaseModel

from cloudvet.config import Settings

MAX_WORKSPACE_BYTES = 50 * 1024 * 1024
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "__pycache__",
    ".worktrees",
    ".cloudvet",
    ".aws",
    ".ssh",
    ".azure",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
}


def safe_workspace_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        return False
    if not path.parts or any(p in ("..", ".") or p.lower() in EXCLUDED_PARTS for p in path.parts):
        return False
    lower = path.name.lower()
    return not (
        lower.startswith(".env")
        and lower != ".env.example"
        or lower.endswith((".pem", ".key", ".p12", ".pfx"))
        or lower in {"credentials", "credentials.json", "id_rsa", "id_ed25519"}
    )


def git(
    root: Path, *args: str, input_bytes: bytes | None = None, discover_parent: bool = True
) -> bytes:
    environment = os.environ.copy()
    if not discover_parent:
        environment["GIT_CEILING_DIRECTORIES"] = str(root.resolve().parent)
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        input=input_bytes,
        capture_output=True,
        timeout=30,
        check=False,
        env=environment,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip() or "Git failed")
    return result.stdout


@dataclass
class Workspace:
    root: Path
    files: dict[str, bytes]
    modes: dict[str, int]
    changed_diff: str
    excluded: list[str]

    @classmethod
    def capture(cls, root: Path) -> "Workspace":
        root = Path(root).resolve()
        # Git also applies nested .gitignore rules and global excludes. Never upload .git itself.
        listing = git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."
        )
        files, modes, excluded = {}, {}, []
        total = 0
        for raw in sorted(set(listing.split(b"\0")) - {b""}):
            name = os.fsdecode(raw)
            path = root / name
            if (
                not safe_workspace_path(name)
                or not path.resolve().is_relative_to(root)
                or any(p.is_symlink() for p in (path, *path.parents))
            ):
                excluded.append(name)
                continue
            if not path.exists():
                continue
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                excluded.append(name)
                continue
            total += metadata.st_size
            if total > MAX_WORKSPACE_BYTES:
                raise ValueError("Workspace exceeds the 50 MiB upload limit; update .gitignore")
            content = path.read_bytes()
            if len(content) != metadata.st_size:
                raise ValueError(f"Workspace changed while reading {name}; retry")
            files[name] = content
            modes[name] = 0o755 if metadata.st_mode & stat.S_IXUSR else 0o644
        # Restricted pathspec prevents excluded credentials being sent in a git diff.
        diff = ""
        if files and cls._has_head(root):
            changed = git(
                root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD"
            )
            changed_names = [os.fsdecode(p) for p in changed.split(b"\0") if p]
            # Bound argument length on Windows even for large repositories.
            chunks = []
            selected = [p for p in changed_names if p in files]
            for offset in range(0, len(selected), 20):
                chunks.append(
                    git(
                        root,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "HEAD",
                        "--",
                        *selected[offset : offset + 20],
                    )
                )
            diff = b"".join(chunks).decode("utf-8", errors="replace")
        return cls(root, files, modes, diff, excluded)

    @staticmethod
    def _has_head(root: Path) -> bool:
        try:
            git(root, "rev-parse", "--verify", "HEAD")
            return True
        except ValueError:
            return False

    def archive(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in self.files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), self.modes[name]
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()


class ExecutionResult(BaseModel):
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxManager:
    def __init__(self, settings: Settings, client: Any = None):
        settings.require_compute()
        self.settings = settings
        self.client = client or AgentBoxClient(
            api_key=settings.agentbox_api_key.get_secret_value(), timeout=30
        )
        self._owned_agents: dict[str, Any] = {}
        self._deadlines: dict[str, float] = {}
        self.launch_details: dict[str, dict] = {}

    def launch_ephemeral_sandbox(self, timeout_seconds: int | None = None) -> Any:
        lifetime = timeout_seconds if timeout_seconds is not None else self.settings.timeout_seconds
        if not 60 <= lifetime <= 3600:
            raise ValueError("Sandbox lifetime must be between 60 and 3600 seconds")
        owned = not self.settings.agent_slug
        agent, sandbox = None, None
        try:
            if owned:
                agent = self.client.agents.create(
                    title=f"cloudvet-{uuid4().hex[:12]}",
                    image_url=self.settings.image,
                    idc=self.settings.default_idc,
                    instance_type=self.settings.instance_type,
                    runtime="sandbox",
                )
                deadline = time.monotonic() + lifetime
                while True:
                    agent.refresh()
                    status = agent.template_build_status
                    if status in ("ready", None):
                        break
                    if status == "error":
                        raise RuntimeError(
                            "GMI template image build failed; inspect the GMI console"
                        )
                    if time.monotonic() >= deadline:
                        raise TimeoutError("GMI template image build timed out")
                    time.sleep(2)
            else:
                agent = self.client.agents.get(self.settings.agent_slug)
            started = time.monotonic()
            sandbox = agent.launch(
                instance_type=self.settings.instance_type,
                timeout_seconds=lifetime,
                metadata={"application": "cloudvet", "version": "0.1.0"},
            )
            self._deadlines[sandbox.id] = started + lifetime
            if owned:
                self._owned_agents[sandbox.id] = agent
            sandbox.wait_until_running(timeout=lifetime)
            self.launch_details[sandbox.id] = {
                "boot_seconds": time.monotonic() - started,
                "idc": agent.idc or self.settings.default_idc,
            }
            return sandbox
        except BaseException:
            if sandbox is not None:
                self._cleanup_after_error(sandbox)
            elif agent is not None and owned:
                try:
                    agent.delete()
                except Exception:
                    warnings.warn(
                        "Could not delete cloudvet template; inspect the GMI console",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            raise

    @contextmanager
    def session(self) -> Iterator[Any]:
        sandbox = self.launch_ephemeral_sandbox()
        try:
            yield sandbox
        except BaseException:
            self._cleanup_after_error(sandbox)
            raise
        else:
            self.cleanup(sandbox)

    def _cleanup_after_error(self, sandbox: Any) -> None:
        try:
            self.cleanup(sandbox)
        except Exception:
            warnings.warn(
                f"Cleanup failed for sandbox {sandbox.id}; TTL remains active. Check GMI console.",
                RuntimeWarning,
                stacklevel=2,
            )

    def cleanup(self, sandbox: Any) -> None:
        last_error = None
        for _ in range(3):
            try:
                sandbox.delete()
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error:
            raise RuntimeError(
                f"Could not delete sandbox {sandbox.id}; inspect GMI console"
            ) from last_error
        self._deadlines.pop(sandbox.id, None)
        agent = self._owned_agents.pop(sandbox.id, None)
        if agent is not None:
            agent.delete()

    def run_command(
        self,
        sandbox: Any,
        cmd: str,
        *,
        cwd: str = "/workspace",
        on_output: Callable[[str, str], None] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        remaining = (
            self._deadlines.get(sandbox.id, started + self.settings.command_timeout + 5) - started
        )
        limit = min(self.settings.command_timeout, int(remaining) - 5)
        if limit < 1:
            raise TimeoutError("Sandbox lifetime exhausted; increase CLOUDVET_TIMEOUT_SECONDS")
        wrapped = (
            f"cd {shlex.quote(cwd)} && timeout --kill-after=5s {limit}s bash -lc {shlex.quote(cmd)}"
        )
        execution = sandbox.execute(wrapped, wait=False)
        if not execution.id:
            raise RuntimeError("GMI returned no execution ID")
        execution_id = execution.id
        offsets = {"stdout": 0, "stderr": 0}
        try:
            while True:
                execution = sandbox.get_execution(execution_id)
                for stream in offsets:
                    content = execution.data.get(stream) or ""
                    if on_output and len(content) > offsets[stream]:
                        on_output(stream, content[offsets[stream] :])
                    offsets[stream] = len(content)
                if execution.exit_code is not None:
                    break
                if execution.status in {"failed", "error", "cancelled", "canceled"}:
                    raise RuntimeError(f"GMI execution ended without exit code: {execution.status}")
                if time.monotonic() - started >= limit:
                    sandbox.cancel_execution(execution_id)
                    return ExecutionResult(
                        command=cmd,
                        exit_code=124,
                        stdout=execution.data.get("stdout") or "",
                        stderr=execution.data.get("stderr") or "",
                        duration_seconds=time.monotonic() - started,
                        timed_out=True,
                    )
                time.sleep(0.25)
        except BaseException:
            try:
                sandbox.cancel_execution(execution_id)
            except Exception:
                pass  # Session cleanup and the server TTL bound a disconnected command.
            raise
        return ExecutionResult(
            command=cmd,
            exit_code=execution.exit_code,
            stdout=execution.data.get("stdout") or "",
            stderr=execution.data.get("stderr") or "",
            duration_seconds=time.monotonic() - started,
            timed_out=execution.exit_code in {124, 137},
        )

    def upload_workspace(self, sandbox: Any, workspace: Workspace) -> None:
        archive_path = f"/tmp/cloudvet-{uuid4().hex}.tar.gz"
        sandbox.upload_file(path=archive_path, file=workspace.archive())
        command = (
            f"mkdir -p /workspace && tar -xzf {shlex.quote(archive_path)} -C /workspace "
            f"&& rm -f {shlex.quote(archive_path)} && cd /workspace "
            "&& git -c init.templateDir= init -q "
            "&& git -c core.hooksPath=/dev/null add -f . "
            "&& git -c core.hooksPath=/dev/null -c user.name=cloudvet "
            "-c user.email=cloudvet@localhost -c commit.gpgsign=false "
            "commit -qm baseline --allow-empty"
        )
        result = self.run_command(sandbox, command, cwd="/tmp")
        if not result.passed:
            raise RuntimeError(
                "Workspace extraction failed; image must include bash, tar, git and timeout"
            )
