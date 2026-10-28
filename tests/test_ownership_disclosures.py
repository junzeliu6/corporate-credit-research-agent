"""V3.3C 通用年报股权披露提取离线测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import credit_research_agent.ownership_disclosures as disclosure_module
from credit_research_agent.documents import (
    DownloadResult,
    PageExtractionStatus,
    ParsedPage,
    PdfExtractionSummary,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    SourceRecord,
    SourceStatus,
    SourceType,
)
from credit_research_agent.ownership import ControlRelationType
from credit_research_agent.ownership_disclosures import (
    ExtractedShareholderRow,
    OwnershipDisclosureError,
    OwnershipDisclosureExtractor,
    OwnershipDisclosureStatus,
)


FIXED_NOW = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)


def _identity() -> CompanyIdentity:
    return CompanyIdentity(
        entity_id="CN-A-SSE-688999",
        legal_name="通用制造测试股份有限公司",
        short_name="通用制造",
        ticker="688999.SH",
        security_code="688999",
        exchange="上海证券交易所",
        country_of_incorporation="中国",
        entity_scope=EntityScope.CONSOLIDATED,
        exchange_code="SSE",
        listing_board="科创板",
        listing_status="listed",
    )


def _source(identity: CompanyIdentity, *, entity_id: str | None = None) -> SourceRecord:
    return SourceRecord(
        source_id="S01",
        entity_id=entity_id or identity.entity_id,
        title=f"{identity.legal_name}2025年年度报告",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="上海证券交易所",
        url="https://example.test/688999-2025.pdf",
        published_at=date(2026, 4, 30),
        report_period="FY2025",
        trusted_tier=1,
        status=SourceStatus.PARSED,
    )


def _write_inputs(
    tmp_path: Path,
    identity: CompanyIdentity,
    source: SourceRecord,
) -> tuple[DownloadResult, Path, Path]:
    texts = [
        "前十名股东持股情况\n测试控股集团 20.00%\n测试自然人甲 8.00%",
        "（一）控股股东情况\n控股股东名称 测试控股集团",
        "（二）实际控制人情况\n实际控制人姓名 张三、李四",
        "本企业最终控制方是张三。",
    ]
    pages_path = tmp_path / "pages.jsonl"
    parsed_pages = [
        ParsedPage(
            page_number=index,
            text=text,
            char_count=len(text),
            status=PageExtractionStatus.EXTRACTED,
        )
        for index, text in enumerate(texts, start=1)
    ]
    pages_path.write_text(
        "".join(item.json(ensure_ascii=False) + "\n" for item in parsed_pages),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "S01.pdf"
    pdf_path.write_bytes(b"%PDF-test-fixture")
    source_hash = calculate_sha256(pdf_path)
    summary = PdfExtractionSummary(
        source_id=source.source_id,
        source_file=pdf_path,
        source_sha256=source_hash,
        pages_sha256=calculate_sha256(pages_path),
        parser_name="fixture-parser",
        parser_version="1.0",
        parsed_at=FIXED_NOW,
        page_numbering="pdf_physical_page_1_based",
        total_pages=len(parsed_pages),
        pages_with_text=len(parsed_pages),
        empty_pages=0,
        failed_pages=0,
        total_characters=sum(item.char_count for item in parsed_pages),
        text_coverage_ratio=1.0,
        warnings=[],
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(summary.json(ensure_ascii=False), encoding="utf-8")
    download = DownloadResult(
        source_id=source.source_id,
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=source.report_period,
        file_path=pdf_path,
        manifest_path=tmp_path / "S01.metadata.json",
        accessed_at=FIXED_NOW,
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=pdf_path.stat().st_size,
        sha256=source_hash,
        from_cache=True,
    )
    return download, summary_path, pages_path


def _shareholders() -> list[ExtractedShareholderRow]:
    percentages = ["20", "8", "7", "6", "5", "4", "3", "2", "1", "0.5"]
    return [
        ExtractedShareholderRow(
            name="测试控股集团" if index == 1 else f"测试股东{index}",
            shareholder_nature="境内非国有法人" if index == 1 else "境内自然人",
            shares=100_000 - index,
            percentage=Decimal(value),
            page_number=1,
            raw_cells=[
                "测试控股集团" if index == 1 else f"测试股东{index}",
                str(100_000 - index),
                value,
            ],
        )
        for index, value in enumerate(percentages, start=1)
    ]


def test_shareholder_display_name_repairs_chinese_pdf_wrap_but_keeps_english_space() -> None:
    assert (
        disclosure_module._display_cell("楚昌投资集团  有限公司")
        == "楚昌投资集团有限公司"
    )
    assert (
        disclosure_module._display_cell("狮龙国际集团 (香港)有限公司")
        == "狮龙国际集团(香港)有限公司"
    )
    assert disclosure_module._display_cell("UBS   AG") == "UBS AG"


def _status_row(
    *,
    status: str,
    percentage: str = "10.37",
    shares: int = 764_045_593,
    nature: str | None = "境内自然人",
) -> ExtractedShareholderRow:
    return ExtractedShareholderRow(
        name="王佶",
        shareholder_nature=nature,
        shares=shares,
        percentage=Decimal(percentage),
        page_number=81,
        raw_cells=[
            "王佶",
            nature or "",
            f"{percentage}%",
            f"{shares:,}",
            status,
            "754,627,848" if status == "质押" else "6,417,745",
        ],
    )


def test_shareholder_status_rows_merge_only_when_core_holding_matches() -> None:
    consolidated = disclosure_module._consolidate_shareholder_status_rows(
        [_status_row(status="质押"), _status_row(status="冻结")]
    )

    assert len(consolidated) == 1
    assert consolidated[0].name == "王佶"
    assert consolidated[0].percentage == Decimal("10.37")
    assert consolidated[0].shares == 764_045_593
    assert "质押" in consolidated[0].raw_cells
    assert "冻结" in consolidated[0].raw_cells
    assert "754,627,848" in consolidated[0].raw_cells
    assert "6,417,745" in consolidated[0].raw_cells


@pytest.mark.parametrize(
    "second_row",
    [
        _status_row(status="冻结", percentage="9.99"),
        _status_row(status="冻结", shares=764_045_592),
        _status_row(status="冻结", nature="境内非国有法人"),
    ],
)
def test_shareholder_status_rows_reject_conflicting_holding_facts(
    second_row: ExtractedShareholderRow,
) -> None:
    with pytest.raises(OwnershipDisclosureError) as exc_info:
        disclosure_module._consolidate_shareholder_status_rows(
            [_status_row(status="质押"), second_row]
        )

    assert exc_info.value.code == "conflicting_shareholder_rows"


@pytest.mark.parametrize("duplicate_status", ["", "质押"])
def test_shareholder_status_rows_reject_unexplained_duplicate(
    duplicate_status: str,
) -> None:
    with pytest.raises(OwnershipDisclosureError) as exc_info:
        disclosure_module._consolidate_shareholder_status_rows(
            [_status_row(status="质押"), _status_row(status=duplicate_status)]
        )

    assert exc_info.value.code == "duplicate_shareholder_rows"


def test_shareholder_status_rows_do_not_merge_different_shareholders() -> None:
    other = _status_row(status="冻结").copy(update={"name": "测试股东乙"})

    consolidated = disclosure_module._consolidate_shareholder_status_rows(
        [_status_row(status="质押"), other]
    )

    assert [row.name for row in consolidated] == ["王佶", "测试股东乙"]


def test_shareholder_status_rows_reject_cross_page_merge() -> None:
    frozen_on_next_page = _status_row(status="冻结").copy(
        update={"page_number": 82}
    )

    with pytest.raises(OwnershipDisclosureError) as exc_info:
        disclosure_module._consolidate_shareholder_status_rows(
            [_status_row(status="质押"), frozen_on_next_page]
        )

    assert exc_info.value.code == "duplicate_shareholder_rows"


def _patch_table_extractors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        disclosure_module,
        "_extract_top_shareholders",
        lambda _pdf_path, _pages: _shareholders(),
    )

    def fake_names(_pdf_path: Path, _page_numbers, *, accepted_keys):
        if accepted_keys[0] == "控股股东名称":
            return [("测试控股集团", 2, ["控股股东名称", "测试控股集团"])]
        return [("张三、李四", 3, ["实际控制人姓名", "张三、李四"])]

    monkeypatch.setattr(disclosure_module, "_extract_key_value_names", fake_names)


def test_generic_ownership_keeps_partial_top_ten_without_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)
    _patch_table_extractors(monkeypatch)

    extraction, result = OwnershipDisclosureExtractor(clock=lambda: FIXED_NOW).extract(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    assert extraction.status is OwnershipDisclosureStatus.PARTIAL_SUCCESS
    assert result.graph.formal_ready is False
    assert len(result.dataset.ownership_edges) == 10
    assert sum(item.percentage for item in result.dataset.ownership_edges) == Decimal("56.5")
    assert result.dataset.completeness_declarations == []
    assert all(item.name != "其他未单列股东" for item in result.dataset.nodes)
    assert all("以岭" not in item.name for item in result.dataset.nodes)


def test_generic_ownership_preserves_multiple_disclosed_controllers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)
    _patch_table_extractors(monkeypatch)

    extraction, result = OwnershipDisclosureExtractor(clock=lambda: FIXED_NOW).extract(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    assert extraction.actual_controllers == ["张三", "李四"]
    actual = next(
        item
        for item in result.dataset.control_relationships
        if item.relation_type is ControlRelationType.ACTUAL_CONTROLLER
    )
    names = {
        item.name
        for item in result.dataset.nodes
        if item.node_id in actual.subject_node_ids
    }
    assert names == {"张三", "李四"}
    assert actual.controlled_node_id == identity.entity_id


def test_ownership_extraction_rejects_source_entity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    source = _source(identity, entity_id="CN-A-SSE-600000")
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)
    _patch_table_extractors(monkeypatch)

    with pytest.raises(OwnershipDisclosureError) as exc_info:
        OwnershipDisclosureExtractor().extract(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert exc_info.value.code == "source_entity_mismatch"


def test_ownership_extraction_rejects_pages_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)
    _patch_table_extractors(monkeypatch)
    pages_path.write_text(pages_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(OwnershipDisclosureError) as exc_info:
        OwnershipDisclosureExtractor().extract(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert exc_info.value.code == "pages_hash_mismatch"
