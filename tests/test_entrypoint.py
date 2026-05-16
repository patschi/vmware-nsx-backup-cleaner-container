"""Unit tests for entrypoint.py.

Focus: the wrapper's own behavior - config parsing, vendor argv building,
instance discovery, scheduling/one-shot dispatch, dry-run safety, timezone
handling, and graceful shutdown. The vendor script itself is intentionally
under-tested: we do not own it, and a single integration smoke test is
enough to prove the wrapper invokes it correctly.

When in doubt about whether to add a test, prefer asserting *observable
wrapper behavior* over implementation details (which stream is captured,
which log level is used, etc.).
"""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

import entrypoint


@pytest.fixture(autouse=True)
def _reset_stop_event():
    # Each test starts with a fresh, un-set shutdown event.
    entrypoint._stop_event.clear()
    yield
    entrypoint._stop_event.clear()


# ---------------------------------------------------------------------------
# read_config
# ---------------------------------------------------------------------------


def test_read_config_uses_defaults_when_env_unset(monkeypatch):
    # Strip the relevant env vars so defaults apply.
    for var in (
        "SCHEDULE",
        "RETENTION_DAYS",
        "MIN_BACKUPS",
        "DISCOVER_INSTANCES",
        "DISCOVER_ONCE",
        "RUN_ON_STARTUP",
        "DRY_RUN",
        "TZ",
    ):
        monkeypatch.delenv(var, raising=False)
    (
        schedule,
        retention_days,
        min_backups,
        discover_enabled,
        discover_once,
        run_on_startup,
        dry_run,
        tz,
    ) = entrypoint.read_config()
    assert schedule == entrypoint.DEFAULT_SCHEDULE
    assert retention_days == entrypoint.DEFAULT_RETENTION_DAYS
    assert min_backups == entrypoint.DEFAULT_MIN_BACKUPS
    assert discover_enabled is entrypoint.DEFAULT_DISCOVER_INSTANCES
    assert discover_once is entrypoint.DEFAULT_DISCOVER_ONCE
    assert run_on_startup is entrypoint.DEFAULT_RUN_ON_STARTUP
    assert dry_run is entrypoint.DEFAULT_DRY_RUN
    assert tz.key == entrypoint.DEFAULT_TZ


def test_read_config_honors_env_overrides(monkeypatch):
    # One test exercises all env-var overrides at once so the whole
    # "environment → config tuple" mapping is verified together.
    monkeypatch.setenv("SCHEDULE", "*/15 * * * *")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("MIN_BACKUPS", "25")
    monkeypatch.setenv("DISCOVER_INSTANCES", "false")
    monkeypatch.setenv("DISCOVER_ONCE", "true")
    monkeypatch.setenv("RUN_ON_STARTUP", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    (
        schedule,
        retention_days,
        min_backups,
        discover_enabled,
        discover_once,
        run_on_startup,
        dry_run,
        tz,
    ) = entrypoint.read_config()
    assert schedule == "*/15 * * * *"
    assert retention_days == 14
    assert min_backups == 25
    assert discover_enabled is False
    assert discover_once is True
    assert run_on_startup is True
    assert dry_run is True
    assert tz.key == "Europe/Berlin"


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Cover one case per truthy/falsy form family; the parser is a
        # simple membership check, so the full case-matrix is excessive.
        ("true", True),
        ("1", True),
        ("FALSE", False),
        ("  off  ", False),
    ],
)
def test_read_config_parses_boolean_env_forms(monkeypatch, raw, expected):
    monkeypatch.setenv("DRY_RUN", raw)
    monkeypatch.setenv("RETENTION_DAYS", "5")
    monkeypatch.setenv("MIN_BACKUPS", "5")
    _, _, _, _, _, _, dry_run, _ = entrypoint.read_config()
    assert dry_run is expected


