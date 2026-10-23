"""V2.3 财务 Function Tool 与 Mock Agent 离线测试。"""

from __future__ import annotations

import json
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
    FinancialNarrativeDraft,
    FinancialResearchAgentRuntime,
    _build_financial_analysis_context,
    create_financial_function_tool,
    validate_financial_narrative,
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
        "change_kind": "normal",
        "requires_deep_analysis": requires_deep_analysis,
        "supporting_fact_ids": [fact_id],
        "source_ids": ["S01", "S10"],
    }


FAKE_METRICS = [
    _fake_metric(
        "FM024",
        "operating_revenue",
        "营业收入",
        "F101",
        "100000000",
        "120000000",
        "20000000",
        "20.00",
        requires_deep_analysis=True,
    ),
    _fake_metric(
        "FM004",
        "accounts_receivable",
        "应收账款",
        "F102",
        "10000000",
        "11000000",
        "1000000",
        "10.00",
    ),
    _fake_metric(
        "FM014",
        "inventory",
        "存货",
        "F103",
        "20000000",
        "18000000",
        "-2000000",
        "-10.00",
    ),
    _fake_metric(
        "FM016",
        "monetary_funds",
        "货币资金",
        "F104",
        "5000000",
        "6000000",
        "1000000",
        "20.00",
        requires_deep_analysis=True,
    ),
    _fake_metric(
        "FM002",
        "accounts_payable",
        "应付账款",
        "F105",
        "10000000",
        "9000000",
        "-1000000",
        "-10.00",
    ),
    _fake_metric(
        "FM020",
        "operating_cash_flow",
        "经营活动产生的现金流量净额",
        "F106",
        "2000000",
        "4000000",
        "2000000",
        "100.00",
        requires_deep_analysis=True,
    ),
]


