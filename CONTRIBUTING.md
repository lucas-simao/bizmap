# Contributing to BizMap

## Development

From a clone of the repository:

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

`tests/test_snapshots.py` keeps a golden, line-per-link summary of every example in
`tests/snapshots/*.txt` and checks that each example scans to byte-identical JSON under
several `PYTHONHASHSEED` values. When a detector change is intended, refresh with
`BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py` and review the `+`/`-` diff
in the PR.

Fixtures are synthetic (`orders`, `users`, `invoices`, …); no proprietary code is
included. MIT licensed. Contributions that add detector rules for new frameworks are
especially welcome — please include a minimal example and let the snapshot diff show
what changed.

## Releasing (maintainers)

Bump the version (`pyproject.toml`, `src/bizmap/__init__.py`, graph version,
`docs/graph-schema.md`, README and `docs/github-action.md` `@vX.Y.Z`), date the CHANGELOG section, merge, then
push a tag on that commit:

```bash
git tag -a vX.Y.Z <commit> -m "vX.Y.Z" && git push origin vX.Y.Z
```

`.github/workflows/release.yml` checks that the tag is on `main` and matches the
version and the CHANGELOG, runs the tests, builds, publishes to PyPI with Trusted
Publishing (no token) and creates the GitHub release with that CHANGELOG section.
One-time setup is described at the top of the workflow. Tags on commits older
than the workflow (v0.3.3–v0.3.5) are published with **Actions → Release → Run
workflow** (from `main`) and the tag name. Limit the `pypi` environment to
`main` and `v*` tags (Deployment branches and tags) so no other branch can
publish.