@pytest.mark.parametrize(
    "var", ["DISCOVER_INSTANCES", "DISCOVER_ONCE", "RUN_ON_STARTUP", "DRY_RUN"]
)
def test_read_config_rejects_invalid_boolean(monkeypatch, var):
    # Anything outside the allowlist must surface as a startup error rather
    # than silently falling back to a default.
    monkeypatch.setenv(var, "maybe")
    monkeypatch.setenv("RETENTION_DAYS", "5")
    monkeypatch.setenv("MIN_BACKUPS", "5")
    with pytest.raises(ValueError, match=var):
        entrypoint.read_config()


@pytest.mark.parametrize("var", ["RETENTION_DAYS", "MIN_BACKUPS"])
def test_read_config_rejects_non_positive_integers(monkeypatch, var):
    # Zero and negative values are nonsense for both knobs; one negative
    # value per var is enough since the check is `<= 0`.
    monkeypatch.setenv(var, "-1")
    other = "MIN_BACKUPS" if var == "RETENTION_DAYS" else "RETENTION_DAYS"
    monkeypatch.setenv(other, "5")
    with pytest.raises(ValueError, match=var):
        entrypoint.read_config()


def test_read_config_rejects_invalid_timezone(monkeypatch):
    # An unknown IANA name must fail fast at startup with a ValueError so
    # the operator sees the misconfiguration immediately.
    monkeypatch.setenv("TZ", "Mars/Olympus_Mons")
    monkeypatch.setenv("RETENTION_DAYS", "5")
    monkeypatch.setenv("MIN_BACKUPS", "5")
    with pytest.raises(ValueError, match="TZ"):
        entrypoint.read_config()


# ---------------------------------------------------------------------------
# run_cleaner / subprocess invocation
# ---------------------------------------------------------------------------


def test_run_cleaner_builds_expected_argv_and_propagates_exit_code():
    # The most important contract of run_cleaner: it must hand the vendor
    # script exactly --dir/--retention-period/--min-count and surface the
    # subprocess exit code unchanged. Asserted together so the wrapper's
    # entire interface to the vendor is covered by one test.
    fake_result = mock.Mock(returncode=5, stdout="")
    with mock.patch.object(
        entrypoint.subprocess, "run", return_value=fake_result
    ) as run:
        rc = entrypoint.run_cleaner("/some/dir", retention_days=3, min_backups=11)
    assert rc == 5
    (called_cmd,), _ = run.call_args
    assert called_cmd[1] == entrypoint.CLEANER_SCRIPT
    assert called_cmd[called_cmd.index("--dir") + 1] == "/some/dir"
    assert called_cmd[called_cmd.index("--retention-period") + 1] == "3"
    assert called_cmd[called_cmd.index("--min-count") + 1] == "11"


def test_run_cleaner_dry_run_never_invokes_subprocess(caplog):
    # DRY_RUN=true is a safety feature: under no circumstance may
    # subprocess.run be called. We enforce that by raising from any call.
    caplog.set_level("INFO")

    def _explode(*_a, **_kw):
        raise AssertionError("subprocess.run must not be called in dry-run mode")

    with mock.patch.object(entrypoint.subprocess, "run", side_effect=_explode):
        rc = entrypoint.run_cleaner("/some/dir", 7, 10, dry_run=True)
    assert rc == 0
    # And the operator must see the would-be command so they can verify it.
    info_lines = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
    assert any("[DRY-RUN]" in line and "/some/dir" in line for line in info_lines)


def test_run_cleaner_for_all_forwards_dry_run_flag():
    # The fan-out helper must propagate dry_run to every per-instance call;
    # otherwise a multi-instance dry-run could still mutate the filesystem.
    seen = []

    def _fake(directory, retention_days, min_backups, dry_run=False):
        seen.append((directory, dry_run))
        return 0

    with mock.patch.object(entrypoint, "run_cleaner", side_effect=_fake):
        entrypoint.run_cleaner_for_all(["/a", "/b"], 7, 10, dry_run=True)
    assert seen == [("/a", True), ("/b", True)]


