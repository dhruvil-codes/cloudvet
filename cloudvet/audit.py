"""Source-only defensive review, inspired by Cloudflare's six-phase workflow.

No repository code, generated probe, exploit or fuzzer is executed by this module.
The report uses cloudvet's own schema; it is not a Cloudflare audit attestation.
"""

import inspect
import json
import shlex
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from cloudvet.sandbox import SandboxManager, Workspace


def scan_sources(sources: dict[str, str], full: bool) -> list[dict]:
    """This self-contained stdlib scanner can run remotely without installing cloudvet."""
    import ast
    from concurrent.futures import ThreadPoolExecutor

    rules = {
        "eval": ("Dynamic evaluation", "Review whether untrusted input can reach evaluation."),
        "exec": ("Dynamic execution", "Replace dynamic execution with a constrained parser."),
        "pickle.loads": (
            "Deserialization",
            "Only deserialize trusted data; prefer a data-only format.",
        ),
        "pickle.load": (
            "Deserialization",
            "Only deserialize trusted data; prefer a data-only format.",
        ),
        "yaml.load": ("Deserialization", "Use yaml.safe_load for untrusted YAML."),
    }

    def scan(item):
        name, text = item
        if not name.endswith(".py"):
            return []
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return [
                {
                    "path": name,
                    "line": 1,
                    "title": "Source could not be parsed",
                    "category": "coverage",
                    "recommendation": "Review this file manually.",
                }
            ]
        results = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = ast.unparse(node.func)
            category, recommendation = "", ""
            if function in rules:
                category, recommendation = rules[function]
            if full:
                if function.startswith("subprocess.") and any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                ):
                    category = "Shell invocation"
                    recommendation = (
                        "Use an argument list with shell=False for untrusted arguments."
                    )
                if any(
                    k.arg == "verify"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is False
                    for k in node.keywords
                ):
                    category = "TLS verification"
                    recommendation = "Keep certificate verification enabled for remote requests."
            if category:
                results.append(
                    {
                        "path": name,
                        "line": node.lineno,
                        "title": f"Review {function}",
                        "category": category,
                        "recommendation": recommendation,
                    }
                )
        return results

    with ThreadPoolExecutor(max_workers=4) as pool:
        return [lead for result in pool.map(scan, sorted(sources.items())) for lead in result]


class Finding(BaseModel):
    id: str
    title: str
    category: str
    path: str
    line: int = Field(ge=1)
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    status: Literal["confirmed", "needs_validation", "rejected"] = "needs_validation"
    evidence: str
    recommendation: str
    blocker: str = "No trust-boundary or dynamic validation performed."


class AuditReport(BaseModel):
    schema_version: str = "cloudvet.audit.v1"
    mode: Literal["quick", "full"]
    run_status: Literal["incomplete"] = "incomplete"
    phases: dict[str, str]
    scanned_files: list[str]
    skipped_files: list[str]
    limitations: list[str]
    confirmed: list[Finding] = Field(default_factory=list)
    needs_validation: list[Finding] = Field(default_factory=list)
    rejected: list[Finding] = Field(default_factory=list)


