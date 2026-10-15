import os
from app.locations import resolve_vault_dir
import re
import glob
import hashlib
from typing import Dict, List, Optional, Any

class ObsidianMemoryManager:
    """Gerenciador de leitura semântica, injeção de contexto e memória persistente no Obsidian Vault.
    
    Permite ao orquestrador ler convenções, regras e notas do projeto no segundo cérebro,
    resolvendo wikilinks [[...]] e higienizando tokens antes da persistência.
    """

    # Padrões de segredos para redação de segurança
    SECRET_PATTERNS = [
        re.compile(r'(?:api[_-]?key|token|secret|password|bearer\s+)["\':=\s]+([a-zA-Z0-9_\-\.]{16,})', re.IGNORECASE),
        re.compile(r'(sk-[a-zA-Z0-9]{20,})'),
        re.compile(r'(ghp_[a-zA-Z0-9]{20,})'),
        re.compile(r'(glpat-[a-zA-Z0-9_\-]{20,})')
    ]

    def __init__(self, vault_project_dir: Optional[str] = None):
        self.vault_project_dir = os.path.abspath(resolve_vault_dir(vault_project_dir))
        self._note_cache: Dict[str, str] = {}
        self._index_vault()

    def _index_vault(self):
        """Indexa os arquivos .md do projeto para busca rápida e resolução de wikilinks."""
        self._note_cache.clear()
        if not os.path.exists(self.vault_project_dir):
            return

        for root, _, files in os.walk(self.vault_project_dir):
            for f in files:
                if f.endswith(".md"):
                    full_path = os.path.join(root, f)
                    note_name = f[:-3]  # Sem .md
                    self._note_cache[note_name.lower()] = full_path

    def sanitize_content(self, text: str) -> str:
        """Higieniza o conteúdo removendo tokens, segredos e chaves de API."""
        sanitized = text
        for pattern in self.SECRET_PATTERNS:
            sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
        return sanitized

    def resolve_wikilink(self, link_name: str) -> Optional[str]:
        """Localiza o conteúdo de uma nota referenciada por wikilink [[...]]."""
        clean_name = link_name.strip("[]").strip().lower()
        path = self._note_cache.get(clean_name)
        if not path or not os.path.exists(path):
            # Tentar busca parcial
            for cached_name, full_path in self._note_cache.items():
                if clean_name in cached_name:
                    path = full_path
                    break

        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return None
        return None

    def search_context(self, query: str, max_notes: int = 3, max_chars_per_note: int = 1500) -> List[Dict[str, str]]:
        """Pesquisa notas relevantes no vault com base nos termos da query."""
        keywords = [k.lower() for k in re.split(r'\W+', query) if len(k) >= 3]
        if not keywords:
            return []

        scored_notes = []

        for note_name, file_path in self._note_cache.items():
            # Não incluir o histórico de execuções passadas na busca de contexto primário
            if "08 histórico" in file_path.lower() or "08 historico" in file_path.lower():
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Pontuar relevância por ocorrência de palavras-chave
                score = 0
                lower_content = content.lower()
                for kw in keywords:
                    if kw in note_name:
                        score += 5  # Maior peso se estiver no título da nota
                    score += lower_content.count(kw)

                if score > 0:
                    scored_notes.append({
                        "title": os.path.basename(file_path)[:-3],
                        "path": file_path,
                        "score": score,
                        "content": content[:max_chars_per_note]
                    })
            except Exception:
                continue

        # Ordenar por maior pontuação
        scored_notes.sort(key=lambda x: x["score"], reverse=True)
        return scored_notes[:max_notes]

    def extract_context_from_instruction(self, instruction: str) -> str:
        """Detecta wikilinks explícitos [[...]] ou pesquisa tópicos no vault e monta o bloco de contexto."""
        context_parts = []

        # 1. Resolver wikilinks explícitos
        wikilinks = re.findall(r'\[\[(.*?)\]\]', instruction)
        resolved_links = set()

        for link in wikilinks:
            clean = link.split("|")[0].strip()  # Suporte a [[Nota|Alias]]
            if clean not in resolved_links:
                content = self.resolve_wikilink(clean)
                if content:
                    resolved_links.add(clean)
                    context_parts.append(
                        f"### Contexto do Vault: [[{clean}]]\n{content[:2000].strip()}\n"
                    )

        # 2. Se não houver wikilink explícito, fazer busca semântica por palavras-chave
        if not context_parts:
            notes = self.search_context(instruction, max_notes=2)
            for n in notes:
                context_parts.append(
                    f"### Contexto do Vault: [[{n['title']}]]\n{n['content'].strip()}\n"
                )

        if context_parts:
            return "\n---\n" + "\n".join(context_parts) + "\n---\n"
        return ""

    def calculate_note_hash(self, content: str) -> str:
        """Gera hash SHA-256 do conteúdo para verificação de duplicatas."""
        clean = re.sub(r'\s+', ' ', content).strip()
        return hashlib.sha256(clean.encode('utf-8')).hexdigest()

    def is_duplicate_note(self, directory: str, content: str) -> bool:
        """Verifica se já existe uma nota com conteúdo idêntico no diretório."""
        target_hash = self.calculate_note_hash(content)
        if not os.path.exists(directory):
            return False

        for f in os.listdir(directory):
            if f.endswith(".md"):
                file_path = os.path.join(directory, f)
                try:
                    with open(file_path, "r", encoding="utf-8") as file:
                        existing = file.read()
                        if self.calculate_note_hash(existing) == target_hash:
                            return True
                except Exception:
                    continue
        return False