# ---------------------------------------------------------------------------
# Instance discovery
# ---------------------------------------------------------------------------


def _make_instance(parent: Path, name: str, markers=("cluster-node-backups",)) -> Path:
    """Create an NSX-instance-shaped folder under `parent` with given marker subdirs."""
    inst = parent / name
    inst.mkdir(parents=True)
    for marker in markers:
        (inst / marker).mkdir()
    return inst


def test_is_nsx_instance_dir_recognizes_markers(tmp_path):
    # Either marker subdir qualifies a folder as an NSX instance; folders
    # without either marker (or non-existent paths) do not. Both sides of
    # the predicate are covered here to keep the surface small.
    cn = tmp_path / "cn-only"
    (cn / "cluster-node-backups").mkdir(parents=True)
    inv = tmp_path / "inv-only"
    (inv / "inventory-summary").mkdir(parents=True)
    noise = tmp_path / "noise"
    noise.mkdir()
    assert entrypoint.is_nsx_instance_dir(str(cn)) is True
    assert entrypoint.is_nsx_instance_dir(str(inv)) is True
    assert entrypoint.is_nsx_instance_dir(str(noise)) is False
    assert entrypoint.is_nsx_instance_dir(str(tmp_path / "ghost")) is False


def test_discover_instances_returns_root_when_root_is_an_instance(tmp_path):
    # Legacy single-instance layout: backup root itself contains the markers.
    (tmp_path / "cluster-node-backups").mkdir()
    (tmp_path / "inventory-summary").mkdir()
    assert entrypoint.discover_instances(str(tmp_path)) == [str(tmp_path)]


def test_discover_instances_finds_nested_instances(tmp_path):
    # Multi-instance layout matching real NAS examples: nsx-at and nsx-de
    # are valid instances; @Recently-Snapshot is noise that must be skipped.
    _make_instance(tmp_path, "nsx-at")
    _make_instance(tmp_path, "nsx-de", markers=("inventory-summary",))
    (tmp_path / "@Recently-Snapshot").mkdir()
    found = entrypoint.discover_instances(str(tmp_path))
    assert found == [str(tmp_path / "nsx-at"), str(tmp_path / "nsx-de")]


def test_resolve_targets_returns_backup_dir_when_discovery_disabled(monkeypatch):
    # Discovery off preserves the original single-mount behavior: pass
    # BACKUP_DIR straight through to the vendor.
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", "/backups")
    assert entrypoint.resolve_targets(discover_enabled=False) == ["/backups"]


def test_resolve_targets_logs_every_discovered_instance(tmp_path, monkeypatch, caplog):
    # Every discovered instance must be visible in the INFO log so an
    # operator can audit what will be cleaned this run.
    _make_instance(tmp_path, "nsx-at")
    _make_instance(tmp_path, "nsx-de")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))
    caplog.set_level("INFO")
    targets = entrypoint.resolve_targets(discover_enabled=True)
    assert targets == [str(tmp_path / "nsx-at"), str(tmp_path / "nsx-de")]
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "Discovered 2 NSX backup instance(s)" in log_text
    assert "nsx-at" in log_text
    assert "nsx-de" in log_text


# ---------------------------------------------------------------------------
# Multi-instance dispatch (run_cleaner_for_all)
# ---------------------------------------------------------------------------


def test_run_cleaner_for_all_invokes_cleaner_per_target():
    # Each discovered target must trigger an independent run_cleaner call.
    calls = []

    def _fake(directory, retention_days, min_backups, dry_run=False):
        calls.append((directory, retention_days, min_backups))
        return 0

    with mock.patch.object(entrypoint, "run_cleaner", side_effect=_fake):
        rc = entrypoint.run_cleaner_for_all(["/a", "/b", "/c"], 7, 10)
    assert rc == 0
    assert calls == [("/a", 7, 10), ("/b", 7, 10), ("/c", 7, 10)]


