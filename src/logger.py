from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR


def setup_logger(name: str, filename: str) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(LOG_DIR / filename, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


app_logger = setup_logger("app", "app.log")
alert_logger = setup_logger("alerts", "alerts.log")
