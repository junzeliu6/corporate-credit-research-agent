"""V3.3C 通用年报基本面披露提取离线测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import credit_research_agent.fundamental_disclosures as disclosure_module
from credit_research_agent.documents import (
    DownloadResult,
    PageExtractionStatus,
    ParsedPage,
    PdfExtractionSummary,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.fundamental_disclosures import (
    FundamentalDisclosureError,
    FundamentalDisclosureExtractor,
    FundamentalExtractionStatus,
    build_generic_fundamental_artifacts,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
)


FIXED_NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


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
    *,
    pages: list[str] | None = None,
) -> tuple[DownloadResult, Path, Path]:
    texts = pages or [
        "公司基本情况\n公司前身为通用制造厂，成立于1998年。\n"
        "2020年在上海证券交易所上市。",
        "报告期内公司从事的主要业务\n公司主要从事工业机器人控制器的研发、生产和销售。",
        "（三）经营模式\n公司采购模式以合格供应商名录为基础。\n"
        "公司生产模式采用订单驱动与合理备货相结合。\n"
        "公司销售模式以直销为主、经销为辅。",
        "主要产品及其用途\n公司主要产品包括机器人控制器、伺服驱动器和工业软件。",
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


def test_generic_fundamentals_extracts_heading_scoped_disclosures_without_seeds(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)

    result = FundamentalDisclosureExtractor(clock=lambda: FIXED_NOW).extract(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    assert result.status is FundamentalExtractionStatus.SUCCESS
    modules = {item.research_module for item in result.bundle.facts}
    assert modules == {
        ResearchModule.DEVELOPMENT_HISTORY,
        ResearchModule.MAIN_BUSINESS,
        ResearchModule.OPERATING_MODEL,
        ResearchModule.MAIN_PRODUCTS,
    }
    assert all(item.entity_id == identity.entity_id for item in result.bundle.facts)
    assert all(item.source_id == "S01" for item in result.bundle.evidence)
    assert all("以岭" not in item.value for item in result.bundle.facts)
    assert {item.page_number for item in result.bundle.evidence} == {1, 2, 3, 4}


def test_generic_fundamental_artifacts_are_agent_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)

    def sandbox_safe_write(path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.resolve()

    # Windows 受限沙箱会拒绝 NamedTemporaryFile 的 os.replace；这里只替换
    # 写盘原语，产物结构和业务内容仍由真实 builder 生成。
    monkeypatch.setattr(disclosure_module, "_atomic_write", sandbox_safe_write)
    artifacts = build_generic_fundamental_artifacts(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        output_dir=tmp_path / "artifacts",
        clock=lambda: FIXED_NOW,
    )

    assert artifacts.entity_id == identity.entity_id
    assert artifacts.status is FundamentalExtractionStatus.SUCCESS
    assert artifacts.fact_count >= 4
    assert artifacts.fundamentals_path.is_file()
    assert artifacts.citations_path.is_file()
    assert identity.entity_id in str(artifacts.fundamentals_path)


def test_fundamental_extraction_rejects_source_entity_mismatch(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(identity, entity_id="CN-A-SSE-600000")
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)

    with pytest.raises(FundamentalDisclosureError) as exc_info:
        FundamentalDisclosureExtractor().extract(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert exc_info.value.code == "source_entity_mismatch"


def test_fundamental_extraction_rejects_pages_hash_mismatch(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(identity)
    download, summary_path, pages_path = _write_inputs(tmp_path, identity, source)
    pages_path.write_text(pages_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(FundamentalDisclosureError) as exc_info:
        FundamentalDisclosureExtractor().extract(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert exc_info.value.code == "pages_hash_mismatch"