def test_run_cleaner_for_all_returns_worst_exit_code_and_continues_on_failure():
    # A single failing instance must not stop the others; the worst non-zero
    # exit code is returned so partial failures still surface in the wrapper's rc.
    rcs = iter([0, 5, 2, 0])
    targets_invoked = []

    def _fake(directory, *_a, **_kw):
        targets_invoked.append(directory)
        return next(rcs)

    with mock.patch.object(entrypoint, "run_cleaner", side_effect=_fake):
        rc = entrypoint.run_cleaner_for_all(["/a", "/b", "/c", "/d"], 7, 10)
    assert rc == 5  # worst (largest) non-zero rc
    assert targets_invoked == ["/a", "/b", "/c", "/d"]  # all four ran


# ---------------------------------------------------------------------------
# Application metadata (pyproject.toml parsing for startup version log)
# ---------------------------------------------------------------------------


def test_get_app_metadata_reads_shipped_pyproject():
    # The real shipped pyproject.toml must yield a non-"unknown" name and
    # version so the startup log line is informative.
    name, version = entrypoint.get_app_metadata()
    assert name == "nsx-backup-cleaner-container"
    assert version != "unknown"


def test_get_app_metadata_falls_back_when_file_unreadable(monkeypatch, tmp_path):
    # A missing/broken pyproject.toml must NOT crash the wrapper; we test
    # the missing-file path as the canonical failure mode.
    monkeypatch.setattr(
        entrypoint, "PYPROJECT_PATH", str(tmp_path / "does-not-exist.toml")
    )
    name, version = entrypoint.get_app_metadata()
    assert name == "unknown"
    assert version == "unknown"


# ---------------------------------------------------------------------------
# main / one-shot dispatch
# ---------------------------------------------------------------------------


def test_main_logs_app_name_and_version_at_startup(monkeypatch, caplog):
    # The first INFO line from main() must identify the running image so
    # operators can see which version is live without inspecting the tag.
    monkeypatch.setenv("SCHEDULE", entrypoint.ONE_SHOT_SENTINEL)
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    monkeypatch.setenv("DISCOVER_INSTANCES", "false")
    caplog.set_level("INFO")
    with (
        mock.patch.object(entrypoint, "run_cleaner", return_value=0),
        pytest.raises(SystemExit),
    ):
        entrypoint.main()
    info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
    assert any(
        msg.startswith("Starting nsx-backup-cleaner-container version ")
        for msg in info_messages
    ), info_messages


