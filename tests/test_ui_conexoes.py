"""UI: a sub-aba "Conexoes" (rotas, OAuth, busca) existe e o JavaScript embutido continua valido."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")


def test_sub_aba_conexoes_esta_ligada():
    assert 'data-sub="connections"' in INDEX and 'id="rt-connections"' in INDEX
    for dom in ("cn-endpoints", "cn-oauth", "cn-oauth-device", "cn-search", "cn-search-q", "cn-search-out"):
        assert f'id="{dom}"' in INDEX
    assert 'if (name === "connections") loadConnections();' in INDEX
    for rota in ("/v1/messages", "/v1/responses", "/v1/embeddings", "/v1/images/generations", "/v1/audio/speech",
                 "/v1/audio/transcriptions", "/v1/videos", "/v1/search"):
        assert f'"{rota}"' in INDEX


def test_funcoes_chamadas_pela_ui_existem():
    for fn in ("loadConnections", "loadOAuth", "oauthConnect", "oauthDisconnect", "oauthDevicePoll", "loadSearchState",
               "runSearchTest", "renderEndpoints"):
        assert re.search(rf"function {fn}\(", INDEX), fn


@pytest.mark.skipif(shutil.which("node") is None, reason="node nao encontrado")
def test_javascript_embutido_tem_sintaxe_valida(tmp_path):
    blocks = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", INDEX, flags=re.S)
    assert len(blocks) >= 2
    for i, code in enumerate(blocks):
        f = tmp_path / f"b{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([shutil.which("node"), "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:300]
