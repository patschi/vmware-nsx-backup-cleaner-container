# vmware-nsx-backup-cleaner-container

![vmware-nsx-backup-cleaner-container logo](static/logo.png)

A small container image that runs VMware's official `nsx_backup_cleaner.py`
on a cron schedule, so old NSX Manager backups stored on your SFTP target
get cleaned up automatically.

This project was developed with the assistance of Claude and has been
manually validated against a real-world multi-instance NSX backup
layout hosted on a QNAP NAS, covering both the discovery logic and the
end-to-end cleanup behavior.

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

### Example Log Output

```text
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Starting vmware-nsx-backup-cleaner-container version 0.1.0 (commit d3d215e1)
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Starting scheduler: schedule='0 9 * * *' tz=Europe/Vienna retention_days=7 min_backups=10 dir=/backups discover_instances=True discover_once=False run_on_startup=True dry_run=False
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Performing startup discovery pass.
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Discovered 2 NSX backup instance(s) under /backups:
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO]   - /backups/nsx-at
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO]   - /backups/nsx-de
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] RUN_ON_STARTUP=true - running cleanup once immediately before entering cron loop.
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Invoking cleaner: /usr/bin/python /app/vendor-scripts/nsx_backup_cleaner.py --dir /backups/nsx-at --retention-period 7 --min-count 10
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [DEBUG] [vendor] Keeping the following backup files for folder /backups/nsx-at/inventory-summary
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [DEBUG] [vendor] /backups/nsx-at/inventory-summary/4.2.3.0.0.24866352-IPv4-xxx-192.168.0.1/inventory-2026-05-03T00_02_52UTC.json
[...]
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Cleaner finished in 0.24s with exit code 0
nsx-backup-cleaner  | 2026-05-16T15:07:24+0200 [INFO] Invoking cleaner: /usr/bin/python /app/vendor-scripts/nsx_backup_cleaner.py --dir /backups/nsx-de --retention-period 7 --min-count 10
nsx-backup-cleaner  | 2026-05-16T15:07:25+0200 [DEBUG] [vendor] Keeping the following backup files for folder /backups/nsx-de/inventory-summary
nsx-backup-cleaner  | 2026-05-16T15:07:25+0200 [DEBUG] [vendor] /backups/nsx-de/inventory-summary/4.2.3.1.0.24954571-IPv4-xxx-192.168.1.1/inventory-2026-02-25T00_31_05UTC.json
[...]
nsx-backup-cleaner  | 2026-05-16T15:07:25+0200 [INFO] Cleaner finished in 0.33s with exit code 0
nsx-backup-cleaner  | 2026-05-16T15:07:25+0200 [INFO] Next run scheduled at 2026-05-17T09:00:00+02:00
```

## Configuration

All configuration is done through environment variables. The backup root
inside the container is hardcoded to `/backups`; bind-mount your SFTP
backup directory there.

| Variable             | Default     | Description                                                                                                                                                                                                                                                    |
| -------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SCHEDULE`           | `0 3 * * *` | 5-field cron expression interpreted in the timezone given by `TZ`. Special value `0` runs the cleanup **once** and exits (useful for one-shot or external cron).                                                                                               |
| `TZ`                 | `UTC`       | IANA timezone name (e.g. `Europe/Berlin`, `America/New_York`) used to interpret `SCHEDULE`. DST transitions are handled automatically. An invalid name fails fast at startup.                                                                                  |
| `RETENTION_DAYS`     | `7`         | Days to retain a backup. Mapped to `--retention-period`.                                                                                                                                                                                                       |
| `MIN_BACKUPS`        | `10`        | Minimum number of backups always kept per folder. Mapped to `--min-count`.                                                                                                                                                                                     |
| `DISCOVER_INSTANCES` | `true`      | When `true`, scan for nested NSX backup **instance folders** and run the vendor script for each. When `false`, the backup root is used directly as the only target (legacy single-instance behavior). Accepts `true/false`, `1/0`, `yes/no`, `on/off`.         |
| `DISCOVER_ONCE`      | `false`     | When `true`, discovery runs **once at startup** and the result is reused for every cron firing. When `false`, discovery re-runs before every firing so newly-added instances are picked up automatically. No effect when `DISCOVER_INSTANCES=false`.           |
| `RUN_ON_STARTUP`     | `false`     | When `true`, run the cleanup **once immediately** at container start before entering the cron loop, then continue with the scheduled firings. Useful for a fresh sweep right after deploy without waiting for the next cron firing. Ignored when `SCHEDULE=0`. |
| `DRY_RUN`            | `false`     | When `true`, the wrapper performs discovery and command-building exactly as in a normal run but **never invokes the vendor script** - it only logs the argv it would have used. Nothing is read, chmodded, or deleted. Safe way to verify configuration.       |

## Mounts

The container only needs one mount: the NSX Manager's SFTP backup root.
That directory must contain the `cluster-node-backups/` and/or
`inventory-summary/` subfolders.

- Host path: wherever the SFTP daemon writes NSX backups (for example
  `/srv/sftp/nsx/backups`).
- Container path: `/backups` (hardcoded).
- Mode: read-write. The cleanup script does `chmod` on files and then
  deletes them with `rm`/`rmtree`.

### Multi-instance layouts

If multiple NSX Manager clusters write to the same SFTP target, you
typically end up with one folder per cluster under the backup root:

```text
/srv/sftp/nsx/backups/
├── nsx-at/                  # NSX instance A
│   ├── cluster-node-backups/
│   └── inventory-summary/
└── nsx-de/                  # NSX instance B
    ├── cluster-node-backups/
    └── inventory-summary/
