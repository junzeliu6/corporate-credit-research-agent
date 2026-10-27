"""V3.3A 官方年报发现、任务级来源登记与下载解析编排。"""

from __future__ import annotations

import html
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from pydantic.v1 import BaseModel, Field

from credit_research_agent.documents import DownloadResult, ExtractionArtifacts
from credit_research_agent.downloader import DownloadError, OfficialPdfDownloader
from credit_research_agent.models import CompanyIdentity, SourceRecord, SourceStatus, SourceType
from credit_research_agent.pdf_parser import PdfExtractionError, extract_to_artifacts
from credit_research_agent.security_master import DynamicCompanyResolution
from credit_research_agent.sources import SourceCatalog


CNINFO_ANNOUNCEMENT_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE_URL = "https://static.cninfo.com.cn/"


class AnnualReportDiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AnnualReportCandidate(BaseModel):
    announcement_id: str
    security_code: str
    short_name: str
    external_org_id: str
    title: str
    published_at: date
    report_year: Optional[int] = None
    document_url: Optional[str] = None
    adjunct_type: Optional[str] = None
    is_full_report: bool = False
    is_summary: bool = False
    is_revision: bool = False
    is_english: bool = False
    is_cancelled: bool = False
    identity_matches: bool = False
    raw_adjunct_url: Optional[str] = None


class ExcludedAnnualReport(BaseModel):
    announcement_id: str
    title: str
    reason: str


class AnnualReportDiscoveryResult(BaseModel):
    schema_version: str = "3.3a.0"
    identity: CompanyIdentity
    provider: str
    discovered_at: datetime
    candidates: List[AnnualReportCandidate]
    selected: List[AnnualReportCandidate]
    excluded: List[ExcludedAnnualReport]
    sources: List[SourceRecord]
    primary_source_id: str
    comparison_source_ids: List[str]
    source_catalog_path: Path
    discovery_path: Path
    cache_status: str
    limitations: List[str] = Field(default_factory=list)


class AnnualReportIdentityValidation(BaseModel):
    source_id: str
    checked_pages: int
    company_name_matched: bool
    security_code_matched: bool
    report_period_matched: bool
    status: str


class PreparedAnnualReport(BaseModel):
    source: SourceRecord
    download: DownloadResult
    extraction: ExtractionArtifacts
    identity_validation: AnnualReportIdentityValidation


class OnboardingDocumentFailure(BaseModel):
    source_id: str
    error_code: str
    error_message: str


class CompanyOnboardingResult(BaseModel):
    schema_version: str = "3.3a.0"
    status: str
    resolution: DynamicCompanyResolution
    discovery: AnnualReportDiscoveryResult
    prepared_reports: List[PreparedAnnualReport] = Field(default_factory=list)
    failures: List[OnboardingDocumentFailure] = Field(default_factory=list)
    output_dir: Path
    identity_path: Path
    manifest_path: Path
    limitations: List[str] = Field(default_factory=list)


class AnnualReportProvider(Protocol):
    def discover(self, identity: CompanyIdentity) -> List[AnnualReportCandidate]:
        ...


def _clean_title(value: object) -> str:
    without_tags = re.sub(r"<[^>]+>", "", html.unescape(str(value or "")))
    return "".join(without_tags.replace("\u3000", " ").split())


def _is_official_pdf_url(value: Optional[str]) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "static.cninfo.com.cn"
        and parsed.path.lower().endswith(".pdf")
    )


