"""V2.3 财务 Function Tool 与简化 LLM 报告链路的离线测试。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from credit_research_agent.agent_runtime import AgentRuntimeError
from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialSourceInput
from credit_research_agent.financial_agent_runtime import (
    FINANCIAL_FUNCTION_TOOL,
    FinancialResearchAgentRuntime,
    _build_financial_analysis_context,
    _build_delivery_manifest,
    create_financial_function_tool,
    parse_financial_narrative_draft,
    render_llm_financial_report,
    restore_financial_report_from_saved_draft,
)
from credit_research_agent.llm_config import load_deepseek_config
from credit_research_agent.skill_runtime import (
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillExecutionTrace,
)
from credit_research_agent.skills import SkillSelection
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
TASK = "请分析以岭药业2023至2025年财务报表、现金流和重大同比变动"
TECHNICAL_MARKER = re.compile(
    r"\[(?:FM\d+|FR\d+|RF\d+|FX(?:\d+|-[A-Z0-9-]+)|FH\d+|"
    r"FQ\d+|FS\d+|F\d+|I\d+|S\d+|M\d+)\]"
)


def _download(tmp_path: Path, source_id: str) -> DownloadResult:
    source = SourceCatalog.from_default_catalog().get(source_id)
    file_path = tmp_path / f"{source_id}.pdf"
    manifest_path = tmp_path / f"{source_id}.json"
    file_path.write_text("fixture", encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")
    return DownloadResult(
        source_id=source_id,
        source_title=source.title,
        source_url=str(source.url),
        file_path=file_path,
        manifest_path=manifest_path,
        accessed_at=FIXED_TIME,
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=7,
        sha256="a" * 64,
        from_cache=True,
    )


def _prepared_inputs(tmp_path: Path) -> Dict[str, Any]:
    catalog = SourceCatalog.from_default_catalog()
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    primary_download = _download(tmp_path, "S01")
    comparison_download = _download(tmp_path, "S10")
    summary_path = tmp_path / "summary.json"
    pages_path = tmp_path / "pages.jsonl"
    comparison_summary = tmp_path / "comparison_summary.json"
    comparison_pages = tmp_path / "comparison_pages.jsonl"
    for path in (summary_path, pages_path, comparison_summary, comparison_pages):
        path.write_text("fixture", encoding="utf-8")
    return {
        "task": TASK,
        "identity": identity,
        "source": catalog.get("S01"),
        "download": primary_download,
        "summary_path": summary_path,
        "pages_path": pages_path,
        "comparison_inputs": [
            FinancialSourceInput(
                source=catalog.get("S10"),
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        "financial_dir": tmp_path / "financial",
        "trace_output": tmp_path / "agent_trace.json",
        "skill_trace_output": tmp_path / "skill_trace.json",
    }


def _fake_metric(
    metric_id: str,
    item: str,
    item_label: str,
    fact_id: str,
    prior_value: str,
    current_value: str,
    absolute_change: str,
    yoy_percent: str,
    *,
    requires_deep_analysis: bool = False,
) -> Dict[str, Any]:
    return {
        "metric_id": metric_id,
        "item": item,
        "item_label": item_label,
        "prior_period": "FY2024",
        "current_period": "FY2025",
        "prior_value": prior_value,
        "current_value": current_value,
        "absolute_change": absolute_change,
        "yoy_percent": yoy_percent,
        "change_kind": "comparable",
        "requires_deep_analysis": requires_deep_analysis,
        "supporting_fact_ids": [fact_id],
        "source_ids": ["S01", "S10"],
    }


FAKE_METRICS = [
    _fake_metric(
        "FM024", "operating_revenue", "营业收入", "F101",
        "100000000", "120000000", "20000000", "20.00",
        requires_deep_analysis=True,
    ),
    _fake_metric(
        "FM004", "accounts_receivable", "应收账款", "F102",
        "10000000", "11000000", "1000000", "10.00",
    ),
    _fake_metric(
        "FM014", "inventory", "存货", "F103",
        "20000000", "18000000", "-2000000", "-10.00",
    ),
    _fake_metric(
        "FM016", "monetary_funds", "货币资金", "F104",
        "5000000", "6000000", "1000000", "20.00",
        requires_deep_analysis=True,
    ),
    _fake_metric(
        "FM002", "accounts_payable", "应付账款", "F105",
        "10000000", "9000000", "-1000000", "-10.00",
    ),
    _fake_metric(
        "FM020", "operating_cash_flow", "经营活动产生的现金流量净额", "F106",
        "2000000", "4000000", "2000000", "100.00",
        requires_deep_analysis=True,
    ),
]


class _FakeFinancialSkillRuntime:
    calls: list[Dict[str, Any]] = []

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def execute(self, **kwargs) -> SkillExecutionResult:
        self.__class__.calls.append(kwargs)
        output_dir = Path(kwargs["tool_arguments"]["output_dir"]) / "fake"
        output_dir.mkdir(parents=True, exist_ok=True)
        identity = kwargs["identity"]
        evidence = [
            {
                "evidence_id": f"E{index:03d}",
                "source_id": "S01",
                "page_number": 100 + index,
                "quote": f"测试事实原文{index}",
            }
            for index in range(1, 7)
        ]
        raw_facts = [
            {
                "fact_id": f"F{100 + index}",
                "subject": identity.legal_name,
                "item": metric["item"],
                "period": "FY2025",
                "value": metric["current_value"],
                "unit": "CNY",
                "claim": f"{metric['item_label']}为{metric['current_value']}元",
                "evidence_ids": [f"E{index:03d}"],
                "source_ids": ["S01"],
            }
            for index, metric in enumerate(FAKE_METRICS, start=1)
        ]
        payloads: Dict[str, Any] = {
            "facts_path": {
                "identity": json.loads(identity.json()),
                "raw_facts": raw_facts,
                "evidence": evidence,
                "missing_information": [
                    {
                        "missing_id": "M201",
                        "topic": "短期借款",
                        "description": "FY2024及FY2025公开主表未形成可比值",
                        "reason": "需补充借款和授信台账",
                    }
                ],
            },
            "metrics_path": {"metrics": FAKE_METRICS, "ratios": []},
            "explanations_path": {
                "explanations": [
                    {
                        "explanation_id": "FX001",
                        "metric_id": "FM020",
                        "kind": "disclosed_reason",
                        "statement": "公司披露主要因销售回款增加。",
                        "disclosed_fact_ids": ["F106"],
                        "source_ids": ["S01"],
                    }
                ],
                "inferences": [],
                "hypotheses": [],
            },
            "reasonableness_path": [
                {
                    "assessment_id": "FA001",
                    "metric_id": "FM020",
                    "status": "supported",
                    "conclusion": "销售收现与经营现金流方向一致。",
                    "credit_concern": "关注回款持续性。",
                }
            ],
            "risk_flags_path": [
                {"flag_id": "RF001", "observation": "经营现金流重大变动。"}
            ],
            "sources_path": {
                "sources": [
                    {
                        "source_id": "S01",
                        "title": "2025年年度报告",
                        "url": "https://example.test/S01.pdf",
                    },
                    {
                        "source_id": "S10",
                        "title": "2024年年度报告",
                        "url": "https://example.test/S10.pdf",
                    },
                ],
                "evidence": evidence,
            },
        }
        artifacts: Dict[str, Any] = {"entity_id": identity.entity_id}
        for field_name, payload in payloads.items():
            path = output_dir / field_name.replace("_path", ".json")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            artifacts[field_name] = str(path)
        for field_name in (
            "analysis_path",
            "data_appendix_path",
            "questions_path",
            "validation_path",
        ):
            path = output_dir / field_name.replace("_path", ".md")
            path.write_text("# 等待 DeepSeek 分析\n", encoding="utf-8")
            artifacts[field_name] = str(path)
        artifacts.update(
            fact_count=len(raw_facts), metric_count=len(FAKE_METRICS),
            ratio_count=0, risk_flag_count=1, material_change_count=3,
            explanation_count=1, hypothesis_count=0, assessment_count=1,
        )
        trace_path = Path(kwargs["trace_output"])
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("{}\n", encoding="utf-8")
        return SkillExecutionResult(
            trace_path=trace_path,
            trace=SkillExecutionTrace(
                trace_id="TR-FIN",
                task=kwargs["task"],
                status=SkillExecutionStatus.SUCCEEDED,
                selected_skill="financial-analysis",
                skill_version="2.3.0",
                skill_sha256="b" * 64,
                tool_name="build_financial_artifacts",
            ),
            selection=SkillSelection(
                task=kwargs["task"],
                selected_skill="financial-analysis",
                matched_keywords=["财务", "现金流"],
                rationale="离线测试",
            ),
            loaded_skill_path=".agents/skills/financial-analysis/SKILL.md",
            tool_name="build_financial_artifacts",
            artifacts=artifacts,
        )


def _runtime(tmp_path: Path, *, model_override=None) -> FinancialResearchAgentRuntime:
    return FinancialResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(tmp_path),
        model_override=model_override,
        skill_runtime_factory=_FakeFinancialSkillRuntime,
        clock=lambda: FIXED_TIME,
        trace_id_factory=lambda: "AF-TEST",
    )


def _fake_skill_result(tmp_path: Path) -> SkillExecutionResult:
    prepared = _prepared_inputs(tmp_path)
    return _FakeFinancialSkillRuntime(PROJECT_ROOT).execute(
        task=prepared["task"],
        identity=prepared["identity"],
        source=prepared["source"],
        tool_arguments={"output_dir": tmp_path / "standalone"},
        trace_output=tmp_path / "standalone_skill_trace.json",
    )


def _scripted_model(final_output: str) -> ScriptedModel:
    return ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="financial-call",
                    )
                ],
                usage=Usage(requests=1, input_tokens=10, output_tokens=10, total_tokens=20),
            ),
            ModelStep(
                output=[assistant_message(final_output)],
                usage=Usage(requests=1, input_tokens=20, output_tokens=40, total_tokens=60),
            ),
        ]
    )


def _simple_draft(*, legacy_ids: bool = False) -> Dict[str, Any]:
    section: Dict[str, Any] = {
        "topic": "executive_summary",
        "heading": "授信结论摘要",
        "analysis_markdown": "【模型原文哨兵-7E9A】收入和现金流需要联动观察。",
    }
    question: Dict[str, Any] = {"question": "请提供期后回款和最新借款台账。"}
    if legacy_ids:
        section.update(
            section_id="FS001",
            metric_ids=["FM024"],
            fact_ids=["F101"],
            source_ids=["S01"],
        )
        section["analysis_markdown"] += "[FM024][F101][S01]"
        question.update(question_id="FQ001", supporting_metric_ids=["FM024"])
    return {
        "report_title": "测试企业财务分析",
        "sections": [section],
        "due_diligence_questions": [question],
    }


def test_financial_function_tool_only_exposes_ticker() -> None:
    tool = create_financial_function_tool()
    assert tool.name == FINANCIAL_FUNCTION_TOOL
    assert set(tool.params_json_schema["properties"]) == {"ticker"}
    assert tool.params_json_schema["required"] == ["ticker"]
    assert tool.params_json_schema["additionalProperties"] is False


def test_analysis_context_contains_data_but_not_internal_ids(tmp_path: Path) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "营业收入" in serialized
    assert "FY2024" in serialized and "FY2025" in serialized
    assert "20.00" in serialized
    assert "公司披露主要因销售回款增加" in serialized
    assert "公开主表未形成可比值" in serialized
    assert "2025年年度报告" in serialized
    assert "metric_id" not in serialized
    assert "fact_id" not in serialized
    assert "source_id" not in serialized
    schema = payload["response_contract"]["final_response_schema"]
    assert set(schema["sections"][0]) == {"topic", "heading", "analysis_markdown"}
    assert set(schema["due_diligence_questions"][0]) == {"question"}


def test_analysis_context_and_tables_use_latest_available_periods(tmp_path: Path) -> None:
    result = _fake_skill_result(tmp_path)
    metrics_path = Path(result.artifacts["metrics_path"])
    metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    for metric in metrics_payload["metrics"]:
        metric["prior_period"] = "FY2025"
        metric["current_period"] = "FY2026"
    metrics_payload["ratios"] = [
        {
            "ratio_id": "FR001",
            "name": "资产负债率",
            "period": period,
            "value_percent": value,
            "formula": "负债合计/资产总计*100%",
            "supporting_fact_ids": [],
        }
        for period, value in (("FY2025", "40.00"), ("FY2026", "42.00"))
    ]
    metrics_path.write_text(
        json.dumps(metrics_payload, ensure_ascii=False), encoding="utf-8"
    )

    payload = _build_financial_analysis_context(result)
    assert payload["analysis_periods"] == {
        "available": ["FY2025", "FY2026"],
        "comparison": ["FY2025", "FY2026"],
        "latest": "FY2026",
    }
    assert {item["current_period"] for item in payload["metrics"]} == {"FY2026"}
    draft = parse_financial_narrative_draft(
        json.dumps(_simple_draft(), ensure_ascii=False)
    )
    report = render_llm_financial_report(draft, payload, model_mode="live")
    assert report.startswith(
        "# 石家庄以岭药业股份有限公司（002603.SZ）2026年度财务分析"
    )
    assert "| 比率 | FY2025 | FY2026 | 计算口径 |" in report
    assert "FY2025 未取得 | FY2026 未取得" in report
    assert "FY2024 未取得" not in report


def test_parser_accepts_simplified_and_legacy_drafts_without_id_contract() -> None:
    simplified = parse_financial_narrative_draft(
        json.dumps(_simple_draft(), ensure_ascii=False)
    )
    legacy = parse_financial_narrative_draft(
        json.dumps(_simple_draft(legacy_ids=True), ensure_ascii=False)
    )
    assert simplified.sections[0].analysis_markdown.startswith("【模型原文哨兵")
    assert legacy.sections[0].topic == "executive_summary"
    assert not hasattr(legacy.sections[0], "fact_ids")
    assert not hasattr(legacy.due_diligence_questions[0], "supporting_metric_ids")


def test_renderer_preserves_llm_prose_and_hides_legacy_markers(tmp_path: Path) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    draft = parse_financial_narrative_draft(
        json.dumps(_simple_draft(legacy_ids=True), ensure_ascii=False)
    )
    report = render_llm_financial_report(draft, payload, model_mode="live")
    assert "【模型原文哨兵-7E9A】" in report
    assert "## 核心财务数据" in report
    assert "FY2024 1.00" in report
    assert "FY2025 1.20" in report
    assert "20.00%" in report
    assert "请提供期后回款和最新借款台账" in report
    assert not TECHNICAL_MARKER.search(report)
    assert "数据来源附录" not in report


def test_renderer_keeps_normal_bracketed_text_while_hiding_internal_ids(
    tmp_path: Path,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    raw = _simple_draft(legacy_ids=True)
    raw["sections"][0]["analysis_markdown"] = (
        "报告期为[FY2025]，关注等级为[IMPORTANT]。[F101][FM024][S01]"
    )
    draft = parse_financial_narrative_draft(json.dumps(raw, ensure_ascii=False))
    report = render_llm_financial_report(draft, payload, model_mode="live")
    assert "[FY2025]" in report
    assert "[IMPORTANT]" in report
    assert "[F101]" not in report
    assert "[FM024]" not in report
    assert "[S01]" not in report


def test_report_title_uses_runtime_bound_company_identity(tmp_path: Path) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    raw = _simple_draft()
    raw["report_title"] = "错误企业（999999.XX）财务分析"
    draft = parse_financial_narrative_draft(json.dumps(raw, ensure_ascii=False))
    report = render_llm_financial_report(draft, payload, model_mode="live")
    assert report.startswith("# 石家庄以岭药业股份有限公司（002603.SZ）2025年度财务分析")
    assert "错误企业（999999.XX）" not in report


def test_missing_questions_is_partial_but_report_remains_deliverable(
    tmp_path: Path,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    raw = _simple_draft()
    raw["due_diligence_questions"] = []
    draft = parse_financial_narrative_draft(json.dumps(raw, ensure_ascii=False))
    manifest = _build_delivery_manifest(
        draft,
        payload,
        provider="deepseek",
        model="deepseek-flash",
        mode="live",
    )
    report = render_llm_financial_report(draft, payload, model_mode="live")
    assert manifest["status"] == "partial"
    assert "due_diligence_questions" in manifest["missing_components"]
    assert "报告完整性：部分完成" in report
    assert "尚未生成尽调问题" in report
    assert "【模型原文哨兵-7E9A】" in report


def test_mock_agent_directly_delivers_model_prose_and_manifest(tmp_path: Path) -> None:
    _FakeFinancialSkillRuntime.calls.clear()
    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path))
    sentinel = (
        "【离线Mock哨兵】本段模拟 DeepSeek 的财务分析正文，仅用于验证"
        "模型原文不会被 Python 模板替换。"
    )
    assert result.selected_skill == "financial-analysis"
    assert result.tool_name == "build_financial_artifacts"
    assert sentinel in result.final_output
    assert "ScriptedModel 的离线模拟响应" in result.final_output
    assert not TECHNICAL_MARKER.search(result.final_output)
    analysis_path = Path(result.artifacts["analysis_path"])
    assert analysis_path.read_text(encoding="utf-8") == result.final_output
    manifest = json.loads(
        Path(result.artifacts["llm_delivery_path"]).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete"
    assert manifest["status_basis"] == "required_topics_and_minimum_json_structure"
    assert manifest["mode"] == "mock"
    assert manifest["llm_authored_report"] is False
    assert manifest["content_validation_gate"] == "disabled"
    assert manifest["python_rewrote_llm_text"] is False
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["selected_tool"] == FINANCIAL_FUNCTION_TOOL
    assert trace["selected_skill"] == "financial-analysis"
    assert trace["tool_call_arguments"][0]["runtime_bound"] == {
        "source_id": "S01",
        "comparison_source_ids": ["S10"],
    }
    assert any(
        event["stage"] == "structured_output_parsed"
        for event in trace["events"]
    )


def test_model_cannot_override_financial_runtime_source(tmp_path: Path) -> None:
    _FakeFinancialSkillRuntime.calls.clear()
    scripted = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ", "source_id": "S99"},
                        call_id="bad-financial-call",
                    )
                ],
                usage=Usage(requests=1, input_tokens=10, output_tokens=10, total_tokens=20),
            )
        ]
    )
    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path, model_override=scripted).run_sync(
            **_prepared_inputs(tmp_path)
        )
    assert error.value.code == "tool_runtime_override_forbidden"
    assert not _FakeFinancialSkillRuntime.calls


def test_invalid_model_json_fails_without_python_narrative_fallback(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path, model_override=_scripted_model("not-json")).run_sync(
            **_prepared_inputs(tmp_path)
        )
    assert error.value.code == "invalid_financial_narrative_json"
    artifact_dir = tmp_path / "financial" / "fake"
    assert (artifact_dir / "facts.json").is_file()
    assert (artifact_dir / "metrics.json").is_file()
    assert (artifact_dir / "financial_llm_draft.json").is_file()
    delivery = json.loads(
        (artifact_dir / "financial_llm_delivery.json").read_text(encoding="utf-8")
    )
    assert delivery["status"] == "failed"
    assert (artifact_dir / "analysis.md").read_text(encoding="utf-8") == (
        "# 等待 DeepSeek 分析\n"
    )


def test_saved_live_draft_restores_all_sections_and_questions_without_api(
    tmp_path: Path,
) -> None:
    source_dir = (
        PROJECT_ROOT
        / "examples"
        / "v2_3_financial_live_narrative_50000_timeout120"
        / "CN-A-SZSE-002603"
    )
    required = [
        "financial_llm_draft.json",
        "financial_llm_reviewed.json",
        "financial_llm_reviewed.metadata.json",
        "financial_facts.json",
        "financial_metrics.json",
        "financial_explanations.json",
        "financial_reasonableness.json",
        "financial_risk_flags.json",
        "financial_sources.json",
    ]
    for name in required:
        assert (source_dir / name).is_file(), f"缺少已保存的Live产物：{name}"
        shutil.copy2(source_dir / name, tmp_path / name)
    checked_in_report = (source_dir / "financial_analysis.md").read_text(
        encoding="utf-8"
    )
    raw_draft_before = (tmp_path / "financial_llm_draft.json").read_bytes()
    restored = restore_financial_report_from_saved_draft(
        tmp_path / "financial_llm_draft.json"
    )
    report = restored["analysis_path"].read_text(encoding="utf-8")
    questions = restored["questions_path"].read_text(encoding="utf-8")
    manifest = json.loads(
        restored["delivery_manifest_path"].read_text(encoding="utf-8")
    )
    reviewed = json.loads(
        (tmp_path / "financial_llm_reviewed.json").read_text(encoding="utf-8")
    )
    assert set(reviewed) == {
        "report_title",
        "sections",
        "due_diligence_questions",
    }
    assert len(reviewed["sections"]) == 9
    assert len(reviewed["due_diligence_questions"]) == 9
    assert all(
        set(section) == {"topic", "heading", "analysis_markdown"}
        for section in reviewed["sections"]
    )
    assert all(
        set(question) == {"question"}
        for question in reviewed["due_diligence_questions"]
    )
    assert not TECHNICAL_MARKER.search(json.dumps(reviewed, ensure_ascii=False))
    assert manifest["section_count"] == 9
    assert manifest["question_count"] == 9
    assert manifest["status"] == "complete"
    assert manifest["llm_authored_report"] is True
    assert manifest["human_reviewed_presentation_copy"] is True
    assert manifest["reviewed_input_path"] == "financial_llm_reviewed.json"
    assert manifest["reviewed_base_draft_sha256"] == (
        "7404d6f82f7a5495edc26e5f53cd892f951bb958137425e0229e4d25302c3612"
    )
    assert manifest["editorial_review_scope"] == (
        "人工审阅展示副本；Python未执行逐句内容差异或事实审计"
    )
    assert report == checked_in_report
    assert (tmp_path / "financial_llm_draft.json").read_bytes() == raw_draft_before
    for section in reviewed["sections"]:
        assert section["heading"] in report
        assert section["analysis_markdown"] in report
        assert section["heading"] in checked_in_report
        assert section["analysis_markdown"] in checked_in_report
    for question in reviewed["due_diligence_questions"]:
        assert question["question"] in report
        assert question["question"] in questions
        assert question["question"] in checked_in_report
    assert "## 核心财务数据" in report
    assert "FY2024 65.13" in report
    assert "FY2025 78.31" in report
    assert "20.24%" in report
    assert "短期借款" in report
    assert "无法确认" in report
    assert not TECHNICAL_MARKER.search(report)
    assert "数据来源附录" not in report
    assert "LLM 财务分析状态：FAILED" not in report

    # 审阅展示副本必须与原始模型草稿绑定，防止新草稿误配旧审阅稿。
    (tmp_path / "financial_llm_draft.json").write_bytes(raw_draft_before + b" ")
    with pytest.raises(AgentRuntimeError) as stale_error:
        restore_financial_report_from_saved_draft(
            tmp_path / "financial_llm_draft.json"
        )
    assert stale_error.value.code == "stale_financial_review_copy"
    (tmp_path / "financial_llm_draft.json").write_bytes(raw_draft_before)

    # 移除审阅副本后仍可明确回退到不可变的原始模型草稿。
    (tmp_path / "financial_llm_reviewed.json").unlink()
    raw_restore = restore_financial_report_from_saved_draft(
        tmp_path / "financial_llm_draft.json",
        output_path=tmp_path / "financial_analysis_raw.md",
        questions_path=tmp_path / "financial_questions_raw.md",
        delivery_manifest_path=tmp_path / "financial_llm_delivery_raw.json",
    )
    raw_report = raw_restore["analysis_path"].read_text(encoding="utf-8")
    raw_manifest = json.loads(
        raw_restore["delivery_manifest_path"].read_text(encoding="utf-8")
    )
    assert "呼吸系统类产品为主要方向性支撑" in raw_report
    assert raw_manifest["human_reviewed_presentation_copy"] is False
    assert raw_manifest["editorial_review_scope"] is None
    assert raw_manifest["reviewed_input_path"] is None
    assert raw_manifest["reviewed_base_draft_sha256"] is None
