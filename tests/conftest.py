"""Configuracao global dos testes.

O token da API e fixado ANTES de qualquer import de `app.server` (que le HERA_API_KEY), para
os testes nao criarem `data/.hera_token` nem dependerem de um arquivo local."""
import os
import tempfile

# Segredos/nonces dos testes vao para uma pasta temporaria (nada de criar arquivos em data/).
os.environ.setdefault("HERA_DATA_DIR", tempfile.mkdtemp(prefix="hera-test-data-"))
os.environ.setdefault("HERA_API_KEY", "test-token-0123456789abcdef0123456789abcdef")
