from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from pydantic.v1 import ValidationError

from credit_research_agent.company import CompanyResolutionError
from credit_research_agent.security_master import (
    DynamicCompanyResolver,
    OfficialAshareSecurityMasterProvider,
    SecurityMasterCache,
    SecurityMasterCacheStatus,
    SecurityMasterError,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)


FIXED_NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


COMPANIES = {
    "002603": ("SZSE", "SZ", "石家庄以岭药业股份有限公司", "以岭药业", "深市主板"),
    "600998": ("SSE", "SH", "九州通医药集团股份有限公司", "九州通", "沪市主板"),
    "601138": ("SSE", "SH", "富士康工业互联网股份有限公司", "工业富联", "沪市主板"),
    "300750": ("SZSE", "SZ", "宁德时代新能源科技股份有限公司", "宁德时代", "创业板"),
}


class FakeHttpResponse:
    def __init__(self, payload: str, *, url: str) -> None:
        self.content = payload.encode("utf-8")
        self.text = payload
        self.url = url
        self.status_code = 200


class FakeHttpSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def make_record(code: str, *, retrieved_at: datetime = FIXED_NOW) -> SecurityMasterRecord:
    exchange_code, suffix, legal_name, short_name, board = COMPANIES[code]
    exchange = "上海证券交易所" if exchange_code == "SSE" else "深圳证券交易所"
    return SecurityMasterRecord(
        security_code=code,
        ticker=f"{code}.{suffix}",
        entity_id=f"CN-A-{exchange_code}-{code}",
        short_name=short_name,
        legal_name=legal_name,
        exchange_code=exchange_code,
        exchange=exchange,
        board=board,
        external_org_id=f"ORG-{code}",
        source_urls=["https://www.cninfo.com.cn/new/snapshot/companyListCn"],
        source_publisher="官方证券主数据离线夹具",
        retrieved_at=retrieved_at,
    )


@pytest.mark.parametrize(
    ("code", "detail_payload", "detail_url", "expected_ticker", "expected_name"),
    [
        (
            "002603",
            'callback({"records":[{"SECCODE":"002603","SECNAME":"以岭药业",'
            '"ORGNAME":"石家庄以岭药业股份有限公司","F003N":"A"}]})',
            "https://www.cninfo.com.cn/data/yellowpages/getYellowpageStockList",
            "002603.SZ",
            "石家庄以岭药业股份有限公司",
        ),
        (
            "600998",
            'callback({"result":[{"COMPANY_CODE":"600998","COMPANY_ABBR":"九州通",'
            '"FULL_NAME":"九州通医药集团股份有限公司",'
            '"FULL_NAME_IN_ENGLISH":"Jointown Pharmaceutical Group Co., Ltd.",'
            '"LIST_DATE":"20101102","STATE_CODE":"2","DELIST_DATE":"-"}]})',
            "https://query.sse.com.cn/sseQuery/commonQuery.do",
            "600998.SH",
            "九州通医药集团股份有限公司",
        ),
    ],
)
def test_official_provider_normalizes_szse_and_sse_responses(
    code, detail_payload, detail_url, expected_ticker, expected_name
) -> None:
    _, _, _, short_name, _ = COMPANIES[code]
    index_payload = json.dumps(
        {
            "stockList": [
                {
                    "code": code,
                    "category": "A股",
                    "orgId": f"ORG-{code}",
                    "zwjc": short_name,
                }
            ]
        },
        ensure_ascii=False,
    )
    session = FakeHttpSession(
        [
            FakeHttpResponse(
                index_payload,
                url="https://www.cninfo.com.cn/new/data/szse_stock.json",
            ),
            FakeHttpResponse(detail_payload, url=detail_url),
        ]
    )
    provider = OfficialAshareSecurityMasterProvider(
        session=session,
        clock=lambda: FIXED_NOW,
    )

    record = provider.lookup(code)

    assert record.ticker == expected_ticker
    assert record.legal_name == expected_name
    assert record.external_org_id == f"ORG-{code}"
    assert len(session.calls) == 2
    if code == "600998":
        assert record.english_name == "Jointown Pharmaceutical Group Co., Ltd."


class FakeProvider:
    def __init__(self, records=None, error: SecurityMasterError | None = None) -> None:
        self.records = records or {code: make_record(code) for code in COMPANIES}
        self.error = error
        self.calls: list[str] = []

    def lookup(self, security_code: str) -> SecurityMasterRecord:
        self.calls.append(security_code)
        if self.error is not None:
            raise self.error
        try:
            return self.records[security_code]
        except KeyError as exc:
            raise SecurityMasterError("security_not_found", security_code) from exc


