"""O teto de gasto diario e a aderencia ao modelo de fallback chegam do config.yaml ao roteador e aparecem em /api/router/state."""
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from helpers import AUTH_HEADERS, TEST_BASE_URL


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", list(server.gateway_tier_router.adapters))
    monkeypatch.setattr(server.gateway_tier_router, "daily_budget_usd", None)       # restaurados ao fim do teste
    monkeypatch.setattr(server.gateway_tier_router, "sticky_minutes", 15)
    return server


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_reload_le_teto_e_aderencia_do_config(api):
    api.config_mgr.save_config({**api.config_mgr.load_config(), "gateway": {"daily_budget_usd": 0, "sticky_minutes": 7}})
    api.reload_gateway()
    assert api.gateway_tier_router.daily_budget_usd == 0.0
    assert api.gateway_tier_router.sticky_minutes == 7
    async with client() as c:
        estado = (await c.get("/api/router/state")).json()
    assert estado["daily_budget_usd"] == 0.0
    assert estado["sticky_minutes"] == 7
    assert isinstance(estado["today_cost_usd"], (int, float)) and estado["today_cost_usd"] >= 0


@pytest.mark.asyncio
async def test_sem_configuracao_nao_ha_teto(api):
    api.config_mgr.save_config({**api.config_mgr.load_config(), "gateway": {}})
    api.reload_gateway()
    assert api.gateway_tier_router.daily_budget_usd is None
    assert api.gateway_tier_router.sticky_minutes == 15
    async with client() as c:
        assert (await c.get("/api/router/state")).json()["daily_budget_usd"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bruto", [-1, "abc", True, float("nan")])
async def test_teto_invalido_vira_sem_teto(api, bruto):
    api.config_mgr.save_config({**api.config_mgr.load_config(), "gateway": {"daily_budget_usd": bruto}})
    api.reload_gateway()
    assert api.gateway_tier_router.daily_budget_usd is None
