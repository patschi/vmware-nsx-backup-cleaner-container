# vmware-nsx-backup-cleaner-container - distroless container image.
#
# Multi-stage build:
#   1. Builder stage:  resolves and installs croniter into a staging
#                      directory using uv + the pinned uv.lock.
#   2. Final stage:    Google distroless Debian 13 Python image with the
#                      interpreter, croniter, the vendor cleanup script,
#                      and the cron-driven wrapper.
#
# Layer ordering is tuned for cache reuse:
#   - Dependencies (slow) come before application source (fast-changing).
#   - vendor-scripts/ is copied separately from the rest of /app because it
#     changes rarely and would otherwise force re-copy of every app file.
#   - LABEL and ENV both reference GIT_HASH (changes every commit). They
#     are placed AFTER all COPYs so source layers stay cached when only
#     the commit hash changes.
#
# Runs as root (UID 0) by default so it can chmod/delete files that NSX
# uploaded over SFTP under whatever UID the SFTP daemon assigned. See
# README.md for instructions on switching to a non-root UID.

# ---------- Stage 1: build dependencies in a full Python image ----------
FROM python:3.13-slim-trixie@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

WORKDIR /build

# uv binary, pinned to a specific tag+SHA for reproducible builds. Renovate
# keeps this digest in sync with the UV_VERSION pin in .gitlab-ci.yml
# (see customManagers in renovate.json).
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv

# Copy ONLY the dependency manifests first so this expensive layer is
# cached and reused whenever the lockfile is unchanged. Subsequent code
# edits (entrypoint.py, vendor-scripts/, README) will not bust this layer.
COPY pyproject.toml uv.lock ./

# Resolve the pre-locked dependency set into a pinned requirements file,
# then install it into a staging directory. --no-emit-project excludes
# the application itself (it is not an importable package; only its
# declared dependencies are needed).
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    uv pip install \
        --no-cache \
        --system \
        --target /opt/site-packages \
        -r /tmp/requirements.txt

# ---------- Stage 2: distroless runtime image ----------
# gcr.io/distroless/python3-debian13 contains only the Python interpreter
# and its core C libraries - no shell, no package manager, minimal attack
# surface. The default (non-:nonroot) tag runs as root (UID 0), which is
# needed to chmod/delete backup files owned by the SFTP user on the host.
FROM gcr.io/distroless/python3-debian13:latest@sha256:3f056d8b0189540a15b5a3a125cfd28fb0960af0af2c56cf8bcec613fd771a00

# Build-time metadata consumed by LABEL/ENV below. BASE_VERSION is read
# from pyproject.toml by the CI pipeline (see .gitlab-ci.yml); GIT_HASH
# is the short commit SHA.
ARG GIT_HASH=unknown
ARG BASE_VERSION=0.1.0

# NSX major version this image is built for. Selects which vendor-scripts/
# subfolder is shipped and is baked into the ENV below so the wrapper picks
# the matching backup-instance markers. The CI pipeline builds one image per
# vendor-scripts/<version> folder and always passes this explicitly (see
# .gitlab-ci.yml); the default only helps bare local `docker build` runs and
# points at the highest supported version (the one tagged `latest`).
ARG NSX_VERSION=4.2

WORKDIR /app

# Pre-built site-packages from the builder stage. Cached until the
# dependency manifest changes upstream.
COPY --from=builder /opt/site-packages /usr/lib/python3/dist-packages

# Vendor script (rarely changes) kept on its own layer so iterating on
# wrapper/app metadata does not re-copy it. Only the selected NSX version's
# folder is shipped, flattened to /app/vendor-scripts/ so the wrapper's
# CLEANER_SCRIPT path stays version-independent. Project memory: must not be
# modified - see CONTRIBUTING.md.
COPY vendor-scripts/${NSX_VERSION}/ ./vendor-scripts/

# Remaining application files share one layer. pyproject.toml ships here
# so entrypoint.get_app_metadata() can report the running version at
# startup; it is NOT used for runtime dependency resolution (that
# happened in the builder stage).
COPY pyproject.toml entrypoint.py README.md ./

# LABEL + ENV reference GIT_HASH which changes every commit. Placing them
# AFTER the COPYs above keeps source layers cached when only the commit
# hash differs between builds.
LABEL org.opencontainers.image.version="${BASE_VERSION}+${GIT_HASH}" \
      org.opencontainers.image.description="Cron-driven container wrapping the VMware nsx_backup_cleaner.py script for periodic cleanup of old NSX Manager backups on an SFTP target." \
      com.vmware.nsx.version="${NSX_VERSION}"

# NSX_VERSION is baked into the runtime environment so entrypoint.py selects
# the backup-instance markers matching the vendor script shipped in this image.
ENV PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    APP_GIT_HASH="${GIT_HASH}" \
    NSX_VERSION="${NSX_VERSION}"

# Backup root - bind-mount the SFTP server's NSX backup directory here.
VOLUME ["/backups"]

# Launch via the wrapper which reads SCHEDULE/RETENTION_DAYS/MIN_BACKUPS
# (and other env vars - see README) and invokes the vendor cleanup script
# either once (SCHEDULE=0) or on the configured cron schedule.
ENTRYPOINT ["python", "entrypoint.py"]
