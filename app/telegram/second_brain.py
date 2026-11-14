from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("hera.telegram.second_brain")

def _descobrir_vault_padrao() -> Path:
    if os.environ.get("NETHUB_VAULT"):
        return Path(os.environ["NETHUB_VAULT"])
    home_vault = Path.home() / "Documents" / "Obsidian Vault"
    if (home_vault / ".obsidian").exists():
        return home_vault
    users_dir = Path(os.environ.get("SystemDrive", "C:") + r"\Users")
    if users_dir.is_dir():
        try:
            for u in users_dir.iterdir():
                if u.is_dir() and u.name.lower() not in ("public", "default", "all users"):
                    cand = u / "Documents" / "Obsidian Vault"
                    if (cand / ".obsidian").exists():
                        return cand
        except OSError:
            pass
    return home_vault


VAULT_PADRAO = _descobrir_vault_padrao()


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


@dataclass
class NotaCache:
    caminho: Path
    titulo: str
    pasta: str
    tags: List[str]
    conteudo: str
    conteudo_norm: str
    mtime: float


class ObsidianSecondBrain:
    """Segundo Cérebro completo conectado ao Obsidian Vault do Kaio.

    Indexa e pesquisa todas as notas do cofre (NetHub, Arquitetura, ADRs, Runbooks,
    Configurações de TV/Apps e Soluções de Erros) para municiar a IA de atendimento.
    """

    def __init__(self, vault_path: Optional[Path | str] = None) -> None:
        if vault_path:
            self.root = Path(vault_path)
        elif VAULT_PADRAO.exists():
            self.root = VAULT_PADRAO
        else:
            self.root = Path.home() / "Documents" / "Obsidian Vault"

        self._notas: Dict[str, NotaCache] = {}
        self._ultimo_scan: float = 0.0
        self._intervalo_reindex_s: float = 60.0  # reindexa após 1 min se houver mudanças

        if self.root.exists():
            self._indexar_vault()
        else:
            log.warning("[second_brain] Vault não encontrado no caminho: %s", self.root)

    def _indexar_vault(self) -> None:
        """Varre o cofre do Obsidian e carrega as notas em memória."""
        inicio = time.time()
        pastas_ignoradas = {".git", ".obsidian", ".trash", "08 histórico", "08 historico"}

        if not self.root.exists():
            return

        novas_notas: Dict[str, NotaCache] = {}

        for root, dirs, files in os.walk(self.root):
            # Filtrar pastas ignoradas
            dirs[:] = [d for d in dirs if d.lower() not in pastas_ignoradas and not d.startswith(".")]

            root_path = Path(root)
            for f in files:
                if not f.endswith(".md"):
                    continue

                full_path = root_path / f
                try:
                    rel = full_path.relative_to(self.root)
                    rel_str = str(rel).replace("\\", "/")
                    mtime = full_path.stat().st_mtime

                    # Se já temos no cache e mtime não mudou, reaproveita
                    if rel_str in self._notas and self._notas[rel_str].mtime == mtime:
                        novas_notas[rel_str] = self._notas[rel_str]
                        continue

                    texto = full_path.read_text(encoding="utf-8", errors="replace")

                    # Extrair tags simples
                    tags = re.findall(r"(?:^|\s)#([a-zA-Z0-9_\-\/]+)", texto)

                    # Título sem .md
                    titulo = f[:-3]
                    pasta_pai = rel.parts[0] if len(rel.parts) > 1 else ""

                    novas_notas[rel_str] = NotaCache(
                        caminho=full_path,
                        titulo=titulo,
                        pasta=pasta_pai,
                        tags=tags,
                        conteudo=texto,
                        conteudo_norm=sem_acento(texto),
                        mtime=mtime,
                    )
                except Exception as exc:
                    log.debug("[second_brain] Falha ao ler nota %s: %s", f, exc)

        self._notas = novas_notas
        self._ultimo_scan = time.time()
        log.info("[second_brain] Indexação concluída em %.2fs. %d notas indexadas.", self._ultimo_scan - inicio, len(self._notas))

    def garantir_indexado(self) -> None:
        """Reindexa o cofre se o intervalo tiver expirado."""
        if time.time() - self._ultimo_scan > self._intervalo_reindex_s:
            self._indexar_vault()

    def buscar_contexto(
        self,
        query: str,
        max_notas: int = 4,
        max_chars_por_nota: int = 1500,
        foco_pasta: str = "NetHub",
    ) -> List[Dict[str, Any]]:
        """Pesquisa notas relevantes no Segundo Cérebro combinando correspondência de títulos, tags e termos.

        Prioriza a pasta do projeto (foco_pasta) e notas de Decisões/ADRs/Operação.
        """
        self.garantir_indexado()
        q_norm = sem_acento(query).strip()
        termos = [t for t in re.split(r"\W+", q_norm) if len(t) >= 3]

        if not termos:
            return []

        resultados: List[Dict[str, Any]] = []

        for rel_str, nota in self._notas.items():
            score = 0
            titulo_norm = sem_acento(nota.titulo)

            # 1. Correspondência exata da query no título (peso altíssimo)
            if q_norm in titulo_norm:
                score += 30

            # 2. Termos no título da nota
            for t in termos:
                if t in titulo_norm:
                    score += 10
                elif any(t in sem_acento(tag) for tag in nota.tags):
                    score += 6
                # Ocorrências no corpo da nota
                score += min(nota.conteudo_norm.count(t), 5)

            # 3. Bônus por pasta de foco (NetHub)
            if nota.pasta.lower() == foco_pasta.lower():
                score += 5

            # 4. Bônus por nota de Operação / Decisões (ADR)
            if "04 decisões" in rel_str.lower() or "04 decisoes" in rel_str.lower():
                score += 3
            elif "03 operação" in rel_str.lower() or "03 operacao" in rel_str.lower():
                score += 4

            if score > 0:
                trecho = self._extrair_trecho_relevante(nota.conteudo, termos, max_chars_por_nota)
                resultados.append({
                    "titulo": nota.titulo,
                    "rel_path": rel_str,
                    "pasta": nota.pasta,
                    "score": score,
                    "conteudo": trecho,
                })

        # Ordenar por maior pontuação
        resultados.sort(key=lambda x: x["score"], reverse=True)
        return resultados[:max_notas]

    def _extrair_trecho_relevante(self, conteudo: str, termos: List[str], max_chars: int) -> str:
        """Localiza a região mais densa em termos da busca no texto da nota."""
        linhas = conteudo.splitlines()
        if len(conteudo) <= max_chars:
            return conteudo.strip()

        melhor_idx = 0
        maior_densidade = -1

        for i in range(len(linhas)):
            bloco_texto = " ".join(linhas[i:i + 20])
            bloco_norm = sem_acento(bloco_texto)
            densidade = sum(bloco_norm.count(t) for t in termos)
            if densidade > maior_densidade:
                maior_densidade = densidade
                melhor_idx = i

        trecho_linhas = linhas[max(0, melhor_idx - 2):melhor_idx + 25]
        saida = "\n".join(trecho_linhas).strip()
        if len(saida) > max_chars:
            saida = saida[:max_chars] + "..."
        return saida

    def formatar_contexto_para_prompt(self, query: str, max_notas: int = 4) -> str:
        """Formata o bloco de conhecimento extraído do Obsidian para injeção no prompt do LLM."""
        notas_relevantes = self.buscar_contexto(query, max_notas=max_notas)
        if not notas_relevantes:
            return ""

        linhas = ["### CONTEXTO DO SEGUNDO CÉREBRO (Obsidian Vault):"]
        for n in notas_relevantes:
            linhas.append(f"#### [[{n['titulo']}]] (Pasta: {n['pasta']}):\n{n['conteudo']}\n")

        return "\n".join(linhas)
