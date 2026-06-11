"""Tests for app/providers/ovh_pricing.py — all 4 cost axes.

cost_usd() covers prompt + completion + image + request. Image and request
cost axes are currently 0 at OVH, but the schema is ready for future
OVH pricing updates.
"""

from __future__ import annotations

from app.providers.ovh_pricing import (
    AUDIO_TOKEN_WEIGHT_PER_SEC,
    MODEL_TOKEN_WEIGHT,
    OVH_AUDIO_PRICING_USD,
    OVH_MODEL_PRICING_USD,
    USD_TO_EUR,
    audio_cost_eur,
    audio_cost_usd,
    billable_tokens,
    cost_eur,
    cost_usd,
    is_priced,
    resolve_pricing_model,
)


# ─── Schema-Integritaet: alle Modelle haben alle 4 Achsen ─────────────


def test_all_models_have_four_cost_axes():
    """Jedes Modell muss alle 4 Cost-Achsen haben (prompt/completion/image/request)."""
    required_axes = {"prompt", "completion", "image", "request"}
    for model, pricing in OVH_MODEL_PRICING_USD.items():
        missing = required_axes - set(pricing.keys())
        assert not missing, f"Model '{model}' fehlen Cost-Achsen: {missing}"


def test_all_costs_are_floats_and_nonnegative():
    for model, pricing in OVH_MODEL_PRICING_USD.items():
        for axis, value in pricing.items():
            assert isinstance(value, float), f"{model}.{axis} ist nicht float: {type(value)}"
            assert value >= 0, f"{model}.{axis} ist negativ: {value}"


# ─── cost_usd() — Standard Token-Cost ──────────────────────────────────


def test_cost_usd_mistral_small_chat_default():
    # Mistral-Small-3.2-24B: $0.10/M prompt + $0.31/M completion
    # 1000 prompt + 500 completion = 1000*0.0000001 + 500*0.00000031
    cost = cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 1000, 500)
    expected = 1000 * 0.0000001 + 500 * 0.00000031
    assert abs(cost - expected) < 1e-12


def test_cost_usd_resolves_chat_alias():
    """Alias-Calls duerfen Cost-Tracking nicht auf 0 setzen."""
    expected = cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 1000, 500)
    assert cost_usd("chat", 1000, 500) == expected


def test_cost_usd_resolves_ovh_prefixed_model():
    expected = cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 1000, 500)
    assert cost_usd("ovh/Mistral-Small-3.2-24B-Instruct-2506", 1000, 500) == expected


def test_cost_usd_llama_70b_xhigh():
    # Meta-Llama-3.3-70B: $0.74/M sym.
    cost = cost_usd("Meta-Llama-3_3-70B-Instruct", 1000, 1000)
    expected = 2000 * 0.00000074
    assert abs(cost - expected) < 1e-12


def test_cost_usd_gpt_oss_120b_reason_asymmetric():
    # gpt-oss-120b: $0.09/M prompt + $0.47/M completion (asymmetrisch)
    cost = cost_usd("gpt-oss-120b", 10000, 500)
    expected = 10000 * 0.00000009 + 500 * 0.00000047
    assert abs(cost - expected) < 1e-12


def test_cost_usd_unknown_model_returns_zero():
    """Unbekanntes Modell -> 0.0, kein Hard-Fail."""
    assert cost_usd("nonexistent-model-xyz", 1000, 500) == 0.0


def test_cost_usd_zero_tokens_returns_zero():
    assert cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 0, 0) == 0.0


# ─── cost_usd() — Image-Cost-Achse (W3-Pre.1) ─────────────────────────


def test_cost_usd_image_gen_sdxl_currently_free():
    """SDXL ist aktuell kostenlos (image=0.0). Schema-aktiv aber Cost=0."""
    cost = cost_usd("stable-diffusion-xl-base-v10", n_images=1)
    assert cost == 0.0


