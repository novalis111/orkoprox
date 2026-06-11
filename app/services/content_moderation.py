"""Qwen3Guard content-moderation layer.

Pre-filter (Qwen3Guard-Gen-0.6B): checks user input before LLM call.
Post-filter (Qwen3Guard-Gen-8B): checks LLM output before delivery.

EU AI Act mapping: Art. 9 (Risk), 13 (Transparency), 14 (Human-in-Loop), 52 (AI labelling).

Cost: $0.0/Token (free tier OVH).
Latency (Bench 2026-05-04): 0.6B p95=436ms, 8B p95=759ms.

Fail-open: bei Guard-Ausfall NICHT blockieren (Verfuegbarkeits-Pflicht);
Audit-Log markiert "guard_unavailable" fuer Ops-Alert.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# ─── Hard-Block-Kategorien ────────────────────────────────────────────────
# Pre-Filter: Strafrechtsrelevant + Jailbreak + PII Dritter.
PRE_BLOCK_CATEGORIES = frozenset({
    "Violent",
    "Non-Violent Illegal Acts",
    "Non-violent Illegal Acts",  # OVH-Schreibweise variiert
    "Suicide & Self-Harm",
    "Jailbreak",
    "PII",
})

# Post-Filter: Toxic-Output (Hate/Harassment/Violence im LLM-Output).
POST_BLOCK_CATEGORIES = frozenset({
    "Violent",
    "Suicide & Self-Harm",
    "Unethical Acts",
    "Sexual Content",
})

SAFE_FALLBACK_TEXT = (
    "Entschuldigung, ich kann auf diese Anfrage nicht antworten. "
    "Bitte formuliere deine Frage anders."
)

# ─── Output-Parser (Qwen3Guard liefert Klartext, kein JSON) ──────────────
_SAFETY_RE = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)", re.IGNORECASE)
_CATEGORIES_RE = re.compile(r"Categories:\s*(.+?)(?:\n|$)", re.IGNORECASE)


@dataclass(frozen=True)
class GuardDecision:
    """Decision eines Guard-Calls. Immutable."""
    safety: str  # "Safe" | "Unsafe" | "Controversial" | "Unknown" (fail-open)
    categories: tuple[str, ...]
    is_blocked: bool
    reason: str
    latency_ms: float
    model: str

    def to_audit_dict(self) -> dict:
        """Flacher Dict fuer Audit-Log-Persistence."""
        return {
            "safety": self.safety,
            "categories": list(self.categories),
            "blocked": self.is_blocked,
            "reason": self.reason,
            "latency_ms": int(self.latency_ms),
            "model": self.model,
        }


def _parse_guard_output(text: str) -> tuple[str, tuple[str, ...]]:
    """Parst Qwen3Guard-Output. Robust gegen Whitespace/Case-Variationen."""
    safety_match = _SAFETY_RE.search(text)
    cats_match = _CATEGORIES_RE.search(text)
    safety = safety_match.group(1).capitalize() if safety_match else "Unknown"
    cats_raw = cats_match.group(1).strip() if cats_match else ""
    if cats_raw.lower() in ("none", "", "n/a"):
        cats: tuple[str, ...] = ()
    else:
        cats = tuple(c.strip() for c in cats_raw.split(",") if c.strip())
    return safety, cats


def _is_blocked(safety: str, categories: tuple[str, ...], block_set: frozenset[str]) -> bool:
    """True wenn Decision blockiert werden soll."""
    if safety != "Unsafe":
        return False
    return bool(set(categories) & block_set)


async def check_safety_pre(
    user_input: str,
    *,
    base_url: str,
    api_key: str,
    model: str = "Qwen3Guard-Gen-0.6B",
    timeout_s: float = 2.0,
    fail_open: bool = True,
) -> GuardDecision:
    """Pre-LLM-Filter: User-Input gegen Qwen3Guard-Gen-0.6B.

    Bei Block: HTTP 451 vom Caller. Bei fail_open + Guard-Ausfall:
    safety="Unknown", is_blocked=False, reason="guard_unavailable".
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": user_input}],
                    "max_tokens": 50,
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        elapsed = (time.perf_counter() - started) * 1000
        logger.warning(
            "Guard-Pre %s failed (%s) — fail_open=%s",
            model, type(e).__name__, fail_open,
        )
        return GuardDecision(
            safety="Unknown",
            categories=(),
            is_blocked=False if fail_open else True,
            reason="guard_unavailable",
            latency_ms=elapsed,
            model=model,
        )

    elapsed = (time.perf_counter() - started) * 1000
    safety, cats = _parse_guard_output(text)
    blocked = _is_blocked(safety, cats, PRE_BLOCK_CATEGORIES)
    return GuardDecision(
        safety=safety,
        categories=cats,
        is_blocked=blocked,
        reason=f"{safety}/{','.join(cats) or 'None'}",
        latency_ms=elapsed,
        model=model,
    )


async def check_safety_post(
    user_input: str,
    llm_output: str,
    *,
    base_url: str,
    api_key: str,
    model: str = "Qwen3Guard-Gen-8B",
    timeout_s: float = 3.0,
    fail_open: bool = True,
) -> GuardDecision:
    """Post-LLM-Filter: LLM-Output gegen Qwen3Guard-Gen-8B.

    Bei Block: Output wird durch SAFE_FALLBACK_TEXT ersetzt + Header
    {prefix}-Guard-Blocked: post.
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": user_input or "?"},
                        {"role": "assistant", "content": llm_output},
                    ],
                    "max_tokens": 50,
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        elapsed = (time.perf_counter() - started) * 1000
        logger.warning(
            "Guard-Post %s failed (%s) — fail_open=%s",
            model, type(e).__name__, fail_open,
        )
        return GuardDecision(
            safety="Unknown",
            categories=(),
            is_blocked=False if fail_open else True,
            reason="guard_unavailable",
            latency_ms=elapsed,
            model=model,
        )

    elapsed = (time.perf_counter() - started) * 1000
    safety, cats = _parse_guard_output(text)
    blocked = _is_blocked(safety, cats, POST_BLOCK_CATEGORIES)
    return GuardDecision(
        safety=safety,
        categories=cats,
        is_blocked=blocked,
        reason=f"{safety}/{','.join(cats) or 'None'}",
        latency_ms=elapsed,
        model=model,
    )


__all__ = [
    "GuardDecision",
    "PRE_BLOCK_CATEGORIES",
    "POST_BLOCK_CATEGORIES",
    "SAFE_FALLBACK_TEXT",
    "check_safety_post",
    "check_safety_pre",
]
