"""Tests for Policy, PolicyLoader, and apply_policy_to_settings."""

from __future__ import annotations

import os
import time

import pytest

from app.policy import Policy, PolicyLoader, apply_policy_to_settings


# ---------------------------------------------------------------------------
# Policy.from_dict
# ---------------------------------------------------------------------------


class TestPolicyFromDict:
    def test_full_dict_parsed(self) -> None:
        data = {
            "aliases": {"chat": "provider/model-a", "reason": "provider/model-b"},
            "limits": {"rate_limit_per_minute": 60, "rate_limit_concurrency": 4},
            "quota": {"default": {"daily_token_limit": 100_000, "monthly_token_limit": 2_000_000}},
        }
        policy = Policy.from_dict(data)
        assert policy.aliases == {"chat": "provider/model-a", "reason": "provider/model-b"}
        assert policy.limits == {"rate_limit_per_minute": 60, "rate_limit_concurrency": 4}
        assert policy.quota_defaults == {
            "daily_token_limit": 100_000,
            "monthly_token_limit": 2_000_000,
        }

    def test_empty_dict_produces_empty_policy(self) -> None:
        policy = Policy.from_dict({})
        assert policy.aliases == {}
        assert policy.limits == {}
        assert policy.quota_defaults == {}

    def test_missing_sections_ignored(self) -> None:
        policy = Policy.from_dict({"aliases": {"hi": "x/y"}})
        assert policy.aliases == {"hi": "x/y"}
        assert policy.limits == {}
        assert policy.quota_defaults == {}

    def test_empty_alias_value_excluded(self) -> None:
        policy = Policy.from_dict({"aliases": {"keep": "target", "drop": ""}})
        assert "keep" in policy.aliases
        assert "drop" not in policy.aliases

    def test_limit_values_cast_to_int(self) -> None:
        policy = Policy.from_dict({"limits": {"rate_limit_per_minute": "120"}})
        assert policy.limits["rate_limit_per_minute"] == 120
        assert isinstance(policy.limits["rate_limit_per_minute"], int)


# ---------------------------------------------------------------------------
# PolicyLoader — disabled path
# ---------------------------------------------------------------------------


class TestPolicyLoaderDisabled:
    def test_none_path_disabled(self) -> None:
        loader = PolicyLoader(None)
        assert loader.enabled is False

    def test_none_path_empty_policy(self) -> None:
        loader = PolicyLoader(None)
        assert loader.policy.aliases == {}
        assert loader.policy.limits == {}

    def test_none_path_reload_returns_false(self) -> None:
        loader = PolicyLoader(None)
        assert loader.reload() is False

    def test_none_path_reload_if_changed_returns_false(self) -> None:
        loader = PolicyLoader(None)
        assert loader.reload_if_changed() is False


# ---------------------------------------------------------------------------
# PolicyLoader — file-based loading (uses tmp_path)
# ---------------------------------------------------------------------------

VALID_TOML = """
[aliases]
chat   = "provider/small-model"
reason = "provider/large-model"

[limits]
rate_limit_per_minute = 90

[quota.default]
daily_token_limit   = 500_000
monthly_token_limit = 10_000_000
"""


