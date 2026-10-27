from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from credit_research_agent.annual_reports import (
    AnnualReportCandidate,
    AnnualReportDiscovery,
    AnnualReportDiscoveryError,
    CompanyOnboardingService,
    CninfoAnnualReportProvider,
    classify_annual_report_candidate,
    select_annual_reports,
    validate_annual_report_identity,
)
from credit_research_agent.documents import DownloadResult, ExtractionArtifacts
from credit_research_agent.models import CompanyIdentity, EntityScope, SourceRecord, SourceStatus, SourceType
from credit_research_agent.security_master import (
    DynamicCompanyResolution,
    SecurityMasterCacheStatus,
)
from credit_research_agent.sources import SourceCatalog


FIXED_NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


def make_identity(
    code: str = "600998",
    *,
    legal_name: str = "九州通医药集团股份有限公司",
    short_name: str = "九州通",
    exchange_code: str = "SSE",
    suffix: str = "SH",
) -> CompanyIdentity:
    return CompanyIdentity(
        entity_id=f"CN-A-{exchange_code}-{code}",
        legal_name=legal_name,
        short_name=short_name,
        ticker=f"{code}.{suffix}",
        security_code=code,
        exchange="上海证券交易所" if exchange_code == "SSE" else "深圳证券交易所",
        country_of_incorporation="中国",
        entity_scope=EntityScope.CONSOLIDATED,
        exchange_code=exchange_code,
        listing_board="沪市主板" if exchange_code == "SSE" else "创业板",
        listing_status="listed",
        external_org_id=f"ORG-{code}",
        identity_source_url="https://www.cninfo.com.cn/new/snapshot/companyListCn",
        resolved_at=FIXED_NOW,
    )


def make_candidate(
    identity: CompanyIdentity,
    announcement_id: str,
    year: int,
    *,
    title: str | None = None,
    published_at: date | None = None,
    is_revision: bool = False,
    is_summary: bool = False,
    identity_matches: bool = True,
    document_url: str | None = None,
    adjunct_type: str | None = "PDF",
) -> AnnualReportCandidate:
    actual_title = title or (
        f"{year}年年度报告（修订版）" if is_revision else f"{year}年年度报告"
    )
    url = document_url
    if url is None and adjunct_type == "PDF":
        url = f"https://static.cninfo.com.cn/finalpage/{year}/{announcement_id}.PDF"
    return AnnualReportCandidate(
        announcement_id=announcement_id,
        security_code=identity.security_code if identity_matches else "000001",
        short_name=identity.short_name,
        external_org_id=(identity.external_org_id or "") if identity_matches else "OTHER",
        title=actual_title,
        published_at=published_at or date(year + 1, 4, 30),
        report_year=year,
        document_url=url,
        adjunct_type=adjunct_type,
        is_full_report=not is_summary and adjunct_type == "PDF" and bool(url),
        is_summary=is_summary,
        is_revision=is_revision,
        identity_matches=identity_matches,
        raw_adjunct_url=(url or "").removeprefix("https://static.cninfo.com.cn/"),
    )


class FixtureAnnualReportProvider:
    def __init__(self, candidates_by_code) -> None:
        self.candidates_by_code = candidates_by_code
        self.calls: list[str] = []

    def discover(self, identity: CompanyIdentity):
        self.calls.append(identity.security_code)
        return list(self.candidates_by_code[identity.security_code])


class FakeAnnouncementResponse:
    status_code = 200
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

    def __init__(self, payload) -> None:
        self.payload = payload

    def json(self):
        return self.payload


class FakeAnnouncementSession:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeAnnouncementResponse(self.payload)


def test_cninfo_provider_builds_subject_bound_query_and_parses_response() -> None:
    identity = make_identity()
    session = FakeAnnouncementSession(
        {
            "announcements": [
                {
                    "announcementId": "1225212712",
                    "secCode": "600998",
                    "secName": "九州通",
                    "orgId": "ORG-600998",
                    "announcementTitle": "九州通2025年年度报告",
                    "announcementTime": 1777334400000,
                    "adjunctUrl": "finalpage/2026-04-28/1225212712.PDF",
                    "adjunctType": "PDF",
                }
            ],
            "hasMore": "false",
        }
    )
    provider = CninfoAnnualReportProvider(
        session=session,
        clock=lambda: FIXED_NOW,
    )

    candidates = provider.discover(identity)

    assert len(candidates) == 1
    assert candidates[0].announcement_id == "1225212712"
    assert candidates[0].identity_matches is True
    assert candidates[0].is_full_report is True
    assert len(session.calls) == 1
    request_data = session.calls[0][1]["data"]
    assert request_data["stock"] == "600998,ORG-600998"
    assert request_data["category"] == "category_ndbg_szsh"


def test_classification_does_not_promote_summary_to_full_report() -> None:
    identity = make_identity()
    item = {
        "announcementId": "123",
        "secCode": identity.security_code,
        "secName": identity.short_name,
        "orgId": identity.external_org_id,
        "announcementTitle": "2025年年度报告摘要",
        "announcementTime": 1777334400000,
        "adjunctUrl": "finalpage/2026-04-28/123.PDF",
        "adjunctType": "PDF",
    }

    candidate = classify_annual_report_candidate(item, identity)

    assert candidate.report_year == 2025
    assert candidate.is_summary is True
    assert candidate.is_full_report is False


