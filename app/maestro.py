from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Literal

Peso = Literal['leve', 'medio', 'pesado']
ORDEM: tuple[Peso, ...] = ('leve', 'medio', 'pesado')

PREFERENCIAS_PADRAO: dict[str, list[str]] = {
    'leve': [
        'deepseek-ai/DeepSeek-V4.1-Flash',  # River (credito com validade) e ~2 s; o Gemini lite chegou a 14-28 s em 29/10/2026
        'gemini-flash-lite-latest',
        'gpt-4o-mini',
        'agnes-2.5-flash',
    ],
    'medio': [
        'deepseek-ai/DeepSeek-V4.1-Flash',
        'zai-org/GLM-5.3-Flash',
        'gpt-oss-120b',
        'agnes-2.5-pro',
        'kimi-k2.7-code',
    ],
    'pesado': [
        'GLM-5.3',
        'kimi-k2-thinking-251104',
        'moonshotai/Kimi-K3',
        'agnes-2.5-pro',
        'deepseek-ai/DeepSeek-V4.1-Flash',
    ],
}

PERFIS: dict[str, dict[str, list[str]]] = {
    'economico': {
        'leve': PREFERENCIAS_PADRAO['leve'][:2],
        'medio': PREFERENCIAS_PADRAO['leve'][:2],
        'pesado': PREFERENCIAS_PADRAO['medio'],
    },
    'qualidade': {
        'leve': PREFERENCIAS_PADRAO['medio'],
        'medio': PREFERENCIAS_PADRAO['pesado'],
        'pesado': PREFERENCIAS_PADRAO['pesado'],
    },
}

_PALAVRAS_PESADAS = (
    'arquitetura',
    'refator',
    'depurar',
    'debug',
    'bug dificil',
    'seguranca',
    'vulnerabilidade',
    'migrar',
    'migracao',
    'projete',
    'desempenho',
    'concorrencia',
    'implemente um sistema',
    'varios arquivos',
)

_VERBOS_CODIGO = (
    'implemente',
    'crie',
    'escreva',
    'corrija',
    'edite',
    'refatore',
    'rode',
    'execute',
    'instale',
    'apague',
)

_SAUDACOES = ('oi', 'ola', 'obrigado')
_PERGUNTAS_CURTAS = ('o que e', 'qual', 'como se diz', 'traduza', 'resuma')


def _normalizar(texto: str) -> str:
    """Minusculas sem acentos para comparacao barata."""
    decomposto = unicodedata.normalize('NFKD', texto)
    sem_acento = ''.join(c for c in decomposto if not unicodedata.combining(c))
    return sem_acento.lower()


def _linhas_bloco_codigo(texto: str) -> int:
    """Conta linhas dentro de blocos de codigo markdown."""
    total = 0
    dentro = False
    for linha in texto.splitlines():
        if linha.strip().startswith('```'):
            dentro = not dentro
            continue
        if dentro:
            total += 1
    return total


def classificar_local(texto: str) -> tuple[str, str]:
    """Classifica o peso do pedido por regras locais baratas."""
    normalizado = _normalizar(texto)
    if len(texto) > 1500:
        return 'pesado', 'texto muito longo'
    if _linhas_bloco_codigo(texto) > 40:
        return 'pesado', 'bloco de codigo grande'
    for palavra in _PALAVRAS_PESADAS:
        if palavra in normalizado:
            return 'pesado', f'palavra-chave pesada: {palavra}'
    if len(texto) <= 120:
        if any(s in normalizado for s in _SAUDACOES):
            return 'leve', 'saudacao curta'
        if any(p in normalizado for p in _PERGUNTAS_CURTAS):
            return 'leve', 'pergunta curta'
        if not any(v in normalizado for v in _VERBOS_CODIGO):
            return 'leve', 'pedido curto sem acao de codigo'
    return 'medio', 'pedido comum'


PROMPT_FAIXA = (
    "Classifique o pedido do usuario em UMA palavra: "
    "leve (saudacao, pergunta curta, traducao, resumo curto), "
    "medio (explicar, documentar, revisar, codigo pequeno), "
    "pesado (arquitetura, bug dificil, varios arquivos, seguranca, raciocinio longo). "
    "Responda somente a palavra.\n"
    "Pedido:\n"
)


def ler_faixa(resposta: str | None) -> str | None:
    """Extrai a primeira faixa valida (leve/medio/pesado) da resposta do juiz."""
    if not resposta:
        return None
    texto = _normalizar(resposta)
    m = re.search(r"\b(leve|medio|pesado)\b", texto)
    if not m:
        return None
    return m.group(1)


def regra_incerta(peso: str, motivo: str, texto: str) -> bool:
    """Diz se a classificacao local merece consulta ao juiz."""
    if motivo == "pedido comum":
        return True
    if peso == "pesado" and motivo.startswith("palavra-chave") and len(texto) < 200:
        return True
    return False


def _faixas_vizinhas(peso: str) -> list[str]:
    """Ordem de fallback entre faixas de peso."""
    if peso == 'leve':
        return ['medio']
    if peso == 'medio':
        return ['leve', 'pesado']
    if peso == 'pesado':
        return ['medio']
    return []


