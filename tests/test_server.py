import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from app.server import app
from app.config_manager import ConfigManager
from helpers import AUTH_HEADERS, TEST_BASE_URL

@pytest.fixture
def temp_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    mgr = ConfigManager(config_path=str(cfg_file))
    return mgr

def test_config_manager_defaults(temp_config):
    cfg = temp_config.load_config()
    assert "general" in cfg
    assert "engines" in cfg
    assert cfg["general"]["default_offline"] is False
    assert "ollama" in cfg["engines"]

def test_config_manager_update(temp_config):
    temp_config.update_engine_model("ollama", "mistral:7b")
    cfg = temp_config.load_config()
    assert cfg["engines"]["ollama"]["default_model"] == "mistral:7b"

    temp_config.set_offline_mode(True)
    cfg2 = temp_config.load_config()
    assert cfg2["general"]["default_offline"] is True

@pytest.mark.asyncio
async def test_api_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "engines" in data
        assert "ollama" in data["engines"]
        assert "hera_router" in data["engines"]

@pytest.mark.asyncio
async def test_api_config_endpoints(tmp_path, monkeypatch):
    # Isolado: antes este teste gravava no config.yaml REAL e deixava o roteador global em modo offline.
    import app.server as server_module
    monkeypatch.setattr(server_module, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server_module.router, "default_offline", server_module.router.default_offline)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        # GET
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        cfg = resp.json()
        assert "general" in cfg
        assert "engines" in cfg

        # POST
        cfg["general"]["default_offline"] = True
        post_resp = await client.post("/api/config", json=cfg)
        assert post_resp.status_code == 200
        assert post_resp.json()["status"] == "ok"

@pytest.mark.asyncio
async def test_api_hardware_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/hardware")
        assert resp.status_code == 200
        data = resp.json()
        assert "cpu_pct" in data
        assert "ram_used_gb" in data
        assert "gpu" in data

@pytest.mark.asyncio
async def test_api_tasks_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

@pytest.mark.asyncio
async def test_api_vault_search_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/api/vault/search?q=test")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

@pytest.mark.asyncio
async def test_api_index_html():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Hera · Orquestração local de IAs" in resp.text and "/static/brand/favicon.svg" in resp.text
