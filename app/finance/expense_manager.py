"""Gerenciador e lançador de despesas nas planilhas financeiras do Kaio."""

from __future__ import annotations

import json
import logging
import os
import shutil
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

log = logging.getLogger("hera.finance.expense_manager")

def _user_home() -> Path:
    h = Path.home()
    if (h / "Desktop").exists() or (h / "Documents").exists():
        return h
    users_dir = Path(os.environ.get("SystemDrive", "C:") + r"\Users")
    if users_dir.is_dir():
        try:
            for u in users_dir.iterdir():
                if u.is_dir() and u.name.lower() not in ("public", "default", "all users"):
                    if (u / "Desktop").exists() or (u / "Documents").exists():
                        return u
        except OSError:
            pass
    return h


_HOME = _user_home()
DESKTOP_PLANILHA = _HOME / "Desktop" / "Finanças Kaio.xlsx"
BACKUP_PLANILHA = Path(r"B:\Finanças\Extratos\Finanças Kaio.xlsx")
GASTOS_2026_PATH = _HOME / "Finanças" / "extratos" / "Gastos 2026.xlsx"
GASTOS_BASE_PATH = Path(r"B:\Finanças\Extratos\00 - Orçamento e Planilhas\Gastos 2026 (Orçamento Base).xlsx")

AUDIT_LOG_PATH = Path(r"B:\Finanças\Extratos\gastos_telegram.jsonl")
RECEIPTS_DIR = Path(r"B:\Finanças\Extratos\Comprovantes Telegram")
BACKUP_DIR = Path(r"B:\Finanças\Extratos\00 - Orçamento e Planilhas\Backups")
OBSIDIAN_FINANCAS = _HOME / "Documents" / "Obsidian Vault" / "Kaio" / "Finanças.md"

MONTH_NAMES = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
}


def _copy_cell_style(source_cell, target_cell):
    """Copia estilos visuais (fonte, alinhamento, borda, preenchimento e formato numérico)."""
    if source_cell.has_style:
        target_cell.font = copy(source_cell.font)
        target_cell.border = copy(source_cell.border)
        target_cell.fill = copy(source_cell.fill)
        target_cell.number_format = copy(source_cell.number_format)
        target_cell.protection = copy(source_cell.protection)
        target_cell.alignment = copy(source_cell.alignment)


