import hashlib
import json
from datetime import datetime, timezone

import pytest

from credit_research_agent.documents import (
    DownloadResult,
    ExtractionArtifacts,
    PageExtractionStatus,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.pdf_parser import (
    ExtractionArtifactWriter,
    PdfExtractionError,
    PdfTextExtractor,
    build_summary,
    extract_to_artifacts,
    locate_terms,
)


FIXED_TIME = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


class FakePage:
    def __init__(self, text=None, error=None) -> None:
        self.text = text
        self.error = error

    def extract_text(self, **kwargs):
        assert kwargs == {"x_tolerance": 2, "y_tolerance": 3}
        if self.error:
            raise self.error
        return self.text


class FakePdf:
    def __init__(self, pages) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@pytest.fixture
def download(tmp_path) -> DownloadResult:
    pdf_path = tmp_path / "S01.pdf"
    content = b"%PDF-1.7\ntest fixture"
    pdf_path.write_bytes(content)
    return DownloadResult(
        source_id="S01",
        source_title="测试年度报告",
        source_url="https://static.cninfo.com.cn/test.pdf",
        published_at=None,
        report_period="FY2025",
        file_path=pdf_path,
        manifest_path=tmp_path / "S01.metadata.json",
        accessed_at=FIXED_TIME,
        final_url="https://static.cninfo.com.cn/test.pdf",
        http_status=200,
        content_type="application/pdf",
        bytes_written=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_parser_preserves_one_based_pages_and_page_failures(download) -> None:
    fake_pdf = FakePdf(
        [
            FakePage("封面\n公司信息"),
            FakePage("   \n"),
            FakePage(error=ValueError("bad page")),
        ]
    )
    extractor = PdfTextExtractor(
        open_pdf=lambda _: fake_pdf,
        clock=lambda: FIXED_TIME,
    )

    document = extractor.parse(download)

    assert document.total_pages == 3
    assert [page.page_number for page in document.pages] == [1, 2, 3]
    assert document.pages[0].status is PageExtractionStatus.EXTRACTED
    assert document.pages[1].status is PageExtractionStatus.EMPTY
    assert document.pages[2].status is PageExtractionStatus.FAILED
    assert document.pages[2].error_message == "bad page"


def test_parser_rejects_file_when_hash_differs_from_manifest(download) -> None:
    download.file_path.write_bytes(b"%PDF-corrupted")
    extractor = PdfTextExtractor(open_pdf=lambda _: FakePdf([]))

    with pytest.raises(PdfExtractionError) as captured:
        extractor.parse(download)

    assert captured.value.code == "source_hash_mismatch"


def test_parser_fails_explicitly_when_document_has_no_text(download) -> None:
    extractor = PdfTextExtractor(
        open_pdf=lambda _: FakePdf([FakePage(None), FakePage("  \n")]),
        clock=lambda: FIXED_TIME,
    )

    with pytest.raises(PdfExtractionError) as captured:
        extractor.parse(download)

    assert captured.value.code == "no_text_extracted"
    assert "OCR" in str(captured.value)


def test_summary_and_term_locations_are_explicit(download) -> None:
    fake_pdf = FakePdf(
        [
            FakePage("第一节\n一、公司信息\n其他内容"),
            FakePage("1、合并资产负债表\n项目"),
            FakePage(None),
        ]
    )
    document = PdfTextExtractor(
        open_pdf=lambda _: fake_pdf,
        clock=lambda: FIXED_TIME,
    ).parse(download)

    summary = build_summary(document)
    sample = locate_terms(document, ["公司信息", "合并资产负债表", "不存在"])

    assert summary.pages_with_text == 2
    assert summary.empty_pages == 1
    assert summary.failed_pages == 0
    assert summary.text_coverage_ratio == pytest.approx(2 / 3)
    assert [(item.query, item.page_number, item.line_number) for item in sample.matches] == [
        ("公司信息", 1, 2),
        ("合并资产负债表", 2, 1),
    ]
    assert sample.missing_queries == ["不存在"]


def test_writer_creates_summary_pages_and_lightweight_sample(
    tmp_path, download
) -> None:
    document = PdfTextExtractor(
        open_pdf=lambda _: FakePdf([FakePage("一、公司信息")]),
        clock=lambda: FIXED_TIME,
    ).parse(download)
    sample_output = tmp_path / "examples" / "sample.json"

    artifacts = ExtractionArtifactWriter().write(
        document,
        tmp_path / "parsed",
        queries=["公司信息"],
        sample_output=sample_output,
    )

    assert artifacts.summary_path.is_file()
    assert artifacts.pages_path.is_file()
    assert artifacts.sample_path == sample_output.resolve()
    summary = json.loads(artifacts.summary_path.read_text(encoding="utf-8"))
    pages = artifacts.pages_path.read_text(encoding="utf-8").splitlines()
    sample = json.loads(sample_output.read_text(encoding="utf-8"))
    assert summary["total_pages"] == 1
    assert summary["pages_sha256"] == calculate_sha256(artifacts.pages_path)
    assert len(pages) == 1
    assert sample["matches"][0]["page_number"] == 1


def test_orchestrator_records_hash_validation_failure(tmp_path, download) -> None:
    download.file_path.write_bytes(b"changed")

    with pytest.raises(PdfExtractionError) as captured:
        extract_to_artifacts(
            download,
            tmp_path / "parsed",
            clock=lambda: FIXED_TIME,
        )

    assert captured.value.code == "source_hash_mismatch"
    failure_path = tmp_path / "parsed" / "S01" / "failure.json"
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure["error_code"] == "source_hash_mismatch"
    assert failure["attempted_at"] == "2026-10-21T09:00:00+00:00"
