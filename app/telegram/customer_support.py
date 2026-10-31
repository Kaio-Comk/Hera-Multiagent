from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Sequence

from app.telegram.facts_loader import get_support_facts, sem_acento
from app.telegram.notifier import enviar_telegram_kaio, limpar_estilo
from app.telegram.schemas import CustomerMessageRequest, CustomerMessageResponse

log = logging.getLogger("hera.telegram.support")

CUMPRIMENTO = {
    "pt": "Oi! Tudo bem? Como posso te ajudar?",
    "es": "¡Hola! ¿Cómo estás? ¿En qué te puedo ayudar?",
}
CONFIRMACAO = {
    "pt": "Combinado! Se precisar de algo, estou por aqui.",
    "es": "¡Listo! Cualquier duda, estoy por aquí.",
}
AGRADECIMENTO = {
    "pt": "Por nada! Se precisar de qualquer coisa, só chamar por aqui.",
    "es": "¡De nada! Cualquier duda, estoy por aquí.",
}
PEDIDO_TITULO = {
    "pt": (
        "Eu não consigo ver o catálogo daqui, então não sei te dizer se esse título vai entrar nem quando. "
        "Se não aparecer na busca do bot, é só pedir em Perfil / Pedidos e depois Fazer um pedido, que o pedido chega pra mim. "
        "Não consigo prometer prazo."
    ),
    "es": (
        "No puedo ver el catálogo desde aquí, así que no puedo decirte si ese título va a entrar ni cuándo. "
        "Si no aparece en la búsqueda del bot, solo tienes que pedirlo en Perfil / Pedidos y luego en Hacer un pedido, "
        "y el pedido me llega. No puedo prometer plazos."
    ),
}
APP_PRESO = {
    "pt": (
        "Isso acontece quando o login do app vence, mas o seu plano continua valendo. "
        "Para resolver sem perder os downloads, baixe o app novo em https://gw.nethubfilmes.org/app/mobile.apk "
        "e instale por cima do atual, sem desinstalar. Depois abra o app e entre de novo com o e-mail e a senha da sua conta Pro. "
        "Se ainda não criou a conta, mande /conta no bot. Se continuar igual, me avise aqui que eu vejo."
    ),
    "es": (
        "Esto pasa cuando el inicio de sesión de la app caduca, pero tu plan sigue activo. "
        "Para resolverlo sin perder las descargas, baja la app nueva en https://gw.nethubfilmes.org/app/mobile.apk "
        "e instálala encima de la actual, sin desinstalar. Luego abre la app e inicia sesión de nuevo con el correo "
        "y la contraseña de tu cuenta Pro. Si aún no creaste la cuenta, envía /conta al bot. Si sigue igual, avísame por aquí."
    ),
}
ESCALAR_FINANCEIRO = {
    "pt": "Oi! Vou conferir isso aqui pra você. Me manda o comprovante ou um print, por favor? Já te respondo.",
    "es": "¡Hola! Voy a revisarlo ahora mismo. ¿Me envías el comprobante o una captura, por favor? Te respondo enseguida.",
}
BUG_RECONHECIDO = {
    "pt": (
        "Recebi o seu relato! Já iniciei a investigação e correção automática do problema pelo nosso sistema Hera. "
        "Assim que a correção for concluída e testada, te aviso aqui imediatamente."
    ),
    "es": (
        "¡Recibí tu reporte! Ya inicié la investigación y corrección automática del problema a través de nuestro sistema Hera. "
        "En cuanto la corrección esté lista y probada, te aviso por aquí de inmediato."
    ),
}
NAO_SEI = {
    "pt": "Vou verificar e já te respondo.",
    "es": "Voy a verificarlo y te respondo enseguida.",
}