def test_cost_usd_image_axis_active_when_priced():
    """Wenn OVH SDXL irgendwann bepreist, soll cost_usd das automatisch tracken."""
    # Simuliere zukuenftiges Pricing: 0.04 USD pro Bild
    OVH_MODEL_PRICING_USD["__test_priced_image"] = {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.04, "request": 0.0,
    }
    try:
        cost = cost_usd("__test_priced_image", n_images=3)
        assert abs(cost - 3 * 0.04) < 1e-12
    finally:
        del OVH_MODEL_PRICING_USD["__test_priced_image"]


def test_cost_usd_combined_image_and_tokens():
    """Multimodaler Call mit Tokens UND Bild: alle Achsen summieren."""
    OVH_MODEL_PRICING_USD["__test_combined"] = {
        "prompt": 0.00000001, "completion": 0.00000002,
        "image": 0.05, "request": 0.0,
    }
    try:
        cost = cost_usd("__test_combined", 100, 50, n_images=2)
        expected = 100 * 0.00000001 + 50 * 0.00000002 + 2 * 0.05
        assert abs(cost - expected) < 1e-12
    finally:
        del OVH_MODEL_PRICING_USD["__test_combined"]


# ─── cost_usd() — Request-Cost-Achse ──────────────────────────────────


def test_cost_usd_request_axis_currently_zero():
    """Request-Cost ist bei OVH aktuell ueberall 0."""
    cost = cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 100, 50, n_requests=5)
    expected = 100 * 0.0000001 + 50 * 0.00000031  # request=0
    assert abs(cost - expected) < 1e-12


def test_cost_usd_request_axis_active_when_priced():
    """Wenn OVH Per-Request-Costs aktiviert, summiert cost_usd das."""
    OVH_MODEL_PRICING_USD["__test_priced_request"] = {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.0, "request": 0.001,
    }
    try:
        cost = cost_usd("__test_priced_request", 0, 0, n_requests=10)
        assert abs(cost - 10 * 0.001) < 1e-12
    finally:
        del OVH_MODEL_PRICING_USD["__test_priced_request"]


# ─── cost_eur() — FX-Konversion ───────────────────────────────────────


def test_cost_eur_uses_usd_to_eur_constant():
    cost_u = cost_usd("Meta-Llama-3_3-70B-Instruct", 1000, 1000)
    cost_e = cost_eur("Meta-Llama-3_3-70B-Instruct", 1000, 1000)
    assert abs(cost_e - cost_u * USD_TO_EUR) < 1e-12


def test_cost_eur_image_gen():
    OVH_MODEL_PRICING_USD["__test_priced_image_eur"] = {
        "prompt": 0.0, "completion": 0.0,
        "image": 0.10, "request": 0.0,
    }
    try:
        eur = cost_eur("__test_priced_image_eur", n_images=2)
        assert abs(eur - 2 * 0.10 * USD_TO_EUR) < 1e-12
    finally:
        del OVH_MODEL_PRICING_USD["__test_priced_image_eur"]


# ─── is_priced() ──────────────────────────────────────────────────────


def test_is_priced_true_for_known_models():
    assert is_priced("Mistral-Small-3.2-24B-Instruct-2506")
    assert is_priced("Meta-Llama-3_3-70B-Instruct")
    assert is_priced("bge-multilingual-gemma2")
    assert is_priced("stable-diffusion-xl-base-v10")
    assert is_priced("whisper-large-v3-turbo")
    assert is_priced("Qwen2.5-VL-72B-Instruct")  # vision_x


def test_is_priced_true_for_proxy_aliases():
    assert is_priced("chat")
    assert is_priced("reason")
    assert is_priced("voice")
    assert is_priced("ovh/gpt-oss-120b")


def test_is_priced_false_for_unknown():
    assert not is_priced("gpt-7-future")


# ─── Cost-Math-Insights (Doktrin-Validation) ──────────────────────────


def test_reason_cheaper_than_xhigh_for_long_prompt_short_output():
    """gpt-oss-120b (reason) is 6-8x cheaper than Llama-70B (xhigh)
    for a long prompt + short output.
    """
    cost_reason = cost_usd("gpt-oss-120b", 10_000, 500)
    cost_xhigh = cost_usd("Meta-Llama-3_3-70B-Instruct", 10_000, 500)
    ratio = cost_xhigh / cost_reason
    assert ratio > 5.0, (
        f"reason sollte >5x guenstiger sein, ist {ratio:.2f}x"
    )


