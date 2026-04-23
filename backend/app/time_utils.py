from __future__ import annotations

from datetime import datetime


def utcnow_naive() -> datetime:
    """
    Return naive UTC datetime.

    We keep DB fields naive-UTC for now (no timezone-aware migration yet).
    """

    return datetime.utcnow()

