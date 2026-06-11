"""Declarative routing/limit policy with hot-reload.

Instead of setting dozens of environment variables, a deployment can keep its
gateway configuration in a single versionable TOML file and point
``POLICY_FILE`` at it. The file is GitOps-friendly, reviewable, and reloaded on
change without a restart (mtime poll — no extra dependency, no watcher thread).

TOML is used deliberately: it parses with the standard-library ``tomllib``
(Python 3.11+), so the policy feature adds no new runtime dependency.

Example ``orkoprox.toml``:

    [aliases]
    chat   = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    reason = "ovh/gpt-oss-120b"

    [limits]
    rate_limit_per_minute = 120
    rate_limit_concurrency = 8

    [quota.default]
    daily_token_limit   = 1_000_000
    monthly_token_limit = 20_000_000

Only keys present in the file are overridden; everything else falls back to the
environment / built-in defaults. Routing-alias changes apply on the next load;
limit/quota changes are read live by the components that consume them.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Policy:
    """Parsed policy document. Empty sections mean "use existing config"."""

    aliases: dict[str, str] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    quota_defaults: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        raw_aliases = data.get("aliases", {}) or {}
        aliases = {str(k): str(v) for k, v in raw_aliases.items() if v}
        raw_limits = data.get("limits", {}) or {}
        limits = {str(k): int(v) for k, v in raw_limits.items()}
        quota = (data.get("quota", {}) or {}).get("default", {}) or {}
        quota_defaults = {str(k): int(v) for k, v in quota.items()}
        return cls(aliases=aliases, limits=limits, quota_defaults=quota_defaults)


class PolicyLoader:
    """Loads a TOML policy file and reloads it when the file changes (mtime)."""

    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None
        self._mtime: float | None = None
        self._policy = Policy()
        if self._path:
            self.reload()

    @property
    def enabled(self) -> bool:
        return self._path is not None

    @property
    def policy(self) -> Policy:
        return self._policy

    def _current_mtime(self) -> float | None:
        try:
            assert self._path is not None
            return self._path.stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Reload the policy if the file's mtime changed. Returns True on reload."""
        if not self._path:
            return False
        mtime = self._current_mtime()
        if mtime is None:
            return False
        if self._mtime is not None and mtime == self._mtime:
            return False
        return self.reload()

    def reload(self) -> bool:
        """Load (or re-load) the policy file. Never raises — on a parse error the
        previous policy is kept and a warning is logged. Returns True on success."""
        if not self._path:
            return False
        try:
            raw = self._path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
            self._policy = Policy.from_dict(data)
            self._mtime = self._current_mtime()
            logger.info(
                "policy loaded: %d aliases, %d limits, %d quota defaults",
                len(self._policy.aliases),
                len(self._policy.limits),
                len(self._policy.quota_defaults),
            )
            return True
        except FileNotFoundError:
            logger.warning("policy file not found: %s", self._path)
            return False
        except (tomllib.TOMLDecodeError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("policy file invalid, keeping previous policy: %s", exc)
            return False


def apply_policy_to_settings(policy: Policy, settings: Any) -> None:
    """Merge a policy's alias + limit overrides into a Settings instance.

    Only fields present in the policy are changed. Alias keys map to
    ``model_alias_<name>`` settings attributes; limit keys map directly.
    """
    for name, target in policy.aliases.items():
        attr = f"model_alias_{name.lower()}"
        if hasattr(settings, attr):
            setattr(settings, attr, target)
    for name, value in policy.limits.items():
        if hasattr(settings, name):
            setattr(settings, name, value)
