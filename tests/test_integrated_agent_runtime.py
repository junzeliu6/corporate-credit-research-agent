"""V2.5 单主 Agent 完整授信研究的纯离线测试。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import SecretStr

from credit_research_agent.agent_runtime import OWNERSHIP_FUNCTION_TOOL
from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialSourceInput
from credit_research_agent.financial_agent_runtime import FINANCIAL_FUNCTION_TOOL
from credit_research_agent.industry_agent_runtime import load_industry_search_fixture
from credit_research_agent.integrated_agent_runtime import (
    COMPLETE_AGENT_INSTRUCTIONS,
    COMPLETE_REQUIRED_TOPICS,
    COMPLETE_TOPIC_LABELS,
    CompleteExecutionTrace,
    IntegratedCreditResearchAgentRuntime,
    IntegratedRuntimeError,
    _ownership_chart_tool_view,
    _ownership_reporting_constraints,
    _public_runtime_limitations,
    build_fundamental_snapshot,
    create_complete_research_tools,
    parse_complete_narrative_draft,
    render_complete_report,
    restore_complete_report_from_saved_draft,
)
from credit_research_agent.llm_config import AgentMode, DeepSeekLLMConfig
from credit_research_agent.retrieval_runtime import (
    EXTRACT_EVIDENCE_TOOL,
    FETCH_SOURCE_TOOL,
    SEARCH_WEB_TOOL,
    RetrievalStatus,
    build_mock_retrieval_dependencies,
)
from credit_research_agent.search import BaiduSearchConfig, SearchBudget, SearchMode
from credit_research_agent.search import SearchError
from credit_research_agent.skill_runtime import (
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillExecutionTrace,
)
from credit_research_agent.skills import SkillSelection
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
FULL_TASK = (
    "请研究以岭药业的企业背景、股权结构、经营情况、"
    "行业政策及财务状况，并从银行授信角度撰写完整研究报告。"
)
FUNDAMENTALS_EXAMPLE_PATH = (
    PROJECT_ROOT
    / "examples"
    / "v1_4_report"
    / "research"
    / "fundamentals"
    / "CN-A-SZSE-002603"
    / "fundamentals.json"
)
OWNERSHIP_EXAMPLE_DIR = PROJECT_ROOT / "examples" / "v1_3_runtime" / "CN-A-SZSE-002603"
FINANCIAL_EXAMPLE_DIR = (
    PROJECT_ROOT
    / "examples"
    / "v2_3_financial_live_narrative_50000_timeout120"
    / "CN-A-SZSE-002603"
)
TECHNICAL_MARKER = re.compile(
    r"(?:\[(?:S|F|FM|E)\d+\]|\b(?:fact_id|metric_id|source_id|candidate_id|evidence_id|source_handle)\b)",
    re.I,
)


def _identity():
    return CompanyResolver.from_default_catalog().resolve("002603.SZ")


def _download(tmp_path: Path, source_id: str) -> tuple[Any, DownloadResult, Path, Path]:
    source = SourceCatalog.from_default_catalog().get(source_id)
    pdf_path = tmp_path / f"{source_id}.pdf"
    manifest_path = tmp_path / f"{source_id}.metadata.json"
    summary_path = tmp_path / f"{source_id}.summary.json"
    pages_path = tmp_path / f"{source_id}.pages.jsonl"
    for path in (pdf_path, manifest_path, summary_path, pages_path):
        path.write_text("offline fixture\n", encoding="utf-8")
    download = DownloadResult(
        source_id=source.source_id,
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=source.report_period,
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
    return source, download, summary_path, pages_path


def _draft_payload(
    topics: Iterable[str] = COMPLETE_REQUIRED_TOPICS,
    *,
    sentinel: str = "V25_LLM_SENTINEL：这是模型撰写的专业分析。",
) -> Dict[str, Any]:
    return {
        "report_title": "模型撰写的完整授信研究",
        "sections": [
            {
                "topic": topic,
                "heading": f"模型章节{index}",
                "analysis_markdown": (
                    sentinel
                    if index == 1
                    else f"模型原文第{index}章，需要人工复核。"
                ),
            }
            for index, topic in enumerate(topics, start=1)
        ],
        "due_diligence_questions": [
            {"question": "请提供最新融资台账并说明核心产品政策适用口径。"}
        ],
        "limitations": ["部分外部行业信息仍待核实。"],
    }


def test_fundamental_snapshot_hides_ids_and_rejects_other_entity(tmp_path: Path) -> None:
    identity = _identity()
    payload = {
        "identity": {"entity_id": identity.entity_id},
        "facts": [
            {
                "fact_id": "F008",
                "source_id": "S01",
                "evidence_ids": ["E007"],
                "research_module": "main_business",
                "field_name": "core_business",
                "value": "专利创新中药的研发、生产和销售",
                "claim": "公司主营专利创新中药。",
                "period": "FY2025",
                "entity_scope": "consolidated",
            },
            {
                "fact_id": "F999",
                "research_module": "financial_analysis",
                "field_name": "ignored",
                "value": "ignored",
                "claim": "ignored",
            },
        ],
        "missing_information": [
            {
                "missing_id": "M003",
                "research_module": "main_products",
                "topic": "单品收入",
                "description": "尚未取得单品收入。",
                "reason": "公开披露不足。",
                "evidence_ids": [],
            }
        ],
    }
    path = tmp_path / "fundamentals.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    snapshot = build_fundamental_snapshot(path, identity)
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["available"] is True
    assert snapshot["facts"][0]["value"] == "专利创新中药的研发、生产和销售"
    constraints = snapshot["reporting_constraints"]
    assert constraints["product_income_share_available"] is False
    assert constraints["product_profit_contribution_available"] is False
    assert constraints["unified_profit_model_available"] is False
    assert "ignored" not in serialized
    assert not TECHNICAL_MARKER.search(serialized)
    assert "missing_id" not in serialized

    payload["identity"]["entity_id"] = "CN-A-SZSE-999999"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    mismatch = build_fundamental_snapshot(path, identity)
    assert mismatch == {
        "available": False,
        "reason": "企业基本面产物与当前研究主体不匹配",
    }


def test_parser_accepts_partial_sections_without_python_filling_gaps() -> None:
    topic = COMPLETE_REQUIRED_TOPICS[0]
    draft = parse_complete_narrative_draft(
        "```json\n" + json.dumps(_draft_payload([topic]), ensure_ascii=False) + "\n```"
    )

    assert [item.topic for item in draft.sections] == [topic]
    assert draft.sections[0].analysis_markdown.startswith("V25_LLM_SENTINEL")
    assert len(draft.due_diligence_questions) == 1


def test_parser_keeps_valid_sections_when_one_section_is_malformed() -> None:
    payload = _draft_payload(COMPLETE_REQUIRED_TOPICS[:2])
    payload["sections"].append(
        {
            "topic": "not_a_supported_topic",
            "heading": "无效章节",
            "analysis_markdown": "不应交付",
        }
    )

    draft = parse_complete_narrative_draft(json.dumps(payload, ensure_ascii=False))

    assert [item.topic for item in draft.sections] == list(
        COMPLETE_REQUIRED_TOPICS[:2]
    )
    assert any("1个章节格式无效" in item for item in draft.limitations)


def test_parser_repairs_unescaped_inner_quotes_without_rewriting_prose() -> None:
    payload = _draft_payload([COMPLETE_REQUIRED_TOPICS[0]])
    payload["limitations"] = ['政策名称为《中医药振兴发展"十五五"规划》，需复核。']
    encoded = json.dumps(payload, ensure_ascii=False)
    malformed = encoded.replace('\\"十五五\\"', '"十五五"')

    draft = parse_complete_narrative_draft(malformed)

    assert draft.limitations == ['政策名称为《中医药振兴发展"十五五"规划》，需复核。']


def test_parser_accepts_short_preamble_before_unique_json_fence() -> None:
    payload = _draft_payload([COMPLETE_REQUIRED_TOPICS[0]])
    payload["limitations"] = ['政策名称含有"十五五"字样，需复核。']
    encoded = json.dumps(payload, ensure_ascii=False)
    malformed = encoded.replace('\\"十五五\\"', '"十五五"')
    model_output = (
        "资料已按预算约束收尾。以下提交完整研究草稿。\n\n"
        "```json\n"
        + malformed
        + "\n```"
    )

    draft = parse_complete_narrative_draft(model_output)

    assert [item.topic for item in draft.sections] == [COMPLETE_REQUIRED_TOPICS[0]]
    assert draft.limitations == ['政策名称含有"十五五"字样，需复核。']


def test_parser_accepts_complete_leading_report_before_placeholder_restatement() -> None:
    payload = _draft_payload()
    for index, section in enumerate(payload["sections"], start=1):
        section["analysis_markdown"] = (
            f"V25_LLM_SENTINEL：这是模型撰写的第{index}章完整专业分析，"
            "包含企业事实、跨模块判断、资料限制和需要进一步核实的事项。"
        )
    payload["reporting_note"] = "模型误加的非契约字段"
    complete_json = json.dumps(payload, ensure_ascii=False)
    redundant_tail = """

