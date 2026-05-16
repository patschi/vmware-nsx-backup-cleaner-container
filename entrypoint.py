"""Cron-driven wrapper around the vendor ``nsx_backup_cleaner.py`` script.

This module is the container entrypoint. It reads the configuration from
the environment, optionally discovers one or more NSX backup instance
folders under the backup root, then either runs the vendor cleanup
script once (when ``SCHEDULE="0"``) or loops forever, sleeping until the
next cron firing time computed by :mod:`croniter`.

Environment variables
---------------------
SCHEDULE
    5-field cron expression evaluated in UTC. Defaults to ``"0 3 * * *"``
    (03:00 UTC). The special value ``"0"`` runs the cleanup once and
    exits, intended for one-shot invocations or when an external
    scheduler (host cron, systemd timer, Kubernetes ``CronJob``) is
    already responsible for timing.
RETENTION_DAYS
    Days to retain a backup. Forwarded to the vendor script as
    ``--retention-period``. Must be a positive integer. Defaults to ``7``.
MIN_BACKUPS
    Minimum number of backups always kept per folder, regardless of age.
    Forwarded to the vendor script as ``--min-count``. Must be a positive
    integer. Defaults to ``10``.
DISCOVER_INSTANCES
    Boolean (``true``/``false``). When ``true`` (the default), scan the
    backup root for nested NSX backup instance folders and run the
    vendor script for each. When ``false``, the backup root is used
    directly as the only target (original single-instance behavior).
DISCOVER_ONCE
    Boolean (``true``/``false``). When ``true``, discovery runs exactly
    once at startup and the result is reused for every subsequent cron
    firing. When ``false`` (the default), discovery runs again before
    each firing so newly-added instances are picked up automatically.
    Has no effect when ``DISCOVER_INSTANCES=false``.

Signal handling
---------------
``SIGTERM`` and ``SIGINT`` are caught and trigger a graceful shutdown:
the scheduler stops waiting for the next cron firing and exits with
code ``0``. If a cleanup subprocess is mid-run when the signal arrives,
it is allowed to finish (vendor script runs are typically seconds and
should not be interrupted in the middle of a delete).

Notes
-----
The backup directory is hardcoded to ``/backups``; bind-mount the SFTP
NSX backup root there. The vendor script ``vendor-scripts/nsx_backup_cleaner.py``
is invoked unchanged as a subprocess. Its stdout/stderr are captured
and emitted line-by-line at DEBUG level so the vendor output remains
visible through the wrapper's logger.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from croniter import croniter

# Hardcoded backup root - mount the NSX SFTP backup directory here.
BACKUP_DIR = "/backups"

# Path to the vendor cleanup script (copied verbatim from the NSX Manager appliance).
CLEANER_SCRIPT = "/app/vendor-scripts/nsx_backup_cleaner.py"

# Default schedule fires once a day at 03:00 UTC.
DEFAULT_SCHEDULE = "0 3 * * *"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MIN_BACKUPS = 10
DEFAULT_DISCOVER_INSTANCES = True
DEFAULT_DISCOVER_ONCE = False

# Sentinel value for SCHEDULE that means "run once and exit" instead of
# entering the cron loop. Picked because it is unambiguously not a valid
# 5-field cron expression.
ONE_SHOT_SENTINEL = "0"

# Subfolder names the vendor script treats as valid NSX backup categories.
# A directory is considered an "NSX backup instance" if at least one of
# these markers exists as a subdirectory inside it. Mirrors the
# eligibility check in vendor `nsx_backup_cleaner.py::main` so the
# wrapper never hands the vendor a directory it would reject.
NSX_INSTANCE_MARKERS = ("cluster-node-backups", "inventory-summary")

# Shutdown signal. Set by the SIGTERM/SIGINT handler and observed by the
# scheduler loop. Using threading.Event lets the loop block in a single
# kernel wait (Event.wait(timeout=...)) instead of chunked time.sleep
# calls, and gives signals an immediate wake-up.
_stop_event = threading.Event()


def setup_logging() -> None:
    """Configure root logging at DEBUG level on stdout.

    Verbose logging is mandatory; there is intentionally no environment
    variable or CLI flag to lower the verbosity.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )


