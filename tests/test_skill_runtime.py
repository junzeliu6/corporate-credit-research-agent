"""V1.3 单 Agent Skill 运行时的离线测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic.v1 import BaseModel

from credit_research_agent.company import CompanyResolver
from credit_research_agent.skill_runtime import (
    SkillExecutionStage,
    SkillExecutionStatus,
    SkillRuntime,
    SkillRuntimeError,
    ToolRegistration,
)
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class _FakeArtifacts(BaseModel):
    result_path: Path
    citations_path: Path


def _identity_and_source():
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    return identity, source


def test_runtime_selects_loads_and_calls_actual_registered_callable(
    tmp_path: Path,
) -> None:
    calls = []

    def fake_ownership_tool(**kwargs):
        calls.append(kwargs)
        result_path = tmp_path / "ownership.json"
        citations_path = tmp_path / "citations.md"
        result_path.write_text("{}\n", encoding="utf-8")
        citations_path.write_text("[S01]\n", encoding="utf-8")
        return _FakeArtifacts(
            result_path=result_path,
            citations_path=citations_path,
        )

    runtime = SkillRuntime(
        PROJECT_ROOT,
        tools={
            "ownership-structure": ToolRegistration(
                name="build_ownership_artifacts",
                skill_name="ownership-structure",
                executor=fake_ownership_tool,
            )
        },
        clock=lambda: FIXED_TIME,
        trace_id_factory=lambda: "TR-TEST000001",
    )
    identity, source = _identity_and_source()
    trace_path = tmp_path / "skill_execution_trace.json"

    result = runtime.execute(
        task="生成股权结构图并核验持股比例",
        identity=identity,
        source=source,
        tool_arguments={"input_marker": "offline-test"},
        trace_output=trace_path,
    )

    assert len(calls) == 1
    assert calls[0]["identity"].ticker == "002603.SZ"
    assert calls[0]["source"].source_id == "S01"
    assert calls[0]["input_marker"] == "offline-test"
    assert result.selection.selected_skill == "ownership-structure"
    assert result.tool_name == "build_ownership_artifacts"
    assert result.trace.status is SkillExecutionStatus.SUCCEEDED
    assert [event.stage for event in result.trace.events] == [
        SkillExecutionStage.ENTITY_RESOLVED,
        SkillExecutionStage.SOURCE_VALIDATED,
        SkillExecutionStage.SKILL_SELECTED,
        SkillExecutionStage.SKILL_LOADED,
        SkillExecutionStage.TOOL_CALLED,
        SkillExecutionStage.STRUCTURED_RESULT_VALIDATED,
        SkillExecutionStage.OUTPUTS_GENERATED,
        SkillExecutionStage.COMPLETED,
    ]
    saved = json.loads(trace_path.read_text(encoding="utf-8"))
    assert saved["selected_skill"] == "ownership-structure"
    assert saved["tool_name"] == "build_ownership_artifacts"
    assert saved["events"][4]["stage"] == "tool_called"


def test_runtime_loads_and_executes_ready_financial_skill(
    tmp_path: Path,
) -> None:
    identity, source = _identity_and_source()
    trace_path = tmp_path / "financial_trace.json"
    calls = []

    def fake_financial_tool(**kwargs):
        calls.append(kwargs)
        result_path = tmp_path / "financial_facts.json"
        citations_path = tmp_path / "financial_sources.json"
        result_path.write_text("{}\n", encoding="utf-8")
        citations_path.write_text("{}\n", encoding="utf-8")
        return _FakeArtifacts(result_path=result_path, citations_path=citations_path)

    runtime = SkillRuntime(
        PROJECT_ROOT,
        tools={
            "financial-analysis": ToolRegistration(
                name="build_financial_artifacts",
                skill_name="financial-analysis",
                executor=fake_financial_tool,
            )
        },
        clock=lambda: FIXED_TIME,
        trace_id_factory=lambda: "TR-TEST000002",
    )

    result = runtime.execute(
        task="分析财务报表和现金流",
        identity=identity,
        source=source,
        tool_arguments={"input_marker": "financial-offline-test"},
        trace_output=trace_path,
    )

    assert len(calls) == 1
    assert result.tool_name == "build_financial_artifacts"
    saved = json.loads(trace_path.read_text(encoding="utf-8"))
    assert saved["status"] == "succeeded"
    assert saved["selected_skill"] == "financial-analysis"
    assert saved["tool_name"] == "build_financial_artifacts"


def test_runtime_rejects_mismatched_source_before_skill_selection(
    tmp_path: Path,
) -> None:
    identity, source = _identity_and_source()
    wrong_source = source.copy(update={"entity_id": "CN-A-SZSE-000001"})
    trace_path = tmp_path / "mismatch_trace.json"
    runtime = SkillRuntime(PROJECT_ROOT, clock=lambda: FIXED_TIME)

    with pytest.raises(SkillRuntimeError, match="不属于主体") as error:
        runtime.execute(
            task="生成股权结构图",
            identity=identity,
            source=wrong_source,
            tool_arguments={},
            trace_output=trace_path,
        )

    assert error.value.code == "source_entity_mismatch"
    saved = json.loads(trace_path.read_text(encoding="utf-8"))
    assert saved["selected_skill"] is None
    assert [item["stage"] for item in saved["events"]] == [
        "entity_resolved",
        "failed",
    ]
