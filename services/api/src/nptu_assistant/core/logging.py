from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pythonjsonlogger.json import JsonFormatter


_SENSITIVE_FIELDS = ("api_key", "authorization", "password", "question", "secret", "token")


def redact_sensitive_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if any(marker in str(key).lower() for marker in _SENSITIVE_FIELDS)
                else redact_sensitive_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_fields(item) for item in value]
    return value


class RedactingJsonFormatter(JsonFormatter):
    def process_log_record(self, log_record: dict[str, Any]) -> dict[str, Any]:
        return redact_sensitive_fields(log_record)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
