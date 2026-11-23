"""Parser inteligente de comprovantes bancários (Pix, boletos, transferências).

Extrai texto via RapidOCR (imagens) e pypdf (PDFs) e estrutura dados contábeis
utilizando os modelos de IA em nuvem do Hera.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from pypdf import PdfReader
from rapidocr_onnxruntime import RapidOCR

from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.tier_router import TierRouter

log = logging.getLogger("hera.finance.receipt_parser")

SYSTEM_FINANCE_PROMPT = """Você é o motor contábil e de conciliação financeira inteligente da Hera Assistente.
Sua função é analisar comprovantes bancários (Pix, boletos, faturas, extratos ou telas de app) e/ou o comentário do usuário e estruturar com máxima fidelidade os dados contábeis.

IDENTIFICAÇÃO DO TIPO DE MOVIMENTAÇÃO (campo 'tipo'):
- 'INCOME' (Receitas, ganhos de corrida 99/Uber, repasses, reembolsos, estornos, devoluções de planos cancelados ex: Claude, recebimentos Pix de clientes/terceiros, salário da esposa, vendas)
- 'EXPENSE' (Despesas, gastos, pagamentos de compras, refeições, combustíveis, contas de consumo, condomínio, assinaturas, compras online)
- 'TRANSFER' (Transferências entre contas bancárias próprias do usuário)

PLANO DE CONTAS OFICIAL (escolha a categoria mais aderente):
Categorias de RECEITA (quando tipo for INCOME):
- Salário Kaillany (salário, remuneração ou proventos da esposa Kaillany)
- Ganhos 99 e Uber (corridas de app, repasses da 99, repasses Uber, corridas particulares)
- Reembolso / Estorno (estorno de cartão, cancelamento de serviços/planos ex: Claude, devoluções de compras)
- Receita / Vendas InfinitePay (vendas comerciais, clientes InfinitePay)
- Recebimento Pix de Clientes / Terceiros (pagamentos de mensalidades NetHub, clientes de suporte, freelas)
- Cashback & Bônus (cashback Cuponomia, InfinitePay, bônus de banco)
- Entradas / Kaillany (outros repasses ou recebimentos da esposa Kaillany)
- Transferências / Entradas Pix (transferências recebidas em geral)
- Outros Recebimentos Pix

Categorias de DESPESA (quando tipo for EXPENSE):
- Alimentação & Mercado (restaurante, almoço, lanche, padaria, quentinha, açougue, ifood, sorvete, supermercado)
- Transporte & Moto (gasolina, combustível, manutenção da Titan 160, lava-jato, óleo, oficina, peças, seguro)
- Moradia & Contas Básicas (aluguel, condomínio, luz/Enel, água/SAAE, reparos da casa)
- Telefonia & Internet (Claro Flex, Sobral Net, recargas)
- Saúde & Farmácia (remédios, farmácia, médico, exames)
- I.A., Software & Ferramentas (assinaturas de IA, Claude, ChatGPT, Cursor, APIs, servidores)
- Repasse Kaillany (dinheiro enviado para a esposa Kaillany)
- Família & Filho (gastos com filho, esposa, mãe, Ana, roupas, presentes)
- Lazer & Passeios (passeios, cinema, praia, viagens)
- Compras & Mercado Livre (encomendas, produtos eletrônicos)
- Outros Gastos (qualquer despesa que não se encaixe nas anteriores)

CONTAS BANCÁRIAS DO USUÁRIO & REGRAS DE CONCILIAÇÃO:
Contas oficiais válidas:
- InfinitePay (CLOUDWALK IP LTDA / Cloudwalk — conta principal de movimentação)
- Nubank (NU PAGAMENTOS - IP / Nubank)
- Banco Inter (BANCO INTER S.A.)
- PicPay (PICPAY INSTITUICAO DE PAGAMENTO)
- Bradesco (Kaillany) (BANCO BRADESCO S.A.)
- Efí (MEI) (EFI S.A. / Gerencianet)
- 99Pay (onde caem repasses da 99)
- Carteira (Dinheiro)

REGRA DE IDENTIFICAÇÃO DO BANCO (campo 'banco'):
⚠️ ATENÇÃO MÁXIMA: O banco indicado deve ser a conta bancária DO USUÁRIO que foi movimentada, NUNCA o banco de terceiros!
1. Se for PAGAMENTO / DESPESA ('EXPENSE'):
   - O dinheiro SAIU da conta do usuário. A instituição afetada é a INSTITUIÇÃO DE ORIGEM!
   - Exemplo: Origem 'CLOUDWALK IP LTDA' e Destino 'NU PAGAMENTOS - IP' -> O banco do usuário é 'InfinitePay' (a Nu Pagamentos é apenas o banco do beneficiário que recebeu).
