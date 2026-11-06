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

SYSTEM_FINANCE_PROMPT = """Você é o motor contábil inteligente da Hera Assistente.
Sua função é analisar o texto extraído de um comprovante bancário (Pix, boleto, cartão ou transferência)
junto com o comentário do usuário (ex: 'comprei um sorvete', 'almoço', 'farmácia') e extrair com exatidão os dados da despesa.

Categorias permitidas no plano de contas:
- Alimentação & Mercado (mercado, padaria, restaurante, lanche, ifood, sorvete, quentinha, açougue)
- Transporte & Moto (gasolina, moto, seguro moto, uber, 99, oficina, peças, multa, estacionamento)
- Moradia & Casa (aluguel, condomínio, luz/enel, água/saae, casa, lavanderia, reformas)
- Telefonia & Internet (sobral net, laricell, plano celular, recarga, vivo)
- Saúde & Cuidados (farmácia, médico, exames, remédios, ótica, dentista)
- Lazer & Passeios (passeios, cinema, praia, lazer, viagens)
- Cartão & Dívidas (fatura de cartão, empréstimo, juros, quitação)
- Despesas Pessoais / Família (filho, esposa, mãe, Ana, roupas, presentes)
- Outros / Diversos (qualquer item que não se encaixe nas anteriores)

Bancos comuns do usuário: InfinitePay, Banco Inter, Nubank, Bradesco, PicPay, 99Pay, Mercado Pago, Efí.

Responda APENAS um JSON válido (sem comentários adicionais) no formato:
{
  "valor": 40.00,
  "data": "YYYY-MM-DD",
  "descricao": "Nome claro e conciso da despesa (ex: Sorvete, Almoço, Ponto dos Pães)",
  "categoria": "Uma das categorias exatas listadas acima",
  "forma": "PIX",
  "banco": "Nome do banco emissor ou detectado",
  "beneficiario": "Nome de quem recebeu o Pix ou estabelecimento",
  "observacao": "Detalhe adicional relevante"
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
                    return data
        except Exception as exc:
            log.warning("Falha na interpretação via IA, usando parser regex de contingência: %s", exc)

        return self._regex_fallback(raw_text, user_comment)

    def _regex_fallback(self, raw_text: str, user_comment: str) -> Dict[str, Any]:
        """Extração baseada em expressões regulares caso a IA esteja temporariamente indisponível."""
        combined = f"{user_comment} {raw_text}"
        
        # 1. Extrair valor
        valor = 0.0
        val_match = re.search(r"R\$\s*([\d\.]+,\d{2})", combined, re.IGNORECASE)
        if val_match:
            valor = float(val_match.group(1).replace(".", "").replace(",", "."))
        else:
            val_match2 = re.search(r"(?:^|\s)(\d{1,4}(?:[,\.]\d{2})?)\s*(?:reais|real|\$)", combined, re.IGNORECASE)
            if val_match2:
                valor = float(val_match2.group(1).replace(",", "."))

        # 2. Descrição
        clean_comment = re.sub(r"(?:comprovante|pix|de|r\$|\d+|reais|paguei|comprei|gastei)", "", user_comment, flags=re.IGNORECASE).strip()
        descricao = clean_comment.capitalize() if clean_comment else "Gasto Pix"

        # 3. Categoria preliminar
        categoria = "Alimentação & Mercado"
        comment_lower = combined.lower()
        if any(w in comment_lower for w in ("moto", "gasolina", "uber", "99", "oficina")):
            categoria = "Transporte & Moto"
        elif any(w in comment_lower for w in ("farmacia", "remédio", "medico", "saude")):
            categoria = "Saúde & Cuidados"
        elif any(w in comment_lower for w in ("luz", "agua", "condominio", "aluguel", "enel", "saae")):
            categoria = "Moradia & Casa"
        elif any(w in comment_lower for w in ("internet", "celular", "recarga", "sobral net", "laricell")):
            categoria = "Telefonia & Internet"

        return {
            "valor": valor,
            "data": datetime.now().strftime("%Y-%m-%d"),
            "descricao": descricao,
            "categoria": categoria,
            "forma": "PIX",
            "banco": "InfinitePay",
            "beneficiario": "",
            "observacao": user_comment or "Comprovante via Telegram",
        }

    @staticmethod
    def is_financial_intent(text: str) -> bool:
        """Identifica se uma mensagem de texto simples é uma declaração de gasto/Pix."""
        t = text.lower()
        has_action = any(w in t for w in ("comprei", "gastei", "paguei", "pix", "comprovante", "despesa", "conta"))
        has_money = bool(re.search(r"(?:r\$|\$|\breais\b|\breal\b|\b\d+,\d{2}\b)", t))
        return has_action and has_money