def test_reason_still_cheaper_than_xhigh_for_long_output():
    """Short prompt + long output: reason is still cheaper (~1.7x)."""
    cost_reason = cost_usd("gpt-oss-120b", 500, 5000)
    cost_xhigh = cost_usd("Meta-Llama-3_3-70B-Instruct", 500, 5000)
    assert cost_reason < cost_xhigh


def test_qwen_vl_72b_is_most_expensive_text_model():
    """Qwen2.5-VL-72B ist mit $1.01/M sym. das teuerste Modell ueberhaupt
    bei OVH (Premium-Vision). Doktrin-Validation.
    """
    test_tokens = (1000, 1000)
    qwen_vl = cost_usd("Qwen2.5-VL-72B-Instruct", *test_tokens)
    llama_70b = cost_usd("Meta-Llama-3_3-70B-Instruct", *test_tokens)
    assert qwen_vl > llama_70b


# ─── Audio-Pricing (Whisper, OVH-Rechnung 2026-05-05) ────────────────


def test_audio_pricing_has_both_whisper_models():
    assert "whisper-large-v3-turbo" in OVH_AUDIO_PRICING_USD
    assert "whisper-large-v3" in OVH_AUDIO_PRICING_USD


def test_audio_pricing_v3_more_expensive_than_turbo():
    """v3-full ist langsamer + teurer als turbo (Schaetzwert 4x)."""
    assert (
        OVH_AUDIO_PRICING_USD["whisper-large-v3"]
        > OVH_AUDIO_PRICING_USD["whisper-large-v3-turbo"]
    )


def test_audio_cost_usd_turbo_503_seconds_matches_invoice():
    """Reality-Check gegen OVH-Rechnung 2026-05-05: 503 sec turbo ≈ 0,01 EUR."""
    eur = audio_cost_eur("whisper-large-v3-turbo", 503)
    assert 0.009 < eur < 0.012, f"503 sec turbo sollte ~0.01 EUR ergeben, war {eur}"


def test_audio_cost_resolves_voice_alias():
    assert audio_cost_usd("voice", 10) == audio_cost_usd("whisper-large-v3-turbo", 10)


def test_audio_cost_unknown_model_returns_zero():
    assert audio_cost_usd("unknown-whisper-model", 100) == 0.0


def test_audio_cost_zero_or_negative_duration_returns_zero():
    assert audio_cost_usd("whisper-large-v3-turbo", 0) == 0.0
    assert audio_cost_usd("whisper-large-v3-turbo", -5) == 0.0


def test_is_priced_includes_whisper_audio_models():
    """Whisper ist via Audio-Achse 'priced' (auch wenn /v1/models 0 zeigt)."""
    assert is_priced("whisper-large-v3-turbo")
    assert is_priced("whisper-large-v3")


# ─── Token-Weighting ──────────────────────────────────────────────────


def test_billable_tokens_anchor_mistral_small_is_one_to_one():
    """Anker: Mistral-Small Output 0.31 USD/M = Faktor 1.0."""
    assert billable_tokens(
        "Mistral-Small-3.2-24B-Instruct-2506",
        prompt_tokens=1000,
        completion_tokens=500,
    ) == 1500


def test_billable_tokens_resolves_chat_alias():
    assert billable_tokens("chat", prompt_tokens=1000, completion_tokens=500) == 1500


def test_resolve_pricing_model_handles_alias_and_prefix():
    assert resolve_pricing_model("chat") == "Mistral-Small-3.2-24B-Instruct-2506"
    assert resolve_pricing_model("ovh/gpt-oss-120b") == "gpt-oss-120b"


def test_billable_tokens_llama_70b_is_24x_anchor():
    """Llama-70B = 2.4× Mistral-Small-Anker."""
    raw = 1000
    weighted = billable_tokens(
        "Meta-Llama-3_3-70B-Instruct",
        prompt_tokens=raw,
        completion_tokens=0,
    )
    assert weighted == int(round(raw * 2.4))


