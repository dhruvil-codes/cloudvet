# Verification notes

Verified on November 16, 2026 from Windows with Python 3.10.19. This document distinguishes local verification from live service acceptance. It is not a production-readiness certificate.

## Local checks

- 67 unit/integration tests passed. Tests cover detection, real temporary Git repositories, snapshot exclusions, SDK lifecycle failures, timeout cancellation, model patch restrictions, local patch consent and stale-file protection, source scanner execution, and JSON report validation.
- Ruff lint and formatting passed.
- An isolated Hatchling build produced the 0.1.0 wheel and sdist; Twine metadata checks passed.
- The wheel installed in a fresh Python 3.13 environment and its CLI help/version ran outside the repository.
- Tests also passed against AgentBox SDK 0.1.0b6, OpenAI 1.55.3, Pydantic 2.7.0 and Typer 0.29.0. The primary environment used SDK 0.1.0b8 and OpenAI 3.51.0.
- Minimum Typer/OpenAI versions were raised after checking CLI union-type support and HTTP client compatibility. CI additionally checks Linux and multiple Python versions after source publication.

## Live GMI checks

The supplied compute key successfully discovered `us-central-iowa16` and `gmi.sandbox.small` and provisioned disposable sandboxes.

- Node: installed the tiny fixture, ran `npm test`, observed exit 0, and deleted its sandbox and owned template.
- Rust: ran `cargo test --quiet`, observed exit 0, and deleted its sandbox and owned template.
- Go: ran `go test ./...`, observed exit 0, and deleted its sandbox and owned template.
- Python: fixture setup received HTTP 403 with the SDK message `Request failed`. A diagnostic transport established that workspace upload/extraction succeeds, while the subsequent `python -m pip install pytest` request is rejected at GMI's `/executions` gateway with a Cloudflare HTML block page. The response title is `Attention Required! | Cloudflare`. This is a provider gateway restriction on an ordinary dependency-install command. Full Python test/repair/audit acceptance remains unverified until GMI permits the request and a complete run passes.

The inference model catalog listed `deepseek-ai/DeepSeek-V4.1-Flash` and `deepseek-ai/DeepSeek-V4-Pro`. It did not list the requested Sonnet model. The live inference check therefore used the task's allowed V4-Pro alternative via the explicit `GMI_FIX_MODEL` override. No silent fallback exists in the application.

A separate fixture synthesis check returned a valid diagnosis and a minimal addition repair. That successful check reported 113 prompt / 113 completion tokens for Flash and 170 prompt / 576 completion tokens for V4-Pro. A previous diagnosis-only check reported 86 prompt / 156 completion tokens and failed schema validation; the prompt was tightened to specify exact field types. These are provider-reported tokens, not billing totals or passing cloud-test evidence.

Local live-run artifacts remain under ignored `.cloudvet/` directories. Credentials were supplied through secure input and process environment variables, and were not written to source or committed artifacts.

## Calculator demo retry

The temporary `demo_app/` fixture contains `calculator.py`, `test_calculator.py` and
`requirements.txt`. Its zero-division test expects `ValueError("Cannot divide by zero")`,
while the intentionally broken implementation raises `ZeroDivisionError`. The local
baseline produced exactly **1 failed, 1 passed**.

The real CLI was invoked against this fixture using the supported SDK interface, with
Flash for triage and an explicit V4-Pro repair override. No command encoding or WAF
evasion was added.

- `cloudvet check`: exited 2 with `GMI compute HTTP 403: Request failed`.
  Total workflow wall time was 21.47 seconds.
- `cloudvet fix calculator.py`: exited 2 with the same HTTP 403 before reaching triage.
  Total workflow wall time was 16.41 seconds. No local patch was applied.
- These times include provisioning, API requests and cleanup. No test execution duration
  was returned by GMI, and neither command reached a cloud test verdict.
- No MaaS calls were made in these two runs because setup failed before inference.
- After both runs, the catalog listed **0 active cloudvet templates and 0 active sandboxes**.
- CLI logs, workflow timings and the cleanup snapshot are retained locally in
  `.cloudvet/demo-live-20261116/` at the repository root. They are ignored by Git.

Python end-to-end repair remains **BLOCKED**, not passed. GMI must resolve the rejection
of legitimate authenticated execution requests before this check can be completed.
An HTTP 403 alone does not establish which WAF rule or shell keyword triggered the block.

The terminal now shows both initial/final repair test verdicts and provider-reported
MaaS usage for completed repair attempts. All 67 tests, Ruff lint and formatting checks
passed after these changes. The initial GitHub CI run also completed successfully.

## Release limits

PyPI publication has not been performed. Full cloud repair acceptance, live audit execution, timeout/interrupt billing reconciliation and a $50 usage target are not complete. No claim is made that $50 has been spent. The account's actual compute/model charges and grant eligibility require billing-console verification.

Security review is a bounded Python source scan. Dynamic exploit validation, outbound-denied execution, independent security verification and Cloudflare report-schema compatibility are not implemented. Reports explicitly remain incomplete.
