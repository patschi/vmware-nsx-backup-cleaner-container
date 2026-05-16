"""Cron-driven wrapper around the vendor ``nsx_backup_cleaner.py`` script.

This module is the container entrypoint. It reads three environment
variables, then either runs the vendor cleanup script once (when
``SCHEDULE="0"``) or loops forever, sleeping until the next cron firing
time computed by :mod:`croniter`.

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
NSX backup root there. The vendor script ``scripts/nsx_backup_cleaner.py``
is invoked unchanged as a subprocess and its stdout/stderr stream through
this wrapper's standard streams.
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
CLEANER_SCRIPT = "/app/scripts/nsx_backup_cleaner.py"

# Default schedule fires once a day at 03:00 UTC.
DEFAULT_SCHEDULE = "0 3 * * *"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MIN_BACKUPS = 10

# Sentinel value for SCHEDULE that means "run once and exit" instead of
# entering the cron loop. Picked because it is unambiguously not a valid
# 5-field cron expression.
ONE_SHOT_SENTINEL = "0"

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


def read_config() -> tuple[str, int, int]:
    """Read SCHEDULE, RETENTION_DAYS and MIN_BACKUPS from the environment.

    Returns:
        A 3-tuple ``(schedule, retention_days, min_backups)`` populated
        from the matching environment variables, falling back to the
        module-level defaults when a variable is unset.

    Raises:
        ValueError: If RETENTION_DAYS or MIN_BACKUPS is not a positive
            integer.
    """
    schedule = os.environ.get("SCHEDULE", DEFAULT_SCHEDULE)
    retention_days = int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    min_backups = int(os.environ.get("MIN_BACKUPS", DEFAULT_MIN_BACKUPS))
    if retention_days <= 0:
        raise ValueError(
            f"RETENTION_DAYS must be a positive integer, got {retention_days!r}"
        )
    if min_backups <= 0:
        raise ValueError(f"MIN_BACKUPS must be a positive integer, got {min_backups!r}")
    return schedule, retention_days, min_backups


def run_cleaner(retention_days: int, min_backups: int) -> int:
    """Invoke the vendor cleanup script once and return its exit code.

    Args:
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.

    Returns:
        The vendor script's exit code (``0`` on success).
    """
    # Build argv for the vendor script. --dir is hardcoded and not user-configurable.
    cmd = [
        sys.executable,
        CLEANER_SCRIPT,
        "--dir",
        BACKUP_DIR,
        "--retention-period",
        str(retention_days),
        "--min-count",
        str(min_backups),
    ]
    logging.info("Invoking cleaner: %s", " ".join(cmd))
    started = time.monotonic()
    # capture_output=False so the vendor script's prints stream live to our stdout.
    result = subprocess.run(cmd, check=False)
    duration = time.monotonic() - started
    logging.info(
        "Cleaner finished in %.2fs with exit code %d", duration, result.returncode
    )
    return result.returncode


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


def run_loop(schedule: str, retention_days: int, min_backups: int) -> None:
    """Run the cleanup on the given cron schedule until shutdown.

    Validates the cron expression up front so a typo fails fast on
    container start rather than after the first wait interval. Exits
    cleanly when ``_stop_event`` is set by the signal handler; an
    in-flight cleanup subprocess is allowed to finish first.

    Args:
        schedule: 5-field cron expression interpreted in UTC.
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.
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
        "Starting scheduler: schedule=%r retention_days=%d min_backups=%d dir=%s",
        schedule,
        retention_days,
        min_backups,
        BACKUP_DIR,
    )
    while not _stop_event.is_set():
        next_fire_at = itr.get_next(datetime)
        logging.info("Next run scheduled at %s", next_fire_at.isoformat())
        if wait_until(next_fire_at):
            # Shutdown signal arrived during the wait - exit without firing.
            break
        run_cleaner(retention_days, min_backups)
    logging.info("Scheduler stopped - graceful shutdown complete.")


def main() -> None:
    """Entrypoint: dispatch to one-shot mode or the scheduling loop."""
    setup_logging()
    install_signal_handlers()

    try:
        schedule, retention_days, min_backups = read_config()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(2)

    # SCHEDULE="0" is the documented one-shot mode: run once and exit.
    if schedule.strip() == ONE_SHOT_SENTINEL:
        logging.info(
            "SCHEDULE=%s detected - running once and exiting.", ONE_SHOT_SENTINEL
        )
        exit_code = run_cleaner(retention_days, min_backups)
        sys.exit(exit_code)

    run_loop(schedule, retention_days, min_backups)


if __name__ == "__main__":
    main()
