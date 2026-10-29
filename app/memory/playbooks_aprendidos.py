"""Playbooks aprendidos a partir de execucoes bem-sucedidas, com aprovacao manual."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from app.privacy_guard import PrivacyGuard


def _assinatura(tipo_tarefa: str, passos: list[dict]) -> str:
    """Monta a assinatura estavel de um playbook."""
    partes = [tipo_tarefa]
    for passo in passos:
        partes.append(f"{passo.get('engine', '')}|{passo.get('model', '')}")
    return "\n".join(partes)


def _id_curto(assinatura: str) -> str:
    """Gera um id curto e estavel (12 hex) a partir da assinatura."""
    return hashlib.sha256(assinatura.encode('utf-8')).hexdigest()[:12]


class PlaybooksAprendidos:
    """Armazena execucoes, propostas e playbooks ativos em um JSON."""

    def __init__(self, caminho: Path, *, minimo: int = 3) -> None:
        self.caminho = Path(caminho)
        self.minimo = minimo
        self._dados = self._carregar()

    # ------------------------------------------------------------------ IO
    def _vazio(self) -> dict:
        return {'execucoes': [], 'propostas': {}, 'ativos': {}, 'recusados': []}

    def _carregar(self) -> dict:
        """Le o arquivo; se ausente ou corrompido, comeca vazio."""
        try:
            texto = self.caminho.read_text(encoding='utf-8')
            dados = json.loads(texto)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return self._vazio()
        if not isinstance(dados, dict):
            return self._vazio()
        base = self._vazio()
        for chave in base:
            if chave in dados:
                base[chave] = dados[chave]
        return base

    def _salvar(self) -> None:
        """Gravacao atomica via arquivo temporario + os.replace."""
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.caminho.parent), prefix='.playbooks-', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(self._dados, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.caminho)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------- registro
    def registrar(self, tipo_tarefa: str, passos: list[dict], sucesso: bool) -> str | None:
        """Registra uma execucao bem-sucedida e propoe playbook se atingir o minimo."""
        if not sucesso:
            return None

        passos_limpos = []
        for passo in passos:
            passos_limpos.append({
                'engine': str(passo.get('engine', '')),
                'model': str(passo.get('model', '')),
                'titulo': PrivacyGuard.sanitize_content(str(passo.get('titulo', ''))),
            })

        assinatura = _assinatura(tipo_tarefa, passos_limpos)
        pid = _id_curto(assinatura)

        self._dados['execucoes'].append({
            'tipo_tarefa': tipo_tarefa,
            'assinatura': assinatura,
            'passos': passos_limpos,
        })

        if pid in self._dados['propostas'] or pid in self._dados['ativos']:
            self._salvar()
            return None
        if pid in self._dados['recusados']:
            self._salvar()
            return None

        total = sum(
            1 for e in self._dados['execucoes'] if e.get('assinatura') == assinatura
        )
        if total < self.minimo:
            self._salvar()
            return None

        self._dados['propostas'][pid] = {
            'id': pid,
            'tipo_tarefa': tipo_tarefa,
            'passos': [
                {'engine': p['engine'], 'model': p['model']} for p in passos_limpos
            ],
            'execucoes': total,
            'assinatura': assinatura,
        }
        self._salvar()
        return pid

    # ------------------------------------------------------------ consultas
    def propostas(self) -> list[dict]:
        """Lista as propostas pendentes."""
        return [
            {
                'id': p['id'],
                'tipo_tarefa': p['tipo_tarefa'],
                'passos': [dict(x) for x in p['passos']],
                'execucoes': p['execucoes'],
            }
            for p in self._dados['propostas'].values()
        ]

    def ativos(self) -> list[dict]:
        """Lista os playbooks ativos."""
        return [
            {
                'id': a['id'],
                'tipo_tarefa': a['tipo_tarefa'],
                'passos': [dict(x) for x in a['passos']],
                'execucoes': a.get('execucoes', 0),
                'usos': a.get('usos', 0),
            }
            for a in self._dados['ativos'].values()
        ]

    # ------------------------------------------------------------- decisoes
    def aprovar(self, id: str) -> bool:
        """Move uma proposta para ativos."""
        proposta = self._dados['propostas'].pop(id, None)
        if proposta is None:
            return False
        proposta['usos'] = 0
        self._dados['ativos'][id] = proposta
        self._salvar()
        return True

    def recusar(self, id: str) -> bool:
        """Remove a proposta e marca o id como recusado."""
        proposta = self._dados['propostas'].pop(id, None)
        if proposta is None:
            return False
        if id not in self._dados['recusados']:
            self._dados['recusados'].append(id)
        self._salvar()
        return True

    def desativar(self, id: str) -> bool:
        """Remove um playbook dos ativos."""
        if id not in self._dados['ativos']:
            return False
        del self._dados['ativos'][id]
        self._salvar()
        return True

    def sugerir(self, tipo_tarefa: str) -> dict | None:
        """Devolve o playbook ativo mais usado do tipo, ou None."""
        candidatos = [
            a for a in self._dados['ativos'].values()
            if a.get('tipo_tarefa') == tipo_tarefa
        ]
        if not candidatos:
            return None
        melhor = max(candidatos, key=lambda a: a.get('usos', 0))
        melhor['usos'] = melhor.get('usos', 0) + 1
        self._salvar()
        return {
            'id': melhor['id'],
            'tipo_tarefa': melhor['tipo_tarefa'],
            'passos': [dict(x) for x in melhor['passos']],
            'execucoes': melhor.get('execucoes', 0),
            'usos': melhor['usos'],
        }
