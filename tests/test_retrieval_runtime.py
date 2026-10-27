from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from credit_research_agent.company import CompanyResolver
from credit_research_agent.llm_config import AgentMode, DeepSeekLLMConfig
from credit_research_agent.retrieval_runtime import (
    EXTRACT_EVIDENCE_TOOL,
    FETCH_SOURCE_TOOL,
    SEARCH_WEB_TOOL,
    InformationRetrievalRuntime,
    RetrievalRuntimeError,
    RetrievalStatus,
    build_mock_retrieval_dependencies,
    load_search_fixture,
)
from credit_research_agent.search import BaiduSearchConfig, SearchBudget, SearchMode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = "请搜索以岭药业官方网站，补充主营业务、主要产品和经营模式，并提供来源。"
FIXTURE_URL = "https://www.yiling.cn/v1-5-offline-mock"


def _identity():
    return CompanyResolver.from_default_catalog().resolve("002603.SZ")


def _runtime(model=None, fixture=None, llm_config=None):
    fixture = fixture or load_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(fixture)
    return InformationRetrievalRuntime(
        PROJECT_ROOT,
        llm_config or DeepSeekLLMConfig(mode=AgentMode.MOCK, max_turns=4),
        BaiduSearchConfig(mode=SearchMode.MOCK),
        model_override=model,
        search_provider_override=provider,
        source_fetcher_override=fetcher,
        trace_id_factory=lambda: "WR-TEST000001",
    ), provider, fetcher


def _run(runtime, tmp_path: Path, **overrides):
    kwargs = {
        "task": TASK,
        "identity": _identity(),
        "output_dir": tmp_path / "run",
        "trace_output": tmp_path / "trace.json",
        "budget": SearchBudget(),
    }
    kwargs.update(overrides)
    return runtime.run_sync(**kwargs)


def test_mock_agent_autonomously_calls_search_fetch_extract_and_has_no_api_cost(tmp_path: Path) -> None:
    runtime, provider, fetcher = _runtime()
    result = _run(runtime, tmp_path)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert [item["tool_name"] for item in trace["tool_calls"]] == [
        SEARCH_WEB_TOOL,
        FETCH_SOURCE_TOOL,
        EXTRACT_EVIDENCE_TOOL,
    ]
    assert trace["bound_source_ids"] == ["S05"]
    assert trace["returned_fact_ids"] == ["F801", "F802"]
    assert trace["search_api_requests"] == 0
    assert trace["estimated_search_cost_cny"] == 0
    assert trace["budget_limits"] == {
        "max_search_rounds": 15,
        "max_search_calls": 15,
        "max_search_requests": 15,
        "max_results_per_request": 5,
        "max_fetches": 20,
        "max_extractions": 20,
        "max_total_tool_calls": 60,
        "max_agent_turns": 70,
    }
    assert result.budget_limits == trace["budget_limits"]
    assert provider.call_count == 1
    assert fetcher.call_count == 1
    assert "【离线 Mock 结果】" in result.final_output
    assert "[S05]" in result.final_output


def test_mock_mode_does_not_need_either_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    runtime, _, _ = _runtime()
    result = _run(runtime, tmp_path)
    assert result.llm_config["api_key_configured"] is False
    assert result.search_config["api_key_configured"] is False


def test_empty_search_result_returns_explicit_no_verified_fact(tmp_path: Path) -> None:
    runtime, _, _ = _runtime(fixture={"results": [], "pages": {}})
    result = _run(runtime, tmp_path)
    assert result.status is RetrievalStatus.FAILED
    assert result.selected_fact_ids == []
    assert "未形成可作为确定性结论的事实" in result.final_output
    assert result.report_path.is_file()
    assert "资料不足" in result.report_path.read_text(encoding="utf-8")


def test_model_cannot_fetch_url_not_returned_by_search(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        FETCH_SOURCE_TOOL,
                        {"url": "https://attacker.example/not-searched"},
                        call_id="bad-fetch",
                    )
                ]
            )
        ]
    )
    runtime, _, _ = _runtime(model=model)
    with pytest.raises(RetrievalRuntimeError) as exc_info:
        _run(runtime, tmp_path)
    assert exc_info.value.code == "source_not_from_search"


