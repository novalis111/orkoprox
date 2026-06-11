"""Tests for keyconfig_bootstrap.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ ist nicht im pythonpath — manuell adden
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def test_derive_tenant_id_lv_prefix():
    from keyconfig_bootstrap import derive_tenant_id

    tenant, package = derive_tenant_id("lv_mitarbeiterdesign_abc123def456")
    assert tenant == "mitarbeiterdesign"
    assert package == "lv"


def test_derive_tenant_id_tc_prefix():
    from keyconfig_bootstrap import derive_tenant_id

    tenant, package = derive_tenant_id("tc_default_xyz789")
    assert tenant == "default"
    assert package == "tc"


def test_derive_tenant_id_oeko_prefix():
    from keyconfig_bootstrap import derive_tenant_id

    tenant, package = derive_tenant_id("oeko_dev_111222333")
    assert tenant == "dev"
    assert package == "oeko"


def test_derive_tenant_id_no_prefix_unknown():
    from keyconfig_bootstrap import derive_tenant_id

    tenant, package = derive_tenant_id("randomkey1234567")
    assert tenant == "unknown"
    assert package == "legacy"


def test_bootstrap_dry_run_no_redis():
    """--dry-run ohne Redis-Verbindung."""
    from keyconfig_bootstrap import bootstrap

    result = bootstrap(
        ["lv_mad_aaa111", "lv_mitarbeiterdesign_bbb222"],
        redis_url="redis://no-redis-needed",
        daily_token_limit=1_000_000,
        dry_run=True,
    )
    assert len(result) == 2
    assert result["lv_mad_aaa111"]["tenant_id"] == "mad"
    assert result["lv_mitarbeiterdesign_bbb222"]["tenant_id"] == "mitarbeiterdesign"
    # dry-run setzt always already_existed=False (kein Lookup)
    assert result["lv_mad_aaa111"]["already_existed"] is False


def test_bootstrap_with_fakeredis():
    """Mit fakeredis als drop-in fuer echtes Redis."""
    fakeredis = pytest.importorskip("fakeredis")
    from keyconfig_bootstrap import bootstrap
    from app.token_metering import TokenMeteringService

    fake = fakeredis.FakeRedis(decode_responses=True)

    # Erstes Mal: NEW

    # Patch import path damit redis-Client vom Skript fakeredis nutzt
    class _FakeRedisModule:
        Redis = type("R", (), {"from_url": staticmethod(lambda *a, **k: fake)})

    sys.modules["redis"] = _FakeRedisModule  # type: ignore[assignment]

    try:
        result1 = bootstrap(
            ["lv_test_aaa111"],
            redis_url="redis://fake",
            daily_token_limit=2_000_000,
            dry_run=False,
        )
        assert result1["lv_test_aaa111"]["already_existed"] is False
        # Zweites Mal: idempotent — already existed
        # _config_cache wird im service-objekt gehalten, neue Instanz heisst neuer cache
        # -> get_key_config liest aus Redis und findet den Eintrag
        svc = TokenMeteringService(fake)
        cfg = svc.get_key_config("lv_test_aaa111")
        assert cfg is not None
        assert cfg.tenant_id == "test"
        assert cfg.daily_token_limit == 2_000_000
    finally:
        sys.modules.pop("redis", None)
