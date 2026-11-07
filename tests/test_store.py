import threading
import time
from multiprocessing import Process
from pathlib import Path

import pytest

from orrery import paths, store


def test_read_missing_is_unavailable(tmp_path):
    """A missing file returns Unavailable("missing")."""
    missing = tmp_path / "nope.txt"
    result = store.read_text(missing)
    assert isinstance(result, store.Unavailable)
    assert result.reason == "missing"


def test_read_unreadable_is_unavailable(tmp_path):
    """An unreadable file returns Unavailable with the error message."""
    unreadable = tmp_path / "secret.txt"
    unreadable.write_text("content")
    unreadable.chmod(0o000)
    try:
        result = store.read_text(unreadable)
        assert isinstance(result, store.Unavailable)
        assert "Permission" in result.reason or "permission" in result.reason.lower()
    finally:
        unreadable.chmod(0o644)


def test_atomic_write_roundtrip_and_mode(tmp_path):
    """Atomic write creates the file with correct content and no temp files left."""
    target = tmp_path / "file.txt"
    content = "test content\n"

    store.atomic_write(target, content)

    # File exists with correct content
    assert target.read_text() == content

    # File mode is 0o600
    assert target.stat().st_mode & 0o777 == 0o600

    # No temp files left behind
    temp_files = list(tmp_path.glob("file.txt.tmp*"))
    assert len(temp_files) == 0


def test_atomic_write_backs_up_previous(tmp_path):
    """Atomic write backs up the previous version before overwriting."""
    target = tmp_path / "config.toml"
    old_content = "old content\n"
    new_content = "new content\n"

    # Write initial version
    target.write_text(old_content)

    # Wait a moment to ensure different timestamp
    time.sleep(0.1)

    # Overwrite with atomic_write
    store.atomic_write(target, new_content)

    # New content is in place
    assert target.read_text() == new_content

    # Exactly one backup file exists with the old content
    backup_files = list(paths.backup_dir().glob("config.toml.*"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text() == old_content


def test_atomic_write_refuses_over_unreadable(tmp_path):
    """Atomic write raises RefusedWrite for an unreadable existing file."""
    target = tmp_path / "locked.txt"
    original_content = b"original bytes"
    target.write_bytes(original_content)
    target.chmod(0o000)

    try:
        with pytest.raises(store.RefusedWrite):
            store.atomic_write(target, "new content")

        # File is unchanged (once we restore permissions to check)
        target.chmod(0o644)
        assert target.read_bytes() == original_content
    finally:
        target.chmod(0o644)


def test_atomic_write_cleans_up_on_fsync_failure(tmp_path, monkeypatch):
    """Atomic write cleans up temp file when fsync fails."""
    target = tmp_path / "target.txt"
    target.write_text("original content")

    # Monkeypatch os.fsync to raise
    import os as os_mod

    def failing_fsync(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(os_mod, "fsync", failing_fsync)

    # Attempt to write should fail
    with pytest.raises(OSError, match="fsync failed"):
        store.atomic_write(target, "new content")

    # No temp files left behind
    temp_files = list(tmp_path.glob("target.txt.tmp*"))
    assert len(temp_files) == 0

    # Original file is unchanged
    assert target.read_text() == "original content"


# Worker function for multiprocessing test - must be at module level
def _lock_worker(counter_file_path, lock_dir_path, iterations):
    """Worker that acquires a lock, reads a counter, increments it, and writes it back.

    Args:
        counter_file_path: Path to the counter file.
        lock_dir_path: Path to the lock directory.
        iterations: Number of iterations to run.
    """
    counter_file = Path(counter_file_path)

    # Override the cache_dir for this process
    import orrery.paths as paths_mod

    def _cache_dir():
        d = Path(lock_dir_path)
        d.mkdir(parents=True, exist_ok=True)
        return d

    paths_mod.cache_dir = _cache_dir

    # Now import store after patching paths
    from orrery import store as store_mod

    for _ in range(iterations):
        with store_mod.file_lock("w"):
            # Read counter
            val = int(counter_file.read_text().strip())
            # Sleep to increase chance of race if lock doesn't work
            time.sleep(0.05)
            # Write incremented value
            counter_file.write_text(f"{val + 1}\n")


def test_lock_serializes_two_processes(tmp_path):
    """File lock serializes access across two processes."""
    counter_file = tmp_path / "counter.txt"
    counter_file.write_text("0\n")

    # Get the actual cache dir from the test fixture
    lock_dir = paths.cache_dir()

    iterations = 20

    # Create two explicit Process workers
    p1 = Process(target=_lock_worker, args=(str(counter_file), str(lock_dir), iterations))
    p2 = Process(target=_lock_worker, args=(str(counter_file), str(lock_dir), iterations))

    p1.start()
    p2.start()

    p1.join()
    p2.join()

    # If the lock works, we should have exactly 40 increments
    final_value = int(counter_file.read_text().strip())
    assert final_value == 40


def _child_check_paths(expected_home_str):
    """Child worker that checks paths resolve under fake HOME.

    Args:
        expected_home_str: The expected fake HOME path as a string.
    """
    import sys
    from pathlib import Path

    from orrery import paths

    expected_home = Path(expected_home_str)
    backup_dir = paths.backup_dir()
    cache_dir = paths.cache_dir()

    # Both should resolve under the expected fake HOME
    try:
        backup_dir.relative_to(expected_home)
        cache_dir.relative_to(expected_home)
    except ValueError:
        # One of them is not under fake HOME
        sys.exit(1)


def test_spawned_child_paths_stay_in_fake_home(tmp_path):
    """Spawned children inherit HOME env and resolve paths under fake HOME."""
    # The fake home is created by the autouse fixture
    fake_home = tmp_path / "home"

    p = Process(target=_child_check_paths, args=(str(fake_home),))
    p.start()
    p.join()

    # Exit code 0 means both paths were under fake HOME
    assert p.exitcode == 0


def test_nested_file_lock_raises_instead_of_deadlocking():
    """A thread taking a lock it already holds gets a named error at once, not a hang;
    the lock is released afterwards, also after an error inside it."""
    got = []

    def nest():
        with store.file_lock("x"):
            try:
                with store.file_lock("x"):
                    got.append("entered twice")
            except RuntimeError as e:
                got.append(str(e))
        with pytest.raises(ValueError), store.file_lock("x"):
            raise ValueError("inside")
        with store.file_lock("x"), store.file_lock("y"):  # two names nest
            got.append("x then y")

    t = threading.Thread(target=nest, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "nested file_lock hung"
    assert got == ["file lock 'x' is already held by this thread", "x then y"]


def test_file_lock_still_serializes_threads():
    """The guard is per thread: a second thread waits for the lock rather than raising."""
    order, errors = [], []
    inside = threading.Event()

    def first():
        with store.file_lock("x"):
            inside.set()
            time.sleep(0.2)
            order.append("first out")

    def second():
        try:
            inside.wait(5)
            with store.file_lock("x"):
                order.append("second in")
        except Exception as e:  # surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=f, daemon=True) for f in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert errors == [] and order == ["first out", "second in"]


def test_file_lock_non_blocking_raises_when_held():
    """blocking=False fails at once with BlockingIOError while another holder has the lock."""
    inside, release = threading.Event(), threading.Event()

    def hold():
        with store.file_lock("x"):
            inside.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    try:
        assert inside.wait(5)
        with pytest.raises(BlockingIOError), store.file_lock("x", blocking=False):
            pass
    finally:
        release.set()
        t.join(5)
    with store.file_lock("x", blocking=False):  # free again
        pass
