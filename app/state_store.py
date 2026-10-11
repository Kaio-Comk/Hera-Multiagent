import sqlite3
import json
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from models.task import AgentTask, TaskStatus

class StateStore:
    """Persistência determinística em SQLite para controle de fila e histórico."""

    def __init__(self, db_path: str = "B:/multiagent-orchestrator/data/orchestrator.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    title TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    workspace TEXT,
                    status TEXT NOT NULL,
                    dependencies TEXT,
                    timeout_seconds INTEGER,
                    max_retries INTEGER,
                    retry_count INTEGER,
                    assigned_agent TEXT,
                    assigned_model TEXT,
                    acceptance_criteria TEXT,
                    validation_commands TEXT,
                    routing_reason TEXT,
                    created_at REAL,
                    updated_at REAL,
                    completed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT,
                    timestamp REAL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )
            """)
            conn.commit()

    def create_task(self, task: AgentTask) -> AgentTask:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO tasks (
                    id, parent_id, title, instruction, task_type, workspace,
                    status, dependencies, timeout_seconds, max_retries,
                    retry_count, assigned_agent, assigned_model,
                    acceptance_criteria, validation_commands, routing_reason,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.id,
                task.parent_id,
                task.title,
                task.instruction,
                task.task_type,
                task.workspace,
                task.status.value,
                json.dumps(task.dependencies),
                task.timeout_seconds,
                task.max_retries,
                task.retry_count,
                task.assigned_agent,
                task.assigned_model,
                json.dumps(task.acceptance_criteria),
                json.dumps(task.validation_commands),
                task.routing_reason,
                task.created_at,
                task.updated_at,
                task.completed_at
            ))
            conn.commit()
        self.record_event(task.id, "TASK_CREATED", f"Tarefa {task.id} criada com status {task.status.value}")
        return task

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        assigned_agent: Optional[str] = None,
        assigned_model: Optional[str] = None,
        routing_reason: Optional[str] = None,
        retry_count: Optional[int] = None
    ) -> bool:
        now = time.time()
        completed_at = now if status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED) else None

        fields = ["status = ?", "updated_at = ?"]
        values: List[Any] = [status.value, now]

        if completed_at is not None:
            fields.append("completed_at = ?")
            values.append(completed_at)
        if assigned_agent is not None:
            fields.append("assigned_agent = ?")
            values.append(assigned_agent)
        if assigned_model is not None:
            fields.append("assigned_model = ?")
            values.append(assigned_model)
        if routing_reason is not None:
            fields.append("routing_reason = ?")
            values.append(routing_reason)
        if retry_count is not None:
            fields.append("retry_count = ?")
            values.append(retry_count)

        values.append(task_id)

        query = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"

        with self._get_connection() as conn:
            cursor = conn.execute(query, values)
            conn.commit()
            updated = cursor.rowcount > 0

        if updated:
            self.record_event(task_id, "STATUS_CHANGED", f"Status alterado para {status.value}")
        return updated

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def list_tasks(self, status: Optional[TaskStatus] = None, limit: Optional[int] = None) -> List[AgentTask]:
        with self._get_connection() as conn:
            limit_clause = f" LIMIT {int(limit)}" if limit else ""
            if status:
                query = f"SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC{limit_clause}"
                rows = conn.execute(query, (status.value,)).fetchall()
            else:
                query = f"SELECT * FROM tasks ORDER BY created_at DESC{limit_clause}"
                rows = conn.execute(query).fetchall()
            return [self._row_to_task(r) for r in rows]

    def record_event(self, task_id: str, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO events (task_id, event_type, message, payload, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id,
                event_type,
                message,
                json.dumps(payload or {}, ensure_ascii=False),
                time.time()
            ))
            conn.commit()

    def get_events(self, task_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()
            return [
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "event_type": r["event_type"],
                    "message": r["message"],
                    "payload": json.loads(r["payload"]) if r["payload"] else {},
                    "timestamp": r["timestamp"]
                }
                for r in rows
            ]

    def recover_interrupted_tasks(self) -> List[str]:
        """Recupera tarefas deixadas em RUNNING ou VALIDATING após encerramento inesperado em transação atômica."""
        recovered_ids: List[str] = []
        now = time.time()
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, title FROM tasks WHERE status IN (?, ?)",
                (TaskStatus.RUNNING.value, TaskStatus.VALIDATING.value)
            ).fetchall()
            for r in rows:
                task_id = r["id"]
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                    (TaskStatus.FAILED.value, now, now, task_id)
                )
                conn.execute(
                    """
                    INSERT INTO events (task_id, event_type, message, payload, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        "CRASH_RECOVERY",
                        "Tarefa marcada como FAILED pela rotina de recuperação atômica após encerramento inesperado do orquestrador.",
                        json.dumps({"recovery_timestamp": now, "previous_status": "ORPHANED_RUNNING"}),
                        now
                    )
                )
                recovered_ids.append(task_id)
            conn.commit()
        return recovered_ids

    def _row_to_task(self, row: sqlite3.Row) -> AgentTask:
        return AgentTask(
            id=row["id"],
            parent_id=row["parent_id"],
            title=row["title"],
            instruction=row["instruction"],
            task_type=row["task_type"],
            workspace=row["workspace"] or "",
            status=TaskStatus(row["status"]),
            dependencies=json.loads(row["dependencies"] or "[]"),
            timeout_seconds=row["timeout_seconds"],
            max_retries=row["max_retries"],
            retry_count=row["retry_count"],
            assigned_agent=row["assigned_agent"],
            assigned_model=row["assigned_model"],
            acceptance_criteria=json.loads(row["acceptance_criteria"] or "[]"),
            validation_commands=json.loads(row["validation_commands"] or "[]"),
            routing_reason=row["routing_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"]
        )
