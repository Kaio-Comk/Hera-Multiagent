"""Catálogo de provedores do Roteador: todo item precisa poder ser adicionado pelo painel sem surpresa."""
import ipaddress
import re
from urllib.parse import urlsplit

import pytest

import app.server as server
from app.gateway.presets import PROVIDER_PRESETS, get_preset
from app.gateway.presets_extra import EXTRA_PRESETS

IDS = [p["id"] for p in PROVIDER_PRESETS]


def test_catalogo_tem_o_basico_mais_os_extras_do_9router():
    assert len(PROVIDER_PRESETS) >= 50 and len(EXTRA_PRESETS) >= 20
    assert len(IDS) == len(set(IDS))                                                    # ids únicos
    assert len({p["key_env"] for p in PROVIDER_PRESETS}) == len(PROVIDER_PRESETS)      # uma variável de chave por provedor
    assert len({p["base_url"].rstrip("/") for p in PROVIDER_PRESETS}) == len(PROVIDER_PRESETS)


@pytest.mark.parametrize("preset", PROVIDER_PRESETS, ids=IDS)
def test_cada_preset_passa_nas_regras_do_servidor(preset):
    assert server._PROVIDER_NAME.match(preset["id"]) and preset["id"] not in server._RESERVED_NAMES
    assert server._KEY_ENV.match(preset["key_env"]) and not preset["key_env"].startswith("HERA_")
    assert preset["tier"] in server.TIERS
    assert server._MODEL_OK.match(preset["default_model"]) and all(server._MODEL_OK.match(m) for m in preset["models"])
    assert preset["default_model"] in preset["models"]
    assert preset["label"].strip() and isinstance(preset["note"], str)
    assert get_preset(preset["id"])["base_url"] == preset["base_url"]


@pytest.mark.parametrize("preset", PROVIDER_PRESETS, ids=IDS)
def test_enderecos_sao_https_publicos_e_sem_rastro_de_caminho_de_chat(preset):
    for key in ("base_url", "signup"):
        parts = urlsplit(preset[key])
        assert parts.scheme == "https" and parts.hostname and not parts.username and not parts.password, (key, preset[key])
        assert parts.hostname not in ("localhost",) and not parts.hostname.endswith((".local", ".internal"))
        with pytest.raises(ValueError):
            ipaddress.ip_address(parts.hostname)                                       # nada de IP literal
    base = urlsplit(preset["base_url"])
    assert not preset["base_url"].endswith("/") and "chat/completions" not in base.path and not base.query and not base.fragment
    import re
    declared = {p["key"] for p in preset.get("params", [])}
    assert (base.port or 443) == 443 and "{" not in (base.hostname or "")
    assert set(re.findall(r"\{([a-z_]+)\}", preset["base_url"])) == declared      # {parte} so existe se o preset a declara em `params`


def test_extras_citam_a_origem_e_avisam_que_nao_foram_testados():
    import app.gateway.presets_extra as mod
    assert "MIT" in mod.__doc__ and "decolua/9router" in mod.__doc__ and "NÃO foram testados" in mod.__doc__
    assert all(p["tier"] in ("cloud_free", "cloud_cheap") for p in EXTRA_PRESETS)


def test_so_o_openrouter_oferece_login_por_conta():
    assert [p["id"] for p in PROVIDER_PRESETS if p.get("login")] == ["openrouter"]
