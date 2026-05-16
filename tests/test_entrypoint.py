"""Unit tests for entrypoint.py.

Covers env parsing + validation, one-shot dispatch, invalid-cron rejection,
signal-driven graceful shutdown, and wait_until math. The vendor script is
intentionally not exercised here - we do not own it and it has its own
upstream test surface.
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
    for var in ("SCHEDULE", "RETENTION_DAYS", "MIN_BACKUPS"):
        monkeypatch.delenv(var, raising=False)
    schedule, retention_days, min_backups = entrypoint.read_config()
    assert schedule == entrypoint.DEFAULT_SCHEDULE
    assert retention_days == entrypoint.DEFAULT_RETENTION_DAYS
    assert min_backups == entrypoint.DEFAULT_MIN_BACKUPS


def test_read_config_honors_env_overrides(monkeypatch):
    monkeypatch.setenv("SCHEDULE", "*/15 * * * *")
    monkeypatch.setenv("RETENTION_DAYS", "14")
    monkeypatch.setenv("MIN_BACKUPS", "25")
    schedule, retention_days, min_backups = entrypoint.read_config()
    assert schedule == "*/15 * * * *"
    assert retention_days == 14
    assert min_backups == 25


@pytest.mark.parametrize(
    "var,value",
    [
        ("RETENTION_DAYS", "0"),
        ("RETENTION_DAYS", "-1"),
        ("MIN_BACKUPS", "0"),
        ("MIN_BACKUPS", "-3"),
    ],
)
def test_read_config_rejects_non_positive_integers(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    # Other var must be valid so we isolate the failure to `var`.
    other = "MIN_BACKUPS" if var == "RETENTION_DAYS" else "RETENTION_DAYS"
    monkeypatch.setenv(other, "5")
    with pytest.raises(ValueError, match=var):
        entrypoint.read_config()


def test_read_config_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "not-a-number")
    with pytest.raises(ValueError):
        entrypoint.read_config()


# ---------------------------------------------------------------------------
# run_cleaner / subprocess invocation
# ---------------------------------------------------------------------------


def test_run_cleaner_builds_expected_argv():
    # Stub subprocess.run so we capture the command without executing anything.
    fake_result = mock.Mock(returncode=0)
    with mock.patch.object(
        entrypoint.subprocess, "run", return_value=fake_result
    ) as run:
        rc = entrypoint.run_cleaner(retention_days=3, min_backups=11)
    assert rc == 0
    (called_cmd,), _ = run.call_args
    # argv must include the hardcoded --dir, the vendor script, and the
    # forwarded retention/min-count values - in that exact form.
    assert called_cmd[1] == entrypoint.CLEANER_SCRIPT
    assert (
        "--dir" in called_cmd
        and called_cmd[called_cmd.index("--dir") + 1] == entrypoint.BACKUP_DIR
    )
    assert "--retention-period" in called_cmd
    assert called_cmd[called_cmd.index("--retention-period") + 1] == "3"
    assert "--min-count" in called_cmd
    assert called_cmd[called_cmd.index("--min-count") + 1] == "11"


def test_run_cleaner_returns_subprocess_exit_code():
    fake_result = mock.Mock(returncode=42)
    with mock.patch.object(entrypoint.subprocess, "run", return_value=fake_result):
        assert entrypoint.run_cleaner(7, 10) == 42


# ---------------------------------------------------------------------------
# main / one-shot dispatch
# ---------------------------------------------------------------------------


def test_main_one_shot_runs_cleaner_once_and_exits(monkeypatch):
    monkeypatch.setenv("SCHEDULE", entrypoint.ONE_SHOT_SENTINEL)
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    with (
        mock.patch.object(entrypoint, "run_cleaner", return_value=0) as runner,
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    runner.assert_called_once_with(7, 10)
    assert exc.value.code == 0


def test_main_propagates_cleaner_exit_code_in_one_shot(monkeypatch):
    monkeypatch.setenv("SCHEDULE", "0")
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    with (
        mock.patch.object(entrypoint, "run_cleaner", return_value=5),
        pytest.raises(SystemExit) as exc,
    ):
        entrypoint.main()
    assert exc.value.code == 5


def test_main_exits_with_2_on_bad_config(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "-1")
    monkeypatch.setenv("MIN_BACKUPS", "10")
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# run_loop / invalid cron rejection
# ---------------------------------------------------------------------------


def test_run_loop_rejects_invalid_cron():
    with pytest.raises(SystemExit) as exc:
        entrypoint.run_loop("not a cron expr", 7, 10)
    assert exc.value.code == 2


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
    # Set the stop event from a thread mid-wait; wait_until must return True.
    target = datetime.now(timezone.utc) + timedelta(seconds=10)

    def _signal_shutdown():
        time.sleep(0.05)
        entrypoint._stop_event.set()

    threading.Thread(target=_signal_shutdown, daemon=True).start()
    t0 = time.monotonic()
    interrupted = entrypoint.wait_until(target)
    elapsed = time.monotonic() - t0
    assert interrupted is True
    # Must have woken up almost immediately - well under the 10s timeout.
    assert elapsed < 1.0


def test_wait_until_past_target_returns_event_state():
    # Target is already in the past - function should return immediately
    # and surface the current event state without sleeping.
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert entrypoint.wait_until(past) is False
    entrypoint._stop_event.set()
    assert entrypoint.wait_until(past) is True


# ---------------------------------------------------------------------------
# Signal handler wires through to _stop_event
# ---------------------------------------------------------------------------


def test_handle_shutdown_signal_sets_stop_event():
    assert not entrypoint._stop_event.is_set()
    entrypoint._handle_shutdown_signal(signal.SIGTERM, None)
    assert entrypoint._stop_event.is_set()


def test_install_signal_handlers_registers_sigterm_and_sigint():
    # Save current handlers so the test does not leak into other tests.
    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    try:
        entrypoint.install_signal_handlers()
        assert signal.getsignal(signal.SIGTERM) is entrypoint._handle_shutdown_signal
        assert signal.getsignal(signal.SIGINT) is entrypoint._handle_shutdown_signal
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)


# ---------------------------------------------------------------------------
# Full graceful-shutdown path: real SIGTERM interrupts a real subprocess wait
# ---------------------------------------------------------------------------


def test_run_loop_exits_cleanly_when_stop_event_set(monkeypatch):
    # Schedule that would fire in ~1 minute; we set _stop_event immediately
    # so the very first wait_until call returns True and the loop breaks
    # WITHOUT invoking run_cleaner.
    called = []
    monkeypatch.setattr(entrypoint, "run_cleaner", lambda *a, **kw: called.append(a))
    entrypoint._stop_event.set()
    # Should return normally (no SystemExit) and never call run_cleaner.
    entrypoint.run_loop("*/1 * * * *", 7, 10)
    assert called == []


# ---------------------------------------------------------------------------
# Integration: real cleanup against a fake NSX backup tree
#
# These tests spawn the actual vendor script as a subprocess via
# entrypoint.run_cleaner and assert that the right backups survived /
# were deleted on the filesystem. They cover the wrapper -> subprocess
# -> vendor script -> filesystem path end to end.
#
# NSX layout reproduced under tmp_path:
#   <root>/cluster-node-backups/<cluster-id>/<backup-N>
#   <root>/inventory-summary/<elem>/<backup-N>
# ---------------------------------------------------------------------------


def _make_backup(parent: Path, name: str, age_days: float) -> Path:
    """Create a fake backup file under `parent` with mtime aged `age_days`."""
    path = parent / name
    path.write_text("backup-payload")
    age_sec = age_days * 86400
    target = time.time() - age_sec
    os.utime(path, (target, target))
    return path


def _run_cleanup(backup_root: Path, retention_days: int, min_backups: int) -> int:
    """Invoke entrypoint.run_cleaner against `backup_root`."""
    project_root = Path(__file__).resolve().parent.parent
    with (
        mock.patch.object(entrypoint, "BACKUP_DIR", str(backup_root)),
        mock.patch.object(
            entrypoint,
            "CLEANER_SCRIPT",
            str(project_root / "scripts" / "nsx_backup_cleaner.py"),
        ),
    ):
        return entrypoint.run_cleaner(retention_days, min_backups)


def test_cleanup_keeps_all_fresh_backups(tmp_path):
    # All 10 backups are 1 day old; retention is 7 days. None should be deleted.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    cluster.mkdir(parents=True)
    for i in range(10):
        _make_backup(cluster, f"backup-{i:02d}", age_days=1)

    rc = _run_cleanup(root, retention_days=7, min_backups=5)
    assert rc == 0
    remaining = sorted(p.name for p in cluster.iterdir())
    assert len(remaining) == 10
    assert remaining == [f"backup-{i:02d}" for i in range(10)]


def test_cleanup_respects_min_backups_floor_when_count_below_min(tmp_path):
    # 3 backups, all 30 days old, but min_backups=5 means the vendor script
    # short-circuits before even building the delete list. All 3 must survive.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    cluster.mkdir(parents=True)
    for i in range(3):
        _make_backup(cluster, f"backup-{i:02d}", age_days=30)

    rc = _run_cleanup(root, retention_days=7, min_backups=5)
    assert rc == 0
    remaining = sorted(p.name for p in cluster.iterdir())
    assert len(remaining) == 3, "min_backups floor must skip deletion when count <= min"


def test_cleanup_deletes_old_backups_capped_by_floor(tmp_path):
    # 10 backups, all aged 30 days (clearly past retention). min_backups=5
    # caps deletion at len(all) - min = 5. The vendor script deletes the
    # 5 oldest by ctime (which equals creation order in our setup), so
    # backup-00..04 are deleted and backup-05..09 remain.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    cluster.mkdir(parents=True)
    for i in range(10):
        _make_backup(cluster, f"backup-{i:02d}", age_days=30)

    rc = _run_cleanup(root, retention_days=7, min_backups=5)
    assert rc == 0
    remaining = sorted(p.name for p in cluster.iterdir())
    assert len(remaining) == 5, "min_backups floor must cap deletion at len(all) - min"
    # The 5 oldest-by-ctime (first-created in our loop) must be gone.
    for i in range(5):
        assert f"backup-{i:02d}" not in remaining
    # The 5 newest-by-ctime must survive.
    for i in range(5, 10):
        assert f"backup-{i:02d}" in remaining


def test_cleanup_mixed_ages_keeps_fresh_deletes_old(tmp_path):
    # 5 old (aged 30d, created first) + 5 fresh (aged 1d, created second).
    # retention=7, min_backups=3: all 5 old qualify for deletion, and the
    # floor (10-3=7) is loose, so all 5 old are deleted. Fresh ones remain.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    cluster.mkdir(parents=True)
    for i in range(5):
        _make_backup(cluster, f"old-{i:02d}", age_days=30)
    for i in range(5):
        _make_backup(cluster, f"fresh-{i:02d}", age_days=1)

    rc = _run_cleanup(root, retention_days=7, min_backups=3)
    assert rc == 0
    remaining = sorted(p.name for p in cluster.iterdir())
    assert len(remaining) == 5
    # Every fresh backup survives; every old backup is gone.
    assert all(name.startswith("fresh-") for name in remaining)


def test_cleanup_processes_both_cluster_node_and_inventory_summary(tmp_path):
    # Same shape in both subfolders: 7 backups (6 aged 30d, 1 aged 1d),
    # retention=7, min_backups=5. delete_count = min(6_old, 7_total-5_min) = 2,
    # so the two oldest by ctime are deleted in each folder, leaving 5.
    root = tmp_path / "backups"
    cluster = root / "cluster-node-backups" / "cluster-1"
    inv = root / "inventory-summary" / "inv-1"
    cluster.mkdir(parents=True)
    inv.mkdir(parents=True)
    for parent, prefix in ((cluster, "cn"), (inv, "inv")):
        for i in range(7):
            age = 30 if i < 6 else 1
            _make_backup(parent, f"{prefix}-{i:02d}", age_days=age)

    rc = _run_cleanup(root, retention_days=7, min_backups=5)
    assert rc == 0

    cluster_remaining = sorted(p.name for p in cluster.iterdir())
    inv_remaining = sorted(p.name for p in inv.iterdir())
    assert len(cluster_remaining) == 5
    assert len(inv_remaining) == 5
    # The two oldest by ctime (created first) must be gone in each folder.
    assert "cn-00" not in cluster_remaining
    assert "cn-01" not in cluster_remaining
    assert "inv-00" not in inv_remaining
    assert "inv-01" not in inv_remaining


def test_real_sigterm_triggers_graceful_shutdown_subprocess(tmp_path):
    # End-to-end check: spawn the actual entrypoint as a subprocess with a
    # schedule that won't fire for hours, send SIGTERM, and verify it exits
    # cleanly within a second with the shutdown log line.
    project_root = Path(__file__).resolve().parent.parent
    backup_root = tmp_path / "backups"
    (backup_root / "cluster-node-backups" / "b1").mkdir(parents=True)
    (backup_root / "cluster-node-backups" / "b1" / "data.tar").touch()

    # Run entrypoint.py as __main__ with patched module-level paths via a
    # tiny harness so we hit the real signal-handling path. Use a far-future
    # cron schedule (yearly at midnight Jan 1) so the loop is firmly in
    # wait_until when we signal it.
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(project_root)!r})\n"
        "import entrypoint\n"
        f"entrypoint.BACKUP_DIR = {str(backup_root)!r}\n"
        f"entrypoint.CLEANER_SCRIPT = {str(project_root / 'scripts' / 'nsx_backup_cleaner.py')!r}\n"
        "entrypoint.main()\n",
    )
    proc = subprocess.Popen(
        [sys.executable, str(harness)],
        env={
            **os.environ,
            "SCHEDULE": "0 0 1 1 *",
            "RETENTION_DAYS": "7",
            "MIN_BACKUPS": "10",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # Give the scheduler a moment to enter wait_until.
        time.sleep(0.5)
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