def test_main_one_shot_runs_cleaner_and_propagates_exit_code(monkeypatch):
    # One-shot path: SCHEDULE="0" must invoke run_cleaner exactly once
    # against BACKUP_DIR and surface its exit code as the process exit code.
    # Using rc != 0 also exercises the exit-code propagation path.
    monkeypatch.setenv("SCHEDULE", entrypoint.ONE_SHOT_SENTINEL)
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    monkeypatch.setenv("DISCOVER_INSTANCES", "false")
    with (
        mock.patch.object(entrypoint, "run_cleaner", return_value=5) as runner,
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    runner.assert_called_once_with(entrypoint.BACKUP_DIR, 7, 10, dry_run=False)
    assert exc.value.code == 5


def test_main_one_shot_discovers_and_runs_per_instance(monkeypatch, tmp_path):
    # End-to-end one-shot path with discovery enabled: every detected
    # instance must trigger an independent run_cleaner call.
    _make_instance(tmp_path, "nsx-at")
    _make_instance(tmp_path, "nsx-de")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULE", "0")
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    monkeypatch.setenv("DISCOVER_INSTANCES", "true")

    calls = []

    def _fake(directory, *_a, **_kw):
        calls.append(directory)
        return 0

    with (
        mock.patch.object(entrypoint, "run_cleaner", side_effect=_fake),
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    assert exc.value.code == 0
    assert calls == [str(tmp_path / "nsx-at"), str(tmp_path / "nsx-de")]


def test_main_one_shot_propagates_dry_run_flag(monkeypatch, tmp_path):
    # In one-shot mode with DRY_RUN=true the wrapper must still invoke
    # run_cleaner so dispatch is exercised, but dry_run=True must be
    # forwarded so the subprocess is skipped end-to-end.
    _make_instance(tmp_path, "nsx-at")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULE", "0")
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    monkeypatch.setenv("DISCOVER_INSTANCES", "true")
    monkeypatch.setenv("DRY_RUN", "true")

    seen = []

    def _fake(directory, retention_days, min_backups, dry_run=False):
        seen.append((directory, dry_run))
        return 0

    with (
        mock.patch.object(entrypoint, "run_cleaner", side_effect=_fake),
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    assert exc.value.code == 0
    assert seen == [(str(tmp_path / "nsx-at"), True)]


def test_main_one_shot_exits_with_2_when_discovery_finds_nothing(monkeypatch, tmp_path):
    # Discovery enabled but the backup root is empty - the wrapper must
    # not silently exit 0 pretending all is well.
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULE", "0")
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    monkeypatch.setenv("DISCOVER_INSTANCES", "true")
    with (
        mock.patch.object(entrypoint, "run_cleaner") as runner,
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    runner.assert_not_called()
    assert exc.value.code == 2


def test_main_exits_with_2_on_bad_config(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "-1")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# run_loop / scheduling
# ---------------------------------------------------------------------------


def test_run_loop_rejects_invalid_cron():
    with pytest.raises(SystemExit) as exc:
        entrypoint.run_loop(
            "not a cron expr",
            7,
            10,
            discover_enabled=False,
            discover_once=False,
            run_on_startup=False,
            dry_run=False,
            tz=ZoneInfo("UTC"),
        )
    assert exc.value.code == 2


def test_run_loop_caches_targets_when_discover_once_true(monkeypatch, tmp_path):
    # With DISCOVER_ONCE=true, discovery must happen exactly once at startup
    # and subsequent firings reuse the cached target list (no re-scan).
    _make_instance(tmp_path, "nsx-at")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))

    discovery_calls = []
    real_discover = entrypoint.discover_instances

    def _spy(root):
        discovery_calls.append(root)
        return real_discover(root)

    monkeypatch.setattr(entrypoint, "discover_instances", _spy)
    # Skip the real wait so the loop iterates immediately.
    monkeypatch.setattr(entrypoint, "wait_until", lambda _t: False)

    fire_count = {"n": 0}

    def _fake_cleaner(*_a, **_kw):
        fire_count["n"] += 1
        if fire_count["n"] >= 3:
            entrypoint._stop_event.set()
        return 0

    monkeypatch.setattr(entrypoint, "run_cleaner", _fake_cleaner)
    entrypoint.run_loop(
        "* * * * *",
        7,
        10,
        discover_enabled=True,
        discover_once=True,
        run_on_startup=False,
        dry_run=False,
        tz=ZoneInfo("UTC"),
    )
    # Discovery should have been triggered exactly once at startup,
    # regardless of how many times the loop fired.
    assert len(discovery_calls) == 1
    assert fire_count["n"] == 3


def test_run_loop_rediscovers_each_firing_with_startup_pass(
    monkeypatch, tmp_path, caplog
):
    # With DISCOVER_ONCE=false:
    #   - Discovery must run once at startup so the operator gets immediate
    #     log visibility (without waiting for the first firing).
    #   - Discovery must also rerun before every firing so newly-added
    #     instance folders are picked up automatically.
    # Both are asserted here to keep the run_loop test surface small.
    _make_instance(tmp_path, "nsx-at")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))

    discovery_calls = []
    real_discover = entrypoint.discover_instances

    def _spy(root):
        discovery_calls.append(root)
        return real_discover(root)

    monkeypatch.setattr(entrypoint, "discover_instances", _spy)
    monkeypatch.setattr(entrypoint, "wait_until", lambda _t: False)

    fire_count = {"n": 0}

    def _fake_cleaner(*_a, **_kw):
        fire_count["n"] += 1
        if fire_count["n"] >= 3:
            entrypoint._stop_event.set()
        return 0

    monkeypatch.setattr(entrypoint, "run_cleaner", _fake_cleaner)
    caplog.set_level("INFO")
    entrypoint.run_loop(
        "* * * * *",
        7,
        10,
        discover_enabled=True,
        discover_once=False,
        run_on_startup=False,
        dry_run=False,
        tz=ZoneInfo("UTC"),
    )
    # 1 startup discovery + 3 per-firing discoveries.
    assert len(discovery_calls) == 4
    # Startup-discovery log line must be visible too.
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "Performing startup discovery pass." in log_text


