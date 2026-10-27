import pytest
from pydantic import ValidationError
from models.plan_contract import PlanContractModel, PlanStepModel


def test_plan_contract_valido():
    plan_data = {
        "strategy": "balanced",
        "steps": [
            {
                "id": "s1",
                "title": "Investigar requisitos",
                "instruction": "Ler o README e entender o problema",
                "type": "research",
                "engine": "antigravity",
                "model": "gemini-2.5-flash",
                "depends_on": []
            },
            {
                "id": "s2",
                "title": "Implementar correcao",
                "instruction": "Modificar arquivos alvo e corrigir bug",
                "type": "coding",
                "engine": "hera_router",
                "model": "deepseek-ai/DeepSeek-V4.1-Flash",
                "depends_on": ["s1"],
                "acceptance_criteria": ["Testes passam com sucesso"],
                "validation_commands": ["pytest tests/test_core.py"]
            }
        ]
    }
    model = PlanContractModel.model_validate(plan_data)
    assert len(model.steps) == 2
    assert model.steps[0].id == "s1"
    assert model.steps[1].depends_on == ["s1"]


def test_plan_contract_rejeita_id_duplicado():
    plan_data = {
        "steps": [
            {"id": "s1", "title": "Passo 1", "instruction": "Fazer A", "type": "research", "engine": "ollama"},
            {"id": "s1", "title": "Passo 1 repetido", "instruction": "Fazer B", "type": "coding", "engine": "ollama"}
        ]
    }
    with pytest.raises(ValidationError) as exc:
        PlanContractModel.model_validate(plan_data)
    assert "ID de passo duplicado" in str(exc.value)


def test_plan_contract_rejeita_auto_dependencia():
    plan_data = {
        "steps": [
            {"id": "s1", "title": "Passo 1", "instruction": "Fazer A", "type": "research", "engine": "ollama", "depends_on": ["s1"]}
        ]
    }
    with pytest.raises(ValidationError) as exc:
        PlanContractModel.model_validate(plan_data)
    assert "nao pode depender de si mesmo" in str(exc.value)


def test_plan_contract_rejeita_dependencia_inexistente():
    plan_data = {
        "steps": [
            {"id": "s1", "title": "Passo 1", "instruction": "Fazer A", "type": "research", "engine": "ollama", "depends_on": ["fantasma"]}
        ]
    }
    with pytest.raises(ValidationError) as exc:
        PlanContractModel.model_validate(plan_data)
    assert "depende do id 'fantasma', que nao existe" in str(exc.value)


def test_plan_contract_rejeita_ciclo():
    plan_data = {
        "steps": [
            {"id": "s1", "title": "Passo 1", "instruction": "Fazer A", "type": "research", "engine": "ollama", "depends_on": ["s2"]},
            {"id": "s2", "title": "Passo 2", "instruction": "Fazer B", "type": "coding", "engine": "ollama", "depends_on": ["s1"]}
        ]
    }
    with pytest.raises(ValidationError) as exc:
        PlanContractModel.model_validate(plan_data)
    assert "Ciclo fechado detectado" in str(exc.value)


def test_plan_contract_rejeita_pipe_em_validacao():
    plan_data = {
        "steps": [
            {
                "id": "s1",
                "title": "Passo 1",
                "instruction": "Fazer A",
                "type": "coding",
                "engine": "ollama",
                "validation_commands": ["pytest | rm -rf /"]
            }
        ]
    }
    with pytest.raises(ValidationError) as exc:
        PlanContractModel.model_validate(plan_data)
    assert "caracteres perigosos" in str(exc.value)