# Regex patterns
_CUMPRIMENTO_RE = re.compile(
    r"^(?:\W|oi+e?|ola|bom dia|boa tarde|boa noite|tudo bem|td bem|tudo bom|e ai|eai|opa|salve|hola|buenas|buen dia|"
    r"buenos dias|buenas tardes|buenas noches|que tal|como estas|hello|hi)+$"
)
_FECHAMENTO_AGRADECIMENTO_RE = re.compile(
    r"^(?:\W|muito|demais|pela\s+ajuda|pela\s+atencao|por\s+tudo|de\s+coracao|amigo|irmao|ah|sim|ja|mesmo|kaio|"
    r"obrigad[ao]|obrigadao|brigad[ao]|obg|valeu|vlw|agradeco|gratidao|tmj|tamo\s+junto|"
    r"gracias|muchas\s+gracias|mil\s+gracias|te\s+agradezco)+$"
)
_FECHAMENTO_CONFIRMACAO_RE = re.compile(
    r"^(?:\W|ah|sim|ja|entao|por\s+enquanto|kaio|"
    r"ok|ta\s+ok|ta\s+bom|tudo\s+bem|tudo\s+bom|tudo\s+certo|td\s+bem|td\s+bom|td\s+certo|"
    r"blz|beleza|show(?:\s+de\s+bola)?|perfeito|combinado|certo|certinho|entendi|entendido|"
    r"compreendi|tranquilo|fechou|maravilha|"
    r"vale|dale|listo|perfecto|genial|buenisimo|joya|de\s+acuerdo)+$"
)
_PALAVRA_CONTEUDO_RE = re.compile(
    r"\b(?:series?|filmes?|temporadas?|episodios?|novelas?|animes?|desenhos?|documentarios?|catalogos?|peliculas?|capitulos?)\b"
)
_EXPRESSAO_DISPONIBILIDADE_RE = re.compile(
    r"(?:\bnao tem\b|\btem no\b|\btem a\b|\btem o\b|\bvai ter\b|\bvao ter\b|\bvai vir\b|\bvir em\b|\bchance de\b"
    r"|\bvao colocar\b|\bvai colocar\b|\bpode colocar\b|\bpodem colocar\b|\bpode adicionar\b|\bpodem adicionar\b"
    r"|\btem como colocar\b|\btem como adicionar\b|\bquando vai\b|\bquando vao\b|\bnao encontrei\b|\bnao achei\b"
    r"|\bsinto falta\b|\bsentimos falta\b|\bno tienen\b|\bno esta en\b|\bvan a agregar\b|\bpueden agregar\b|\bcuando va a\b)"
)
_APP_RE = re.compile(r"\b(?:app|aplicativo|aplicacion)\b")
_APP_TRAVADO_RE = re.compile(
    r"(?:\bnao esta indo\b|\bnao ta indo\b|\bnao vai\b|\bnao abre\b|\bnao carrega\b|\btravad[oa]\b"
    r"|\bpres[oa]\b|\bcarregando\b|\bno abre\b|\bno carga\b|\bcargando\b)"
)