```

With `DISCOVER_INSTANCES=true` (the default) the wrapper scans
`/backups` and runs the vendor cleanup script once per detected
instance. Each detected instance is logged at INFO level on every
firing so you can verify the auto-detection is matching what you
expect. Folders that don't contain `cluster-node-backups/` or
`inventory-summary/` (for example `@Recently-Snapshot/`) are skipped
because the vendor script itself would reject them anyway.

If `/backups` already contains `cluster-node-backups/` /
`inventory-summary/` directly (the legacy single-instance layout), the
wrapper detects that and uses `/backups` as the only target without
scanning further.

Set `DISCOVER_INSTANCES=false` to fully disable detection and force
the wrapper back to its original behavior of passing `/backups`
straight to the vendor script.

## Startup output

Right after the container starts, the wrapper logs:

1. The **application name and version** (parsed from the
   `pyproject.toml` shipped inside the image) plus the build-time git
   commit hash. Example:

    ```text
    [INFO] Starting vmware-nsx-backup-cleaner-container version 0.1.0 (commit abc1234)
    ```

2. The **scheduler configuration** (active cron schedule, retention,
   discovery and startup flags) on a single line for quick auditing.

3. The result of the **initial discovery pass** (when
   `DISCOVER_INSTANCES=true`). Every detected NSX backup instance
   folder is listed at INFO level so you can verify the auto-detection
   matches your expectations immediately - no need to wait for the
   first cron firing. This output happens regardless of the
   `DISCOVER_ONCE` setting.

4. If `RUN_ON_STARTUP=true`, the vendor cleanup script is invoked once
   right after the startup discovery, before the loop enters its first
   `wait_until`. After the startup pass completes, the loop continues
   with the configured cron schedule as usual.

## Timezone (`TZ`)

By default `SCHEDULE` is interpreted in UTC. Set `TZ` to any IANA
timezone name to interpret it in your local zone instead:

```yaml
environment:
  SCHEDULE: "0 3 * * *"   # 03:00 local time
  TZ: "Europe/Berlin"
```

DST transitions are handled automatically by [`zoneinfo`][zoneinfo] +
[`croniter`][croniter], so a `0 3 * * *` schedule still fires at 03:00
local time across spring/autumn clock changes. The active timezone is
logged on the "Starting scheduler" line and on every "Next run
scheduled at ..." line for easy verification.

An unknown timezone name fails fast at container start with exit
code `2`.

[zoneinfo]: https://docs.python.org/3/library/zoneinfo.html
[croniter]: https://github.com/pallets-eco/croniter

## Dry-run mode (`DRY_RUN`)

Set `DRY_RUN=true` to exercise the wrapper end-to-end without ever
deleting anything. The wrapper still:

- Validates configuration and the cron expression.
- Performs discovery and logs all detected instances.
- Builds the exact vendor argv for each instance and logs it,
  prefixed with `[DRY-RUN]`.

What it does **not** do in dry-run mode:

- Launch the `nsx_backup_cleaner.py` subprocess.
- Open, `chmod`, or `unlink` any file under `/backups`.

Useful for:

- Verifying a fresh deployment before it touches real backups.
- Reviewing the impact of a retention/min-count change.
- Capturing a "would have run" audit log for change control.

Combine with `SCHEDULE=0` for a single dry-run pass and exit, or with
`RUN_ON_STARTUP=true` to get an immediate dry-run on every container
start.

## Usage

### docker-compose.yml

```yaml
services:
  nsx-backup-cleaner:
    image: ghcr.io/patschi/vmware-nsx-backup-cleaner-container:latest
    container_name: nsx-backup-cleaner
    restart: unless-stopped
    environment:
      SCHEDULE: "0 3 * * *"      # 03:00 in the TZ below
      TZ: "UTC"                  # any IANA name, e.g. "Europe/Berlin"
      RETENTION_DAYS: "7"
      MIN_BACKUPS: "10"
      DISCOVER_INSTANCES: "true" # auto-detect nested NSX instance folders
      DISCOVER_ONCE: "false"     # re-detect before every firing
      RUN_ON_STARTUP: "false"    # set "true" to also clean up right at container start
      DRY_RUN: "false"           # set "true" to log what would run without invoking the vendor
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
docker build -t vmware-nsx-backup-cleaner-container:latest .
```

The `Dockerfile` uses a two-stage build:

1. A `python:3.13-slim-trixie` stage with `uv` installs `croniter` into
   a staging directory from the pinned `uv.lock`.
2. The final `gcr.io/distroless/python3-debian13` stage copies the
   interpreter's `dist-packages`, the vendor script, the wrapper, and
   this README.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup, testing
(`uv run pytest`), and the rule that the vendor script under `vendor-scripts/`
must never be modified.

## License

The vendor script `vendor-scripts/nsx_backup_cleaner.py` is the property of
VMware / Broadcom and is included here as shipped on the NSX Manager
appliance. The wrapper code and packaging in this repository are
released under GPL-3.0-or-later.
