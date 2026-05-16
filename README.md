# nsx-backup-cleaner-container

![nsx-backup-cleaner-container logo](static/logo.png)

A small container image that runs VMware's official `nsx_backup_cleaner.py`
on a cron schedule, so old NSX Manager backups stored on your SFTP target
get cleaned up automatically.

The vendor script is shipped verbatim from a VMware NSX Manager appliance
(`/var/vmware/nsx/file-store/nsx_backup_cleaner.py`) and is **not modified**
in this repository. A small Python wrapper (`entrypoint.py`) handles cron
parsing, environment variables, and logging, then invokes the vendor script
as a subprocess.

Vendor documentation:
[Removing old backups (Broadcom TechDocs)][vendor-docs].

[vendor-docs]: https://techdocs.broadcom.com/us/en/vmware-cis/nsx/vmware-nsx/4-1/administration-guide/backing-up-and-restoring-the-nsx-manager/removing-old-backups.html

## What it does

NSX Manager uploads backups to the SFTP server under two subfolders:

- `cluster-node-backups/`
- `inventory-summary/`

Inside each, every backup is its own dated subdirectory. Over time these
accumulate, and the vendor script `nsx_backup_cleaner.py` is the
supported way to delete old ones. It keeps any backup younger than the
retention window and never deletes below a per-folder minimum count.
This container schedules that script for you.

## Configuration

All configuration is done through environment variables. The backup root
inside the container is hardcoded to `/backups`; bind-mount your SFTP
backup directory there.

| Variable         | Default     | Description                                                                                                                   |
| ---------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `SCHEDULE`       | `0 3 * * *` | 5-field cron expression in UTC. Special value `0` runs the cleanup **once** and exits (useful for one-shot or external cron). |
| `RETENTION_DAYS` | `7`         | Days to retain a backup. Mapped to `--retention-period`.                                                                      |
| `MIN_BACKUPS`    | `10`        | Minimum number of backups always kept per folder. Mapped to `--min-count`.                                                    |

## Mounts

The container only needs one mount: the NSX Manager's SFTP backup root.
That directory must contain the `cluster-node-backups/` and/or
`inventory-summary/` subfolders.

- Host path: wherever the SFTP daemon writes NSX backups (for example
  `/srv/sftp/nsx/backups`).
- Container path: `/backups` (hardcoded).
- Mode: read-write. The cleanup script does `chmod` on files and then
  deletes them with `rm`/`rmtree`.

## Usage

### docker-compose.yml

```yaml
services:
  nsx-backup-cleaner:
    image: ghcr.io/patschi/vmware-nsx-backup-cleaner-container:latest
    container_name: nsx-backup-cleaner
    restart: unless-stopped
    read_only: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    environment:
      SCHEDULE: "0 3 * * *"     # 03:00 UTC daily
      RETENTION_DAYS: "7"
      MIN_BACKUPS: "10"
    volumes:
      - /srv/sftp/nsx/backups:/backups
```

Hardening explained:

- `read_only: true` makes the container's root filesystem immutable; the
  only writable path is the `/backups` bind mount.
- `security_opt: ["no-new-privileges:true"]` prevents the container
  process (and any child it spawns) from gaining new privileges via
  setuid/setgid binaries.
- `cap_drop: [ALL]` strips every Linux capability. The cleanup workload
  only reads, `chmod`s and unlinks files inside a mounted directory and
  needs no capabilities.

### One-time run

Set `SCHEDULE=0` to run the cleanup once and exit. This is useful for
manual cleanups, ad-hoc verification, or when an external scheduler
(systemd timer, host cron, Kubernetes `CronJob`) is already in charge of
when to run.

```sh
docker run --rm \
    --read-only \
    --security-opt=no-new-privileges \
    --cap-drop=ALL \
    -e SCHEDULE=0 \
    -e RETENTION_DAYS=7 \
    -e MIN_BACKUPS=10 \
    -v /srv/sftp/nsx/backups:/backups \
    ghcr.io/patschi/vmware-nsx-backup-cleaner-container:latest
```

## Container user (UID)

The image runs as **root (UID 0)** by default. NSX uploads backups over
SFTP under whatever UID the SFTP daemon assigned (often a dedicated
service user), and the vendor script needs to `chmod` and delete those
files. Running as root is the simplest way to avoid ownership mismatches
on a bind mount.

If you prefer to run as a non-root UID, you must ensure the mounted
backup directory and everything inside it is writable by that UID. Then
override at runtime:

```sh
docker run --user 1000:1000 ... ghcr.io/patschi/vmware-nsx-backup-cleaner-container:latest
```

or in compose:

```yaml
services:
  nsx-backup-cleaner:
    user: "1000:1000"
    # ... rest of config
```

## Build

```sh
docker build -t nsx-backup-cleaner-container:latest .
```

The `Dockerfile` uses a two-stage build:

1. A `python:3.13-slim-trixie` stage with `uv` installs `croniter` into
   a staging directory from the pinned `uv.lock`.
2. The final `gcr.io/distroless/python3-debian13` stage copies the
   interpreter's `dist-packages`, the vendor script, the wrapper, and
   this README.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup, testing
(`uv run pytest`), and the rule that the vendor script under `scripts/`
must never be modified.

## License

The vendor script `scripts/nsx_backup_cleaner.py` is the property of
VMware / Broadcom and is included here as shipped on the NSX Manager
appliance. The wrapper code and packaging in this repository are
released under GPL-3.0-or-later.
