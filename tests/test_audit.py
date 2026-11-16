import json
import subprocess
from unittest.mock import MagicMock

import pytest
from conftest import execution

from cloudvet.audit import AuditReport, audit_workspace, scan_sources, write_report


def test_scanner_never_executes_source(tmp_path):
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\neval(user_input)\n"
    leads = scan_sources({"app.py": source}, full=False)
    assert not marker.exists()
    assert leads[0]["line"] == 3 and leads[0]["category"] == "Dynamic evaluation"


def test_full_adds_shell_and_tls_checks():
    source = "import subprocess\nsubprocess.run(cmd, shell=True)\nrequests.get(url, verify=False)"
    assert not scan_sources({"app.py": source}, False)
    assert len(scan_sources({"app.py": source}, True)) == 2


def test_audit_runs_only_trusted_scanner(workspace, sandbox, tmp_path):
    workspace.files["app.py"] = b"eval(user_input)\n"
    manager = MagicMock()
    manager.run_command.return_value = execution(
        stdout=json.dumps(scan_sources({"app.py": "eval(user_input)"}, False))
    )
    report = audit_workspace(manager, sandbox, workspace)
    assert report.run_status == "incomplete" and not report.confirmed
    assert report.needs_validation[0].status == "needs_validation"
    assert "outbound" in " ".join(report.limitations).lower()
    # Execute the exact generated stdlib scanner as a real subprocess against source data.
    uploads = {
        call.kwargs["path"]: call.kwargs["file"] for call in sandbox.upload_file.call_args_list
    }
    script_path = next(p for p in uploads if p.endswith(".py"))
    data_path = next(p for p in uploads if p.endswith(".json"))
    script, data = tmp_path / "scan.py", tmp_path / "sources.json"
    script.write_bytes(uploads[script_path])
    data.write_bytes(uploads[data_path])
    import sys

    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(data)], capture_output=True, check=True
    )
    assert json.loads(completed.stdout)[0]["path"] == "app.py"
    paths = write_report(report, tmp_path / "report")
    assert AuditReport.model_validate_json(paths[0].read_text()).needs_validation
    with pytest.raises(FileExistsError):
        write_report(report, tmp_path / "report")


def test_rejects_fabricated_evidence(workspace, sandbox):
    manager = MagicMock()
    manager.run_command.return_value = execution(stdout='[{"path":"outside.py","line":1}]')
    with pytest.raises(ValueError, match="outside reviewed"):
        audit_workspace(manager, sandbox, workspace)
