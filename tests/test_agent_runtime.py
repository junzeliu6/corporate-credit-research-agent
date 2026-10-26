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
    OWNERSHIP_RESPONSE_CONTRACT_VERSION,
    AgentExecutionStage,
    AgentRuntimeError,
    CreditResearchAgentRuntime,
    _build_compact_tool_result,
    build_ownership_snapshot_from_artifacts,
    build_ownership_response_contract,
    create_ownership_function_tool,
    parse_ownership_fact_selection,
    render_ownership_fact_selection,
    validate_grounded_output,
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
VALID_FAKE_FACT_IDS = ["OE001", "OE002", "CR001", "PV001"]
VALID_FAKE_MISSING_IDS = ["M101"]


def _prepared_inputs(tmp_path: Path, source_id: str = "S01") -> Dict[str, Any]:
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get(source_id)
    pdf_path = tmp_path / f"{source_id}.pdf"
    manifest_path = tmp_path / f"{source_id}.metadata.json"
    summary_path = tmp_path / "summary.json"
    pages_path = tmp_path / "pages.jsonl"
    for path in (pdf_path, manifest_path, summary_path, pages_path):
        path.write_text("offline fixture\n", encoding="utf-8")
    download = DownloadResult(
        source_id=source_id,
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
        bound_source = kwargs["source"]
        bound_source_id = bound_source.source_id
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
                    "edge_id": "OE001",
                    "source_node_id": "holder",
                    "target_node_id": "target",
                    "percentage": 31.53,
                    "percentage_origin": "disclosed",
                    "as_of_date": "2025-12-31",
                    "capital_scope": "total_ordinary_share_capital",
                    "status": "verified",
                    "source_ids": [bound_source_id],
                    "evidence_ids": ["E020"],
                },
                {
                    "edge_id": "OE002",
                    "source_node_id": "other",
                    "target_node_id": "target",
                    "percentage": 68.47,
                    "percentage_origin": "calculated_residual",
                    "as_of_date": "2025-12-31",
                    "capital_scope": "total_ordinary_share_capital",
                    "status": "verified",
                    "source_ids": [bound_source_id],
                    "evidence_ids": ["E031"],
                },
            ],
            "control_relationships": [
                {
                    "relationship_id": "CR001",
                    "subject_node_ids": ["person"],
                    "controlled_node_id": "target",
                    "relation_type": "ultimate_controller",
                    "as_of_date": "2025-12-31",
                    "status": "verified",
                    "source_ids": [bound_source_id],
                    "evidence_ids": ["E030"],
                    "note": None,
                }
            ],
            "evidence": [
                {
                    "evidence_id": "E020",
                    "source_id": bound_source_id,
                    "page_number": 97,
                },
                {
                    "evidence_id": "E030",
                    "source_id": bound_source_id,
                    "page_number": 97,
                },
                {
                    "evidence_id": "E031",
                    "source_id": bound_source_id,
                    "page_number": 217,
                },
            ],
            "sources": [
                {
                    "source_id": bound_source_id,
                    "title": bound_source.title,
                    "publisher": bound_source.publisher,
                    "url": str(bound_source.url),
                    "published_at": (
                        str(bound_source.published_at)
                        if bound_source.published_at
                        else None
                    ),
                }
            ],
            "missing_information": [
                {
                    "missing_id": "M101",
                    "topic": "完整股东名册",
                    "description": "公开年报未逐一列示全部股东",
                    "reason": "年报披露的是持股5%以上股东或前10名股东",
                    "evidence_ids": ["E020"],
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
                    "edge_ids": ["OE001", "OE002"],
                    "calculated_residual": 68.47,
                    "residual_label": "其他未单列股东",
                    "source_ids": [bound_source_id],
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


def _grounding_payload() -> Dict[str, Any]:
    payload = {
        "identity": {
            "legal_name": "石家庄以岭药业股份有限公司",
            "short_name": "以岭药业",
            "ticker": "002603.SZ",
        },
        "verified_relations": [
            {
                "fact_id": "OE001",
                "holder": "以岭医药科技有限公司",
                "held_entity": "石家庄以岭药业股份有限公司",
                "percentage": 31.53,
                "percentage_origin": "disclosed",
                "target_node_id": "target",
                "as_of_date": "2025-12-31",
                "source_ids": ["S01"],
                "evidence_ids": ["E020"],
            },
            {
                "fact_id": "OE002",
                "holder": "其他未单列股东",
                "held_entity": "石家庄以岭药业股份有限公司",
                "percentage": 68.47,
                "percentage_origin": "calculated_residual",
                "target_node_id": "target",
                "as_of_date": "2025-12-31",
                "source_ids": ["S01"],
                "evidence_ids": ["E031"],
            },
        ],
        "verified_control_relationships": [
            {
                "claim_id": "CR001",
                "subjects": ["吴以岭"],
                "controlled_entity": "石家庄以岭药业股份有限公司",
                "relation_type": "ultimate_controller",
                "as_of_date": "2025-12-31",
                "source_ids": ["S01"],
                "evidence_ids": ["E030"],
            }
        ],
        "percentage_validation": {
            "branches": [
                {
                    "validation_id": "PV001",
                    "target_name": "石家庄以岭药业股份有限公司",
                    "target_node_id": "target",
                    "known_total": 31.53,
                    "calculated_residual": 68.47,
                    "status": "complete_with_calculated_residual",
                    "edge_ids": ["OE001"],
                    "evidence_ids": ["E020", "E031"],
                    "source_ids": ["S01"],
                }
            ]
        },
        "missing_information": [
            {
                "missing_id": "M101",
                "topic": "完整股东名册",
                "description": "公开年报未逐一列示全部股东",
                "source_ids": ["S01"],
            }
        ],
        "sources": [{"source_id": "S01"}],
        "as_of_date": "2025-12-31",
        "artifacts": {"svg_path": "ownership_structure.svg"},
    }
    payload["response_contract"] = build_ownership_response_contract(
        payload
    ).model_dump(mode="json")
    return payload


def _snapshot_artifacts(tmp_path: Path) -> Dict[str, Any]:
    """复用现有假 Skill 产物，并补入快照边界测试所需关系。"""

    prepared = _prepared_inputs(tmp_path)
    result = _FakeSkillRuntime(PROJECT_ROOT).execute(
        task=prepared["task"],
        identity=prepared["identity"],
        source=prepared["source"],
        tool_arguments={"output_dir": tmp_path / "snapshot-output"},
        trace_output=tmp_path / "snapshot-skill-trace.json",
    )
    artifacts = dict(result.artifacts)
    graph_path = Path(artifacts["graph_path"])
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["edges"][1]["synthetic"] = True
    graph["edges"].append(
        {
            "edge_id": "OE-VOTING",
            "source_node_id": "person",
            "target_node_id": "target",
            "relation_type": "voting_rights",
            "percentage": 55.0,
            "percentage_origin": "disclosed",
            "as_of_date": "2025-12-31",
            "capital_scope": "voting_rights",
            "status": "verified",
            "source_ids": ["S01"],
            "evidence_ids": ["E099"],
            "synthetic": False,
        }
    )
    graph["warnings"] = ["股权图仅反映指定披露日的公开信息"]
    graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    Path(artifacts["dataset_path"]).write_text(
        json.dumps(
            {"limitations": ["未取得完整股东名册", "股权图仅反映指定披露日的公开信息"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return artifacts


def _all_mapping_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_all_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_all_mapping_keys(item))
    return keys


def test_ownership_snapshot_hides_technical_ids_and_voting_rights(
    tmp_path: Path,
) -> None:
    snapshot = build_ownership_snapshot_from_artifacts(_snapshot_artifacts(tmp_path))

    assert [item["percentage"] for item in snapshot["ownership_relations"]] == [
        31.53,
        68.47,
    ]
    assert snapshot["ownership_relations"][1]["synthetic"] is True
    assert "voting_rights" not in json.dumps(snapshot, ensure_ascii=False)
    assert "response_contract" not in snapshot
    assert not any(
        key.endswith("_id") or key.endswith("_ids")
        for key in _all_mapping_keys(snapshot)
    )


def test_ownership_snapshot_keeps_controls_validation_and_limitations(
    tmp_path: Path,
) -> None:
    snapshot = build_ownership_snapshot_from_artifacts(_snapshot_artifacts(tmp_path))

    assert snapshot["subject"] == {
        "legal_name": "石家庄以岭药业股份有限公司",
        "short_name": "以岭药业",
        "ticker": "002603.SZ",
    }
    assert snapshot["control_relationships"] == [
        {
            "subjects": ["吴以岭"],
            "controlled_entity": "石家庄以岭药业股份有限公司",
            "relation_type": "ultimate_controller",
            "as_of_date": "2025-12-31",
            "verification_status": "verified",
            "note": None,
        }
    ]
    validation = snapshot["percentage_validation"]
    assert validation["all_complete"] is True
    assert validation["branches"][0]["held_entity"] == "石家庄以岭药业股份有限公司"
    assert validation["branches"][0]["calculated_residual"] == 68.47
    assert snapshot["limitations"] == [
        "未取得完整股东名册",
        "股权图仅反映指定披露日的公开信息",
    ]
    assert snapshot["render_engine"] == "test"
    assert snapshot["graph_paths"]["svg"].endswith("svg.txt")


def test_function_tool_is_only_high_level_ownership_tool() -> None:
    tool = create_ownership_function_tool()

    assert tool.name == OWNERSHIP_FUNCTION_TOOL
    assert "ownership-structure" in tool.description
    assert "financial" not in tool.name
    properties = tool.params_json_schema["properties"]
    assert set(properties) == {"ticker"}
    assert tool.params_json_schema["required"] == ["ticker"]
    assert tool.params_json_schema["additionalProperties"] is False


def test_runtime_binds_registered_source_without_llm_argument(tmp_path: Path) -> None:
    _FakeSkillRuntime.calls.clear()

    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path))

    assert _FakeSkillRuntime.calls[0]["source"].source_id == "S01"
    saved = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert saved["source_id"] == "S01"
    assert saved["bound_source_id"] == "S01"
    assert "runtime_source_bound" in [item["stage"] for item in saved["events"]]
    assert saved["tool_call_arguments"][0]["model_arguments"] == {
        "ticker": "002603.SZ"
    }
    assert saved["tool_call_arguments"][0]["runtime_bound"] == {
        "source_id": "S01"
    }


def test_runtime_can_bind_another_registered_compatible_source(tmp_path: Path) -> None:
    _FakeSkillRuntime.calls.clear()

    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path, "S04"))

    assert _FakeSkillRuntime.calls[0]["source"].source_id == "S04"
    assert "[S04]" in result.final_output
    saved = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert saved["bound_source_id"] == "S04"
    assert saved["tool_call_arguments"][0]["runtime_bound"] == {
        "source_id": "S04"
    }


def test_unregistered_runtime_source_is_rejected_before_llm(tmp_path: Path) -> None:
    _FakeSkillRuntime.calls.clear()
    inputs = _prepared_inputs(tmp_path)
    inputs["source"] = inputs["source"].copy(update={"source_id": "S99"})

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "runtime_source_not_registered"
    saved = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert saved["llm_call_count"] == 0
    assert saved["tool_call_count"] == 0
    assert saved["status"] == "failed"
    assert not _FakeSkillRuntime.calls


def test_mismatched_runtime_source_record_is_rejected(tmp_path: Path) -> None:
    inputs = _prepared_inputs(tmp_path)
    inputs["source"] = inputs["source"].copy(
        update={"entity_id": "CN-A-SSE-600000"}
    )

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "runtime_source_record_mismatch"


def test_registered_source_for_other_entity_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    inputs = _prepared_inputs(tmp_path)
    mismatched_source = inputs["source"].copy(
        update={"entity_id": "CN-A-SSE-600000"}
    )
    inputs["source"] = mismatched_source
    monkeypatch.setattr(
        "credit_research_agent.agent_runtime.SourceCatalog.from_default_catalog",
        lambda: SourceCatalog([mismatched_source]),
    )

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "runtime_source_entity_mismatch"


def test_download_source_must_match_bound_runtime_source(tmp_path: Path) -> None:
    inputs = _prepared_inputs(tmp_path)
    inputs["download"] = inputs["download"].copy(update={"source_id": "S04"})

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "runtime_download_source_mismatch"


def test_llm_cannot_override_runtime_bound_source(tmp_path: Path) -> None:
    _FakeSkillRuntime.calls.clear()
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ", "source_id": "S04"},
                        call_id="source-override",
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

    assert error.value.code == "tool_source_override_forbidden"
    saved = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert saved["bound_source_id"] == "S01"
    assert saved["tool_call_count"] == 0
    assert saved["validator_rule_id"] == "TOOL-INPUT-001"
    assert saved["rejected_sentence"] == "source_id"
    assert not _FakeSkillRuntime.calls


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
    assert saved["tool_call_arguments"] == [
        {
            "tool_name": OWNERSHIP_FUNCTION_TOOL,
            "model_arguments": {"ticker": "002603.SZ"},
            "runtime_bound": {"source_id": "S01"},
        }
    ]
    assert saved["bound_source_id"] == "S01"
    assert saved["returned_fact_ids"] == VALID_FAKE_FACT_IDS
    assert saved["selected_fact_ids"] == VALID_FAKE_FACT_IDS
    assert saved["returned_missing_information_ids"] == VALID_FAKE_MISSING_IDS
    assert saved["selected_missing_information_ids"] == VALID_FAKE_MISSING_IDS
    assert saved["selected_analysis_claim_ids"] == ["A001", "A002"]
    candidate = json.loads(saved["model_candidate_output"])
    assert candidate["fact_ids"] == VALID_FAKE_FACT_IDS
    assert candidate["missing_information_ids"] == VALID_FAKE_MISSING_IDS
    assert [item["claim_id"] for item in candidate["analysis_claims"]] == [
        "A001",
        "A002",
    ]
    assert saved["deterministic_artifacts_preserved"] is True
    assert len(saved["model_candidate_output_sha256"]) == 64
    assert saved["usage"]["total_tokens"] == 152
    completed = next(
        item for item in saved["events"] if item["stage"] == "skill_runtime_completed"
    )
    assert (
        completed["details"]["response_contract_version"]
        == OWNERSHIP_RESPONSE_CONTRACT_VERSION
    )
    assert completed["details"]["allowed_percentage_count"] >= 1


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
                        {"ticker": "600000.SH"},
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
        {"ticker": "002603.SZ"},
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
        {"ticker": "002603.SZ"},
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


def test_model_output_cannot_invent_fact_id(tmp_path: Path) -> None:
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="grounding-call",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": VALID_FAKE_FACT_IDS + ["FAKE-999"],
                                "missing_information_ids": VALID_FAKE_MISSING_IDS,
                                "analysis_claims": [],
                            },
                            ensure_ascii=False,
                        )
                    )
                ],
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

    assert error.value.code == "unknown_fact_id"
    saved = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert saved["usage"]["requests"] == 2
    assert saved["usage"]["input_tokens"] == 1
    assert saved["usage"]["output_tokens"] == 1
    assert saved["usage"]["total_tokens"] == 2
    assert saved["validator_rule_id"] == "FACT-SELECTION-002"
    assert saved["rejected_sentence"] == "FAKE-999"
    assert "FAKE-999" in saved["model_candidate_output"]