@pytest.mark.parametrize(
    "title",
    [
        "九州通2025年年度报告",
        "九州通：2025年年度报告",
        "九州通2020年度报告",
        "九州通医药集团股份有限公司2025年年度报告（修订版）",
    ],
)
def test_classification_accepts_verified_company_prefix_and_legacy_year_title(
    title,
) -> None:
    identity = make_identity()
    item = {
        "announcementId": "full",
        "secCode": identity.security_code,
        "secName": identity.short_name,
        "orgId": identity.external_org_id,
        "announcementTitle": title,
        "announcementTime": 1777334400000,
        "adjunctUrl": "finalpage/2026-04-28/full.PDF",
        "adjunctType": "PDF",
    }

    candidate = classify_annual_report_candidate(item, identity)

    assert candidate.is_full_report is True
    assert candidate.report_year in {2020, 2025}


@pytest.mark.parametrize(
    "title",
    [
        "关于2025年年度报告的更正公告",
        "2025年年度报告审计报告",
        "关于2025年年度报告问询函的回复",
        "其他公司2025年年度报告",
    ],
)
def test_classification_rejects_related_notices_as_full_report(title) -> None:
    identity = make_identity()
    item = {
        "announcementId": "notice",
        "secCode": identity.security_code,
        "secName": identity.short_name,
        "orgId": identity.external_org_id,
        "announcementTitle": title,
        "announcementTime": 1777334400000,
        "adjunctUrl": "finalpage/2026-04-28/notice.PDF",
        "adjunctType": "PDF",
    }

    candidate = classify_annual_report_candidate(item, identity)

    assert candidate.is_full_report is False
    assert candidate.is_revision is False


def test_revision_is_selected_and_summary_entity_mismatch_and_non_pdf_are_excluded() -> None:
    identity = make_identity()
    original = make_candidate(identity, "100", 2025, published_at=date(2026, 4, 20))
    revision = make_candidate(
        identity,
        "101",
        2025,
        is_revision=True,
        published_at=date(2026, 4, 28),
    )
    summary = make_candidate(identity, "102", 2025, is_summary=True)
    mismatch = make_candidate(identity, "103", 2024, identity_matches=False)
    non_pdf = make_candidate(
        identity,
        "104",
        2023,
        document_url="https://static.cninfo.com.cn/finalpage/2024/report.html",
        adjunct_type="HTML",
    )

    selected, excluded = select_annual_reports(
        identity,
        [original, revision, summary, mismatch, non_pdf],
        report_count=1,
    )

    assert [item.announcement_id for item in selected] == ["101"]
    reasons = {item.announcement_id: item.reason for item in excluded}
    assert reasons["100"] == "superseded_same_period_version"
    assert reasons["102"] == "annual_report_summary"
    assert reasons["103"] == "entity_mismatch"
    assert reasons["104"] == "not_official_pdf"


def test_selection_rejects_pdf_from_non_official_host() -> None:
    identity = make_identity()
    candidate = make_candidate(
        identity,
        "external",
        2025,
        document_url="https://example.com/2025-annual-report.pdf",
    )

    with pytest.raises(AnnualReportDiscoveryError) as captured:
        select_annual_reports(identity, [candidate], report_count=1)

    assert captured.value.code == "annual_report_not_found"


def test_discovery_assigns_task_local_sources_and_keeps_companies_isolated(tmp_path) -> None:
    jiuzhoutong = make_identity()
    catl = make_identity(
        "300750",
        legal_name="宁德时代新能源科技股份有限公司",
        short_name="宁德时代",
        exchange_code="SZSE",
        suffix="SZ",
    )
    provider = FixtureAnnualReportProvider(
        {
            jiuzhoutong.security_code: [
                make_candidate(jiuzhoutong, "J25", 2025),
                make_candidate(jiuzhoutong, "J24", 2024),
            ],
            catl.security_code: [
                make_candidate(catl, "C25", 2025),
                make_candidate(catl, "C24", 2024),
            ],
        }
    )
    discovery = AnnualReportDiscovery(provider, clock=lambda: FIXED_NOW)

    first = discovery.discover(jiuzhoutong, tmp_path / jiuzhoutong.entity_id)
    second = discovery.discover(catl, tmp_path / catl.entity_id)

    assert [source.source_id for source in first.sources] == ["S01", "S02"]
    assert [source.source_id for source in second.sources] == ["S01", "S02"]
    assert {source.entity_id for source in first.sources} == {jiuzhoutong.entity_id}
    assert {source.entity_id for source in second.sources} == {catl.entity_id}
    assert first.source_catalog_path.parent != second.source_catalog_path.parent
    assert first.source_catalog_path.is_file()
    assert second.source_catalog_path.is_file()
    assert {
        source.entity_id
        for source in SourceCatalog.from_path(first.source_catalog_path).list_all()
    } == {jiuzhoutong.entity_id}
    assert {
        source.entity_id
        for source in SourceCatalog.from_path(second.source_catalog_path).list_all()
    } == {catl.entity_id}