def classify_annual_report_candidate(
    item: Dict[str, object], identity: CompanyIdentity
) -> AnnualReportCandidate:
    """把巨潮公告元数据分类；摘要或附件不会被升级为年报全文。"""

    title = _clean_title(item.get("announcementTitle"))
    year_match = re.search(r"(?<!\d)(20\d{2})(?:年)?年度报告", title)
    report_year = int(year_match.group(1)) if year_match else None
    is_summary = "摘要" in title
    is_english = bool(re.search(r"英文版|ENGLISH", title, re.I))
    is_cancelled = any(marker in title for marker in ("取消", "撤回", "作废"))
    revision_suffix = re.search(
        r"[（(](?:修订版|修订稿|更正版|更新版)[）)]$", title
    )
    is_revision = revision_suffix is not None
    # 巨潮对部分上交所公告保留证券简称前缀（如“九州通2025年年度报告”）。
    # 只移除当前已核验主体的全称/简称，不允许任意文本前缀冒充主体。
    canonical_title = title
    identity_names = sorted(
        {
            _clean_title(identity.legal_name),
            _clean_title(identity.short_name),
        }
        - {""},
        key=len,
        reverse=True,
    )
    for name in identity_names:
        if canonical_title.startswith(name):
            canonical_title = canonical_title[len(name) :].lstrip("：:")
            break
    # 仅接受“年度报告/年度报告全文”及其明确修订版。诸如“关于年度报告的
    # 更正公告”“审计报告”“问询函回复”即使也是PDF，也不能冒充报告全文。
    full_title_pattern = re.compile(
        r"^20\d{2}(?:年)?年度报告(?:全文)?"
        r"(?:[（(](?:修订版|修订稿|更正版|更新版)[）)])?$"
    )
    disallowed_title_markers = (
        "更正公告",
        "提示性公告",
        "审计报告",
        "问询函",
        "回复",
        "董事会工作报告",
        "社会责任报告",
        "环境、社会及管治报告",
    )
    adjunct_type = str(item.get("adjunctType") or "").upper() or None
    raw_url = str(item.get("adjunctUrl") or "").strip() or None
    document_url: Optional[str] = None
    if raw_url:
        candidate_url = urljoin(CNINFO_STATIC_BASE_URL, raw_url)
        if _is_official_pdf_url(candidate_url):
            document_url = candidate_url
    identity_matches = (
        str(item.get("secCode") or "").strip() == identity.security_code
        and str(item.get("orgId") or "").strip() == str(identity.external_org_id or "")
    )
    is_full_report = bool(
        report_year
        and full_title_pattern.fullmatch(canonical_title)
        and not any(marker in title for marker in disallowed_title_markers)
        and not is_summary
        and not is_english
        and not is_cancelled
        and document_url
        and adjunct_type == "PDF"
    )
    timestamp_ms = item.get("announcementTime")
    try:
        published_at = datetime.fromtimestamp(
            int(timestamp_ms) / 1000, ZoneInfo("Asia/Shanghai")
        ).date()
    except (TypeError, ValueError, OSError) as exc:
        raise AnnualReportDiscoveryError(
            "invalid_announcement_time", f"公告 {item.get('announcementId')} 日期无效"
        ) from exc
    return AnnualReportCandidate(
        announcement_id=str(item.get("announcementId") or "").strip(),
        security_code=str(item.get("secCode") or "").strip(),
        short_name=str(item.get("secName") or "").strip(),
        external_org_id=str(item.get("orgId") or "").strip(),
        title=title,
        published_at=published_at,
        report_year=report_year,
        document_url=document_url,
        adjunct_type=adjunct_type,
        is_full_report=is_full_report,
        is_summary=is_summary,
        is_revision=is_revision,
        is_english=is_english,
        is_cancelled=is_cancelled,
        identity_matches=identity_matches,
        raw_adjunct_url=raw_url,
    )


