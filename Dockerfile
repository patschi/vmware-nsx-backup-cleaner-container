# nsx-backup-cleaner-container - distroless container image.
#
# Multi-stage build:
#   1. Builder stage:  resolves and installs croniter into a staging
#                      directory using uv + the pinned uv.lock.
#   2. Final stage:    Google distroless Debian 13 Python image with the
#                      interpreter, croniter, the vendor cleanup script,
#                      the cron-driven wrapper, and the README.
#
# Layer ordering is tuned for cache reuse: dependency manifests are
# copied and installed BEFORE any application source, so iterating on
# entrypoint.py or the vendor script never invalidates the (slow) deps
# layer. Source files are copied last and in roughly stable-to-volatile
# order: vendor script (rarely changes) -> wrapper -> README.
#
# Runs as root (UID 0) by default so it can chmod/delete files that NSX
# uploaded over SFTP under whatever UID the SFTP daemon assigned. See
# README.md for instructions on switching to a non-root UID.

# ---------- Stage 1: build dependencies in a full Python image ----------
FROM python:3.13-slim-trixie@sha256:dc1546eefcbe8caaa1f004f16ab76b204b5e1dbd58ff81b899f21cd40541232f AS builder

WORKDIR /build

# Copy uv binary from the official image. Pinned to a specific tag + SHA
# for reproducible builds. Renovate keeps this digest in sync with the
# UV_VERSION pin in .gitlab-ci.yml (see customManagers in renovate.json).
COPY --from=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 /uv /usr/local/bin/uv

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
# Tip: pin the SHA digest once chosen, e.g.
#   FROM gcr.io/distroless/python3-debian13:latest@sha256:<digest>
FROM gcr.io/distroless/python3-debian13:latest

# Build-time metadata. BASE_VERSION is read from pyproject.toml by the CI
# pipeline (see .gitlab-ci.yml) and GIT_HASH is the short commit SHA.
ARG GIT_HASH=unknown
ARG BASE_VERSION=0.1.0
LABEL org.opencontainers.image.version="${BASE_VERSION}+${GIT_HASH}"
LABEL org.opencontainers.image.description="Cron-driven container wrapping the VMware nsx_backup_cleaner.py script for periodic cleanup of old NSX Manager backups on an SFTP target."

WORKDIR /app

# Copy installed packages into the version-independent dist-packages
# directory that is on every Debian Python's default sys.path. This
# layer is cached until the dependency manifest changes upstream.
COPY --from=builder /opt/site-packages /usr/lib/python3/dist-packages

# Python interpreter behavior and build metadata. Application defaults
# (SCHEDULE / RETENTION_DAYS / MIN_BACKUPS) live in entrypoint.py as the
# single source of truth and are intentionally NOT redeclared here.
ENV PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    APP_GIT_HASH="${GIT_HASH}"

# Copy application content last and in stable-to-volatile order so the
# most frequently edited files invalidate the smallest number of layers.
# The vendor script must not be modified - see project memory.
COPY vendor-scripts/ ./vendor-scripts/
COPY entrypoint.py ./
COPY README.md ./

# Backup root - bind-mount the SFTP server's NSX backup directory here.
VOLUME ["/backups"]

# Launch via the wrapper which reads SCHEDULE/RETENTION_DAYS/MIN_BACKUPS
# from the environment and invokes vendor-scripts/nsx_backup_cleaner.py either
# once (SCHEDULE=0) or on the configured cron schedule.
ENTRYPOINT ["python", "entrypoint.py"]