def test_run_loop_run_on_startup_runs_cleanup_before_loop(monkeypatch, tmp_path):
    # With RUN_ON_STARTUP=true the wrapper must invoke the cleanup once
    # immediately, then continue with the scheduled loop. We stop the loop
    # before any firing so the only run_cleaner call we see is the startup one.
    _make_instance(tmp_path, "nsx-at")
    monkeypatch.setattr(entrypoint, "BACKUP_DIR", str(tmp_path))
    entrypoint._stop_event.set()

    calls = []

    def _fake(directory, *_a, **_kw):
        calls.append(directory)
        return 0

    monkeypatch.setattr(entrypoint, "run_cleaner", _fake)
    entrypoint.run_loop(
        "* * * * *",
        7,
        10,
        discover_enabled=True,
        discover_once=False,
        run_on_startup=True,
        dry_run=False,
        tz=ZoneInfo("UTC"),
    )
    assert calls == [str(tmp_path / "nsx-at")]


def test_run_loop_uses_configured_timezone_for_cron(monkeypatch, caplog):
    # croniter must be fed a tz-aware "now" so SCHEDULE is interpreted in
    # the operator's configured zone. We capture the base datetime passed
    # to croniter() and assert its tzinfo matches what we passed.
    seen_base = {}
    real_croniter = entrypoint.croniter

    def _spy(expr, base, *a, **kw):
        seen_base["base"] = base
        return real_croniter(expr, base, *a, **kw)

    monkeypatch.setattr(entrypoint, "croniter", _spy)
    entrypoint._stop_event.set()
    caplog.set_level("INFO")
    berlin = ZoneInfo("Europe/Berlin")
    entrypoint.run_loop(
        "0 3 * * *",
        7,
        10,
        discover_enabled=False,
        discover_once=False,
        run_on_startup=False,
        dry_run=False,
        tz=berlin,
    )
    assert seen_base["base"].tzinfo is berlin
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "tz=Europe/Berlin" in log_text


def test_run_loop_exits_cleanly_when_stop_event_set(monkeypatch):
    # SIGTERM equivalent: _stop_event is already set when run_loop starts,
    # so the loop must exit without ever invoking run_cleaner.
    called = []
    monkeypatch.setattr(entrypoint, "run_cleaner", lambda *a, **kw: called.append(a))
    entrypoint._stop_event.set()
    entrypoint.run_loop(
        "*/1 * * * *",
        7,
        10,
        discover_enabled=False,
        discover_once=False,
        run_on_startup=False,
        dry_run=False,
        tz=ZoneInfo("UTC"),
    )
    assert called == []


# ---------------------------------------------------------------------------
# wait_until
# ---------------------------------------------------------------------------


def test_wait_until_returns_false_on_normal_timeout():
    # Pick a target ~0.2s in the future so the wait actually returns quickly.
    target = datetime.now(timezone.utc) + timedelta(milliseconds=200)
    t0 = time.monotonic()
    interrupted = entrypoint.wait_until(target)
    elapsed = time.monotonic() - t0
    assert interrupted is False
    assert 0.15 <= elapsed < 1.0


