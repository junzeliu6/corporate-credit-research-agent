"""V3.3B 本机九州通真实年报集成测试；缓存不存在时跳过。"""

from decimal import Decimal
from pathlib import Path

import pytest

from credit_research_agent.annual_reports import CompanyOnboardingResult
from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialItem, FinancialSourceInput, FinancialStatement
from credit_research_agent.financial_statements import (
    FinancialStatementExtractor,
    StatementExtractionStatus,
)
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONBOARDING_PATH = (
    PROJECT_ROOT
    / "data"
    / "onboarding"
    / "CN-A-SSE-600998"
    / "onboarding.json"
)


def test_jiuzhoutong_real_annual_report_extracts_three_statements() -> None:
    if not ONBOARDING_PATH.is_file():
        pytest.skip("本机尚未准备九州通V3.3A年报产物")
    onboarding = CompanyOnboardingResult.parse_raw(
        ONBOARDING_PATH.read_text(encoding="utf-8")
    )
    if not onboarding.prepared_reports:
        pytest.skip("九州通onboarding未包含已解析年报")
    inputs = [
        FinancialSourceInput(
            source=item.source,
            download=item.download,
            summary_path=item.extraction.summary_path,
            pages_path=item.extraction.pages_path,
        )
        for item in onboarding.prepared_reports
        if item.source.report_period == "FY2025"
    ]
    if not inputs:
        pytest.skip("九州通FY2025年报解析产物不存在")

    result = FinancialStatementExtractor().extract(
        identity=onboarding.resolution.identity,
        source_inputs=inputs,
    )

    assert result.status is StatementExtractionStatus.SUCCESS
    assert set(result.statements_found) == set(FinancialStatement)
    assert result.missing_statements == []
    assert {
        section.currency_basis for section in result.sections
    } == {"statement_header_explicit"}
    lookup = {(fact.item, fact.period): fact for fact in result.financial_facts}
    assert lookup[(FinancialItem.OPERATING_REVENUE, "FY2025")].value == Decimal(
        "161389993006.56"
    )
    assert lookup[(FinancialItem.NET_PROFIT, "FY2025")].value == Decimal(
        "2409584803.58"
    )
    assert lookup[(FinancialItem.OPERATING_CASH_FLOW, "FY2025")].value == Decimal(
        "3440654842.71"
    )
    assert {item.source_id for item in result.evidence} == {"S01"}
    assert all(item.page_number >= 152 for item in result.evidence)
    assert not [
        item for item in result.issues if item.code == "ambiguous_amount_columns"
    ]
    assert not [item for item in result.issues if item.severity.value == "error"]


def test_yiling_real_annual_report_supports_different_statement_layout() -> None:
    required = [
        PROJECT_ROOT / "data" / "cache" / "S01.metadata.json",
        PROJECT_ROOT / "data" / "parsed" / "S01" / "summary.json",
        PROJECT_ROOT / "data" / "parsed" / "S01" / "pages.jsonl",
    ]
    if any(not path.is_file() for path in required):
        pytest.skip("本机尚未准备以岭药业S01年报产物")
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    source_input = FinancialSourceInput(
        source=source,
        download=DownloadResult.parse_raw(required[0].read_text(encoding="utf-8")),
        summary_path=required[1],
        pages_path=required[2],
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    # 以岭主表中的短期借款两期均为空，首版正确降级为部分成功且不补零。
    assert result.status is StatementExtractionStatus.PARTIAL_SUCCESS
    assert set(result.statements_found) == set(FinancialStatement)
    assert {
        section.currency_basis for section in result.sections
    } == {"annual_report_accounting_policy"}
    lookup = {(fact.item, fact.period): fact for fact in result.financial_facts}
    assert lookup[(FinancialItem.OPERATING_REVENUE, "FY2025")].value == Decimal(
        "7830737438.74"
    )
    assert not {
        key for key in lookup if key[0] is FinancialItem.SHORT_TERM_BORROWINGS
    }
    assert not [item for item in result.issues if item.severity.value == "error"]

