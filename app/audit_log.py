"""Append-only audit log.

Records one line per admin action and (optionally) per request: who, when,
which key (prefix only), which model, and cost. It NEVER logs the full API key
and NEVER logs prompt or response content — privacy by default.

The log is newline-delimited JSON (JSONL). Writes are best-effort: an audit
failure is logged but never propagated to the request path, so auditing can
never take the gateway down.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def mask_key(api_key: str | None) -> str:
    """Return a non-reversible reference to a key for audit purposes.

    Keeps a short prefix for correlation; the rest is never stored.
    """
    if not api_key:
        return "anonymous"
    key = api_key.strip()
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:8] + "..."


class AuditLog:
    """Best-effort append-only JSONL audit sink."""

    def __init__(self, enabled: bool = False, path: str | None = None) -> None:
        self._enabled = enabled
        self._path = Path(path) if path else None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._path is not None

    def record(self, event: str, **fields: Any) -> None:
        """Append one audit event. Never raises.

        ``api_key`` in ``fields`` is masked to a prefix automatically. Callers
        must NOT pass prompt/response content.
        """
        if not self.enabled:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
        }
        for key, value in fields.items():
            if key == "api_key":
                entry["key"] = mask_key(value)
            else:
                entry[key] = value
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            assert self._path is not None  # narrowed by self.enabled
            with self._lock:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 — auditing must never break a request
            logger.warning("audit_log write failed: %s", exc)
