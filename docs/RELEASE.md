# Release 0.1.0

Source publication and PyPI publication are separate steps. The repository includes a buildable wheel and source distribution configuration; it does not imply that the PyPI name has been registered.

## Acceptance

Before treating this release as production-ready:

- Run `check` against real Python, Node, Rust and Go fixtures using suitable images. Keep the JSON verdicts, image digests, SDK version, selected IDC and SKU.
- Verify a failing revision gets a minimal passing repair and that declining the local prompt leaves the working tree unchanged.
- Verify an interruption and a command timeout stop billing after deletion or the server lifetime. Inspect the GMI console for leaked resources.
- Verify the configured model IDs are available to the account and record the provider usage tokens. Confirm actual charges and grant eligibility in the console.
- Review the audit limitations. This release does not implement dynamic exploit validation or Cloudflare schema compatibility.

Do not report these checks as complete based on mocked unit tests.

The current live Python acceptance blocker is a Cloudflare HTTP 403 from GMI's execution gateway for the dependency-install command. Workspace upload and the other three fixture stacks succeeded. Resolve this API gateway policy with GMI before rerunning Python acceptance; see [VERIFICATION.md](VERIFICATION.md).

## Build

```bash
python -m pip install -e '.[dev]'
pytest -q
ruff check .
ruff format --check .
python -m build
python -m twine check dist/*
```

Install the wheel into a fresh environment outside the checkout and run `cloudvet --version`, `cloudvet --help` and all command help screens. Inspect the wheel and sdist for credentials and unwanted workspace files.

## Publish

1. Register the PyPI project or pending publisher for `cloudvet` under an account authorized to own that name. Availability must be checked at release time.
2. Configure GitHub trusted publishing for owner `dhruvil-codes`, repository `cloudvet`, workflow `publish.yml`, environment `pypi`.
3. Configure the GitHub `pypi` environment's required reviewers if a release review is desired.
4. Review the exact commit and CI results, then dispatch **Publish to PyPI** from that commit's branch. The workflow rebuilds and tests before publication.
5. Verify `python -m pip install cloudvet==0.1.0` in a new environment, then create the GitHub tag/release with checksums and validation evidence.

The workflow is manual and has not been dispatched as part of source preparation. Never put API keys or a PyPI token in the repository.
