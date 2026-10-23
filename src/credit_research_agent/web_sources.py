"""V1.5 网页/PDF 原文获取、动态来源登记与证据提取。

外部页面始终是不可信输入。本模块只把原文保存为数据，不执行页面内指令。成功
获取的HTML/PDF原文可形成verified或single_source事实；无法获取原文时，搜索摘要
只以unverified候选EvidenceRecord保存，不能升级为确定性企业事实。
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import requests
from pydantic.v1 import BaseModel, Field, root_validator

from credit_research_agent.documents import (
    DownloadResult,
    PageExtractionStatus,
    ParsedDocument,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
)
from credit_research_agent.pdf_parser import PdfExtractionError, PdfTextExtractor
from credit_research_agent.search import SearchBudget, SearchError, SearchResult, canonicalize_url
from credit_research_agent.sources import SourceCatalog


class SourceFetchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: float = 15.0
    max_bytes: int = 10 * 1024 * 1024
    max_redirects: int = 3
    max_content_characters: int = 40_000
    user_agent: str = "CorporateCreditResearchAgent/1.5 (public-web-research)"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于0")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes 必须大于0")
        if self.max_redirects < 0 or self.max_redirects > 10:
            raise ValueError("max_redirects 必须在0—10之间")
        if self.max_content_characters < 1_000:
            raise ValueError("max_content_characters 至少为1000")


class FetchedSegment(BaseModel):
    segment_id: str
    page_number: int = Field(ge=1)
    line_number: int = Field(ge=1)
    locator: str
    text: str = Field(min_length=1)


class FetchedSource(BaseModel):
    schema_version: str = "1.5.0"
    source_handle: str
    source_id: str
    title: str
    original_url: str
    final_url: str
    source_domain: str
    accessed_at: datetime
    content_type: str
    cache_path: Path
    sha256: str
    bytes_written: int = Field(ge=0)
    from_cache: bool = False
    parser_name: str
    content_truncated: bool = False
    prompt_injection_detected: bool = False
    segments: List[FetchedSegment]

    @root_validator
    def require_content(cls, values):
        if not values.get("segments"):
            raise ValueError("获取的来源不包含可定位正文")
        return values


class WebEvidenceCandidate(BaseModel):
    candidate_id: str
    entity_id: str
    source_id: str
    evidence_id: str
    research_question: str
    excerpt: str
    verification_status: EvidenceStatus
    reason: str


class WebEvidenceConflict(BaseModel):
    conflict_id: str
    entity_id: str
    field_name: str
    period: Optional[str] = None
    source_ids: List[str]
    values: List[str]
    status: EvidenceStatus = EvidenceStatus.CONFLICTING


class WebEvidenceExtraction(BaseModel):
    schema_version: str = "1.5.0"
    identity: CompanyIdentity
    research_question: str
    generated_at: datetime
    facts: List[FactRecord] = Field(default_factory=list)
    evidence: List[EvidenceRecord] = Field(default_factory=list)
    candidates: List[WebEvidenceCandidate] = Field(default_factory=list)
    conflicts: List[WebEvidenceConflict] = Field(default_factory=list)
    sources: List[SourceRecord] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class URLSafetyValidator:
    """拒绝本机、内网、保留地址和云元数据地址。"""

    BLOCKED_HOSTS = {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data",
    }

    def __init__(
        self,
        *,
        resolver: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.resolver = resolver or socket.getaddrinfo

    def validate(self, url: str) -> str:
        raw_parsed = urlsplit(url.strip())
        if raw_parsed.username or raw_parsed.password:
            raise SourceFetchError("unsafe_url", "来源 URL 不得包含认证信息")
        canonical = canonicalize_url(url)
        parsed = urlsplit(canonical)
        if parsed.scheme.lower() not in ("http", "https"):
            raise SourceFetchError("unsafe_url", "仅允许 HTTP/HTTPS 来源")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or host in self.BLOCKED_HOSTS or host.endswith(".local"):
            raise SourceFetchError("unsafe_host", f"禁止访问目标主机：{host or '[empty]'}")
        if parsed.port not in (None, 80, 443):
            raise SourceFetchError("unsafe_port", "仅允许标准 HTTP/HTTPS 端口")

        literal_ip: Optional[ipaddress._BaseAddress]
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        addresses: List[ipaddress._BaseAddress] = []
        if literal_ip is not None:
            addresses.append(literal_ip)
        else:
            try:
                resolved = self.resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80))
            except OSError as exc:
                raise SourceFetchError(
                    "dns_resolution_failed", f"无法解析来源域名：{host}", retryable=True
                ) from exc
            for item in resolved:
                raw_address = item[4][0]
                try:
                    addresses.append(ipaddress.ip_address(raw_address))
                except ValueError:
                    continue
        if not addresses:
            raise SourceFetchError("dns_resolution_failed", f"域名没有可用地址：{host}")
        for address in addresses:
            if not address.is_global:
                raise SourceFetchError(
                    "blocked_network_target",
                    f"禁止访问非公网地址：{host}",
                )
        return canonical


class _ReadableHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "article",
        "section",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._current: List[str] = []
        self.paragraphs: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK_TAGS:
            self._flush()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._in_title:
            self.title_parts.append(normalized)
            return
        self._current.append(normalized)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        value = " ".join(self._current).strip()
        self._current.clear()
        if len(value) >= 8 and (not self.paragraphs or self.paragraphs[-1] != value):
            self.paragraphs.append(value)


PROMPT_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all instructions",
    "system prompt",
    "developer message",
    "忽略此前指令",
    "忽略上述指令",
    "忽略系统提示",
    "调用工具",
    "执行shell",
    "执行 shell",
)

RESTRICTED_PAGE_URL_MARKERS = (
    "/captcha/",
    "/challenge/",
    "/verify/",
    "wappass.baidu.com",
)

RESTRICTED_PAGE_TEXT_MARKERS = (
    "百度安全验证",
    "请输入验证码",
    "完成验证后继续",
    "网络不给力，请稍后重试",
    "访问过于频繁",
    "access denied",
    "verify you are human",
    "checking your browser",
    "captcha",
    "登录后继续",
)


def detect_restricted_page(
    *, title: str, final_url: str, paragraphs: Sequence[str]
) -> Optional[str]:
    """识别验证码、登录墙和反爬占位页，不尝试绕过访问控制。"""

    normalized_url = final_url.lower()
    sample = "\n".join([title, *paragraphs[:8]]).lower()
    if any(marker in normalized_url for marker in RESTRICTED_PAGE_URL_MARKERS):
        return "目标页面重定向至验证码或访问验证地址"
    if any(marker in sample for marker in RESTRICTED_PAGE_TEXT_MARKERS):
        return "目标页面返回验证码、登录限制或反爬虫提示"
    return None


def contains_prompt_injection(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PROMPT_INJECTION_MARKERS)


class WebSourceFetcher:
    """受控获取 HTML/PDF，并缓存原文与可定位正文。"""

    REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

    def __init__(
        self,
        *,
        config: Optional[FetchConfig] = None,
        session: Optional[requests.Session] = None,
        validator: Optional[URLSafetyValidator] = None,
        clock: Optional[Callable[[], datetime]] = None,
        pdf_extractor: Optional[PdfTextExtractor] = None,
    ) -> None:
        self.config = config or FetchConfig()
        self.session = session or requests.Session()
        self.validator = validator or URLSafetyValidator()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pdf_extractor = pdf_extractor or PdfTextExtractor(clock=self.clock)

    def fetch(
        self,
        *,
        url: str,
        source_id: str,
        source_handle: str,
        title_hint: str,
        output_dir: Path,
    ) -> FetchedSource:
        canonical_url = self.validator.validate(url)
        source_dir = Path(output_dir) / source_id
        manifest_path = source_dir / "fetched_source.json"
        cached = self._read_cache(manifest_path, canonical_url, source_handle)
        if cached is not None:
            return cached
        source_dir.mkdir(parents=True, exist_ok=True)

        current_url = canonical_url
        response = None
        for redirect_count in range(self.config.max_redirects + 1):
            self.validator.validate(current_url)
            try:
                response = self.session.get(
                    current_url,
                    headers={
                        "User-Agent": self.config.user_agent,
                        "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1",
                    },
                    timeout=self.config.timeout_seconds,
                    stream=True,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                raise SourceFetchError("fetch_timeout", "网页原文获取超时", retryable=True) from exc
            except requests.RequestException as exc:
                raise SourceFetchError(
                    "fetch_network_error", "网页原文网络请求失败", retryable=True
                ) from exc

            if response.status_code in self.REDIRECT_STATUS_CODES:
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    raise SourceFetchError("invalid_redirect", "重定向响应缺少 Location")
                if redirect_count >= self.config.max_redirects:
                    raise SourceFetchError("too_many_redirects", "网页重定向次数超过限制")
                current_url = self.validator.validate(urljoin(current_url, location))
                continue
            break

        assert response is not None
        try:
            if response.status_code in (401, 403):
                raise SourceFetchError(
                    "access_restricted",
                    "目标网页需要登录、授权或拒绝访问；不会绕过访问控制",
                )
            if response.status_code == 429:
                raise SourceFetchError("fetch_rate_limited", "目标网站请求受限", retryable=True)
            if not 200 <= response.status_code < 300:
                raise SourceFetchError(
                    "fetch_http_error", f"网页原文返回 HTTP {response.status_code}"
                )
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > self.config.max_bytes:
                        raise SourceFetchError("source_too_large", "网页或PDF声明大小超过限制")
                except ValueError:
                    pass
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                raw.extend(chunk)
                if len(raw) > self.config.max_bytes:
                    raise SourceFetchError("source_too_large", "网页或PDF实际大小超过限制")
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            final_url = self.validator.validate(str(getattr(response, "url", None) or current_url))
        finally:
            response.close()

        if not raw:
            raise SourceFetchError("empty_source", "网页或PDF响应为空")
        is_pdf = bytes(raw[:5]) == b"%PDF-" or content_type == "application/pdf"
        suffix = ".pdf" if is_pdf else ".html"
        raw_path = source_dir / f"source{suffix}"
        self._atomic_write_bytes(raw_path, bytes(raw))
        sha256 = calculate_sha256(raw_path)
        accessed_at = self.clock()
        if is_pdf:
            fetched = self._parse_pdf(
                source_id=source_id,
                source_handle=source_handle,
                title_hint=title_hint,
                original_url=canonical_url,
                final_url=final_url,
                content_type=content_type or "application/pdf",
                raw_path=raw_path,
                manifest_path=manifest_path,
                sha256=sha256,
                bytes_written=len(raw),
                accessed_at=accessed_at,
            )
        else:
            fetched = self._parse_html(
                source_id=source_id,
                source_handle=source_handle,
                title_hint=title_hint,
                original_url=canonical_url,
                final_url=final_url,
                content_type=content_type or "text/html",
                raw_path=raw_path,
                sha256=sha256,
                bytes_written=len(raw),
                accessed_at=accessed_at,
                raw=bytes(raw),
            )
        self._write_json(manifest_path, fetched.dict())
        return fetched

    def _parse_pdf(
        self,
        *,
        source_id: str,
        source_handle: str,
        title_hint: str,
        original_url: str,
        final_url: str,
        content_type: str,
        raw_path: Path,
        manifest_path: Path,
        sha256: str,
        bytes_written: int,
        accessed_at: datetime,
    ) -> FetchedSource:
        download = DownloadResult(
            source_id=source_id,
            source_title=title_hint,
            source_url=original_url,
            file_path=raw_path.resolve(),
            manifest_path=manifest_path.resolve(),
            accessed_at=accessed_at,
            final_url=final_url,
            http_status=200,
            content_type=content_type,
            bytes_written=bytes_written,
            sha256=sha256,
        )
        try:
            document = self.pdf_extractor.parse(download)
        except PdfExtractionError as exc:
            raise SourceFetchError(exc.code, str(exc)) from exc
        segments: List[FetchedSegment] = []
        total_chars = 0
        truncated = False
        for page in document.pages:
            if page.status is not PageExtractionStatus.EXTRACTED:
                continue
            for line_number, line in enumerate(page.text.splitlines(), start=1):
                text = " ".join(line.split()).strip()
                if len(text) < 8:
                    continue
                if total_chars + len(text) > self.config.max_content_characters:
                    truncated = True
                    break
                total_chars += len(text)
                segments.append(
                    FetchedSegment(
                        segment_id=f"P{page.page_number:03d}L{line_number:03d}",
                        page_number=page.page_number,
                        line_number=line_number,
                        locator=f"PDF第{page.page_number}页第{line_number}行",
                        text=text,
                    )
                )
            if truncated:
                break
        if not segments:
            raise SourceFetchError("insufficient_content", "PDF未提取到可用正文")
        return FetchedSource(
            source_handle=source_handle,
            source_id=source_id,
            title=title_hint,
            original_url=original_url,
            final_url=final_url,
            source_domain=(urlsplit(final_url).hostname or "").lower(),
            accessed_at=accessed_at,
            content_type="application/pdf",
            cache_path=raw_path.resolve(),
            sha256=sha256,
            bytes_written=bytes_written,
            parser_name="pdfplumber",
            content_truncated=truncated,
            prompt_injection_detected=any(
                contains_prompt_injection(item.text) for item in segments
            ),
            segments=segments,
        )

    def _parse_html(
        self,
        *,
        source_id: str,
        source_handle: str,
        title_hint: str,
        original_url: str,
        final_url: str,
        content_type: str,
        raw_path: Path,
        sha256: str,
        bytes_written: int,
        accessed_at: datetime,
        raw: bytes,
    ) -> FetchedSource:
        encoding = "utf-8"
        match = re.search(br"charset=[\"']?([A-Za-z0-9._-]+)", raw[:4096], re.I)
        if match:
            encoding = match.group(1).decode("ascii", errors="ignore") or "utf-8"
        try:
            decoded = raw.decode(encoding, errors="replace")
        except LookupError:
            decoded = raw.decode("utf-8", errors="replace")
        parser = _ReadableHTMLParser()
        parser.feed(decoded)
        parser.close()
        paragraphs = parser.paragraphs
        if not paragraphs and content_type.startswith("text/plain"):
            paragraphs = [
                " ".join(line.split())
                for line in decoded.splitlines()
                if len(" ".join(line.split())) >= 8
            ]
        title = " ".join(parser.title_parts).strip() or title_hint
        restricted_reason = detect_restricted_page(
            title=title,
            final_url=final_url,
            paragraphs=paragraphs,
        )
        if restricted_reason:
            raise SourceFetchError("access_restricted_content", restricted_reason)
        segments: List[FetchedSegment] = []
        total_chars = 0
        truncated = False
        for index, paragraph in enumerate(paragraphs, start=1):
            cleaned = html.unescape(" ".join(paragraph.split())).strip()
            if not cleaned:
                continue
            if total_chars + len(cleaned) > self.config.max_content_characters:
                truncated = True
                break
            total_chars += len(cleaned)
            segments.append(
                FetchedSegment(
                    segment_id=f"P{index:03d}",
                    page_number=1,
                    line_number=index,
                    locator=f"网页段落P{index:03d}",
                    text=cleaned,
                )
            )
        if not segments:
            raise SourceFetchError(
                "insufficient_content", "网页未提取到可定位正文，不能使用搜索摘要代替"
            )
        return FetchedSource(
            source_handle=source_handle,
            source_id=source_id,
            title=title,
            original_url=original_url,
            final_url=final_url,
            source_domain=(urlsplit(final_url).hostname or "").lower(),
            accessed_at=accessed_at,
            content_type=content_type,
            cache_path=raw_path.resolve(),
            sha256=sha256,
            bytes_written=bytes_written,
            parser_name="stdlib-html-parser",
            content_truncated=truncated,
            prompt_injection_detected=any(
                contains_prompt_injection(item.text) for item in segments
            ),
            segments=segments,
        )

    def _read_cache(
        self, manifest_path: Path, original_url: str, source_handle: str
    ) -> Optional[FetchedSource]:
        if not manifest_path.is_file():
            return None
        try:
            cached = FetchedSource.parse_raw(manifest_path.read_text(encoding="utf-8"))
            if cached.original_url != original_url or cached.source_handle != source_handle:
                return None
            if not cached.cache_path.is_file():
                return None
            if calculate_sha256(cached.cache_path) != cached.sha256:
                return None
            if cached.content_type.startswith("text/"):
                restricted_reason = detect_restricted_page(
                    title=cached.title,
                    final_url=cached.final_url,
                    paragraphs=[item.text for item in cached.segments],
                )
                if restricted_reason:
                    raise SourceFetchError(
                        "access_restricted_content", restricted_reason
                    )
            return cached.copy(update={"from_cache": True})
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        WebSourceFetcher._atomic_write_bytes(path, content.encode("utf-8"))


class FixtureSourceFetcher:
    """离线正文获取器；fixture 明确标记为 Mock，不执行 DNS 或网络请求。"""

    def __init__(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.pages = {canonicalize_url(key): dict(value) for key, value in pages.items()}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.call_count = 0

    def fetch(
        self,
        *,
        url: str,
        source_id: str,
        source_handle: str,
        title_hint: str,
        output_dir: Path,
    ) -> FetchedSource:
        canonical = canonicalize_url(url)
        try:
            page = self.pages[canonical]
        except KeyError as exc:
            raise SourceFetchError("fixture_source_missing", "Mock fixture中不存在该URL") from exc
        self.call_count += 1
        status = int(page.get("status", 200))
        if status in (401, 403):
            raise SourceFetchError("access_restricted", "Mock来源模拟访问受限")
        if not 200 <= status < 300:
            raise SourceFetchError("fetch_http_error", f"Mock来源返回HTTP {status}")
        body = str(page.get("body") or "")
        parser = _ReadableHTMLParser()
        parser.feed(body)
        parser.close()
        if not parser.paragraphs:
            raise SourceFetchError("insufficient_content", "Mock来源没有可用原文")
        final_url = canonicalize_url(str(page.get("final_url") or canonical))
        title = " ".join(parser.title_parts).strip() or title_hint
        restricted_reason = detect_restricted_page(
            title=title,
            final_url=final_url,
            paragraphs=parser.paragraphs,
        )
        if restricted_reason:
            raise SourceFetchError("access_restricted_content", restricted_reason)
        source_dir = Path(output_dir) / source_id
        source_dir.mkdir(parents=True, exist_ok=True)
        raw_path = source_dir / "source.html"
        raw_bytes = body.encode("utf-8")
        WebSourceFetcher._atomic_write_bytes(raw_path, raw_bytes)
        segments = [
            FetchedSegment(
                segment_id=f"P{index:03d}",
                page_number=1,
                line_number=index,
                locator=f"网页段落P{index:03d}",
                text=text,
            )
            for index, text in enumerate(parser.paragraphs, start=1)
        ]
        fetched = FetchedSource(
            source_handle=source_handle,
            source_id=source_id,
            title=title,
            original_url=canonical,
            final_url=final_url,
            source_domain=(urlsplit(canonical).hostname or "").lower(),
            accessed_at=self.clock(),
            content_type=str(page.get("content_type") or "text/html"),
            cache_path=raw_path.resolve(),
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            bytes_written=len(raw_bytes),
            parser_name="fixture-html-parser",
            prompt_injection_detected=any(contains_prompt_injection(item.text) for item in segments),
            segments=segments,
        )
        WebSourceFetcher._write_json(source_dir / "fetched_source.json", fetched.dict())
        return fetched


class RuntimeSourceRegistry:
    """以只读 SourceCatalog 为种子，运行时确定性分配新的 Sxx。"""

    def __init__(
        self,
        base_catalog: SourceCatalog,
        *,
        entity_id: str,
        output_path: Path,
    ) -> None:
        self.entity_id = entity_id
        self.output_path = Path(output_path)
        self._base_sources: Dict[str, SourceRecord] = {
            item.source_id: item for item in base_catalog.list_all()
        }
        self._company_sites: Dict[str, str] = {}
        for item in self._base_sources.values():
            if (
                item.entity_id == self.entity_id
                and item.source_type is SourceType.COMPANY_WEBSITE
            ):
                host = (urlsplit(str(item.url)).hostname or "").lower().rstrip(".")
                if host:
                    self._company_sites[host] = item.publisher
        self._sources: Dict[str, SourceRecord] = dict(self._base_sources)
        if self.output_path.is_file():
            payload = json.loads(self.output_path.read_text(encoding="utf-8"))
            for item in payload:
                source = SourceRecord.parse_obj(item)
                existing = self._sources.get(source.source_id)
                if existing is not None and str(existing.url) != str(source.url):
                    raise ValueError(f"运行时来源编号冲突：{source.source_id}")
                self._sources[source.source_id] = source

    def reserve(self, result: SearchResult) -> SourceRecord:
        canonical = canonicalize_url(result.url)
        for source in self._sources.values():
            if canonicalize_url(str(source.url)) == canonical:
                if source.entity_id not in (None, self.entity_id):
                    raise ValueError("已登记URL属于其他研究主体")
                return source
        source_type, tier, publisher = classify_source(
            result.source_domain,
            company_sites=self._company_sites,
        )
        source = SourceRecord(
            source_id=self._next_source_id(),
            entity_id=self.entity_id,
            title=result.title,
            source_type=source_type,
            publisher=publisher,
            url=canonical,
            published_at=result.published_at.date() if result.published_at else None,
            trusted_tier=tier,
            status=SourceStatus.REGISTERED,
            notes="由V1.5百度搜索发现；搜索摘要不作为已验证事实。",
        )
        self._sources[source.source_id] = source
        self._persist()
        return source

    def mark_parsed(self, source_id: str, fetched: FetchedSource) -> SourceRecord:
        source = self.get(source_id)
        updated_payload = source.dict()
        updated_payload.update(
            {
                "title": fetched.title or source.title,
                "url": fetched.final_url,
                "status": SourceStatus.PARSED,
                "accessed_at": fetched.accessed_at,
                "local_path": str(fetched.cache_path),
                "page_or_section": (
                    "PDF物理页码（1-based）"
                    if fetched.content_type == "application/pdf"
                    else "网页段落Pxxx"
                ),
            }
        )
        updated = SourceRecord.parse_obj(updated_payload)
        self._sources[source_id] = updated
        self._persist()
        return updated

    def mark_failed(
        self,
        source_id: str,
        *,
        accessed_at: datetime,
        error_code: str,
        message: str,
    ) -> SourceRecord:
        """记录单一来源读取失败，但保留可追溯的候选来源。"""

        source = self.get(source_id)
        updated_payload = source.dict()
        existing_note = str(source.notes or "").strip()
        failure_note = f"原文获取失败（{error_code}）：{message}"
        updated_payload.update(
            {
                "status": SourceStatus.FAILED,
                "accessed_at": accessed_at,
                "notes": "；".join(
                    item for item in (existing_note, failure_note) if item
                ),
            }
        )
        updated = SourceRecord.parse_obj(updated_payload)
        self._sources[source_id] = updated
        self._persist()
        return updated

    def get(self, source_id: str) -> SourceRecord:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise KeyError(f"未登记运行时来源：{source_id}") from exc

    def runtime_sources(self) -> List[SourceRecord]:
        return [
            source
            for source_id, source in sorted(self._sources.items())
            if source_id not in self._base_sources
            or source != self._base_sources[source_id]
        ]

    def _next_source_id(self) -> str:
        used = {int(source_id[1:]) for source_id in self._sources if source_id[1:].isdigit()}
        for number in range(1, 100):
            if number not in used:
                return f"S{number:02d}"
        raise ValueError("Sxx来源编号已用尽")

    def _persist(self) -> None:
        runtime = [
            item.dict()
            for source_id, item in self._sources.items()
            if source_id not in self._base_sources
            or item != self._base_sources[source_id]
        ]
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        WebSourceFetcher._write_json(self.output_path, runtime)


def classify_source(
    domain: str,
    *,
    company_sites: Optional[Mapping[str, str]] = None,
) -> tuple[SourceType, int, str]:
    host = domain.lower().rstrip(".")
    if host == "cninfo.com.cn" or host.endswith(".cninfo.com.cn"):
        return SourceType.DISCLOSURE_PORTAL, 1, "巨潮资讯网"
    if host == "szse.cn" or host.endswith(".szse.cn"):
        return SourceType.EXCHANGE_WEBSITE, 1, "深圳证券交易所"
    if host == "sse.com.cn" or host.endswith(".sse.com.cn"):
        return SourceType.EXCHANGE_WEBSITE, 1, "上海证券交易所"
    for registered_host, publisher in (company_sites or {}).items():
        normalized = registered_host.lower().rstrip(".")
        if host == normalized or host.endswith("." + normalized):
            return SourceType.COMPANY_WEBSITE, 1, publisher
    if host == "gov.cn" or host.endswith(".gov.cn"):
        return SourceType.GOVERNMENT_WEBSITE, 1, host
    return SourceType.WEB_PAGE, 3, host


def _question_module(question: str) -> ResearchModule:
    if any(word in question for word in ("政策", "监管", "医保", "集采", "带量采购", "药品目录")):
        return ResearchModule.POLICY_REGULATION
    if any(
        word in question
        for word in (
            "行业",
            "市场规模",
            "竞争格局",
            "竞争对手",
            "市场地位",
            "发展趋势",
            "周期性",
        )
    ):
        return ResearchModule.INDUSTRY_RESEARCH
    if "发展" in question or "历史" in question:
        return ResearchModule.DEVELOPMENT_HISTORY
    if "产品" in question:
        return ResearchModule.MAIN_PRODUCTS
    if "经营模式" in question or any(word in question for word in ("采购", "生产", "销售", "研发")):
        return ResearchModule.OPERATING_MODEL
    if "股权" in question or "股东" in question:
        return ResearchModule.OWNERSHIP_STRUCTURE
    return ResearchModule.MAIN_BUSINESS


QUESTION_TERM_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("政策", "监管", "医保", "集采", "带量采购", "药品目录"),
        (
            "政策",
            "监管",
            "医保",
            "药品目录",
            "集中带量采购",
            "集采",
            "中医药",
            "药品注册",
            "药品生产",
            "质量监管",
            "实施时间",
            "适用范围",
        ),
    ),
    (
        ("行业", "市场规模", "竞争格局", "竞争对手", "市场地位", "发展趋势", "周期性"),
        (
            "行业",
            "市场规模",
            "增长",
            "需求",
            "周期",
            "驱动因素",
            "竞争格局",
            "竞争企业",
            "市场地位",
            "发展趋势",
            "风险",
        ),
    ),
    (
        ("主营业务", "业务构成", "核心业务", "业务板块"),
        (
            "主营业务",
            "核心业务",
            "业务布局",
            "业务构成",
            "业务板块",
            "产业布局",
            "营业收入",
            "营收结构",
            "收入占比",
            "毛利率",
        ),
    ),
    (
        ("主要产品", "产品类别", "产品布局", "产品矩阵", "产品线"),
        (
            "主要产品",
            "产品群",
            "产品矩阵",
            "产品布局",
            "产品管线",
            "品种",
            "药物",
            "药品",
            "制剂",
            "胶囊",
            "治疗",
        ),
    ),
    (
        ("经营模式", "销售模式", "盈利模式", "采购模式", "生产模式"),
        (
            "经营模式",
            "销售模式",
            "营销模式",
            "采购",
            "生产",
            "研发",
            "销售",
            "经销",
            "渠道",
            "学术推广",
            "盈利模式",
        ),
    ),
    (
        ("发展历史", "发展历程", "公司沿革"),
        ("发展历史", "发展历程", "成立", "创立", "更名", "上市", "收购", "里程碑"),
    ),
    (
        ("股权", "股东", "实际控制人", "控股股东"),
        ("股权", "股东", "实际控制人", "控股股东", "持股", "表决权"),
    ),
)


def _question_tokens(question: str) -> List[str]:
    """把研究问题展开为确定性的同义词集合，不让LLM决定召回规则。"""

    normalized = " ".join(question.split())
    tokens: List[str] = []
    atomic_terms = (
        "注册资本",
        "实际控制人",
        "控股股东",
        "股东",
        "股权",
        "采购",
        "生产",
        "研发",
        "销售",
    )
    tokens.extend(item for item in atomic_terms if item in normalized)
    for triggers, related_terms in QUESTION_TERM_GROUPS:
        if any(item in normalized for item in triggers):
            tokens.extend(related_terms)
    for part in re.split(r"[\s，。、；：:（）()《》/]+", normalized):
        cleaned = part.strip("的与及等相关信息情况")
        if 2 <= len(cleaned) <= 20:
            tokens.append(cleaned)
    tokens.extend(re.findall(r"[A-Za-z0-9_.-]{3,}", normalized))
    return list(dict.fromkeys(item for item in tokens if item))


def _segment_relevance_score(
    segment: FetchedSegment,
    *,
    tokens: Sequence[str],
    identity: CompanyIdentity,
) -> int:
    text = segment.text.lower()
    score = 0
    for token in tokens:
        lowered = token.lower()
        if lowered in text:
            score += 3 if len(lowered) >= 4 else 1
    if score and (
        identity.short_name.lower() in text or identity.legal_name.lower() in text
    ):
        score += 2
    # 页脚、导航和免责声明即使偶然命中关键词，也不应挤占业务正文。
    if any(
        marker in text
        for marker in ("免责声明", "网站备案号", "版权所有", "不良信息举报", "登录")
    ):
        score -= 5
    return max(score, 0)


def _subject_matches(document: FetchedSource, identity: CompanyIdentity) -> bool:
    sample = (document.title + "\n" + "\n".join(item.text for item in document.segments)).lower()
    candidates = [
        identity.legal_name,
        identity.short_name,
        identity.ticker,
        identity.security_code,
    ]
    return any(candidate.lower() in sample for candidate in candidates if candidate)


def _extract_scalar(text: str) -> Optional[str]:
    values = re.findall(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(亿元|万元|元|%|％|年)", text
    )
    unique = list(dict.fromkeys(f"{number}{unit.replace('％', '%')}" for number, unit in values))
    return unique[0] if len(unique) == 1 else None


class WebEvidenceStore:
    """把原文片段登记为 Evidence/Fact，并保留候选与冲突。"""

    def __init__(
        self,
        *,
        identity: CompanyIdentity,
        output_path: Path,
        clock: Optional[Callable[[], datetime]] = None,
        used_fact_ids: Sequence[str] = (),
        used_evidence_ids: Sequence[str] = (),
    ) -> None:
        self.identity = identity
        self.output_path = Path(output_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._used_fact_ids = set(used_fact_ids)
        self._used_evidence_ids = set(used_evidence_ids)
        self.facts: List[FactRecord] = []
        self.evidence: List[EvidenceRecord] = []
        self.candidates: List[WebEvidenceCandidate] = []
        self.conflicts: List[WebEvidenceConflict] = []
        self.sources: Dict[str, SourceRecord] = {}
        self.warnings: List[str] = []
        self._question_scalars: Dict[str, tuple[str, str, str, str]] = {}

    def record_search_candidate(
        self,
        *,
        source: SourceRecord,
        result: SearchResult,
        research_question: str,
        reason: str,
    ) -> WebEvidenceCandidate:
        """把尚未取得原文的搜索结果保存为未核验候选，而不是企业事实。"""

        self.sources[source.source_id] = source
        existing = next(
            (
                item
                for item in self.candidates
                if item.source_id == source.source_id
                and item.verification_status is EvidenceStatus.UNVERIFIED
                and item.research_question == research_question
            ),
            None,
        )
        if existing is not None:
            return existing

        excerpt = " ".join(str(result.snippet or "").split()).strip()
        if not excerpt:
            excerpt = f"搜索结果标题：{result.title}"
        evidence_id = self._allocate_id("E", self._used_evidence_ids)
        digest_input = "\n".join(
            [result.title, result.url, excerpt, result.searched_at.isoformat()]
        )
        self.evidence.append(
            EvidenceRecord(
                evidence_id=evidence_id,
                source_id=source.source_id,
                source_sha256=hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
                page_number=1,
                line_number=1,
                page_or_section="百度搜索结果摘要（未读取原文）",
                quote=excerpt[:500],
                status=EvidenceStatus.UNVERIFIED,
                verification_method=VerificationMethod.SEARCH_SNIPPET,
            )
        )
        candidate = WebEvidenceCandidate(
            candidate_id=f"CAND{len(self.candidates) + 1:03d}",
            entity_id=self.identity.entity_id,
            source_id=source.source_id,
            evidence_id=evidence_id,
            research_question=research_question,
            excerpt=excerpt[:500],
            verification_status=EvidenceStatus.UNVERIFIED,
            reason=reason,
        )
        self.candidates.append(candidate)
        self.warnings.append(
            f"来源{source.source_id}尚未取得原文，仅保留搜索结果摘要作为未核验候选"
        )
        self._persist(research_question)
        return candidate

    def extract(
        self,
        *,
        source: SourceRecord,
        fetched: FetchedSource,
        research_question: str,
        max_segments: int = 5,
    ) -> WebEvidenceExtraction:
        if source.source_id != fetched.source_id:
            raise ValueError("来源记录与已获取正文的source_id不一致")
        if source.entity_id not in (None, self.identity.entity_id):
            raise ValueError("来源主体与研究主体不一致")
        self.sources[source.source_id] = source
        tokens = _question_tokens(research_question)
        scored: List[tuple[int, FetchedSegment]] = []
        for segment in fetched.segments:
            score = _segment_relevance_score(
                segment,
                tokens=tokens,
                identity=self.identity,
            )
            if score:
                scored.append((score, segment))
        scored.sort(key=lambda item: (-item[0], item[1].page_number, item[1].line_number))
        selected = [item[1] for item in scored[:max_segments]]
        if not selected:
            self.warnings.append("原文未找到与研究问题匹配的段落；搜索摘要未被用作证据")
            self._persist(research_question)
            return self._snapshot(research_question)

        subject_match = _subject_matches(fetched, self.identity)
        module = _question_module(research_question)
        contextual_module = module in {
            ResearchModule.INDUSTRY_RESEARCH,
            ResearchModule.POLICY_REGULATION,
        }
        field_name = f"web_{module.value}_" + hashlib.sha256(
            " ".join(research_question.split()).encode("utf-8")
        ).hexdigest()[:12]
        # 百度搜索结果中的 published_at 只是发现阶段元数据，不能替代网页原文
        # 对政策发布日期/实施日期的核验。只有来源本身已绑定报告期间时才写入
        # Fact.period；网页发布日期继续保存在 SourceRecord 供后台人工核查。
        period = source.report_period

        for segment in selected:
            evidence_id = self._allocate_id("E", self._used_evidence_ids)
            is_injection = contains_prompt_injection(segment.text)
            # 行业与政策事实描述的是外部环境，不要求网页必须出现目标企业名称；
            # 但仍要求问题关键词命中、原文可定位且来源级别可判断。企业自身事实
            # 继续执行原有主体匹配规则，避免把一般行业信息误记为公司披露。
            relevant_subject = subject_match or contextual_module
            verified = source.trusted_tier <= 2 and relevant_subject and not is_injection
            single_source = source.trusted_tier > 2 and relevant_subject and not is_injection
            status = (
                EvidenceStatus.VERIFIED
                if verified
                else (
                    EvidenceStatus.SINGLE_SOURCE
                    if single_source
                    else EvidenceStatus.UNVERIFIED
                )
            )
            quote = segment.text[:500]
            evidence = EvidenceRecord(
                evidence_id=evidence_id,
                source_id=source.source_id,
                source_sha256=fetched.sha256,
                page_number=segment.page_number,
                line_number=segment.line_number,
                page_or_section=segment.locator,
                quote=quote,
                status=status,
                verification_method=VerificationMethod.EXACT_TEXT_MATCH,
            )
            self.evidence.append(evidence)
            if verified or single_source:
                fact_id = self._allocate_id("F", self._used_fact_ids)
                fact = FactRecord(
                    fact_id=fact_id,
                    entity_id=self.identity.entity_id,
                    subject=(
                        (source.title or source.publisher)
                        if contextual_module
                        else self.identity.legal_name
                    ),
                    category=(
                        FactCategory.IDENTITY
                        if module is ResearchModule.COMPANY_IDENTITY
                        else (
                            FactCategory.INDUSTRY
                            if contextual_module
                            else FactCategory.BUSINESS
                        )
                    ),
                    research_module=module,
                    field_name=field_name,
                    value=quote,
                    claim=quote,
                    entity_scope=self.identity.entity_scope,
                    period=period,
                    evidence_ids=[evidence_id],
                )
                scalar = _extract_scalar(quote)
                conflict_key = f"{field_name}|{period or ''}"
                prior = self._question_scalars.get(conflict_key)
                if scalar and prior and prior[0] != scalar:
                    prior_scalar, prior_source_id, prior_fact_id, prior_evidence_id = prior
                    self._mark_conflict(
                        conflict_key=conflict_key,
                        field_name=field_name,
                        period=period,
                        prior_scalar=prior_scalar,
                        prior_source_id=prior_source_id,
                        prior_fact_id=prior_fact_id,
                        prior_evidence_id=prior_evidence_id,
                        scalar=scalar,
                        source_id=source.source_id,
                        evidence=evidence,
                        question=research_question,
                    )
                    continue
                self.facts.append(fact)
                if scalar:
                    self._question_scalars[conflict_key] = (
                        scalar,
                        source.source_id,
                        fact_id,
                        evidence_id,
                    )
                if single_source:
                    self.warnings.append(
                        f"来源{source.source_id}为单一第三方来源，事实可交付但待交叉核实"
                    )
            else:
                reason = (
                    "prompt_injection_detected"
                    if is_injection
                    else "subject_or_source_not_verified"
                )
                excerpt = (
                    "[疑似网页指令注入，原文未提供给模型]"
                    if is_injection
                    else quote
                )
                self.candidates.append(
                    WebEvidenceCandidate(
                        candidate_id=f"CAND{len(self.candidates) + 1:03d}",
                        entity_id=self.identity.entity_id,
                        source_id=source.source_id,
                        evidence_id=evidence_id,
                        research_question=research_question,
                        excerpt=excerpt,
                        verification_status=EvidenceStatus.UNVERIFIED,
                        reason=reason,
                    )
                )
                if is_injection:
                    self.warnings.append("检测到疑似Prompt Injection；页面文字仅按不可信数据保存")
                elif not relevant_subject:
                    self.warnings.append("网页正文无法确认与当前研究主体一致")
                elif source.trusted_tier > 2:
                    self.warnings.append("第三方网页未通过主体核对，仅作为待核实线索")
        self._persist(research_question)
        return self._snapshot(research_question)

    def _mark_conflict(
        self,
        *,
        conflict_key: str,
        field_name: str,
        period: Optional[str],
        prior_scalar: str,
        prior_source_id: str,
        prior_fact_id: str,
        prior_evidence_id: str,
        scalar: str,
        source_id: str,
        evidence: EvidenceRecord,
        question: str,
    ) -> None:
        self.facts = [item for item in self.facts if item.fact_id != prior_fact_id]
        updated_evidence: List[EvidenceRecord] = []
        for item in self.evidence:
            if item.evidence_id in (prior_evidence_id, evidence.evidence_id):
                updated_evidence.append(item.copy(update={"status": EvidenceStatus.CONFLICTING}))
            else:
                updated_evidence.append(item)
        self.evidence = updated_evidence
        conflict = WebEvidenceConflict(
            conflict_id=f"CONFLICT{len(self.conflicts) + 1:03d}",
            entity_id=self.identity.entity_id,
            field_name=field_name,
            period=period,
            source_ids=[prior_source_id, source_id],
            values=[prior_scalar, scalar],
        )
        self.conflicts.append(conflict)
        for candidate_source, candidate_evidence, candidate_value in (
            (prior_source_id, prior_evidence_id, prior_scalar),
            (source_id, evidence.evidence_id, scalar),
        ):
            self.candidates.append(
                WebEvidenceCandidate(
                    candidate_id=f"CAND{len(self.candidates) + 1:03d}",
                    entity_id=self.identity.entity_id,
                    source_id=candidate_source,
                    evidence_id=candidate_evidence,
                    research_question=question,
                    excerpt=candidate_value,
                    verification_status=EvidenceStatus.CONFLICTING,
                    reason="conflicting_source_values",
                )
            )
        self._question_scalars.pop(conflict_key, None)
        self.warnings.append("不同来源对同一问题给出冲突值，未选择任一值作为确定性事实")

    def to_evidence_bundle(self) -> EvidenceBundle:
        verified_evidence_ids = {
            evidence_id
            for fact in self.facts
            for evidence_id in fact.evidence_ids
        }
        verified_evidence = [
            item for item in self.evidence if item.evidence_id in verified_evidence_ids
        ]
        source_ids = {item.source_id for item in verified_evidence}
        return EvidenceBundle(
            schema_version="1.5.0",
            identity=self.identity,
            generated_at=self.clock(),
            facts=list(self.facts),
            evidence=verified_evidence,
            sources=[self.sources[source_id] for source_id in sorted(source_ids)],
            limitations=list(dict.fromkeys(self.warnings)),
        )

    def _snapshot(self, question: str) -> WebEvidenceExtraction:
        return WebEvidenceExtraction(
            identity=self.identity,
            research_question=question,
            generated_at=self.clock(),
            facts=list(self.facts),
            evidence=list(self.evidence),
            candidates=list(self.candidates),
            conflicts=list(self.conflicts),
            sources=list(self.sources.values()),
            warnings=list(dict.fromkeys(self.warnings)),
        )

    def snapshot(self, question: str) -> WebEvidenceExtraction:
        """返回并持久化当前证据状态，供确定性报告兜底使用。"""

        self._persist(question)
        return self._snapshot(question)

    def _persist(self, question: str) -> None:
        snapshot = self._snapshot(question)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        WebSourceFetcher._write_json(self.output_path, snapshot.dict())

    @staticmethod
    def _allocate_id(prefix: str, used: set[str]) -> str:
        # V1.5网页证据暂使用801—999，避免与V0/V1.1现有F/E编号冲突；
        # 最终仍是原有FactRecord/EvidenceRecord，不引入另一套编号格式。
        for number in range(801, 1000):
            candidate = f"{prefix}{number:03d}"
            if candidate not in used:
                used.add(candidate)
                return candidate
        raise ValueError(f"{prefix}xxx编号已用尽")
