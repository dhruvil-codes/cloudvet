"""Transparent terminal cards, bounded to 80 columns with plain-text fallbacks."""

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from pygments.token import Generic
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.style import Style
from rich.syntax import ANSISyntaxTheme, Syntax
from rich.table import Table
from rich.text import Text

from cloudvet import __version__
from cloudvet.healer import ModelUsage
from cloudvet.sandbox import ExecutionResult

ACCENT = "#79c0ff"
SUCCESS = "#3fb950"
ERROR = "#ff7b72"
MUTED = "#8b949e"
ADDED = "#56d364"
DELETED = "#f85149"
console = Console(highlight=False)
DIFF_THEME = ANSISyntaxTheme(
    {
        Generic.Inserted: Style(color=ADDED),
        Generic.Deleted: Style(color=DELETED),
        Generic.Subheading: Style(color=ACCENT),
        Generic.Heading: Style(color=MUTED),
    }
)


def card(content, title: str, color: str = ACCENT) -> Panel:
    if isinstance(content, str):
        content = Text(content)
    return Panel(
        content,
        title=Text(title, style=color),
        title_align="left",
        border_style=MUTED,
        box=box.SQUARE,
        padding=(1, 2),
        width=min(console.width, 80),
        safe_box=True,
    )


def fields(rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style=MUTED, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    for label, value in rows:
        table.add_row(Text(label), Text(value))
    return table


def show_header() -> None:
    console.print()
    name = "cloudvet" if console.options.ascii_only else "☁  cloudvet"
    console.print(
        Text.assemble((name, f"bold {ACCENT}"), (f" v{__version__}  ·  GMI AgentBox v2", MUTED))
    )
    console.print()


def note(message: str) -> None:
    console.print(Text(message, style=MUTED))


@contextmanager
def stage(label: str):
    pending_node = "> " if console.options.ascii_only else "◇ "
    complete_node = "+ " if console.options.ascii_only else "◆ "
    pending = Text.assemble((pending_node, ACCENT), (label, MUTED))
    try:
        if console.is_terminal and not console.is_dumb_terminal:
            with console.status(pending, spinner="dots", spinner_style=ACCENT):
                yield
        else:
            console.print(pending)
            yield
    except BaseException:
        console.print(Text.assemble((pending_node, ERROR), (label + " · stopped", ERROR)))
        raise
    else:
        console.print(Text.assemble((complete_node, SUCCESS), (label, MUTED)))


def test_summary(result: ExecutionResult) -> str:
    output = result.stdout + "\n" + result.stderr
    rust = re.findall(r"test result: (?:ok|FAILED)\.\s+(\d+) passed;\s+(\d+) failed", output)
    if rust:
        passed = sum(int(p) for p, _ in rust)
        failed = sum(int(f) for _, f in rust)
        return f"{passed} passed, {failed} failed"
    for line in reversed(output.splitlines()):
        if re.search(r"\bin \d+(?:\.\d+)?s", line):
            counts = re.findall(r"\b(\d+) (passed|failed|skipped|errors?)\b", line)
            if counts:
                return ", ".join(f"{number} {status}" for number, status in counts)
    return (
        "Command passed; counts not reported"
        if result.passed
        else "Command failed; counts not reported"
    )


def show_execution(
    result: ExecutionResult, *, stack: str = "", boot_seconds: float | None = None
) -> None:
    rows = [("Toolchain", stack)] if stack else []
    if boot_seconds is not None:
        rows.append(("Sandbox boot", f"{boot_seconds:.2f}s · launch to running"))
    rows += [
        ("Command", result.command),
        ("Exit", str(result.exit_code)),
        ("Tests", test_summary(result)),
        ("Execution", f"{result.duration_seconds:.2f}s"),
    ]
    output = [fields(rows)]
    for text in (result.stdout, result.stderr):
        if text.strip():
            output += [Text(""), Text(Text.from_ansi(text.rstrip()).plain)]
    console.print(
        card(
            Group(*output), "PASS" if result.passed else "FAIL", SUCCESS if result.passed else ERROR
        )
    )


def show_diff(diff: str) -> None:
    syntax = Syntax(
        diff.rstrip(), "diff", theme=DIFF_THEME, line_numbers=True, word_wrap=True, padding=0
    )
    console.print(card(syntax, "Verified patch · diff line numbers"))


def show_model_usage(usage: list[ModelUsage]) -> None:
    if not usage:
        note("No MaaS token usage reported.")
        return
    rows = [
        (call.model, f"{call.prompt_tokens} prompt / {call.completion_tokens} completion")
        for call in usage
    ]
    console.print(card(fields(rows), "MaaS usage · provider-reported"))


@dataclass
class Workflow:
    stack: str = ""
    command: str = ""
    idc: str = ""
    boot_seconds: float | None = None
    started: float = field(default_factory=time.monotonic)

    def summary(
        self,
        verdict: str,
        execution: ExecutionResult | None = None,
        usage: list[ModelUsage] | None = None,
        attempts: int = 0,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        calls = usage or []
        rows = []
        if self.stack:
            rows.append(("Stack", self.stack))
        if self.command:
            rows.append(("Command", self.command))
        sandbox = "GMI AgentBox v2"
        if self.idc:
            sandbox += f" · {self.idc}"
        if self.boot_seconds is not None:
            sandbox += f" · {self.boot_seconds:.2f}s boot"
        rows.append(("Sandbox", sandbox))
        if execution:
            rows += [
                ("Tests", test_summary(execution)),
                ("Execution", f"{execution.duration_seconds:.2f}s · final test command"),
            ]
        if attempts:
            rows.append(("Repair", f"{attempts} patch attempts"))
        if calls:
            rows.append(("Inference", " + ".join(dict.fromkeys(call.model for call in calls))))
            rows.append(
                (
                    "Tokens",
                    f"{sum(c.prompt_tokens for c in calls)} prompt / "
                    f"{sum(c.completion_tokens for c in calls)} completion",
                )
            )
            rows.append(("Cost", "Not calculated; consult GMI billing"))
        rows += extra or []
        rows.append(("Duration", f"{time.monotonic() - self.started:.2f}s total wall time"))
        color = SUCCESS if verdict.startswith("PASS") else ERROR if verdict == "FAIL" else ACCENT
        console.print()
        console.print(card(fields(rows), "CLOUDVET VERDICT: " + verdict, color))
