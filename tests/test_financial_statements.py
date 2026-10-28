"""V3.3B 通用财务报表定位、解析与既有财务 Skill 兼容测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import credit_research_agent.financial as financial_module
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial_agent_runtime import (
    build_financial_snapshot_from_artifacts,
)
from credit_research_agent.financial import (
    FinancialItem,
    FinancialSourceInput,
    FinancialStatement,
)
from credit_research_agent.financial_statements import (
    FinancialStatementExtractionError,
    FinancialStatementExtractor,
    build_generic_financial_artifacts,
    parse_financial_amount,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    SourceRecord,
    SourceStatus,
    SourceType,
)


FIXED_NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


def _identity(
    code: str = "600998",
    *,
    legal_name: str = "九州通医药集团股份有限公司",
    short_name: str = "九州通",
) -> CompanyIdentity:
    return CompanyIdentity(
        entity_id=f"CN-A-SSE-{code}",
        legal_name=legal_name,
        short_name=short_name,
        ticker=f"{code}.SH",
        security_code=code,
        exchange="上海证券交易所",
        country_of_incorporation="中国",
        entity_scope=EntityScope.CONSOLIDATED,
        exchange_code="SSE",
        listing_board="沪市主板",
        listing_status="listed",
    )


def _statement_pages(
    *,
    unit: str = "元",
    include_balance: bool = True,
    include_income: bool = True,
    include_cash_flow: bool = True,
    current_revenue: str = "3,000.00",
    prior_revenue: str = "2,500.00",
    prior_borrowings: str = "200.00",
) -> list[str]:
    """构造包含摘要、合并表、母公司表及附注干扰项的精简年报文本。"""

    pages = [
        "主要会计数据\n营业收入 999,999.00 888,888.00\n"
        "本页仅为财务摘要，不是法定合并财务报表。"
    ]
    if include_balance:
        pages.extend(
            [
                "合并资产负债表\n2025年12月31日\n编制单位：九州通医药集团股份有限公司\n"
                f"单位：人民币{unit}\n项目 2025年12月31日 2024年12月31日\n"
                "货币资金 1,200.00 1,000.00\n"
                "应收票据及应收账款 500.00 450.00\n"
                "预付款项 60.00 50.00\n"
                "存货\n700.00 600.00\n"
                "流动资产合计 3,000.00 2,500.00",
                "合并资产负债表（续）\n2025年12月31日\n"
                f"单位：人民币{unit}\n"
                "固定资产 2,000.00 1,800.00\n"
                "资产合计 8,000.00 7,000.00\n"
                f"短期借款 300.00 {prior_borrowings}\n"
                "应付账款 900.00 800.00\n"
                "流动负债合计 2,200.00 2,000.00\n"
                "负债总计 4,000.00 3,800.00\n"
                "所有者权益合计 4,000.00 3,200.00",
                "母公司资产负债表\n2025年12月31日\n"
                f"单位：人民币{unit}\n"
                "货币资金 999,999.00 888,888.00\n"
                "资产总计 999,999.00 888,888.00",
            ]
        )
    if include_income:
        pages.extend(
            [
                "合并利润表\n2025年度\n编制单位：九州通医药集团股份有限公司\n"
                f"单位：人民币{unit}\n项目 2025年度 2024年度\n"
                "一、营业总收入 9,999.00 8,888.00\n"
                f"其中：营业收入 {current_revenue} {prior_revenue}\n"
                "其中：营业成本 (2,100.00) (1,700.00)\n"
                "五、净利润（净亏损以“-”号填列） -80.00 60.00",
                "母公司利润表\n2025年度\n"
                f"单位：人民币{unit}\n营业收入 999,999.00 888,888.00",
            ]
        )
    if include_cash_flow:
        pages.extend(
            [
                "合并现金流量表\n2025年度\n编制单位：九州通医药集团股份有限公司\n"
                f"单位：人民币{unit}\n项目 2025年度 2024年度\n"
                "销售商品、提供劳务收到的现金 3,400.00 2,900.00\n"
                "经营活动产生的现金流量净额 420.00 (120.00)",
                "母公司现金流量表\n2025年度\n"
                f"单位：人民币{unit}\n经营活动产生的现金流量净额 999,999.00 888,888.00",
            ]
        )
    pages.append(
        "财务报表附注\n应收账款附注余额 777,777.00\n"
        "附注中的同名科目不得冒充合并主表数据。"
    )
    return pages


def _values_before_label_pages() -> list[str]:
    """模拟 PDF 阅读序为“两期金额→科目名”的合并主表。"""

    return [
        "合并资产负债表\n2025年12月31日\n编制单位：九州通医药集团股份有限公司\n"
        "单位：人民币元\n项目 2025年12月31日 2024年12月31日\n"
        "11,807,695,171.46 6,527,717,310.58\n货币资金\n"
        "3,963,090,283.54 3,395,150,530.43\n应收账款\n"
        "766,002,809.14 761,184,010.69\n存货\n"
        "20,000,000,000.00 14,000,000,000.00\n流动资产合计\n"
        "1,264,831,784.07 1,423,870,401.22\n固定资产\n"
        "44,428,668,050.23 38,924,497,999.89\n资产总计\n"
        "887,661,029.95 846,505,137.61\n短期借款\n"
        "1,081,200,076.36 1,044,518,434.13\n应付账款\n"
        "5,000,000,000.00 5,100,000,000.00\n流动负债合计\n"
        "13,208,915,558.28 13,474,077,625.00\n负债总计\n"
        "所有者权益合计 31,219,752,491.95 25,450,420,374.89",
        "合并利润表\n2025年度\n单位：人民币元\n项目 2025年度 2024年度\n"
        "营业收入 30,000,000,000.00 25,000,000,000.00\n"
        "营业成本 20,000,000,000.00 18,000,000,000.00\n"
        "净利润 2,000,000,000.00 1,800,000,000.00",
        "合并现金流量表\n2025年度\n单位：人民币元\n项目 2025年度 2024年度\n"
        "销售商品、提供劳务收到的现金 32,000,000,000.00 27,000,000,000.00\n"
        "经营活动产生的现金流量净额 3,000,000,000.00 2,000,000,000.00",
    ]


def _source_input(
    tmp_path: Path,
    identity: CompanyIdentity,
    pages: list[str],
    *,
    source_id: str = "S01",
    report_period: str = "FY2025",
) -> FinancialSourceInput:
    entity_dir = tmp_path / identity.entity_id / source_id
    entity_dir.mkdir(parents=True, exist_ok=True)
    pages_path = entity_dir / "pages.jsonl"
    serialized_pages = "".join(
        json.dumps(
            {
                "page_number": page_number,
                "text": text,
                "char_count": len(text),
                "status": "extracted",
                "error_message": None,
            },
            ensure_ascii=False,
        )
        + "\n"
        for page_number, text in enumerate(pages, start=1)
    )
    pages_path.write_text(serialized_pages, encoding="utf-8")
    pages_sha256 = hashlib.sha256(pages_path.read_bytes()).hexdigest()
    pdf_path = entity_dir / f"{source_id}.pdf"
    pdf_path.write_bytes(b"%PDF-fixture")
    source_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    summary_path = entity_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "source_file": str(entity_dir / f"{source_id}.pdf"),
                "source_sha256": source_sha256,
                "pages_sha256": pages_sha256,
                "parser_name": "fixture-parser",
                "parser_version": "1.0",
                "parsed_at": FIXED_NOW.isoformat(),
                "page_numbering": "pdf_physical_page_1_based",
                "total_pages": len(pages),
                "pages_with_text": len(pages),
                "empty_pages": 0,
                "failed_pages": 0,
                "total_characters": sum(len(item) for item in pages),
                "text_coverage_ratio": 1.0,
                "warnings": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest_path = entity_dir / f"{source_id}.metadata.json"
    manifest_path.write_text("{}", encoding="utf-8")
    source = SourceRecord(
        source_id=source_id,
        entity_id=identity.entity_id,
        title=f"{identity.legal_name}2025年年度报告",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="巨潮资讯网（上市公司公告）",
        url=f"https://static.cninfo.com.cn/finalpage/2026/{source_id}.PDF",
        published_at=date(2026, 4, 30),
        report_period=report_period,
        trusted_tier=1,
        status=SourceStatus.PARSED,
        local_path=str(pdf_path),
    )
    download = DownloadResult(
        source_id=source_id,
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=report_period,
        file_path=pdf_path,
        manifest_path=manifest_path,
        accessed_at=FIXED_NOW,
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=pdf_path.stat().st_size,
        sha256=source_sha256,
        from_cache=True,
    )
    return FinancialSourceInput(
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )


def _status_value(result) -> str:
    status = result.status
    return status.value if hasattr(status, "value") else str(status)


def _fact(result, item: FinancialItem, period: str):
    return next(
        fact
        for fact in result.financial_facts
        if fact.item is item and fact.period == period
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", Decimal("1234.56")),
        ("-1,234.56", Decimal("-1234.56")),
        ("(1,234.56)", Decimal("-1234.56")),
        (" 1 234.56 ", Decimal("1234.56")),
        ("—", None),
        ("-", None),
        ("", None),
    ],
)
def test_parse_financial_amount_distinguishes_negative_and_missing(
    raw: str, expected: Decimal | None
) -> None:
    assert parse_financial_amount(raw) == expected


def test_extracts_only_consolidated_statements_and_excludes_noise(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(tmp_path, identity, _statement_pages())

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "success"
    assert set(result.statements_found) == {
        FinancialStatement.BALANCE_SHEET,
        FinancialStatement.INCOME_STATEMENT,
        FinancialStatement.CASH_FLOW_STATEMENT,
    }
    assert result.missing_statements == []
    assert all(fact.entity_scope is EntityScope.CONSOLIDATED for fact in result.financial_facts)
    assert all(fact.value != Decimal("999999.00") for fact in result.financial_facts)
    assert all(fact.value != Decimal("777777.00") for fact in result.financial_facts)


def test_extracts_periods_split_labels_aliases_and_negative_values(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(tmp_path, identity, _statement_pages())

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _fact(result, FinancialItem.INVENTORY, "FY2025").value == Decimal("700.00")
    assert _fact(result, FinancialItem.INVENTORY, "FY2024").value == Decimal("600.00")
    assert _fact(result, FinancialItem.TOTAL_ASSETS, "FY2025").value == Decimal("8000.00")
    assert _fact(result, FinancialItem.TOTAL_LIABILITIES, "FY2024").value == Decimal("3800.00")
    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal("3000.00")
    assert _fact(result, FinancialItem.OPERATING_COST, "FY2025").value == Decimal("-2100.00")
    assert _fact(result, FinancialItem.NET_PROFIT, "FY2025").value == Decimal("-80.00")
    assert _fact(result, FinancialItem.OPERATING_CASH_FLOW, "FY2024").value == Decimal("-120.00")


def test_explicit_blank_short_term_borrowings_row_is_disclosed_zero(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("短期借款 300.00 200.00", "短期借款")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "success"
    assert _fact(
        result, FinancialItem.SHORT_TERM_BORROWINGS, "FY2025"
    ).value == Decimal("0")
    assert _fact(
        result, FinancialItem.SHORT_TERM_BORROWINGS, "FY2024"
    ).value == Decimal("0")
    row = next(
        row
        for row in result.rows
        if row.item is FinancialItem.SHORT_TERM_BORROWINGS
    )
    assert row.extraction_status == "disclosed_blank_as_zero"
    assert row.layout_direction == "explicit_blank_cells"
    assert row.current_raw_value == row.prior_raw_value == "0"
    assert not [
        issue
        for issue in result.issues
        if issue.item is FinancialItem.SHORT_TERM_BORROWINGS
    ]


def test_missing_short_term_borrowings_row_is_not_invented_as_zero(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("\n短期借款 300.00 200.00", "")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert not [
        fact
        for fact in result.financial_facts
        if fact.item is FinancialItem.SHORT_TERM_BORROWINGS
    ]
    assert any(
        issue.code == "financial_item_not_found"
        and issue.item is FinancialItem.SHORT_TERM_BORROWINGS
        for issue in result.issues
    )


def test_single_amount_short_term_borrowings_row_remains_ambiguous(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("短期借款 300.00 200.00", "短期借款 300.00")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert not [
        fact
        for fact in result.financial_facts
        if fact.item is FinancialItem.SHORT_TERM_BORROWINGS
    ]
    assert any(
        issue.code == "ambiguous_amount_columns"
        and issue.item is FinancialItem.SHORT_TERM_BORROWINGS
        for issue in result.issues
    )


def test_values_before_labels_are_bound_to_the_correct_items(
    tmp_path: Path,
) -> None:
    identity = _identity(
        "002602",
        legal_name="浙江世纪华通集团股份有限公司",
        short_name="世纪华通",
    )
    source_input = _source_input(
        tmp_path,
        identity,
        _values_before_label_pages(),
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert _fact(result, FinancialItem.MONETARY_FUNDS, "FY2025").value == Decimal(
        "11807695171.46"
    )
    assert _fact(
        result, FinancialItem.ACCOUNTS_RECEIVABLE, "FY2025"
    ).value == Decimal("3963090283.54")
    assert _fact(result, FinancialItem.INVENTORY, "FY2024").value == Decimal(
        "761184010.69"
    )
    assert _fact(result, FinancialItem.TOTAL_ASSETS, "FY2025").value == Decimal(
        "44428668050.23"
    )
    assert _fact(
        result, FinancialItem.TOTAL_LIABILITIES, "FY2025"
    ).value == Decimal("13208915558.28")
    assert _fact(result, FinancialItem.TOTAL_EQUITY, "FY2025").value == Decimal(
        "31219752491.95"
    )
    assert {
        row.layout_direction
        for row in result.rows
        if row.statement is FinancialStatement.BALANCE_SHEET
        and row.item is not FinancialItem.TOTAL_EQUITY
    } == {"values_before_label"}
    assert not [
        issue
        for issue in result.issues
        if issue.code.startswith("balance_sheet_equation")
    ]


def test_split_label_around_values_preserves_full_label_context(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = _statement_pages()
    pages[2] = pages[2].replace(
        "所有者权益合计 4,000.00 3,200.00",
        "归属于母公司所有者权益\n3,500.00 2,800.00\n（或股东权益）合计\n"
        "所有者权益（或股东权\n4,000.00 3,200.00\n益）合计",
    )
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _fact(result, FinancialItem.TOTAL_EQUITY, "FY2025").value == Decimal(
        "4000.00"
    )
    equity_row = next(
        row for row in result.rows if row.item is FinancialItem.TOTAL_EQUITY
    )
    assert equity_row.layout_direction == "split_label_around_values"
    assert "所有者权益（或股东权" in equity_row.quote
    assert "益）合计" in equity_row.quote
    assert equity_row.current_value_cny != Decimal("3500.00")
    assert any(
        issue.code == "blocked_split_label_context"
        and issue.item is FinancialItem.TOTAL_EQUITY
        for issue in result.issues
    )


def test_page_local_layout_direction_and_net_profit_narrative_filter(
    tmp_path: Path,
) -> None:
    identity = _identity()
    base_pages = _statement_pages(include_income=False)
    income_pages = [
        "合并利润表\n2025年度\n单位：人民币元\n项目 2025年度 2024年度\n"
        "3,000.00 2,500.00\n其中：营业收入\n"
        "2,100.00 1,700.00\n其中：营业成本",
        "项目 2025年度 2024年度\n"
        "五、净利润（净亏损以‘-’号填\n"
        "80.00 60.00\n列）\n"
        "本期发生同一控制下企业合并的，被合并方在合并前实现的"
        "净利润为：0.00元，上期为：0.00元。",
    ]
    # 摘要+资产负债表三页后插入合并利润表，再进入现金流量表。
    pages = [*base_pages[:4], *income_pages, *base_pages[4:]]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal(
        "3000.00"
    )
    assert _fact(result, FinancialItem.OPERATING_COST, "FY2025").value == Decimal(
        "2100.00"
    )
    assert _fact(result, FinancialItem.NET_PROFIT, "FY2025").value == Decimal(
        "80.00"
    )
    net_profit_row = next(
        row for row in result.rows if row.item is FinancialItem.NET_PROFIT
    )
    assert net_profit_row.layout_direction == "label_before_values"
    assert "被合并方" not in net_profit_row.quote


def test_balance_sheet_mismatch_is_quarantined_but_other_statements_survive(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace(
            "所有者权益合计 4,000.00 3,200.00",
            "所有者权益合计 3,900.00 3,200.00",
        )
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert [
        row for row in result.rows if row.statement is FinancialStatement.BALANCE_SHEET
    ]
    assert not [
        fact
        for fact in result.financial_facts
        if fact.statement is FinancialStatement.BALANCE_SHEET
    ]
    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal(
        "3000.00"
    )
    assert _fact(result, FinancialItem.OPERATING_CASH_FLOW, "FY2025").value == Decimal(
        "420.00"
    )
    mismatch = next(
        issue
        for issue in result.issues
        if issue.code == "balance_sheet_equation_mismatch"
    )
    assert mismatch.period == "FY2025"
    assert result.validation is not None and result.validation.is_valid is False


def test_missing_equity_quarantines_unverified_balance_sheet(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("\n所有者权益合计 4,000.00 3,200.00", "")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert not [
        fact
        for fact in result.financial_facts
        if fact.statement is FinancialStatement.BALANCE_SHEET
    ]
    assert any(
        issue.code == "balance_sheet_equation_unverified"
        and "所有者权益合计" in issue.message
        for issue in result.issues
    )
    assert result.validation is not None and result.validation.is_valid is True


def test_balance_sheet_dimension_conflict_is_rejected_before_equation_check(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(tmp_path, identity, _statement_pages())
    extractor = FinancialStatementExtractor()
    result = extractor.extract(identity=identity, source_inputs=[source_input])
    conflicting_rows = [
        row.copy(update={"prior_period": "FY2023"})
        if row.item is FinancialItem.TOTAL_EQUITY
        else row
        for row in result.rows
        if row.statement is FinancialStatement.BALANCE_SHEET
    ]

    quarantined, issues = extractor._validate_balance_sheet_structure(  # noqa: SLF001
        conflicting_rows
    )

    assert quarantined == {"S01"}
    assert [issue.code for issue in issues] == [
        "balance_sheet_dimension_conflict"
    ]


def test_balance_sheet_rounding_tolerance_uses_reported_precision(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace(
            "所有者权益合计 4,000.00 3,200.00",
            "所有者权益合计 3,999.99 3,200.00",
        )
        for page in _statement_pages(unit="万元")
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    # 三项均以0.01万元披露，单项0.005万元的舍入误差上限
    # 可解释100元的勾稽差，但不会放宽到整1万元。
    assert _fact(result, FinancialItem.TOTAL_ASSETS, "FY2025").value == Decimal(
        "80000000.00"
    )
    assert not [
        issue
        for issue in result.issues
        if issue.code == "balance_sheet_equation_mismatch"
    ]


def test_unit_wan_is_normalized_to_cny_without_losing_raw_unit(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        _statement_pages(unit="万元"),
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    revenue = _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025")
    assert revenue.value == Decimal("30000000.00")
    assert revenue.unit == "CNY"
    revenue_row = next(
        item
        for item in result.rows
        if item.item is FinancialItem.OPERATING_REVENUE
    )
    assert revenue_row.original_unit == "万元"
    assert revenue_row.unit_multiplier == Decimal("10000")
    assert revenue_row.current_raw_value == "3,000.00"
    assert revenue_row.current_value_cny == Decimal("30000000.00")


def test_missing_prior_value_is_not_fabricated_as_zero(tmp_path: Path) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path, identity, _statement_pages(prior_borrowings="—")
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    borrowings = [
        fact
        for fact in result.financial_facts
        if fact.item is FinancialItem.SHORT_TERM_BORROWINGS
    ]
    assert [(item.period, item.value) for item in borrowings] == [
        ("FY2025", Decimal("300.00"))
    ]
    borrowing_row = next(
        item
        for item in result.rows
        if item.item is FinancialItem.SHORT_TERM_BORROWINGS
    )
    assert borrowing_row.prior_raw_value == "—"
    assert borrowing_row.prior_value_cny is None


def test_adjusted_comparative_column_is_recorded_as_restatement(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        [
            "合并利润表\n2025年度\n编制单位：九州通医药集团股份有限公司\n"
            "单位：人民币元\n项目 2025年度 2024年度（调整后）\n"
            "营业收入 3,000.00 2,400.00\n"
            "营业成本 2,100.00 1,600.00\n"
            "净利润 80.00 60.00"
        ],
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    revenue_row = next(
        item
        for item in result.rows
        if item.item is FinancialItem.OPERATING_REVENUE
    )
    assert revenue_row.comparison_restated is True
    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2024").value == Decimal(
        "2400.00"
    )


def test_missing_one_statement_is_partial_success(tmp_path: Path) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        _statement_pages(include_cash_flow=False),
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "partial_success"
    assert FinancialStatement.CASH_FLOW_STATEMENT in result.missing_statements
    assert _fact(result, FinancialItem.TOTAL_ASSETS, "FY2025").value == Decimal("8000.00")
    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal("3000.00")


def test_no_financial_statements_returns_failed_without_guessing(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        ["年度报告正文\n本页没有三大财务报表。", "财务报表附注\n营业收入 123.00"],
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "failed"
    assert result.financial_facts == []
    assert set(result.missing_statements) == set(FinancialStatement)


def test_generic_artifacts_reuse_existing_calculations_without_default_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    source_input = _source_input(tmp_path / "input", identity, _statement_pages())

    def fail_if_default_seed_is_loaded():
        raise AssertionError("新企业不得加载以岭药业默认 financial_seeds.json")

    monkeypatch.setattr(
        financial_module,
        "load_financial_seed_catalog",
        fail_if_default_seed_is_loaded,
    )

    artifacts = build_generic_financial_artifacts(
        identity=identity,
        source_inputs=[source_input],
        output_dir=tmp_path / "output",
    )

    assert artifacts.fact_count > 0
    assert artifacts.metric_count > 0
    assert artifacts.validation_path.is_file()
    assert artifacts.facts_path.is_file()
    facts_payload = json.loads(artifacts.facts_path.read_text(encoding="utf-8"))
    assert facts_payload["identity"]["entity_id"] == identity.entity_id
    assert {item["entity_id"] for item in facts_payload["financial_facts"]} == {
        identity.entity_id
    }
    snapshot = build_financial_snapshot_from_artifacts(
        artifacts.skill_context_paths()
    )
    assert snapshot["company"]["ticker"] == identity.ticker
    assert snapshot["scope"]["periods"] == ["FY2024", "FY2025"]
    assert snapshot["verified_financial_metrics"]


def test_different_companies_remain_isolated_even_with_same_source_id(
    tmp_path: Path,
) -> None:
    first_identity = _identity()
    second_identity = _identity(
        "601138",
        legal_name="富士康工业互联网股份有限公司",
        short_name="工业富联",
    )
    first_input = _source_input(
        tmp_path / "inputs",
        first_identity,
        _statement_pages(current_revenue="3,000.00"),
    )
    second_input = _source_input(
        tmp_path / "inputs",
        second_identity,
        _statement_pages(current_revenue="9,000.00"),
    )

    first = FinancialStatementExtractor().extract(
        identity=first_identity,
        source_inputs=[first_input],
    )
    second = FinancialStatementExtractor().extract(
        identity=second_identity,
        source_inputs=[second_input],
    )

    assert {fact.entity_id for fact in first.financial_facts} == {
        first_identity.entity_id
    }
    assert {fact.entity_id for fact in second.financial_facts} == {
        second_identity.entity_id
    }
    assert _fact(first, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal("3000.00")
    assert _fact(second, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal("9000.00")
    assert {fact.source_id for fact in first.financial_facts} == {"S01"}
    assert {fact.source_id for fact in second.financial_facts} == {"S01"}


def test_source_bound_to_another_company_is_rejected(tmp_path: Path) -> None:
    first_identity = _identity()
    second_identity = _identity(
        "601138",
        legal_name="富士康工业互联网股份有限公司",
        short_name="工业富联",
    )
    wrong_input = _source_input(
        tmp_path,
        second_identity,
        _statement_pages(),
    )

    with pytest.raises(FinancialStatementExtractionError, match="主体|entity"):
        FinancialStatementExtractor().extract(
            identity=first_identity,
            source_inputs=[wrong_input],
        )


def test_source_without_entity_binding_is_rejected(tmp_path: Path) -> None:
    identity = _identity()
    source_input = _source_input(tmp_path, identity, _statement_pages())
    unbound_input = source_input.copy(
        update={"source": source_input.source.copy(update={"entity_id": None})}
    )

    with pytest.raises(
        FinancialStatementExtractionError, match="未绑定研究主体"
    ) as exc_info:
        FinancialStatementExtractor().extract(
            identity=identity,
            source_inputs=[unbound_input],
        )

    assert exc_info.value.code == "source_entity_missing"


def test_explicit_non_cny_statement_is_rejected_instead_of_silent_conversion(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("单位：人民币元", "单位：元 币种：美元")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "failed"
    assert result.financial_facts == []
    assert {
        issue.code for issue in result.issues if issue.severity.value == "error"
    } == {"unsupported_statement_currency"}


def test_unit_without_currency_evidence_is_not_assumed_to_be_cny(
    tmp_path: Path,
) -> None:
    identity = _identity()
    pages = [
        page.replace("单位：人民币元", "单位：元")
        for page in _statement_pages()
    ]
    source_input = _source_input(tmp_path, identity, pages)

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _status_value(result) == "failed"
    assert result.financial_facts == []
    assert {
        issue.code for issue in result.issues if issue.severity.value == "error"
    } == {"statement_currency_not_found"}


def test_reversed_period_columns_are_mapped_by_header_order(tmp_path: Path) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        [
            "合并利润表\n2025年度\n编制单位：九州通医药集团股份有限公司\n"
            "单位：人民币元\n项目 2024年度 2025年度\n"
            "营业收入 2,400.00 3,000.00\n"
            "营业成本 1,600.00 2,100.00\n"
            "净利润 60.00 80.00"
        ],
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2025").value == Decimal(
        "3000.00"
    )
    assert _fact(result, FinancialItem.OPERATING_REVENUE, "FY2024").value == Decimal(
        "2400.00"
    )


def test_three_value_columns_are_not_guessed(tmp_path: Path) -> None:
    identity = _identity()
    source_input = _source_input(
        tmp_path,
        identity,
        [
            "合并利润表\n2025年度\n编制单位：九州通医药集团股份有限公司\n"
            "单位：人民币元\n项目 2025年度 2024年度（调整前） 2024年度（调整后）\n"
            "营业收入 3,000.00 2,500.00 2,400.00\n"
            "营业成本 2,100.00 1,700.00 1,600.00\n"
            "净利润 80.00 70.00 60.00"
        ],
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[source_input],
    )

    assert not [
        fact
        for fact in result.financial_facts
        if fact.item is FinancialItem.OPERATING_REVENUE
    ]
    assert any(
        issue.code == "ambiguous_amount_columns"
        and issue.item is FinancialItem.OPERATING_REVENUE
        for issue in result.issues
    )


def test_newer_explicitly_restated_comparative_value_is_authoritative(
    tmp_path: Path,
) -> None:
    identity = _identity()
    latest = _source_input(
        tmp_path,
        identity,
        [
            "合并利润表\n2025年度\n单位：人民币元\n"
            "项目 2025年度 2024年度（调整后）\n"
            "营业收入 3,000.00 2,400.00\n"
            "营业成本 2,100.00 1,600.00\n"
            "净利润 80.00 60.00"
        ],
        source_id="S01",
        report_period="FY2025",
    )
    older = _source_input(
        tmp_path,
        identity,
        [
            "合并利润表\n2024年度\n单位：人民币元\n"
            "项目 2024年度 2023年度\n"
            "营业收入 2,500.00 2,000.00\n"
            "营业成本 1,700.00 1,400.00\n"
            "净利润 70.00 50.00"
        ],
        source_id="S02",
        report_period="FY2024",
    )

    result = FinancialStatementExtractor().extract(
        identity=identity,
        source_inputs=[latest, older],
    )

    fy2024_revenue = [
        fact
        for fact in result.financial_facts
        if fact.item is FinancialItem.OPERATING_REVENUE
        and fact.period == "FY2024"
    ]
    assert [(fact.source_id, fact.value) for fact in fy2024_revenue] == [
        ("S01", Decimal("2400.00"))
    ]
    assert any(
        issue.code == "restated_comparative_value_selected"
        for issue in result.issues
    )
    normalized = next(
        fact
        for fact in result.normalized_facts
        if fact.item is FinancialItem.OPERATING_REVENUE
        and fact.period == "FY2024"
    )
    assert normalized.value == Decimal("2400.00")