def escolher_modelo(
    peso: str,
    disponiveis: Collection[str],
    preferencias: dict[str, list[str]] | None = None,
    *,
    indisponiveis: Collection[str] = (),
) -> str | None:
    """Primeiro modelo da faixa disponivel e nao indisponivel."""
    prefs = preferencias if preferencias is not None else PREFERENCIAS_PADRAO
    disp = set(disponiveis)
    indisp = set(indisponiveis)
    for faixa in [peso, *_faixas_vizinhas(peso)]:
        for modelo in prefs.get(faixa, []):
            if modelo in disp and modelo not in indisp:
                return modelo
    return None


@dataclass(frozen=True)
class Escolha:
    """Resultado da escolha de modelo."""

    modelo: str
    peso: str
    motivo: str
    forcada: bool = False


class Maestro:
    """Escolhe modelo por peso sem gastar chamada de IA."""

    def __init__(
        self,
        disponiveis_de: Callable[[], Awaitable[list[str]]],
        *,
        ativo: bool = True,
        forcado: str | None = None,
        perfil: str | None = None,
        preferencias: dict[str, list[str]] | None = None,
        ttl: float = 60.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        if perfil is not None and perfil not in PERFIS:
            raise ValueError(f'perfil desconhecido: {perfil}')
        self._disponiveis_de = disponiveis_de
        self.ativo = ativo
        self.forcado = forcado
        self._perfil = perfil
        self._preferencias = preferencias
        self._ttl = ttl
        self._relogio = relogio
        self._cache: list[str] = []
        self._cache_em: float | None = None
        self._indisponiveis: dict[str, float] = {}
        self.juiz: Callable[[str], Awaitable[str | None]] | None = None
        self.timeout_juiz: float = 8.0
        self.pausa_juiz: float = 300.0  # apos falha ou timeout, o juiz descansa (nao atrasa cada pedido)
        self._juiz_pausado_ate: float = 0.0

    @property
    def perfil(self) -> str | None:
        """Perfil atual."""
        return self._perfil

    @perfil.setter
    def perfil(self, valor: str | None) -> None:
        if valor is not None and valor not in PERFIS:
            raise ValueError(f'perfil desconhecido: {valor}')
        self._perfil = valor

    def _prefs_efetivas(self) -> dict[str, list[str]]:
        if self._preferencias is not None:
            return self._preferencias
        if self._perfil is not None:
            return PERFIS[self._perfil]
        return PREFERENCIAS_PADRAO

    def _indisponiveis_ativos(self) -> set[str]:
        agora = self._relogio()
        return {m for m, exp in self._indisponiveis.items() if exp > agora}

    async def _disponiveis(self) -> list[str]:
        agora = self._relogio()
        if self._cache_em is not None and (agora - self._cache_em) < self._ttl:
            return self._cache
        try:
            lista = await self._disponiveis_de()
        except Exception:
            return self._cache
        self._cache = list(lista)
        self._cache_em = agora
        return self._cache

    def marcar_indisponivel(self, modelo: str, segundos: float = 300.0) -> None:
        """Marca modelo como indisponivel ate expirar."""
        self._indisponiveis[modelo] = self._relogio() + segundos

    async def escolher(self, texto: str) -> Escolha:
        peso, motivo = classificar_local(texto)
        if self.forcado:
            return Escolha(self.forcado, peso, "modelo forcado pelo usuario", True)
        if not self.ativo:
            return Escolha("default", "medio", "maestro desligado")
        if self.juiz and self._relogio() >= self._juiz_pausado_ate and regra_incerta(peso, motivo, texto):
            try:
                resposta = await asyncio.wait_for(self.juiz(texto[:1500]), self.timeout_juiz)
            except Exception:
                resposta = None
                self._juiz_pausado_ate = self._relogio() + self.pausa_juiz
            faixa = ler_faixa(resposta)
            if faixa:
                motivo = f"juiz: {faixa} (regra local: {motivo})"
                peso = faixa
        disponiveis = await self._disponiveis()
        modelo = escolher_modelo(peso, disponiveis, self._prefs_efetivas(), indisponiveis=self._indisponiveis_ativos())
        if modelo is None:
            return Escolha("default", peso, "nenhum modelo da faixa disponivel")
        return Escolha(modelo, peso, motivo)

    async def escolher_faixa(self, peso: str) -> Escolha:
        """Escolhe modelo para uma faixa especifica sem aplicar modelo forcado."""
        faixa = peso if peso in ORDEM else 'medio'
        if not self.ativo:
            return Escolha('default', faixa, 'maestro desligado')
        disponiveis = await self._disponiveis()
        modelo = escolher_modelo(
            faixa,
            disponiveis,
            self._prefs_efetivas(),
            indisponiveis=self._indisponiveis_ativos(),
        )
        if modelo is None:
            return Escolha('default', faixa, 'nenhum modelo da faixa disponivel')
        return Escolha(modelo, faixa, f'faixa {faixa}')

    def escalar(self, escolha: Escolha) -> Escolha | None:
        """Sobe uma faixa de peso, se possivel."""
        if escolha.forcada or escolha.peso == 'pesado':
            return None
        idx = ORDEM.index(escolha.peso)
        novo_peso = ORDEM[idx + 1]
        return Escolha(escolha.modelo, novo_peso, 'escalado', escolha.forcada)