def test_wait_until_returns_true_when_signal_fires():
    # Set the stop event from a thread mid-wait; wait_until must return True
    # almost immediately instead of running to its (10s) timeout.
    target = datetime.now(timezone.utc) + timedelta(seconds=10)

    def _signal_shutdown():
        time.sleep(0.05)
        entrypoint._stop_event.set()

    threading.Thread(target=_signal_shutdown, daemon=True).start()
    t0 = time.monotonic()
    interrupted = entrypoint.wait_until(target)
    elapsed = time.monotonic() - t0
    assert interrupted is True
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Integration: real vendor script + real SIGTERM
#
# Two end-to-end tests:
#   - Vendor smoke test: confirms the wrapper -> subprocess -> vendor ->
#     filesystem path actually works against a fake NSX tree.
#   - SIGTERM test: confirms a running container exits cleanly on signal.
#
# Both are kept because they cover code paths (subprocess, signal delivery)
# that unit tests can only approximate.
# ---------------------------------------------------------------------------


def _make_backup(parent: Path, name: str, age_days: float) -> Path:
    """Create a fake backup file under `parent` with mtime aged `age_days`."""
    path = parent / name
    path.write_text("backup-payload")
    age_sec = age_days * 86400
    target = time.time() - age_sec
    os.utime(path, (target, target))
    return path


def test_vendor_smoke_keeps_fresh_deletes_old(tmp_path):
    # End-to-end: spawn the actual vendor script via run_cleaner against a
    # fake NSX tree with mixed-age backups. After cleanup, every fresh
    # backup must survive and every old backup must be gone. This is the
    # canonical "the wrapper actually drives the vendor" smoke test.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    cluster.mkdir(parents=True)
    for i in range(5):
        _make_backup(cluster, f"old-{i:02d}", age_days=30)
    for i in range(5):
        _make_backup(cluster, f"fresh-{i:02d}", age_days=1)

    project_root = Path(__file__).resolve().parent.parent
    with mock.patch.object(
        entrypoint,
        "CLEANER_SCRIPT",
        str(project_root / "vendor-scripts" / "nsx_backup_cleaner.py"),
    ):
        rc = entrypoint.run_cleaner(str(root), retention_days=7, min_backups=3)
    assert rc == 0
    remaining = sorted(p.name for p in cluster.iterdir())
    assert len(remaining) == 5
    assert all(name.startswith("fresh-") for name in remaining)


def test_real_sigterm_triggers_graceful_shutdown_subprocess(tmp_path):
    # End-to-end check: spawn the actual entrypoint as a subprocess with a
    # schedule that won't fire for a long time, send SIGTERM, and verify it
    # exits cleanly within a second with the shutdown log line. This is the
    # only test that exercises real signal delivery through to the wrapper.
    project_root = Path(__file__).resolve().parent.parent
    backup_root = tmp_path / "backups"
    (backup_root / "cluster-node-backups" / "b1").mkdir(parents=True)
    (backup_root / "cluster-node-backups" / "b1" / "data.tar").touch()

    harness = tmp_path / "harness.py"
    harness.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(project_root)!r})\n"
        "import entrypoint\n"
        f"entrypoint.BACKUP_DIR = {str(backup_root)!r}\n"
        f"entrypoint.CLEANER_SCRIPT = {str(project_root / 'vendor-scripts' / 'nsx_backup_cleaner.py')!r}\n"
        "entrypoint.main()\n",
    )
    proc = subprocess.Popen(
        [sys.executable, str(harness)],
        env={
            **os.environ,
            # Yearly schedule so the loop is firmly inside wait_until when
            # we signal it.
            "SCHEDULE": "0 0 1 1 *",
            "RETENTION_DAYS": "7",
            "MIN_BACKUPS": "10",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(0.5)  # give the scheduler a moment to enter wait_until
        proc.send_signal(signal.SIGTERM)
        try:
            stdout, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            pytest.fail("entrypoint did not exit within 3s of SIGTERM")
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert b"graceful shutdown complete" in stdout.lower()
