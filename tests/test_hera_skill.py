import pytest
from unittest.mock import patch, MagicMock
from cli.hera_skill import cmd_setup, cmd_report


def test_hera_skill_setup():
    with patch("cli.hera_skill._http_request") as mock_req:
        mock_req.side_effect = [
            {"version": "1.0.0"},
            {"active_engines": ["ollama", "antigravity"], "offline": False}
        ]
        res = cmd_setup()
        assert res["hera_online"] is True
        assert res["version"] == "1.0.0"
        assert "antigravity" in res["engines"]


def test_hera_skill_report():
    fake_events = [
        {"ts": 1793080000.0, "step": "s1", "kind": "step_end", "status": "SUCCEEDED", "tokens": 500, "duration": 2.0}
    ]
    with patch("cli.hera_skill._http_request") as mock_req:
        mock_req.return_value = {"events": fake_events}
        res = cmd_report("run-fake-test")
        assert res["run_id"] == "run-fake-test"
        assert res["status"] == "SUCCEEDED"
        assert res["steps_count"] == 1
        assert res["total_tokens"] == 500
        assert "file:///" in res["file_url"]