def test_rejected_analysis_preserves_deterministic_artifacts(tmp_path: Path) -> None:
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="artifact-preservation-call",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": VALID_FAKE_FACT_IDS,
                                "missing_information_ids": VALID_FAKE_MISSING_IDS,
                                "analysis_claims": [
                                    {
                                        "claim_id": "A001",
                                        "claim_type": "supported_inference",
                                        "statement": "某股东持股5%，应作为确定性企业事实。",
                                        "supporting_fact_ids": ["OE001"],
                                        "related_missing_ids": [],
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
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

    assert error.value.code == "analysis_business_rule_as_fact"
    trace = json.loads(Path(error.value.trace_path).read_text(encoding="utf-8"))
    assert trace["deterministic_artifacts_preserved"] is True
    assert trace["artifact_paths"]
    assert all(Path(path).is_file() for path in trace["artifact_paths"])
    assert trace["validator_rule_id"] == "ANALYSIS-RULE-001"


def test_response_contract_marks_python_calculations_and_excludes_llm_sum() -> None:
    payload = _grounding_payload()
    contract = payload["response_contract"]
    percentages = {
        item["value"]: item for item in contract["allowed_percentages"]
    }

    assert contract["version"] == OWNERSHIP_RESPONSE_CONTRACT_VERSION
    assert contract["prohibit_llm_arithmetic"] is True
    assert "31.53" in percentages
    assert "68.47" in percentages
    assert "100.00" in percentages
    assert "23.15" not in percentages
    assert percentages["68.47"]["origin"] in {
        "calculated_residual",
        "calculated_sum",
    }
    assert contract["required_fact_ids"] == VALID_FAKE_FACT_IDS
    assert contract["required_missing_information_ids"] == VALID_FAKE_MISSING_IDS
    assert {item["value"] for item in contract["percentage_rules"]} == {
        "5.00",
        "25.00",
    }


def test_compact_tool_result_does_not_send_threshold_reason_to_model(
    tmp_path: Path,
) -> None:
    inputs = _prepared_inputs(tmp_path)
    result = _FakeSkillRuntime(PROJECT_ROOT).execute(
        task=inputs["task"],
        identity=inputs["identity"],
        source=inputs["source"],
        tool_arguments={"output_dir": tmp_path / "ownership"},
        trace_output=tmp_path / "skill_trace_for_compaction.json",
    )

    compact = _build_compact_tool_result(result)
    missing = compact["missing_information"][0]

    assert "reason" not in missing
    assert "5%以上" not in json.dumps(
        compact["missing_information"], ensure_ascii=False
    )
    rules = compact["response_contract"]["percentage_rules"]
    assert any(item["rule_id"].startswith("RULE-") for item in rules)


def test_model_can_reference_true_fact_ids_and_python_renders_output() -> None:
    payload = _grounding_payload()
    selection = parse_ownership_fact_selection(
        json.dumps(
            {
                "fact_ids": VALID_FAKE_FACT_IDS,
                "missing_information_ids": VALID_FAKE_MISSING_IDS,
                "analysis_claims": [],
            }
        ),
        payload,
    )

    rendered = render_ownership_fact_selection(selection, payload)

    assert selection.fact_ids == VALID_FAKE_FACT_IDS
    assert "以岭医药科技有限公司持有" in rendered
    assert "31.53%" in rendered
    assert "[S01]" in rendered
    validate_grounded_output(rendered, payload)


def test_python_calculation_fact_is_citable_and_keeps_derivation() -> None:
    contract = _grounding_payload()["response_contract"]
    calculated = next(
        item for item in contract["grounded_facts"] if item["fact_id"] == "OE002"
    )

    assert calculated["provenance"] == "python_calculation"
    assert calculated["verification_status"] == "calculated"
    assert calculated["value"] == "68.47"
    assert calculated["unit"] == "%"
    assert calculated["derived_from"] == ["OE001"]


def test_grounded_risk_analysis_is_rendered_with_fact_sources() -> None:
    payload = _grounding_payload()
    draft = {
        "fact_ids": VALID_FAKE_FACT_IDS,
        "missing_information_ids": VALID_FAKE_MISSING_IDS,
        "analysis_claims": [
            {
                "claim_id": "A001",
                "claim_type": "risk_signal",
                "statement": "股权与控制关系较为集中，需关注关键控制主体变动对治理稳定性的影响。",
                "supporting_fact_ids": ["OE001", "CR001"],
                "related_missing_ids": [],
            }
        ],
    }

    selection = parse_ownership_fact_selection(json.dumps(draft), payload)
    rendered = render_ownership_fact_selection(selection, payload)

    assert "风险关注" in rendered
    assert draft["analysis_claims"][0]["statement"] in rendered
    assert "[S01]" in rendered
    validate_grounded_output(rendered, payload)


def test_data_gap_can_generate_grounded_due_diligence_question() -> None:
    payload = _grounding_payload()
    draft = {
        "fact_ids": VALID_FAKE_FACT_IDS,
        "missing_information_ids": VALID_FAKE_MISSING_IDS,
        "analysis_claims": [
            {
                "claim_id": "A001",
                "claim_type": "due_diligence_question",
                "statement": "建议补充取得完整股东名册并核验未单列股东构成。",
                "supporting_fact_ids": [],
                "related_missing_ids": ["M101"],
            }
        ],
    }

    selection = parse_ownership_fact_selection(json.dumps(draft), payload)
    rendered = render_ownership_fact_selection(selection, payload)

    assert "尽调问题" in rendered
    assert "完整股东名册" in rendered
    assert "资料限制与待核查事项" in rendered


def test_analysis_cannot_turn_five_percent_rule_into_company_fact() -> None:
    payload = _grounding_payload()
    draft = {
        "fact_ids": VALID_FAKE_FACT_IDS,
        "missing_information_ids": VALID_FAKE_MISSING_IDS,
        "analysis_claims": [
            {
                "claim_id": "A001",
                "claim_type": "supported_inference",
                "statement": "某机构股东实际持股5%，因此构成重要股东。",
                "supporting_fact_ids": ["OE001"],
                "related_missing_ids": [],
            }
        ],
    }

    with pytest.raises(AgentRuntimeError) as error:
        parse_ownership_fact_selection(json.dumps(draft), payload)

    assert error.value.code == "analysis_business_rule_as_fact"
    assert error.value.rule_id == "ANALYSIS-RULE-001"


def test_analysis_cannot_use_unverified_web_candidate() -> None:
    payload = _grounding_payload()
    payload["response_contract"]["grounded_facts"].append(
        {
            "fact_id": "WEB001",
            "company": "石家庄以岭药业股份有限公司",
            "subject": "石家庄以岭药业股份有限公司",
            "metric": "market_share",
            "value": "未知候选值",
            "unit": None,
            "period": None,
            "as_of_date": None,
            "source_id": "S02",
            "additional_source_ids": [],
            "source_type": "company_website",
            "source_location": "搜索结果摘要，尚未核对网页正文",
            "source_url": "https://example.invalid/result",
            "derived_from": [],
            "evidence_ids": [],
            "provenance": "verified_external_fact",
            "verification_status": "unverified",
        }
    )
    draft = {
        "fact_ids": VALID_FAKE_FACT_IDS,
        "missing_information_ids": VALID_FAKE_MISSING_IDS,
        "analysis_claims": [
            {
                "claim_id": "A001",
                "claim_type": "supported_inference",
                "statement": "公司在相关市场具有领先地位。",
                "supporting_fact_ids": ["WEB001"],
                "related_missing_ids": [],
            }
        ],
    }

    with pytest.raises(AgentRuntimeError) as error:
        parse_ownership_fact_selection(json.dumps(draft), payload)

    assert error.value.code == "unverified_analysis_fact"
    assert error.value.rule_id == "ANALYSIS-FACT-002"


@pytest.mark.parametrize(
    "text",
    [
        "2024年，以岭医药科技有限公司持有石家庄以岭药业股份有限公司31.53%[S01]。",
        "吴以岭持有石家庄以岭药业股份有限公司31.53%[S01]。",
    ],
)
def test_true_percentage_with_wrong_period_or_subject_is_rejected(text: str) -> None:
    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(text, _grounding_payload())

    assert error.value.code == "misclassified_percentage"
    assert error.value.rule_id == "PERCENTAGE-FACT-002"


def test_true_percentage_with_wrong_registered_source_is_rejected() -> None:
    payload = _grounding_payload()
    payload["response_contract"]["allowed_source_ids"].append("S02")

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(
            "以岭医药科技有限公司持有石家庄以岭药业股份有限公司31.53%[S02]。",
            payload,
        )

    assert error.value.code == "percentage_fact_source_mismatch"
    assert error.value.rule_id == "PERCENTAGE-SOURCE-001"


def test_business_rule_five_percent_is_not_enterprise_fact() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(
            "内部绘图规则规定低于5%的机构股东可以合并展示。[S01]",
            payload,
        )

    assert error.value.code == "business_rule_percentage_in_final_output"
    assert error.value.rule_id == "PERCENTAGE-RULE-001"


def test_unverified_five_percent_holding_is_rejected() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output("某机构股东持有目标公司5%。[S01]", payload)

    assert error.value.code == "ungrounded_percentage"
    assert error.value.rule_id == "PERCENTAGE-FACT-001"


def test_unrelated_percentage_is_rejected() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output("公司市场份额约12.34%。[S01]", payload)

    assert error.value.code == "ungrounded_percentage"


def test_fact_selection_must_include_all_required_ids() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        parse_ownership_fact_selection(
            json.dumps(
                {
                    "fact_ids": ["OE001"],
                    "missing_information_ids": VALID_FAKE_MISSING_IDS,
                    "analysis_claims": [],
                }
            ),
            payload,
        )

    assert error.value.code == "missing_required_fact_id"
    assert error.value.rule_id == "FACT-SELECTION-003"


def test_grounding_rejects_mathematically_derivable_but_unlisted_percentage() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(
            "以岭医药科技有限公司持有石家庄以岭药业股份有限公司"
            "31.53%[S01]；另有合计23.15%[S01]。",
            payload,
        )

    assert error.value.code == "ungrounded_percentage"


def test_grounding_accepts_only_whitelisted_calculated_residual() -> None:
    payload = _grounding_payload()

    validate_grounded_output(
        "其他未单列股东为确定性差额68.47%[S01]；"
        "分支按100%校验[S01]。",
        payload,
    )


def test_grounding_rejects_unlisted_control_subject() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(
            "年报披露最终控制主体为张三[S01]。",
            payload,
        )

    assert error.value.code == "ungrounded_control_claim"


def test_control_claim_requires_matching_citation_in_same_clause() -> None:
    payload = _grounding_payload()

    with pytest.raises(AgentRuntimeError) as error:
        validate_grounded_output(
            "股权数据来自年报[S01]。年报披露最终控制主体为吴以岭。",
            payload,
        )

    assert error.value.code == "missing_control_claim_citation"


def test_financial_task_is_not_exposed_to_agent(tmp_path: Path) -> None:
    inputs = _prepared_inputs(tmp_path)
    inputs["task"] = "请调用financial-analysis分析财务报表"

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path).run_sync(**inputs)

    assert error.value.code == "unsupported_agent_task"