class CninfoAnnualReportProvider:
    """使用巨潮公告查询页面的数据源发现年报，不猜测静态PDF URL。"""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20.0,
        lookback_years: int = 5,
        max_pages: int = 3,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if timeout_seconds <= 0 or lookback_years < 1 or max_pages < 1:
            raise ValueError("年报发现配置必须为正数")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.lookback_years = lookback_years
        self.max_pages = max_pages
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def discover(self, identity: CompanyIdentity) -> List[AnnualReportCandidate]:
        if not identity.external_org_id:
            raise AnnualReportDiscoveryError(
                "missing_external_org_id", "企业身份缺少巨潮orgId，不能可靠查询年报"
            )
        exchange_params = {
            "SZSE": ("szse", "sz"),
            "SSE": ("sse", "sh"),
            "BSE": ("bj", "bj"),
        }
        try:
            column, plate = exchange_params[str(identity.exchange_code)]
        except KeyError as exc:
            raise AnnualReportDiscoveryError(
                "unsupported_exchange", f"暂不支持交易所：{identity.exchange_code}"
            ) from exc
        current_year = self._clock().year
        date_window = f"{current_year - self.lookback_years}-01-01~{current_year}-12-31"
        candidates: List[AnnualReportCandidate] = []
        seen_ids: set[str] = set()
        for page_number in range(1, self.max_pages + 1):
            data = {
                "pageNum": str(page_number),
                "pageSize": "30",
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": f"{identity.security_code},{identity.external_org_id}",
                "searchkey": "",
                "secid": "",
                "category": "category_ndbg_szsh",
                "trade": "",
                "seDate": date_window,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            try:
                response = self.session.post(
                    CNINFO_ANNOUNCEMENT_QUERY_URL,
                    data=data,
                    headers={
                        "Accept": "application/json,text/plain,*/*",
                        "Origin": "https://www.cninfo.com.cn",
                        "Referer": "https://www.cninfo.com.cn/",
                        "User-Agent": "CorporateCreditResearchAgent/3.3 (public-disclosure-research)",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise AnnualReportDiscoveryError(
                    "annual_report_discovery_network_error", str(exc)
                ) from exc
            status_code = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status_code < 300:
                raise AnnualReportDiscoveryError(
                    "annual_report_discovery_http_error",
                    f"巨潮公告查询返回 HTTP {status_code}",
                )
            final_host = (urlparse(str(getattr(response, "url", ""))).hostname or "").lower()
            if final_host and not (
                final_host == "cninfo.com.cn" or final_host.endswith(".cninfo.com.cn")
            ):
                raise AnnualReportDiscoveryError(
                    "untrusted_redirect", f"巨潮公告查询重定向至非官方域名：{final_host}"
                )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise AnnualReportDiscoveryError(
                    "invalid_announcement_response", "巨潮公告查询未返回有效JSON"
                ) from exc
            rows = payload.get("announcements") if isinstance(payload, dict) else None
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise AnnualReportDiscoveryError(
                    "invalid_announcement_response", "巨潮公告查询字段 announcements 无效"
                )
            for item in rows:
                if not isinstance(item, dict):
                    continue
                candidate = classify_annual_report_candidate(item, identity)
                if not candidate.announcement_id or candidate.announcement_id in seen_ids:
                    continue
                seen_ids.add(candidate.announcement_id)
                candidates.append(candidate)
            has_more = payload.get("hasMore")
            if has_more not in (True, 1, "1", "true", "True"):
                break
        return candidates


def select_annual_reports(
    identity: CompanyIdentity,
    candidates: List[AnnualReportCandidate],
    *,
    report_count: int = 2,
) -> tuple[List[AnnualReportCandidate], List[ExcludedAnnualReport]]:
    """确定性选择各年度全文；同年度明确修订版优先，摘要永不替代全文。"""

    if report_count < 1 or report_count > 9:
        raise ValueError("report_count 必须在1—9之间")
    valid_by_year: Dict[int, List[AnnualReportCandidate]] = {}
    excluded: List[ExcludedAnnualReport] = []
    for candidate in candidates:
        reason: Optional[str] = None
        if not candidate.identity_matches:
            reason = "entity_mismatch"
        elif candidate.report_year is None:
            reason = "not_annual_report"
        elif candidate.is_summary:
            reason = "annual_report_summary"
        elif candidate.is_english:
            reason = "english_version"
        elif candidate.is_cancelled:
            reason = "cancelled_or_withdrawn"
        elif (
            not _is_official_pdf_url(candidate.document_url)
            or candidate.adjunct_type != "PDF"
        ):
            reason = "not_official_pdf"
        elif not candidate.is_full_report:
            reason = "not_full_annual_report"
        if reason:
            excluded.append(
                ExcludedAnnualReport(
                    announcement_id=candidate.announcement_id,
                    title=candidate.title,
                    reason=reason,
                )
            )
            continue
        assert candidate.report_year is not None
        valid_by_year.setdefault(candidate.report_year, []).append(candidate)

    selected_by_year: List[AnnualReportCandidate] = []
    for year in sorted(valid_by_year, reverse=True):
        versions = sorted(
            valid_by_year[year],
            key=lambda item: (
                item.is_revision,
                item.published_at,
                int(item.announcement_id) if item.announcement_id.isdigit() else 0,
            ),
            reverse=True,
        )
        selected_by_year.append(versions[0])
        for item in versions[1:]:
            excluded.append(
                ExcludedAnnualReport(
                    announcement_id=item.announcement_id,
                    title=item.title,
                    reason="superseded_same_period_version",
                )
            )
    selected = selected_by_year[:report_count]
    for item in selected_by_year[report_count:]:
        excluded.append(
            ExcludedAnnualReport(
                announcement_id=item.announcement_id,
                title=item.title,
                reason="outside_requested_comparison_window",
            )
        )
    if not selected:
        raise AnnualReportDiscoveryError(
            "annual_report_not_found",
            f"未找到 {identity.ticker} 可核验的年度报告全文；摘要不会替代全文",
        )
    return selected, excluded


class AnnualReportDiscovery:
    def __init__(
        self,
        provider: AnnualReportProvider,
        *,
        cache_max_age: timedelta = timedelta(days=1),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.provider = provider
        self.cache_max_age = cache_max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def discover(
        self,
        identity: CompanyIdentity,
        output_dir: Path,
        *,
        report_count: int = 2,
        force_refresh: bool = False,
    ) -> AnnualReportDiscoveryResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        discovery_path = output_dir / "annual_report_discovery.json"
        cached = self._read_cache(discovery_path, identity, report_count)
        if cached is not None and not force_refresh and self._is_fresh(cached):
            return cached.copy(update={"cache_status": "fresh"})
        try:
            candidates = self.provider.discover(identity)
        except AnnualReportDiscoveryError:
            if cached is None:
                raise
            limitations = list(cached.limitations)
            limitations.append("官方公告查询刷新失败，本次显式使用过期发现缓存")
            return cached.copy(update={"cache_status": "stale", "limitations": limitations})
        selected, excluded = select_annual_reports(
            identity, candidates, report_count=report_count
        )
        sources = [
            SourceRecord(
                source_id=f"S{index:02d}",
                entity_id=identity.entity_id,
                title=f"{identity.legal_name}{item.report_year}年年度报告",
                source_type=SourceType.ANNUAL_REPORT,
                publisher="巨潮资讯网（上市公司公告）",
                url=item.document_url,
                published_at=item.published_at,
                report_period=f"FY{item.report_year}",
                trusted_tier=1,
                status=SourceStatus.REGISTERED,
                notes=(
                    f"动态发现；公告ID={item.announcement_id}；原始标题={item.title}；"
                    f"revision={str(item.is_revision).lower()}"
                ),
            )
            for index, item in enumerate(selected, start=1)
        ]
        source_catalog_path = SourceCatalog(sources).write(output_dir / "sources.json")
        now = self._clock()
        result = AnnualReportDiscoveryResult(
            identity=identity,
            provider="cninfo_official_disclosure",
            discovered_at=now,
            candidates=candidates,
            selected=selected,
            excluded=excluded,
            sources=sources,
            primary_source_id=sources[0].source_id,
            comparison_source_ids=[item.source_id for item in sources[1:]],
            source_catalog_path=source_catalog_path,
            discovery_path=discovery_path.resolve(),
            cache_status="refreshed",
            limitations=[
                "巨潮网页内部查询没有公开、版本化API契约；已保存任务级缓存，接口失败时不得猜测URL。"
            ],
        )
        _atomic_write_json(discovery_path, result.dict())
        return result

    def _read_cache(
        self, path: Path, identity: CompanyIdentity, report_count: int
    ) -> Optional[AnnualReportDiscoveryResult]:
        if not path.is_file():
            return None
        try:
            cached = AnnualReportDiscoveryResult.parse_raw(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if cached.identity.entity_id != identity.entity_id:
            return None
        # 每次任务的 S01/S02 编号只在当前选择窗口内有意义。请求数量改变时
        # 必须重新选择并重写任务级目录，不能把旧缓存中多余年度静默带入。
        if len(cached.sources) != report_count:
            return None
        if not Path(cached.source_catalog_path).is_file():
            return None
        return cached

    def _is_fresh(self, result: AnnualReportDiscoveryResult) -> bool:
        timestamp = result.discovered_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = self._clock() - timestamp
        return timedelta(0) <= age <= self.cache_max_age


def validate_annual_report_identity(
    identity: CompanyIdentity,
    source: SourceRecord,
    pages_path: Path,
    *,
    max_pages: int = 20,
) -> AnnualReportIdentityValidation:
    """从PDF前若干页复核主体、代码和报告年度，防止跨企业缓存污染。"""

    compact_parts: List[str] = []
    checked_pages = 0
    try:
        with Path(pages_path).open("r", encoding="utf-8") as handle:
            for line in handle:
                payload = json.loads(line)
                page_number = int(payload.get("page_number") or 0)
                if page_number > max_pages:
                    break
                if payload.get("status") != "extracted":
                    continue
                checked_pages += 1
                compact_parts.append("".join(str(payload.get("text") or "").split()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AnnualReportDiscoveryError(
            "annual_report_validation_failed",
            f"来源 {source.source_id} 的逐页文本无法用于主体复核：{exc}",
        ) from exc
    compact = "".join(compact_parts)
    company_name_matched = any(
        "".join(name.split()) in compact
        for name in (identity.legal_name, identity.short_name)
        if name
    )
    security_code_matched = identity.security_code in compact
    year = (source.report_period or "").removeprefix("FY")
    report_period_matched = bool(
        year
        and (
            f"{year}年年度报告" in compact
            or f"{year}年度报告" in compact
        )
    )
    status = (
        "verified"
        if company_name_matched and security_code_matched and report_period_matched
        else "mismatch"
    )
    result = AnnualReportIdentityValidation(
        source_id=source.source_id,
        checked_pages=checked_pages,
        company_name_matched=company_name_matched,
        security_code_matched=security_code_matched,
        report_period_matched=report_period_matched,
        status=status,
    )
    if status != "verified":
        missing = [
            name
            for ok, name in (
                (company_name_matched, "公司名称"),
                (security_code_matched, "证券代码"),
                (report_period_matched, "报告年度"),
            )
            if not ok
        ]
        raise AnnualReportDiscoveryError(
            "annual_report_identity_mismatch",
            f"来源 {source.source_id} 前{max_pages}页未同时核实：{'、'.join(missing)}",
        )
    return result


class CompanyOnboardingService:
    """把动态主体、年报来源、下载与PDF解析编排为可复用的V3.3A入口。"""

    def __init__(
        self,
        discovery: AnnualReportDiscovery,
        *,
        downloader: Optional[OfficialPdfDownloader] = None,
        extractor: Callable[..., ExtractionArtifacts] = extract_to_artifacts,
    ) -> None:
        self.discovery = discovery
        self.downloader = downloader or OfficialPdfDownloader()
        self.extractor = extractor

    def prepare(
        self,
        resolution: DynamicCompanyResolution,
        output_root: Path,
        *,
        report_count: int = 2,
        force_refresh: bool = False,
        discover_only: bool = False,
    ) -> CompanyOnboardingResult:
        identity = resolution.identity
        if not identity.external_org_id:
            raise AnnualReportDiscoveryError(
                "missing_external_org_id", "当前身份没有官方orgId，不能自动发现年报"
            )
        entity_dir = Path(output_root) / identity.entity_id
        entity_dir.mkdir(parents=True, exist_ok=True)
        identity_path = entity_dir / "company_identity.json"
        _atomic_write_json(identity_path, identity.dict())
        discovery = self.discovery.discover(
            identity,
            entity_dir,
            report_count=report_count,
            force_refresh=force_refresh,
        )
        prepared: List[PreparedAnnualReport] = []
        failures: List[OnboardingDocumentFailure] = []
        if not discover_only:
            for source in discovery.sources:
                try:
                    download = self.downloader.download(
                        source,
                        entity_dir / "downloads",
                        force=force_refresh,
                    )
                    extraction = self.extractor(
                        download,
                        entity_dir / "parsed",
                    )
                    validation = validate_annual_report_identity(
                        identity, source, extraction.pages_path
                    )
                    prepared.append(
                        PreparedAnnualReport(
                            source=source,
                            download=download,
                            extraction=extraction,
                            identity_validation=validation,
                        )
                    )
                except (DownloadError, PdfExtractionError, AnnualReportDiscoveryError) as exc:
                    failures.append(
                        OnboardingDocumentFailure(
                            source_id=source.source_id,
                            error_code=getattr(exc, "code", "annual_report_prepare_failed"),
                            error_message=str(exc),
                        )
                    )
        if discover_only:
            status = "discovered"
        elif prepared and not failures:
            status = "success"
        elif any(
            item.source.source_id == discovery.primary_source_id for item in prepared
        ):
            status = "partial_success"
        else:
            status = "failed"
        limitations = list(discovery.limitations)
        if failures:
            limitations.append("部分年报下载、解析或主体复核失败；已保留其他有效产物。")
        manifest_path = entity_dir / "onboarding.json"
        result = CompanyOnboardingResult(
            status=status,
            resolution=resolution,
            discovery=discovery,
            prepared_reports=prepared,
            failures=failures,
            output_dir=entity_dir.resolve(),
            identity_path=identity_path.resolve(),
            manifest_path=manifest_path.resolve(),
            limitations=limitations,
        )
        _atomic_write_json(manifest_path, result.dict())
        return result


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        temp_path.replace(path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

