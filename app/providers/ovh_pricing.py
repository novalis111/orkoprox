"""OVH AI Endpoints — statische Pricing-Map + Cost-Berechnung.

Daten gepullt 2026-05-04 von `GET /v1/models` (anonymous lesbar) plus
Audio-Pricing (`audio_seconds`-Achse) aus echter OVH-Rechnung 2026-05-05.

Preise intern in **USD pro Einheit** (Token bzw. Audio-Sekunde).
OVH-Account-Billing in EUR — wir konvertieren beim Header-Output mit
USD_TO_EUR (statischer Wert).

WICHTIG — OVH-API-Inkonsistenz fuer Audio:
Der `/v1/models`-Endpoint zeigt fuer Whisper `prompt=0, completion=0`,
aber die echte Rechnung listet eine separate Achse
`whisper-large-v3-turbo-seconds` mit ~0.0000199 EUR/sec. Audio-Preise
sind also NICHT aus der Models-API ableitbar — wir tracken sie hier
manuell und korrigieren nach jeder OVH-Rechnung.

Refresh-Skript: `scripts/ovh_pricing_sync.py` zieht Token-Preise aus
`/v1/models`. Audio-Preise muessen separat aus der monatlichen OVH-
Rechnung in `OVH_AUDIO_PRICING_USD` nachgepflegt werden.

Pricing-Schema:
- prompt:              USD/Token (Input)
- completion:          USD/Token (Output)
- image:               USD/Image
- request:             USD/Request
- audio_seconds:       USD/Audio-Sekunde (NUR Whisper, NICHT in /v1/models)

Provenance:
- 2026-05-04 OVH switch, schema extended with image + request axes
- 2026-05-05 audio_seconds axis + real Whisper prices from OVH invoice
"""

from __future__ import annotations

from typing import Final