def test_billable_tokens_qwen_vl_is_most_expensive():
    """Qwen2.5-VL-72B = 3.3× Anker, teuerstes Text-Modell."""
    weighted = billable_tokens(
        "Qwen2.5-VL-72B-Instruct",
        prompt_tokens=100,
        completion_tokens=100,
    )
    assert weighted == int(round(200 * 3.3))


def test_billable_tokens_guard_models_free():
    """Guard-Modelle = 0× → ziehen NICHTS vom Budget."""
    assert billable_tokens(
        "Qwen3Guard-Gen-0.6B",
        prompt_tokens=10_000,
        completion_tokens=200,
    ) == 0
    assert billable_tokens(
        "Qwen3Guard-Gen-8B",
        prompt_tokens=10_000,
        completion_tokens=200,
    ) == 0


def test_billable_tokens_sdxl_free():
    """SDXL = 0× → Image-Gen blockt Quota nicht."""
    assert billable_tokens(
        "stable-diffusion-xl-base-v10",
        prompt_tokens=50,
        completion_tokens=0,
    ) == 0


def test_billable_tokens_whisper_turbo_audio_only():
    """Whisper-turbo: ~69.7 virtuelle Tokens/sec (cost-konsistent)."""
    weighted = billable_tokens(
        "whisper-large-v3-turbo",
        audio_seconds=503,
    )
    expected = int(round(503 * 69.7))
    assert weighted == expected
    # 503 sec ≈ 35.058 vTokens → entspricht ~0,01 EUR (OVH-Rechnung-Reality)
    assert 30_000 < weighted < 40_000


def test_billable_tokens_whisper_v3_full_4x_turbo():
    """v3-full = 4× turbo → 278.7/sec."""
    turbo = billable_tokens("whisper-large-v3-turbo", audio_seconds=100)
    full = billable_tokens("whisper-large-v3", audio_seconds=100)
    assert full == int(round(100 * 278.7))
    # ~4x mit Rundungs-/Konstanten-Toleranz (0.05%)
    assert abs(full / (4 * turbo) - 1.0) < 0.005


def test_billable_tokens_unknown_model_falls_back_to_one():
    """Unbekanntes Modell → weight=1.0 (konservativ, kein Silent-Bypass)."""
    assert billable_tokens(
        "future-model-xyz",
        prompt_tokens=200,
        completion_tokens=100,
    ) == 300


def test_billable_tokens_zero_input_returns_zero():
    assert billable_tokens("Mistral-Small-3.2-24B-Instruct-2506") == 0


def test_billable_tokens_anchor_factor_consistent_with_pricing():
    """Faktor=1.0 fuer Anker — direkt aus MODEL_TOKEN_WEIGHT validiert."""
    assert MODEL_TOKEN_WEIGHT["Mistral-Small-3.2-24B-Instruct-2506"] == 1.0
    # Llama-70B-Faktor (2.4) ≈ Llama-70B-Cost / Mistral-Small-Cost (sym.)
    llama_cost = OVH_MODEL_PRICING_USD["Meta-Llama-3_3-70B-Instruct"]["completion"]
    anchor_cost = OVH_MODEL_PRICING_USD["Mistral-Small-3.2-24B-Instruct-2506"]["completion"]
    ratio = llama_cost / anchor_cost
    factor = MODEL_TOKEN_WEIGHT["Meta-Llama-3_3-70B-Instruct"]
    # Faktor 2.4 vs. Real-Ratio ~2.39 — stimmig
    assert abs(factor - ratio) < 0.1


def test_audio_token_weight_consistent_with_pricing():
    """AUDIO_TOKEN_WEIGHT_PER_SEC ≈ USD/sec / Anker-USD-per-Token."""
    anchor = 0.00000031  # Mistral-Small Completion
    for model, rate_usd_per_sec in OVH_AUDIO_PRICING_USD.items():
        if model not in AUDIO_TOKEN_WEIGHT_PER_SEC:
            continue
        derived = rate_usd_per_sec / anchor
        actual = AUDIO_TOKEN_WEIGHT_PER_SEC[model]
        # 5% Toleranz wegen Rundung
        assert abs(actual - derived) / derived < 0.05, (
            f"{model}: derived={derived:.4f}, actual={actual}"
        )