def _handle_shutdown_signal(signum: int, _frame: object) -> None:
    """Signal handler that requests a graceful shutdown.

    Sets the module-level ``_stop_event`` so the scheduler loop wakes up
    from its ``Event.wait`` and exits cleanly. Safe to call multiple
    times - the event is idempotent.
    """
    name = signal.Signals(signum).name
    logging.info("Received %s - shutting down after current work completes.", name)
    _stop_event.set()


def install_signal_handlers() -> None:
    """Install SIGTERM and SIGINT handlers for graceful shutdown.

    Replaces Python's default SIGTERM disposition (immediate
    terminate) with one that flips ``_stop_event`` so the main loop
    can exit deterministically and log a shutdown message.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


# Accepted truthy/falsy string forms for boolean-typed env vars. Compared
# case-insensitively after a strip(). Kept conservative: no surprises
# like "y"/"n" or "enable"/"disable".
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def _parse_bool_env(var_name: str, raw_value: str) -> bool:
    """Parse a boolean-style env var value.

    Accepts a small, explicit allowlist (``1/0``, ``true/false``,
    ``yes/no``, ``on/off``) case-insensitively. Anything else raises so
    the user finds out at startup instead of silently getting a
    default.
    """
    normalized = raw_value.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    raise ValueError(
        f"{var_name} must be a boolean (one of: "
        f"{sorted(_TRUE_STRINGS | _FALSE_STRINGS)}), got {raw_value!r}"
    )


def read_config() -> tuple[str, int, int, bool, bool]:
    """Read all wrapper configuration from the environment.

    Returns:
        A 5-tuple
        ``(schedule, retention_days, min_backups,
        discover_instances_enabled, discover_once)`` populated from the
        matching environment variables, falling back to the
        module-level defaults when a variable is unset.

    Raises:
        ValueError: If RETENTION_DAYS or MIN_BACKUPS is not a positive
            integer, or if DISCOVER_INSTANCES/DISCOVER_ONCE is not a
            recognized boolean.
    """
    schedule = os.environ.get("SCHEDULE", DEFAULT_SCHEDULE)
    retention_days = int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    min_backups = int(os.environ.get("MIN_BACKUPS", DEFAULT_MIN_BACKUPS))

    # Boolean env vars - parse via the strict allowlist parser so typos fail fast.
    discover_raw = os.environ.get("DISCOVER_INSTANCES", None)
    discover_instances_enabled = (
        DEFAULT_DISCOVER_INSTANCES
        if discover_raw is None
        else _parse_bool_env("DISCOVER_INSTANCES", discover_raw)
    )
    once_raw = os.environ.get("DISCOVER_ONCE", None)
    discover_once = (
        DEFAULT_DISCOVER_ONCE
        if once_raw is None
        else _parse_bool_env("DISCOVER_ONCE", once_raw)
    )

    if retention_days <= 0:
        raise ValueError(
            f"RETENTION_DAYS must be a positive integer, got {retention_days!r}"
        )
    if min_backups <= 0:
        raise ValueError(f"MIN_BACKUPS must be a positive integer, got {min_backups!r}")
    return (
        schedule,
        retention_days,
        min_backups,
        discover_instances_enabled,
        discover_once,
    )


def is_nsx_instance_dir(path: str) -> bool:
    """Return True if ``path`` looks like a valid NSX backup instance folder.

    Mirrors the eligibility check from vendor ``nsx_backup_cleaner.py``:
    the vendor script only processes a directory if it contains at
    least one of the marker subfolders (``cluster-node-backups`` or
    ``inventory-summary``). Anything else would print
    "Cleanup script works only in folders..." and exit without doing
    work, so the wrapper filters those out up front.
    """
    if not os.path.isdir(path):
        return False
    for marker in NSX_INSTANCE_MARKERS:
        # Vendor only checks names via os.listdir; we additionally
        # require the marker to be a directory so we never feed the
        # vendor a folder it would later choke on.
        if os.path.isdir(os.path.join(path, marker)):
            return True
    return False


def discover_instances(root: str) -> list[str]:
    """Discover NSX backup instance folders under ``root``.

    Two-tier strategy that preserves backward compatibility:

    1. If ``root`` itself qualifies as an NSX instance, return ``[root]``.
       This is the legacy single-instance mount layout.
    2. Otherwise, scan depth-1 children of ``root`` and return every
       child that qualifies. This is the multi-instance layout
       (e.g. ``/backups/nsx-at`` and ``/backups/nsx-de`` sitting
       alongside vendor noise like ``@Recently-Snapshot``).

    Results are sorted by path for deterministic ordering across runs,
    which keeps logs/tests stable.
    """
    # Legacy single-instance layout: backup root is itself an NSX instance.
    if is_nsx_instance_dir(root):
        return [root]

    instances: list[str] = []
    try:
        # Walk only depth-1 entries; deeper nesting is not a supported layout.
        children = sorted(os.listdir(root))
    except OSError as exc:
        logging.error("Cannot list backup root %s: %s", root, exc)
        return instances

    for elem in children:
        candidate = os.path.join(root, elem)
        if is_nsx_instance_dir(candidate):
            instances.append(candidate)
    return instances


def resolve_targets(discover_enabled: bool) -> list[str]:
    """Return the list of directories the cleanup should run against.

    When discovery is disabled the wrapper preserves the original
    behavior of running the vendor script directly against
    ``BACKUP_DIR``. When discovery is enabled every detected instance
    is logged so the operator can see at a glance which folders the
    next firing will touch.
    """
    if not discover_enabled:
        # Discovery off: forward BACKUP_DIR straight to the vendor script,
        # preserving pre-discovery behavior for single-mount setups.
        return [BACKUP_DIR]

    instances = discover_instances(BACKUP_DIR)
    if not instances:
        # Discovery on but nothing matched - surface this loudly so a
        # misconfigured mount or empty backup root is immediately obvious.
        logging.warning(
            "Discovery enabled but no NSX backup instance folders found under %s",
            BACKUP_DIR,
        )
        return []

    # Log every discovered instance so operators can audit what will be cleaned.
    logging.info(
        "Discovered %d NSX backup instance(s) under %s:", len(instances), BACKUP_DIR
    )
    for inst in instances:
        logging.info("  - %s", inst)
    return instances


def run_cleaner(directory: str, retention_days: int, min_backups: int) -> int:
    """Invoke the vendor cleanup script once against ``directory`` and return its exit code.

    Captures the vendor script's stdout+stderr and re-emits each line
    through :mod:`logging` at DEBUG level. This keeps the vendor output
    available for troubleshooting without polluting the wrapper's
    INFO-level summary lines.

    Args:
        directory: Absolute path forwarded as ``--dir``.
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.

    Returns:
        The vendor script's exit code (``0`` on success).
    """
    # Build argv for the vendor script. --dir is supplied by the caller so
    # multi-instance dispatch can target a different folder per call.
    cmd = [
        sys.executable,
        CLEANER_SCRIPT,
        "--dir",
        directory,
        "--retention-period",
        str(retention_days),
        "--min-count",
        str(min_backups),
    ]
    logging.info("Invoking cleaner: %s", " ".join(cmd))
    started = time.monotonic()
    # Capture both streams and merge stderr into stdout so the relative
    # ordering of any vendor prints/errors is preserved in the log.
    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Emit each captured line at DEBUG so vendor output is visible without
    # cluttering INFO logs. Strip the trailing newline that splitlines()
    # already discards; empty lines from the vendor script are kept so
    # the operator sees exactly what the vendor printed.
    for line in (result.stdout or "").splitlines():
        logging.debug("[vendor] %s", line)
    duration = time.monotonic() - started
    logging.info(
        "Cleaner finished in %.2fs with exit code %d", duration, result.returncode
    )
    return result.returncode


def run_cleaner_for_all(
    targets: list[str], retention_days: int, min_backups: int
) -> int:
    """Run the vendor cleanup script once per target directory.

    Iterates ``targets`` in order, invoking :func:`run_cleaner` for each.
    Continues through the full list even if one instance fails so a
    single broken folder cannot block cleanup of the others. The
    returned exit code is the worst (largest) non-zero return code
    seen, mirroring how shell pipelines surface partial failures.
    """
    worst_rc = 0
    for target in targets:
        # Run each instance independently; do not short-circuit on failure.
        rc = run_cleaner(target, retention_days, min_backups)
        # worst_rc starts at 0, so `rc > worst_rc` already excludes successful runs.
        if rc > worst_rc:
            worst_rc = rc
    return worst_rc


def wait_until(next_fire_at: datetime) -> bool:
    """Block until ``next_fire_at`` arrives or a shutdown signal fires.

    Uses a single ``threading.Event.wait`` so the kernel can wake us
    instantly on signal delivery, no polling needed.

    Args:
        next_fire_at: Timezone-aware UTC datetime to wake up at.

    Returns:
        ``True`` if the wait was interrupted by a shutdown signal,
        ``False`` if the timeout elapsed normally.
    """
    remaining = (next_fire_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return _stop_event.is_set()
    return _stop_event.wait(timeout=remaining)


def run_loop(
    schedule: str,
    retention_days: int,
    min_backups: int,
    discover_enabled: bool,
    discover_once: bool,
) -> None:
    """Run the cleanup on the given cron schedule until shutdown.

    Validates the cron expression up front so a typo fails fast on
    container start rather than after the first wait interval. Exits
    cleanly when ``_stop_event`` is set by the signal handler; an
    in-flight cleanup subprocess is allowed to finish first.

    Args:
        schedule: 5-field cron expression interpreted in UTC.
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.
        discover_enabled: When True, scan the backup root for nested
            NSX instances; when False, target ``BACKUP_DIR`` directly.
        discover_once: When True, run discovery exactly once at startup
            and reuse the result for every firing; when False, rerun
            discovery before each firing so new instances are detected
            without restarting the container.
    """
    # Construct croniter directly and let its ValueError serve as the
    # validation step; avoids the separate is_valid() pass that would
    # parse the same expression twice.
    try:
        itr = croniter(schedule, datetime.now(timezone.utc))
    except (ValueError, KeyError) as exc:
        logging.error("Invalid cron expression %r: %s", schedule, exc)
        sys.exit(2)

    logging.info(
        "Starting scheduler: schedule=%r retention_days=%d min_backups=%d "
        "dir=%s discover_instances=%s discover_once=%s",
        schedule,
        retention_days,
        min_backups,
        BACKUP_DIR,
        discover_enabled,
        discover_once,
    )

    # Cache of resolved targets when DISCOVER_ONCE is true. Populated at
    # startup so the discovery cost (and the listing log) happens once
    # and every cron firing reuses the same list. When DISCOVER_ONCE is
    # false this stays None and we resolve fresh on every firing.
    cached_targets: list[str] | None = None
    if discover_enabled and discover_once:
        logging.info("DISCOVER_ONCE=true - performing one-time discovery at startup.")
        cached_targets = resolve_targets(discover_enabled=True)

    while not _stop_event.is_set():
        next_fire_at = itr.get_next(datetime)
        logging.info("Next run scheduled at %s", next_fire_at.isoformat())
        if wait_until(next_fire_at):
            # Shutdown signal arrived during the wait - exit without firing.
            break

        # Resolve which directories to clean for this firing. Use the
        # startup cache when DISCOVER_ONCE is set, otherwise rediscover
        # so new instance folders show up without a container restart.
        if cached_targets is not None:
            targets = cached_targets
        else:
            targets = resolve_targets(discover_enabled)

        if not targets:
            # Either discovery returned nothing or BACKUP_DIR was empty;
            # skip this firing and wait for the next scheduled time.
            logging.warning("No cleanup targets for this run - skipping.")
            continue

        run_cleaner_for_all(targets, retention_days, min_backups)
    logging.info("Scheduler stopped - graceful shutdown complete.")


def main() -> None:
    """Entrypoint: dispatch to one-shot mode or the scheduling loop."""
    setup_logging()
    install_signal_handlers()

    try:
        (
            schedule,
            retention_days,
            min_backups,
            discover_enabled,
            discover_once,
        ) = read_config()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(2)

    # SCHEDULE="0" is the documented one-shot mode: run once and exit.
    if schedule.strip() == ONE_SHOT_SENTINEL:
        logging.info(
            "SCHEDULE=%s detected - running once and exiting.", ONE_SHOT_SENTINEL
        )
        # One-shot still honors discovery so the same multi-instance
        # layout works for ad-hoc runs. DISCOVER_ONCE has no meaningful
        # effect here since there is only one firing.
        targets = resolve_targets(discover_enabled)
        if not targets:
            logging.error("No cleanup targets resolved - exiting with code 2.")
            sys.exit(2)
        exit_code = run_cleaner_for_all(targets, retention_days, min_backups)
        sys.exit(exit_code)

    run_loop(schedule, retention_days, min_backups, discover_enabled, discover_once)


if __name__ == "__main__":
    main()