上述 JSON 中末尾多了一个非规范字段，最终提交版本以以下规范 JSON 为准（内容一致）：

{
  "report_title": "模型撰写的完整授信研究",
  "sections": [
    {"topic": "company_background_and_ownership", "heading": "第一章", "analysis_markdown": "见上"}
  ],
  "due_diligence_questions": [省略，同上],
  "limitations": [省略，同上]
}
"""

    draft = parse_complete_narrative_draft(complete_json + redundant_tail)

    assert [item.topic for item in draft.sections] == list(COMPLETE_REQUIRED_TOPICS)
    assert len(draft.due_diligence_questions) == 1
    assert draft.sections[0].analysis_markdown.startswith("V25_LLM_SENTINEL")


def test_parser_rejects_two_complete_report_objects() -> None:
    payload = _draft_payload()
    complete_json = json.dumps(payload, ensure_ascii=False)
    model_output = (
        complete_json
        + "\n\n上述 JSON 需要修正，最终提交版本以以下规范 JSON 为准（内容一致）：\n\n"
        + complete_json
    )

    with pytest.raises(IntegratedRuntimeError) as exc_info:
        parse_complete_narrative_draft(model_output)

    assert exc_info.value.code == "invalid_complete_narrative_json"


def test_parser_rejects_ambiguous_or_incomplete_fenced_output() -> None:
    valid_json = json.dumps(
        _draft_payload([COMPLETE_REQUIRED_TOPICS[0]]),
        ensure_ascii=False,
    )
    invalid_outputs = [
        "说明\n```json\n"
        + valid_json
        + "\n```\n```json\n"
        + valid_json
        + "\n```",
        "说明\n```json\n" + valid_json,
        "```json\n" + valid_json,
        '说明\n```json\n{"report_title":"未完成","sections":[\n```',
        "说" * 201 + "\n```json\n" + valid_json + "\n```",
    ]

    for model_output in invalid_outputs:
        with pytest.raises(IntegratedRuntimeError) as exc_info:
            parse_complete_narrative_draft(model_output)

        assert exc_info.value.code == "invalid_complete_narrative_json"


def test_parser_does_not_guess_truncated_or_non_json_output() -> None:
    with pytest.raises(IntegratedRuntimeError) as exc_info:
        parse_complete_narrative_draft('{"report_title":"未完成","sections":[')

    assert exc_info.value.code == "invalid_complete_narrative_json"


def test_saved_complete_draft_can_be_recovered_offline_after_quote_repair(
    tmp_path: Path,
) -> None:
    identity = _identity()
    payload = _draft_payload()
    payload["limitations"].append('政策名称含有"十五五"字样。')
    encoded = json.dumps(payload, ensure_ascii=False)
    malformed = encoded.replace('\\"十五五\\"', '"十五五"')
    draft_path = tmp_path / "credit_research_llm_draft.json"
    draft_path.write_text(
        json.dumps(
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "mode": "live",
                "raw_model_output": malformed,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "credit_research_context.json").write_text(
        json.dumps(
            {
                "company": {
                    "legal_name": identity.legal_name,
                    "short_name": identity.short_name,
                    "ticker": identity.ticker,
                    "exchange": identity.exchange,
                    "entity_scope": identity.entity_scope.value,
                },
                "module_results": {"financial": {}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "credit_research_execution_trace.json").write_text(
        json.dumps(
            {
                "entity_id": identity.entity_id,
                "status": "failed",
                "error_code": "invalid_complete_narrative_json",
                "degraded_reasons": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    restored = restore_complete_report_from_saved_draft(draft_path)
    report = restored["report_path"].read_text(encoding="utf-8")
    manifest = json.loads(
        restored["recovery_manifest_path"].read_text(encoding="utf-8")
    )

    assert "V25_LLM_SENTINEL：这是模型撰写的专业分析。" in report
    assert '政策名称含有"十五五"字样。' in report
    assert manifest["status"] == "recovered_offline"
    assert manifest["network_requests_during_recovery"] == 0
    assert manifest["llm_text_rewritten"] is False
    assert manifest["question_count"] == 1


def test_report_keeps_llm_prose_inserts_graph_and_financial_table_without_ids(
    tmp_path: Path,
) -> None:
    raw = _draft_payload(
        sentinel=(
            "## 模型小标题\n\n"
            "V25_LLM_SENTINEL：模型原文不能被 Python 模板替换。"
            "[S01] fact_id FM002 DOC003\n\n"
            "### 模型子标题\n\n正文。"
        )
    )
    raw["due_diligence_questions"][0]["question"] += "[FM002]"
    draft = parse_complete_narrative_draft(json.dumps(raw, ensure_ascii=False))
    graph = tmp_path / "assets" / "ownership_structure.svg"
    graph.parent.mkdir(parents=True)
    graph.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    financial = {
        "verified_financial_metrics": [
            {
                "item": "operating_revenue",
                "item_label": "营业收入",
                "prior_value": "100000000",
                "current_value": "120000000",
                "change_display": "20.00%",
            }
        ],
        "verified_ratios": [
            {"name": "资产负债率", "period": "FY2025", "value_percent": "35.50"}
        ],
    }

    report = render_complete_report(
        draft,
        identity=_identity(),
        status=RetrievalStatus.PARTIAL_SUCCESS,
        mock_mode=False,
        ownership_image=graph,
        report_dir=tmp_path,
        financial_snapshot=financial,
        runtime_limitations=(
            "未成功取得外部网页或PDF原文",
            "未完成外部原文证据提取",
            "S05:fetch_http_error",
            "search_web:search_call_limit_exceeded",
        ),
    )

    assert "V25_LLM_SENTINEL：模型原文不能被 Python 模板替换。" in report
    assert "### 模型小标题" in report
    assert "#### 模型子标题" in report
    assert len(re.findall(r"^## (?!#)", report, flags=re.MULTILINE)) == 6
    assert all(f"## {COMPLETE_TOPIC_LABELS[topic]}" in report for topic in COMPLETE_REQUIRED_TOPICS)
    assert "![股权结构图](assets/ownership_structure.svg)" in report
    assert "可能不代表完整股东名册或完整股权穿透" in report
    assert "### 重点财务数据" in report
    assert "| 营业收入 | 1.00 | 1.20 | 20.00% |" in report
    assert "| 资产负债率 | 35.50% |" in report
    assert "请提供最新融资台账" in report
    assert "外部检索已执行，但未取得可核验原文" in report
    assert report.count("外部检索已执行，但未取得可核验原文") == 1
    assert "检索次数已达到本次运行预算上限" in report
    assert ":fetch_http_error" not in report
    assert "search_web:search_call_limit_exceeded" not in report
    assert not TECHNICAL_MARKER.search(report)


def test_report_skips_ownership_block_when_no_image_without_affecting_other_sections(
    tmp_path: Path,
) -> None:
    draft = parse_complete_narrative_draft(
        json.dumps(_draft_payload(), ensure_ascii=False)
    )

    report = render_complete_report(
        draft,
        identity=_identity(),
        status=RetrievalStatus.SUCCEEDED,
        mock_mode=False,
        ownership_image=None,
        report_dir=tmp_path,
        financial_snapshot=None,
    )

    assert "## 第一章：企业基本情况" in report
    assert "## 第二章：经营情况分析" in report
    assert "## 第六章：待补充资料及尽调问题" in report
    assert "### 股权结构图" not in report
    assert "![股权结构图]" not in report


def test_ownership_tool_view_exposes_chart_status_but_not_equity_narrative_data(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "ownership_structure.svg"
    graph.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    snapshot = {
        "as_of_date": "2025-12-31",
        "formal_ready": True,
        "ownership_relations": [
            {"holder": "不应发送给主模型的股东", "percentage": 31.53}
        ],
        "control_relationships": [
            {"relation_type": "actual_controller", "subjects": ["不应发送的控制人"]}
        ],
        "skill_method": {"available": True, "instructions_markdown": "完整规则"},
    }

    view = _ownership_chart_tool_view(snapshot, {"svg_path": str(graph)})
    serialized = json.dumps(view, ensure_ascii=False)

    assert view["reporting_scope"] == "chart_only"
    assert view["chart_generated"] is True
    assert view["as_of_date"] == "2025-12-31"
    assert view["skill_method"]["available"] is True
    assert "31.53" not in serialized
    assert "不应发送给主模型的股东" not in serialized
    assert "不应发送的控制人" not in serialized
    assert "完整规则" not in serialized


def test_complete_shareholder_register_alone_does_not_prove_no_state_ownership() -> None:
    constraints = _ownership_reporting_constraints(
        {
            "ownership_relations": [
                {
                    "holder": "某机构股东",
                    "held_entity": "目标公司",
                    "percentage": 100,
                    "percentage_origin": "disclosed",
                }
            ],
            "missing_information": [],
        }
    )

    assert constraints["complete_shareholder_register_available"] is True
    assert constraints["state_ownership_assessment"] == "not_assessed"
    assert constraints["may_conclude_absence_of_state_ownership"] is False


@pytest.mark.parametrize(
    "code",
    [
        "search_round_limit_exceeded",
        "search_call_limit_exceeded",
        "supplemental_search_limit_exceeded",
        "search_budget_exceeded",
        "fetch_budget_exceeded",
        "evidence_extraction_budget_exceeded",
        "total_tool_budget_exceeded",
    ],
)
def test_runtime_budget_codes_are_humanized_for_markdown(code: str) -> None:
    limitations = _public_runtime_limitations([f"search_web:{code}"])

    assert len(limitations) == 1
    assert code not in limitations[0]
    assert "已达到本次运行预算上限" in limitations[0]


def test_complete_agent_instructions_forbid_known_live_overclaims() -> None:
    """Live复盘发现的过度外推应由数据边界约束，而非事后重写正文。"""

    for required in (
        "第一章是“企业基本情况”",
        "正文不得介绍股东名单、持股比例、控股股东、实际控制人",
        "企业历史按时间顺序写到研究日",
        "当前行业/产业链定位",
        "上游和下游分别承担的角色",
        "默认全局搜索上限为15次",
        "资料充分时应提前停止",
        "第五章必须关联经营、行业政策及财务表现中的至少两个维度",
        "只有取得明确原文支持的控制权变更、资金占用、重大关联交易、股权质押",
        "不得据此断言“无国资背景”",
        "不得写“高度依赖少数品种”“大单品驱动”",
        "不得把两期变化表述为“长期”“连续多年”",
        "只能引用 Python 返回的增量贡献率",
        "不得确定性声称“营运资金占用总体改善/收敛”",
        "不得把毛利率定性为“高毛利”“行业领先”",
        "search_web 返回候选后应先 fetch_source",
        "搜索预算耗尽只停止新增 search_web",
        "基础法律、长期政策或现行监管制度时默认 recency=null",
        "栏目/列表页时，应继续查找并读取具体政策详情页",
    ):
        assert required in COMPLETE_AGENT_INSTRUCTIONS


def test_skill_tool_schemas_expose_only_ticker() -> None:
    tools = {tool.name: tool for tool in create_complete_research_tools()}
    for name in (OWNERSHIP_FUNCTION_TOOL, FINANCIAL_FUNCTION_TOOL):
        schema = tools[name].params_json_schema
        assert set(schema["properties"]) == {"ticker"}
        assert schema["required"] == ["ticker"]
        assert schema["additionalProperties"] is False
        serialized = json.dumps(schema, ensure_ascii=False)
        assert "source_id" not in serialized
        assert "output_dir" not in serialized
        assert "pages_path" not in serialized


def test_shared_total_budget_counts_both_skill_tools() -> None:
    """两项 Skill 与检索 Tool 共用 SearchBudget 的总 Function Tool 计数器。"""

    budget = SearchBudget(max_total_tool_calls=2)
    budget.begin_tool_call(OWNERSHIP_FUNCTION_TOOL)
    budget.begin_tool_call(FINANCIAL_FUNCTION_TOOL)

    assert budget.total_tool_calls == 2
    with pytest.raises(SearchError) as error:
        budget.begin_tool_call("search_web")
    assert error.value.code == "total_tool_budget_exceeded"
    assert budget.total_tool_calls == 2


def test_live_modes_require_two_confirmations_before_model_or_provider_use(
    tmp_path: Path,
) -> None:
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.LIVE,
            api_key=SecretStr("deepseek-test-secret"),
            max_turns=10,
            max_tool_calls=10,
        ),
        BaiduSearchConfig(
            mode=SearchMode.LIVE,
            api_key=SecretStr("baidu-test-secret"),
        ),
        model_override=ScriptedModel([]),
        search_provider_override=object(),
        source_fetcher_override=object(),
    )
    kwargs = {
        "task": FULL_TASK,
        "identity": _identity(),
        "source": source,
        "download": download,
        "summary_path": summary_path,
        "pages_path": pages_path,
        "comparison_inputs": [
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        "fundamentals_path": tmp_path / "never-read.json",
        "output_dir": tmp_path / "complete",
        "trace_output": tmp_path / "trace.json",
    }

    # 两项确认都在协程第一次挂起前校验，因此可直接推进协程，证明此时尚未
    # 创建模型客户端、调用Provider或写入输出目录。
    with pytest.raises(IntegratedRuntimeError) as llm_error:
        runtime.run(**kwargs).send(None)
    assert llm_error.value.code == "live_llm_confirmation_required"
    assert not (tmp_path / "complete").exists()

    with pytest.raises(IntegratedRuntimeError) as search_error:
        runtime.run(**kwargs, confirmed_llm_live=True).send(None)
    assert search_error.value.code == "live_search_confirmation_required"
    assert not (tmp_path / "complete").exists()


class _ArtifactBackedSkillRuntime:
    """以已入库的离线制品模拟Skill执行，不触发PDF或网络。"""

    calls: list[str] = []

    def __init__(self, _project_root: Path) -> None:
        pass

    def execute(self, **kwargs: Any) -> SkillExecutionResult:
        selected = str(kwargs["selected_skill"])
        self.__class__.calls.append(selected)
        trace_path = Path(kwargs["trace_output"])
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("{}\n", encoding="utf-8")
        if selected == "ownership-structure":
            artifacts = {
                "entity_id": _identity().entity_id,
                "dataset_path": str(OWNERSHIP_EXAMPLE_DIR / "ownership.json"),
                "graph_path": str(OWNERSHIP_EXAMPLE_DIR / "ownership_graph.json"),
                "validation_path": str(
                    OWNERSHIP_EXAMPLE_DIR / "ownership_validation.json"
                ),
                "svg_path": str(OWNERSHIP_EXAMPLE_DIR / "ownership_structure.svg"),
                "png_path": str(OWNERSHIP_EXAMPLE_DIR / "ownership_structure.png"),
                "dot_path": str(OWNERSHIP_EXAMPLE_DIR / "ownership_structure.dot"),
                "render_engine": "graphviz-dot",
            }
            tool_name = "build_ownership_artifacts"
            version = "1.3.2"
        else:
            artifacts = {
                "entity_id": _identity().entity_id,
                "facts_path": str(FINANCIAL_EXAMPLE_DIR / "financial_facts.json"),
                "metrics_path": str(FINANCIAL_EXAMPLE_DIR / "financial_metrics.json"),
                "explanations_path": str(
                    FINANCIAL_EXAMPLE_DIR / "financial_explanations.json"
                ),
                "reasonableness_path": str(
                    FINANCIAL_EXAMPLE_DIR / "financial_reasonableness.json"
                ),
                "risk_flags_path": str(
                    FINANCIAL_EXAMPLE_DIR / "financial_risk_flags.json"
                ),
                "sources_path": str(FINANCIAL_EXAMPLE_DIR / "financial_sources.json"),
            }
            tool_name = "build_financial_artifacts"
            version = "2.3.0"
        return SkillExecutionResult(
            trace_path=trace_path,
            trace=SkillExecutionTrace(
                trace_id=f"TR-{selected}",
                task=str(kwargs["task"]),
                status=SkillExecutionStatus.SUCCEEDED,
                entity_id=_identity().entity_id,
                source_id=kwargs["source"].source_id,
                selected_skill=selected,
                skill_version=version,
                skill_sha256="b" * 64,
                tool_name=tool_name,
            ),
            selection=SkillSelection(
                task=str(kwargs["task"]),
                selected_skill=selected,
                matched_keywords=["离线测试"],
                rationale="使用已入库制品进行V2.5编排测试",
            ),
            loaded_skill_path=f".agents/skills/{selected}/SKILL.md",
            skill_instructions=(
                PROJECT_ROOT / ".agents" / "skills" / selected / "SKILL.md"
            ).read_text(encoding="utf-8"),
            tool_name=tool_name,
            artifacts=artifacts,
        )


def test_one_scripted_agent_calls_both_skills_and_delivers_six_chapters(
    tmp_path: Path,
) -> None:
    _ArtifactBackedSkillRuntime.calls.clear()
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(fixture, clock=lambda: FIXED_TIME)
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=30,
            max_tool_calls=60,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
        trace_id_factory=lambda: "CR-V25-OFFLINE",
    )
    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "complete",
        trace_output=tmp_path / "complete_trace.json",
        budget=SearchBudget(max_agent_turns=30, max_total_tool_calls=60),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    report = result.report_path.read_text(encoding="utf-8")
    sources = json.loads(result.sources_path.read_text(encoding="utf-8"))
    context_payload = json.loads(result.context_path.read_text(encoding="utf-8"))
    tool_names = [item["tool_name"] for item in trace["tool_calls"]]

    assert _ArtifactBackedSkillRuntime.calls == [
        "ownership-structure",
        "financial-analysis",
    ]
    assert trace["executed_skills"] == [
        "ownership-structure",
        "financial-analysis",
    ]
    assert OWNERSHIP_FUNCTION_TOOL in tool_names
    assert FINANCIAL_FUNCTION_TOOL in tool_names
    assert result.module_status == {"ownership": "success", "financial": "success"}
    assert result.delivered_topics == list(COMPLETE_REQUIRED_TOPICS)
    assert all(f"## {COMPLETE_TOPIC_LABELS[topic]}" in report for topic in COMPLETE_REQUIRED_TOPICS)
    assert "离线夹具验证主Agent能够把经营、行业政策和财务维度" in report
    assert "![股权结构图](assets/ownership_structure.svg)" in report
    assert "### 重点财务数据" in report
    assert not TECHNICAL_MARKER.search(report)
    assert provider.call_count == 1
    assert fetcher.call_count >= 1
    assert result.search_api_requests == 0
    assert result.estimated_search_cost_cny == 0
    assert context_payload["research_as_of_date"] == "2026-10-23"
    assert context_payload["tool_policy"]["ownership_narrative_scope"] == "chart_only"
    assert any(
        "发展历程" in item.get("query", "") and "产业链" in item.get("query", "")
        for item in trace["search_queries"]
    )
    ownership_snapshot = context_payload["module_results"]["ownership"]
    financial_snapshot = context_payload["module_results"]["financial"]
    assert "graph_paths" not in ownership_snapshot
    assert ownership_snapshot["skill_method"]["available"] is True
    assert "股权比例100%完整性校验" in ownership_snapshot["skill_method"][
        "instructions_markdown"
    ]
    ownership_method = ownership_snapshot["skill_method"]["instructions_markdown"]
    assert "正文无需输出 Fact ID" in ownership_method
    assert "每条分析必须绑定已核实 Fact ID" not in ownership_method
    assert "Python 根据 Fact ID 渲染" not in ownership_method
    ownership_limits = ownership_snapshot["reporting_constraints"]
    assert ownership_limits["complete_shareholder_register_available"] is False
    assert ownership_limits["unlisted_shareholder_residual_present"] is True
    assert ownership_limits["may_conclude_absence_of_state_ownership"] is False
    assert financial_snapshot["skill_method"]["available"] is True
    assert "银行授信财务分析的写作方法" in financial_snapshot["skill_method"][
        "instructions_markdown"
    ]
    financial_limits = financial_snapshot["reporting_constraints"]
    assert financial_limits["supports_long_term_trend_conclusion"] is False
    assert financial_limits["complete_working_capital_bridge_available"] is False
    attribution = financial_limits["current_assets_change_attribution"]
    assert attribution["monetary_funds_contribution_percent"] == "28.92"
    assert attribution["selected_components_fully_reconcile_current_assets"] is False
    assert [item["source_id"] for item in sources["official_sources"]] == [
        "S01",
        "S10",
    ]
    assert all(
        item["runtime_status"] == "parsed" for item in sources["official_sources"]
    )
    assert all(
        Path(item["download"]["file_path"]).is_file()
        and Path(item["extraction"]["pages_path"]).is_file()
        for item in sources["official_sources"]
    )


def test_complete_agent_search_limit_still_fetches_and_extracts_pending_candidate(
    tmp_path: Path,
) -> None:
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    first_url = "https://www.yiling.cn/v2-4-industry-mock"
    second_url = "https://www.gov.cn/zhengce/v2-4-policy-mock"
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="ownership",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="financial",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 中药行业 政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-1",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(FETCH_SOURCE_TOOL, {"url": first_url}, call_id="fetch-1")
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC001",
                            "research_question": "以岭药业主营业务和主要产品",
                        },
                        call_id="extract-1",
                    )
                ]
            ),
            # 第二次搜索超过局部search预算；Runtime应保留第二候选且允许继续fetch/extract。
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "补充中医药监管政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-2",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(FETCH_SOURCE_TOOL, {"url": second_url}, call_id="fetch-2")
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC002",
                            "research_question": "中医药政策监管适用范围",
                        },
                        call_id="extract-2",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(json.dumps(_draft_payload(), ensure_ascii=False))
                ]
            ),
        ]
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=9,
            max_tool_calls=20,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "search-budget-partial",
        trace_output=tmp_path / "search-budget-partial-trace.json",
        budget=SearchBudget(
            max_search_calls=1,
            max_agent_turns=9,
            max_total_tool_calls=20,
        ),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert provider.call_count == 1
    assert fetcher.call_count == 2
    assert trace["fetched_source_handles"] == ["DOC001", "DOC002"]
    assert any(
        item.get("error_code") == "search_call_limit_exceeded"
        for item in trace["tool_calls"]
    )
    assert any(
        item.get("tool_name") == EXTRACT_EVIDENCE_TOOL
        and item.get("model_arguments", {}).get("source_handle") == "DOC002"
        for item in trace["tool_calls"]
    )
    assert result.report_path.is_file()


class _FinancialFailingSkillRuntime(_ArtifactBackedSkillRuntime):
    calls: list[str] = []

    def execute(self, **kwargs: Any) -> SkillExecutionResult:
        selected = str(kwargs["selected_skill"])
        if selected == "financial-analysis":
            self.__class__.calls.append(selected)
            raise RuntimeError("离线夹具模拟财务Skill失败")
        return super().execute(**kwargs)


class _OwnershipFailingSkillRuntime(_ArtifactBackedSkillRuntime):
    calls: list[str] = []

    def execute(self, **kwargs: Any) -> SkillExecutionResult:
        selected = str(kwargs["selected_skill"])
        if selected == "ownership-structure":
            self.__class__.calls.append(selected)
            raise RuntimeError("离线夹具模拟股权资料不完整且无法出图")
        return super().execute(**kwargs)


class _FailingSearchProvider:
    call_count = 0

    def search(self, _query: Any) -> Any:
        self.call_count += 1
        raise SearchError("search_timeout", "离线夹具模拟百度搜索超时")


def test_one_skill_failure_keeps_other_outputs_and_returns_partial_success(
    tmp_path: Path,
) -> None:
    _FinancialFailingSkillRuntime.calls.clear()
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=30,
            max_tool_calls=60,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_FinancialFailingSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "partial",
        trace_output=tmp_path / "partial_trace.json",
        budget=SearchBudget(max_agent_turns=30, max_total_tool_calls=60),
    )
    report = result.report_path.read_text(encoding="utf-8")

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.module_status == {"ownership": "success", "financial": "failed"}
    assert result.ownership_svg_path is not None
    assert result.ownership_svg_path.is_file()
    assert "离线夹具模拟财务Skill失败" in report
    assert "![股权结构图](assets/ownership_structure.svg)" in report
    assert "本章仅保留模型已形成的分析及资料限制" in report


def test_ownership_failure_skips_graph_but_does_not_block_complete_live_delivery(
    tmp_path: Path,
) -> None:
    """使用离线替身模拟Live状态，证明股权图失败不是整份报告门禁。"""

    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    first_url = "https://www.yiling.cn/v2-4-industry-mock"
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="ownership-fails",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="financial-succeeds",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 发展历程 业务定位 产业链",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="history-search",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": first_url},
                        call_id="history-fetch",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC001",
                            "research_question": "以岭药业发展历程、主营业务、业务定位和产业链角色",
                        },
                        call_id="history-extract",
                    )
                ]
            ),
            ModelStep(
                output=[assistant_message(json.dumps(_draft_payload(), ensure_ascii=False))]
            ),
        ]
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.LIVE,
            api_key=SecretStr("deepseek-offline-test-secret"),
            max_turns=10,
            max_tool_calls=20,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(
            mode=SearchMode.LIVE,
            api_key=SecretStr("baidu-offline-test-secret"),
        ),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_OwnershipFailingSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "ownership-nonblocking",
        trace_output=tmp_path / "ownership-nonblocking-trace.json",
        budget=SearchBudget(max_agent_turns=10, max_total_tool_calls=20),
        confirmed_llm_live=True,
        confirmed_search_live=True,
    )
    report = result.report_path.read_text(encoding="utf-8")
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.SUCCEEDED
    assert result.module_status == {"ownership": "failed", "financial": "success"}
    assert result.ownership_svg_path is None
    assert result.ownership_png_path is None
    assert "### 股权结构图" not in report
    assert "离线夹具模拟股权资料不完整且无法出图" not in report
    assert result.delivered_topics == list(COMPLETE_REQUIRED_TOPICS)
    assert trace["module_errors"]["ownership"].startswith("离线夹具模拟股权")


def test_search_failure_keeps_both_skill_outputs_and_delivers_partial_report(
    tmp_path: Path,
) -> None:
    fixture = load_industry_search_fixture()
    _unused_provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    provider = _FailingSearchProvider()
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=10,
            max_tool_calls=20,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "search-failure",
        trace_output=tmp_path / "search_failure_trace.json",
        budget=SearchBudget(max_agent_turns=10, max_total_tool_calls=20),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    report = result.report_path.read_text(encoding="utf-8")

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.module_status == {"ownership": "success", "financial": "success"}
    assert provider.call_count == 1
    assert "search_web:search_timeout" in result.limitations
    assert any(
        item.get("tool_name") == "search_web"
        and item.get("status") == "failed_but_task_continues"
        for item in trace["tool_calls"]
    )
    assert "离线夹具验证主Agent能够把经营、行业政策和财务维度" in report
    assert result.ownership_svg_path is not None


def test_malformed_search_tool_json_is_recoverable_and_does_not_call_provider(
    tmp_path: Path,
) -> None:
    """第三方模型偶发输出畸形Function Calling时，应让模型修正而非终止整个报告。"""

    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    first_url = "https://www.yiling.cn/v2-4-industry-mock"
    malformed = (
        '{"query":"以岭药业 "十五五" 行业政策","max_results":5,'
        '"domain_filter":[],"recency":null}'
    )
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="ownership",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="financial",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        malformed,
                        call_id="malformed-search",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 行业政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="corrected-search",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": first_url},
                        call_id="fetch",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC001",
                            "research_question": "以岭药业主营业务和行业影响",
                        },
                        call_id="extract",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(json.dumps(_draft_payload(), ensure_ascii=False))
                ]
            ),
        ]
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=10,
            max_tool_calls=20,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "malformed-search-recovered",
        trace_output=tmp_path / "malformed-search-recovered-trace.json",
        budget=SearchBudget(max_agent_turns=10, max_total_tool_calls=20),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    report = result.report_path.read_text(encoding="utf-8")
    invalid_calls = [
        item
        for item in trace["tool_calls"]
        if item.get("error_code") == "invalid_tool_arguments"
    ]

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.module_status == {"ownership": "success", "financial": "success"}
    assert provider.call_count == 1
    assert len(trace["search_queries"]) == 1
    assert trace["tool_call_count"] == 6
    assert len(invalid_calls) == 1
    assert invalid_calls[0]["status"] == "invalid_arguments_retryable"
    assert invalid_calls[0]["json_error"]["position"] > 0
    assert invalid_calls[0]["raw_input_length"] == len(malformed)
    assert len(invalid_calls[0]["raw_input_sha256"]) == 64
    assert "raw_input" not in invalid_calls[0]
    assert "十五五" not in json.dumps(invalid_calls[0], ensure_ascii=False)
    assert "V25_LLM_SENTINEL" in report


def test_repeated_invalid_tool_arguments_are_bounded_and_report_can_finish(
    tmp_path: Path,
) -> None:
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    malformed_calls = [
        '{"query":"未闭合",',
        '["not", "an", "object"]',
        '{"query":123,"max_results":"five","domain_filter":[],"recency":null}',
    ]
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="ownership",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": "002603.SZ"},
                        call_id="financial",
                    )
                ]
            ),
            *[
                ModelStep(
                    output=[
                        function_call(
                            SEARCH_WEB_TOOL,
                            arguments,
                            call_id=f"bad-search-{index}",
                        )
                    ]
                )
                for index, arguments in enumerate(malformed_calls, start=1)
            ],
            ModelStep(
                output=[
                    assistant_message(json.dumps(_draft_payload(), ensure_ascii=False))
                ]
            ),
        ]
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    comparison_source, comparison_download, comparison_summary, comparison_pages = _download(
        tmp_path, "S10"
    )
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=8,
            max_tool_calls=8,
            max_output_tokens=5000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[
            FinancialSourceInput(
                source=comparison_source,
                download=comparison_download,
                summary_path=comparison_summary,
                pages_path=comparison_pages,
            )
        ],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "malformed-search-bounded",
        trace_output=tmp_path / "malformed-search-bounded-trace.json",
        budget=SearchBudget(max_agent_turns=8, max_total_tool_calls=8),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    invalid_calls = [
        item
        for item in trace["tool_calls"]
        if item.get("error_code") == "invalid_tool_arguments"
    ]

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert provider.call_count == 0
    assert trace["tool_call_count"] == 5
    assert [item["attempt"] for item in invalid_calls] == [1, 2, 3]
    assert invalid_calls[-1]["status"] == "invalid_arguments_skipped"
    assert "search_web:invalid_tool_arguments_repeated" in result.limitations
    assert "V25_LLM_SENTINEL" in result.report_path.read_text(encoding="utf-8")


def test_invalid_model_output_preserves_diagnostics_without_python_analysis_fallback(
    tmp_path: Path,
) -> None:
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(
        fixture, clock=lambda: FIXED_TIME
    )
    source, download, summary_path, pages_path = _download(tmp_path, "S01")
    runtime = IntegratedCreditResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=5,
            max_tool_calls=5,
            max_output_tokens=1000,
        ),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=ScriptedModel(
            [ModelStep(output=[assistant_message("这不是有效的JSON报告")])]
        ),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        skill_runtime_factory=_ArtifactBackedSkillRuntime,
        clock=lambda: FIXED_TIME,
    )

    result = runtime.run_sync(
        task=FULL_TASK,
        identity=_identity(),
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        comparison_inputs=[],
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        output_dir=tmp_path / "invalid-output",
        trace_output=tmp_path / "invalid_trace.json",
        budget=SearchBudget(max_agent_turns=5, max_total_tool_calls=5),
    )
    report = result.report_path.read_text(encoding="utf-8")
    delivery = json.loads(result.delivery_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.FAILED
    assert "正式报告尚未完成" in report
    assert "### 重点财务数据" not in report
    assert delivery["llm_authored_report"] is False
    assert delivery["python_generated_fallback_analysis"] is False