def test_model_cannot_override_source_id_in_extract_tool(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {
                            "source_handle": "DOC001",
                            "research_question": "主营业务",
                            "source_id": "S99",
                        },
                        call_id="bad-source-override",
                    )
                ]
            )
        ]
    )
    runtime, _, _ = _runtime(model=model)
    with pytest.raises(RetrievalRuntimeError) as exc_info:
        _run(runtime, tmp_path)
    assert exc_info.value.code == "source_id_override_forbidden"


def test_unknown_fact_id_is_rejected(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[assistant_message('{"fact_ids":["F999"],"candidate_ids":[],"analysis_claims":[]}')]
            )
        ]
    )
    runtime, _, _ = _runtime(model=model)
    with pytest.raises(RetrievalRuntimeError) as exc_info:
        _run(runtime, tmp_path)
    assert exc_info.value.code == "unknown_evidence_reference"


def test_analysis_cannot_invent_percentage_even_with_valid_fact(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 主营业务",
                            "max_results": 5,
                            "domain_filter": ["yiling.cn"],
                            "recency": None,
                        },
                        call_id="search",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(FETCH_SOURCE_TOOL, {"url": FIXTURE_URL}, call_id="fetch")
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "主营业务"},
                        call_id="extract",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": ["F801"],
                                "candidate_ids": [],
                                "analysis_claims": [
                                    {
                                        "claim_id": "WA001",
                                        "statement": "该业务占比达到5%",
                                        "supporting_fact_ids": ["F801"],
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
    runtime, _, _ = _runtime(model=model)
    result = _run(runtime, tmp_path)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.selected_fact_ids == ["F801"]
    assert "5%" not in result.final_output
    assert any(
        "model_output_rejected:analysis_contains_unrendered_data" in item
        for item in trace["events"][-1]["details"]["degraded_reasons"]
    )


def test_max_turns_stops_repeated_search_loop(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "第一轮",
                            "max_results": 1,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-1",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "第二轮",
                            "max_results": 1,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-2",
                    )
                ]
            ),
        ]
    )
    runtime, _, _ = _runtime(model=model)
    result = _run(runtime, tmp_path, budget=SearchBudget(max_agent_turns=2))
    assert result.status is RetrievalStatus.FAILED
    assert result.report_path.is_file()
    assert "资料不足" in result.report_path.read_text(encoding="utf-8")


def test_total_tool_budget_degrades_to_partial_report_with_existing_fact(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业",
                            "max_results": 1,
                            "domain_filter": ["yiling.cn"],
                            "recency": None,
                        },
                        call_id="search",
                    )
                ]
            ),
            ModelStep(
                output=[function_call(FETCH_SOURCE_TOOL, {"url": FIXTURE_URL}, call_id="fetch-1")]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "主营业务"},
                        call_id="extract-1",
                    )
                ]
            ),
            ModelStep(
                output=[function_call(FETCH_SOURCE_TOOL, {"url": FIXTURE_URL}, call_id="fetch-2")]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": ["F801"],
                                "candidate_ids": [],
                                "analysis_claims": [],
                            }
                        )
                    )
                ]
            ),
        ]
    )
    runtime, _, _ = _runtime(model=model)
    result = _run(
        runtime,
        tmp_path,
        budget=SearchBudget(max_total_tool_calls=3),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.selected_fact_ids == ["F801"]
    assert any(
        item.get("error_code") == "total_tool_budget_exceeded"
        for item in trace["tool_calls"]
    )
    assert result.report_path.is_file()


def test_one_failed_page_yields_partial_success_and_keeps_other_evidence(
    tmp_path: Path,
) -> None:
    blocked_url = "https://blocked.example/article"
    official_url = "https://www.yiling.cn/available"
    fixture = {
        "results": [
            {
                "title": "无法读取的第三方页面",
                "url": blocked_url,
                "snippet": "某来源称存在产品线索，但原文尚未核对。",
                "published_at": "2026-10-01T00:00:00+08:00",
            },
            {
                "title": "可读取的公司官网页面",
                "url": official_url,
                "snippet": "官网页面搜索摘要。",
                "published_at": "2026-10-02T00:00:00+08:00",
            },
        ],
        "pages": {
            blocked_url: {"status": 403, "body": ""},
            official_url: {
                "status": 200,
                "body": (
                    "<html><head><title>以岭药业官网测试页</title></head>"
                    "<body><p>石家庄以岭药业股份有限公司主营业务官网原文。</p></body></html>"
                ),
            },
        },
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 主营业务",
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
                    function_call(FETCH_SOURCE_TOOL, {"url": blocked_url}, call_id="fetch-bad")
                ]
            ),
            ModelStep(
                output=[
                    function_call(FETCH_SOURCE_TOOL, {"url": official_url}, call_id="fetch-good")
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "主营业务"},
                        call_id="extract",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": ["F801"],
                                "candidate_ids": ["CAND001"],
                                "analysis_claims": [],
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
            ),
        ]
    )
    runtime, _, fetcher = _runtime(
        model=model,
        fixture=fixture,
        llm_config=DeepSeekLLMConfig(mode=AgentMode.MOCK, max_turns=5),
    )

    result = _run(runtime, tmp_path)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert result.selected_fact_ids == ["F801"]
    assert result.selected_candidate_ids == ["CAND001"]
    assert fetcher.call_count == 2
    assert "【已核验】" in result.final_output
    assert "【未核验】某来源称" in result.final_output
    assert any(item["stage"] == "source_fetch_failed" for item in trace["events"])
    failed_call = next(
        item
        for item in trace["tool_calls"]
        if item.get("status") == "failed_but_preserved_as_candidate"
    )
    assert failed_call["error_code"] == "access_restricted"
    assert failed_call["recommended_alternative_urls"] == [official_url]
    assert trace["status"] == "partial_success"


