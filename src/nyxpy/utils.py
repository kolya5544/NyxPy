from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

_LOGGER_NAME = "nyxpy"


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
        logger.addHandler(logging.NullHandler())
    return logger


def configure_logging(level: int = logging.INFO) -> None:
    logger = get_logger()
    logger.handlers = [h for h in logger.handlers if not isinstance(h, logging.NullHandler)]
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)


def _safe_json(obj: Mapping[str, Any]) -> Dict[str, Any]:
    REPLACEMENTS = {"password", "pass", "token", "access_token", "refresh_token"}
    redacted: Dict[str, Any] = {}
    for k, v in obj.items():
        redacted[k] = "***" if k in REPLACEMENTS else v
    return redacted


def _safe_json_obj(obj: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        return _safe_json(obj)
    except Exception:
        return {"_raw": str(obj)}
