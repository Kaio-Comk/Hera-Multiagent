"""
Exportador de dados das planilhas Finanças Kaio.xlsx e Gastos 2026.xlsx
para o formato de backup oficial v3 do aplicativo Android Fluxo (com.pessoal.fluxo).

Gera o arquivo JSON validado compatível com BackupCodec.kt e BackupRepository.kt.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl

EXCEL_FINANCAS = Path(r"C:\Users\Kaio Lima\Desktop\Finanças Kaio.xlsx")
EXCEL_GASTOS_2026 = Path(r"C:\Users\Kaio Lima\Finanças\extratos\Gastos 2026.xlsx")
OUTPUT_BACKUP = Path(r"B:\Finanças\Extratos\fluxo-backup-latest.json")

EPOCH = date(1970, 1, 1)

def to_epoch_day(d: Any) -> int:
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        # YYYY-MM-DD
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    return (d - EPOCH).days


def build_fluxo_backup(
    financas_path: Path = EXCEL_FINANCAS,
    gastos_path: Path = EXCEL_GASTOS_2026,
    output_path: Path = OUTPUT_BACKUP,
) -> Dict[str, Any]:
    wb_financas = openpyxl.load_workbook(financas_path, data_only=True)
    wb_gastos = openpyxl.load_workbook(gastos_path, data_only=True) if gastos_path.exists() else None

    # 1. CONTAS (Accounts)
    # Cores ARGB: formato inteiro assinado (32 bits)
    # Nubank: #820ad1 (-8254767)
    # Inter: #ff7a00 (-34304)
    # InfinitePay: #00c067 (-16727961)
    # PicPay: #11c76f (-15612049)
    # Bradesco: #cc092f (-3405521)
    # Efí MEI: #f26522 (-891614)
    # 99Pay: #ffcc00 (-13312)
    # Carteira: #4caf50 (-11737704)
    accounts = [
        {
            "id": 1,
            "name": "InfinitePay",
            "type": "CHECKING",
            "initialBalanceCents": 0,
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
            "initialBalanceCents": 0,
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
            "initialBalanceCents": 0,
            "colorArgb": -3405521,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": True,
        },
        {
            "id": 6,
            "name": "Efí (MEI)",
            "type": "CHECKING",
            "initialBalanceCents": 0,
            "colorArgb": -891614,
            "archived": False,
            "createdAt": 1700000000000,
            "includeInTotal": False,
        },
        {
            "id": 7,
            "name": "99Pay",
            "type": "WALLET",
            "initialBalanceCents": 0,
            "colorArgb": -13312,
            "archived": True,
            "createdAt": 1700000000000,
            "includeInTotal": False,
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

    account_map: Dict[str, int] = {
        "infinitepay": 1,
        "cloudwalk": 1,
        "nubank": 2,
        "nu pagamentos": 2,
        "banco inter": 3,
        "inter": 3,
        "picpay": 4,
        "bradesco": 5,
        "bradesco kaillany": 5,
        "bradesco celular": 5,
        "efí (mei)": 6,
        "efí s.a.": 6,
        "gerencianet": 6,
        "99pay": 7,
        "99 motorista": 7,
        "carteira": 8,
        "dinheiro": 8,
    }

    def get_account_id(bank_str: str) -> int:
        b = str(bank_str).lower().strip()
        for k, v in account_map.items():
            if k in b:
                return v
        return 1  # Default InfinitePay

    # 2. CATEGORIAS (Categories)
    # Ler todas as categorias do Extrato e do Gastos
    ws_extrato = wb_financas["Extrato"]
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

    # Categorias básicas adicionais
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
        # Determina INCOME vs EXPENSE
        n_lower = name.lower()
        if any(w in n_lower for w in ["receita", "vendas", "ganhos", "salário", "salario", "recebimento", "entrada", "cashback", "lucros"]):
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
        # Match parcial
        for k, v in category_map.items():
            if k in c or c in k:
                return v
        return category_map.get("entradas pix diversas" if is_income else "outros gastos", 1)

    # 3. TRANSAÇÕES (Transactions)
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

        # Determina valor e tipo
        in_val = float(val_in) if isinstance(val_in, (int, float)) and val_in > 0 else 0.0
        out_val = float(val_out) if isinstance(val_out, (int, float)) and val_out > 0 else 0.0

        if in_val <= 0 and out_val <= 0:
            continue

        is_transfer = "transferência entre contas" in str(cat_raw or "").lower()

        if is_transfer:
            # Transferência
            amount_cents = int(round((out_val or in_val) * 100))
            if amount_cents <= 0:
                continue
            # Escolhe outra conta como destino
            to_acc = 2 if acc_id != 2 else 1
            t_type = "TRANSFER"
            cat_id = None
        elif in_val > 0:
            amount_cents = int(round(in_val * 100))
            t_type = "INCOME"
            cat_id = get_category_id(str(cat_raw or ""), is_income=True)
            to_acc = None
        else:
            amount_cents = int(round(out_val * 100))
            t_type = "EXPENSE"
            cat_id = get_category_id(str(cat_raw or ""), is_income=False)
            to_acc = None

        transactions.append({
            "id": trans_id,
            "type": t_type,
            "amountCents": amount_cents,
            "date": epoch_day,
            "description": desc,
            "notes": notes,
            "accountId": acc_id,
            "toAccountId": to_acc,
            "categoryId": cat_id,
            "isPaid": True,
            "excludeFromReports": False,
            "recurrenceGroupId": None,
            "cardId": None,
            "invoiceMonth": None,
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
    # Honda Titan 160 (Santander Aymoré) da aba Dividas
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
        "notes": "42 parcelas já pagas. 6 restantes com desconto para quitação antecipada.",
        "archived": False,
        "createdAt": 1700000000000,
    }]

    # 48 parcelas
    loan_installments = []
    # Quotas de desconto conhecidas da aba Dividas:
    # 35: 710.16 (2027-01-30)
    # 36: 697.18 (2027-02-28)
    # 37: 684.00 (2027-03-30)
    # 39: 657.97 (2027-05-30)
    # 40: 645.13 (2027-06-30)
    # 41: 632.94 (2027-07-30)
    # 42: 620.58 (2027-08-30)
    # 43: 608.46 (2027-09-30)
    quotes = {
        35: 71016, 36: 69718, 37: 68400, 39: 65797,
        40: 64513, 41: 63294, 42: 62058, 43: 60846,
    }

    quote_date_epoch = to_epoch_day(date(2026, 10, 15))

    for n in range(1, 49):
        # Data de vencimento estimada: dia 30 de cada mês a partir de 2023-08
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

    # 5. DÍVIDAS (Debts)
    debts = [
        {
            "id": 1,
            "creditor": "Serasa Limpa Nome / Negociações",
            "contractNumber": "Acordos Kaio",
            "originalDueDate": to_epoch_day(date(2025, 1, 1)),
            "amountCents": 150000,
            "negativado": True,
            "offerCents": 45000,
            "offerCondition": "Desconto de 70% à vista",
            "offerInstallments": 1,
            "offerInstallmentCents": 45000,
            "offerValidUntil": to_epoch_day(date(2026, 12, 31)),
            "status": "NEGOTIATING",
            "notes": "Acompanhamento de ofertas no Serasa Limpa Nome.",
            "paidDate": None,
            "transactionId": None,
            "createdAt": 1700000000000,
        }
    ]

    # 6. GANHOS DE APLICATIVO (RideEarnings - 99 e Uber)
    ride_earnings = []
    ride_id = 1
    ws_ganhos = wb_financas["Ganhos"]

    # Meses Jan a Out 2026
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

    # 7. METAS (Goals) da aba Pagar Contas
    goals = [
        {
            "id": 1,
            "name": "Quitar a Moto",
            "description": "Quitação antecipada da Honda Titan 160 com desconto",
            "targetCents": 582678,
            "deadline": to_epoch_day(date(2026, 10, 31)),
            "colorArgb": -34304,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 2,
            "name": "Máquina de Lavar",
            "description": "Compra da máquina de lavar para a casa",
            "targetCents": 150000,
            "deadline": to_epoch_day(date(2026, 11, 30)),
            "colorArgb": -16727961,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 3,
            "name": "Ajeitar a Casa",
            "description": "Reforma e melhorias na residência",
            "targetCents": 300000,
            "deadline": to_epoch_day(date(2026, 12, 31)),
            "colorArgb": -8254767,
            "status": "ACTIVE",
            "createdAt": 1700000000000,
        },
        {
            "id": 4,
            "name": "Carro",
            "description": "Aquisição do veículo da família",
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
            "body": "Histórico completo auditado das contas InfinitePay, Nubank, Inter, PicPay, Bradesco, Efí e 99Pay.\nMeta prioritária: Quitação da Honda Titan 160.",
            "status": "INFO",
            "pinned": True,
            "createdAt": 1700000000000,
            "updatedAt": 1700000000000,
        }
    ]

    # Cartões de Crédito (Cards)
    cards = [
        {
            "id": 1,
            "name": "Cartão Kaillany",
            "limitCents": 300000,
            "closingDay": 5,
            "dueDay": 12,
            "colorArgb": -3405521,
            "defaultPaymentAccountId": 5,
            "archived": False,
        }
    ]

    snapshot: Dict[str, Any] = {
        "format": "fluxo-backup",
        "version": 3,
        "exportedAt": int(datetime.now().timestamp() * 1000),
        "accounts": accounts,
        "categories": categories,
        "cards": cards,
        "purchases": [],
        "installments": [],
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

    # Salva JSON
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"✅ Backup do Fluxo gerado com sucesso!")
    print(f"📁 Destino: {output_path}")
    print(f"📊 Resumo do snapshot:")
    print(f"   • Contas: {len(accounts)}")
    print(f"   • Categorias: {len(categories)}")
    print(f"   • Transações: {len(transactions)}")
    print(f"   • Financiamentos: {len(loans)} ({len(loan_installments)} parcelas)")
    print(f"   • Dívidas: {len(debts)}")
    print(f"   • Ganhos de app (99/Uber): {len(ride_earnings)}")
    print(f"   • Metas: {len(goals)}")
    print(f"   • Notas: {len(notes)}")

    return snapshot

if __name__ == "__main__":
    build_fluxo_backup()
