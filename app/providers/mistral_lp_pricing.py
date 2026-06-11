"""Mistral La Plateforme — statische Pricing-Map + Token-Weighting.

Modell-Preise (USD pro Token, gepullt aus mistral.ai/pricing 2026-05):
- mistral-large-latest: $2.00/$6.00 per M = 0.000002 / 0.000006 USD/Token
- mistral-small-latest: $0.20/$0.60 per M = 0.0000002 / 0.0000006 USD/Token
- mistral-medium-latest: $0.40/$2.00 per M (Stand 2024-09)
- pixtral-large-latest: $2.00/$6.00 per M (Vision-Premium, Mistral-Large multimodal)
- ministral-3b-latest: $0.04/$0.04 per M (kleiner Mini)
- ministral-8b-latest: $0.10/$0.10 per M
- codestral-latest: $0.30/$0.90 per M (Code-spezialisiert)

Anker für Token-Weighting (identisch zu ovh_pricing):
    Mistral-Small Output via OVH = $0.31/M = Faktor 1.0

Damit ist mistral-large (Mistral-LP, $6.00 Output) ~ Faktor 19.4 — der
Client zieht 19.4× so viele virtuelle Tokens für jeden Output-Token
verglichen mit OVH-Mistral-Small. Spiegelt die echte Cost-Realität.

Mistral-LP ist deutlich teurer als OVH und wird für hochwertige
Premium-Anwendungsfälle eingesetzt. Clients müssen den Premium-Alias
EXPLIZIT anfordern (`report_premium`, `report_structure`).
"""

from __future__ import annotations

from typing import Final

# Preise USD pro einzelnem Token (1e6 = "pro 1M Tokens").
MISTRAL_LP_PRICING_USD: Final[dict[str, dict[str, float]]] = {
    "mistral-large-latest": {
        "prompt": 0.000002, "completion": 0.000006,
        "image": 0.0, "request": 0.0,
    },
    "mistral-large-2407": {
        "prompt": 0.000002, "completion": 0.000006,
        "image": 0.0, "request": 0.0,
    },
    "mistral-medium-latest": {
        "prompt": 0.0000004, "completion": 0.000002,
        "image": 0.0, "request": 0.0,
    },
    "mistral-small-latest": {
        "prompt": 0.0000002, "completion": 0.0000006,
        "image": 0.0, "request": 0.0,
    },
    "ministral-3b-latest": {
        "prompt": 0.00000004, "completion": 0.00000004,
        "image": 0.0, "request": 0.0,
    },
    "ministral-8b-latest": {
        "prompt": 0.0000001, "completion": 0.0000001,
        "image": 0.0, "request": 0.0,
    },
    "codestral-latest": {
        "prompt": 0.0000003, "completion": 0.0000009,
        "image": 0.0, "request": 0.0,
    },
    "pixtral-large-latest": {
        "prompt": 0.000002, "completion": 0.000006,
        "image": 0.0, "request": 0.0,
    },
}


# Aliases: Mistral hat versionierte und unversionierte Modellnamen.
# Normalisierung: alles → "-latest"-Schreibweise für die Pricing-Map.
MISTRAL_LP_PRICING_MODEL_ALIASES: Final[dict[str, str]] = {
    "mistral-large": "mistral-large-latest",
    "mistral-medium": "mistral-medium-latest",
    "mistral-small": "mistral-small-latest",
    "ministral-3b": "ministral-3b-latest",
    "ministral-8b": "ministral-8b-latest",
    "codestral": "codestral-latest",
    "pixtral-large": "pixtral-large-latest",
}


def resolve_pricing_model(model: str) -> str:
    """Normalisiere Modellname zu Pricing-Map-Key.

    Akzeptiert:
    - `"mistral-large-latest"` (canonical)
    - `"mistral-large"` (alias → canonical)
    - `"Mistral-Large"` (case-insensitive)
    """
    if not model:
        return ""
    normalized = model.strip()
    if normalized in MISTRAL_LP_PRICING_USD:
        return normalized
    lower = normalized.lower()
    return MISTRAL_LP_PRICING_MODEL_ALIASES.get(lower, lower)


# Anker = OVH Mistral-Small completion (0.31 USD/M). Identisch zu ovh_pricing
# damit Token-Weights provider-übergreifend vergleichbar sind.
_ANCHOR_USD_PER_TOKEN: Final[float] = 0.00000031

MISTRAL_LP_TOKEN_WEIGHT: Final[dict[str, float]] = {
    # large = 6.00/0.31 ≈ 19.4 — Premium-Anker
    "mistral-large-latest": 19.4,
    "mistral-large-2407": 19.4,
    # medium = 2.00/0.31 ≈ 6.5
    "mistral-medium-latest": 6.5,
    # small = 0.60/0.31 ≈ 1.9 (etwas teurer als OVH Mistral-Small wegen $0.60 vs $0.31)
    "mistral-small-latest": 1.9,
    # Tiny-Tier (Mini-Modelle): unter Anker, billig
    "ministral-3b-latest": 0.15,
    "ministral-8b-latest": 0.35,
    # Codestral: spezialisiert, mittlere Stufe
    "codestral-latest": 2.9,
    # Pixtral = Mistral-Large multimodal, gleicher Preis
    "pixtral-large-latest": 19.4,
}


def billable_tokens(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> int:
    """Gewichtete Tokens für Quota-Tracking (Mistral-LP-Pfad).

    Anker = OVH-Mistral-Small (1.0). Mistral-LP-Large = 19.4× pro Token,
    sehr teuer — Konsumenten sollen das spüren.

    Unbekannte Modelle: weight = 5.0 (konservativ, da Mistral-LP grundsätzlich
    Premium-Preise hat — keinen Free-Lunch-Default).
    """
    pricing_model = resolve_pricing_model(model)
    weight = MISTRAL_LP_TOKEN_WEIGHT.get(pricing_model, 5.0)
    weighted = (prompt_tokens + completion_tokens) * weight
    if weighted <= 0:
        return 0
    return int(round(weighted))


def cost_usd(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> float:
    """Reale Kosten in USD."""
    pricing_model = resolve_pricing_model(model)
    pricing = MISTRAL_LP_PRICING_USD.get(pricing_model)
    if not pricing:
        return 0.0
    return (
        prompt_tokens * pricing["prompt"]
        + completion_tokens * pricing["completion"]
    )


def is_priced(model: str) -> bool:
    """True wenn das Modell in der Pricing-Map liegt."""
    return resolve_pricing_model(model) in MISTRAL_LP_PRICING_USD


__all__ = [
    "MISTRAL_LP_PRICING_USD",
    "MISTRAL_LP_PRICING_MODEL_ALIASES",
    "MISTRAL_LP_TOKEN_WEIGHT",
    "billable_tokens",
    "cost_usd",
    "is_priced",
    "resolve_pricing_model",
]
