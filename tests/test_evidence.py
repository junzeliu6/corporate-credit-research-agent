import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import (
    DownloadResult,
    PageExtractionStatus,
    ParsedPage,
    PdfExtractionSummary,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.evidence import (
    EvidenceBuildError,
    EvidenceBuilder,
    build_evidence_artifacts,
    render_citations,
)
from credit_research_agent.models import EvidenceBundle
from credit_research_agent.sources import SourceCatalog


FIXED_TIME = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
PAGE_6_TEXT = """石家庄以岭药业股份有限公司 2025 年年度报告全文
第二节 公司简介和主要财务指标
一、公司信息
股票简称 以岭药业 股票代码 002603
股票上市证券交易所 深圳证券交易所
公司的中文名称 石家庄以岭药业股份有限公司"""


@pytest.fixture
def identity():
    return CompanyResolver.from_default_catalog().resolve("以岭药业")


@pytest.fixture
def source():
    return SourceCatalog.from_default_catalog().get("S01")


@pytest.fixture
def download(tmp_path, source):
    return DownloadResult(
        source_id="S01",
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=source.report_period,
        file_path=tmp_path / "S01.pdf",
        manifest_path=tmp_path / "S01.metadata.json",
        accessed_at=FIXED_TIME,
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=100,
        sha256="a" * 64,
        from_cache=True,
    )


def _write_extraction_artifacts(tmp_path, download, text=PAGE_6_TEXT):
    pages_path = tmp_path / "pages.jsonl"
    page = ParsedPage(
        page_number=6,
        text=text,
        char_count=len(text),
        status=PageExtractionStatus.EXTRACTED,
    )
    pages_path.write_text(page.json(ensure_ascii=False) + "\n", encoding="utf-8")
    summary = PdfExtractionSummary(
        source_id="S01",
        source_file=download.file_path,
        source_sha256=download.sha256,
        pages_sha256=calculate_sha256(pages_path),
        parser_name="pdfplumber",
        parser_version="0.11.10",
        parsed_at=FIXED_TIME,
        page_numbering="pdf_physical_page_1_based",
        total_pages=1,
        pages_with_text=1,
        empty_pages=0,
        failed_pages=0,
        total_characters=len(text),
        text_coverage_ratio=1.0,
        warnings=[],
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(summary.json(ensure_ascii=False), encoding="utf-8")
    return summary_path, pages_path


def test_builder_creates_cross_referenced_identity_facts(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(tmp_path, download)

    bundle = EvidenceBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    assert bundle.schema_version == "0.4.0"
    assert len(bundle.facts) == 4
    assert len(bundle.evidence) == 3
    assert bundle.identity.evidence_ids == ["E001", "E002", "E003"]
    assert bundle.facts[2].value == "002603"
    assert bundle.facts[2].evidence_ids == ["E002"]
    assert bundle.evidence[1].page_number == 6
    assert bundle.evidence[1].quote == "股票简称 以岭药业 股票代码 002603"
    assert bundle.sources[0].status.value == "parsed"


def test_citation_renderer_uses_source_id_url_and_physical_page(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(tmp_path, download)
    bundle = EvidenceBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    citations = render_citations(bundle)

    assert "[S01]" in citations
    assert str(source.url) in citations
    assert "访问日期 2026-10-21" in citations
    assert "PDF 第 6 页" in citations


def test_builder_rejects_tampered_pages_artifact(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(tmp_path, download)
    pages_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(EvidenceBuildError) as captured:
        EvidenceBuilder().build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert captured.value.code == "pages_hash_mismatch"


def test_builder_rejects_missing_expected_source_text(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(
        tmp_path,
        download,
        text="本页没有配置的身份字段",
    )

    with pytest.raises(EvidenceBuildError) as captured:
        EvidenceBuilder().build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert captured.value.code == "evidence_text_mismatch"


def test_bundle_schema_rejects_dangling_evidence_reference(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(tmp_path, download)
    bundle = EvidenceBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )
    payload = json.loads(bundle.json())
    payload["facts"][0]["evidence_ids"] = ["E999"]

    with pytest.raises(ValidationError, match="不存在的证据"):
        EvidenceBundle.parse_obj(payload)


def test_orchestrator_writes_evidence_json_citations_and_failure_record(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_extraction_artifacts(tmp_path, download)
    evidence_output = tmp_path / "examples" / "evidence.json"
    citations_output = tmp_path / "examples" / "citations.md"

    artifacts = build_evidence_artifacts(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        output_dir=tmp_path / "evidence",
        evidence_output=evidence_output,
        citations_output=citations_output,
        clock=lambda: FIXED_TIME,
    )

    assert artifacts.fact_count == 4
    assert artifacts.evidence_count == 3
    assert evidence_output.is_file()
    assert citations_output.is_file()

    pages_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(EvidenceBuildError):
        build_evidence_artifacts(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            output_dir=tmp_path / "evidence",
            clock=lambda: FIXED_TIME,
        )
    failure = json.loads(
        (tmp_path / "evidence" / identity.entity_id / "failure.json").read_text(
            encoding="utf-8"
        )
    )
    assert failure["error_code"] == "pages_hash_mismatch"