def test_pending_official_candidate_is_fetched_before_another_paid_search(
    tmp_path: Path,
) -> None:
    nhsa_url = "https://www.nhsa.gov.cn/policy/available"
    nmpa_url = "https://www.nmpa.gov.cn/policy/also-available"
    fixture = {
        "results": [
            {
                "title": "医保部门政策原文",
                "url": nhsa_url,
                "snippet": "医保政策搜索摘要。",
                "published_at": "2026-10-01T00:00:00+08:00",
            },
            {
                "title": "药监部门政策原文",
                "url": nmpa_url,
                "snippet": "药品监管搜索摘要。",
                "published_at": "2026-10-02T00:00:00+08:00",
            },
        ],
        "pages": {
            nhsa_url: {
                "status": 200,
                "body": (
                    "<html><head><title>医保政策原文</title></head><body>"
                    "<p>中成药集中带量采购政策适用范围和实施要求测试原文。</p>"
                    "</body></html>"
                ),
            },
            nmpa_url: {"status": 200, "body": "<html><body>药品监管原文</body></html>"},
        },
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "中成药医保政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-1",
                    )
                ]
            ),
            # Runtime应阻止这次新增付费搜索，并把已有NHSA候选重新交给模型。
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "中药监管政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-2",
                    )
                ]
            ),
            ModelStep(
                output=[function_call(FETCH_SOURCE_TOOL, {"url": nhsa_url}, call_id="fetch")]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "医保集采政策"},
                        call_id="extract",
                    )
                ]
            ),
            ModelStep(
                output=[assistant_message('{"fact_ids":[],"candidate_ids":[],"analysis_claims":[]}')]
            ),
        ]
    )
    runtime, provider, fetcher = _runtime(
        model=model,
        fixture=fixture,
        llm_config=DeepSeekLLMConfig(mode=AgentMode.MOCK, max_turns=5),
    )

    result = _run(runtime, tmp_path)
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))
    blocked_search = next(
        item
        for item in trace["tool_calls"]
        if item.get("status") == "candidate_fetch_required"
    )

    assert provider.call_count == 1
    assert fetcher.call_count == 1
    assert blocked_search["recommended_candidate_urls"][0] == nhsa_url
    assert trace["fetched_source_handles"] == ["DOC001"]
    assert result.selected_fact_ids