class CustomerSupportService:
    def __init__(self, autofix_service=None, llm_caller=None):
        self.autofix_service = autofix_service
        self.llm_caller = llm_caller

    def identificar_intencao_rapida(self, texto: str, idioma: str, facts) -> tuple[Optional[str], Optional[str]]:
        t_norm = sem_acento(texto).strip()
        if not t_norm:
            return None, None

        if _CUMPRIMENTO_RE.match(t_norm):
            return "cumprimento", CUMPRIMENTO[idioma]

        if "?" not in texto:
            if _FECHAMENTO_AGRADECIMENTO_RE.match(t_norm):
                return "fechamento", AGRADECIMENTO[idioma]
            if _FECHAMENTO_CONFIRMACAO_RE.match(t_norm):
                return "fechamento", CONFIRMACAO[idioma]

        # Trava financeira / escala
        if any(sem_acento(item) in t_norm for item in facts.escalar):
            return "pagamento", ESCALAR_FINANCEIRO[idioma]

        # Pedido de titulo
        if _PALAVRA_CONTEUDO_RE.search(t_norm) and _EXPRESSAO_DISPONIBILIDADE_RE.search(t_norm):
            return "duvida", PEDIDO_TITULO[idioma]

        # App preso
        if "carregando perfil" in t_norm or "cargando perfil" in t_norm or (_APP_RE.search(t_norm) and _APP_TRAVADO_RE.search(t_norm)):
            return "duvida", APP_PRESO[idioma]

        # Bug report
        if any(sem_acento(termo) in t_norm for termo in facts.bug):
            return "bug", BUG_RECONHECIDO[idioma]

        return None, None

    async def responder_duvida_com_ia(self, texto: str, idioma: str, facts, historico: list) -> str:
        fatos_lista = facts.fatos.get(idioma) or facts.fatos.get("pt") or []
        fatos_formatados = "\n".join(f"- {f}" for f in fatos_lista)

        prompt_sistema = (
            "Você é o Kaio, criador e atendente do NetHub no Telegram.\n"
            "Diretrizes obrigatórias:\n"
            "1. Responda em até no máximo 3 frases curtas e informais.\n"
            "2. NUNCA use emojis. NUNCA use travessão nem hífen solto (use vírgula ou ponto).\n"
            "3. Você é o Kaio atendendo um cliente: o cliente NUNCA se chama Kaio. Nunca chame o cliente de Kaio.\n"
            "4. Baseie-se ESTRITAMENTE nos fatos abaixo. Não invente preços, promoções ou prazos.\n"
            f"5. Idioma da resposta: {'Português do Brasil' if idioma == 'pt' else 'Espanhol neutro'}.\n"
            f"6. Se a dúvida não estiver coberta pelos fatos, responda exatamente: \"{NAO_SEI[idioma]}\"\n\n"
            f"FATOS DISPONÍVEIS:\n{fatos_formatados}\n"
        )

        mensagens = [{"role": "system", "content": prompt_sistema}]
        # Incluir historico recente se houver
        if historico:
            for item in historico[-4:]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    papel = "assistant" if item[0] in ("nethub", "ia", "assistant") else "user"
                    mensagens.append({"role": papel, "content": str(item[1])})
                elif isinstance(item, dict):
                    papel = "assistant" if item.get("role") in ("nethub", "ia", "assistant") else "user"
                    mensagens.append({"role": papel, "content": str(item.get("content", ""))})

        mensagens.append({"role": "user", "content": texto})

        if self.llm_caller:
            try:
                resposta_ia = await self.llm_caller(mensagens)
                resposta_limpa = limpar_estilo(resposta_ia)
                if resposta_limpa:
                    return resposta_limpa
            except Exception as exc:
                log.warning("[customer_support] Erro ao chamar LLM para dúvida: %s", exc)

        return NAO_SEI[idioma]

    async def processar_mensagem(self, req: CustomerMessageRequest) -> CustomerMessageResponse:
        edicao = (req.edicao or "br").lower()
        idioma = "es" if edicao == "es" else "pt"
        facts = get_support_facts()

        if req.anexo and not req.texto.strip():
            msg = (
                "Recebi! Já passei para o time responsável conferir e te respondo aqui."
                if idioma == "pt"
                else "¡Recibido! Ya se lo pasé al equipo responsable para revisarlo y te respondo por aquí."
            )
            enviar_telegram_kaio(
                f"Cliente {req.nome or req.username or req.chat_id} (id: {req.chat_id}) enviou anexo/foto sem texto.",
                edicao=edicao,
                titulo="ANEXO RECEBIDO",
            )
            return CustomerMessageResponse(
                status="answered",
                resposta_cliente=limpar_estilo(msg),
                motivo="anexo",
                detalhes="Anexo recebido sem texto",
            )

        # Checagem de intenção rápida
        motivo, resposta_rapida = self.identificar_intencao_rapida(req.texto, idioma, facts)

        if motivo == "cumprimento" or motivo == "fechamento":
            return CustomerMessageResponse(
                status="answered",
                resposta_cliente=limpar_estilo(resposta_rapida or ""),
                motivo=motivo,
            )

        if motivo == "pagamento":
            enviar_telegram_kaio(
                f"Cliente {req.nome or req.username or req.chat_id} (id: {req.chat_id}) mencionou assunto financeiro:\n"
                f"Mensagem: \"{req.texto}\"",
                edicao=edicao,
                titulo="ASSUNTO FINANCEIRO",
            )
            return CustomerMessageResponse(
                status="escalated",
                resposta_cliente=limpar_estilo(resposta_rapida or ""),
                motivo="pagamento",
                detalhes="Escalado para conferência de pagamento pelo Kaio",
            )

        if motivo == "bug":
            task_id = None
            if self.autofix_service:
                task_id = await self.autofix_service.agendar_autofix_cliente(
                    chat_id=req.chat_id,
                    nome=req.nome,
                    username=req.username,
                    edicao=edicao,
                    texto=req.texto,
                )

            enviar_telegram_kaio(
                f"BUG RELATADO POR CLIENTE:\n"
                f"Cliente: {req.nome or req.username or req.chat_id} (id: {req.chat_id})\n"
                f"Edição: NetHub {edicao.upper()}\n"
                f"Relato: \"{req.texto}\"\n"
                f"Ação do Hera: Auto-fix iniciado imediatamente (Task: {task_id or 'em processamento'}).",
                edicao=edicao,
                titulo="BUG RELATADO - AUTO-FIX INICIADO",
            )

            return CustomerMessageResponse(
                status="bug_investigating",
                resposta_cliente=limpar_estilo(resposta_rapida or BUG_RECONHECIDO[idioma]),
                motivo="bug",
                task_id=task_id,
                detalhes="Auto-fix autônomo iniciado para diagnosticar e reparar o bug relatado.",
            )

        if resposta_rapida and motivo == "duvida":
            return CustomerMessageResponse(
                status="answered",
                resposta_cliente=limpar_estilo(resposta_rapida),
                motivo="duvida",
            )

        # Caso geral: dúvida analisada com IA
        resposta_ia = await self.responder_duvida_com_ia(req.texto, idioma, facts, req.historico)
        return CustomerMessageResponse(
            status="answered",
            resposta_cliente=limpar_estilo(resposta_ia),
            motivo="duvida",
        )
