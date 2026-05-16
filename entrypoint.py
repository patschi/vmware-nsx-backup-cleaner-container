"""Cron-driven wrapper around the vendor ``nsx_backup_cleaner.py`` script.

This module is the container entrypoint. It reads the configuration from
the environment, optionally discovers one or more NSX backup instance
folders under the backup root, then either runs the vendor cleanup
script once (when ``SCHEDULE="0"``) or loops forever, sleeping until the
next cron firing time computed by :mod:`croniter`.

Environment variables
---------------------
SCHEDULE
    5-field cron expression evaluated in the timezone given by ``TZ``
    (default UTC). Defaults to ``"0 3 * * *"`` (03:00 in the configured
    zone). The special value ``"0"`` runs the cleanup once and exits,
    intended for one-shot invocations or when an external scheduler
    (host cron, systemd timer, Kubernetes ``CronJob``) is already
    responsible for timing.
TZ
    IANA timezone name (e.g. ``UTC``, ``Europe/Berlin``,
    ``America/New_York``) used to interpret ``SCHEDULE``. Defaults to
    ``UTC``. DST transitions are handled automatically by zoneinfo +
    croniter. An invalid timezone name fails fast at startup.
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
RUN_ON_STARTUP
    Boolean (``true``/``false``). When ``true``, the wrapper runs the
    vendor cleanup script once immediately at container start before
    entering the cron loop, then continues with the normal schedule.
    Useful for operators who want a fresh cleanup right after deploy
    without waiting for the next cron firing. Defaults to ``false`` so
    existing deployments keep their current "wait for cron" behavior.
    Ignored in one-shot mode (``SCHEDULE="0"``) since the only firing
    *is* the startup firing.
DRY_RUN
    Boolean (``true``/``false``). When ``true``, the wrapper performs
    discovery, scheduling, and command-building exactly as in a normal
    run but the vendor subprocess is never launched - the wrapper only
    logs the argv it would have used. Nothing is read, chmodded, or
    deleted on the filesystem. Defaults to ``false``.

Startup output
--------------
Regardless of ``DISCOVER_ONCE``, the wrapper performs a discovery pass
at startup whenever ``DISCOVER_INSTANCES=true`` and logs every detected
instance. This gives operators immediate visibility into what the next
cron firing will touch instead of having to wait until the first
firing for the discovery log lines to appear.

The wrapper also logs the application name and version (parsed from
the ``pyproject.toml`` shipped inside the image) plus the build-time
git commit hash (``APP_GIT_HASH``) as the very first log line, so the
image identity is unambiguous in the container logs.

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
import tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
# Default for RUN_ON_STARTUP is False so existing deployments keep their
# pre-feature behavior of only firing on the cron schedule. Operators who
# want a fresh cleanup right after deploy flip this to true explicitly.
DEFAULT_RUN_ON_STARTUP = False
# Default for DRY_RUN is False: actually run the vendor script. DRY_RUN=true
# is opt-in for safe verification (logs the exact command that WOULD have
# been invoked but skips the subprocess - the wrapper never deletes anything).
DEFAULT_DRY_RUN = False
# Default cron timezone is UTC, matching the original behavior. Set the TZ
# env var (any IANA name such as "Europe/Berlin" or "America/New_York") to
# interpret SCHEDULE in a different zone. DST transitions are handled by
# zoneinfo + croniter automatically.
DEFAULT_TZ = "UTC"

# Path to the pyproject.toml shipped inside the container alongside this
# entrypoint. Used by get_app_metadata() to log the app name+version at
# startup so the container's identity (which version is actually running)
# is visible in the logs without relying on the image tag alone.
PYPROJECT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pyproject.toml"
)

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


def get_app_metadata() -> tuple[str, str]:
    """Return ``(name, version)`` parsed from the shipped ``pyproject.toml``.

    ``pyproject.toml`` is copied into the container image alongside
    ``entrypoint.py`` (see Dockerfile) so the running container can
    identify itself in its own logs without relying on the image tag or
    a separate build-time env var. Falls back to
    ``("unknown", "unknown")`` if the file is missing or malformed - the
    wrapper should not refuse to start just because version metadata is
    unavailable.
    """
    try:
        # Open in binary mode as required by tomllib.
        with open(PYPROJECT_PATH, "rb") as fh:
            data = tomllib.load(fh)
        # The [project] table holds PEP 621 metadata. Use explicit defaults
        # so a partially-malformed file still yields something printable.
        project = data.get("project", {})
        return (
            project.get("name", "unknown"),
            project.get("version", "unknown"),
        )
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # A missing or broken pyproject.toml is non-fatal for the wrapper;
        # log at WARNING so the operator can investigate without the
        # container crash-looping.
        logging.warning("Could not read %s for version info: %s", PYPROJECT_PATH, exc)
        return "unknown", "unknown"


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


def _read_bool_env(name: str, default: bool) -> bool:
    """Look up ``name`` in the environment and parse it via the strict allowlist.

    Returns ``default`` when the variable is unset; otherwise delegates to
    :func:`_parse_bool_env` so typos like ``"yse"`` surface at startup
    instead of silently becoming False.
    """
    raw = os.environ.get(name, None)
    return default if raw is None else _parse_bool_env(name, raw)


def read_config() -> tuple[str, int, int, bool, bool, bool, bool, ZoneInfo]:
    """Read all wrapper configuration from the environment.

    Returns:
        An 8-tuple
        ``(schedule, retention_days, min_backups,
        discover_instances_enabled, discover_once, run_on_startup,
        dry_run, tz)`` populated from the matching environment
        variables, falling back to the module-level defaults when a
        variable is unset.

    Raises:
        ValueError: If RETENTION_DAYS or MIN_BACKUPS is not a positive
            integer, if any of the boolean env vars
            (DISCOVER_INSTANCES/DISCOVER_ONCE/RUN_ON_STARTUP/DRY_RUN) is
            not a recognized boolean, or if TZ is not a known IANA
            timezone name.
    """
    schedule = os.environ.get("SCHEDULE", DEFAULT_SCHEDULE)
    retention_days = int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    min_backups = int(os.environ.get("MIN_BACKUPS", DEFAULT_MIN_BACKUPS))

    # Boolean env vars - parse via the strict allowlist parser so typos fail fast.
    # DISCOVER_INSTANCES: scan for nested NSX instances vs. legacy single-mount.
    # DISCOVER_ONCE: cache discovery at startup vs. rediscover every firing.
    # RUN_ON_STARTUP: immediate cleanup pass right after container start.
    # DRY_RUN: log the would-be vendor argv but never invoke the subprocess.
    discover_instances_enabled = _read_bool_env(
        "DISCOVER_INSTANCES", DEFAULT_DISCOVER_INSTANCES
    )
    discover_once = _read_bool_env("DISCOVER_ONCE", DEFAULT_DISCOVER_ONCE)
    run_on_startup = _read_bool_env("RUN_ON_STARTUP", DEFAULT_RUN_ON_STARTUP)
    dry_run = _read_bool_env("DRY_RUN", DEFAULT_DRY_RUN)
    # TZ controls the timezone in which SCHEDULE is interpreted. Default is
    # UTC. Any IANA name accepted by zoneinfo works (Europe/Berlin,
    # America/New_York, ...). Invalid names fail fast at startup.
    tz_name = os.environ.get("TZ", DEFAULT_TZ)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"TZ must be a valid IANA timezone name (e.g. 'UTC', 'Europe/Berlin'), "
            f"got {tz_name!r}"
        ) from exc

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
        run_on_startup,
        dry_run,
        tz,
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


def run_cleaner(
    directory: str,
    retention_days: int,
    min_backups: int,
    dry_run: bool = False,
) -> int:
    """Invoke the vendor cleanup script once against ``directory`` and return its exit code.

    Captures the vendor script's stdout+stderr and re-emits each line
    through :mod:`logging` at DEBUG level. This keeps the vendor output
    available for troubleshooting without polluting the wrapper's
    INFO-level summary lines.

    Args:
        directory: Absolute path forwarded as ``--dir``.
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.
        dry_run: When True, skip the vendor subprocess entirely and only
            log the exact command that WOULD have been invoked. The
            wrapper never deletes or chmods anything in dry-run mode -
            useful for verifying multi-instance dispatch, discovery,
            and argument forwarding without touching the filesystem.

    Returns:
        The vendor script's exit code (``0`` on success). In dry-run
        mode, always ``0`` since no subprocess is launched.
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
    if dry_run:
        # Stop here in dry-run mode: log the would-be command at INFO so
        # the operator can verify it, then short-circuit before any
        # subprocess (and thus any filesystem mutation) can happen.
        logging.info("[DRY-RUN] Would invoke cleaner: %s", " ".join(cmd))
        return 0
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
    targets: list[str],
    retention_days: int,
    min_backups: int,
    dry_run: bool = False,
) -> int:
    """Run the vendor cleanup script once per target directory.

    Iterates ``targets`` in order, invoking :func:`run_cleaner` for each.
    Continues through the full list even if one instance fails so a
    single broken folder cannot block cleanup of the others. The
    returned exit code is the largest non-zero return code seen,
    mirroring how shell pipelines surface partial failures.

    When ``dry_run`` is True the flag is forwarded to each
    :func:`run_cleaner` call so no subprocess is ever launched.
    """
    max_rc = 0
    for target in targets:
        # Run each instance independently; do not short-circuit on failure.
        rc = run_cleaner(target, retention_days, min_backups, dry_run=dry_run)
        # max_rc starts at 0, so `rc > max_rc` already excludes successful runs.
        if rc > max_rc:
            max_rc = rc
    return max_rc


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
    run_on_startup: bool,
    dry_run: bool,
    tz: ZoneInfo,
) -> None:
    """Run the cleanup on the given cron schedule until shutdown.

    Validates the cron expression up front so a typo fails fast on
    container start rather than after the first wait interval. Exits
    cleanly when ``_stop_event`` is set by the signal handler; an
    in-flight cleanup subprocess is allowed to finish first.

    Args:
        schedule: 5-field cron expression interpreted in the supplied ``tz``.
        retention_days: Value forwarded as ``--retention-period``.
        min_backups: Value forwarded as ``--min-count``.
        discover_enabled: When True, scan the backup root for nested
            NSX instances; when False, target ``BACKUP_DIR`` directly.
        discover_once: When True, run discovery exactly once at startup
            and reuse the result for every firing; when False, rerun
            discovery before each firing so new instances are detected
            without restarting the container.
        run_on_startup: When True, run the cleanup immediately at
            startup (before entering the cron wait) and then continue
            with the scheduled loop. When False, the first cleanup only
            happens at the next cron firing.
        dry_run: When True, the vendor subprocess is never launched;
            run_cleaner only logs the argv it would have used. The
            scheduling loop, discovery, and signal handling are
            otherwise unchanged.
        tz: Timezone in which ``schedule`` is interpreted. Next-fire
            datetimes produced by croniter are tz-aware in this zone
            so the "Next run scheduled at ..." log line shows the
            operator's local time including DST offset.
    """
    # Construct croniter directly and let its ValueError serve as the
    # validation step; avoids the separate is_valid() pass that would
    # parse the same expression twice. Feed it a tz-aware "now" so the
    # iterator's next-fire datetimes carry the user-configured tz.
    try:
        itr = croniter(schedule, datetime.now(tz))
    except (ValueError, KeyError) as exc:
        logging.error("Invalid cron expression %r: %s", schedule, exc)
        sys.exit(2)

    logging.info(
        "Starting scheduler: schedule=%r tz=%s retention_days=%d min_backups=%d "
        "dir=%s discover_instances=%s discover_once=%s run_on_startup=%s dry_run=%s",
        schedule,
        tz.key,
        retention_days,
        min_backups,
        BACKUP_DIR,
        discover_enabled,
        discover_once,
        run_on_startup,
        dry_run,
    )

    # Always perform an initial discovery pass at startup (when enabled)
    # so the operator immediately sees which instances will be cleaned,
    # without waiting for the first cron firing. The result is also
    # reused as the long-lived cache when DISCOVER_ONCE=true and fed
    # straight into the optional RUN_ON_STARTUP pass below so we don't
    # redo the scan in the same second.
    startup_targets: list[str] | None = None
    if discover_enabled:
        logging.info("Performing startup discovery pass.")
        startup_targets = resolve_targets(discover_enabled=True)

    # Optional immediate cleanup right after container start. Reuses the
    # startup discovery (when available) so we don't scan twice; when
    # discovery is disabled, fall back to resolve_targets which simply
    # returns [BACKUP_DIR].
    if run_on_startup:
        logging.info(
            "RUN_ON_STARTUP=true - running cleanup once immediately before entering cron loop."
        )
        startup_run_targets = (
            startup_targets if discover_enabled else resolve_targets(discover_enabled)
        )
        if startup_run_targets:
            run_cleaner_for_all(
                startup_run_targets, retention_days, min_backups, dry_run=dry_run
            )
        else:
            # Discovery turned up nothing - log loudly so the operator notices
            # but still continue into the scheduled loop (a fresh mount may
            # show up later).
            logging.warning(
                "RUN_ON_STARTUP=true but no cleanup targets resolved - skipping startup run."
            )
        # If a SIGTERM arrived during the startup cleanup, exit immediately
        # instead of falling through to wait_until just to exit on the next
        # loop iteration.
        if _stop_event.is_set():
            logging.info("Scheduler stopped - graceful shutdown complete.")
            return

    while not _stop_event.is_set():
        next_fire_at = itr.get_next(datetime)
        logging.info("Next run scheduled at %s", next_fire_at.isoformat())
        if wait_until(next_fire_at):
            # Shutdown signal arrived during the wait - exit without firing.
            break

        # Resolve which directories to clean for this firing. Reuse the
        # startup discovery when DISCOVER_ONCE is set, otherwise rediscover
        # so new instance folders show up without a container restart.
        if discover_enabled and discover_once:
            targets = startup_targets
        else:
            targets = resolve_targets(discover_enabled)

        if not targets:
            # Either discovery returned nothing or BACKUP_DIR was empty;
            # skip this firing and wait for the next scheduled time.
            logging.warning("No cleanup targets for this run - skipping.")
            continue

        run_cleaner_for_all(targets, retention_days, min_backups, dry_run=dry_run)
    logging.info("Scheduler stopped - graceful shutdown complete.")


