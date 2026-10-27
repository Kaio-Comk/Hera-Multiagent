import pytest
from app.context_reducer import ContextReducer
import tempfile
from pathlib import Path


def test_scan_python_ast():
    code = """
import os
from sys import path

class Calculadora:
    \"\"\"Docstring da classe Calculadora.\"\"\"
    def somar(self, a, b):
        return a + b

    async def multiplicar_async(self, a, b):
        return a * b

def funcao_auxiliar():
    return 42
"""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code)
        f_path = f.name

    try:
        res = ContextReducer.scan_python_ast(f_path)
        assert res["file"].endswith(".py")
        assert len(res["classes"]) == 1
        assert res["classes"][0]["name"] == "Calculadora"
        assert "somar" in res["classes"][0]["methods"]
        assert "multiplicar_async" in res["classes"][0]["methods"]
        assert len(res["functions"]) == 1
        assert res["functions"][0]["name"] == "funcao_auxiliar"
        assert "os" in res["imports"]
        assert "sys.path" in res["imports"]
    finally:
        Path(f_path).unlink(missing_ok=True)


def test_summarize_test_output():
    raw_pytest_log = """
============================= test session starts =============================
platform win32 -- Python 3.12.7, pytest-9.1.1
collected 10 items

tests/test_auth.py .F.                                                    [ 30%]

=================================== FAILURES ====================================
__________________________________ test_login ___________________________________
tests/test_auth.py:42: in test_login
    assert response.status_code == 200
E   AssertionError: assert 401 == 200
=========================== short test summary info ===========================
FAILED tests/test_auth.py::test_login - AssertionError: assert 401 == 200
========================= 1 failed, 9 passed in 0.42s =========================
"""
    summary = ContextReducer.summarize_test_output(raw_pytest_log)
    assert "FAILED tests/test_auth.py::test_login" in summary
    assert "AssertionError: assert 401 == 200" in summary
    assert len(summary) < 1000


def test_summarize_git_diff():
    diff_sample = """
diff --git a/app/core.py b/app/core.py
index 12345..67890 100644
--- a/app/core.py
+++ b/app/core.py
@@ -10,3 +10,4 @@
 def run():
-    return False
+    # Comentario adicionado
+    return True
"""
    diff_sum = ContextReducer.summarize_git_diff(diff_sample)
    assert "app/core.py" in diff_sum
    assert "+2 / -1 linhas" in diff_sum


def test_reduce_step_output_for_synthesis():
    step = {
        "id": "s1",
        "title": "Correcao de Bug",
        "status": "SUCCEEDED",
        "engine": "ollama",
        "summary": "Corrigida a linha 42",
        "artifacts": ["app/core.py"]
    }
    reduced = ContextReducer.reduce_step_output_for_synthesis(step)
    assert "Passo s1 (Correcao de Bug) [SUCCEEDED] via ollama" in reduced
    assert "Corrigida a linha 42" in reduced
    assert "app/core.py" in reduced
