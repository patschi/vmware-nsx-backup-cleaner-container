# Contributing

Thank you for your interest in contributing. Please read this guide before submitting changes.

## Reporting issues

Found a bug or have a feature request? [Open an issue](../../issues) first. Describe what you observed, what you
expected, and include logs if relevant.

For **larger changes** (new features, refactors, architectural changes), always file an issue first to discuss the
approach before writing code. This avoids wasted effort if the direction needs adjustment.

## The vendor script is off-limits

`vendor-scripts/nsx_backup_cleaner.py` is shipped verbatim from a VMware NSX Manager appliance
(`/var/vmware/nsx/file-store/nsx_backup_cleaner.py`). **Do not modify it** - not for lint, not for Python 2/3 cleanup,
not for refactoring, not for anything. The whole point of this repository is to wrap it as-is. If the behavior of the
cleanup itself needs to change, that is a VMware concern, not ours.

All project logic lives in `entrypoint.py`. Anything you'd want to change about *when*, *how*, or *with what arguments*
the vendor script runs goes there.

## Development setup

### Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker (or any OCI-compatible runtime) for container builds

### Local environment

```bash
uv sync                              # create .venv and install deps (incl. dev)
uv run python entrypoint.py          # run the wrapper against /backups (must exist locally)
```

For a quick ad-hoc test against a fake backup tree:

```bash
mkdir -p /tmp/nsxtest/cluster-node-backups/b1
touch /tmp/nsxtest/cluster-node-backups/b1/data.tar
SCHEDULE=0 RETENTION_DAYS=7 MIN_BACKUPS=10 \
  uv run python -c "
import entrypoint
entrypoint.BACKUP_DIR = '/tmp/nsxtest'
entrypoint.CLEANER_SCRIPT = 'vendor-scripts/nsx_backup_cleaner.py'
entrypoint.main()
"
```

### Running tests

```bash
uv run pytest -v
```

Tests cover the wrapper only - the vendor script is intentionally not exercised by unit tests since we do not own it.

### Linting

```bash
uvx ruff check entrypoint.py
uvx ruff format --check entrypoint.py
```

CI runs the same commands on every MR via `.gitlab-ci.yml`.

### Building the container

```bash
docker build -t nsx-backup-cleaner-container:latest .
```

To force a clean build after changing `pyproject.toml`:

```bash
docker build --no-cache -t nsx-backup-cleaner-container:latest .
```

## Submitting changes

When submitting a pull request:

1. **Explain why** the change is needed and **what** it does in the PR description.
2. **Add or update tests** if your change touches `entrypoint.py` behavior.
3. **Update documentation** (`README.md` / this file) if your change affects configuration, defaults, or runtime
   behavior.
4. **Comment your code** with descriptive comments that explain what each meaningful block does. WHAT-comments are
   welcome here; not just rare WHY-comments.
5. Keep commits focused and messages clear.

## Python version alignment

The builder stage in `Dockerfile` (`python:3.13-slim-trixie`) and the runtime stage
(`gcr.io/distroless/python3-debian13`) must always use the **same Python minor version**. They are intentionally tied
to the same Debian release (Trixie = Debian 13).

When bumping the Python version (e.g. moving to Python 3.14 with a new distroless base):

1. Update the `FROM python:3.13-slim-...` line in `Dockerfile` to the new version and Debian codename.
2. Update the `FROM gcr.io/distroless/python3-debian13` line to the matching `python3-debian14` (or equivalent) tag.
3. Update the `image: python:3.13-slim@sha256:...` line in `.gitlab-ci.yml` to the new version.
4. Update the `allowedVersions` regex in `renovate.json` from `/^3\.13/` to `/^3\.14/`.
5. Update `requires-python` in `pyproject.toml`.

All five must change together. Renovate is intentionally prevented from bumping the Python minor version
automatically - the upgrade is a deliberate, coordinated change.

## License

By contributing, you agree that your contributions will be licensed under the [GPLv3 License](LICENSE).
