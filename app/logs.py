"""File logging, so failures that never reach the UI are still recoverable.

Apollo's "you ran out of mobile number credits" arrived inside a webhook body
and was silently discarded; everything worth diagnosing now lands in
data/logs/.
"""
import logging
from logging.handlers import RotatingFileHandler

from . import config

LOG_DIR = config.DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-10s %(message)s")
_cache: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    if name in _cache:
        return _cache[name]
    log = logging.getLogger(f"harvester.{name}")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(_FMT)
    log.addHandler(handler)
    _cache[name] = log
    return log
