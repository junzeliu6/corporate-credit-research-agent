"""任务级来源与本地文档绑定的离线回归测试。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from credit_research_agent.documents import (
    DocumentBindingError,
    DownloadResult,
    validate_download_binding,
)
from credit_research_agent.models import SourceRecord, SourceStatus, SourceType


def _source(*, entity_id: str = "CN-A-SSE-600998") -> SourceRecord:
    return SourceRecord(
        source_id="S01",
        entity_id=entity_id,
        title="九州通医药集团股份有限公司2025年年度报告",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="上海证券交易所",
        url="https://static.cninfo.com.cn/finalpage/2026-04-28/jzt.PDF",
        published_at=date(2026, 4, 28),
        report_period="FY2025",
        trusted_tier=1,
        status=SourceStatus.DOWNLOADED,
    )


def _download(tmp_path: Path, source: SourceRecord) -> DownloadResult:
    pdf_path = tmp_path / "S01.pdf"
    pdf_path.write_bytes(b"%PDF-task-bound-fixture")
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    return DownloadResult(
        source_id=source.source_id,
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=source.report_period,
        file_path=pdf_path,
        manifest_path=tmp_path / "S01.metadata.json",
        accessed_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=pdf_path.stat().st_size,
        sha256=digest,
        from_cache=True,
    )


def test_download_binding_accepts_exact_task_source(tmp_path: Path) -> None:
    source = _source()
    validate_download_binding(source, _download(tmp_path, source))


def test_same_source_id_cannot_bind_another_company_document(tmp_path: Path) -> None:
    source = _source()
    download = _download(tmp_path, source).copy(
        update={
            "source_title": "石家庄以岭药业股份有限公司2025年年度报告",
            "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-28/yiling.PDF",
        }
    )

    with pytest.raises(DocumentBindingError) as exc_info:
        validate_download_binding(source, download)

    assert exc_info.value.code == "download_source_metadata_mismatch"


def test_download_binding_rejects_mutated_cached_pdf(tmp_path: Path) -> None:
    source = _source()
    download = _download(tmp_path, source)
    download.file_path.write_bytes(b"%PDF-replaced-by-another-company")

    with pytest.raises(DocumentBindingError) as exc_info:
        validate_download_binding(source, download)

    assert exc_info.value.code == "download_file_hash_mismatch"