def audit_workspace(
    manager: SandboxManager,
    sandbox: Any,
    workspace: Workspace,
    *,
    full: bool = False,
) -> AuditReport:
    sources, skipped, total = {}, [], 0
    for name, data in workspace.files.items():
        if not name.endswith(".py") or len(data) > 100_000 or total + len(data) > 2_000_000:
            skipped.append(name)
            continue
        try:
            sources[name] = data.decode("utf-8-sig")
            total += len(data)
        except UnicodeError:
            skipped.append(name)
    prefix = f"/tmp/cloudvet-audit-{uuid4().hex}"
    data_path, script_path = prefix + ".json", prefix + ".py"
    script = (
        "import json, sys\n"
        + inspect.getsource(scan_sources)
        + "\nwith open(sys.argv[1], encoding='utf-8') as f: data = json.load(f)\n"
        + f"print(json.dumps(scan_sources(data, {full!r})))\n"
    )
    sandbox.upload_file(path=data_path, file=json.dumps(sources).encode())
    sandbox.upload_file(path=script_path, file=script.encode())
    result = manager.run_command(
        sandbox,
        f"python3 -I {shlex.quote(script_path)} {shlex.quote(data_path)}",
        cwd="/tmp",
    )
    if not result.passed:
        raise RuntimeError("Source review failed; use a sandbox image with Python 3.10+")
    leads = json.loads(result.stdout)
    if not isinstance(leads, list) or len(leads) > 10000:
        raise ValueError("Invalid scanner output")
    findings, rejected = [], []
    for index, lead in enumerate(leads, 1):
        name, line = lead["path"], lead["line"]
        if (
            name not in sources
            or not isinstance(line, int)
            or not 1 <= line <= len(sources[name].splitlines())
        ):
            raise ValueError("Scanner cited a location outside reviewed source")
        finding = Finding(
            id=f"CV-{index:04d}",
            evidence=sources[name].splitlines()[line - 1][:300],
            **lead,
        )
        if finding.category == "coverage":
            skipped.append(name)
        findings.append(finding)
    return AuditReport(
        mode="full" if full else "quick",
        phases={
            "1_reconnaissance": "File inventory only; manual trust-boundary mapping needed.",
            "2_hunting": "Bounded parallel Python AST checks; leads are not vulnerability claims.",
            "3_validation": "Source locations checked. Dynamic adversarial validation disabled.",
            "4_structured_output": "Validated against cloudvet.audit.v1 Pydantic schema.",
            "5_independent_verification": "Not performed; independent human review required.",
            "6_reporting": "JSON and Markdown include coverage gaps and unverified status.",
        },
        scanned_files=sorted(sources),
        skipped_files=sorted(set(skipped + workspace.excluded)),
        limitations=[
            "Python syntax heuristics only. Aliases, cross-file data flow "
            "and trust boundaries are not resolved.",
            "Node, Rust, Go, prompt injection, authorization, SSRF "
            "and memory safety need manual review.",
            "Outbound network isolation is not exposed by this AgentBox SDK; "
            "no target code or probes run.",
            "No exploitability confirmation, independent verifier "
            "or Cloudflare schema conformance.",
            "At most 100 kB per Python file and 2 MB total are reviewed; "
            "all exclusions are listed.",
        ],
        needs_validation=findings,
        rejected=rejected,
    )


def write_report(report: AuditReport, output: Path) -> tuple[Path, Path]:
    # A fresh directory prevents a new run from overwriting prior evidence.
    output.mkdir(parents=True, exist_ok=False)
    json_path, markdown_path = output / "findings.json", output / "REPORT.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    lines = [
        "# cloudvet source review",
        "",
        f"Status: {report.run_status}. Mode: {report.mode}.",
        "",
        "No confirmed vulnerabilities. This is a limited source review.",
        "",
        "## Phases",
        "",
    ]
    lines += [f"- {phase}: {status}" for phase, status in report.phases.items()]
    lines += ["", "## Leads requiring review", ""]
    for finding in report.needs_validation:
        lines += [
            f"### {finding.id}: {finding.title}",
            "",
            f"Location: {json.dumps(finding.path)} line {finding.line}.",
            "",
            finding.recommendation,
            "",
            f"Blocker: {finding.blocker}",
            "",
        ]
    if not report.needs_validation:
        lines += ["No heuristic matches. This does not establish that the code is secure.", ""]
    lines += ["## Limitations", ""] + [f"- {item}" for item in report.limitations]
    lines += ["", "## Scanned files", ""] + [f"- {json.dumps(p)}" for p in report.scanned_files]
    lines += ["", "## Skipped files", ""] + [f"- {json.dumps(p)}" for p in report.skipped_files]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
