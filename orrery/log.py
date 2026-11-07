import logging
import logging.handlers

from orrery import paths

_MAX_BYTES = 1_000_000  # 1 MB
_BACKUPS = 3


def setup_logging() -> None:
    """Set up rotating file logging for orrery.

    Logs are written to log_dir()/orrery.log with rotation at 1 MB × 3 backups.
    """
    d = paths.log_dir()  # looked up per call, so a patched log_dir is the one used
    d.mkdir(parents=True, exist_ok=True)
    path = d / "orrery.log"
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
