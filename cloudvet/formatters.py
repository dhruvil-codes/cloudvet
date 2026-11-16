"""Terminal presentation without interpreting target output as Rich markup."""

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from cloudvet.sandbox import ExecutionResult

console = Console()


def show_execution(result: ExecutionResult) -> None:
    table = Table("Verdict", "Exit", "Seconds", "Command")
    table.add_row(
        "PASS" if result.passed else "FAIL",
        str(result.exit_code),
        f"{result.duration_seconds:.2f}",
        result.command,
    )
    console.print(table)
    for text in (result.stdout, result.stderr):
        if text:
            console.print(
                text, markup=False, highlight=False, end="" if text.endswith("\n") else "\n"
            )


def show_diff(diff: str) -> None:
    console.print(Syntax(diff, "diff", word_wrap=True))
