"""Gerenciador e Banco de Dados Central do Fluxo no PC (SQLite).

Permite que qualquer IA neste computador (Hera, Antigravity, Claude, scripts)
leia e escreva dados diretamente no Fluxo sem precisar de cabos ou planilhas.
Compatível 100% com o schema do Room v3 do app Android Fluxo.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("hera.finance.fluxo_db")

SCHEMA_JSON = Path(r"B:\Projetos\Mobills REA\reconstrucao_pessoal\app\schemas\com.pessoal.fluxo.data.database.AppDatabase\3.json")
DB_PATH = Path(r"B:\Finanças\fluxo.db")
BACKUP_SNAPSHOT = Path(r"B:\Finanças\Extratos\fluxo-backup-latest.json")

EPOCH = date(1970, 1, 1)


def to_epoch_day(d: Any) -> int:
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    return (d - EPOCH).days


def from_epoch_day(days: int) -> str:
    return date.fromordinal(date(1970, 1, 1).toordinal() + days).isoformat()


class FluxoDatabase:
    """Banco de dados SQLite do Fluxo no PC, com esquema idêntico ao Room v3."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_database()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_database(self) -> None:
        """Cria as tabelas e índices oficiais do Room v3 se ainda não existirem."""
        if not SCHEMA_JSON.exists():
            log.warning("Arquivo de esquema Room não encontrado: %s", SCHEMA_JSON)
            return

        with open(SCHEMA_JSON, "r", encoding="utf-8") as f:
            schema_data = json.load(f)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for entity in schema_data.get("database", {}).get("entities", []):
                tname = entity["tableName"]
                create_sql = entity["createSql"].replace("${TABLE_NAME}", tname)
                cursor.execute(create_sql)

                for idx in entity.get("indices", []):
                    idx_sql = idx["createSql"].replace("${TABLE_NAME}", tname)
                    cursor.execute(idx_sql)

            # Tabela de metadados do Room
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS room_master_table (id INTEGER PRIMARY KEY, identity_hash TEXT);"
            )
            cursor.execute(
                "INSERT OR REPLACE INTO room_master_table (id, identity_hash) VALUES (42, ?);",
                (schema_data.get("database", {}).get("identityHash", "3"),),
            )
            conn.commit()

    def populate_from_snapshot(self, snapshot_path: Path = BACKUP_SNAPSHOT) -> Dict[str, int]:
        """Carrega todos os dados do JSON de snapshot para o banco SQLite."""
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot não encontrado: {snapshot_path}")

        with open(snapshot_path, "r", encoding="utf-8") as f:
            s = json.load(f)

        counts = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Desativa foreign keys temporariamente para inserção limpa em ordem
            cursor.execute("PRAGMA foreign_keys = OFF;")

            # Limpar tabelas
            tables = [
                "transaction_tags", "purchase_tags", "card_installments", "card_purchases",
                "loan_installments", "loans", "debts", "ride_earnings", "notes",
                "goal_contributions", "goals", "budgets", "fixed_rules", "favorites",
                "monthly_limits", "custom_budgets", "transactions", "credit_cards",
                "categories", "accounts", "tags"
            ]
            for t in tables:
                cursor.execute(f"DELETE FROM {t};")

            # Contas
            for a in s.get("accounts", []):
                cursor.execute(
                    """INSERT INTO accounts (id, name, type, initialBalanceCents, colorArgb, archived, createdAt, includeInTotal)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                    (a["id"], a["name"], a["type"], a["initialBalanceCents"], a["colorArgb"],
                     1 if a["archived"] else 0, a["createdAt"], 1 if a.get("includeInTotal", True) else 0)
                )
            counts["accounts"] = len(s.get("accounts", []))

            # Categorias (pais primeiro, depois filhas)
            cats = s.get("categories", [])
            for c in sorted(cats, key=lambda x: 1 if x.get("parentId") else 0):
                cursor.execute(
                    """INSERT INTO categories (id, name, kind, parentId, colorArgb, archived)
                       VALUES (?, ?, ?, ?, ?, ?);""",
                    (c["id"], c["name"], c["kind"], c.get("parentId"), c["colorArgb"], 1 if c["archived"] else 0)
                )
            counts["categories"] = len(cats)

            # Cartões
            for card in s.get("cards", []):
                cursor.execute(
                    """INSERT INTO credit_cards (id, name, limitCents, closingDay, dueDay, colorArgb, defaultPaymentAccountId, archived)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                    (card["id"], card["name"], card["limitCents"], card["closingDay"], card["dueDay"],
                     card["colorArgb"], card.get("defaultPaymentAccountId"), 1 if card["archived"] else 0)
                )
            counts["cards"] = len(s.get("cards", []))

            # Transações
            for t in s.get("transactions", []):
                cursor.execute(
                    """INSERT INTO transactions (
                           id, type, amountCents, date, description, notes, accountId, toAccountId,
                           categoryId, isPaid, excludeFromReports, recurrenceGroupId, cardId, invoiceMonth,
                           createdAt, fixedRuleId, currency, originalAmountCents, latitude, longitude, importKey
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (t["id"], t["type"], t["amountCents"], t["date"], t["description"], t.get("notes", ""),
                     t["accountId"], t.get("toAccountId"), t.get("categoryId"), 1 if t.get("isPaid", True) else 0,
                     1 if t.get("excludeFromReports", False) else 0, t.get("recurrenceGroupId"),
                     t.get("cardId"), t.get("invoiceMonth"), t.get("createdAt", int(datetime.now().timestamp() * 1000)),
                     t.get("fixedRuleId"), t.get("currency", "BRL"), t.get("originalAmountCents"),
                     t.get("latitude"), t.get("longitude"), t.get("importKey"))
                )
            counts["transactions"] = len(s.get("transactions", []))

            # Financiamentos e parcelas
            for l in s.get("loans", []):
                cursor.execute(
                    """INSERT INTO loans (id, name, lender, contractNumber, principalCents, installmentCount,
                                          installmentCents, firstDueDate, monthlyRateBp, cetBp, accountId, categoryId,
                                          notes, archived, createdAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (l["id"], l["name"], l.get("lender", ""), l.get("contractNumber", ""), l.get("principalCents"),
                     l["installmentCount"], l["installmentCents"], l["firstDueDate"], l.get("monthlyRateBp"),
                     l.get("cetBp"), l.get("accountId"), l.get("categoryId"), l.get("notes", ""),
                     1 if l.get("archived", False) else 0, l.get("createdAt", 0))
                )
            for li in s.get("loanInstallments", []):
                cursor.execute(
                    """INSERT INTO loan_installments (id, loanId, number, dueDate, nominalCents, paidDate,
                                                      paidCents, quoteCents, quoteDate, transactionId)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (li["id"], li["loanId"], li["number"], li["dueDate"], li["nominalCents"], li.get("paidDate"),
                     li.get("paidCents"), li.get("quoteCents"), li.get("quoteDate"), li.get("transactionId"))
                )
            counts["loans"] = len(s.get("loans", []))

            # Dívidas
            for d in s.get("debts", []):
                cursor.execute(
                    """INSERT INTO debts (id, creditor, contractNumber, originalDueDate, amountCents, negativado,
                                          offerCents, offerCondition, offerInstallments, offerInstallmentCents,
                                          offerValidUntil, status, notes, paidDate, transactionId, createdAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (d["id"], d["creditor"], d.get("contractNumber", ""), d.get("originalDueDate"), d["amountCents"],
                     1 if d.get("negativado", True) else 0, d.get("offerCents"), d.get("offerCondition", ""),
                     d.get("offerInstallments"), d.get("offerInstallmentCents"), d.get("offerValidUntil"),
                     d["status"], d.get("notes", ""), d.get("paidDate"), d.get("transactionId"), d.get("createdAt", 0))
                )
            counts["debts"] = len(s.get("debts", []))

            # Metas
            for g in s.get("goals", []):
                cursor.execute(
                    """INSERT INTO goals (id, name, description, targetCents, deadline, colorArgb, status, createdAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                    (g["id"], g["name"], g.get("description", ""), g["targetCents"], g.get("deadline"),
                     g["colorArgb"], g["status"], g.get("createdAt", 0))
                )
            counts["goals"] = len(s.get("goals", []))

            # Ganhos de Aplicativo
            for r in s.get("rideEarnings", []):
                cursor.execute(
                    """INSERT INTO ride_earnings (id, platform, startDate, endDate, rides, distanceKm, grossCents,
                                                  feesCents, netCents, notes, transactionId, createdAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (r["id"], r["platform"], r["startDate"], r["endDate"], r.get("rides"), r.get("distanceKm"),
                     r.get("grossCents"), r.get("feesCents", 0), r["netCents"], r.get("notes", ""),
                     r.get("transactionId"), r.get("createdAt", 0))
                )
            counts["ride_earnings"] = len(s.get("rideEarnings", []))

            # Notas
            for n in s.get("notes", []):
                cursor.execute(
                    """INSERT INTO notes (id, title, body, status, pinned, createdAt, updatedAt)
                       VALUES (?, ?, ?, ?, ?, ?, ?);""",
                    (n["id"], n["title"], n.get("body", ""), n["status"], 1 if n.get("pinned", False) else 0,
                     n.get("createdAt", 0), n.get("updatedAt", 0))
                )
            counts["notes"] = len(s.get("notes", []))

            cursor.execute("PRAGMA foreign_keys = ON;")
            conn.commit()

        return counts

    def add_transaction(
        self,
        descricao: str,
        valor: float,
        tipo: str = "EXPENSE",
        categoria_id: Optional[int] = None,
        conta_id: int = 1,
        data_transacao: Optional[date] = None,
        observacao: str = "",
        import_key: Optional[str] = None,
    ) -> int:
        """Adiciona uma transação diretamente no SQLite do Fluxo no PC."""
        d = data_transacao or date.today()
        epoch_day = to_epoch_day(d)
        amount_cents = int(round(valor * 100))
        now_ms = int(datetime.now().timestamp() * 1000)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO transactions (
                       type, amountCents, date, description, notes, accountId, toAccountId,
                       categoryId, isPaid, excludeFromReports, createdAt, currency, importKey
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1, 0, ?, 'BRL', ?);""",
                (tipo, amount_cents, epoch_day, descricao.strip(), observacao.strip(), conta_id,
                 categoria_id, now_ms, import_key)
            )
            conn.commit()
            new_id = cursor.lastrowid

        # Atualiza o arquivo de snapshot para manter a sincronização viva
        self.export_snapshot()
        return new_id

    def export_snapshot(self, output_path: Path = BACKUP_SNAPSHOT) -> Dict[str, Any]:
        """Exporta o banco SQLite atual para o formato JSON do Fluxo."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            accounts = [dict(r) for r in cursor.execute("SELECT * FROM accounts ORDER BY id;").fetchall()]
            for a in accounts:
                a["archived"] = bool(a["archived"])
                a["includeInTotal"] = bool(a.get("includeInTotal", 1))

            categories = [dict(r) for r in cursor.execute("SELECT * FROM categories ORDER BY id;").fetchall()]
            for c in categories:
                c["archived"] = bool(c["archived"])

            cards = [dict(r) for r in cursor.execute("SELECT * FROM credit_cards ORDER BY id;").fetchall()]
            for c in cards:
                c["archived"] = bool(c["archived"])

            transactions = [dict(r) for r in cursor.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC;").fetchall()]
            for t in transactions:
                t["isPaid"] = bool(t["isPaid"])
                t["excludeFromReports"] = bool(t["excludeFromReports"])

            loans = [dict(r) for r in cursor.execute("SELECT * FROM loans ORDER BY id;").fetchall()]
            for l in loans:
                l["archived"] = bool(l["archived"])

            loan_installments = [dict(r) for r in cursor.execute("SELECT * FROM loan_installments ORDER BY id;").fetchall()]
            debts = [dict(r) for r in cursor.execute("SELECT * FROM debts ORDER BY id;").fetchall()]
            for d in debts:
                d["negativado"] = bool(d["negativado"])

            goals = [dict(r) for r in cursor.execute("SELECT * FROM goals ORDER BY id;").fetchall()]
            ride_earnings = [dict(r) for r in cursor.execute("SELECT * FROM ride_earnings ORDER BY id;").fetchall()]
            notes = [dict(r) for r in cursor.execute("SELECT * FROM notes ORDER BY id;").fetchall()]
            for n in notes:
                n["pinned"] = bool(n["pinned"])

        snapshot = {
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

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        return snapshot

    def get_summary(self) -> Dict[str, Any]:
        """Retorna resumo financeiro executivo lido diretamente do banco SQLite."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Saldo por conta
            balances = cursor.execute("""
                SELECT a.id, a.name, a.type, a.initialBalanceCents,
                       COALESCE(SUM(CASE
                           WHEN t.type = 'INCOME' THEN t.amountCents
                           WHEN t.type = 'EXPENSE' THEN -t.amountCents
                           WHEN t.type = 'TRANSFER' AND t.accountId = a.id THEN -t.amountCents
                           WHEN t.type = 'TRANSFER' AND t.toAccountId = a.id THEN t.amountCents
                           ELSE 0 END), 0) as movementCents
                FROM accounts a
                LEFT JOIN transactions t ON (t.accountId = a.id OR t.toAccountId = a.id) AND t.isPaid = 1
                WHERE a.archived = 0
                GROUP BY a.id;
            """).fetchall()

            account_list = []
            total_balance_cents = 0
            for b in balances:
                current_cents = b["initialBalanceCents"] + b["movementCents"]
                account_list.append({
                    "id": b["id"],
                    "nome": b["name"],
                    "saldo": current_cents / 100.0,
                })
                total_balance_cents += current_cents

            # Total de transações
            total_tx = cursor.execute("SELECT COUNT(*) FROM transactions;").fetchone()[0]

            # Últimos 5 lançamentos
            recent = cursor.execute("""
                SELECT t.id, t.date, t.description, t.amountCents, t.type, c.name as categoria, a.name as conta
                FROM transactions t
                LEFT JOIN categories c ON t.categoryId = c.id
                LEFT JOIN accounts a ON t.accountId = a.id
                ORDER BY t.date DESC, t.id DESC
                LIMIT 5;
            """).fetchall()

            recent_tx = []
            for r in recent:
                recent_tx.append({
                    "id": r["id"],
                    "data": from_epoch_day(r["date"]),
                    "descricao": r["description"],
                    "valor": r["amountCents"] / 100.0,
                    "tipo": r["type"],
                    "categoria": r["categoria"] or "Outros",
                    "conta": r["conta"] or "Conta",
                })

        return {
            "total_saldo": total_balance_cents / 100.0,
            "contas": account_list,
            "total_transacoes": total_tx,
            "ultimas_transacoes": recent_tx,
        }


# Instância global singleton para uso imediato em qualquer módulo
fluxo_db = FluxoDatabase()

if __name__ == "__main__":
    counts = fluxo_db.populate_from_snapshot()
    print("✅ Banco SQLite do Fluxo (B:\\Finanças\\fluxo.db) criado com sucesso!")
    print("📊 Registros carregados:", counts)
    summary = fluxo_db.get_summary()
    print("\n💰 Resumo Executivo:")
    print(f"   • Saldo total em contas: R$ {summary['total_saldo']:.2f}")
    print(f"   • Total de transações: {summary['total_transacoes']}")
    print("   • Contas:")
    for a in summary["contas"]:
        print(f"     - {a['nome']}: R$ {a['saldo']:.2f}")
    print("\n   • Últimos lançamentos:")
    for tx in summary["ultimas_transacoes"]:
        print(f"     - {tx['data']} | R$ {tx['valor']:.2f} | {tx['descricao']} ({tx['categoria']})")
