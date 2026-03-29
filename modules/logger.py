"""Halo logger -- rotating daily logs at ~/.halo/logs/"""

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


_logger = None


def get_logger(log_dir: Path = None, debug: bool = False) -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    log_dir = log_dir or Path.home() / ".halo" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("halo")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()

    # File handler -- daily rotation
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = TimedRotatingFileHandler(
        str(log_file), when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    # Console handler -- only in debug mode
    if debug:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_fmt = logging.Formatter("[HALO %(levelname)s] %(message)s")
        console_handler.setFormatter(console_fmt)
        logger.addHandler(console_handler)

    _logger = logger
    return logger


def log_tool_call(tool_name: str, args: dict, result: str, latency_ms: float = 0):
    logger = get_logger()
    logger.info(f"TOOL:{tool_name} args={args} latency={latency_ms:.0f}ms result_len={len(result)}")


def log_session_event(event: str, details: str = ""):
    logger = get_logger()
    logger.info(f"SESSION:{event} {details}".strip())


def log_vault_query(query: str, num_results: int, latency_ms: float = 0):
    logger = get_logger()
    logger.info(f"VAULT:query='{query}' results={num_results} latency={latency_ms:.0f}ms")


def log_error(context: str, error: Exception):
    logger = get_logger()
    logger.error(f"ERROR:{context} -- {type(error).__name__}: {error}")
