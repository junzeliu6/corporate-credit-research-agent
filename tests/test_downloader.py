import json
import hashlib
from datetime import datetime, timezone

import pytest
import requests

from credit_research_agent.downloader import (
    DownloadConfig,
    DownloadError,
    OfficialPdfDownloader,
)
from credit_research_agent.sources import SourceCatalog


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        url: str = "https://static.cninfo.com.cn/report.pdf",
        content_type: str = "application/pdf",
        content_length: int | None = None,
    ) -> None:
        self._content = content
        self.status_code = status_code
        self.url = url
        self.closed = False
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, actions) -> None:
        self.actions = list(actions)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture
def annual_report_source():
    return SourceCatalog.from_default_catalog().get("S01")


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 10, 21, 8, 30, tzinfo=timezone.utc)


def test_download_writes_pdf_and_auditable_metadata(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    pdf = b"%PDF-1.7\n" + b"public annual report"
    response = FakeResponse(
        pdf,
        url="https://static.cninfo.com.cn/finalpage/report.PDF",
        content_length=len(pdf),
    )
    session = FakeSession([response])
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    result = downloader.download(annual_report_source, tmp_path)

    assert result.file_path.read_bytes() == pdf
    assert result.bytes_written == len(pdf)
    assert result.sha256 == hashlib.sha256(pdf).hexdigest()
    assert result.from_cache is False
    assert response.closed is True
    metadata = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert metadata["source_id"] == "S01"
    assert metadata["source_title"] == annual_report_source.title
    assert metadata["source_url"] == str(annual_report_source.url)
    assert metadata["published_at"] == "2026-04-28"
    assert metadata["accessed_at"] == "2026-10-21T08:30:00+00:00"


def test_second_download_uses_hash_verified_cache(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    response = FakeResponse(b"%PDF-1.4\ncached")
    session = FakeSession([response])
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    first = downloader.download(annual_report_source, tmp_path)
    second = downloader.download(annual_report_source, tmp_path)

    assert first.from_cache is False
    assert second.from_cache is True
    assert len(session.calls) == 1


def test_corrupted_cache_is_not_reused(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    first_response = FakeResponse(b"%PDF-1.4\nfirst")
    second_response = FakeResponse(b"%PDF-1.4\nrecovered")
    session = FakeSession([first_response, second_response])
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    first = downloader.download(annual_report_source, tmp_path)
    first.file_path.write_bytes(b"corrupted")
    recovered = downloader.download(annual_report_source, tmp_path)

    assert recovered.from_cache is False
    assert recovered.file_path.read_bytes() == b"%PDF-1.4\nrecovered"
    assert len(session.calls) == 2


def test_invalid_pdf_is_rejected_and_failure_is_recorded(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    session = FakeSession(
        [FakeResponse(b"<html>access denied</html>", content_type="text/html")]
    )
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    with pytest.raises(DownloadError) as captured:
        downloader.download(annual_report_source, tmp_path)

    assert captured.value.code == "invalid_pdf"
    assert not (tmp_path / "S01.pdf").exists()
    failure = json.loads((tmp_path / "S01.failure.json").read_text(encoding="utf-8"))
    assert failure["error_code"] == "invalid_pdf"
    assert failure["attempts"] == 1


def test_non_retryable_http_error_stops_after_one_attempt(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    session = FakeSession([FakeResponse(b"not found", status_code=404)])
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    with pytest.raises(DownloadError, match="HTTP 404") as captured:
        downloader.download(annual_report_source, tmp_path)

    assert captured.value.code == "http_error"
    assert len(session.calls) == 1


def test_transient_network_error_is_retried(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    session = FakeSession(
        [requests.ConnectionError("temporary"), FakeResponse(b"%PDF-1.7\nretry ok")]
    )
    downloader = OfficialPdfDownloader(
        session=session,
        clock=fixed_clock,
        sleep=lambda _: None,
    )

    result = downloader.download(annual_report_source, tmp_path)

    assert result.http_status == 200
    assert len(session.calls) == 2


def test_declared_file_over_limit_is_rejected(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    session = FakeSession(
        [FakeResponse(b"%PDF-1.7\nlarge", content_length=101)]
    )
    downloader = OfficialPdfDownloader(
        session=session,
        clock=fixed_clock,
        config=DownloadConfig(max_bytes=100),
    )

    with pytest.raises(DownloadError) as captured:
        downloader.download(annual_report_source, tmp_path)

    assert captured.value.code == "file_too_large"


def test_redirect_to_untrusted_domain_is_rejected(
    tmp_path, annual_report_source, fixed_clock
) -> None:
    session = FakeSession(
        [FakeResponse(b"%PDF-1.7\nunsafe", url="https://example.com/report.pdf")]
    )
    downloader = OfficialPdfDownloader(session=session, clock=fixed_clock)

    with pytest.raises(DownloadError) as captured:
        downloader.download(annual_report_source, tmp_path)

    assert captured.value.code == "untrusted_domain"
