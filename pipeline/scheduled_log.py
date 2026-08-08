"""Rotating file logs for scheduled ingestion runs. Never log secrets."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_configured = False


def setup_scheduled_logging(log_dir: str | Path | None = None) -> Path:
    """Attach a timed rotating handler under logs/scheduled/. Idempotent."""
    global _configured
    root = Path(__file__).resolve().parents[1]
    log_path = Path(log_dir) if log_dir else root / "logs" / "scheduled"
    log_path.mkdir(parents=True, exist_ok=True)
    logfile = log_path / "ctw-scheduled.log"

    if _configured:
        return logfile

    handler = TimedRotatingFileHandler(
        filename=str(logfile),
        when="D",
        interval=1,
        backupCount=21,  # ~3 weeks
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    # Never attach secrets — redact common env-looking patterns in a filter
    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = str(record.getMessage())
            for key in ("DISCORD_WEBHOOK", "API_KEY", "GEMINI_API", "OPENAI_API", "Bearer "):
                if key in msg:
                    record.msg = "[REDACTED sensitive log line]"
                    record.args = ()
            return True

    handler.addFilter(_Redact())
    _configured = True
    logging.getLogger("ctw.full_cycle").info("Scheduled logging → %s", logfile)
    return logfile
