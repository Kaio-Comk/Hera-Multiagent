"""
Exportador de dados das planilhas Finanças Kaio.xlsx e Gastos 2026.xlsx
para o formato de backup oficial v3 do aplicativo Android Fluxo (com.pessoal.fluxo).

Corrige matematicamente os saldos reais auditados:
- InfinitePay: R$ 74,16 (R$ -0,84 após gastos de 06/11 R$ 75,00)
- Bradesco (Kaillany): R$ 3,04
- Nubank: R$ 0,00
- Banco Inter: R$ 0,00
- PicPay: R$ 0,00
- Efí MEI: R$ 0,00 (desconsiderado do total pessoal)
- 99Pay: R$ 0,00 (encerrada)
- Carteira: R$ 0,00
- Saldo Líquido Total Disponível: R$ 2,20 (ou R$ 77,20 antes de 06/11).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl

EXCEL_FINANCAS = Path(r"B:\Finanças\Extratos\Finanças Kaio.xlsx")
if not EXCEL_FINANCAS.exists():
    EXCEL_FINANCAS = Path(r"B:\Finanças\Extratos\00 - Orçamento e Planilhas\Finanças Kaio.xlsx")

OUTPUT_BACKUP = Path(r"B:\Finanças\Extratos\fluxo-backup-latest.json")

EPOCH = date(1970, 1, 1)

def to_epoch_day(d: Any) -> int:
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    return (d - EPOCH).days


def build_fluxo_backup(
    financas_path: Path = EXCEL_FINANCAS,
    output_path: Path = OUTPUT_BACKUP,
) -> Dict[str, Any]:
    wb = openpyxl.load_workbook(financas_path, data_only=True)
    ws_extrato = wb["Extrato"]

    # 1. MAPEAMENTO DE CONTAS
    # Cores ARGB:
    # InfinitePay: -16727961 (#00c067)
    # Nubank: -8254767 (#820ad1)
    # Inter: -34304 (#ff7a00)
    # PicPay: -15612049 (#11c76f)
    # Bradesco: -3405521 (#cc092f)
    # Efí MEI: -891614 (#f26522)
    # 99Pay: -13312 (#ffcc00)
    # Carteira: -11737704 (#4caf50)

    # Initial balances calculados exatamente para atingir os saldos auditados reais:
    # Net das transações:
    # 1 (InfinitePay): -7.84 (com almoço 35 + sorvete 40 de 06/11). Com init 82.00 -> saldo atual = -0.84 (antes de hoje era 74.16).
    # 2 (Nubank): 0.00 -> init 0
    # 3 (Banco Inter): -1274.53 -> init 1274.53 -> saldo 0.00
    # 4 (PicPay): 0.00 -> init 0 -> saldo 0.00
    # 5 (Bradesco): -575.85 -> init 578.89 -> saldo 3.04
    # 6 (Efí MEI): +260.17 -> init -260.17 -> saldo 0.00
    # 7 (99Pay): -169.48 -> init 169.48 -> saldo 0.00
    # 8 (Carteira): 0.00 -> init 0 -> saldo 0.00

    accounts = [
        {
            "id": 1,
            "name": "InfinitePay",
            "type": "CHECKING",
            "initialBalanceCents": 8200,  # R$ 82,00 inicial -> R$ 74,16 em 05/11 -> R$ -0,84 em 06/11
            "colorArgb": -16727961,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 2,
            "name": "Nubank",
            "type": "CHECKING",
            "initialBalanceCents": 0,
            "colorArgb": -8254767,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 3,
            "name": "Banco Inter",
            "type": "CHECKING",
            "initialBalanceCents": 127453,  # R$ 1.274,53 inicial -> R$ 0,00 final
            "colorArgb": -34304,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 4,
            "name": "PicPay",
            "type": "WALLET",
            "initialBalanceCents": 0,
            "colorArgb": -15612049,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 5,
            "name": "Bradesco (Kaillany)",
            "type": "CHECKING",
            "initialBalanceCents": 57889,  # R$ 578,89 inicial -> R$ 3,04 final
            "colorArgb": -3405521,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 6,
            "name": "Efí (MEI)",
            "type": "CHECKING",
            "initialBalanceCents": -26017,  # Desconsiderado no pessoal -> R$ 0,00 final
            "colorArgb": -891614,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": False,  # Conta jurídica separada
        },
        {
            "id": 7,
            "name": "99Pay",
            "type": "WALLET",
            "initialBalanceCents": 16948,  # R$ 169,48 inicial -> R$ 0,00 final
            "colorArgb": -13312,
            "archived": True,  # Carteira encerrada em julho
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 8,
            "name": "Carteira (Dinheiro)",
            "type": "WALLET",
            "initialBalanceCents": 0,
            "colorArgb": -11737704,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
    ]

    account_map = {
        "infinitepay": 1,
        "nubank": 2,
        "inter": 3,
        "picpay": 4,
        "bradesco": 5,
        "efí": 6,
        "efi": 6,
        "mei": 6,
        "99pay": 7,
        "99 motorista": 7,
        "dinheiro": 8,
        "carteira": 8,
    }

    def get_account_id(bank_str: str) -> int:
        b = str(bank_str).lower().strip()
        for k, v in account_map.items():
            if k in b:
                return v
        return 1

    # 2. CATEGORIAS (Categories)
    cat_names: Set[str] = set()
    cat_kind_hint: Dict[str, str] = {}

    for r in range(34, ws_extrato.max_row + 1):
        cat = ws_extrato.cell(r, 6).value
        val_in = ws_extrato.cell(r, 7).value
        val_out = ws_extrato.cell(r, 8).value
        if cat:
            cname = str(cat).strip()
            cat_names.add(cname)
            if val_in and not val_out:
                cat_kind_hint[cname] = "INCOME"
            elif val_out and not val_in:
                if cname not in cat_kind_hint:
                    cat_kind_hint[cname] = "EXPENSE"

    default_cats = [
        ("Alimentação & Mercado", "EXPENSE", -11737704),
        ("Transporte & Moto", "EXPENSE", -34304),
        ("Moradia & Contas Básicas", "EXPENSE", -891614),
        ("Saúde & Farmácia", "EXPENSE", -16727961),
        ("Lazer & Passeios", "EXPENSE", -8254767),
        ("I.A., Software & Ferramentas", "EXPENSE", -14197773),
        ("Compras & Mercado Livre", "EXPENSE", -3405521),
        ("Família & Filho", "EXPENSE", -13312),
        ("Outros Gastos", "EXPENSE", -7829368),
        ("Receita / Vendas InfinitePay", "INCOME", -16727961),
        ("Ganhos 99 e Uber", "INCOME", -34304),
        ("Salário Kaillany", "INCOME", -11737704),
        ("Entradas Pix Diversas", "INCOME", -8254767),
        ("Transferência entre Contas", "EXPENSE", -7829368),
    ]

    for name, kind, _ in default_cats:
        cat_names.add(name)
        if name not in cat_kind_hint:
            cat_kind_hint[name] = kind

    sorted_cat_names = sorted(list(cat_names))
    categories = []
    category_map: Dict[str, int] = {}

    cat_colors = [
        -16727961, -8254767, -34304, -15612049, -3405521, -891614, -13312, -11737704,
        -14197773, -65536, -16711681, -256, -8388480, -16776961, -7829368,
    ]

    for idx, name in enumerate(sorted_cat_names, start=1):
        n_lower = name.lower()
        if any(w in n_lower for w in ["receita", "vendas", "ganhos", "salário", "salario", "recebimento", "entrada", "cashback", "lucros", "reembolso", "estorno"]):
            kind = "INCOME"
        else:
            kind = cat_kind_hint.get(name, "EXPENSE")

        category_map[name.lower()] = idx
        categories.append({
            "id": idx,
            "name": name,
            "kind": kind,
            "parentId": None,
            "colorArgb": cat_colors[idx % len(cat_colors)],
            "archived": False,
        })

    def get_category_id(cat_str: Optional[str], is_income: bool = False) -> int:
        if not cat_str:
            return category_map.get("entradas pix diversas" if is_income else "outros gastos", 1)
        c = str(cat_str).lower().strip()
        if c in category_map:
            return category_map[c]
        for k, v in category_map.items():
            if k in c or c in k:
                return v
        return category_map.get("entradas pix diversas" if is_income else "outros gastos", 1)

    # 3. TRANSAÇÕES (Transactions)
    # REGRA FUNDAMENTAL:
    # Cada linha da planilha pertence ao extrato daquela conta específica.
    # Entrada -> INCOME na conta (sem debitar terceiros).
    # Saída   -> EXPENSE na conta (sem creditar terceiros).
    # toAccountId permanece NULL para NÃO criar transferências cruzadas artificiais que distorciam os saldos!
    transactions = []
    trans_id = 1

    for r in range(34, ws_extrato.max_row + 1):
        dt_raw = ws_extrato.cell(r, 2).value
        bank_raw = ws_extrato.cell(r, 3).value
        tipo_raw = ws_extrato.cell(r, 4).value
        desc_raw = ws_extrato.cell(r, 5).value
        cat_raw = ws_extrato.cell(r, 6).value
        val_in = ws_extrato.cell(r, 7).value
        val_out = ws_extrato.cell(r, 8).value

        if dt_raw is None and bank_raw is None and desc_raw is None:
            continue

        epoch_day = to_epoch_day(dt_raw) if dt_raw else to_epoch_day(date(2026, 1, 1))
        acc_id = get_account_id(str(bank_raw or ""))

        desc = str(desc_raw or tipo_raw or "Transação").strip()
        notes = f"{tipo_raw or ''}".strip()

        in_val = float(val_in) if isinstance(val_in, (int, float)) and val_in > 0 else 0.0
        out_val = float(val_out) if isinstance(val_out, (int, float)) and val_out > 0 else 0.0

        if in_val <= 0 and out_val <= 0:
            continue

        card_id = None
        inv_month = None

        if in_val > 0:
            amount_cents = int(round(in_val * 100))
            t_type = "INCOME"
            cat_id = get_category_id(str(cat_raw or ""), is_income=True)
        else:
            amount_cents = int(round(out_val * 100))
            t_type = "EXPENSE"
            cat_id = get_category_id(str(cat_raw or ""), is_income=False)

            # Identificação precisa de pagamentos de fatura de cartão Bradesco:
            d_lower = desc.lower()
            if "gastos cartao de credito" in d_lower or "pagto antec" in d_lower or (acc_id == 7 and amount_cents == 80973 and "5806" in desc):
                card_id = 1
                t_type = "INVOICE_PAYMENT"
                if amount_cents == 23699:
                    inv_month = 202602
                elif amount_cents == 55143:
                    inv_month = 202603
                elif amount_cents == 81720:
                    inv_month = 202604
                elif amount_cents == 95819:
                    inv_month = 202605
                elif amount_cents == 50062:
                    inv_month = 202606
                elif amount_cents == 80973:
                    inv_month = 202607
                elif amount_cents in (47620, 63431):
                    inv_month = 202608
                elif amount_cents == 113434:
                    inv_month = 202609
                elif amount_cents == 102003:
                    inv_month = 202610

        transactions.append({
            "id": trans_id,
            "type": t_type,
            "amountCents": amount_cents,
            "date": epoch_day,
            "description": desc,
            "notes": notes,
            "accountId": acc_id,
            "toAccountId": None,
            "categoryId": cat_id,
            "isPaid": True,
            "excludeFromReports": False,
            "recurrenceGroupId": None,
            "cardId": card_id,
            "invoiceMonth": inv_month,
            "createdAt": 1700000000000 + trans_id * 1000,
            "fixedRuleId": None,
            "currency": "BRL",
            "originalAmountCents": None,
            "latitude": None,
            "longitude": None,
            "importKey": f"extrato_row_{r}",
        })
        trans_id += 1

    # 4. FINANCIAMENTOS (Loans) e PARCELAS (LoanInstallments)
    loans = [{
        "id": 1,
        "name": "Honda Titan 160",
        "lender": "Santander Aymoré",
        "contractNumber": "Contrato 48x",
        "principalCents": 2200000,
        "installmentCount": 48,
        "installmentCents": 76017,
        "firstDueDate": to_epoch_day(date(2023, 8, 30)),
        "monthlyRateBp": 193,  # 1.93% a.m.
        "cetBp": 240,
        "accountId": 1,  # InfinitePay
        "categoryId": get_category_id("Transporte & Moto"),
        "notes": "42 parcelas já pagas (87,5%). 6 restantes com desconto para quitação antecipada por R$ 3.937,80.",
        "archived": False,
        "createdAt": 1700000000000,
    }]

    quotes = {
        35: 71016, 36: 69718, 37: 68400, 39: 65797,
        40: 64513, 41: 63294, 42: 62058, 43: 60846,
    }
    quote_date_epoch = to_epoch_day(date(2026, 10, 15))

    loan_installments = []
    for n in range(1, 49):
        year = 2023 + (7 + n - 1) // 12
        month = (7 + n - 1) % 12 + 1
        day = 28 if month == 2 else 30
        due_date = to_epoch_day(date(year, month, day))

        is_paid = (n <= 42)
        paid_date = due_date if is_paid else None
        paid_cents = 76017 if is_paid else None
        quote_val = quotes.get(n)

        loan_installments.append({
            "id": n,
            "loanId": 1,
            "number": n,
            "dueDate": due_date,
            "nominalCents": 76017,
            "paidDate": paid_date,
            "paidCents": paid_cents,
            "quoteCents": quote_val,
            "quoteDate": quote_date_epoch if quote_val else None,
            "transactionId": None,
        })

    # 5. DÍVIDAS (Debts) - Serasa Limpa Nome
    debts = [
        {
            "id": 1,
            "creditor": "Nu Financeira (Nubank CPF Principal)",
            "contractNumber": "Contrato Principal",
            "originalDueDate": to_epoch_day(date(2025, 1, 1)),
            "amountCents": 717903,
            "negativado": True,
            "offerCents": 100322,
            "offerCondition": "86% de desconto à vista",
            "offerInstallments": 1,
            "offerInstallmentCents": 100322,
            "offerValidUntil": to_epoch_day(date(2026, 12, 31)),
            "status": "NEGOTIATING",
            "notes": "Maior dívida do Nubank no CPF. Oportunidade de quitar por R$ 1.003,22.",
            "paidDate": None,
            "transactionId": None,
            "createdAt": 1700000000000,
        },
        {
            "id": 2,
            "creditor": "Nu Financeira (Nubank CNPJ)",
            "contractNumber": "Contrato CNPJ",
            "originalDueDate": to_epoch_day(date(2025, 1, 1)),
            "amountCents": 142052,
            "negativado": True,
            "offerCents": 42524,
            "offerCondition": "70% de desconto à vista",
            "offerInstallments": 1,
            "offerInstallmentCents": 42524,
            "offerValidUntil": to_epoch_day(date(2026, 12, 31)),
            "status": "NEGOTIATING",
            "notes": "Dívida jurídica Nubank.",
            "paidDate": None,
            "transactionId": None,
            "createdAt": 1700000000000,
        },
        {
            "id": 3,
            "creditor": "Nu Financeira (Nubank Menor)",
            "contractNumber": "Contrato Menor",
            "originalDueDate": to_epoch_day(date(2025, 1, 1)),
            "amountCents": 55563,
            "negativado": True,
            "offerCents": 18683,
            "offerCondition": "66% de desconto à vista",
            "offerInstallments": 1,
            "offerInstallmentCents": 18683,
            "offerValidUntil": to_epoch_day(date(2026, 12, 31)),
            "status": "NEGOTIATING",
            "notes": "Dívida menor Nubank.",
            "paidDate": None,
            "transactionId": None,
            "createdAt": 1700000000000,
        },
        {
            "id": 4,
            "creditor": "Telefônica Brasil (Vivo)",
            "contractNumber": "Contrato Vivo 3x",
            "originalDueDate": to_epoch_day(date(2025, 1, 1)),
            "amountCents": 31040,
            "negativado": True,
            "offerCents": 17072,
            "offerCondition": "45% de desconto à vista",
            "offerInstallments": 1,
            "offerInstallmentCents": 17072,
            "offerValidUntil": to_epoch_day(date(2026, 12, 31)),
            "status": "NEGOTIATING",
            "notes": "Dívida de telefonia Vivo.",
            "paidDate": None,
            "transactionId": None,
            "createdAt": 1700000000000,
        },
    ]

    # 6. GANHOS DE APLICATIVO (RideEarnings)
    ride_earnings = []
    ride_id = 1
    ws_ganhos = wb["Ganhos"]

    month_dates = [
        (1, date(2026, 1, 1), date(2026, 1, 31)),
        (2, date(2026, 2, 1), date(2026, 2, 28)),
        (3, date(2026, 3, 1), date(2026, 3, 31)),
        (4, date(2026, 4, 1), date(2026, 4, 30)),
        (5, date(2026, 5, 1), date(2026, 5, 31)),
        (6, date(2026, 6, 1), date(2026, 6, 30)),
        (7, date(2026, 7, 1), date(2026, 7, 31)),
        (8, date(2026, 8, 1), date(2026, 8, 31)),
        (9, date(2026, 9, 1), date(2026, 9, 30)),
        (10, date(2026, 10, 1), date(2026, 10, 31)),
    ]

    for m_idx, s_date, e_date in month_dates:
        row_num = 5 + m_idx
        v_99 = ws_ganhos.cell(row_num, 3).value
        v_uber = ws_ganhos.cell(row_num, 4).value

        if isinstance(v_99, (int, float)) and v_99 > 0:
            ride_earnings.append({
                "id": ride_id,
                "platform": "99",
                "startDate": to_epoch_day(s_date),
                "endDate": to_epoch_day(e_date),
                "rides": None,
                "distanceKm": None,
                "grossCents": int(round(v_99 * 100)),
                "feesCents": 0,
                "netCents": int(round(v_99 * 100)),
                "notes": f"Líquido 99 ({s_date.strftime('%B/%Y')})",
                "transactionId": None,
                "createdAt": 1700000000000 + ride_id,
            })
            ride_id += 1

        if isinstance(v_uber, (int, float)) and v_uber > 0:
            ride_earnings.append({
                "id": ride_id,
                "platform": "Uber",
                "startDate": to_epoch_day(s_date),
                "endDate": to_epoch_day(e_date),
                "rides": None,
                "distanceKm": None,
                "grossCents": int(round(v_uber * 100)),
                "feesCents": 0,
                "netCents": int(round(v_uber * 100)),
                "notes": f"Líquido Uber ({s_date.strftime('%B/%Y')})",
                "transactionId": None,
                "createdAt": 1700000000000 + ride_id,
            })
            ride_id += 1

    # 7. METAS (Goals)
    goals = [
        {
            "id": 1,
            "name": "🏍️ Quitar a Moto (Titan 160)",
            "description": "Quitação das 6 parcelas restantes com deságio de juros por R$ 3.937,80 (42 de 48 parcelas já quitadas).",
            "targetCents": 393780,  # Valor real auditado de quitação hoje
            "deadline": to_epoch_day(date(2026, 12, 31)),
            "colorArgb": -34304,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 2,
            "name": "🧺 Máquina de Lavar",
            "description": "Compra da máquina de lavar para a casa (meta seguinte após a moto).",
            "targetCents": 150000,
            "deadline": to_epoch_day(date(2027, 3, 31)),
            "colorArgb": -16727961,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 3,
            "name": "🏠 Ajeitar a Casa",
            "description": "Reforma e melhorias na residência.",
            "targetCents": 300000,
            "deadline": to_epoch_day(date(2027, 6, 30)),
            "colorArgb": -8254767,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 4,
            "name": "🚗 Compra do Carro",
            "description": "Aquisição do veículo da família (médio/longo prazo).",
            "targetCents": 6500000,
            "deadline": to_epoch_day(date(2028, 12, 31)),
            "colorArgb": -3405521,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
    ]

    # 8. NOTAS (Notes)
    notes = [
        {
            "id": 1,
            "title": "Finanças Consolidadas Kaio & Kaillany",
            "body": "Histórico auditado oficial. Saldo líquido pessoal: R$ 77,20 (InfinitePay R$ 74,16 + Bradesco R$ 3,04). Meta prioritária: Quitação da Moto por R$ 3.937,80.",
            "status": "INFO",
            "pinned": True,
            "createdAt": 1700000000000,
            "updatedAt": 1700000000000,
        }
    ]

    cards = [
        {
            "id": 1,
            "name": "Cartão Kaillany (Bradesco)",
            "limitCents": 300000,
            "closingDay": 5,
            "dueDay": 10,
            "colorArgb": -3405521,
            "defaultPaymentAccountId": 5,
            "archived": False,
        }
    ]

    # 9. COMPRAS E PARCELAS DO CARTÃO DE CRÉDITO
    card_purchases = []
    card_installments = []
    p_id = 1
    i_id = 1

    month_name_map = {
        'fevereiro': (2026, 2),
        'março': (2026, 3),
        'marco': (2026, 3),
        'abril': (2026, 4),
        'maio': (2026, 5),
        'junho': (2026, 6),
        'julho': (2026, 7),
        'agosto': (2026, 8),
        'setembro': (2026, 9),
        'outubro': (2026, 10),
        'novembro': (2026, 11),
        'dezembro': (2026, 12),
    }

    def get_card_category_id(c_desc: str) -> int:
        d = c_desc.lower()
        if any(w in d for w in ["uber", "99", "gasolina", "combustivel", "moto", "estacionamento"]):
            return get_category_id("Transporte & Moto")
        if any(w in d for w in ["farmacia", "drogaria", "pague menos", "diony", "saude", "medico", "consulta"]):
            return get_category_id("Saúde & Farmácia")
        if any(w in d for w in ["assai", "supermercado", "mercado", "frutas", "leon", "ifd*", "ifood", "restaurante", "padaria", "panificadora", "lanches", "acai", "sorvete"]):
            return get_category_id("Alimentação & Mercado")
        if any(w in d for w in ["mercadolivre", "mercado livre", "amazon", "shopee", "claudia bolsas", "loja", "tntinfoloja", "magazine", "compras", "etceter", "marjorie"]):
            return get_category_id("Compras & Mercado Livre")
        if any(w in d for w in ["apple", "google", "chatgpt", "openai", "cursor", "anthropic", "github", "software", "nuvem"]):
            return get_category_id("I.A., Software & Ferramentas")
        if any(w in d for w in ["vivo", "claro", "tim", "enel", "sobral net", "internet", "luz", "agua", "gas", "nosso gas"]):
            return get_category_id("Moradia & Contas Básicas")
        if any(w in d for w in ["queima diaria", "cinema", "show", "lazer"]):
            return get_category_id("Lazer & Passeios")
        return get_category_id("Outros Gastos")

    if "Gastos" in wb.sheetnames:
        import re
        ws_gastos = wb["Gastos"]
        for r in range(202, ws_gastos.max_row + 1):
            fat = ws_gastos.cell(r, 2).value
            if not fat:
                continue
            f_str = str(fat).strip()
            m_match = re.search(r'([A-Za-zç]+)/?(\d{4})?', f_str, re.IGNORECASE)
            if not m_match:
                continue
            m_name = m_match.group(1).lower()
            if m_name not in month_name_map:
                continue
            y, m = month_name_map[m_name]
            inv_month = y * 100 + m

            dt_val = ws_gastos.cell(r, 3).value
            card_final = str(ws_gastos.cell(r, 4).value or '').strip()
            c_desc = str(ws_gastos.cell(r, 5).value or 'Compra Cartão').strip()
            val = ws_gastos.cell(r, 7).value
            if val is None:
                continue
            val = float(val)
            amount_cents = int(round(val * 100))

            p_date = None
            if isinstance(dt_val, (datetime, date)):
                p_date = dt_val if isinstance(dt_val, date) else dt_val.date()
            elif isinstance(dt_val, str) and "/" in dt_val:
                parts = dt_val.strip().split("/")
                if len(parts) == 2:
                    try:
                        d_day = int(parts[0])
                        d_mon = int(parts[1])
                        d_yr = 2026
                        if d_mon == 12 and m <= 2:
                            d_yr = 2025
                        p_date = date(d_yr, d_mon, d_day)
                    except Exception:
                        pass
            if not p_date:
                p_date = date(y, m, 1)

            inst_match = re.search(r'(\d{1,2})/(\d{1,2})', c_desc)
            if inst_match:
                inst_num = int(inst_match.group(1))
                inst_count = int(inst_match.group(2))
            else:
                inst_num = 1
                inst_count = 1

            epoch_d = to_epoch_day(p_date)
            cat_id = get_card_category_id(c_desc)

            card_purchases.append({
                "id": p_id,
                "cardId": 1,
                "description": c_desc,
                "categoryId": cat_id,
                "totalCents": amount_cents,
                "installmentCount": inst_count,
                "purchaseDate": epoch_d,
                "kind": "PURCHASE",
                "notes": f"Cartão final {card_final}".strip(),
                "createdAt": 1700000000000 + p_id * 1000,
                "fixedRuleId": None,
                "currency": "BRL",
                "originalAmountCents": None,
                "latitude": None,
                "longitude": None,
                "importKey": f"card_purchase_{r}",
            })

            card_installments.append({
                "id": i_id,
                "purchaseId": p_id,
                "cardId": 1,
                "number": inst_num,
                "amountCents": amount_cents,
                "invoiceMonth": inv_month,
            })

            p_id += 1
            i_id += 1

    snapshot: Dict[str, Any] = {
        "format": "fluxo-backup",
        "version": 3,
        "exportedAt": int(datetime.now().timestamp() * 1000),
        "accounts": accounts,
        "categories": categories,
        "cards": cards,
        "purchases": card_purchases,
        "installments": card_installments,
        "transactions": transactions,
        "budgets": [],
        "goals": goals,
        "contributions": [],
        "tags": [],
        "transactionTags": [],
        "purchaseTags": [],
        "fixedRules": [],
        "favorites": [],
        "monthlyLimits": [],
        "customBudgets": [],
        "rideEarnings": ride_earnings,
        "loans": loans,
        "loanInstallments": loan_installments,
        "debts": debts,
        "notes": notes,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"✅ Backup do Fluxo gerado com sucesso!")
    print(f"📁 Destino: {output_path}")
    print(f"📊 Resumo do snapshot corrigido:")
    print(f"   • Contas: {len(accounts)}")
    print(f"   • Categorias: {len(categories)}")
    print(f"   • Transações: {len(transactions)}")
    print(f"   • Financiamentos: {len(loans)} ({len(loan_installments)} parcelas)")
    print(f"   • Dívidas: {len(debts)}")
    print(f"   • Ganhos de app: {len(ride_earnings)}")
    print(f"   • Metas: {len(goals)}")
    print(f"   • Notas: {len(notes)}")

    return snapshot

if __name__ == "__main__":
    build_fluxo_backup()
