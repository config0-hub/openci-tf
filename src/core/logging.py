# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured logging for openci-tf Lambda functions."""

from __future__ import annotations

import json
import logging
import os
import sys


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured for structured JSON output in Lambda."""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)

    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    return logger


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        # Merge any extra fields passed via logger.info("msg", extra={...})
        for key in ("trace_id", "run_id", "order_name", "action", "repo",
                    "source", "trigger_id", "event_type", "status_code"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry)
