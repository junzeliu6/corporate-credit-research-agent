from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from credit_research_agent.search import (
    BAIDU_SEARCH_BASE_URL,
    BAIDU_SEARCH_CALL_PRICE_CNY,
    BaiduAuthHeader,
    BaiduSearchConfig,
    BaiduSearchProvider,
    FixtureSearchProvider,
    SearchBudget,
    SearchCache,
    SearchError,
    SearchMode,
    SearchQuery,
    SearchRecency,
    SearchService,
    load_baidu_search_config,
)


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.closed = False

    def json(self):
        return self._payload

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _live_config(**overrides) -> BaiduSearchConfig:
    payload = {
        "mode": SearchMode.LIVE,
        "api_key": "test-baidu-key-not-real",
    }
    payload.update(overrides)
    return BaiduSearchConfig(**payload)


def test_mock_config_does_not_require_or_read_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BAIDU_SEARCH_API_KEY", "must-not-enter-mock-config")
    config = load_baidu_search_config(tmp_path, mode="mock")
    assert config.api_key is None
    assert config.sanitized_summary()["api_key_configured"] is False


def test_live_config_reads_env_file_without_exposing_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SEARCH_PROVIDER=baidu\nBAIDU_SEARCH_API_KEY=test-key-placeholder\n",
        encoding="utf-8",
    )
    config = load_baidu_search_config(tmp_path, mode="live", env_file=env_file)
    assert config.api_key is not None
    assert "test-key-placeholder" not in repr(config)
    assert "test-key-placeholder" not in str(config.sanitized_summary())


def test_live_config_rejects_missing_key_and_other_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    with pytest.raises(SearchError, match="BAIDU_SEARCH_API_KEY"):
        load_baidu_search_config(tmp_path, mode="live")
    monkeypatch.setenv("SEARCH_PROVIDER", "other")
    with pytest.raises(SearchError) as exc_info:
        load_baidu_search_config(tmp_path, mode="mock")
    assert exc_info.value.code == "unsupported_search_provider"


def test_baidu_request_maps_official_parameters_and_one_auth_header() -> None:
    provider = BaiduSearchProvider(
        _live_config(), session=FakeSession(), clock=lambda: NOW
    )
    query = SearchQuery(
        query="以岭药业 主营业务",
        max_results=5,
        domain_filter=["yiling.cn", "gov.cn"],
        recency=SearchRecency.YEAR,
    )
    headers, payload = provider.build_request(query)
    assert headers["Authorization"].startswith("Bearer ")
    assert "X-Appbuilder-Authorization" not in headers
    assert payload == {
        "messages": [{"role": "user", "content": "以岭药业 主营业务"}],
        "edition": "standard",
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": 5}],
        "search_filter": {"match": {"site": ["yiling.cn", "gov.cn"]}},
        "search_recency_filter": "year",
    }


def test_baidu_auth_header_override_does_not_send_both_headers() -> None:
    provider = BaiduSearchProvider(
        _live_config(auth_header=BaiduAuthHeader.X_APPBUILDER_AUTHORIZATION),
        session=FakeSession(),
    )
    headers, _ = provider.build_request(SearchQuery(query="测试"))
    assert set(headers) == {"Content-Type", "X-Appbuilder-Authorization"}


def test_baidu_response_converts_references_and_deduplicates_urls() -> None:
    response = FakeResponse(
        payload={
            "request_id": "REQ-1",
            "references": [
                {
                    "id": 1,
                    "type": "web",
                    "title": "公司官网",
                    "url": "https://www.yiling.cn/about#intro",
                    "content": "候选摘要",
                    "date": "2026-10-01 08:00:00",
                    "authority_score": 0.9,
                    "rerank_score": 0.8,
                },
                {
                    "id": 2,
                    "type": "web",
                    "title": "重复",
                    "url": "https://www.yiling.cn/about",
                    "content": "重复摘要",
                },
                {"id": 3, "type": "image", "url": "https://img.example/x.jpg"},
            ],
        }
    )
    session = FakeSession(response=response)
    provider = BaiduSearchProvider(_live_config(), session=session, clock=lambda: NOW)
    result = provider.search(SearchQuery(query="以岭药业", max_results=5))
    assert result.request_id == "REQ-1"
    assert len(result.results) == 1
    assert result.results[0].url == "https://www.yiling.cn/about"
    assert result.results[0].is_search_snippet is True
    assert response.closed is True


def test_empty_search_results_are_valid() -> None:
    provider = BaiduSearchProvider(
        _live_config(),
        session=FakeSession(FakeResponse(payload={"references": []})),
        clock=lambda: NOW,
    )
    result = provider.search(SearchQuery(query="不存在的查询"))
    assert result.results == []


