import pytest
from app.playbooks import PlaybookManager
from app.quota import QuotaTracker


def test_playbooks_carrega_yaml():
    mgr = PlaybookManager()
    assert mgr.playbooks.schema_version == 1
    assert "savings" in mgr.playbooks.strategies
    assert "balanced" in mgr.playbooks.strategies
    assert "aggressive_quality" in mgr.playbooks.strategies


def test_resolve_role_balanced():
    mgr = PlaybookManager()
    engine, model, reason = mgr.resolve_role("builder", strategy_name="balanced")
    assert engine == "hera_router"
    assert "DeepSeek" in model
    assert "balanced" in reason


def test_resolve_role_savings():
    mgr = PlaybookManager()
    engine, model, reason = mgr.resolve_role("builder", strategy_name="savings")
    assert engine == "ollama"
    assert "savings" in reason


def test_resolve_role_auto_downgrade_on_survival_quota():
    mgr = PlaybookManager()
    quota = QuotaTracker()

    # Normal: balanced usa hera_router
    engine, _, _ = mgr.resolve_role("builder", strategy_name="balanced", quota_tracker=quota)
    assert engine == "hera_router"

    # Simular cota esgotada (survival) no hera_router
    quota.record_metrics("hera_router", {
        "x-ratelimit-remaining-tokens": "1000",
        "x-ratelimit-limit-tokens": "100000"
    })
    assert quota.get_health_status("hera_router") == "survival"

    # Agora deve comutar automaticamente para savings (ollama)
    downgraded_engine, _, reason = mgr.resolve_role("builder", strategy_name="balanced", quota_tracker=quota)
    assert downgraded_engine == "ollama"
    assert "comutado de balanced para savings" in reason


def test_resolve_role_fallback_when_blocked():
    mgr = PlaybookManager()
    quota = QuotaTracker()

    # Bloquear hera_router
    quota.mark("hera_router", 600, "rate limit")
    engine, _, reason = mgr.resolve_role("builder", strategy_name="balanced", quota_tracker=quota)
    # Deve comutar para savings ou fallback antigravity
    assert engine in ("ollama", "antigravity")
    assert "fallback" in reason or "comutado" in reason
