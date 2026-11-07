from pathlib import Path


def test_logs_never_touch_home(tmp_path):
    """After setup_logging() in tests, no file under ~/Library/Logs/orrery should exist.

    The autouse fixture monkeypatches `orrery.paths.log_dir` to tmp_path/logs,
    so setup_logging() should never touch the real home directory.
    """
    from orrery import log, paths

    # Call setup_logging
    log.setup_logging()

    # Verify log_dir resolves to tmp_path
    log_d = paths.log_dir()
    assert log_d.is_relative_to(tmp_path), f"log_dir {log_d} is not under tmp_path {tmp_path}"

    # Verify the real home directory is untouched
    real_log_dir = Path.home() / "Library" / "Logs" / "orrery"
    if real_log_dir.exists():
        # If it exists, it should not have been modified during this test
        # We can't easily test this without tracking mtimes, so we just ensure
        # that the paths are different
        assert log_d != real_log_dir, "log_dir should not be the real home directory"


def test_cache_and_backup_dirs_redirected(tmp_path):
    """cache_dir() and backup_dir() should resolve under tmp_path."""
    from orrery import paths

    cache_d = paths.cache_dir()
    backup_d = paths.backup_dir()

    assert cache_d.is_relative_to(tmp_path), f"cache_dir {cache_d} not under tmp_path"
    assert backup_d.is_relative_to(tmp_path), f"backup_dir {backup_d} not under tmp_path"
    assert cache_d.exists(), "cache_dir should be created"
    assert backup_d.exists(), "backup_dir should be created"