class ExpenseManager:
    """Gerenciador que insere novos gastos e comprovantes de forma atômica e segura."""

    def __init__(self):
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    def _backup_files(self) -> None:
        """Cria cópia de segurança antes de qualquer modificação."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if DESKTOP_PLANILHA.exists():
            shutil.copy2(DESKTOP_PLANILHA, BACKUP_DIR / f"Financas_Kaio_{timestamp}.xlsx")
        if GASTOS_2026_PATH.exists():
            shutil.copy2(GASTOS_2026_PATH, BACKUP_DIR / f"Gastos_2026_{timestamp}.xlsx")

    def record_expense(self, expense: Dict[str, Any], receipt_path: Optional[Path] = None) -> Dict[str, Any]:
        """Lança a movimentação financeira (despesa ou receita) no Fluxo SQLite e histórico."""
        self._backup_files()

        tipo = str(expense.get("tipo") or "EXPENSE").upper().strip()
        if tipo not in ("INCOME", "EXPENSE", "TRANSFER"):
            tipo = "EXPENSE"

        valor = float(expense.get("valor", 0.0))
        data_str = expense.get("data") or datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(data_str, "%Y-%m-%d")
        mes_nome = MONTH_NAMES.get(dt.month, "Novembro")
        descricao = str(expense.get("descricao") or ("Receita" if tipo == "INCOME" else "Despesa")).strip()
        categoria = str(expense.get("categoria") or ("Outros Recebimentos Pix" if tipo == "INCOME" else "Alimentação & Mercado")).strip()
        forma = str(expense.get("forma") or "PIX").strip()
        banco = str(expense.get("banco") or "InfinitePay").strip()
        beneficiario = str(expense.get("beneficiario") or "").strip()
        observacao = str(expense.get("observacao") or "Lançamento via Telegram").strip()

        # 1. Atualizar Finanças Kaio.xlsx apenas se for despesa e planilha existir
        if tipo == "EXPENSE" and DESKTOP_PLANILHA.exists():
            try:
                wb = openpyxl.load_workbook(DESKTOP_PLANILHA)
                self._update_gastos_sheet(wb, dt, mes_nome, descricao, categoria, forma, valor, observacao)
                self._update_extrato_sheet(wb, dt, banco, descricao, beneficiario, categoria, valor)
                wb.save(DESKTOP_PLANILHA)
                shutil.copy2(DESKTOP_PLANILHA, BACKUP_PLANILHA)
                log.info("Finanças Kaio.xlsx atualizado no Desktop.")
            except Exception as e_sheet:
                log.warning("Aviso ao atualizar Finanças Kaio.xlsx: %s", e_sheet)

        # 2. Atualizar Gastos 2026.xlsx apenas se for despesa e planilha existir
        if tipo == "EXPENSE" and GASTOS_2026_PATH.exists():
            try:
                wb_g = openpyxl.load_workbook(GASTOS_2026_PATH)
                if mes_nome in wb_g.sheetnames:
                    ws_m = wb_g[mes_nome]
                    insert_row = ws_m.max_row + 1
                    for r in range(ws_m.max_row, 2, -1):
                        if ws_m.cell(r, 1).value is not None or ws_m.cell(r, 2).value is not None:
                            insert_row = r + 1
                            break
                    ws_m.cell(insert_row, 1, float(dt.day))
                    ws_m.cell(insert_row, 2, descricao)
                    ws_m.cell(insert_row, 5, valor)
                    ws_m.cell(insert_row, 7, "OK")
                    wb_g.save(GASTOS_2026_PATH)
                    if GASTOS_BASE_PATH.parent.exists():
                        shutil.copy2(GASTOS_2026_PATH, GASTOS_BASE_PATH)
            except Exception as e_gastos:
                log.warning("Aviso ao atualizar Gastos 2026.xlsx: %s", e_gastos)

        # 3. Arquivar comprovante
        saved_receipt_path = None
        if receipt_path and Path(receipt_path).exists():
            receipt_p = Path(receipt_path)
            mes_folder = RECEIPTS_DIR / dt.strftime("%Y-%m")
            mes_folder.mkdir(parents=True, exist_ok=True)
            clean_desc = "".join(c for c in descricao if c.isalnum() or c in (" ", "-", "_")).strip()
            dest_name = f"{dt.strftime('%Y-%m-%d')} — {tipo} R$ {valor:.2f} — {clean_desc}{receipt_p.suffix}".replace(",", ".")
            dest_file = mes_folder / dest_name
            shutil.copy2(receipt_p, dest_file)
            saved_receipt_path = str(dest_file)

        # 4. Registrar no log de auditoria JSONL
        audit_entry = {
            "timestamp": datetime.now().isoformat(),
            "data": data_str,
            "mes": mes_nome,
            "tipo": tipo,
            "valor": valor,
            "descricao": descricao,
            "categoria": categoria,
            "forma": forma,
            "banco": banco,
            "beneficiario": beneficiario,
            "observacao": observacao,
            "comprovante": saved_receipt_path
        }
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

        # 5. Atualizar Obsidian Vault se acessível
        self._append_to_obsidian(audit_entry)

        # 6. Gravar diretamente no banco SQLite central do Fluxo (PC)
        try:
            from app.finance.fluxo_db import fluxo_db
            
            # Mapeamento inteligente de conta
            b_low = banco.lower()
            conta_id = 1  # InfinitePay default
            if "99pay" in b_low or "99 pay" in b_low or "99" in b_low:
                conta_id = 7
            elif "inter" in b_low:
                conta_id = 3
            elif "nubank" in b_low or "nu" in b_low:
                conta_id = 2
            elif "picpay" in b_low:
                conta_id = 4
            elif "bradesco" in b_low:
                conta_id = 5
            elif "efi" in b_low or "efí" in b_low:
                conta_id = 6
            elif "dinheiro" in b_low or "carteira" in b_low:
                conta_id = 8

            # Resolver categoria_id por nome exato no fluxo_db
            cat_id = None
            with fluxo_db.get_connection() as conn:
                r_cat = conn.execute("SELECT id FROM categories WHERE name = ? AND archived = 0 LIMIT 1;", (categoria,)).fetchone()
                if r_cat:
                    cat_id = r_cat["id"]
                else:
                    r_kind = conn.execute("SELECT id FROM categories WHERE kind = ? AND archived = 0 ORDER BY id LIMIT 1;", (tipo,)).fetchone()
                    if r_kind:
                        cat_id = r_kind["id"]

            fluxo_db.add_transaction(
                descricao=descricao,
                valor=valor,
                tipo=tipo,
                categoria_id=cat_id,
                conta_id=conta_id,
                data_transacao=dt.date(),
                observacao=observacao,
                import_key=f"telegram_{int(datetime.now().timestamp())}",
            )
            log.info("Lançamento (%s R$ %.2f) gravado diretamente no SQLite do Fluxo (B:\\Finanças\\fluxo.db)", tipo, valor)
        except Exception as err:
            log.warning(f"Erro ao gravar no fluxo.db: {err}")

        # 7. Sincronizar snapshot do app Fluxo (Android)
        try:
            from app.finance.fluxo_exporter import build_fluxo_backup
            build_fluxo_backup()
            log.info("Backup do app Fluxo (fluxo-backup-latest.json) sincronizado com sucesso.")
        except Exception as err:
            log.warning(f"Erro ao sincronizar snapshot do Fluxo: {err}")

        return audit_entry

    record_transaction = record_expense

    def _update_gastos_sheet(self, wb, dt, mes_nome, descricao, categoria, forma, valor, observacao):
        """Insere o lançamento na tabela do Orçamento 2026 da aba Gastos."""
        if "Gastos" not in wb.sheetnames:
            return
        ws = wb["Gastos"]

        # Localizar onde inserir dentro do mês ou no final da tabela
        target_row = None
        last_month_row = None

        for r in range(45, ws.max_row + 1):
            cell_mes = ws.cell(r, 3).value
            if cell_mes == mes_nome:
                last_month_row = r
            elif last_month_row and cell_mes != mes_nome:
                target_row = r
                break

        if not target_row:
            target_row = (last_month_row + 1) if last_month_row else 170

        ws.insert_rows(target_row)

        ws.cell(target_row, 2, dt)
        ws.cell(target_row, 3, mes_nome)
        ws.cell(target_row, 4, descricao)
        ws.cell(target_row, 5, categoria)
        ws.cell(target_row, 6, forma)
        ws.cell(target_row, 7, valor)
        ws.cell(target_row, 8, "Pago")
        ws.cell(target_row, 9, observacao)

        # Copiar formatação da linha de referência (target_row - 1)
        ref_row = target_row - 1
        for col in range(2, 10):
            _copy_cell_style(ws.cell(ref_row, col), ws.cell(target_row, col))
        
        # Formatos específicos
        ws.cell(target_row, 2).number_format = 'DD/MM/YYYY'
        ws.cell(target_row, 7).number_format = 'R$ #,##0.00;[Red]-R$ #,##0.00'

    def _update_extrato_sheet(self, wb, dt, banco, descricao, beneficiario, categoria, valor):
        """Acrescenta o lançamento no extrato detalhado e ajusta subtotais."""
        if "Extrato" not in wb.sheetnames:
            return
        ws = wb["Extrato"]
        new_row = ws.max_row + 1

        desc_fav = f"{descricao} ({beneficiario})" if beneficiario and beneficiario.lower() not in descricao.lower() else descricao

        ws.cell(new_row, 2, dt)
        ws.cell(new_row, 3, banco)
        ws.cell(new_row, 4, "Pix")
        ws.cell(new_row, 5, desc_fav)
        ws.cell(new_row, 6, categoria)
        ws.cell(new_row, 7, None)
        ws.cell(new_row, 8, valor)
        ws.cell(new_row, 9, None)

        ref_row = new_row - 1
        for col in range(2, 10):
            _copy_cell_style(ws.cell(ref_row, col), ws.cell(new_row, col))

        ws.cell(new_row, 2).number_format = 'DD/MM/YYYY'
        ws.cell(new_row, 8).number_format = 'R$ #,##0.00;[Red]-R$ #,##0.00'

        # Atualizar fórmulas de subtotal na linha 32
        ws['G32'] = f'=SUBTOTAL(109, G34:G{new_row})'
        ws['H32'] = f'=SUBTOTAL(109, H34:H{new_row})'

        # Atualizar título na linha 1
        count = new_row - 34 + 1
        ws['B1'] = f'Extratos Bancários Consolidados ({count:,} Transações)'.replace(",", ".")

    def _append_to_obsidian(self, entry: Dict[str, Any]) -> None:
        """Registra a despesa na nota Finanças.md do Obsidian Vault."""
        try:
            if not OBSIDIAN_FINANCAS.exists():
                return
            content = OBSIDIAN_FINANCAS.read_text(encoding="utf-8")
            log_line = f"- **{entry['data']}**: R$ {entry['valor']:.2f} — {entry['descricao']} ({entry['categoria']}, via {entry['forma']} {entry['banco']})"
            
            section_header = "### 📱 Últimos Gastos Lançados via Telegram"
            if section_header in content:
                content = content.replace(section_header, f"{section_header}\n{log_line}")
            else:
                content += f"\n\n{section_header}\n{log_line}\n"
            OBSIDIAN_FINANCAS.write_text(content, encoding="utf-8")
        except Exception as exc:
            log.warning("Falha ao registrar despesa no Obsidian: %s", exc)