class TestPolicyLoaderFile:
    def test_loads_aliases_from_toml(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        assert loader.enabled is True
        assert loader.policy.aliases["chat"] == "provider/small-model"
        assert loader.policy.aliases["reason"] == "provider/large-model"

    def test_loads_limits_from_toml(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        assert loader.policy.limits["rate_limit_per_minute"] == 90

    def test_loads_quota_defaults_from_toml(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        assert loader.policy.quota_defaults["daily_token_limit"] == 500_000
        assert loader.policy.quota_defaults["monthly_token_limit"] == 10_000_000

    def test_reload_returns_true_on_success(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        assert loader.reload() is True


# ---------------------------------------------------------------------------
# PolicyLoader — error resilience
# ---------------------------------------------------------------------------


class TestPolicyLoaderErrors:
    def test_broken_toml_reload_returns_false(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        assert loader.policy.aliases != {}  # valid state loaded

        p.write_text("[[[ broken toml }")
        result = loader.reload()
        assert result is False

    def test_broken_toml_keeps_previous_policy(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        original_aliases = dict(loader.policy.aliases)

        p.write_text("not valid toml [[[")
        loader.reload()

        # Previous valid policy must be intact
        assert loader.policy.aliases == original_aliases

    def test_missing_file_reload_returns_false(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "nonexistent.toml"  # type: ignore[operator]
        loader = PolicyLoader(str(p))
        assert loader.reload() is False

    def test_missing_file_no_raise(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "nonexistent.toml"  # type: ignore[operator]
        loader = PolicyLoader(str(p))  # must not raise
        assert loader.policy.aliases == {}

    def test_missing_file_enabled_false(self, tmp_path: pytest.TempPathFactory) -> None:
        """A non-None path counts as 'enabled' even if file is missing."""
        p = tmp_path / "missing.toml"  # type: ignore[operator]
        loader = PolicyLoader(str(p))
        assert loader.enabled is True  # path was provided — loader is armed


# ---------------------------------------------------------------------------
# PolicyLoader — reload_if_changed (mtime-based, no real sleeps)
# ---------------------------------------------------------------------------


class TestPolicyLoaderReloadIfChanged:
    def test_unchanged_file_returns_false(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        # mtime has not changed since initial load
        assert loader.reload_if_changed() is False

    def test_changed_file_returns_true(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))

        updated_toml = VALID_TOML + "\n# updated\n"
        p.write_text(updated_toml)
        # Advance mtime explicitly to guarantee the change is visible
        future_t = time.time() + 10
        os.utime(str(p), (future_t, future_t))

        assert loader.reload_if_changed() is True

    def test_changed_file_loads_new_content(self, tmp_path: pytest.TempPathFactory) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))

        new_toml = '[aliases]\nchat = "provider/new-model"\n'
        p.write_text(new_toml)
        future_t = time.time() + 10
        os.utime(str(p), (future_t, future_t))

        loader.reload_if_changed()
        assert loader.policy.aliases["chat"] == "provider/new-model"
        # Old "reason" alias gone — new file has only "chat"
        assert "reason" not in loader.policy.aliases

    def test_reload_if_changed_twice_without_change_returns_false(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        p = tmp_path / "policy.toml"  # type: ignore[operator]
        p.write_text(VALID_TOML)
        loader = PolicyLoader(str(p))
        loader.reload_if_changed()  # first call after no-change
        assert loader.reload_if_changed() is False  # second call, still no change


# ---------------------------------------------------------------------------
# apply_policy_to_settings
# ---------------------------------------------------------------------------


class TestApplyPolicyToSettings:
    def test_known_alias_is_applied(self) -> None:
        from app.config import Settings

        settings = Settings(proxy_auth_required=False)
        policy = Policy(aliases={"chat": "provider/override-model"})
        apply_policy_to_settings(policy, settings)
        assert settings.model_alias_chat == "provider/override-model"

    def test_known_limit_is_applied(self) -> None:
        from app.config import Settings

        settings = Settings(proxy_auth_required=False)
        policy = Policy(limits={"rate_limit_per_minute": 99})
        apply_policy_to_settings(policy, settings)
        assert settings.rate_limit_per_minute == 99

    def test_unknown_alias_key_is_ignored(self) -> None:
        """An alias name that doesn't map to a model_alias_* attr must not crash."""
        from app.config import Settings

        settings = Settings(proxy_auth_required=False)
        policy = Policy(aliases={"no_such_alias_xyz": "provider/x"})
        apply_policy_to_settings(policy, settings)  # must not raise
        assert not hasattr(settings, "model_alias_no_such_alias_xyz")

    def test_multiple_aliases_applied_together(self) -> None:
        from app.config import Settings

        settings = Settings(proxy_auth_required=False)
        policy = Policy(aliases={"chat": "p/chat-model", "reason": "p/reason-model"})
        apply_policy_to_settings(policy, settings)
        assert settings.model_alias_chat == "p/chat-model"
        assert settings.model_alias_reason == "p/reason-model"

    def test_empty_policy_leaves_settings_unchanged(self) -> None:
        from app.config import Settings

        settings = Settings(proxy_auth_required=False)
        original_chat = settings.model_alias_chat
        apply_policy_to_settings(Policy(), settings)
        assert settings.model_alias_chat == original_chat