def main() -> None:
    """Entrypoint: dispatch to one-shot mode or the scheduling loop."""
    setup_logging()
    install_signal_handlers()

    # Identify the running image right at the top of the log so that even
    # a container that fails to start has its version stamped on the very
    # first line. APP_GIT_HASH is injected at build time by the Dockerfile
    # (Stage 2 ENV); when running from a checkout outside the container it
    # falls back to "unknown" which is still useful context.
    app_name, app_version = get_app_metadata()
    git_hash = os.environ.get("APP_GIT_HASH", "unknown")
    logging.info("Starting %s version %s (commit %s)", app_name, app_version, git_hash)

    try:
        (
            schedule,
            retention_days,
            min_backups,
            discover_enabled,
            discover_once,
            run_on_startup,
            dry_run,
            tz,
        ) = read_config()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(2)

    # Surface the dry-run state at INFO so it is obvious in the logs why
    # nothing got deleted on this run. Done before mode dispatch so it
    # applies to both one-shot and scheduled invocations.
    if dry_run:
        logging.info(
            "DRY_RUN=true - vendor cleanup script will NOT be invoked; "
            "the wrapper will only log what would have run."
        )

    # SCHEDULE="0" is the documented one-shot mode: run once and exit.
    if schedule.strip() == ONE_SHOT_SENTINEL:
        logging.info(
            "SCHEDULE=%s detected - running once and exiting.", ONE_SHOT_SENTINEL
        )
        # RUN_ON_STARTUP is meaningless in one-shot mode because the only
        # firing IS the startup firing. Warn (not error) so a user who set
        # both notices the redundancy without the container refusing to start.
        if run_on_startup:
            logging.warning(
                "RUN_ON_STARTUP=true is redundant when SCHEDULE=%s - ignoring.",
                ONE_SHOT_SENTINEL,
            )
        # One-shot still honors discovery so the same multi-instance
        # layout works for ad-hoc runs. DISCOVER_ONCE has no meaningful
        # effect here since there is only one firing.
        targets = resolve_targets(discover_enabled)
        if not targets:
            logging.error("No cleanup targets resolved - exiting with code 2.")
            sys.exit(2)
        exit_code = run_cleaner_for_all(
            targets, retention_days, min_backups, dry_run=dry_run
        )
        sys.exit(exit_code)

    run_loop(
        schedule,
        retention_days,
        min_backups,
        discover_enabled,
        discover_once,
        run_on_startup,
        dry_run,
        tz,
    )


if __name__ == "__main__":
    main()
