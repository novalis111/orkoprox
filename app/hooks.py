"""app/hooks.py — Pluggable Guard-Hook interface (F6).

Pre/post-request hooks for compliance, PII redaction, and output tagging.
Off by default; activated via GUARD_HOOKS env var (comma-separated names).

Built-in hooks (v0.1):
  pii_redact   — masks PII in user input before it reaches the provider
  ai_act_tag   — tags LLM output as AI-generated (EU AI Act Art. 50)

Custom hooks via dotted-path ("my.module:MyHook") are supported when the
class is importable at runtime; unknown names are ignored with a warning.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class HookResult:
    """Return value from every hook call."""

    text: str
    """The (possibly transformed) text."""

    tags: dict[str, str] = field(default_factory=dict)
    """Key/value pairs that will be emitted as response headers
    ``{brand_prefix}-Hook-{key}: {value}``."""

    blocked: bool = False
    """True → gateway returns HTTP 451 and the request is not forwarded."""

    block_reason: str = ""
    """Human-readable reason surfaced in the 451 body (only used when blocked=True)."""


# ── Hook protocol ─────────────────────────────────────────────────────────────

@runtime_checkable
class GuardHook(Protocol):
    """Protocol every guard hook must satisfy."""

    name: str

    def pre(self, text: str, context: dict) -> HookResult:
        """Called on the last user message *before* it is sent to the provider.

        May transform the text (e.g. redact PII), add tags, or block the
        request entirely.  Returns a HookResult; never raises.
        """
        ...

    def post(self, text: str, context: dict) -> HookResult:
        """Called on the assistant reply *after* it is received from the provider.

        May transform the text or add tags.  Post-hooks cannot block in v0.1
        (the response has already been received; blocking is done via pre).
        Returns a HookResult; never raises.
        """
        ...


# ── Built-in: PII redaction ───────────────────────────────────────────────────

# Patterns are intentionally conservative: false negatives are safer than
# false positives that destroy meaningful prompt content.
_PII_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "email",
        "[EMAIL]",
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE),
    ),
    (
        "iban",
        "[IBAN]",
        # DE/AT/CH/NL/… IBANs: country code + 2 check digits + BBAN
        re.compile(r"\b[A-Z]{2}\d{2}[\s]?(?:\d{4}[\s]?){3,6}\d{1,4}\b"),
    ),
    (
        "card",
        "[CARD]",
        # 16-digit card numbers with optional spaces/dashes (Luhn not checked — conservative)
        re.compile(r"\b(?:\d[\s\-]?){15}\d\b"),
    ),
    (
        "phone",
        "[PHONE]",
        # International (+49…) and German local formats (0… with optional separators)
        re.compile(
            r"(?<!\d)"
            r"(?:\+\d{1,3}[\s\-./]?)?"       # optional country code
            r"(?:\(0?\d{1,5}\)[\s\-./]?)?"    # optional area code in parens
            r"(?:0\d{2,5}[\s\-./]?)?"         # optional leading 0 + area
            r"\d{3,5}"                          # subscriber start
            r"(?:[\s\-./]?\d{2,5}){1,3}"       # remaining blocks
            r"(?!\d)"
        ),
    ),
]


class PIIRedactHook:
    """Pre-hook: replaces common PII tokens with labelled placeholders.

    post() is a no-op — LLM output is not redacted (the caller receives it).
    """

    name = "pii_redact"

    def pre(self, text: str, context: dict) -> HookResult:  # noqa: ARG002
        result = text
        count = 0

        # Apply in a fixed order so that card-pattern doesn't eat IBAN digits
        # first.  IBAN is matched before card for the same reason.
        for _name, placeholder, pattern in _PII_PATTERNS:
            new, n = pattern.subn(placeholder, result)
            result = new
            count += n

        return HookResult(
            text=result,
            tags={"pii-redacted": str(count)},
        )

    def post(self, text: str, context: dict) -> HookResult:  # noqa: ARG002
        return HookResult(text=text)


# ── Built-in: EU AI Act transparency tag ─────────────────────────────────────

class AIActTagHook:
    """Post-hook: marks LLM output as AI-generated (EU AI Act Art. 50).

    pre() is a no-op.  The hook only adds a header tag; it does NOT modify
    the response text so downstream clients receive the unaltered content.
    """

    name = "ai_act_tag"

    def pre(self, text: str, context: dict) -> HookResult:  # noqa: ARG002
        return HookResult(text=text)

    def post(self, text: str, context: dict) -> HookResult:  # noqa: ARG002
        return HookResult(
            text=text,
            tags={"ai-generated": "true"},
        )


# ── Registry + loader ─────────────────────────────────────────────────────────

_BUILTIN_HOOKS: dict[str, type[GuardHook]] = {
    "pii_redact": PIIRedactHook,  # type: ignore[dict-item]
    "ai_act_tag": AIActTagHook,   # type: ignore[dict-item]
}


def _load_dotted_path(path: str) -> GuardHook | None:
    """Import ``module.path:ClassName`` and return an instance.

    Returns None and logs a warning on any error.
    """
    if ":" not in path:
        logger.warning("guard_hooks: %r is not a known built-in and has no ':' — skipping", path)
        return None
    module_path, class_name = path.rsplit(":", 1)
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        instance = cls()
        if not isinstance(instance, GuardHook):
            logger.warning(
                "guard_hooks: %r does not satisfy GuardHook protocol — skipping", path
            )
            return None
        return instance
    except Exception:  # noqa: BLE001
        logger.warning("guard_hooks: failed to load %r — skipping", path, exc_info=True)
        return None


def load_hooks(spec: str) -> list[GuardHook]:
    """Parse a comma-separated hook spec and return instantiated hooks.

    Built-in names (``pii_redact``, ``ai_act_tag``) are resolved directly.
    Dotted-path names (``my.module:MyHook``) are imported at call time.
    Unknown names without a ``:`` separator are ignored with a warning.
    An empty spec returns an empty list (off by default).
    """
    if not spec or not spec.strip():
        return []

    hooks: list[GuardHook] = []
    for raw in spec.split(","):
        name = raw.strip()
        if not name:
            continue
        if name in _BUILTIN_HOOKS:
            hooks.append(_BUILTIN_HOOKS[name]())  # type: ignore[abstract]
        else:
            instance = _load_dotted_path(name)
            if instance is not None:
                hooks.append(instance)

    return hooks


# ── Runner helpers ────────────────────────────────────────────────────────────

def run_pre_hooks(
    hooks: list[GuardHook],
    text: str,
    context: dict,
) -> tuple[str, dict[str, str], bool, str]:
    """Run all pre-hooks in order.

    Each hook receives the text as (possibly already) transformed by the
    previous hook.  Tags from all hooks are merged (later hooks win on
    key collision).

    A hook that returns ``blocked=True`` stops the chain immediately.
    A hook that raises is caught, a warning is logged, and processing
    continues with the next hook (fail-open).

    Returns:
        (final_text, merged_tags, blocked, block_reason)
    """
    merged_tags: dict[str, str] = {}
    for hook in hooks:
        try:
            result = hook.pre(text, context)
        except Exception:  # noqa: BLE001
            logger.warning(
                "guard_hooks: pre() on hook %r raised — skipping (fail-open)",
                hook.name,
                exc_info=True,
            )
            continue
        text = result.text
        merged_tags.update(result.tags)
        if result.blocked:
            return text, merged_tags, True, result.block_reason
    return text, merged_tags, False, ""


def run_post_hooks(
    hooks: list[GuardHook],
    text: str,
    context: dict,
) -> tuple[str, dict[str, str]]:
    """Run all post-hooks in order.

    Post-hooks cannot block in v0.1 (output already received from provider).
    A hook that raises is caught and skipped (fail-open).

    Returns:
        (final_text, merged_tags)
    """
    merged_tags: dict[str, str] = {}
    for hook in hooks:
        try:
            result = hook.post(text, context)
        except Exception:  # noqa: BLE001
            logger.warning(
                "guard_hooks: post() on hook %r raised — skipping (fail-open)",
                hook.name,
                exc_info=True,
            )
            continue
        text = result.text
        merged_tags.update(result.tags)
    return text, merged_tags