@pytest.mark.parametrize(
    ("query", "ticker", "legal_name", "entity_id", "board"),
    [
        (
            "002603.SZ",
            "002603.SZ",
            "石家庄以岭药业股份有限公司",
            "CN-A-SZSE-002603",
            "深市主板",
        ),
        (
            "600998.SH",
            "600998.SH",
            "九州通医药集团股份有限公司",
            "CN-A-SSE-600998",
            "沪市主板",
        ),
        (
            "601138",
            "601138.SH",
            "富士康工业互联网股份有限公司",
            "CN-A-SSE-601138",
            "沪市主板",
        ),
        (
            "300750.sz",
            "300750.SZ",
            "宁德时代新能源科技股份有限公司",
            "CN-A-SZSE-300750",
            "创业板",
        ),
    ],
)
def test_dynamic_resolver_supports_multiple_a_share_companies(
    tmp_path, query, ticker, legal_name, entity_id, board
) -> None:
    provider = FakeProvider()
    cache = SecurityMasterCache(
        tmp_path / "security_master.json", provider, clock=lambda: FIXED_NOW
    )
    resolver = DynamicCompanyResolver(cache)

    result = resolver.resolve_with_metadata(query)

    assert result.identity.ticker == ticker
    assert result.identity.legal_name == legal_name
    assert result.identity.entity_id == entity_id
    assert result.identity.listing_board == board
    assert result.identity.external_org_id == f"ORG-{ticker[:6]}"
    assert result.cache_status is SecurityMasterCacheStatus.REFRESHED
    assert provider.calls == [ticker[:6]]


def test_dynamic_resolver_rejects_exchange_suffix_mismatch(tmp_path) -> None:
    provider = FakeProvider()
    cache = SecurityMasterCache(
        tmp_path / "security_master.json", provider, clock=lambda: FIXED_NOW
    )
    resolver = DynamicCompanyResolver(cache)

    with pytest.raises(CompanyResolutionError, match="后缀不匹配"):
        resolver.resolve("600998.SZ")


def test_fresh_cache_avoids_second_provider_call(tmp_path) -> None:
    provider = FakeProvider()
    cache = SecurityMasterCache(
        tmp_path / "security_master.json", provider, clock=lambda: FIXED_NOW
    )

    first = cache.lookup("600998")
    second = cache.lookup("600998")

    assert first.cache_status is SecurityMasterCacheStatus.REFRESHED
    assert second.cache_status is SecurityMasterCacheStatus.FRESH
    assert provider.calls == ["600998"]


def test_stale_cache_is_explicit_when_refresh_fails(tmp_path) -> None:
    cache_path = tmp_path / "security_master.json"
    initial_provider = FakeProvider()
    initial_cache = SecurityMasterCache(
        cache_path, initial_provider, clock=lambda: FIXED_NOW
    )
    initial_cache.lookup("601138")

    failing_provider = FakeProvider(
        error=SecurityMasterError("official_source_network_error", "offline")
    )
    stale_cache = SecurityMasterCache(
        cache_path,
        failing_provider,
        max_age=timedelta(days=1),
        clock=lambda: FIXED_NOW + timedelta(days=8),
    )

    result = stale_cache.lookup("601138")

    assert result.cache_status is SecurityMasterCacheStatus.STALE
    assert result.record.ticker == "601138.SH"
    assert result.warnings == ["官方证券主数据刷新失败，本次显式使用过期缓存"]
    assert failing_provider.calls == ["601138"]


def test_corrupt_security_master_cache_is_rejected(tmp_path) -> None:
    cache_path = tmp_path / "security_master.json"
    cache_path.write_text('{"updated_at":', encoding="utf-8")
    cache = SecurityMasterCache(cache_path, FakeProvider(), clock=lambda: FIXED_NOW)

    with pytest.raises(SecurityMasterError) as captured:
        cache.lookup("600998")

    assert captured.value.code == "corrupt_security_master_cache"


def test_security_master_record_rejects_cross_exchange_identity() -> None:
    payload = make_record("600998").dict()
    payload["ticker"] = "600998.SZ"
    with pytest.raises(ValidationError, match="ticker"):
        SecurityMasterRecord.parse_obj(payload)


def test_refreshing_one_company_does_not_make_other_old_record_fresh(tmp_path) -> None:
    cache_path = tmp_path / "security_master.json"
    old_time = FIXED_NOW - timedelta(days=30)
    snapshot = SecurityMasterSnapshot(
        updated_at=old_time,
        records=[
            make_record("600998", retrieved_at=old_time),
            make_record("601138", retrieved_at=old_time),
        ],
    )
    cache_path.write_text(snapshot.json(ensure_ascii=False), encoding="utf-8")

    class SelectiveProvider(FakeProvider):
        def lookup(self, security_code: str) -> SecurityMasterRecord:
            self.calls.append(security_code)
            if security_code == "601138":
                raise SecurityMasterError("official_source_network_error", "offline")
            return make_record(security_code, retrieved_at=FIXED_NOW)

    provider = SelectiveProvider()
    cache = SecurityMasterCache(
        cache_path,
        provider,
        max_age=timedelta(days=7),
        clock=lambda: FIXED_NOW,
    )

    assert cache.lookup("600998").cache_status is SecurityMasterCacheStatus.REFRESHED
    second = cache.lookup("601138")

    assert second.cache_status is SecurityMasterCacheStatus.STALE
    assert provider.calls == ["600998", "601138"]
