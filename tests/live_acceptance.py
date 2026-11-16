"""Explicit, billable GMI acceptance test using small, owned fixture repositories.

Run from the repository root: python tests/live_acceptance.py --stack python --heal
This file is not collected by pytest. Credentials are read only from the environment.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

# Running a script under tests/ otherwise omits the package's parent from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudvet.audit import audit_workspace, write_report  # noqa: E402
from cloudvet.config import Settings  # noqa: E402
from cloudvet.detector import detect_stack  # noqa: E402
from cloudvet.healer import Healer, MaaS  # noqa: E402
from cloudvet.sandbox import SandboxManager, Workspace, git  # noqa: E402

FIXTURES = {
    "python": {
        "app.py": "def add(a, b):\n    return a - b\n",
        "test_app.py": "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        "requirements.txt": "pytest>=8,<9\n",
    },
    "node": {
        "package.json": '{"name":"cloudvet-fixture","private":true,'
        '"scripts":{"test":"node --test"}}',
        "sum.test.js": (
            "const {test} = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "test('addition', () => assert.equal(2 + 3, 5));\n"
        ),
    },
    "rust": {
        "Cargo.toml": '[package]\nname="cloudvet-fixture"\nversion="0.1.0"\nedition="2021"\n',
        "src/lib.rs": "#[test]\nfn addition() { assert_eq!(2 + 3, 5); }\n",
    },
    "go": {
        "go.mod": "module example.com/cloudvetfixture\n\ngo 1.21\n",
        "sum_test.go": (
            'package fixture\nimport "testing"\n'
            'func TestAddition(t *testing.T) { if 2+3 != 5 { t.Fatal("addition") } }\n'
        ),
    },
}
IMAGES = {
    "python": "docker.io/library/python:3.12-bookworm",
    "node": "docker.io/library/node:22-bookworm",
    "rust": "docker.io/library/rust:1-bookworm",
    "go": "docker.io/library/golang:1-bookworm",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=[*FIXTURES, "all"], default="python")
    parser.add_argument("--heal", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reports = []
    for language in FIXTURES if args.stack == "all" else [args.stack]:
        print(f"Live acceptance: {language}", flush=True)
        settings = Settings.from_env().model_copy(update={"image": IMAGES[language]})
        if args.heal and language == "python":
            settings.require_inference()
        record = {"stack": language, "image": settings.image, "cleanup": "not_started"}
        with tempfile.TemporaryDirectory(prefix="cloudvet-acceptance-") as directory:
            root = Path(directory)
            for name, content in FIXTURES[language].items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content.encode())
            git(root, "-c", "init.templateDir=", "init", "-q")
            workspace, stack = Workspace.capture(root), detect_stack(root)
            manager = SandboxManager(settings)
            try:
                with manager.session() as sandbox:
                    record["sandbox_id"] = sandbox.id
                    record["cleanup"] = "session_finalizer_on_exit"
                    manager.upload_workspace(sandbox, workspace)
                    setup = manager.run_command(sandbox, stack.install_command)
                    record["setup"] = setup.model_dump()
                    if not setup.passed:
                        raise RuntimeError("Fixture dependency installation failed")
                    result = manager.run_command(sandbox, stack.test_command)
                    record["initial"] = result.model_dump()
                    print(f"Initial {language} test exit: {result.exit_code}", flush=True)
                    if language == "python" and args.heal:
                        models = MaaS(settings)
                        try:
                            repaired = Healer(settings, manager, models).repair(
                                sandbox,
                                workspace,
                                stack.test_command,
                                result,
                            )
                            record["repair"] = repaired.model_dump()
                            if not repaired.verified:
                                raise RuntimeError("Fixture repair did not verify")
                        finally:
                            models.close()
                    elif not result.passed and language != "python":
                        raise RuntimeError("Fixture tests failed")
                    if language == "python":
                        report = audit_workspace(manager, sandbox, workspace, full=True)
                        write_report(report, args.output / "audit")
                        record["audit_status"] = report.run_status
                record["cleanup"] = "delete_returned_success"
                record["status"] = "passed"
            except Exception as exc:
                record["status"] = "failed"
                record["error_type"] = type(exc).__name__
                record["error_code"] = getattr(exc, "code", None)
                record["http_status"] = getattr(exc, "status_code", None)
                message = str(exc)
                for secret in (settings.agentbox_api_key, settings.maas_api_key):
                    if secret.get_secret_value():
                        message = message.replace(secret.get_secret_value(), "[redacted]")
                record["error_message"] = message[:1000]
                # Do not print raw provider exceptions, which may carry authentication metadata.
                print(f"Acceptance blocked: {type(exc).__name__}: {message[:1000]}", flush=True)
            reports.append(record)
            (args.output / "acceptance.json").write_text(
                json.dumps(reports, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"{language}: {record['status']}; cleanup: {record['cleanup']}", flush=True)
    return 0 if all(r["status"] == "passed" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
