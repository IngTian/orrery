import os
import pathlib
import pwd
import shutil
import subprocess
import tomllib
import zipfile

import pytest


def test_version_matches_pyproject():
    """`pyproject.toml` and `__version__` are two places carrying one fact.

    Nothing forced them to agree, so a bump could touch one and leave the other —
    and the symptom is `orrery --version` disagreeing with the wheel anyone installed,
    which is the kind of thing nobody notices until it matters.
    """
    import orrery

    root = pathlib.Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert declared == orrery.__version__, (
        f"pyproject says {declared}, orrery.__version__ says {orrery.__version__}"
    )


def test_main_help_exits_zero():
    """The help screen should exit cleanly with code 0."""
    from orrery.__main__ import main

    try:
        code = main(["--help"])
    except SystemExit as e:
        code = int(e.code) if e.code else 0

    assert code == 0, "Help should exit with code 0"


def test_tcss_is_package_data():
    """Verify that *.tcss files are declared as package data for orrery.tui."""
    root = pathlib.Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    package_data = config.get("tool", {}).get("setuptools", {}).get("package-data", {})

    assert "orrery.tui" in package_data, "orrery.tui not in package-data"
    assert "*.tcss" in package_data["orrery.tui"], "*.tcss not in orrery.tui package-data"


@pytest.mark.skipif(shutil.which("uv") is None, reason="building the wheel needs uv")
def test_wheel_ships_the_stylesheet(tmp_path):
    """A built wheel carries orrery/tui/app.tcss: without it an installed orrery starts
    unstyled. Built from a copy, so the repo gets no build/ output."""
    root = pathlib.Path(__file__).resolve().parents[1]
    src = tmp_path / "src"
    shutil.copytree(root / "orrery", src / "orrery", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(root / name, src / name)
    # uv's own cache lives under the real HOME; the suite's HOME is a temporary one.
    env = {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir}
    dist = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(dist), str(src)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if built.returncode and "network was disabled" in built.stderr:
        pytest.skip("the build backend isn't in uv's cache, and the build runs offline")
    assert built.returncode == 0, built.stderr
    (wheel,) = dist.glob("*.whl")
    assert "orrery/tui/app.tcss" in zipfile.ZipFile(wheel).namelist()


def test_external_runners_disabled():
    """The autouse fixture should set ORRERY_DISABLE_EXTERNAL_RUNNERS=1."""
    assert os.environ["ORRERY_DISABLE_EXTERNAL_RUNNERS"] == "1"
