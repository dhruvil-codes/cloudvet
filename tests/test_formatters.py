import io

import pytest
from conftest import PATCH, execution
from rich.console import Console
from rich.style import Style

from cloudvet import formatters
from cloudvet.healer import ModelUsage


@pytest.mark.parametrize("width", [40, 80, 120])
def test_terminal_cards_fit_and_never_force_background(monkeypatch, width):
    output = io.StringIO()
    terminal = Console(
        file=output,
        width=width,
        height=25,
        force_terminal=True,
        color_system="truecolor",
        record=True,
        _environ={"TERM": "xterm-256color"},
    )
    monkeypatch.setattr(formatters, "console", terminal)
    formatters.show_header()
    result = execution(0, stdout="test result: ok. 3 passed; 0 failed; 0 ignored;\n")
    formatters.show_execution(result, stack="Rust", boot_seconds=1.25)
    formatters.show_diff(PATCH)
    usage = [
        ModelUsage(model="deepseek-ai/DeepSeek-V4-Pro", prompt_tokens=123, completion_tokens=456)
    ]
    formatters.show_model_usage(usage)
    formatters.Workflow(stack="Rust", command="cargo test --quiet").summary(
        "PASS WITH VERIFIED REPAIR",
        result,
        usage,
        attempts=1,
    )
    plain = terminal.export_text(clear=False)
    assert "3 passed, 0 failed" in plain
    assert "H100" not in plain and "$0." not in plain
    assert max(map(len, plain.splitlines())) <= min(width, 80)
    for segment in terminal._record_buffer:
        if isinstance(segment.style, Style):
            assert segment.style.bgcolor is None


def test_rust_counts_include_all_test_suites():
    result = execution(
        stdout=("test result: ok. 0 passed; 0 failed;\ntest result: FAILED. 2 passed; 1 failed;\n")
    )
    assert formatters.test_summary(result) == "2 passed, 1 failed"


def test_unknown_counts_and_markup_remain_literal(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(formatters, "console", Console(file=output, width=80))
    result = execution(1, stdout="[red]target text[/red]\n")
    formatters.show_execution(result)
    assert "[red]target text[/red]" in output.getvalue()
    assert formatters.test_summary(result) == "Command failed; counts not reported"


def test_stage_failures_never_announce_completion(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(formatters, "console", Console(file=output, width=80, force_terminal=False))
    with pytest.raises(ValueError):
        with formatters.stage("Transfer source"):
            raise ValueError("blocked")
    assert "stopped" in output.getvalue()
    assert "◆" not in output.getvalue()
