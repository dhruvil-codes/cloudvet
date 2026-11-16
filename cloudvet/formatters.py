"""Terminal presentation without interpreting target output as Rich markup."""

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from cloudvet.healer import ModelUsage
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


def show_model_usage(usage: list[ModelUsage]) -> None:
    if not usage:
        console.print("No MaaS token usage reported.")
        return
    table = Table("MaaS model", "Prompt tokens", "Completion tokens")
    for call in usage:
        table.add_row(call.model, str(call.prompt_tokens), str(call.completion_tokens))
    console.print(table)