def test_search_budget_exhaustion_still_allows_fetch_and_extract_of_pending_candidate(
    tmp_path: Path,
) -> None:
    first_url = "https://www.nhsa.gov.cn/policy/first"
    second_url = "https://www.nmpa.gov.cn/policy/second"
    fixture = {
        "results": [
            {
                "title": "医保政策一",
                "url": first_url,
                "snippet": "医保政策一摘要。",
            },
            {
                "title": "药监政策二",
                "url": second_url,
                "snippet": "药监政策二摘要。",
            },
        ],
        "pages": {
            first_url: {
                "status": 200,
                "body": "<html><body><p>医保政策适用范围测试原文。</p></body></html>",
            },
            second_url: {
                "status": 200,
                "body": "<html><body><p>药品质量监管实施要求测试原文。</p></body></html>",
            },
        },
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "医药政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-1",
                    )
                ]
            ),
            ModelStep(output=[function_call(FETCH_SOURCE_TOOL, {"url": first_url}, call_id="fetch-1")]),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "医保政策"},
                        call_id="extract-1",
                    )
                ]
            ),
            # 局部search预算在此耗尽，但不能禁止继续处理已有第二候选。
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "补充药品监管政策",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search-2",
                    )
                ]
            ),
            ModelStep(output=[function_call(FETCH_SOURCE_TOOL, {"url": second_url}, call_id="fetch-2")]),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC002", "research_question": "药品质量监管"},
                        call_id="extract-2",
                    )
                ]
            ),
            ModelStep(
                output=[assistant_message('{"fact_ids":[],"candidate_ids":[],"analysis_claims":[]}')]
            ),
        ]
    )
    runtime, provider, fetcher = _runtime(
        model=model,
        fixture=fixture,
        llm_config=DeepSeekLLMConfig(mode=AgentMode.MOCK, max_turns=7),
    )

    result = _run(
        runtime,
        tmp_path,
        budget=SearchBudget(max_search_calls=1, max_agent_turns=7),
    )
    trace = json.loads(result.trace_path.read_text(encoding="utf-8"))

    assert provider.call_count == 1
    assert fetcher.call_count == 2
    assert any(
        item.get("error_code") == "search_call_limit_exceeded"
        for item in trace["tool_calls"]
    )
    assert trace["fetched_source_handles"] == ["DOC001", "DOC002"]
    assert result.selected_fact_ids
    assert result.status is RetrievalStatus.PARTIAL_SUCCESS


def test_single_source_fact_and_analysis_keep_qualification(tmp_path: Path) -> None:
    url = "https://thirdparty.example/article"
    fixture = {
        "results": [
            {
                "title": "第三方业务报道",
                "url": url,
                "snippet": "第三方搜索摘要。",
                "published_at": "2026-10-01T00:00:00+08:00",
            }
        ],
        "pages": {
            url: {
                "status": 200,
                "body": (
                    "<html><head><title>以岭药业第三方报道</title></head>"
                    "<body><p>石家庄以岭药业股份有限公司主营业务第三方原文。</p></body></html>"
                ),
            }
        },
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        SEARCH_WEB_TOOL,
                        {
                            "query": "以岭药业 主营业务",
                            "max_results": 5,
                            "domain_filter": [],
                            "recency": None,
                        },
                        call_id="search",
                    )
                ]
            ),
            ModelStep(
                output=[function_call(FETCH_SOURCE_TOOL, {"url": url}, call_id="fetch")]
            ),
            ModelStep(
                output=[
                    function_call(
                        EXTRACT_EVIDENCE_TOOL,
                        {"source_handle": "DOC001", "research_question": "主营业务"},
                        call_id="extract",
                    )
                ]
            ),
            ModelStep(
                output=[
                    assistant_message(
                        json.dumps(
                            {
                                "fact_ids": ["F801"],
                                "candidate_ids": [],
                                "analysis_claims": [
                                    {
                                        "claim_id": "WA001",
                                        "statement": "该信息仍需进一步交叉核实",
                                        "supporting_fact_ids": ["F801"],
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
    runtime, _, _ = _runtime(model=model, fixture=fixture)

    result = _run(runtime, tmp_path)

    assert result.status is RetrievalStatus.PARTIAL_SUCCESS
    assert "【单一来源，待交叉核实】" in result.final_output
    assert "【基于单一来源的初步分析】" in result.final_output


def test_live_modes_require_separate_confirmations_before_network(tmp_path: Path) -> None:
    fixture = load_search_fixture()
    provider, fetcher = build_mock_retrieval_dependencies(fixture)
    runtime = InformationRetrievalRuntime(
        PROJECT_ROOT,
        DeepSeekLLMConfig(mode=AgentMode.MOCK),
        BaiduSearchConfig(
            mode=SearchMode.LIVE,
            api_key="test-baidu-key-not-real",
        ),
        search_provider_override=provider,
        source_fetcher_override=fetcher,
    )
    with pytest.raises(RetrievalRuntimeError) as exc_info:
        _run(runtime, tmp_path)
    assert exc_info.value.code == "live_search_confirmation_required"
    assert provider.call_count == 0
