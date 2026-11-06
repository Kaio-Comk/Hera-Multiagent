"""Ferramenta para qualquer I.A. ou script ler e manipular o app Fluxo no PC.

Pode ser executada via linha de comando ou importada diretamente em Python:
    from tools.fluxo_tool import FluxoTool
    tool = FluxoTool()
    print(tool.get_summary())
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(r"B:\Finanças\fluxo.db")


class FluxoTool:
    """Interface unificada para leitura e escrita no banco do Fluxo no PC."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        if not self.db_path.exists():
            from app.finance.fluxo_db import fluxo_db
            self.db = fluxo_db
        else:
            from app.finance.fluxo_db import FluxoDatabase
            self.db = FluxoDatabase(self.db_path)

    def get_summary(self) -> Dict[str, Any]:
        """Retorna o resumo executivo financeiro."""
        return self.db.get_summary()

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Executa uma consulta SQL arbitrária no banco de dados do Fluxo."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()]

    def add_expense(
        self,
        descricao: str,
        valor: float,
        categoria: Optional[str] = None,
        conta: str = "InfinitePay",
        observacao: str = "",
    ) -> int:
        """Adiciona uma nova despesa diretamente no banco do Fluxo."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            # Mapear categoria se fornecida
            cat_id = None
            if categoria:
                row = cursor.execute(
                    "SELECT id FROM categories WHERE LOWER(name) LIKE LOWER(?) LIMIT 1;",
                    (f"%{categoria.strip()}%",),
                ).fetchone()
                if row:
                    cat_id = row[0]

            # Mapear conta
            acc_id = 1
            row_acc = cursor.execute(
                "SELECT id FROM accounts WHERE LOWER(name) LIKE LOWER(?) LIMIT 1;",
                (f"%{conta.strip()}%",),
            ).fetchone()
            if row_acc:
                acc_id = row_acc[0]

        return self.db.add_transaction(
            descricao=descricao,
            valor=valor,
            tipo="EXPENSE",
            categoria_id=cat_id,
            conta_id=acc_id,
            observacao=observacao,
        )


def main():
    parser = argparse.ArgumentParser(description="Fluxo Tool para I.A.s e terminal")
    subparsers = parser.add_subparsers(dest="command")

    # summary
    subparsers.add_parser("summary", help="Exibe o resumo executivo financeiro")

    # query
    q_parser = subparsers.add_parser("query", help="Executa uma consulta SQL")
    q_parser.add_argument("sql", help="Consulta SQL a ser executada")

    # add
    add_parser = subparsers.add_parser("add", help="Adiciona uma despesa")
    add_parser.add_argument("descricao", help="Descrição do gasto")
    add_parser.add_argument("valor", type=float, help="Valor em reais (ex: 40.0)")
    add_parser.add_argument("--cat", default=None, help="Nome da categoria")
    add_parser.add_argument("--conta", default="InfinitePay", help="Nome da conta bancária")
    add_parser.add_argument("--obs", default="", help="Observação")

    args = parser.parse_args()
    tool = FluxoTool()

    if args.command == "summary" or not args.command:
        s = tool.get_summary()
        print(f"💰 Saldo Total: R$ {s['total_saldo']:.2f}")
        print(f"📊 Total de Lançamentos: {s['total_transacoes']}")
        print("\n🏦 Contas:")
        for a in s["contas"]:
            print(f"  • {a['nome']}: R$ {a['saldo']:.2f}")
        print("\n🧾 Últimas Transações:")
        for t in s["ultimas_transacoes"]:
            print(f"  • {t['data']} | R$ {t['valor']:.2f} | {t['descricao']} ({t['categoria']})")

    elif args.command == "query":
        rows = tool.query(args.sql)
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    elif args.command == "add":
        new_id = tool.add_expense(args.descricao, args.valor, args.cat, args.conta, args.obs)
        print(f"✅ Despesa adicionada com sucesso ao Fluxo! ID: {new_id} ({args.descricao} — R$ {args.valor:.2f})")


if __name__ == "__main__":
    main()
