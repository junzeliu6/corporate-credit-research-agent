"""V2.4 行业与政策研究运行时的纯离线测试。"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import SecretStr

from credit_research_agent.company import CompanyResolver
from credit_research_agent.industry_agent_runtime import (
    INDUSTRY_REQUIRED_TOPICS,
    IndustryResearchAgentRuntime,
    IndustryRuntimeError,
    build_industry_research_context,
    load_industry_search_fixture,
    project_industry_tool_output,
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = (
    "请研究以岭药业所属行业、竞争格局、政策环境，并结合财务表现分析授信影响。"
)
FINANCIAL_EXAMPLE_DIR = (
    PROJECT_ROOT
    / "examples"
    / "v2_3_financial_live_narrative_50000_timeout120"
    / "CN-A-SZSE-002603"
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
TECHNICAL_MARKER = re.compile(
    r"(?:\[(?:S|F|FM|E)\d+\]|\b(?:fact_id|metric_id|source_id)\b)", re.I
)


def _identity():
    return CompanyResolver.from_default_catalog().resolve("002603.SZ")


def _runtime(
    *,
    model: ScriptedModel | None = None,
    fixture: Dict[str, Any] | None = None,
    llm_config: DeepSeekLLMConfig | None = None,
    search_config: BaiduSearchConfig | None = None,
):
    selected_fixture = copy.deepcopy(fixture or load_industry_search_fixture())
    provider, fetcher = build_mock_retrieval_dependencies(selected_fixture)
    runtime = IndustryResearchAgentRuntime(
        PROJECT_ROOT,
        llm_config
        or DeepSeekLLMConfig(
            mode=AgentMode.MOCK, max_turns=20, max_tool_calls=60
        ),
        search_config or BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        trace_id_factory=lambda: "IR-TEST000001",
    )
    return runtime, provider, fetcher


def _run(runtime: IndustryResearchAgentRuntime, tmp_path: Path, **overrides):
    kwargs = {
        "task": TASK,
        "identity": _identity(),
        "output_dir": tmp_path / "industry",
        "trace_output": tmp_path / "industry_trace.json",
        "fundamentals_path": FUNDAMENTALS_EXAMPLE_PATH,
        "financial_dir": FINANCIAL_EXAMPLE_DIR,
        "budget": SearchBudget(max_agent_turns=20),
    }
    kwargs.update(overrides)
    return runtime.run_sync(**kwargs)


def _draft_payload(
    topics: Iterable[str] = INDUSTRY_REQUIRED_TOPICS,
    *,
    sentinel: str = "CUSTOM_LLM_SENTINEL：这是模型逐字生成的行业分析。",
) -> Dict[str, Any]:
    sections = [
        {
            "topic": topic,
            "heading": f"模型章节{index}",
            "analysis_markdown": (
                sentinel if index == 1 else f"模型原文第{index}节；相关因果关系需进一步核实。"
            ),
        }
        for index, topic in enumerate(topics, start=1)
    ]
    return {
        "report_title": "模型撰写的行业研究",
        "sections": sections,
        "due_diligence_questions": [
            {"question": "请核实行业政策对核心产品的实际适用范围。"}
        ],
        "limitations": [],
    }


def _final_only_model(payload: Dict[str, Any] | str) -> ScriptedModel:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return ScriptedModel([ModelStep(output=[assistant_message(text)])])


def test_default_mock_calls_search_fetch_extract_without_external_api_cost(
    tmp_path: Path,
) -> None:
    runtime, provider, fetcher = _runtime()
    result = _run(runtime, tmp_path)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    tool_names = [item["tool_name"] for item in trace["tool_calls"]]

    assert tool_names.count(SEARCH_WEB_TOOL) == 1
    assert tool_names.count(FETCH_SOURCE_TOOL) == 3
    assert tool_names.count(EXTRACT_EVIDENCE_TOOL) == 3
    assert provider.call_count == 1
    assert fetcher.call_count == 3
    assert result.search_api_requests == 0
    assert result.estimated_search_cost_cny == 0
    assert trace["search_api_requests"] == 0
    assert result.report_path.is_file()
    assert result.context_path.is_file()
    assert result.sources_path.is_file()


def test_industry_context_reuses_nine_real_v23_metrics_without_technical_ids() -> None:
    context = build_industry_research_context(
        task=TASK,
        identity=_identity(),
        fundamentals_path=FUNDAMENTALS_EXAMPLE_PATH,
        financial_dir=FINANCIAL_EXAMPLE_DIR,
    )
    financial = context["financial_context"]
    serialized = json.dumps(context, ensure_ascii=False)

    assert financial["available"] is True
    assert len(financial["snapshot"]["verified_financial_metrics"]) == 9
    assert {item["item"] for item in financial["snapshot"]["verified_financial_metrics"]} >= {
        "operating_revenue",
        "inventory",
        "operating_cash_flow",
    }
    net_profit = next(
        item
        for item in financial["snapshot"]["verified_financial_metrics"]
        if item["item"] == "net_profit"
    )
    assert net_profit["change_kind"] == "sign_change"
    assert net_profit["yoy_percent"] is None
    assert "跨符号" in net_profit["change_display"]
    assert "fact_id" not in serialized
    assert "metric_id" not in serialized
    assert "source_id" not in serialized
    assert {item["kind"] for item in financial["snapshot"]["disclosed_reasons"]} == {
        "disclosed_reason"
    }
    assert {
        item["kind"] for item in financial["snapshot"]["supported_inferences"]
    } == {"supported_inference"}
    assert all(
        item.get("verification_status") == "unverified"
        and item.get("required_evidence")
        for item in financial["snapshot"]["analysis_hypotheses"]
    )


def test_policy_without_company_name_is_verified_and_third_party_is_single_source(
    tmp_path: Path,
) -> None:
    runtime, _, _ = _runtime()
    result = _run(runtime, tmp_path)
    bundle = json.loads(result.sources_path.read_text(encoding="utf-8"))
    sources = {item["source_id"]: item for item in bundle["sources"]}
    evidence = {item["evidence_id"]: item for item in bundle["evidence"]}

    government_source_ids = {
        source_id
        for source_id, item in sources.items()
        if item["source_type"] == "government_website"
    }
    third_party_source_ids = {
        source_id
        for source_id, item in sources.items()
        if item["source_type"] == "web_page"
    }
    government_facts = [
        fact
        for fact in bundle["facts"]
        if fact["research_module"] == "policy_regulation"
        and any(
            evidence[evidence_id]["source_id"] in government_source_ids
            for evidence_id in fact["evidence_ids"]
        )
    ]
    third_party_facts = [
        fact
        for fact in bundle["facts"]
        if fact["research_module"] == "industry_research"
        and any(
            evidence[evidence_id]["source_id"] in third_party_source_ids
            for evidence_id in fact["evidence_ids"]
        )
    ]

    assert government_facts
    assert all(
        evidence[evidence_id]["status"] == "verified"
        for fact in government_facts
        for evidence_id in fact["evidence_ids"]
    )
    assert all("以岭药业" not in fact["claim"] for fact in government_facts)
    assert all(
        fact["subject"] != _identity().legal_name for fact in government_facts
    )
    assert all(fact["period"] is None for fact in government_facts)
    assert third_party_facts
    assert all(
        evidence[evidence_id]["status"] == "single_source"
        for fact in third_party_facts
        for evidence_id in fact["evidence_ids"]
    )
    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert any("单一第三方来源" in item for item in result.limitations)


def test_markdown_preserves_scripted_model_text_and_hides_technical_ids(
    tmp_path: Path,
) -> None:
    sentinel = "CUSTOM_LLM_SENTINEL：毛利率与竞争环境的关系仍需核实。"
    runtime, _, _ = _runtime(model=_final_only_model(_draft_payload(sentinel=sentinel)))
    result = _run(runtime, tmp_path)
    report = result.report_path.read_text(encoding="utf-8")
    delivery = json.loads(result.delivery_path.read_text(encoding="utf-8"))

    assert sentinel in report
    assert not TECHNICAL_MARKER.search(report)
    assert delivery["llm_authored_report"] is False
    assert delivery["authorship"] == "scripted_mock"
    assert delivery["model_path_exercised"] is True
    assert delivery["python_rewrote_llm_text"] is False


def test_industry_tool_projection_hides_tracking_ids_but_keeps_research_content() -> None:
    raw = json.dumps(
        {
            "request_id": "REQ001",
            "facts": [
                {
                    "fact_id": "F801",
                    "subject": "中医药行业政策",
                    "value": "政策原文内容",
                    "source_ids": ["S06"],
                    "verification_status": "verified",
                }
            ],
            "source_handle": "DOC001",
            "source_id": "S06",
            "next_step": "使用source_id以外的已取得资料继续分析",
        },
        ensure_ascii=False,
    )

    projected = project_industry_tool_output(raw)
    payload = json.loads(projected)

    assert payload["facts"] == [
        {
            "subject": "中医药行业政策",
            "value": "政策原文内容",
            "verification_status": "verified",
        }
    ]
    assert payload["source_handle"] == "DOC001"
    assert not TECHNICAL_MARKER.search(projected)


def test_model_technical_ids_are_kept_in_draft_but_not_delivered(
    tmp_path: Path,
) -> None:
    payload = _draft_payload(sentinel="模型错误引用了 [S99] 和 fact_id。")
    runtime, _, _ = _runtime(model=_final_only_model(payload))

    result = _run(runtime, tmp_path)
    report = result.report_path.read_text(encoding="utf-8")
    draft = result.draft_path.read_text(encoding="utf-8")

    assert result.status is RetrievalStatus.FAILED
    assert "本报告尚未完成" in report
    assert not TECHNICAL_MARKER.search(report)
    assert "[S99]" in draft
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["error_code"] == "industry_output_contains_technical_ids"


def test_one_403_source_degrades_to_partial_but_keeps_llm_report(
    tmp_path: Path,
) -> None:
    fixture = copy.deepcopy(load_industry_search_fixture())
    government_url = "https://www.gov.cn/zhengce/v2-4-policy-mock"
    fixture["pages"][government_url]["status"] = 403
    payload = _draft_payload(sentinel="PARTIAL_REPORT_SENTINEL")
    model = ScriptedModel(
        [
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
                        call_id="search",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": "https://www.yiling.cn/v2-4-industry-mock"},
                        call_id="fetch-good",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC001",
                            "research_question": "以岭药业主营业务主要产品",
                        },
                        call_id="extract-good",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": government_url},
                        call_id="fetch-403",
                    )
                ]
            ),
            ModelStep(output=[assistant_message(json.dumps(payload, ensure_ascii=False))]),
        ]
    )
    runtime, _, fetcher = _runtime(model=model, fixture=fixture)
    result = _run(runtime, tmp_path)
    report = result.report_path.read_text(encoding="utf-8")
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert "PARTIAL_REPORT_SENTINEL" in report
    assert fetcher.call_count == 2
    assert any("access_restricted" in reason for reason in trace["degraded_reasons"])
    assert any(
        item.get("status") == "failed_but_preserved_as_candidate"
        for item in trace["tool_calls"]
    )


def test_invalid_model_json_writes_incomplete_notice_without_python_analysis(
    tmp_path: Path,
) -> None:
    runtime, provider, fetcher = _runtime(model=_final_only_model("not-json"))
    result = _run(runtime, tmp_path)
    report = result.report_path.read_text(encoding="utf-8")
    delivery = json.loads(result.delivery_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.FAILED
    assert "本报告尚未完成" in report
    assert "DeepSeek 未生成可解析" in report
    assert "所属行业及发展趋势" not in report
    assert delivery["llm_authored_report"] is False
    assert delivery["python_generated_fallback_analysis"] is False
    assert provider.call_count == 0
    assert fetcher.call_count == 0

def test_missing_sections_are_partial_and_preserve_available_model_section(
    tmp_path: Path,
) -> None:
    only_topic = INDUSTRY_REQUIRED_TOPICS[0]
    payload = _draft_payload([only_topic], sentinel="ONLY_SECTION_SENTINEL")
    runtime, _, _ = _runtime(model=_final_only_model(payload))
    result = _run(runtime, tmp_path)
    report = result.report_path.read_text(encoding="utf-8")

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.delivered_topics == [only_topic]
    assert result.missing_topics == list(INDUSTRY_REQUIRED_TOPICS[1:])
    assert "ONLY_SECTION_SENTINEL" in report
    assert "模型未完成章节" in report


def test_non_budget_tool_error_still_writes_incomplete_artifacts(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": "https://not-searched.example/article"},
                        call_id="invalid-fetch",
                    )
                ]
            )
        ]
    )
    runtime, _, _ = _runtime(model=model)
    result = _run(runtime, tmp_path)

    assert result.status is RetrievalStatus.FAILED
    assert result.report_path.is_file()
    assert result.sources_path.is_file()
    assert result.delivery_path.is_file()
    assert "尚未完成" in result.report_path.read_text(encoding="utf-8")
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["error_code"] == "source_not_from_search"


def test_missing_mock_dependencies_still_write_preflight_artifacts(
    tmp_path: Path,
) -> None:
    runtime = IndustryResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(mode=AgentMode.MOCK, max_turns=4, max_tool_calls=1),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=_final_only_model(_draft_payload()),
        trace_id_factory=lambda: "IR-PREFLIGHT0001",
    )

    result = _run(runtime, tmp_path)

    assert result.status is RetrievalStatus.FAILED
    assert result.report_path.is_file()
    assert result.sources_path.is_file()
    assert result.trace_path.is_file()
    assert result.delivery_path.is_file()
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["error_code"] == "industry_runtime_setup_failed"


def test_llm_config_caps_explicit_retrieval_budget(tmp_path: Path) -> None:
    runtime, _, _ = _runtime(
        llm_config=DeepSeekLLMConfig(
            mode=AgentMode.MOCK,
            max_turns=5,
            max_tool_calls=2,
        )
    )
    result = _run(
        runtime,
        tmp_path,
        budget=SearchBudget(max_agent_turns=20, max_total_tool_calls=60),
    )

    assert result.budget_limits["max_agent_turns"] == 5
    assert result.budget_limits["max_total_tool_calls"] == 2
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert trace["tool_call_count"] <= 2


def test_live_llm_and_search_require_separate_confirmation_before_any_call(
    tmp_path: Path,
) -> None:
    fixture = load_industry_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(fixture)
    runtime = IndustryResearchAgentRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(
            mode=AgentMode.LIVE,
            api_key=SecretStr("deepseek-test-key"),
        ),
        BaiduSearchConfig(
            mode=SearchMode.LIVE,
            api_key=SecretStr("baidu-test-key"),
        ),
        model_override=_final_only_model(_draft_payload()),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
    )
    common = {
        "task": TASK,
        "identity": _identity(),
        "output_dir": tmp_path / "live-boundary",
        "trace_output": tmp_path / "live-boundary-trace.json",
    }

    with pytest.raises(IndustryRuntimeError) as llm_error:
        runtime.run_sync(**common)
    assert llm_error.value.code == "live_llm_confirmation_required"

    with pytest.raises(IndustryRuntimeError) as search_error:
        runtime.run_sync(**common, confirmed_llm_live=True)
    assert search_error.value.code == "live_search_confirmation_required"
    assert provider.call_count == 0
    assert fetcher.call_count == 0
