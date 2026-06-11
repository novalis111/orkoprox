from __future__ import annotations

import json
import logging
from typing import Any


def configure_logging(level: str, json_logs: bool, log_file: str = "") -> logging.Logger:
    logger = logging.getLogger("llm-unified-proxy")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        if json_logs:
            file_handler.setFormatter(logging.Formatter("%(message)s"))
        else:
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=True, default=str))


# Truncation-Limit fuer Upstream-Fehler-Bodies in Logs: genug, um die
# konkrete Parameter-Meldung zu sehen (z.B. "max_tokens must be at least
# 1, got -28938"), ohne die Logs zu fluten. Fehler-Bodies enthalten
# Parameter-Infos, keine Prompts.
_PROVIDER_RESPONSE_LOG_LIMIT = 500


def _provider_status_of(exc: Any) -> Any:
    """Lies ``provider_status`` aus ProviderError.details (duck-typed)."""
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        return details.get("provider_status")
    return None


def _provider_response_excerpt(exc: Any) -> str | None:
    """Serialisiere ``provider_response`` aus ProviderError.details, truncated.

    Gibt den rohen Upstream-Fehler-Body als String zurueck (auf
    ``_PROVIDER_RESPONSE_LOG_LIMIT`` Zeichen gekuerzt), damit Logs die echte
    Upstream-Diagnose tragen statt nur "ovh error 400". ``None`` wenn keine
    Detail-Info vorhanden ist.
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    provider_response = details.get("provider_response")
    if provider_response is None:
        return None
    if isinstance(provider_response, str):
        text = provider_response
    else:
        try:
            text = json.dumps(provider_response, ensure_ascii=True, default=str)
        except Exception:
            text = str(provider_response)
    return text[:_PROVIDER_RESPONSE_LOG_LIMIT]