2. Se for RECEBIMENTO / RECEITA ('INCOME'):
   - O dinheiro ENTROU na conta do usuário. A instituição afetada é a INSTITUIÇÃO DE DESTINO!
   - Exemplo: Origem 'BCO BRADESCO' e Destino 'CLOUDWALK IP LTDA' -> O banco do usuário é 'InfinitePay'.

Responda APENAS um JSON válido (sem markdown adicional, sem texto extra) no formato:
{
  "tipo": "INCOME" ou "EXPENSE" ou "TRANSFER",
  "valor": 120.00,
  "data": "YYYY-MM-DD",
  "descricao": "Nome claro, conciso e natural da movimentação",
  "categoria": "Uma das categorias exatas listadas acima",
  "forma": "PIX",
  "banco": "Nome exato da conta bancária do usuário afetada",
  "beneficiario": "Nome do pagador (se receita) ou recebedor (se despesa) ou vazio",
  "observacao": "Comentário ou contexto fornecido pelo usuário"
}"""


class ReceiptParser:
    """Parser de comprovantes com OCR neural e interpretação semântica por IA."""

    def __init__(self):
        self._ocr = None

    @property
    def ocr(self) -> RapidOCR:
        if self._ocr is None:
            self._ocr = RapidOCR()
        return self._ocr

    def extract_text_from_image(self, image_path: Path) -> str:
        """Extrai todo o texto da imagem do comprovante via RapidOCR."""
        try:
            result, _ = self.ocr(str(image_path))
            if not result:
                return ""
            return "\n".join(r[1] for r in result if len(r) > 1 and r[1])
        except Exception as exc:
            log.exception("Erro ao executar OCR na imagem %s: %s", image_path, exc)
            return ""

    def extract_text_from_pdf(self, pdf_path: Path) -> str:
        """Extrai texto de comprovante PDF."""
        try:
            reader = PdfReader(str(pdf_path))
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
            return text.strip()
        except Exception as exc:
            log.exception("Erro ao ler PDF %s: %s", pdf_path, exc)
            return ""

    async def parse_receipt(
        self,
        raw_text: str,
        user_comment: str,
        router: TierRouter,
    ) -> Dict[str, Any]:
        """Interpreta os dados financeiros usando a IA com fallback defensivo."""
        user_prompt = f"Data atual de referência: {datetime.now().strftime('%Y-%m-%d')}\n"
        if user_comment:
            user_prompt += f"Comentário do usuário: {user_comment}\n"
        if raw_text:
            user_prompt += f"Texto extraído do comprovante:\n{raw_text}\n"

        req = ChatCompletionRequest(
            model="default",
            messages=[
                ChatMessage(role="system", content=SYSTEM_FINANCE_PROMPT),
                ChatMessage(role="user", content=user_prompt),
            ],
            temperature=0.1,
        )

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await router.route_and_execute(req, client)
                content = (resp.choices[0].message.content or "").strip()
                
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    # Normalizar valor para float
                    if "valor" in data:
                        if isinstance(data["valor"], str):
                            data["valor"] = float(re.sub(r"[^\d,\.]", "", data["valor"]).replace(",", "."))
                        else:
                            data["valor"] = float(data["valor"])
                    
                    # Normalizar tipo
                    raw_tipo = str(data.get("tipo") or "").upper().strip()
                    if raw_tipo in ("INCOME", "RECEITA", "ENTRADA", "GANHO"):
                        data["tipo"] = "INCOME"
                    elif raw_tipo in ("TRANSFER", "TRANSFERENCIA", "TRANSFERÊNCIA"):
                        data["tipo"] = "TRANSFER"
                    else:
                        data["tipo"] = "EXPENSE"
                    
                    return data
        except Exception as exc:
            log.warning("Falha na interpretação via IA, usando parser regex de contingência: %s", exc)

        return self._regex_fallback(raw_text, user_comment)

    def _regex_fallback(self, raw_text: str, user_comment: str) -> Dict[str, Any]:
        """Extração baseada em expressões regulares caso a IA esteja temporariamente indisponível."""
        combined = f"{user_comment} {raw_text}".strip()
        comment_lower = combined.lower()

        # 1. Extrair valor
        valor = 0.0
        val_match = re.search(r"R\$\s*([\d\.]+,\d{2})", combined, re.IGNORECASE)
        if val_match:
            valor = float(val_match.group(1).replace(".", "").replace(",", "."))
        else:
            val_match2 = re.search(r"(?:^|\s)(\d{1,4}(?:[,\.]\d{2})?)\s*(?:reais|real|\$)", combined, re.IGNORECASE)
            if val_match2:
                valor = float(val_match2.group(1).replace(",", "."))

        # 2. Detectar Tipo (INCOME vs EXPENSE)
        is_income = any(w in comment_lower for w in (
            "recebi", "ganhos", "ganhei", "reembolso", "estorno", "devolucao", "devolução",
            "pix recebido", "transferencia recebida", "transferência recebida", "entrada", "renda"
        ))

        # 3. Detectar Banco
        banco = "InfinitePay"
        if "99pay" in comment_lower or "99 pay" in comment_lower:
            banco = "99Pay"
        elif "inter" in comment_lower:
            banco = "Banco Inter"
        elif "nubank" in comment_lower or "nu pagamentos" in comment_lower:
            banco = "Nubank"
        elif "picpay" in comment_lower:
            banco = "PicPay"
        elif "bradesco" in comment_lower:
            banco = "Bradesco (Kaillany)"
        elif "efí" in comment_lower or "efi" in comment_lower:
            banco = "Efí (MEI)"

        # 4. Classificar Categoria e Descrição
        clean_comment = re.sub(
            r"(?:comprovante|pix|de|r\$|\d+|reais|paguei|comprei|gastei|recebi|mandei|esse|dinheiro|para|o)",
            "", user_comment, flags=re.IGNORECASE
        ).strip()

        if is_income:
            tipo = "INCOME"
            if any(w in comment_lower for w in ("salario", "salário", "esposa", "kaillany")):
                categoria = "Salário Kaillany"
                descricao = "Salário da esposa Kaillany"
            elif any(w in comment_lower for w in ("99", "uber", "corrida", "ganhos 99")):
                categoria = "Ganhos 99 e Uber"
                descricao = "Ganhos de corrida 99 / Uber"
            elif any(w in comment_lower for w in ("reembolso", "estorno", "claude", "cancelamento")):
                categoria = "Reembolso / Estorno"
                descricao = f"Reembolso {clean_comment}".strip() or "Reembolso de serviço"
            elif any(w in comment_lower for w in ("cliente", "nethub", "mensalidade")):
                categoria = "Recebimento Pix de Clientes / Terceiros"
                descricao = "Recebimento de Cliente"
            elif "cashback" in comment_lower:
                categoria = "Cashback & Bônus"
                descricao = "Cashback Recebido"
            else:
                categoria = "Outros Recebimentos Pix"
                descricao = clean_comment.capitalize() if clean_comment else "Entrada Pix"
        else:
            tipo = "EXPENSE"
            descricao = clean_comment.capitalize() if clean_comment else "Despesa Pix"
            categoria = "Outros Gastos"
            if any(w in comment_lower for w in ("sorvete", "almoço", "almoco", "lanche", "comida", "mercado", "padaria", "quentinha", "ifood", "açougue")):
                categoria = "Alimentação & Mercado"
            elif any(w in comment_lower for w in ("gasolina", "combustivel", "moto", "oleo", "oficina", "peças", "estacionamento")):
                categoria = "Transporte & Moto"
            elif any(w in comment_lower for w in ("farmacia", "farmácia", "remédio", "remedio", "medico", "saude")):
                categoria = "Saúde & Farmácia"
            elif any(w in comment_lower for w in ("luz", "agua", "água", "condominio", "aluguel", "enel", "saae")):
                categoria = "Moradia & Contas Básicas"
            elif any(w in comment_lower for w in ("internet", "celular", "recarga", "sobral net", "laricell", "claro")):
                categoria = "Telefonia & Internet"
            elif any(w in comment_lower for w in ("claude", "chatgpt", "openai", "cursor", "software", "api", "servidor")):
                categoria = "I.A., Software & Ferramentas"

        return {
            "tipo": tipo,
            "valor": valor,
            "data": datetime.now().strftime("%Y-%m-%d"),
            "descricao": descricao,
            "categoria": categoria,
            "forma": "PIX",
            "banco": banco,
            "beneficiario": "",
            "observacao": user_comment or "Lançamento via Telegram",
        }

    @staticmethod
    def is_financial_intent(text: str) -> bool:
        """Identifica se uma mensagem de texto simples é uma declaração financeira (gasto ou receita)."""
        t = text.lower()
        actions = (
            "comprei", "gastei", "paguei", "pix", "comprovante", "despesa", "conta",
            "recebi", "ganhei", "ganhos", "reembolso", "estorno", "devolução", "devolucao",
            "salário", "salario", "corrida", "faturamento", "renda", "entrada"
        )
        has_action = any(w in t for w in actions)
        has_money = bool(re.search(r"(?:r\$|\$|\breais\b|\breal\b|\b\d+[\.,]\d{2}\b|\b\d+\s*reais\b)", t))
        return has_action and has_money
