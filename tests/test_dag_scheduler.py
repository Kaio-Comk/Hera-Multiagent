import pytest
import asyncio
from models.task import AgentTask, AgentResult, TaskStatus
from app.state_store import StateStore
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.dag_scheduler import TaskDAG, DAGScheduler, CycleDetectedError, DependencyNotFoundError
from adapters.ollama import OllamaAdapter
from adapters.base import AgentAdapter

class MockAdapter:
    def __init__(self, name: str = "mock", delay: float = 0.05, should_fail: bool = False):
        self._name = name
        self.delay = delay
        self.should_fail = should_fail

    @property
    def name(self) -> str:
        return self._name

    async def health_check(self):
        pass

    async def capabilities(self):
        pass

    async def submit(self, task: AgentTask):
        from models.agent import RunHandle
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        await asyncio.sleep(self.delay)
        if self.should_fail:
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.FAILED,
                summary="Mock failure"
            )
        return AgentResult(
            task_id=handle.task_id,
            status=TaskStatus.SUCCEEDED,
            summary="Mock success"
        )

    async def cancel(self, handle):
        return True

def test_dag_validation_success():
    dag = TaskDAG()
    t1 = AgentTask(id="t1", title="T1", instruction="i1")
    t2 = AgentTask(id="t2", title="T2", instruction="i2", dependencies=["t1"])
    t3 = AgentTask(id="t3", title="T3", instruction="i3", dependencies=["t2"])

    dag.add_task(t1)
    dag.add_task(t2)
    dag.add_task(t3)

    # Não deve lançar exceção
    dag.validate()

def test_dag_cycle_detection():
    dag = TaskDAG()
    t1 = AgentTask(id="t1", title="T1", instruction="i1", dependencies=["t3"])
    t2 = AgentTask(id="t2", title="T2", instruction="i2", dependencies=["t1"])
    t3 = AgentTask(id="t3", title="T3", instruction="i3", dependencies=["t2"])

    dag.add_task(t1)
    dag.add_task(t2)
    dag.add_task(t3)

    with pytest.raises(CycleDetectedError):
        dag.validate()

def test_dag_missing_dependency():
    dag = TaskDAG()
    t1 = AgentTask(id="t1", title="T1", instruction="i1", dependencies=["inexistente"])
    dag.add_task(t1)

    with pytest.raises(DependencyNotFoundError):
        dag.validate()

@pytest.mark.asyncio
async def test_dag_parallel_execution(tmp_path):
    db_path = str(tmp_path / "dag_test.db")
    store = StateStore(db_path=db_path)
    coord = Coordinator(store=store)

    adapter = MockAdapter(delay=0.1)
    router = TaskRouter(adapters={"mock": adapter})

    notifications = []
    scheduler = DAGScheduler(
        router=router,
        coordinator=coord,
        max_concurrency=2,
        on_feedback=lambda msg: notifications.append(msg)
    )

    dag = TaskDAG()
    # Tarefas t1 e t2 podem rodar em paralelo
    t1 = AgentTask(id="t1", title="T1", instruction="i1", assigned_agent="mock")
    t2 = AgentTask(id="t2", title="T2", instruction="i2", assigned_agent="mock")
    # Tarefa t3 depende de ambas
    t3 = AgentTask(id="t3", title="T3", instruction="i3", dependencies=["t1", "t2"], assigned_agent="mock")

    dag.add_task(t1)
    dag.add_task(t2)
    dag.add_task(t3)

    results = await scheduler.execute_dag(dag)

    assert len(results) == 3
    assert results["t1"].status == TaskStatus.SUCCEEDED
    assert results["t2"].status == TaskStatus.SUCCEEDED
    assert results["t3"].status == TaskStatus.SUCCEEDED

    # Verificar persistência no banco SQLite
    assert store.get_task("t1").status == TaskStatus.SUCCEEDED
    assert store.get_task("t2").status == TaskStatus.SUCCEEDED
    assert store.get_task("t3").status == TaskStatus.SUCCEEDED

@pytest.mark.asyncio
async def test_dag_cascade_cancellation_on_failure(tmp_path):
    db_path = str(tmp_path / "dag_fail_test.db")
    store = StateStore(db_path=db_path)
    coord = Coordinator(store=store)

    adapter = MockAdapter(should_fail=True)
    router = TaskRouter(adapters={"mock": adapter})

    scheduler = DAGScheduler(router=router, coordinator=coord, max_concurrency=2)

    dag = TaskDAG()
    t1 = AgentTask(id="t-fail", title="Fail", instruction="fail", assigned_agent="mock")
    t2 = AgentTask(id="t-dep", title="Dependent", instruction="wait", dependencies=["t-fail"], assigned_agent="mock")

    dag.add_task(t1)
    dag.add_task(t2)

    results = await scheduler.execute_dag(dag)

    assert results["t-fail"].status == TaskStatus.FAILED
    assert results["t-dep"].status == TaskStatus.CANCELLED