def make_source(identity: CompanyIdentity, year: int = 2025) -> SourceRecord:
    return SourceRecord(
        source_id="S01",
        entity_id=identity.entity_id,
        title=f"{identity.legal_name}{year}年年度报告",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="巨潮资讯网（上市公司公告）",
        url=f"https://static.cninfo.com.cn/finalpage/{year + 1}/report.PDF",
        published_at=date(year + 1, 4, 30),
        report_period=f"FY{year}",
        trusted_tier=1,
        status=SourceStatus.REGISTERED,
    )


def write_pages(path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )


def test_pdf_identity_validation_requires_company_code_and_period(tmp_path) -> None:
    identity = make_identity()
    source = make_source(identity)
    pages_path = tmp_path / "pages.jsonl"
    write_pages(
        pages_path,
        [
            {
                "page_number": 1,
                "status": "extracted",
                "text": "九州通医药集团股份有限公司 证券代码：600998 2025年年度报告",
            }
        ],
    )

    result = validate_annual_report_identity(identity, source, pages_path)

    assert result.status == "verified"
    assert result.company_name_matched is True
    assert result.security_code_matched is True
    assert result.report_period_matched is True


def test_pdf_identity_validation_rejects_cross_company_document(tmp_path) -> None:
    identity = make_identity()
    source = make_source(identity)
    pages_path = tmp_path / "pages.jsonl"
    write_pages(
        pages_path,
        [
            {
                "page_number": 1,
                "status": "extracted",
                "text": "其他上市公司 证券代码：601138 2025年年度报告",
            }
        ],
    )

    with pytest.raises(AnnualReportDiscoveryError) as captured:
        validate_annual_report_identity(identity, source, pages_path)

    assert captured.value.code == "annual_report_identity_mismatch"


class FakeDownloader:
    def __init__(self) -> None:
        self.destinations = []

    def download(self, source, destination_dir, *, force=False):
        destination_dir.mkdir(parents=True, exist_ok=True)
        self.destinations.append(destination_dir)
        file_path = destination_dir / f"{source.source_id}.pdf"
        manifest_path = destination_dir / f"{source.source_id}.download.json"
        file_path.write_bytes(b"%PDF-1.4\nfixture")
        manifest_path.write_text("{}", encoding="utf-8")
        return DownloadResult(
            source_id=source.source_id,
            source_title=source.title,
            source_url=str(source.url),
            published_at=source.published_at,
            report_period=source.report_period,
            file_path=file_path,
            manifest_path=manifest_path,
            accessed_at=FIXED_NOW,
            final_url=str(source.url),
            http_status=200,
            content_type="application/pdf",
            bytes_written=file_path.stat().st_size,
            sha256="0" * 64,
        )


def make_fake_extractor(identity: CompanyIdentity):
    def extract(download, output_dir):
        source_dir = output_dir / download.source_id
        pages_path = source_dir / "pages.jsonl"
        summary_path = source_dir / "summary.json"
        sample_path = source_dir / "sample.json"
        year = (download.report_period or "").removeprefix("FY")
        write_pages(
            pages_path,
            [
                {
                    "page_number": 1,
                    "status": "extracted",
                    "text": (
                        f"{identity.legal_name} 证券代码：{identity.security_code} "
                        f"{year}年年度报告"
                    ),
                }
            ],
        )
        summary_path.write_text("{}", encoding="utf-8")
        sample_path.write_text("{}", encoding="utf-8")
        return ExtractionArtifacts(
            source_id=download.source_id,
            summary_path=summary_path,
            pages_path=pages_path,
            sample_path=sample_path,
            total_pages=1,
            pages_with_text=1,
            failed_pages=0,
        )

    return extract


def test_onboarding_download_and_parse_paths_are_scoped_by_entity(tmp_path) -> None:
    identity = make_identity()
    provider = FixtureAnnualReportProvider(
        {identity.security_code: [make_candidate(identity, "J25", 2025)]}
    )
    downloader = FakeDownloader()
    service = CompanyOnboardingService(
        AnnualReportDiscovery(provider, clock=lambda: FIXED_NOW),
        downloader=downloader,
        extractor=make_fake_extractor(identity),
    )
    resolution = DynamicCompanyResolution(
        identity=identity,
        resolution_source="official_security_master",
        cache_status=SecurityMasterCacheStatus.FRESH,
    )

    result = service.prepare(resolution, tmp_path, report_count=1)

    assert result.status == "success"
    assert result.output_dir == (tmp_path / identity.entity_id).resolve()
    assert downloader.destinations == [tmp_path / identity.entity_id / "downloads"]
    assert result.prepared_reports[0].download.file_path.parent == (
        tmp_path / identity.entity_id / "downloads"
    )
    assert result.prepared_reports[0].extraction.pages_path.parent == (
        tmp_path / identity.entity_id / "parsed" / "S01"
    )
