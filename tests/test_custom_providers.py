"""Custom OpenAI-compatible providers via CUSTOM_PROVIDERS (Baseten, Groq, …)."""

import json

from app.config import Settings
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.router import ProviderRegistry


def _settings_with(custom: dict) -> Settings:
    return Settings(custom_providers=json.dumps(custom))


def test_custom_provider_config_parses():
    s = _settings_with(
        {
            "baseten": {
                "base_url": "https://inference.baseten.co/v1",
                "api_key": "sk-test",
                "default_model": "nvidia/Nemotron-120B-A12B",
            }
        }
    )
    cfg = s.custom_provider_configs()
    assert cfg["baseten"]["base_url"] == "https://inference.baseten.co/v1"
    assert cfg["baseten"]["api_key"] == "sk-test"
    assert cfg["baseten"]["default_model"] == "nvidia/Nemotron-120B-A12B"


def test_custom_provider_is_built_as_openai_compatible():
    s = _settings_with(
        {"groq": {"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk-x"}}
    )
    registry = ProviderRegistry(settings=s)
    provider = registry.get_provider("groq")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "groq"
    assert provider.base_url == "https://api.groq.com/openai/v1"


def test_custom_provider_prefix_routes_as_prefixed():
    s = _settings_with(
        {"baseten": {"base_url": "https://inference.baseten.co/v1", "api_key": "x"}}
    )
    registry = ProviderRegistry(settings=s)
    assert registry._route_key("baseten/nvidia/Nemotron-120B-A12B") == "prefixed"


def test_empty_or_invalid_custom_providers_is_safe():
    assert Settings(custom_providers="").custom_provider_configs() == {}
    assert Settings(custom_providers="not json").custom_provider_configs() == {}
    # entries without a base_url are dropped
    s = Settings(custom_providers=json.dumps({"bad": {"api_key": "x"}}))
    assert s.custom_provider_configs() == {}


def test_unknown_provider_without_custom_falls_back_to_stub():
    registry = ProviderRegistry(settings=Settings())
    provider = registry.get_provider("baseten")  # not configured
    assert provider.name == "stub"