# Preise: USD pro einzelnem Token (oder pro Image bei `image`-Cost-Modellen).
# Entspricht $/1e6 fuer "pro 1M tokens".
# Beispiel: Mistral-7B prompt=0.00000011 USD/token = $0.11/M Tokens.
OVH_MODEL_PRICING_USD: Final[dict[str, dict[str, float]]] = {
    "Mistral-7B-Instruct-v0.3": {
        "prompt": 0.00000011, "completion": 0.00000011,
        "image": 0.0, "request": 0.0,
    },
    "Mistral-Nemo-Instruct-2407": {
        "prompt": 0.00000014, "completion": 0.00000014,
        "image": 0.0, "request": 0.0,
    },
    "Mistral-Small-3.2-24B-Instruct-2506": {
        "prompt": 0.0000001, "completion": 0.00000031,
        "image": 0.0, "request": 0.0,
    },
    "Llama-3.1-8B-Instruct": {
        "prompt": 0.00000011, "completion": 0.00000011,
        "image": 0.0, "request": 0.0,
    },
    "Meta-Llama-3_3-70B-Instruct": {
        "prompt": 0.00000074, "completion": 0.00000074,
        "image": 0.0, "request": 0.0,
    },
    "Qwen3-32B": {
        "prompt": 0.00000009, "completion": 0.00000025,
        "image": 0.0, "request": 0.0,
    },
    "Qwen3.5-9B": {
        "prompt": 0.00000012, "completion": 0.00000018,
        "image": 0.0, "request": 0.0,
    },
    "Qwen3-Coder-30B-A3B-Instruct": {
        "prompt": 0.00000007, "completion": 0.00000026,
        "image": 0.0, "request": 0.0,
    },
    "Qwen2.5-VL-72B-Instruct": {
        "prompt": 0.00000101, "completion": 0.00000101,
        "image": 0.0, "request": 0.0,
    },
    "gpt-oss-20b": {
        "prompt": 0.00000005, "completion": 0.00000018,
        "image": 0.0, "request": 0.0,
    },
    "gpt-oss-120b": {
        "prompt": 0.00000009, "completion": 0.00000047,
        "image": 0.0, "request": 0.0,
    },
    # Embeddings
    "bge-m3": {
        "prompt": 0.00000001, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "bge-multilingual-gemma2": {
        "prompt": 0.00000001, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "Qwen3-Embedding-8B": {
        "prompt": 0.00000012, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    # Free (Guard-Modelle)
    "Qwen3Guard-Gen-8B": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "Qwen3Guard-Gen-0.6B": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    # Whisper: Audio-Pricing in OVH_AUDIO_PRICING_USD (separate Achse, nicht
    # in /v1/models gelistet). Token-Felder hier 0, weil Whisper keine
    # Tokens berechnet. Caller MUSS audio_cost_usd() nutzen.
    "whisper-large-v3-turbo": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "whisper-large-v3": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    # Image-Generation: aktuell kostenlos (SDXL kostenfrei seit W3-Pre).
    "stable-diffusion-xl-base-v10": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "stabilityai/stable-diffusion-xl-base-1.0": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
    "ppl": {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.0,
    },
}


# Audio-Pricing (USD pro Audio-Sekunde).
# Quelle: OVH-Rechnung 2026-05-05.
# Rückrechnung:
#   whisper-large-v3-turbo: 503 sec → 0,01 EUR
#                        ⇒ 0.0000199 EUR/sec
#                        ⇒ ~0.0000216 USD/sec (bei 1 EUR = 1.087 USD)
# whisper-large-v3 (full) hatte in der Stichprobe nur 4 Sekunden,
# gerundet 0,00 EUR — als konservativer Schaetzwert 4x turbo, da
# v3-full in Bench ~3-5x langsamer/teurer als v3-turbo ist.
# WICHTIG: Bei jeder OVH-Rechnung gegenpruefen + ggf. korrigieren.
OVH_AUDIO_PRICING_USD: Final[dict[str, float]] = {
    "whisper-large-v3-turbo": 0.0000216,  # ~0.078 USD/h
    "whisper-large-v3": 0.0000864,        # ~0.311 USD/h (Schaetzwert 4x turbo)
}


# Pricing darf nicht davon abhaengen, ob ein Caller schon den Router-Resolve
# durchlaufen hat. Live-Befund 2026-05-06: Alias-Calls koennen sonst Tokens
# zaehlen, aber `cost_micro_usd` bleibt 0.
OVH_PRICING_MODEL_ALIASES: Final[dict[str, str]] = {
    "xhigh": "Meta-Llama-3_3-70B-Instruct",
    "high": "Mistral-Small-3.2-24B-Instruct-2506",
    "medium": "Mistral-Small-3.2-24B-Instruct-2506",
    "low": "Mistral-7B-Instruct-v0.3",
    "classify": "Mistral-7B-Instruct-v0.3",
    "extract": "Mistral-Small-3.2-24B-Instruct-2506",
    "compose": "Mistral-Small-3.2-24B-Instruct-2506",
    "chat": "Mistral-Small-3.2-24B-Instruct-2506",
    "reason": "gpt-oss-120b",
    "report": "Meta-Llama-3_3-70B-Instruct",
    "ocr": "Mistral-Small-3.2-24B-Instruct-2506",
    "vision": "Mistral-Small-3.2-24B-Instruct-2506",
    "vision_x": "Qwen2.5-VL-72B-Instruct",
    "image": "stable-diffusion-xl-base-v10",
    "voice": "whisper-large-v3-turbo",
    "voice_hq": "whisper-large-v3",
    "reason_lite": "gpt-oss-20b",
    "long_context": "Qwen3.5-9B",
    "reason_mid": "Qwen3-32B",
}


def resolve_pricing_model(model: str) -> str:
    """Normalize proxy aliases/provider-prefixed IDs to OVH pricing IDs."""
    normalized = (model or "").strip()
    if "/" in normalized:
        maybe_prefix, maybe_model = normalized.split("/", 1)
        if maybe_prefix.lower() == "ovh":
            normalized = maybe_model
    return OVH_PRICING_MODEL_ALIASES.get(normalized.lower(), normalized)

# FX-Konversion USD → EUR. Konservativer Statik-Wert. Bei Kursschwankungen
# +/- 5% Toleranz akzeptabel — Token-Metering ist intern, nicht Abrechnung.
USD_TO_EUR: Final[float] = 0.92


def cost_usd(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    *,
    n_images: int = 0,
    n_requests: int = 1,
) -> float:
    """Berechnet die OVH-Kosten in USD fuer einen Inference-Call.

    Deckt alle 4 OVH-Cost-Achsen ab (prompt, completion, image, request).
    Aktuell sind image+request bei OVH meist 0, aber das Schema ist
    aktiv-ready fuer zukuenftige OVH-Pricing-Updates.

    Args:
        model: OVH-Modell-ID (z.B. "Mistral-Small-3.2-24B-Instruct-2506").
        prompt_tokens: Input-Tokens (Default 0 — bei Image-Gen typischerweise 0).
        completion_tokens: Output-Tokens (Default 0).
        n_images: Anzahl generierter Bilder (Default 0). Nur fuer Image-Gen.
        n_requests: Anzahl Requests (Default 1). Wird mit `request`-Cost
                    multipliziert.

    Returns:
        Gesamtkosten in USD. Unbekannte Modelle → 0.0 (kein Hard-Fail,
        damit ein neues OVH-Modell den Request nicht killt).
    """
    pricing_model = resolve_pricing_model(model)
    pricing = OVH_MODEL_PRICING_USD.get(pricing_model)
    if not pricing:
        return 0.0
    return (
        prompt_tokens * pricing["prompt"]
        + completion_tokens * pricing["completion"]
        + n_images * pricing.get("image", 0.0)
        + n_requests * pricing.get("request", 0.0)
    )


def cost_eur(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    *,
    n_images: int = 0,
    n_requests: int = 1,
) -> float:
    """USD-Cost in EUR konvertiert."""
    return (
        cost_usd(
            model,
            prompt_tokens,
            completion_tokens,
            n_images=n_images,
            n_requests=n_requests,
        )
        * USD_TO_EUR
    )


def audio_cost_usd(model: str, duration_seconds: float) -> float:
    """OVH-Kosten in USD fuer Whisper-Transkription (audio_seconds-Achse).

    Audio-Pricing ist NICHT in /v1/models gelistet — Quelle ist die echte
    OVH-Rechnung. Caller (z.B. transcriptions-Endpoint) muessen dies
    statt cost_usd() nutzen, weil prompt/completion-Felder fuer Whisper
    immer 0 sind.

    Args:
        model: Whisper-Modell-ID.
        duration_seconds: Audio-Laenge in Sekunden (kommt aus OVH-Response
                          `data.duration` bei verbose_json).

    Returns:
        Kosten in USD. Unbekanntes Modell → 0.0.
    """
    pricing_model = resolve_pricing_model(model)
    rate = OVH_AUDIO_PRICING_USD.get(pricing_model)
    if rate is None or duration_seconds <= 0:
        return 0.0
    return float(duration_seconds) * rate


def audio_cost_eur(model: str, duration_seconds: float) -> float:
    """Audio-Cost in EUR."""
    return audio_cost_usd(model, duration_seconds) * USD_TO_EUR


def is_priced(model: str) -> bool:
    """True wenn fuer das Modell ein expliziter Preis hinterlegt ist."""
    pricing_model = resolve_pricing_model(model)
    return pricing_model in OVH_MODEL_PRICING_USD or pricing_model in OVH_AUDIO_PRICING_USD


# ---------------------------------------------------------------------------
# Token-Weighting
# ---------------------------------------------------------------------------
# Tenant-Quota wird in "virtuellen Tokens" gefuehrt. Anker = Mistral-Small
# Completion (0.31 USD/M = Faktor 1.0). Teurere Modelle ziehen proportional
# mehr Tokens vom Budget. Header-Format und Limit-Werte bleiben identisch
# zur Pre-Weighting-Welt (kein Cost-Header, KEINE Client-Aenderung noetig).
#
# Faktor-Heuristik: weight = max(prompt_per_token, completion_per_token) / 0.31e-6.
# Audio: USD/sec / 0.31e-6 = virtuelle Tokens/sec.
# Free-Modelle (Guard, SDXL): 0.0 → kostenfrei, blockt Quota nicht.

_ANCHOR_USD_PER_TOKEN: Final[float] = 0.00000031  # Mistral-Small Completion = 0.31 USD/M

MODEL_TOKEN_WEIGHT: Final[dict[str, float]] = {
    "Mistral-7B-Instruct-v0.3": 0.4,
    "Mistral-Nemo-Instruct-2407": 0.5,
    "Mistral-Small-3.2-24B-Instruct-2506": 1.0,  # Anker
    "Llama-3.1-8B-Instruct": 0.4,
    "Meta-Llama-3_3-70B-Instruct": 2.4,
    "Qwen3-32B": 0.85,
    "Qwen3.5-9B": 0.6,
    "Qwen3-Coder-30B-A3B-Instruct": 0.85,
    "Qwen2.5-VL-72B-Instruct": 3.3,
    "gpt-oss-20b": 0.6,
    "gpt-oss-120b": 1.5,
    "bge-m3": 0.05,
    "bge-multilingual-gemma2": 0.05,
    "Qwen3-Embedding-8B": 0.4,
    # Free
    "Qwen3Guard-Gen-0.6B": 0.0,
    "Qwen3Guard-Gen-8B": 0.0,
    "stable-diffusion-xl-base-v10": 0.0,
    "stabilityai/stable-diffusion-xl-base-1.0": 0.0,
}

# Virtuelle Tokens/Audio-Sekunde = (USD/sec) / (USD/token-Anker).
#   turbo: 0.0000216 / 0.00000031 ≈ 69.7 vTokens/sec
#   v3-full: 0.0000864 / 0.00000031 ≈ 278.7 vTokens/sec
# Sanity: 1 Min turbo ≈ 4.181 vTokens. 1 h turbo ≈ 250.838 vTokens
# (~ 0,072 EUR — entspricht ~250k Mistral-Small-Output-Tokens, was
# inhaltlich ein 200-300-Seiten-Roman waere. Faire Quota-Achse.)
AUDIO_TOKEN_WEIGHT_PER_SEC: Final[dict[str, float]] = {
    "whisper-large-v3-turbo": 69.7,
    "whisper-large-v3": 278.7,
}


def billable_tokens(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    audio_seconds: float = 0.0,
) -> int:
    """Gewichtete Tokens fuer Quota-Tracking.

    Tenant sieht weiter Tokens — teurere Modelle ziehen aber proportional
    mehr aus seinem Budget. Anker = Mistral-Small (1.0). Llama-70B = 2.4×,
    Qwen2.5-VL = 3.3×, Whisper-turbo = 0.07/sec (peanuts), kostenlose
    Modelle (Guard, SDXL) = 0×.

    Unbekannte Modelle: weight=1.0 (konservativ — eher mehr ziehen als zu
    wenig, damit ein neuer OVH-Eintrag nicht silent das Budget umgeht).

    Args:
        model: Modell-ID (Original, NICHT aufgeloeste OpenAI-Compat-ID).
        prompt_tokens: Eingabe-Tokens.
        completion_tokens: Ausgabe-Tokens.
        audio_seconds: Audio-Laenge bei Whisper-Calls.

    Returns:
        Gewichtete (virtuelle) Token-Anzahl, mindestens 0. Bei reinen
        Free-Modellen ohne Audio: 0.
    """
    pricing_model = resolve_pricing_model(model)
    w = MODEL_TOKEN_WEIGHT.get(pricing_model, 1.0)
    audio_w = AUDIO_TOKEN_WEIGHT_PER_SEC.get(pricing_model, 0.0)
    weighted = (prompt_tokens + completion_tokens) * w + audio_seconds * audio_w
    if weighted <= 0:
        return 0
    return int(round(weighted))


__all__ = [
    "AUDIO_TOKEN_WEIGHT_PER_SEC",
    "MODEL_TOKEN_WEIGHT",
    "OVH_AUDIO_PRICING_USD",
    "OVH_MODEL_PRICING_USD",
    "OVH_PRICING_MODEL_ALIASES",
    "USD_TO_EUR",
    "audio_cost_eur",
    "audio_cost_usd",
    "billable_tokens",
    "cost_eur",
    "cost_usd",
    "is_priced",
    "resolve_pricing_model",
]
