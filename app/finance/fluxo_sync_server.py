"""Servidor REST de Sincronização Sem Fio do Fluxo (Hera Fluxo Sync).

Executa em segundo plano no PC (porta 8765), permitindo que o app Fluxo no celular
e qualquer IA no computador leiam e sincronizem dados de forma 100% sem fio.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.finance.fluxo_db import FluxoDatabase, fluxo_db, from_epoch_day, to_epoch_day

log = logging.getLogger("hera.finance.sync_server")

app = FastAPI(
    title="Hera Fluxo Sync API",
    description="API de Sincronização Sem Fio entre o app Android Fluxo e o PC",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TransactionIn(BaseModel):
    descricao: str
    valor: float
    tipo: str = "EXPENSE"
    categoria_id: Optional[int] = None
    conta_id: int = 1
    data: Optional[str] = None  # YYYY-MM-DD
    observacao: str = ""
    import_key: Optional[str] = None


@app.get("/health")
@app.get("/api/fluxo/ping")
def ping():
    return {
        "status": "ok",
        "service": "Hera Fluxo Wireless Sync",
        "timestamp": int(datetime.now().timestamp() * 1000),
        "db": "connected",
    }


@app.get("/api/fluxo/summary")
def get_summary():
    """Retorna saldo em contas, contagem e últimos lançamentos."""
    return fluxo_db.get_summary()


@app.get("/api/fluxo/snapshot")
def get_snapshot():
    """Retorna o snapshot completo (v3) para restauração com 1 toque no app."""
    return fluxo_db.export_snapshot()


@app.post("/api/fluxo/snapshot")
def restore_snapshot(payload: Dict[str, Any]):
    """Recebe um snapshot do celular e atualiza o banco do PC."""
    temp_path = Path(r"B:\Finanças\Extratos\temp_sync_snapshot.json")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    counts = fluxo_db.populate_from_snapshot(temp_path)
    if temp_path.exists():
        temp_path.unlink()
    return {"status": "success", "counts": counts}


@app.get("/api/fluxo/transactions")
def list_transactions(limit: int = 50, since: Optional[int] = None):
    """Lista transações do banco, opcionalmente filtradas por data de modificação/criação."""
    with fluxo_db.get_connection() as conn:
        cursor = conn.cursor()
        if since:
            rows = cursor.execute(
                """SELECT t.*, c.name as categoria, a.name as conta
                   FROM transactions t
                   LEFT JOIN categories c ON t.categoryId = c.id
                   LEFT JOIN accounts a ON t.accountId = a.id
                   WHERE t.createdAt > ?
                   ORDER BY t.date DESC, t.id DESC LIMIT ?;""",
                (since, limit),
            ).fetchall()
        else:
            rows = cursor.execute(
                """SELECT t.*, c.name as categoria, a.name as conta
                   FROM transactions t
                   LEFT JOIN categories c ON t.categoryId = c.id
                   LEFT JOIN accounts a ON t.accountId = a.id
                   ORDER BY t.date DESC, t.id DESC LIMIT ?;""",
                (limit,),
            ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["data_formatada"] = from_epoch_day(d["date"])
        d["valor_reais"] = d["amountCents"] / 100.0
        result.append(d)
    return result


@app.post("/api/fluxo/transactions")
def add_transaction(item: TransactionIn):
    """Adiciona uma transação vinda do app ou de qualquer IA."""
    dt = datetime.strptime(item.data, "%Y-%m-%d").date() if item.data else None
    new_id = fluxo_db.add_transaction(
        descricao=item.descricao,
        valor=item.valor,
        tipo=item.tipo,
        categoria_id=item.categoria_id,
        conta_id=item.conta_id,
        data_transacao=dt,
        observacao=item.observacao,
        import_key=item.import_key,
    )
    return {"status": "created", "id": new_id, "descricao": item.descricao, "valor": item.valor}


def start_server(host: str = "0.0.0.0", port: int = 8765):
    """Inicia o servidor Uvicorn."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_server()
