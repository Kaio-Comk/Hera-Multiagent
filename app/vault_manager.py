"""Gerenciador do cofre do Obsidian dentro do Hera: ler, criar, editar, renomear (atualizando links), mover para a
lixeira, buscar, backlinks, tags e nota diária.

Segurança (o cofre tem notas privadas e a pasta `.obsidian` guarda plugins, que são CÓDIGO):
  * todo caminho é relativo ao cofre, sem `..`, sem unidade, sem `:` (fluxos NTFS), sem nomes reservados do Windows
    e é conferido depois de resolvido (links simbólicos/junções que saem do cofre são recusados);
  * qualquer pasta ou arquivo que comece com `.` (`.obsidian`, `.git`, `.trash`) é invisível e intocável aqui;
  * só se edita `.md` em UTF-8 e até 2 MiB; imagens só podem ser LIDAS (png/jpg/gif/webp), sem SVG;
  * escrita atômica (arquivo temporário + `os.replace`) e detecção de conflito por hash: se a nota mudou no disco
    (por exemplo, editada no Obsidian) o Hera recusa sobrescrever sem você decidir;
  * "excluir" move para `.trash/` do cofre (como o Obsidian), nunca apaga de verdade.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_FILES = 20000
MAX_NAME_LEN = 150
MAX_DEPTH = 12
MAX_RENAME_REWRITES = 2000
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-zÀ-ÿ0-9_\-/]+)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


class VaultError(ValueError):
    """Pedido recusado (mensagem pronta para mostrar ao usuário). `status` sugere o HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class VaultConflict(VaultError):
    def __init__(self, message: str, current_hash: str):
        super().__init__(message, 409)
        self.current_hash = current_hash


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class Parsed:
    mtime_ns: int
    size: int
    title: str
    tags: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)          # alvos brutos dos [[wikilinks]]
    headings: List[Dict[str, Any]] = field(default_factory=list)
    text: str = ""                                          # minúsculo, para busca ("" se o arquivo for grande)


def strip_code(text: str) -> str:
    return _FENCE.sub("", text)


def parse_note(path: str, raw: bytes, mtime_ns: int) -> Parsed:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Parsed(mtime_ns, len(raw), Path(path).stem)
    body = strip_code(text)
    tags = set(m.group(1) for m in _TAG.finditer(body) if not m.group(1).isdigit())
    front = _FRONT.match(text)
    if front:
        for line in front.group(1).splitlines():
            if line.lower().startswith("tags:"):
                rest = line.split(":", 1)[1]
                tags |= {t.strip(" #\"'") for t in re.split(r"[,\s\[\]]+", rest) if t.strip(" #\"'")}
    links = [m.group(2) for m in _WIKILINK.finditer(body)]
    heads = [{"level": len(m.group(1)), "text": m.group(2)} for m in _HEADING.finditer(body)][:200]
    return Parsed(mtime_ns, len(raw), Path(path).stem, sorted(tags)[:50], links[:2000], heads,
                  text.lower() if len(raw) <= 300_000 else "")


