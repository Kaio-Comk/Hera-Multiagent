from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.telegram.facts_loader import get_support_facts, sem_acento
from app.telegram.guardrails import aplicar_guardrails
from app.telegram.notifier import enviar_telegram_kaio, limpar_estilo
from app.telegram.schemas import CustomerMessageRequest, CustomerMessageResponse
from app.telegram.second_brain import ObsidianSecondBrain

log = logging.getLogger("hera.telegram.support")

CUMPRIMENTO = {
    "pt": "Oi! Tudo bem? Como posso te ajudar?",
    "es": "¡Hola! ¿Cómo estás? ¿En qué te puedo ayudar?",
}
CONFIRMACAO = {
    "pt": "Combinado! Se precisar de algo, estou por aqui.",
    "es": "¡Listo! Qualquer dúvida, estou por aqui.",
}
AGRADECIMENTO = {
    "pt": "Por nada! Se precisar de qualquer coisa, só chamar por aqui.",
    "es": "¡De nada! Cualquier duda, estoy por aquí.",
}
INTENCAO_ASSINATURA = {
    "pt": (
        "O Plano Completo sai por R$ 9,90 por mês e dá acesso total pelo Telegram, site e apps de celular e TV. "
        "O pagamento é por PIX direto no bot @NetHub_Filme_Bot: é só abrir o bot, mandar /assinatura "
        "(ou tocar em Assinar no menu) que ele gera o QR Code e o código Pix copia e cola na hora."
    ),
    "es": (
        "La suscripción a NetHub te da acceso total por Telegram, sitio web y apps. "
        "El pago se realiza directamente en el bot @NetHub_Peliculas_Bot con tarjeta (vía Stripe) o con Telegram Stars: "
        "solo abre el bot y manda /suscripcion (o toca en Suscribirse en el menú) para ver los planes."
    ),
}
CONFIG_SERVIDOR = {
    "pt": (
        "A URL do provedor para preencher na TV ou no app é https://gw.nethubfilmes.org. "
        "Depois de digitar a URL, clique em Conectar Servidor para carregar o catálogo. "
        "Em seguida, basta entrar com o e-mail e a senha da sua conta Pro (criada com /conta no bot)."
    ),
    "es": (
        "La URL del proveedor para ingresar en la TV o en la app es https://gw.nethubfilmes.org. "
        "Después de ingresar la URL, pulsa en Conectar Servidor para cargar el catálogo. "
        "Luego, inicia sesión con el correo y la contraseña de tu cuenta Pro (creada con /conta en el bot)."
    ),
}
INFO_RECEBIDA = {
    "pt": "Recebi as informações! Já passei para conferência do time responsável e te respondo aqui.",
    "es": "¡Recibí la información! Ya se lo pasé al equipo responsable para revisarlo y te respondo por aquí.",
}
RECUPERACAO_SENHA = {
    "pt": (
        "O código de recuperação é enviado para o e-mail cadastrado na sua conta Pro. "
        "Dê uma olhada nas pastas de Spam, Promoções e Atualizações. "
        "Se ainda assim não chegar ou você não lembrar o e-mail cadastrado, mande /conta no bot @NetHub_Filme_Bot "
        "para conferir os dados da sua conta. Se precisar, me avise aqui que eu te ajudo."
    ),
    "es": (
        "El código de recuperación se envía al correo registrado en tu cuenta Pro. "
        "Revisa también las carpetas de Spam, Promociones y Notificaciones de tu correo. "
        "Si aún no llega o no recuerdas el correo registrado, envía /conta al bot @NetHub_Peliculas_Bot "
        "para verificar los datos de tu cuenta. Si necesitas ayuda, avísame por aquí."
    ),
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
ROKU_TV = {
    "pt": (
        "Na Roku TV e Roku Express você pode assistir pelo app Magic Player da loja da Roku. "
        "Basta ir em Add Playlist, colocar Nome NetHub e a URL M3U https://gw.nethubfilmes.org/get.php?username=[EMAIL]&password=[SENHA]&type=m3u_plus com o e-mail e senha da sua conta Pro. "
        "Também funcionam os apps Braziper TV e Assist via MAC, e lembramos que nosso catálogo tem filmes e séries sob demanda, sem canais de TV ao vivo."
    ),
    "es": (
        "En Roku TV y Roku Express puedes ver usando la app Magic Player de la tienda de Roku. "
        "Solo ve a Add Playlist, pon Nombre NetHub y la URL M3U https://gw.nethubfilmes.org/get.php?username=TU_CORREO&password=TU_CONTRASEÑA&type=m3u_plus con el correo y contraseña de tu cuenta Pro. "
        "También funcionan Braziper TV y Assist vía MAC, y recordamos que nuestro catálogo tiene películas y series bajo demanda, sin canales de TV en vivo."
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

# Respostas anti-repetição (quando a IA acabou de mandar exatamente o mesmo texto)
REPETICAO_FINANCEIRO = {
    "pt": "Já anotei aqui e chamei o Kaio para te atender diretamente por aqui mesmo. Fica tranquilo que ele já vai te responder.",
    "es": "Ya lo anoté y avisé a Kaio para que te atienda directamente por aquí. No te preocupes, en breve te responde.",
}
REPETICAO_ASSINATURA = {
    "pt": (
        "Como te falei, o Pix é gerado automaticamente pelo bot @NetHub_Filme_Bot ao mandar /assinatura. "
        "Se você estiver com qualquer dificuldade para gerar por lá, me avisa que o Kaio já vem te ajudar por aqui."
    ),
    "es": (
        "Como te comenté, la suscripción se realiza directamente en el bot @NetHub_Peliculas_Bot con /suscripcion. "
        "Si tienes cualquier dificultad, avísame que Kaio te ayuda enseguida por aquí."
    ),
}
REPETICAO_CONFIG_OU_PRESO = {
    "pt": "Se você já tentou esses passos e ainda não deu certo, me explica o que apareceu na tela que eu aviso o Kaio para te ajudar.",
    "es": "Si ya intentaste estos pasos y no funcionó, cuéntame qué te aparece en pantalla y le aviso a Kaio para que te ayude.",
}
REPETICAO_NAO_SEI = {
    "pt": "Já passei sua dúvida diretamente para o Kaio e ele vai te responder assim que ver. Obrigado pela paciência!",
    "es": "Ya le pasé tu consulta directamente a Kaio y te responderá en cuanto lo vea. ¡Gracias por la paciencia!",
}
REPETICAO_GERAL = {
    "pt": "Já anotei o que você precisa e avisei o Kaio para te atender por aqui em instantes.",
    "es": "Ya anoté lo que necesitas y le avisé a Kaio para que te atienda por aquí en breve.",
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
_APENAS_EMAIL_RE = re.compile(
    r"^(?:(?:meu\s+)?e?mail(?:\s+[eé])?\s*[:=]?\s*)?[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)
_CONFIG_SERVIDOR_RE = re.compile(
    r"(?:\bprovedor\b|"
    r"\burl\s+do\s+servidor\b|"
    r"\bservidor\b.*\b(?:tv|app|aplicativo|colocar|preencher|conectar)\b|"
    r"\b(?:tv|app|aplicativo|colocar|preencher)\b.*\bservidor\b|"
    r"\bconectar\s+servidor\b|"
    r"\bconfigura[cç][aã]o\s+de\s+gateway\b|"
    r"\bpedindo\s+url\b|"
    r"\bpedindo\s+servidor\b|"
    r"\bqual\s+url\b|"
    r"\bqual\s+[eé]\s+a\s+url\b|"
    r"\bqual\s+o\s+servidor\b|"
    r"\bqual\s+servidor\b|"
    r"\bproveedor\b|"
    r"\burl\s+del\s+servidor\b|"
    r"\bpidiendo\s+url\b|"
    r"\bpidiendo\s+servidor\b)",
    re.IGNORECASE,
)
_PROBLEMA_PAGAMENTO_RE = re.compile(
    r"(?:\b(?:j[aá]\s+)?(?:fiz|fizemos|mandei|mandamos|enviei|efetuei|transferi|paguei|pagamos|cobrou|debitou)\b.*"
    r"\b(?:pix|pagamento|valor|grana|dinheiro|mensalidade|assinatura|taxa|cartao|cart[aã]o|pago)\b|"
    r"\b(?:pix|pagamento|valor|cartao|cart[aã]o)\b.*\b(?:j[aá]\s+)?(?:fiz|fizemos|mandei|mandamos|enviei|efetuei|transferi|paguei|pagamos|cobrou|debitou)\b|"
    r"\b(?:j[aá]\s+)?paguei\b|\b(?:j[aá]|est[aá]|t[aá]|foi)\s+pago\b|\b(?:j[aá]\s+)?fiz\s+(?:o\s+)?pix\b|\b(?:j[aá]\s+)?mandei\s+(?:o\s+)?pix\b|"
    r"\b(?:n[aã]o|nao)\s+(?:ativou|liberou|caiu|entrou|compensou|atualizou)\b|"
    r"\bcomprovante\b|\bprint\s+do\s+pix\b|\bprint\s+do\s+pagamento\b|"
    r"\breembolso\b|\bestorno\b|\bdevolver\s+dinheiro\b|\bdevolu[cç][aã]o\b|\bcancelar\s+(?:a\s+)?assinatura\b|"
    r"\bcancelar\s+(?:o\s+)?plano\b|\bcancelamento\b|"
    r"\bcobran[cç]a\s+indevida\b|\bcobrou\s+duas\s+vezes\b|\bcobrou\s+errado\b|"
    r"\bbloqueio\b|\bbloquead[oa]\b|\bconta\s+suspensa\b|\bbanid[oa]\b|"
    r"\bgolpe\b|\bprocon\b|\badvogad[oa]\b|\bprocesso\b|\bden[uú]ncia\b|"
    # ES
    r"\b(?:ya\s+)?(?:pagu[eé]|pagamos|hice|transfer[ií]|mand[eé]|envi[eé])\b.*"
    r"\b(?:pago|tarjeta|dinero|suscripci[oó]n|transferencia)\b|"
    r"\b(?:ya\s+)?pagu[eé]\b|\b(?:ya\s+)?hice\s+el\s+pago\b|"
    r"\bno\s+(?:se\s+)?(?:activ[oó]|acredito|reflej[oó]|desbloque[oó])\b|"
    r"\bcomprobante\b|\bcaptura\s+del\s+pago\b|\breembolso\b|\bdevoluci[oó]n\b|"
    r"\bcancelar\s+(?:la\s+)?suscripci[oó]n\b|\bcancelar\s+plan\b|\bestafa\b|\bdenuncia\b)",
    re.IGNORECASE,
)
_INTENCAO_COMPRA_RE = re.compile(
    r"(?:\b(?:me\s+)?(?:passa|manda|envia|informa)\s+(?:o|seu|a|chave)?\s*pix\b|"
    r"\bqual\s+(?:[eé]\s+)?(?:o|a\s+chave|seu)?\s*pix\b|"
    r"\bchave\s+pix\b|"
    r"\b(?:como|onde|qual\s+lugar)\s+(?:eu\s+)?(?:fa[cç]o\s+(?:pra|para|p/|p)\s+|consigo\s+)?(?:pagar|pago|fazer\s+o\s+pix|assinar|comprar)\b|"
    r"\bcomo\s+(?:se\s+)?assina\b|\bcomo\s+(?:assinar|assino)\b|\bcomo\s+(?:pagar|pago)\b|\bonde\s+(?:pagar|pago|assinar|assino)\b|"
    r"\bquero\s+(?:pagar|assinar|comprar|o\s+plano|renovar|contratar|adquirir|ter\s+acesso)\b|"
    r"\bquanto\s+(?:tenho\s+(?:que|q|de)|devo|preciso|custa|sai|fica)\s+(?:a\s+)?(?:pagar|p/|pra|para)?\b|"
    r"\bquanto\s+(?:custa|sai|fica|[eé])\s*(?:o\s+plano|a\s+assinatura|o\s+acesso|o\s+nethub|por\s+m[eê]s)?\b|"
    r"\bqual\s+(?:[eé]\s+)?(?:o\s+)?(?:valor|pre[cç]o)\s*(?:do\s+plano|da\s+assinatura|mensal)?\b|"
    r"\baceita\s+(?:pix|cartao|cart[aã]o)\b|\bformas?\s+de\s+pagamento\b|"
    r"\bquais\s+(?:s[aã]o\s+os|os)\s+planos\b|\bqual\s+plano\b|"
    r"\b(?:para|pra|p)\s+(?:eu\s+)?te\s+pagar\b|"
    # ES
    r"\bc[oó]mo\s+(?:puedo\s+|hago\s+para\s+)?(?:pagar|pago|suscribirme|contratar)\b|"
    r"\bd[oó]nde\s+(?:puedo\s+)?(?:pagar|pago)\b|"
    r"\bquiero\s+(?:pagar|suscribirme|el\s+plan|renovar|contratar)\b|"
    r"\bcu[aá]nto\s+(?:cuesta|tengo\s+que\s+pagar|vale|sale)\b|"
    r"\bcu[aá]l\s+es\s+el\s+precio\b|\bformas\s+de\s+pago\b|\baceptan\s+tarjeta\b)",
    re.IGNORECASE,
)
_RECUPERACAO_SENHA_RE = re.compile(
    r"(?:\b(?:c[oó]digo|senha|link)\b.*\b(?:n[aã]o|nao)\s+(?:chega|chegou|recebi|veio|caiu)\b|"
    r"\b(?:n[aã]o|nao)\s+(?:chega|chegou|recebi|veio|caiu)\b.*\b(?:c[oó]digo|senha|link)\b|"
    r"\besqueci\s+(?:a\s+)?senha\b|\bperdi\s+(?:a\s+)?senha\b|\btrocar\s+(?:a\s+)?senha\b|"
    r"\bno\s+llega\s+(?:el\s+)?c[oó]digo\b|\bolvid[eé]\s+(?:la\s+)?contrase[nñ]a\b)",
    re.IGNORECASE,
)
_PALAVRA_CONTEUDO_RE = re.compile(
    r"\b(?:series?|filmes?|temporadas?|episodios?|novelas?|animes?|desenhos?|documentarios?|catalogos?|peliculas?|capitulos?)\b"
)
_EXPRESSAO_DISPONIBILIDADE_RE = re.compile(
    r"(?:\bnao tem\b|\btem no\b|\btem a\b|\btem o\b|\bvai ter\b|\bvao ter\b|\bvai vir\b|\bvir em\b|\bchance de\b"
    r"|\bvao colocar\b|\bvai colocar\b|\bpode colocar\b|\bpodem colocar\b|\bpode adicionar\b|\bpodem adicionar\b"
    r"|\btem como colocar\b|\btem como adicionar\b|\bquando vai\b|\bquando vao\b|\bnao encontrei\b|\bnao achei\b"
    r"|\bvai liberar\b|\bsera liberado\b|\bquando libera\b|\bvai sair\b|\bquando sai\b|\bvai lancar\b|\bvai estrear\b"
    r"|\bsinto falta\b|\bsentimos falta\b|\bno tienen\b|\bno esta en\b|\bvan a agregar\b|\bpueden agregar\b|\bcuando va a\b)"
)
_APP_RE = re.compile(r"\b(?:app|aplicativo|aplicacion)\b")
_APP_TRAVADO_RE = re.compile(
    r"(?:\bnao esta indo\b|\bnao ta indo\b|\bnao vai\b|\bnao abre\b|\bnao carrega\b|\btravad[oa]\b"
    r"|\bpres[oa]\b|\bcarregando\b|\bno abre\b|\bno carga\b|\bcargando\b)"
)
_ROKU_RE = re.compile(r"\broku\b", re.IGNORECASE)


def obter_ultima_resposta_assistente(historico: Optional[list]) -> Optional[str]:
    """Retorna o texto da última resposta enviada pelo assistente/IA no histórico."""
    if not historico:
        return None
    for item in reversed(historico):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            papel = str(item[0]).lower()
            if papel in ("nethub", "ia", "assistant", "bot"):
                return str(item[1]).strip()
        elif isinstance(item, dict):
            papel = str(item.get("role") or item.get("papel") or "").lower()
            if papel in ("nethub", "ia", "assistant", "bot"):
                conteudo = item.get("content") or item.get("texto") or ""
                return str(conteudo).strip()
    return None


def fatos_da_conta(contexto: dict, idioma: str) -> list[str]:
    """Resumo da conta (vindo do NetHub, sem dado pessoal) em fatos para o modelo. Só o que o NetHub conferiu no banco."""
    if not contexto:
        return []
    es = idioma == "es"
    fatos = []
    plano = str(contexto.get("plano") or "").strip()
    ate = str(contexto.get("valido_ate") or "").strip()
    if plano:
        if contexto.get("ativo") and ate:
            fatos.append(
                f"Este cliente tiene el plan {plano} activo hasta el {ate}."
                if es
                else f"Este cliente tem o plano {plano} ativo até {ate}."
            )
        elif ate:
            fatos.append(
                f"El plan de este cliente venció el {ate}."
                if es
                else f"O plano deste cliente venceu em {ate}."
            )
    if "tem_conta_site" in contexto:
        if contexto.get("tem_conta_site"):
            fatos.append(
                "Este cliente ya tiene la cuenta con correo y contraseña."
                if es
                else "Este cliente já tem a conta do site e dos apps (e-mail e senha)."
            )
        else:
            fatos.append(
                "Este cliente todavía no creó la cuenta con correo y contraseña."
                if es
                else "Este cliente ainda não criou a conta do site e dos apps (e-mail e senha)."
            )
    for p in (contexto.get("pedidos") or [])[:5]:
        titulo, status = str(p.get("titulo") or "").strip(), str(p.get("status") or "").strip()
        if not titulo:
            continue
        if status == "adicionado":
            fatos.append(
                f"El pedido de este cliente \"{titulo}\" ya está en el catálogo."
                if es
                else f"O pedido deste cliente \"{titulo}\" já está no catálogo."
            )
        elif status == "pendente":
            fatos.append(
                f"El pedido de este cliente \"{titulo}\" está registrado y todavía no entró; no hay plazo."
                if es
                else f"O pedido deste cliente \"{titulo}\" está registrado e ainda não entrou; não há prazo."
            )
    return fatos


class CustomerSupportService:
    def __init__(
        self,
        autofix_service=None,
        llm_caller=None,
        second_brain: Optional[ObsidianSecondBrain] = None,
    ):
        self.autofix_service = autofix_service
        self.llm_caller = llm_caller
        self.second_brain = second_brain or ObsidianSecondBrain()

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

        # Cliente enviou apenas endereço de e-mail (geralmente resposta a pedido de conta)
        if _APENAS_EMAIL_RE.match(texto.strip()):
            return "pagamento", INFO_RECEBIDA[idioma]

        # Configuração de gateway / provedor de mídia na TV ou app
        if _CONFIG_SERVIDOR_RE.search(t_norm) and not _PROBLEMA_PAGAMENTO_RE.search(t_norm):
            return "duvida", CONFIG_SERVIDOR[idioma]

        # Reclamação real de pagamento / já pago / comprovante / estorno / bloqueio
        if _PROBLEMA_PAGAMENTO_RE.search(t_norm):
            return "pagamento", ESCALAR_FINANCEIRO[idioma]

        # Intenção de compra / assinar / pedir chave pix / quanto custa / como pagar
        if _INTENCAO_COMPRA_RE.search(t_norm):
            return "duvida", INTENCAO_ASSINATURA[idioma]

        # Recuperação de senha / código não chega no e-mail
        if _RECUPERACAO_SENHA_RE.search(t_norm):
            return "duvida", RECUPERACAO_SENHA[idioma]

        # Pedido de título
        if _PALAVRA_CONTEUDO_RE.search(t_norm) and _EXPRESSAO_DISPONIBILIDADE_RE.search(t_norm):
            return "duvida", PEDIDO_TITULO[idioma]

        # App preso
        if "carregando perfil" in t_norm or "cargando perfil" in t_norm or (_APP_RE.search(t_norm) and _APP_TRAVADO_RE.search(t_norm)):
            return "duvida", APP_PRESO[idioma]

        # Roku TV / Roku Express
        if _ROKU_RE.search(t_norm) and not any(sem_acento(termo) in t_norm for termo in facts.bug):
            return "duvida", ROKU_TV[idioma]

        # Bug report
        if any(sem_acento(termo) in t_norm for termo in facts.bug):
            return "bug", BUG_RECONHECIDO[idioma]

        # Trava financeira / escala genérica para termos não capturados acima
        # (evita falsos positivos com palavras genéricas como "pix" isolado que já foram tratadas)
        termos_escalar_especificos = [
            item for item in facts.escalar
            if sem_acento(item).strip() not in ("pix", "pagamento", "cartao", "tarjeta", "pago")
        ]
        if any(sem_acento(item) in t_norm for item in termos_escalar_especificos):
            return "pagamento", ESCALAR_FINANCEIRO[idioma]

        return None, None

    async def responder_duvida_com_ia(
        self,
        texto: str,
        idioma: str,
        facts,
        historico: list,
        contexto: Optional[dict] = None,
    ) -> str:
        fatos_lista = facts.fatos.get(idioma) or facts.fatos.get("pt") or []
        fatos_lista = list(fatos_lista) + fatos_da_conta(contexto or {}, idioma)
        fatos_formatados = "\n".join(f"- {f}" for f in fatos_lista)

        # 🧠 SEGUNDO CÉREBRO: Consulta o cofre completo do Obsidian (NetHub, ADRs, Runbooks)
        contexto_obsidian = ""
        if self.second_brain:
            try:
                contexto_obsidian = self.second_brain.formatar_contexto_para_prompt(texto, max_notas=4)
            except Exception as exc_sb:
                log.warning("[customer_support] Erro ao consultar Segundo Cérebro: %s", exc_sb)

        prompt_sistema = (
            "Você é o Kaio, criador, engenheiro de software sênior e suporte oficial do NetHub no Telegram.\n"
            "Você tem o mesmo poder analítico, conhecimento arquitetural e capacidade de diagnosticar e resolver erros que o Antigravity.\n"
            "Você possui acesso ao Segundo Cérebro completo (Obsidian Vault) com todas as decisões, runbooks, arquitetura e regras de operação do NetHub.\n\n"
            "DIRETRIZES DE ATENDIMENTO:\n"
            "1. LIBERDADE CONVERSACIONAL TOTAL: Ajude o cliente com clareza, empatia, paciência e conhecimento técnico aprofundado.\n"
            "2. Estilo de escrita do Kaio: Informal, prestativo, direto e sem rodeios. NUNCA use emojis. NUNCA use travessão nem hífen solto (use vírgula ou ponto).\n"
            "3. Você é o Kaio atendendo um cliente: o cliente NUNCA se chama Kaio. Nunca chame o cliente de Kaio.\n"
            "4. Diagnóstico e Resolução Técnica: Se o cliente relatar dificuldades técnicas com apps (Android, TV, Roku, Magic Player, Braziper), player web, áudio/legendas ou bot, forneça a instrução técnica exata baseada nos fatos e no Segundo Cérebro.\n"
            "5. Títulos fora do catálogo e lançamentos de cinema: Quando o cliente perguntar se um filme que ainda não está no catálogo vai entrar ou quando sai (como lançamentos recentes do cinema), explique com clareza. Informe que o filme entra no NetHub assim que sair em qualidade digital/streaming oficial. Oriente o cliente a fazer o pedido direto pelo bot em Perfil > Pedidos > Fazer um pedido para que ele seja avisado automaticamente assim que entrar!\n"
            "6. NUNCA responda genericamente 'Vou verificar e já te respondo' quando puder dar uma resposta útil, direta e resolutiva ao cliente.\n"
            f"7. Idioma da resposta: {'Português do Brasil' if idioma == 'pt' else 'Espanhol neutro'}.\n\n"
            "GUARDRAILS INVIOLÁVEIS DE SEGURANÇA (P0):\n"
            "- REGRA 1 (ANTI-VIP GRÁTIS): Você NUNCA pode conceder, prometer, ativar ou estender plano gratuito, VIP grátis ou dias extras a nenhum cliente. Não existe liberação manual de VIP pelo suporte. Se o cliente pedir VIP grátis ou dias extras, oriente com educação a assinar via /assinatura no bot oficial @NetHub_Filme_Bot.\n"
            "- REGRA 2 (ANTI-APAGAR MENSAGENS): Você NUNCA apaga nem sugere apagar mensagens do Telegram. Todo o histórico de mensagens é preservado e imutável.\n"
            "- REGRA 3 (ANTI-VAZAMENTO DE SEGREDOS): Você NUNCA deve falar ou revelar tokens de API (Telegram, Supabase, Stripe, Hera), senhas de banco de dados, chaves de ambiente (.env), dados pessoais de outros clientes ou credenciais do servidor.\n"
            "- REGRA 4 (SEGURANÇA DO NETHUB): NUNCA sugira ou execute comandos destrutivos ao sistema ou ao banco de dados.\n\n"
            f"FATOS DISPONÍVEIS:\n{fatos_formatados}\n\n"
            f"{contexto_obsidian}\n"
        )

        mensagens = [{"role": "system", "content": prompt_sistema}]
        # Incluir historico recente se houver
        if historico:
            for item in historico[-4:]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    papel = "assistant" if item[0] in ("nethub", "ia", "assistant", "bot") else "user"
                    conteudo_hist = str(item[1])
                    if papel == "assistant" and any(p in conteudo_hist for p in ("Vou verificar e já te respondo", "Já passei sua dúvida")):
                        continue
                    mensagens.append({"role": papel, "content": conteudo_hist})
                elif isinstance(item, dict):
                    papel = "assistant" if item.get("role") in ("nethub", "ia", "assistant", "bot") else "user"
                    conteudo_hist = str(item.get("content", ""))
                    if papel == "assistant" and any(p in conteudo_hist for p in ("Vou verificar e já te respondo", "Já passei sua dúvida")):
                        continue
                    mensagens.append({"role": papel, "content": conteudo_hist})

        mensagens.append({"role": "user", "content": texto})

        if self.llm_caller:
            try:
                resposta_ia = await self.llm_caller(mensagens)
                resposta_limpa = limpar_estilo(resposta_ia)
                if resposta_limpa:
                    # Aplicação estrita de guardrails determinísticos
                    resposta_protegida = aplicar_guardrails(resposta_limpa, idioma)
                    return resposta_protegida
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

        # Se não casou intenção rápida, tenta LLM / fatos
        if not motivo:
            resposta_ia = await self.responder_duvida_com_ia(
                req.texto, idioma, facts, req.historico or [], contexto=req.contexto
            )
            nao_sei = resposta_ia.strip() == limpar_estilo(NAO_SEI[idioma]).strip()
            motivo = "nao_sei" if nao_sei else "duvida"
            resposta_candidata = resposta_ia
        else:
            resposta_candidata = resposta_rapida or ""

        resposta_limpa = aplicar_guardrails(limpar_estilo(resposta_candidata), idioma)

        # ── ANTI-REPETIÇÃO / ANTI-LOOPING ──────────────────────────────────
        # Se a IA já enviou exatamente a mesma resposta na interação imediatamente anterior,
        # não repete como papagaio; oferece variação empática e alerta o Kaio.
        ult_resp = obter_ultima_resposta_assistente(req.historico)
        if ult_resp and sem_acento(resposta_limpa) == sem_acento(ult_resp):
            log.info(
                "[customer_support] Resposta repetida detectada para chat %s. Aplicando anti-loop.",
                req.chat_id,
            )
            if motivo == "pagamento":
                resposta_limpa = limpar_estilo(REPETICAO_FINANCEIRO[idioma])
                enviar_telegram_kaio(
                    f"ATENÇÃO: Cliente {req.nome or req.username or req.chat_id} (id: {req.chat_id}) insistindo em assunto financeiro / comprovante.\n"
                    f"Mensagem: \"{req.texto}\"\n"
                    f"Resposta adaptada enviada: \"{resposta_limpa}\"",
                    edicao=edicao,
                    titulo="FINANCEIRO REPETIDO - ATENÇÃO HUMANA",
                )
            elif "assinatura" in sem_acento(resposta_limpa) or "pix" in sem_acento(resposta_limpa):
                resposta_limpa = limpar_estilo(REPETICAO_ASSINATURA[idioma])
                enviar_telegram_kaio(
                    f"CLIENTE COM DÚVIDA REPETIDA DE ASSINATURA/PIX:\n"
                    f"Cliente {req.nome or req.username or req.chat_id} (id: {req.chat_id})\n"
                    f"Mensagem: \"{req.texto}\"\n"
                    f"Pode estar com dificuldade em usar o bot. Verifique.",
                    edicao=edicao,
                    titulo="DIFICULDADE ASSINATURA / PIX",
                )
            elif motivo == "nao_sei":
                resposta_limpa = limpar_estilo(REPETICAO_NAO_SEI[idioma])
                enviar_telegram_kaio(
                    f"CLIENTE AGUARDANDO RESPOSTA HUMANA (REPETIDA):\n"
                    f"Cliente: {req.nome or req.username or req.chat_id} (id: {req.chat_id})\n"
                    f"Mensagem: \"{req.texto}\"",
                    edicao=edicao,
                    titulo="CLIENTE AGUARDANDO RETORNO",
                )
            else:
                resposta_limpa = limpar_estilo(REPETICAO_GERAL[idioma])
                motivo = "nao_sei"
                enviar_telegram_kaio(
                    f"CLIENTE REPETINDO DÚVIDA / INSISTINDO:\n"
                    f"Cliente: {req.nome or req.username or req.chat_id} (id: {req.chat_id})\n"
                    f"Mensagem: \"{req.texto}\"",
                    edicao=edicao,
                    titulo="CLIENTE REPETINDO MENSAGEM",
                )

        # ── ROTEAMENTO DOS CASOS E NOTIFICAÇÕES ─────────────────────────────
        if motivo in ("cumprimento", "fechamento"):
            return CustomerMessageResponse(
                status="answered",
                resposta_cliente=resposta_limpa,
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
                resposta_cliente=resposta_limpa,
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
                resposta_cliente=resposta_limpa or limpar_estilo(BUG_RECONHECIDO[idioma]),
                motivo="bug",
                task_id=task_id,
                detalhes="Auto-fix autônomo iniciado para diagnosticar e reparar o bug relatado.",
            )

        if motivo == "nao_sei":
            enviar_telegram_kaio(
                f"DÚVIDA NÃO COBERTA PELA IA:\n"
                f"Cliente: {req.nome or req.username or req.chat_id} (id: {req.chat_id})\n"
                f"Edição: NetHub {edicao.upper()}\n"
                f"Mensagem: \"{req.texto}\"\n"
                f"Resposta enviada: \"{resposta_limpa}\"\n"
                f"Ação: Kaio precisa responder o cliente no Telegram.",
                edicao=edicao,
                titulo="DÚVIDA NÃO COBERTA - KAIO",
            )
            return CustomerMessageResponse(
                status="escalated",
                resposta_cliente=resposta_limpa,
                motivo="nao_sei",
                detalhes="a IA não achou a resposta nos fatos nem na conta",
            )

        # Caso geral de dúvida respondida
        return CustomerMessageResponse(
            status="answered",
            resposta_cliente=resposta_limpa,
            motivo="duvida",
        )