@pytest.mark.parametrize(
    ("response", "error", "expected_code"),
    [
        (FakeResponse(status_code=401), None, "search_authentication_failed"),
        (FakeResponse(status_code=429), None, "search_rate_limited"),
        (None, requests.Timeout("timeout"), "search_timeout"),
    ],
)
def test_baidu_errors_are_mapped(response, error, expected_code) -> None:
    provider = BaiduSearchProvider(
        _live_config(), session=FakeSession(response=response, error=error)
    )
    with pytest.raises(SearchError) as exc_info:
        provider.search(SearchQuery(query="测试"))
    assert exc_info.value.code == expected_code


def test_search_cache_prevents_repeated_provider_call(tmp_path: Path) -> None:
    provider = FixtureSearchProvider(
        [{"title": "官网", "url": "https://www.yiling.cn/about", "snippet": "摘要"}],
        clock=lambda: NOW,
    )
    budget = SearchBudget()
    service = SearchService(
        provider,
        SearchCache(tmp_path),
        budget,
        external_requests=False,
    )
    query = SearchQuery(query="以岭药业")
    first = service.search(query)
    second = service.search(query)
    assert first.from_cache is False
    assert second.from_cache is True
    assert provider.call_count == 1
    assert budget.search_api_requests == 0
    assert budget.estimated_search_cost_cny == 0


def test_search_budget_enforces_requests_results_and_supplemental_rounds(tmp_path: Path) -> None:
    provider = FixtureSearchProvider([], clock=lambda: NOW)
    budget = SearchBudget(
        max_search_requests=1,
        max_results_per_request=2,
        max_supplemental_rounds=0,
    )
    service = SearchService(provider, SearchCache(tmp_path), budget, external_requests=True)
    with pytest.raises(SearchError) as exc_info:
        service.search(SearchQuery(query="测试", max_results=3))
    assert exc_info.value.code == "search_result_limit_exceeded"
    service.search(SearchQuery(query="第一条", max_results=2))
    with pytest.raises(SearchError) as exc_info:
        service.search(SearchQuery(query="第二条", max_results=2))
    assert exc_info.value.code in {
        "supplemental_search_limit_exceeded",
        "search_budget_exceeded",
    }


def test_v15_default_budget_allows_fifteenth_round_and_blocks_sixteenth() -> None:
    budget = SearchBudget()

    for round_id in range(1, 16):
        budget.record_search_round(round_id)
    assert budget.search_rounds == 15
    with pytest.raises(SearchError) as exc_info:
        budget.record_search_round(16)
    assert exc_info.value.code == "search_round_limit_exceeded"


def test_v15_default_budget_allows_fifteenth_search_and_blocks_sixteenth() -> None:
    budget = SearchBudget()

    for index in range(15):
        query = SearchQuery(query=f"检索主题-{index}")
        budget.check_query(query, cache_hit=False, external_request=True)
        budget.record_search(cache_hit=False, external_request=True)
    assert budget.search_tool_calls == 15
    assert budget.search_api_requests == 15

    with pytest.raises(SearchError) as exc_info:
        budget.check_query(
            SearchQuery(query="第16次检索"),
            cache_hit=False,
            external_request=True,
        )
    assert exc_info.value.code == "search_call_limit_exceeded"


def test_all_tool_types_share_hard_total_budget() -> None:
    budget = SearchBudget()

    for index in range(60):
        budget.begin_tool_call(f"tool-{index}")
    assert budget.total_tool_calls == 60
    with pytest.raises(SearchError) as exc_info:
        budget.begin_tool_call("tool-61")
    assert exc_info.value.code == "total_tool_budget_exceeded"


def test_v15_default_individual_tool_budgets() -> None:
    budget = SearchBudget()

    assert budget.max_search_calls == 15
    assert budget.max_search_requests == 15
    assert budget.max_fetches == 20
    assert budget.max_extractions == 20
    assert budget.max_total_tool_calls == 60
    assert budget.max_agent_turns == 70

    for _ in range(20):
        budget.record_fetch(cache_hit=False)
        budget.record_extraction()
    with pytest.raises(SearchError, match="最多读取"):
        budget.record_fetch(cache_hit=False)
    with pytest.raises(SearchError, match="证据提取"):
        budget.record_extraction()


def test_failed_external_search_still_consumes_local_request_budget(tmp_path: Path) -> None:
    class FailingProvider:
        def search(self, query):
            raise SearchError("search_timeout", "模拟超时", retryable=True)

    budget = SearchBudget(max_search_requests=1)
    service = SearchService(
        FailingProvider(),
        SearchCache(tmp_path),
        budget,
        external_requests=True,
    )
    with pytest.raises(SearchError, match="模拟超时"):
        service.search(SearchQuery(query="第一次失败"))
    assert budget.search_api_requests == 1
    assert budget.estimated_search_cost_cny == BAIDU_SEARCH_CALL_PRICE_CNY
    with pytest.raises(SearchError) as exc_info:
        service.search(SearchQuery(query="不得发出第二次请求"))
    assert exc_info.value.code == "search_budget_exceeded"


def test_default_base_url_matches_official_endpoint() -> None:
    assert BAIDU_SEARCH_BASE_URL == "https://qianfan.baidubce.com/v2/ai_search/web_search"
