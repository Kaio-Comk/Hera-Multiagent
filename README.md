# Multiagent Orchestrator

Sistema de orquestração multiagente local para Windows, integrando Claude Code CLI, Antigravity CLI, 9Router, Ollama local e vault do Obsidian.

## Arquitetura de Fases
- **Fase 1**: Core de Estados & Coordenação (SQLite WAL, feedback em tempo real).
- **Fase 2**: Roteamento Inteligente & Adaptador Ollama (GPU local).
- **Fase 3**: Adaptadores Claude Code, Antigravity & 9Router.
- **Fase 4**: Validação, Testes e Sincronização com Obsidian Vault.

## Execução de Testes
```bash
python -m pytest tests -v
```
