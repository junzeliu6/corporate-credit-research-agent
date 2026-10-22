import json
from collections import Counter, defaultdict
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
    load_default_seed_catalog,
    render_citations,
)
from credit_research_agent.fundamentals import (
    FundamentalsBuilder,
    build_fundamental_artifacts,
    load_fundamental_seed_catalog,
    merge_seed_catalogs,
)
from credit_research_agent.models import FactRecord, FundamentalResearchBundle
from credit_research_agent.sources import SourceCatalog


FIXED_TIME = datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)


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


def _write_reviewed_seed_pages(tmp_path, download, *, omit_evidence_id=None):
    merged = merge_seed_catalogs(
        load_default_seed_catalog(), load_fundamental_seed_catalog()
    )
    lines_by_page = defaultdict(list)
    for seed in merged.evidence:
        if seed.evidence_id != omit_evidence_id:
            lines_by_page[seed.page_number].append(seed.match_text)

    pages = []
    for page_number, lines in sorted(lines_by_page.items()):
        text = "\n".join(lines)
        pages.append(
            ParsedPage(
                page_number=page_number,
                text=text,
                char_count=len(text),
                status=PageExtractionStatus.EXTRACTED,
            )
        )

    pages_path = tmp_path / "pages.jsonl"
    pages_path.write_text(
        "".join(page.json(ensure_ascii=False) + "\n" for page in pages),
        encoding="utf-8",
    )
    summary = PdfExtractionSummary(
        source_id="S01",
        source_file=download.file_path,
        source_sha256=download.sha256,
        pages_sha256=calculate_sha256(pages_path),
        parser_name="pdfplumber",
        parser_version="0.11.10",
        parsed_at=FIXED_TIME,
        page_numbering="pdf_physical_page_1_based",
        total_pages=229,
        pages_with_text=len(pages),
        empty_pages=0,
        failed_pages=0,
        total_characters=sum(page.char_count for page in pages),
        text_coverage_ratio=len(pages) / 229,
        warnings=[],
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(summary.json(ensure_ascii=False), encoding="utf-8")
    return summary_path, pages_path


def test_builder_groups_reviewed_facts_and_keeps_statement_types_separate(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_reviewed_seed_pages(tmp_path, download)

    bundle = FundamentalsBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    assert bundle.schema_version == "1.1.0"
    assert len(bundle.facts) == 21
    assert len(bundle.evidence) == 19
    assert bundle.inferences == []
    assert [item.missing_id for item in bundle.missing_information] == [
        "M001",
        "M002",
        "M003",
    ]
    assert bundle.identity.evidence_ids == ["E001", "E002", "E003"]
    assert bundle.sources[0].page_or_section == "PDF第6、10、27、28、128页"

    counts = Counter(fact.research_module.value for fact in bundle.facts)
    assert counts == {
        "company_identity": 4,
        "development_history": 3,
        "main_business": 2,
        "operating_model": 8,
        "main_products": 4,
    }
    assert all(fact.statement_type.value == "disclosed_fact" for fact in bundle.facts)


def test_builder_handles_values_split_across_pdf_lines(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_reviewed_seed_pages(tmp_path, download)
    bundle = FundamentalsBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )

    facts = {item.fact_id: item for item in bundle.facts}
    assert facts["F005"].value == "2001 年由石家庄以岭药业有限公司整体变更设立"
    assert facts["F005"].evidence_ids == ["E004", "E005"]
    assert facts["F021"].value == "芪苈强心胶囊"
    assert facts["F021"].evidence_ids == ["E018", "E019"]


def test_fact_record_rejects_model_inference_disguised_as_fact() -> None:
    with pytest.raises(ValidationError, match="只保存年报披露事实"):
        FactRecord.parse_obj(
            {
                "fact_id": "F999",
                "entity_id": "CN-A-SZSE-002603",
                "category": "business",
                "research_module": "main_business",
                "statement_type": "model_inference",
                "field_name": "unsupported_summary",
                "value": "示例",
                "claim": "示例",
                "entity_scope": "consolidated",
                "evidence_ids": ["E999"],
            }
        )


def test_bundle_rejects_dangling_missing_information_reference(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_reviewed_seed_pages(tmp_path, download)
    bundle = FundamentalsBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )
    payload = json.loads(bundle.json())
    payload["missing_information"][0]["related_fact_ids"] = ["F999"]

    with pytest.raises(ValidationError, match="悬空引用"):
        FundamentalResearchBundle.parse_obj(payload)


def test_missing_reviewed_line_fails_instead_of_generating_fact(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_reviewed_seed_pages(
        tmp_path, download, omit_evidence_id="E019"
    )

    with pytest.raises(EvidenceBuildError) as captured:
        FundamentalsBuilder().build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )

    assert captured.value.code == "evidence_text_mismatch"


def test_artifact_writer_outputs_json_citations_and_counts(
    tmp_path, identity, source, download
) -> None:
    summary_path, pages_path = _write_reviewed_seed_pages(tmp_path, download)
    fundamentals_output = tmp_path / "examples" / "fundamentals.json"
    citations_output = tmp_path / "examples" / "citations.md"

    artifacts = build_fundamental_artifacts(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        output_dir=tmp_path / "fundamentals",
        fundamentals_output=fundamentals_output,
        citations_output=citations_output,
        clock=lambda: FIXED_TIME,
    )

    assert artifacts.fact_count == 21
    assert artifacts.evidence_count == 19
    assert artifacts.inference_count == 0
    assert artifacts.missing_information_count == 3
    assert artifacts.module_fact_counts["main_products"] == 4
    assert fundamentals_output.is_file()
    assert "PDF 第 6、10、27、28、128 页" in citations_output.read_text(
        encoding="utf-8"
    )

    bundle = FundamentalsBuilder(clock=lambda: FIXED_TIME).build(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )
    assert "[S01]" in render_citations(bundle)