class _FakeFinancialSkillRuntime:
    calls = []

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
        ] + [
            {
                "evidence_id": "E280",
                "source_id": "S01",
                "page_number": 280,
                "quote": "经营活动现金流净额增加主要因销售回款增加。",
            }
        ]
        raw_facts = [
            {
                "fact_id": f"F{100 + index}",
                "subject": identity.legal_name,
                "item": metric["item"],
                "period": "FY2025",
                "value": metric["current_value"],
                "unit": "CNY",
                "evidence_ids": [f"E{index:03d}"],
                "source_ids": ["S01"],
            }
            for index, metric in enumerate(FAKE_METRICS, start=1)
        ] + [
            {
                "fact_id": "F280",
                "subject": identity.legal_name,
                "item": "operating_cash_flow_reason",
                "period": "FY2025",
                "value": None,
                "unit": None,
                "evidence_ids": ["E280"],
                "source_ids": ["S01"],
            }
        ]
        payloads = {
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
            "metrics_path": {
                "metrics": FAKE_METRICS,
                "ratios": [],
            },
            "explanations_path": {
                "explanations": [
                    {
                        "explanation_id": "FX001",
                        "metric_id": "FM020",
                        "kind": "disclosed_reason",
                        "statement": "公司披露主要因销售回款增加。",
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
                    "due_diligence_questions": ["请提供销售回款明细。"],
                }
            ],
            "risk_flags_path": [
                {
                    "flag_id": "RF001",
                    "observation": "经营现金流同比重大变动，需持续核查。",
                }
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
            path.write_text(
                "# Python财务数据附录\n\n[S01] [S10]\n"
                if field_name == "data_appendix_path"
                else "# 尚未取得通过校验的 DeepSeek 分析正文\n",
                encoding="utf-8",
            )
            artifacts[field_name] = str(path)
        artifacts.update(
            fact_count=len(raw_facts),
            metric_count=len(FAKE_METRICS),
            ratio_count=0,
            risk_flag_count=1,
            material_change_count=3,
            explanation_count=1,
            hypothesis_count=0,
            assessment_count=1,
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


def _scripted_financial_model(draft: Dict[str, Any]) -> ScriptedModel:
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
                usage=Usage(
                    requests=1, input_tokens=10, output_tokens=10, total_tokens=20
                ),
            ),
            ModelStep(
                output=[assistant_message(json.dumps(draft, ensure_ascii=False))],
                usage=Usage(
                    requests=1, input_tokens=20, output_tokens=40, total_tokens=60
                ),
            ),
        ]
    )


def _draft_section(
    *,
    section_id: str,
    topic: str,
    heading: str,
    analysis_markdown: str,
    metric_ids: list[str] | None = None,
    ratio_ids: list[str] | None = None,
    fact_ids: list[str] | None = None,
    explanation_ids: list[str] | None = None,
    inference_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    missing_information_ids: list[str] | None = None,
) -> Dict[str, Any]:
    return {
        "section_id": section_id,
        "topic": topic,
        "heading": heading,
        "analysis_markdown": analysis_markdown,
        "metric_ids": metric_ids or [],
        "ratio_ids": ratio_ids or [],
        "fact_ids": fact_ids or [],
        "explanation_ids": explanation_ids or [],
        "inference_ids": inference_ids or [],
        "source_ids": source_ids or [],
        "missing_information_ids": missing_information_ids or [],
    }


def test_financial_function_tool_only_exposes_ticker() -> None:
    tool = create_financial_function_tool()

    assert tool.name == FINANCIAL_FUNCTION_TOOL
    assert set(tool.params_json_schema["properties"]) == {"ticker"}
    assert tool.params_json_schema["required"] == ["ticker"]
    assert tool.params_json_schema["additionalProperties"] is False


def test_analysis_context_always_includes_focus_items_and_grounding_links(
    tmp_path: Path,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))

    assert set(payload["focus_items"]) == {
        "operating_revenue",
        "accounts_receivable",
        "inventory",
        "monetary_funds",
        "short_term_borrowings",
        "accounts_payable",
        "operating_cash_flow",
    }
    metrics = {item["item"]: item for item in payload["metrics"]}
    assert {
        "operating_revenue",
        "accounts_receivable",
        "inventory",
        "monetary_funds",
        "accounts_payable",
        "operating_cash_flow",
    }.issubset(metrics)
    # 低于20%重大变动阈值的重点科目也必须进入模型上下文。
    assert metrics["accounts_receivable"]["requires_deep_analysis"] is False
    assert metrics["inventory"]["requires_deep_analysis"] is False
    assert metrics["accounts_payable"]["requires_deep_analysis"] is False
    assert metrics["operating_cash_flow"]["supporting_fact_ids"] == ["F106"]
    assert any(item["fact_id"] == "F106" for item in payload["facts"])
    assert any(item["evidence_id"] == "E006" for item in payload["evidence"])
    assert payload["response_contract"]["topic_requirements"][
        "short_term_borrowings_and_debt_structure"
    ]["missing_information_ids"] == ["M201"]
    assert payload["artifacts"]["data_appendix_path"].endswith(
        "data_appendix.md"
    )


