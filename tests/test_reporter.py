import pytest
from app.reporter import RunReporter
import tempfile
from pathlib import Path


def test_generate_html_report_offline_zero_cdns():
    sample_run = {
        "id": "run-test-1234",
        "status": "SUCCEEDED",
        "created_at": 1793080000.0,
        "steps": [
            {
                "id": "s1",
                "title": "Passo 1: Reconhecimento",
                "status": "SUCCEEDED",
                "engine": "antigravity",
                "model": "gemini-2.5-flash",
                "tokens": 1500,
                "duration": 4.5,
                "summary": "Analise concluida sem erros."
            },
            {
                "id": "s2",
                "title": "Passo 2: Implementacao",
                "status": "SUCCEEDED",
                "engine": "hera_router",
                "model": "deepseek-ai/DeepSeek-V4.1-Flash",
                "tokens": 4200,
                "duration": 12.3,
                "summary": "Codigo refatorado."
            }
        ]
    }

    html = RunReporter.generate_html(sample_run)
    assert "<!DOCTYPE html>" in html
    assert "run-test-1234" in html
    assert "SUCCEEDED" in html
    assert "5,700" in html  # Total tokens formatado
    assert "antigravity" in html
    assert "hera_router" in html
    assert "<svg" in html
    assert "</svg>" in html

    # Zero CDNs externas (nenhum link de folha de estilo ou script externo)
    assert '<link rel="stylesheet" href="http' not in html
    assert '<script src="http' not in html


def test_save_report_to_disk():
    sample_run = {
        "id": "run-save-test",
        "status": "FAILED",
        "steps": []
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "custom_report.html"
        saved = RunReporter.save_report(sample_run, str(target))
        assert Path(saved).is_file()
        content = Path(saved).read_text(encoding="utf-8")
        assert "run-save-test" in content
        assert "FAILED" in content
