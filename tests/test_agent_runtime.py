"""V1.3B主Agent、Function Tool、限制及证据约束的离线测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from credit_research_agent.agent_runtime import (
    OWNERSHIP_FUNCTION_TOOL,
    AgentExecutionStage,
    AgentRuntimeError,
    CreditResearchAgentRuntime,
    create_ownership_function_tool,
)
from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.llm_config import load_deepseek_config
from credit_research_agent.skill_runtime import (
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillExecutionTrace,
)
from credit_research_agent.skills import SkillSelection
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = "请分析以岭药业（002603.SZ）的股权结构并生成股权穿透图"
FIXED_TIME = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def _prepared_inputs(tmp_path: Path) -> Dict[str, Any]:
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    pdf_path = tmp_path / "S01.pdf"
    manifest_path = tmp_path / "S01.metadata.json"
    summary_path = tmp_path / "summary.json"
    pages_path = tmp_path / "pages.jsonl"
    for path in (pdf_path, manifest_path, summary_path, pages_path):
        path.write_text("offline fixture\n", encoding="utf-8")
    download = DownloadResult(
        source_id="S01",
        source_title=source.title,
        source_url=str(source.url),
        file_path=pdf_path,
        manifest_path=manifest_path,
        accessed_at=FIXED_TIME,
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=16,
        sha256="a" * 64,
        from_cache=True,
    )
    return {
        "task": TASK,
        "identity": identity,
        "source": source,
        "download": download,
        "summary_path": summary_path,
        "pages_path": pages_path,
        "ownership_dir": tmp_path / "ownership",
        "trace_output": tmp_path / "agent_trace.json",
        "skill_trace_output": tmp_path / "skill_trace.json",
    }


class _FakeSkillRuntime:
    calls = []

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def execute(self, **kwargs) -> SkillExecutionResult:
        self.__class__.calls.append(kwargs)
        output_dir = Path(kwargs["tool_arguments"]["output_dir"]) / "fake"
        output_dir.mkdir(parents=True, exist_ok=True)
        graph_path = output_dir / "ownership_graph.json"
        validation_path = output_dir / "ownership_validation.json"
        graph = {
            "identity": {
                "legal_name": "石家庄以岭药业股份有限公司",
                "short_name": "以岭药业",
                "ticker": "002603.SZ",
            },
            "as_of_date": "2025-12-31",
            "formal_ready": True,
            "nodes": [
                {
                    "node_id": "target",
                    "name": "石家庄以岭药业股份有限公司",
                },
                {"node_id": "holder", "name": "以岭医药科技有限公司"},
                {"node_id": "person", "name": "吴以岭"},
                {"node_id": "other", "name": "其他未单列股东"},
            ],
            "edges": [
                {
                    "source_node_id": "holder",
                    "target_node_id": "target",
                    "percentage": 31.53,
                    "percentage_origin": "disclosed",
                    "as_of_date": "2025-12-31",
                    "capital_scope": "total_ordinary_share_capital",
                    "status": "verified",
                    "source_ids": ["S01"],
                    "evidence_ids": ["E020"],
                },
                {
                    "source_node_id": "other",
                    "target_node_id": "target",
                    "percentage": 68.47,
                    "percentage_origin": "calculated_residual",
                    "as_of_date": "2025-12-31",
                    "capital_scope": "total_ordinary_share_capital",
                    "status": "verified",
                    "source_ids": ["S01"],
                    "evidence_ids": ["E031"],
                },
            ],
            "control_relationships": [
                {
                    "subject_node_ids": ["person"],
                    "controlled_node_id": "target",
                    "relation_type": "ultimate_controller",
                    "as_of_date": "2025-12-31",
                    "status": "verified",
                    "source_ids": ["S01"],
                    "evidence_ids": ["E030"],
                    "note": None,
                }
            ],
            "evidence": [
                {"source_id": "S01", "page_number": 97},
                {"source_id": "S01", "page_number": 217},
            ],
            "sources": [
                {
                    "source_id": "S01",
                    "title": "石家庄以岭药业股份有限公司2025年年度报告",
                    "publisher": "巨潮资讯网（上市公司公告）",
                    "url": "https://static.cninfo.com.cn/report.pdf",
                    "published_at": "2026-04-28",
                }
            ],
            "missing_information": [
                {
                    "topic": "完整股东名册",
                    "description": "公开年报未逐一列示全部股东",
                    "reason": "仅披露前十大股东",
                }
            ],
        }
        validation = {
            "tolerance": 0.01,
            "branches": [
                {
                    "target_node_id": "target",
                    "capital_scope": "total_ordinary_share_capital",
                    "known_total": 31.53,
                    "tolerance": 0.01,
                    "status": "complete_with_calculated_residual",
                    "edge_ids": ["e1"],
                    "calculated_residual": 68.47,
                    "residual_label": "其他未单列股东",
                    "source_ids": ["S01"],
                    "evidence_ids": ["E020", "E031"],
                    "message": "已按同口径计算差额",
                }
            ],
        }
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False), encoding="utf-8"
        )
        artifacts = {
            "entity_id": "CN-A-SZSE-002603",
            "graph_path": str(graph_path),
            "validation_path": str(validation_path),
            "render_engine": "test",
        }
        for name in (
            "dataset_path",
            "svg_path",
            "png_path",
            "dot_path",
            "citations_path",
            "summary_path",
            "sources_path",
            "questions_path",
        ):
            path = output_dir / name.replace("_path", ".txt")
            path.write_text("fixture\n", encoding="utf-8")
            artifacts[name] = str(path)
        trace_path = Path(kwargs["trace_output"])
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("{}\n", encoding="utf-8")
        trace = SkillExecutionTrace(
            trace_id="TR-FAKE",
            task=kwargs["task"],
            status=SkillExecutionStatus.SUCCEEDED,
            selected_skill="ownership-structure",
            skill_version="1.3.0",
            skill_sha256="b" * 64,
            tool_name="build_ownership_artifacts",
        )
        return SkillExecutionResult(
            trace_path=trace_path,
            trace=trace,
            selection=SkillSelection(
                task=kwargs["task"],
                selected_skill="ownership-structure",
                matched_keywords=["股权"],
                rationale="离线测试",
            ),
            loaded_skill_path=".agents/skills/ownership-structure/SKILL.md",
            tool_name="build_ownership_artifacts",
            artifacts=artifacts,
        )


def _runtime(tmp_path: Path, **config_overrides) -> CreditResearchAgentRuntime:
    config = load_deepseek_config(tmp_path, **config_overrides)
    return CreditResearchAgentRuntime(
        PROJECT_ROOT,
        config,
        skill_runtime_factory=_FakeSkillRuntime,
        clock=lambda: FIXED_TIME,
        trace_id_factory=lambda: "AR-TEST",
    )


def test_function_tool_is_only_high_level_ownership_tool() -> None:
    tool = create_ownership_function_tool()

    assert tool.name == OWNERSHIP_FUNCTION_TOOL
    assert "ownership-structure" in tool.description
    assert "financial" not in tool.name


def test_mock_agent_loop_calls_tool_loads_skill_and_returns_grounded_result(
    tmp_path: Path, monkeypatch
) -> None:
    _FakeSkillRuntime.calls.clear()
    monkeypatch.setattr(
        "credit_research_agent.agent_runtime.build_live_deepseek_model",
        lambda *_: pytest.fail("Mock模式不得创建Live客户端"),
    )

    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path))

    assert len(_FakeSkillRuntime.calls) == 1
    assert result.selected_skill == "ownership-structure"
    assert result.tool_name == "build_ownership_artifacts"
    assert "31.53%" in result.final_output
    assert "[S01]" in result.final_output
    saved = json.loads(result.trace_path.read_text(encoding="utf-8"))
    stages = [item["stage"] for item in saved["events"]]
    assert "llm_tool_selected" in stages
    assert "skill_runtime_completed" in stages
    assert saved["loaded_skill_path"] == ".agents/skills/ownership-structure/SKILL.md"
    assert saved["percentage_validation_passed"] is True
    assert saved["tool_call_count"] == 1
    assert saved["usage"]["total_tokens"] == 152


def test_mock_mode_does_not_require_or_read_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-be-used")

    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path))

    saved = result.trace_path.read_text(encoding="utf-8")
    assert "sk-must-not-be-used" not in saved
    assert result.config["api_key_configured"] is False


def test_invalid_tool_entity_is_rejected(tmp_path: Path) -> None:
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "600000.SH", "source_id": "S01"},
                        call_id="invalid-entity",
                    )
                ]
            )
        ]
    )
    runtime = CreditResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(tmp_path),
        model_override=scripted,
        skill_runtime_factory=_FakeSkillRuntime,
    )

    with pytest.raises(AgentRuntimeError) as error:
        runtime.run_sync(**_prepared_inputs(tmp_path))

    assert error.value.code == "tool_entity_mismatch"
    saved = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert saved["status"] == "failed"


def test_maximum_tool_call_limit_stops_repeated_call(tmp_path: Path) -> None:
    call = lambda call_id: function_call(
        OWNERSHIP_FUNCTION_TOOL,
        {"ticker": "002603.SZ", "source_id": "S01"},
        call_id=call_id,
    )
    scripted = ScriptedModel(
        [ModelStep(output=[call("first")]), ModelStep(output=[call("second")])]
    )
    runtime = CreditResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(tmp_path, max_tool_calls=1),
        model_override=scripted,
        skill_runtime_factory=_FakeSkillRuntime,
    )

    with pytest.raises(AgentRuntimeError) as error:
        runtime.run_sync(**_prepared_inputs(tmp_path))

    assert error.value.code == "max_tool_calls_exceeded"
    saved = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert saved["tool_call_count"] == 1


def test_maximum_turn_limit_is_reported(tmp_path: Path) -> None:
    call = lambda call_id: function_call(
        OWNERSHIP_FUNCTION_TOOL,
        {"ticker": "002603.SZ", "source_id": "S01"},
        call_id=call_id,
    )
    scripted = ScriptedModel(
        [
            ModelStep(output=[call("turn-1")]),
            ModelStep(output=[call("turn-2")]),
            ModelStep(output=[assistant_message("不应到达[S01]")]),
        ]
    )
    runtime = CreditResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(tmp_path, max_turns=2, max_tool_calls=3),
        model_override=scripted,
        skill_runtime_factory=_FakeSkillRuntime,
    )

    with pytest.raises(AgentRuntimeError) as error:
        runtime.run_sync(**_prepared_inputs(tmp_path))

    assert error.value.code == "max_turns_exceeded"


def test_model_output_cannot_invent_percentage_or_source(tmp_path: Path) -> None:
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ", "source_id": "S01"},
                        call_id="grounding-call",
                    )
                ]
            ),
            ModelStep(
                output=[assistant_message("某股东持股99.99%，来源为[S99]。")],
                usage=Usage(requests=1, input_tokens=1, output_tokens=1, total_tokens=2),
            ),
        ]
    )
    runtime = CreditResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(tmp_path),
        model_override=scripted,
        skill_runtime_factory=_FakeSkillRuntime,
    )

    with pytest.raises(AgentRuntimeError) as error:
        runtime.run_sync(**_prepared_inputs(tmp_path))

    assert error.value.code == "unknown_source_citation"


def test_financial_task_is_not_exposed_to_agent(tmp_path: Path) -> None:
    inputs = _prepared_inputs(tmp_path)
    inputs["task"] = "请调用financial-analysis分析财务报表"

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "unsupported_agent_task"