@pytest.mark.parametrize(
    ("analysis_markdown", "source_ids", "expected_rule", "reason_fragment"),
    [
        (
            "营业收入同比999.00%。[FM024] [F101] [S01]",
            ["S01"],
            "FIN-NARRATIVE-012",
            "比例 999.00% 未被本段引用指标",
        ),
        (
            "营业收入同比20.00%。[FM024] [F101] [S10]",
            ["S10"],
            "FIN-NARRATIVE-010",
            "Fact F101 与本段来源不匹配",
        ),
        (
            "营业收入同比20.00%，主要因渠道扩张。[FM024] [F101] [S01]",
            ["S01"],
            "FIN-NARRATIVE-014",
            "提出具体因果解释",
        ),
    ],
)
def test_financial_narrative_rejects_unsupported_numbers_sources_and_causes(
    tmp_path: Path,
    analysis_markdown: str,
    source_ids: list[str],
    expected_rule: str,
    reason_fragment: str,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    draft = FinancialNarrativeDraft.model_validate(
        {
            "report_title": "测试财务分析",
            "sections": [
                _draft_section(
                    section_id="FS001",
                    topic="operating_revenue",
                    heading="营业收入",
                    analysis_markdown=analysis_markdown,
                    metric_ids=["FM024"],
                    fact_ids=["F101"],
                    source_ids=source_ids,
                )
            ],
            "due_diligence_questions": [],
        }
    )

    accepted, _, validation = validate_financial_narrative(draft, payload)

    assert accepted == []
    rejected = validation.rejected_sections[0]
    assert expected_rule in rejected.rule_ids
    assert any(reason_fragment in reason for reason in rejected.reasons)


@pytest.mark.parametrize(
    ("analysis_markdown", "expected_rule"),
    [
        (
            "营业收入同比-20.00%。[FM024] [F101] [S01]",
            "FIN-NARRATIVE-012",
        ),
        (
            (
                "营业收入为0.11亿元和应收账款为1.20亿元。"
                "[FM024] [FM004] [F101] [F102] [S01]"
            ),
            "FIN-NARRATIVE-021",
        ),
    ],
)
def test_narrative_rejects_sign_reversal_and_cross_metric_value_swaps(
    tmp_path: Path, analysis_markdown: str, expected_rule: str
) -> None:
    """同段数字属于白名单也不能反转符号或交换科目。"""

    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    draft = FinancialNarrativeDraft.model_validate(
        {
            "report_title": "测试财务分析",
            "sections": [
                _draft_section(
                    section_id="FS020",
                    topic="cross_metric_analysis",
                    heading="跨指标分析",
                    analysis_markdown=analysis_markdown,
                    metric_ids=["FM024", "FM004"],
                    fact_ids=["F101", "F102"],
                    source_ids=["S01"],
                )
            ],
            "due_diligence_questions": [],
        }
    )

    accepted, _, validation = validate_financial_narrative(draft, payload)

    assert accepted == []
    assert expected_rule in validation.rejected_sections[0].rule_ids


def test_narrative_accepts_explicit_decrease_wording_without_negative_sign(
    tmp_path: Path,
) -> None:
    """“下降10%/减少0.02亿元”是负变化的合法自然语言表达。"""

    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    draft = FinancialNarrativeDraft.model_validate(
        {
            "report_title": "测试财务分析",
            "sections": [
                _draft_section(
                    section_id="FS021",
                    topic="inventory",
                    heading="存货",
                    analysis_markdown=(
                        "存货由0.20亿元变为0.18亿元，同比下降10.00%，"
                        "减少0.02亿元。[FM014] [F103] [S01]"
                    ),
                    metric_ids=["FM014"],
                    fact_ids=["F103"],
                    source_ids=["S01"],
                )
            ],
            "due_diligence_questions": [],
        }
    )

    accepted, _, validation = validate_financial_narrative(draft, payload)

    assert [item.section_id for item in accepted] == ["FS021"]
    assert validation.rejected_sections == []


def test_partial_validation_keeps_llm_prose_and_marks_rejected_section_incomplete(
    tmp_path: Path,
) -> None:
    sentinel = "【保留原文哨兵-7E9A】模型写出的跨科目判断应当原样保留。"
    draft = {
        "report_title": "以岭药业财务分析",
        "sections": [
            _draft_section(
                section_id="FS001",
                topic="executive_summary",
                heading="摘要",
                analysis_markdown=sentinel,
            ),
            _draft_section(
                section_id="FS002",
                topic="operating_revenue",
                heading="营业收入",
                analysis_markdown="营业收入同比999.00%。[FM024] [F101] [S01]",
                metric_ids=["FM024"],
                fact_ids=["F101"],
                source_ids=["S01"],
            ),
        ],
        "due_diligence_questions": [],
    }
    result = _runtime(
        tmp_path, model_override=_scripted_financial_model(draft)
    ).run_sync(**_prepared_inputs(tmp_path))

    assert sentinel in result.final_output
    assert "营业收入同比999.00%" not in result.final_output
    assert "## 未完成章节" in result.final_output
    assert "主要变动与银行授信观察" not in result.final_output
    analysis = Path(result.artifacts["analysis_path"]).read_text(encoding="utf-8")
    assert analysis == result.final_output
    validation = json.loads(
        Path(result.artifacts["llm_validation_path"]).read_text(encoding="utf-8")
    )
    assert validation["status"] == "partial"
    assert validation["accepted_section_ids"] == ["FS001"]
    assert validation["rejected_sections"][0]["section_id"] == "FS002"
    assert "FIN-NARRATIVE-012" in validation["rejected_sections"][0]["rule_ids"]


def test_unknown_ids_reject_only_the_bad_section_without_crashing(
    tmp_path: Path,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    sentinel = "【有效段落】该段不包含未经支持的企业事实。"
    draft = FinancialNarrativeDraft.model_validate(
        {
            "report_title": "测试财务分析",
            "sections": [
                _draft_section(
                    section_id="FS001",
                    topic="executive_summary",
                    heading="摘要",
                    analysis_markdown=sentinel,
                ),
                _draft_section(
                    section_id="FS002",
                    topic="operating_revenue",
                    heading="错误引用",
                    analysis_markdown="营业收入同比99.99%。[FM999] [F999] [S99]",
                    metric_ids=["FM999"],
                    fact_ids=["F999"],
                    source_ids=["S99"],
                ),
            ],
            "due_diligence_questions": [],
        }
    )

    accepted, _, validation = validate_financial_narrative(draft, payload)

    assert [item.section_id for item in accepted] == ["FS001"]
    assert validation.status == "partial"
    bad = next(item for item in validation.rejected_sections if item.section_id == "FS002")
    assert "FIN-NARRATIVE-003" in bad.rule_ids


@pytest.mark.parametrize(
    ("section", "expected_rule"),
    [
        (
            _draft_section(
                section_id="FS010",
                topic="short_term_borrowings_and_debt_structure",
                heading="债务结构",
                analysis_markdown="公司无短期借款。[M201]",
                missing_information_ids=["M201"],
            ),
            "FIN-NARRATIVE-019",
        ),
        (
            _draft_section(
                section_id="FS011",
                topic="operating_revenue",
                heading="营业收入",
                analysis_markdown=(
                    "公司披露营业收入增长主要因销售回款增加。"
                    "[FM024] [F101] [FX001] [S01]"
                ),
                metric_ids=["FM024"],
                fact_ids=["F101"],
                explanation_ids=["FX001"],
                source_ids=["S01"],
            ),
            "FIN-NARRATIVE-022",
        ),
        (
            _draft_section(
                section_id="FS012",
                topic="operating_revenue",
                heading="营业收入",
                analysis_markdown=(
                    "FY2024营业收入为1.20亿元，FY2025营业收入为1.00亿元。"
                    "[FM024] [F101] [S01]"
                ),
                metric_ids=["FM024"],
                fact_ids=["F101"],
                source_ids=["S01"],
            ),
            "FIN-NARRATIVE-012",
        ),
    ],
)
def test_narrative_rejects_missing_data_reversal_reason_mismatch_and_wrong_period(
    tmp_path: Path,
    section: dict[str, Any],
    expected_rule: str,
) -> None:
    payload = _build_financial_analysis_context(_fake_skill_result(tmp_path))
    draft = FinancialNarrativeDraft.model_validate(
        {
            "report_title": "测试财务分析",
            "sections": [section],
            "due_diligence_questions": [],
        }
    )

    accepted, _, validation = validate_financial_narrative(draft, payload)

    assert accepted == []
    assert expected_rule in validation.rejected_sections[0].rule_ids


def test_all_rejected_sections_leave_incomplete_report_without_python_fallback(
    tmp_path: Path,
) -> None:
    draft = {
        "report_title": "以岭药业财务分析",
        "sections": [
            _draft_section(
                section_id="FS001",
                topic="operating_revenue",
                heading="营业收入",
                analysis_markdown="营业收入同比999.00%。[FM024] [F101] [S01]",
                metric_ids=["FM024"],
                fact_ids=["F101"],
                source_ids=["S01"],
            )
        ],
        "due_diligence_questions": [],
    }

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(
            tmp_path, model_override=_scripted_financial_model(draft)
        ).run_sync(**_prepared_inputs(tmp_path))

    assert error.value.code == "financial_narrative_validation_failed"
    analysis_path = tmp_path / "financial" / "fake" / "analysis.md"
    report = analysis_path.read_text(encoding="utf-8")
    assert "离线 Mock 财务分析状态：FAILED" in report
    assert "## 未完成章节" in report
    assert "主要变动与银行授信观察" not in report
    assert "已披露原因与分析判断" not in report
    validation = json.loads(
        (analysis_path.parent / "financial_llm_validation.json").read_text(
            encoding="utf-8"
        )
    )
    assert validation["status"] == "failed"
    # Python 计算产物和数据附录仍然保留，不会被失败正文覆盖。
    assert (analysis_path.parent / "data_appendix.md").is_file()
    assert (analysis_path.parent / "facts.json").is_file()


def test_mock_agent_preserves_llm_prose_and_writes_validation_artifacts(
    tmp_path: Path,
) -> None:
    _FakeFinancialSkillRuntime.calls.clear()

    result = _runtime(tmp_path).run_sync(**_prepared_inputs(tmp_path))

    assert result.selected_skill == "financial-analysis"
    assert result.tool_name == "build_financial_artifacts"
    sentinel = (
        "【离线Mock哨兵】本段模拟 DeepSeek 的财务分析正文，"
        "仅用于验证模型原文不会被 Python 模板替换。"
    )
    assert sentinel in result.final_output
    assert "ScriptedModel 的离线模拟响应" in result.final_output
    assert "不是 DeepSeek 正式研究成果" in result.final_output
    analysis_path = Path(result.artifacts["analysis_path"])
    assert analysis_path.read_text(encoding="utf-8") == result.final_output
    assert "主要变动与银行授信观察" not in result.final_output
    draft = json.loads(Path(result.artifacts["llm_draft_path"]).read_text("utf-8"))
    validation = json.loads(
        Path(result.artifacts["llm_validation_path"]).read_text("utf-8")
    )
    assert sentinel in draft["raw_model_output"]
    assert validation["status"] == "complete"
    assert validation["generation_mode"] == "mock"
    assert validation["llm_authored_report"] is False
    assert validation["python_rewrote_llm_text"] is False
    questions = Path(result.artifacts["questions_path"]).read_text(encoding="utf-8")
    assert "财务尽调问题清单（离线 ScriptedModel）" in questions
    assert "财务尽调问题清单（DeepSeek）" not in questions
    call = _FakeFinancialSkillRuntime.calls[0]
    assert call["source"].source_id == "S01"
    assert call["tool_arguments"]["comparison_inputs"][0].source.source_id == "S10"
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["selected_tool"] == FINANCIAL_FUNCTION_TOOL
    assert trace["selected_skill"] == "financial-analysis"
    assert trace["tool_call_arguments"][0]["runtime_bound"] == {
        "source_id": "S01",
        "comparison_source_ids": ["S10"],
    }
    assert trace["events"][-2]["details"]["python_rewrote_llm_text"] is False


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
                usage=Usage(
                    requests=1, input_tokens=10, output_tokens=10, total_tokens=20
                ),
            )
        ]
    )

    with pytest.raises(AgentRuntimeError) as error:
        _runtime(tmp_path, model_override=scripted).run_sync(
            **_prepared_inputs(tmp_path)
        )

    assert error.value.code == "tool_runtime_override_forbidden"
    assert not _FakeFinancialSkillRuntime.calls