class VaultManager:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._parsed: Dict[str, Parsed] = {}
        self._files: Dict[str, int] = {}                    # md: caminho -> mtime_ns
        self._scan_at = 0.0
        self._by_stem: Dict[str, List[str]] = {}
        self._backlinks: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------ caminhos
    def exists(self) -> bool:
        return self.root.is_dir()

    def safe(self, rel: str, *, must_exist: Optional[bool] = None, md_only: bool = False) -> Tuple[str, Path]:
        """Valida um caminho relativo e devolve (relativo normalizado com '/', caminho absoluto)."""
        if not isinstance(rel, str) or not rel.strip() or len(rel) > 600:
            raise VaultError("Caminho inválido.")
        rel = rel.replace("\\", "/").strip()
        if rel.startswith("/"):
            raise VaultError("Use caminhos relativos ao cofre.")
        rel = rel.rstrip("/")
        parts = rel.split("/") if rel else []
        if not parts or len(parts) > MAX_DEPTH:
            raise VaultError("Caminho inválido ou fundo demais.")
        for part in parts:
            stem = part.split(".")[0].lower()
            if (part in (".", "..") or part.startswith(".") or _BAD_CHARS.search(part) or len(part) > MAX_NAME_LEN
                    or part != part.rstrip(" .") or stem in _RESERVED):
                raise VaultError(f"Nome não permitido: {part!r}")
        if md_only and not parts[-1].lower().endswith(".md"):
            raise VaultError("Só notas .md podem ser editadas aqui.")
        full = (self.root / Path(*parts)).resolve()
        try:
            full.relative_to(self.root)
        except ValueError:
            raise VaultError("Esse caminho sai do cofre.", 403)
        if must_exist is True and not full.exists():
            raise VaultError("Não encontrado.", 404)
        if must_exist is False and full.exists():
            raise VaultError("Já existe.", 409)
        return "/".join(parts), full

    # ------------------------------------------------------------------ varredura
    def _refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._scan_at < 2.0:
                return
            files: Dict[str, int] = {}
            stack = [self.root]
            while stack and len(files) < MAX_FILES:
                directory = stack.pop()
                try:
                    entries = list(os.scandir(directory))
                except OSError:
                    continue
                for e in entries:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(".md"):
                            rel = Path(e.path).relative_to(self.root).as_posix()
                            files[rel] = e.stat().st_mtime_ns
                    except OSError:
                        continue
            for rel in list(self._parsed):
                if rel not in files:
                    del self._parsed[rel]
            for rel, mtime in files.items():
                cached = self._parsed.get(rel)
                if cached is not None and cached.mtime_ns == mtime:
                    continue
                try:
                    raw = (self.root / rel).read_bytes()[:MAX_NOTE_BYTES]
                except OSError:
                    continue
                self._parsed[rel] = parse_note(rel, raw, mtime)
            self._files = files
            self._by_stem = {}
            for rel in files:
                self._by_stem.setdefault(Path(rel).stem.lower(), []).append(rel)
            for paths in self._by_stem.values():
                paths.sort(key=lambda p: (p.count("/"), p))
            self._backlinks = {}
            for rel, parsed in self._parsed.items():
                for target in parsed.links:
                    resolved = self._resolve(target)
                    if resolved and resolved != rel:
                        self._backlinks.setdefault(resolved, set()).add(rel)
            self._scan_at = time.monotonic()

    def _resolve(self, target: str) -> Optional[str]:
        """Alvo de um [[wikilink]] -> caminho da nota (ou None)."""
        name = target.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/").strip("/")
        if not name:
            return None
        low = name.lower()
        if low.endswith(".md"):
            low = low[:-3]
        if "/" in low:
            for rel in self._files:
                if rel.lower()[:-3] == low or rel.lower()[:-3].endswith("/" + low):
                    return rel
            return None
        paths = self._by_stem.get(low)
        return paths[0] if paths else None

    # ------------------------------------------------------------------ consulta
    def info(self) -> Dict[str, Any]:
        self._refresh()
        return {"name": self.root.name, "notes": len(self._files), "writable": os.access(self.root, os.W_OK)}

    def tree(self) -> Dict[str, Any]:
        self._refresh(force=True)
        root: Dict[str, Any] = {"name": self.root.name, "path": "", "type": "folder", "children": []}
        folders: Dict[str, Dict[str, Any]] = {"": root}

        def folder_for(path: str) -> Dict[str, Any]:
            if path in folders:
                return folders[path]
            parent = folder_for(path.rsplit("/", 1)[0] if "/" in path else "")
            node = {"name": path.rsplit("/", 1)[-1], "path": path, "type": "folder", "children": []}
            parent["children"].append(node)
            folders[path] = node
            return node

        # pastas vazias também aparecem
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel = Path(dirpath).relative_to(self.root).as_posix()
            if rel != ".":
                folder_for(rel)
        for rel in sorted(self._files, key=str.lower):
            parent = folder_for(rel.rsplit("/", 1)[0] if "/" in rel else "")
            parent["children"].append({"name": Path(rel).stem, "path": rel, "type": "note"})

        def order(node: Dict[str, Any]) -> None:
            node["children"].sort(key=lambda c: (c["type"] != "folder", c["name"].lower()))
            for c in node["children"]:
                if c["type"] == "folder":
                    order(c)
        order(root)
        return root

    def read(self, rel: str) -> Dict[str, Any]:
        rel, full = self.safe(rel, must_exist=True, md_only=True)
        raw = full.read_bytes()
        if len(raw) > MAX_NOTE_BYTES:
            raise VaultError("Nota grande demais para editar aqui (limite de 2 MiB).", 413)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise VaultError("A nota não está em UTF-8; abra no Obsidian.", 415)
        self._refresh()
        parsed = self._parsed.get(rel) or parse_note(rel, raw, full.stat().st_mtime_ns)
        outgoing = []
        for target in dict.fromkeys(parsed.links):
            resolved = self._resolve(target)
            outgoing.append({"target": target.split("|", 1)[0], "path": resolved})
        return {"path": rel, "content": text.replace("\r\n", "\n"), "hash": content_hash(raw),
                "crlf": "\r\n" in text, "mtime": full.stat().st_mtime, "size": len(raw), "tags": parsed.tags,
                "headings": parsed.headings, "outgoing": outgoing,
                "backlinks": sorted(self._backlinks.get(rel, ()), key=str.lower)}

    def read_image(self, rel: str) -> Tuple[bytes, str]:
        rel, full = self.safe(rel, must_exist=True)
        kind = IMAGE_TYPES.get(full.suffix.lower())
        if not kind or not full.is_file():
            raise VaultError("Só imagens png, jpg, gif e webp podem ser abertas.", 415)
        if full.stat().st_size > 20 * 1024 * 1024:
            raise VaultError("Imagem grande demais.", 413)
        return full.read_bytes(), kind

    def find_attachment(self, name: str) -> Optional[str]:
        """Procura uma imagem pelo nome (como o Obsidian faz com ![[foto.png]])."""
        name = name.split("|", 1)[0].strip().replace("\\", "/")
        if Path(name).suffix.lower() not in IMAGE_TYPES:
            return None
        direct = (self.root / name).resolve()
        try:
            if direct.is_file():
                return direct.relative_to(self.root).as_posix()
        except (ValueError, OSError):
            pass
        base = Path(name).name.lower()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                if f.lower() == base:
                    return (Path(dirpath) / f).relative_to(self.root).as_posix()
        return None

    # ------------------------------------------------------------------ escrita
    def _write_atomic(self, full: Path, data: bytes) -> None:
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(full.name + ".hera-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, full)

    def _encode(self, content: str, crlf: bool) -> bytes:
        if not isinstance(content, str):
            raise VaultError("Conteúdo inválido.")
        text = content.replace("\r\n", "\n")
        if crlf:
            text = text.replace("\n", "\r\n")
        raw = text.encode("utf-8")
        if len(raw) > MAX_NOTE_BYTES:
            raise VaultError("Nota grande demais (limite de 2 MiB).", 413)
        return raw

    def create(self, rel: str, content: str = "") -> Dict[str, Any]:
        if not rel.lower().endswith(".md"):
            rel += ".md"
        rel, full = self.safe(rel, must_exist=False, md_only=True)
        raw = self._encode(content, False)
        self._write_atomic(full, raw)
        self._refresh(force=True)
        return {"path": rel, "hash": content_hash(raw)}

    def write(self, rel: str, content: str, base_hash: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        rel, full = self.safe(rel, md_only=True)
        crlf = False
        if full.exists():
            current = full.read_bytes()
            crlf = b"\r\n" in current
            if base_hash and not force and content_hash(current) != base_hash:
                raise VaultConflict("A nota mudou no disco depois que você a abriu (talvez no Obsidian).", content_hash(current))
        elif base_hash:
            raise VaultError("A nota foi apagada ou movida depois que você a abriu.", 404)
        raw = self._encode(content, crlf)
        self._write_atomic(full, raw)
        self._refresh(force=True)
        return {"path": rel, "hash": content_hash(raw), "mtime": full.stat().st_mtime}

    def mkdir(self, rel: str) -> Dict[str, Any]:
        rel, full = self.safe(rel, must_exist=False)
        full.mkdir(parents=True, exist_ok=False)
        return {"path": rel}

    def trash(self, rel: str) -> Dict[str, Any]:
        rel, full = self.safe(rel, must_exist=True)
        if not full.is_file() and not full.is_dir():
            raise VaultError("Não encontrado.", 404)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_dir = self.root / ".trash"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"{full.stem if full.is_file() else full.name}-{stamp}{full.suffix if full.is_file() else ''}"
        os.replace(full, dest)
        self._refresh(force=True)
        return {"trashed": rel, "to": f".trash/{dest.name}"}

    # ------------------------------------------------------------------ renomear (atualizando [[links]])
    def _rewrite_links(self, text: str, old_rel: str, new_rel: str) -> Tuple[str, int]:
        new_stem = Path(new_rel).stem
        count = 0

        def fix_segment(segment: str) -> str:
            nonlocal count

            def repl(m: "re.Match[str]") -> str:
                nonlocal count
                inner = m.group(2)
                name = inner.split("|", 1)[0].split("#", 1)[0].strip()
                if self._resolve(name) != old_rel:
                    return m.group(0)
                rest = inner[len(inner.split("|", 1)[0].split("#", 1)[0]):]
                if "/" in name:
                    new_name = new_rel[:-3]
                else:
                    others = [p for p in self._by_stem.get(new_stem.lower(), []) if p != old_rel]
                    new_name = new_stem if not others else new_rel[:-3]
                count += 1
                return f"{m.group(1)}[[{new_name}{rest}]]"
            return _WIKILINK.sub(repl, segment)

        parts, last = [], 0
        for fence in _FENCE.finditer(text):                    # não mexe em blocos de código
            parts.append(fix_segment(text[last:fence.start()]))
            parts.append(fence.group(0))
            last = fence.end()
        parts.append(fix_segment(text[last:]))
        return "".join(parts), count

    def rename(self, src: str, dst: str, update_links: bool = True, dry_run: bool = False) -> Dict[str, Any]:
        src_rel, src_full = self.safe(src, must_exist=True)
        is_note = src_full.is_file()
        if is_note and not dst.lower().endswith(".md"):
            dst += ".md"
        dst_rel, dst_full = self.safe(dst, must_exist=False, md_only=is_note)
        if is_note and not src_rel.lower().endswith(".md"):
            raise VaultError("Só notas .md podem ser renomeadas aqui.")
        self._refresh(force=True)
        changed: List[Dict[str, Any]] = []
        if update_links and is_note:
            affected = sorted(set(self._backlinks.get(src_rel, set())) | {src_rel})
            if len(affected) > MAX_RENAME_REWRITES:
                raise VaultError("Notas demais apontam para esta; renomeie no Obsidian.", 413)
            for rel in affected:
                path = self.root / rel
                try:
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                new_text, n = self._rewrite_links(text, src_rel, dst_rel)
                if n:
                    changed.append({"path": rel, "links": n, "_raw": raw, "_text": new_text})
        summary = [{"path": c["path"], "links": c["links"]} for c in changed]
        if dry_run:
            return {"dry_run": True, "from": src_rel, "to": dst_rel, "notes_to_update": summary}
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_full, dst_full)
        for c in changed:
            target = dst_full if c["path"] == src_rel else self.root / c["path"]
            data = c["_text"].encode("utf-8")
            if len(data) <= MAX_NOTE_BYTES:
                self._write_atomic(target, data)
        self._refresh(force=True)
        return {"dry_run": False, "from": src_rel, "to": dst_rel, "notes_updated": summary}

    # ------------------------------------------------------------------ busca, tags, backlinks
    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        terms = [t for t in re.split(r"\s+", query.lower().strip()) if t][:8]
        if not terms:
            return []
        self._refresh()
        results = []
        for rel, parsed in self._parsed.items():
            text = parsed.text
            if not text and parsed.size <= MAX_NOTE_BYTES:
                try:
                    text = (self.root / rel).read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    continue
            title, path = parsed.title.lower(), rel.lower()
            tags = " ".join(parsed.tags).lower()
            haystack = f"{title}\n{path}\n{tags}\n{text}"
            if not all(t in haystack for t in terms):
                continue
            score = sum(5 * (t in title) + 2 * (t in path) + 3 * (t in tags) + min(text.count(t), 10) for t in terms)
            pos = min((text.find(t) for t in terms if t in text), default=-1)
            snippet = ""
            if pos >= 0:
                snippet = text[max(0, pos - 70):pos + 110].replace("\n", " ").strip()
            results.append({"path": rel, "title": parsed.title, "score": score, "snippet": snippet, "tags": parsed.tags})
        results.sort(key=lambda r: (-r["score"], r["path"].lower()))
        return results[:limit]

    def tags(self, tag: Optional[str] = None) -> Any:
        self._refresh()
        if tag:
            tag = tag.lstrip("#").lower()
            return sorted(rel for rel, p in self._parsed.items() if tag in (t.lower() for t in p.tags))
        counts: Dict[str, int] = {}
        for p in self._parsed.values():
            for t in p.tags:
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))][:300]

    def graph(self, limit: int = 400) -> Dict[str, Any]:
        """Nós e arestas para o mapa de notas (as mais ligadas primeiro)."""
        self._refresh()
        degree: Dict[str, int] = {}
        edges: List[Tuple[str, str]] = []
        for rel, parsed in self._parsed.items():
            for target in dict.fromkeys(parsed.links):
                resolved = self._resolve(target)
                if resolved and resolved != rel:
                    edges.append((rel, resolved))
                    degree[rel] = degree.get(rel, 0) + 1
                    degree[resolved] = degree.get(resolved, 0) + 1
        keep = set(sorted(degree, key=lambda r: -degree[r])[:limit])
        return {"nodes": [{"path": r, "title": Path(r).stem, "degree": degree[r]} for r in keep],
                "edges": [{"from": a, "to": b} for a, b in edges if a in keep and b in keep]}

    # ------------------------------------------------------------------ nota diária
    def daily(self, day: Optional[datetime] = None) -> Dict[str, Any]:
        day = day or datetime.now()
        folder, fmt, template = "Daily", "YYYY-MM-DD", ""
        cfg = self.root / ".obsidian" / "daily-notes.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            folder = str(data.get("folder") or "").strip("/\\") or ""
            fmt = str(data.get("format") or fmt)
            template = str(data.get("template") or "")
        except (OSError, ValueError):
            pass
        if not re.fullmatch(r"(?:YYYY|MM|DD|[-_ ./])+", fmt):
            fmt = "YYYY-MM-DD"
        name = fmt.replace("YYYY", f"{day.year:04d}").replace("MM", f"{day.month:02d}").replace("DD", f"{day.day:02d}")
        rel = f"{folder}/{name}.md" if folder else f"{name}.md"
        rel, full = self.safe(rel, md_only=True)
        created = False
        if not full.exists():
            body = f"# {day.strftime('%Y-%m-%d')}\n\n"
            if template:
                try:
                    _, tfull = self.safe(template if template.lower().endswith(".md") else template + ".md", must_exist=True, md_only=True)
                    body = tfull.read_text(encoding="utf-8")
                except (VaultError, OSError):
                    pass
            self.create(rel, body)
            created = True
        return {"path": rel, "created": created}
