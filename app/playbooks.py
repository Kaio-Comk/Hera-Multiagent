"""Motor de Playbooks Declarativos para o Hera (Anthropic CMA - Plan Big, Execute Small).

Carrega e valida politicas em YAML (policies/playbooks.yaml), resolve papeis canonicos
(Planner, Scout, Builder, Reviewer) e comuta dinamicamente a estrategia conforme
a saude da quota (healthy, degraded, survival).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml
from pydantic import BaseModel, Field, model_validator


class RoleConfig(BaseModel):
    engine: str
    model: Optional[str] = None
    fallback: List[str] = Field(default_factory=list)


class StrategyConfig(BaseModel):
    description: str
    preferred_tiers: List[str] = Field(default_factory=list)
    roles: Dict[str, RoleConfig] = Field(default_factory=dict)
    chain: List[Dict[str, str]] = Field(default_factory=list)
    fallback_strategy: Optional[str] = None


class QuotaPolicyConfig(BaseModel):
    threshold_min_pct: int
    action: str
    effort_override: Optional[str] = None
    worker_override: Optional[str] = None
    forced_playbook: Optional[str] = None


class PlaybooksSchema(BaseModel):
    schema_version: int = 1
    defaults: Dict[str, Any] = Field(default_factory=dict)
    quota_policies: Dict[str, QuotaPolicyConfig] = Field(default_factory=dict)
    strategies: Dict[str, StrategyConfig]


class PlaybookManager:
    """Gerenciador central de playbooks e resolucao de politicas de execucao."""

    def __init__(self, yaml_path: Optional[str] = None) -> None:
        if yaml_path is None:
            yaml_path = str(Path(__file__).resolve().parent.parent / "policies" / "playbooks.yaml")
        self.yaml_path = Path(yaml_path)
        self.playbooks: PlaybooksSchema = self._load()

    def _load(self) -> PlaybooksSchema:
        if not self.yaml_path.is_file():
            raise FileNotFoundError(f"Arquivo de playbooks nao encontrado: {self.yaml_path}")
        raw = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8"))
        return PlaybooksSchema.model_validate(raw)

    def reload(self) -> None:
        self.playbooks = self._load()

    def get_strategy(self, name: str) -> StrategyConfig:
        strat = self.playbooks.strategies.get(name)
        if not strat:
            default_strat = self.playbooks.defaults.get("strategy", "balanced")
            strat = self.playbooks.strategies.get(default_strat)
            if not strat:
                raise KeyError(f"Estrategia '{name}' nao encontrada e default '{default_strat}' ausente.")
        return strat

    def map_task_type_to_role(self, task_type: str) -> str:
        """Mapeia o tipo da tarefa para o papel canonico correspondente."""
        t = (task_type or "").lower()
        if t in ("plan", "planning", "summary"):
            return "planner"
        elif t in ("research", "search", "scout", "discovery"):
            return "scout"
        elif t in ("coding", "builder", "implementation", "fix"):
            return "builder"
        elif t in ("review", "validation", "test", "audit"):
            return "reviewer"
        return "builder"

    def resolve_role(
        self,
        role: str,
        strategy_name: str = "balanced",
        quota_tracker: Optional[Any] = None
    ) -> Tuple[str, str, str]:
        """Resolve (engine, model, reason) para um papel especifico considerando a quota atual.

        Se a quota de motores caros estiver em 'survival', comuta automaticamente
        para o playbook 'savings'.
        """
        # Checar se ha necessidade de forcar savings por quota
        effective_strategy = strategy_name
        forced_reason = ""

        if quota_tracker:
            # Se a estrategia solicitada usa claude_code ou hera_router e a saude for survival
            candidate_strat = self.get_strategy(strategy_name)
            primary_engine = candidate_strat.roles.get(role, RoleConfig(engine="")).engine
            if primary_engine and quota_tracker.get_health_status(primary_engine) == "survival":
                effective_strategy = "savings"
                forced_reason = f" (comutado de {strategy_name} para savings por cota survival em {primary_engine})"

        strat = self.get_strategy(effective_strategy)
        role_cfg = strat.roles.get(role)

        if not role_cfg:
            # Fallback para papel builder ou default
            role_cfg = strat.roles.get("builder", RoleConfig(engine="ollama", model=""))

        engine = role_cfg.engine
        model = role_cfg.model or ""
        reason = f"Playbook {effective_strategy} / papel {role}{forced_reason}"

        # Se o motor principal estiver bloqueado no quota_tracker, buscar na lista de fallbacks
        if quota_tracker and quota_tracker.blocked(engine):
            for fb_engine in role_cfg.fallback:
                if not quota_tracker.blocked(fb_engine):
                    reason += f" (fallback de {engine} para {fb_engine})"
                    engine = fb_engine
                    model = ""
                    break

        return engine, model, reason
