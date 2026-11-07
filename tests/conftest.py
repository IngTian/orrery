import datetime as dt

import pytest

FROZEN_NOW = dt.datetime(2026, 11, 12, 9, 0)
SUBJECTS = (("math", "Math"), ("finance", "Finance"), ("geography", "Geography"))
KINDS = ("learn", "rep", "fun", "geography", "warmup")


# The week template for testing
def _block(part, subject, kind, sessions):
    """Lazy import to avoid circular dependencies."""
    from orrery.model import Block

    return Block(part, subject, kind, sessions)


TEMPLATE = {
    0: (
        _block("warmup", "math", "rep", 1),
        _block("main", "math", "learn", 2),
        _block("residual", "geography", "geography", 1),
    ),
    1: (
        _block("main", "finance", "learn", 2),
        _block("residual", "geography", "geography", 1),
    ),
    2: (
        _block("main", "math", "rep", 1),
        _block("main", "finance", "rep", 1),
        _block("residual", "geography", "geography", 1),
    ),
    3: (
        _block("main", "math", "learn", 2),
        _block("residual", "geography", "geography", 1),
    ),
    4: (
        _block("main", "math", "learn", 2),
        _block("residual", "geography", "geography", 1),
    ),
    5: (
        _block("main", "math", "fun", 1),
        _block("residual", "math", "rep", 1),
    ),
    6: (),
}


@pytest.fixture
def frozen_now():
    """A fixed datetime for tests: Thursday 2026-11-12 at 9:00 AM."""
    return FROZEN_NOW


@pytest.fixture
def orrery_home(tmp_path, monkeypatch):
    """Set ORRERY_HOME to a temporary directory."""
    home = tmp_path / "orrery"
    home.mkdir()
    monkeypatch.setenv("ORRERY_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _logs_to_tmp(tmp_path, monkeypatch):
    """Keep the suite out of the user's real log directory.

    `orrery.paths.log_dir()` resolves to `~/Library/Logs/orrery` by default, and tests
    calling `setup_logging()` would write to the live file. This fixture patches
    `log_dir`, `cache_dir`, and `backup_dir` to redirect everything to tmp_path.

    `setup_logging` also does `root_logger.handlers = [handler]`, so without this the handler
    left behind points at the live file for the rest of the session.

    Additionally, monkeypatch HOME so spawned child processes (which inherit env but not
    monkeypatches) also resolve paths under the fake HOME.
    """
    from orrery import paths

    # Clear ORRERY_HOME so tests use the default data root
    monkeypatch.delenv("ORRERY_HOME", raising=False)

    # Create fake home and set HOME env var for spawned children
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    log_tmp = tmp_path / "logs"
    cache_tmp = tmp_path / "cache"
    backup_tmp = tmp_path / "backup"

    def _log_dir():
        log_tmp.mkdir(parents=True, exist_ok=True)
        return log_tmp

    def _cache_dir():
        cache_tmp.mkdir(parents=True, exist_ok=True)
        return cache_tmp

    def _backup_dir():
        backup_tmp.mkdir(parents=True, exist_ok=True)
        return backup_tmp

    monkeypatch.setattr(paths, "log_dir", _log_dir)
    monkeypatch.setattr(paths, "cache_dir", _cache_dir)
    monkeypatch.setattr(paths, "backup_dir", _backup_dir)


@pytest.fixture(autouse=True)
def _fast_pilot(monkeypatch):
    """Shorten Textual's idle-wait tick (0.02 s) so `pilot.pause()` doesn't sleep the suite.

    The attribute is private, so `monkeypatch.setattr` raises if it is ever renamed.
    """
    import textual._wait

    monkeypatch.setattr(textual._wait, "SLEEP_GRANULARITY", 0.002)


@pytest.fixture(autouse=True)
def _disable_external_runners(monkeypatch):
    """Set ORRERY_DISABLE_EXTERNAL_RUNNERS=1 for all tests."""
    monkeypatch.setenv("ORRERY_DISABLE_EXTERNAL_RUNNERS", "1")


@pytest.fixture
def seeded_db(orrery_home):
    """A database seeded with subjects, kinds, settings, and an empty template."""
    from orrery import db, schedule, settings

    # Write config.toml
    (orrery_home / "config.toml").write_text(settings.CONFIG_TEMPLATE)

    # Open database
    conn = db.open_db(orrery_home)

    # Seed the database
    schedule.seed(
        conn,
        subjects=SUBJECTS,
        kinds=KINDS,
        cap=4,
        starts=dt.date(2026, 11, 9),
        now=FROZEN_NOW,
    )

    return conn


@pytest.fixture
def week_db(seeded_db):
    """A seeded_db with TEMPLATE written."""
    from orrery import ledger, schedule

    with ledger.change(seeded_db, actor="orrery", kind="setup", now=FROZEN_NOW):
        schedule.write_template(seeded_db, TEMPLATE)

    return seeded_db


@pytest.fixture
def week_home(week_db, orrery_home):
    """Returns orrery_home for tests whose code opens its own connections."""
    return orrery_home
