# Conch repository guidance

`AGENTS.md` links to this file so coding agents share the same instructions.

## Preflight: every merge to main

- Always update documentation before merging into `main`, including docs-only and maintenance merges. Update `CHANGELOG.md` and keep `README.md`, `config.example`, and affected `docs/` pages accurate for the changes being shipped.
- Always increment `conch.__version__` in `conch/__init__.py` above the version on freshly fetched `origin/main`. Use the requested release version; otherwise increment the patch version. Never merge another change to `main` under an already-shipped version.
- Keep the README's current-release label/link and PyPI availability note, the dated changelog entry, and the `v{version}` release tag/notes consistent. `pyproject.toml` reads the version dynamically; do not add a second version source.
- Before merging, run the checks for the changed behavior and confirm `conch --version` and `conch-ask --version` report the new version.
