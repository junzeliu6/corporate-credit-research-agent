"""V3.3A A股证券主数据获取、缓存与动态企业识别。

本模块只负责上市主体身份，不负责年报业务事实提取。公开网站内部接口并无
稳定、版本化的开放 API 契约，因此网络 Provider 与本地缓存严格分离；刷新
失败时只能显式使用已有缓存，不能猜测公司名称或交易所。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol
from urllib.parse import urlparse

import requests
from pydantic.v1 import BaseModel, Field, root_validator, validator

from credit_research_agent.company import CompanyResolutionError, CompanyResolver
from credit_research_agent.models import CompanyIdentity, EntityScope


CNINFO_STOCK_INDEX_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_COMPANY_LIST_URL = (
    "https://www.cninfo.com.cn/data/yellowpages/getYellowpageStockList"
)
CNINFO_COMPANY_LIST_PAGE = "https://www.cninfo.com.cn/new/snapshot/companyListCn"
SSE_STOCK_QUERY_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SSE_STOCK_LIST_PAGE = "https://www.sse.com.cn/assortment/stock/list/share/"


class SecurityMasterError(RuntimeError):
    """证券主数据读取或校验失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SecurityMasterCacheStatus(str, Enum):
    FRESH = "fresh"
    REFRESHED = "refreshed"
    STALE = "stale"
    STATIC_FALLBACK = "static_fallback"


class SecurityMasterRecord(BaseModel):
    """由官方证券主数据标准化后的单一A股上市主体。"""

    security_code: str
    exchange_code: str
    ticker: str
    entity_id: str
    short_name: str
    legal_name: str
    english_name: Optional[str] = None
    exchange: str
    board: str
    listing_status: str = "listed"
    listed_at: Optional[date] = None
    external_org_id: str
    source_urls: List[str] = Field(min_items=1)
    source_publisher: str
    retrieved_at: datetime
    raw_status_code: Optional[str] = None

    @validator("security_code")
    def validate_security_code(cls, value: str) -> str:
        if not re.fullmatch(r"\d{6}", value):
            raise ValueError("security_code 必须为6位数字")
        return value

    @validator("ticker")
    def validate_ticker(cls, value: str, values: Dict[str, object]) -> str:
        expected_suffix = {
            "SSE": "SH",
            "SZSE": "SZ",
            "BSE": "BJ",
        }.get(str(values.get("exchange_code") or ""))
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
            raise ValueError("ticker 必须采用 600000.SH 形式")
        if expected_suffix and not value.endswith(f".{expected_suffix}"):
            raise ValueError("ticker 与 exchange_code 不一致")
        return value

    @root_validator
    def validate_identity_keys(cls, values: Dict[str, object]) -> Dict[str, object]:
        code = str(values.get("security_code") or "")
        exchange_code = str(values.get("exchange_code") or "")
        ticker = str(values.get("ticker") or "")
        entity_id = str(values.get("entity_id") or "")
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange_code)
        if suffix is None:
            raise ValueError("exchange_code 必须为 SSE、SZSE 或 BSE")
        if ticker and ticker != f"{code}.{suffix}":
            raise ValueError("ticker、security_code 与 exchange_code 不一致")
        if entity_id and entity_id != f"CN-A-{exchange_code}-{code}":
            raise ValueError("entity_id 与证券代码/交易所不一致")
        return values

    def to_company_identity(self) -> CompanyIdentity:
        return CompanyIdentity(
            entity_id=self.entity_id,
            legal_name=self.legal_name,
            english_name=self.english_name,
            short_name=self.short_name,
            ticker=self.ticker,
            security_code=self.security_code,
            exchange=self.exchange,
            country_of_incorporation="中国",
            entity_scope=EntityScope.CONSOLIDATED,
            evidence_ids=[],
            exchange_code=self.exchange_code,
            listing_board=self.board,
            listing_status=self.listing_status,
            listed_at=self.listed_at,
            external_org_id=self.external_org_id,
            identity_source_url=self.source_urls[-1],
            resolved_at=self.retrieved_at,
        )


class SecurityMasterSnapshot(BaseModel):
    schema_version: str = "3.3a.0"
    updated_at: datetime
    records: List[SecurityMasterRecord]


class SecurityMasterLookupResult(BaseModel):
    record: SecurityMasterRecord
    cache_status: SecurityMasterCacheStatus
    warnings: List[str] = Field(default_factory=list)


class DynamicCompanyResolution(BaseModel):
    identity: CompanyIdentity
    resolution_source: str
    cache_status: SecurityMasterCacheStatus
    security_master_record: Optional[SecurityMasterRecord] = None
    warnings: List[str] = Field(default_factory=list)


class SecurityMasterProvider(Protocol):
    def lookup(self, security_code: str) -> SecurityMasterRecord:
        ...


def _response_text(response: object) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise SecurityMasterError("invalid_response", "官方主数据响应缺少文本正文")
    return text


def _parse_json_or_jsonp(text: str) -> Dict[str, object]:
    stripped = text.strip()
    if stripped.startswith("{"):
        payload = stripped
    else:
        opening = stripped.find("(")
        closing = stripped.rfind(")")
        if opening <= 0 or closing <= opening or stripped[closing + 1 :].strip(" ;"):
            raise SecurityMasterError("invalid_jsonp", "官方主数据未返回合法 JSON/JSONP")
        payload = stripped[opening + 1 : closing]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SecurityMasterError("invalid_json", "官方主数据 JSON 无法解析") from exc
    if not isinstance(parsed, dict):
        raise SecurityMasterError("invalid_payload", "官方主数据响应必须为对象")
    return parsed


def _normalise_name(value: object) -> str:
    return "".join(str(value or "").replace("\u3000", " ").split()).strip()


def _route_exchange(security_code: str) -> str:
    if security_code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SSE"
    if security_code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZSE"
    if security_code.startswith(("4", "8", "92")):
        return "BSE"
    raise SecurityMasterError(
        "unsupported_security_code",
        f"证券代码 {security_code} 不属于V3.3A已支持的A股代码段",
    )


def parse_a_share_code(query: str) -> tuple[str, str, str]:
    """离线解析代码语法，返回 ``ticker, exchange_code, entity_id``。

    这里只判断代码段和后缀一致性，不声称证券真实存在；存在性仍由官方主数据
    Resolver 核验。因此 Streamlit 刷新不会为了展示任务目录而发起网络请求。
    """

    normalised = "".join(str(query or "").strip().upper().split())
    match = re.fullmatch(r"(?P<code>\d{6})(?:\.(?P<suffix>SH|SZ|BJ))?", normalised)
    if match is None:
        raise SecurityMasterError(
            "invalid_security_code_format", "请输入6位A股代码或标准交易所后缀代码"
        )
    code = match.group("code")
    exchange_code = _route_exchange(code)
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange_code]
    supplied = match.group("suffix")
    if supplied and supplied != suffix:
        raise SecurityMasterError(
            "security_suffix_mismatch",
            f"代码 {code} 应使用 .{suffix} 后缀，而不是 .{supplied}",
        )
    return f"{code}.{suffix}", exchange_code, f"CN-A-{exchange_code}-{code}"


def _exchange_metadata(exchange_code: str, security_code: str) -> tuple[str, str, str]:
    if exchange_code == "SSE":
        board = "科创板" if security_code.startswith(("688", "689")) else "沪市主板"
        return "上海证券交易所", "SH", board
    if exchange_code == "SZSE":
        board = "创业板" if security_code.startswith(("300", "301")) else "深市主板"
        return "深圳证券交易所", "SZ", board
    return "北京证券交易所", "BJ", "北交所"


def _ensure_official_response(response: object, allowed_hosts: Iterable[str]) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        raise SecurityMasterError(
            "official_source_http_error", f"官方主数据请求返回 HTTP {status_code}"
        )
    final_url = str(getattr(response, "url", "") or "")
    if final_url:
        host = (urlparse(final_url).hostname or "").lower()
        if not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
            raise SecurityMasterError(
                "untrusted_redirect", f"官方主数据请求重定向至非白名单域名：{host}"
            )


class OfficialAshareSecurityMasterProvider:
    """低频读取巨潮/交易所公开页面数据的沪深A股主数据 Provider。

    巨潮统一代码索引用于确认代码和 ``orgId``；公司全称分别由巨潮公司列表
    （深市）和上交所股票列表（沪市）复核。页面内部接口不是公开 API 契约，
    因此生产使用必须配合 ``SecurityMasterCache``，不能把实时端点当成唯一依赖。
    """

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20.0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于0")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def lookup(self, security_code: str) -> SecurityMasterRecord:
        if not re.fullmatch(r"\d{6}", security_code):
            raise SecurityMasterError("invalid_security_code", "证券代码必须为6位数字")
        exchange_code = _route_exchange(security_code)
        index_item = self._lookup_cninfo_index(security_code)
        if _normalise_name(index_item.get("category")) != "A股":
            raise SecurityMasterError(
                "unsupported_security_type", f"证券 {security_code} 不是A股普通股"
            )
        if exchange_code == "BSE":
            raise SecurityMasterError(
                "unsupported_exchange",
                "V3.3A首版尚未完成北交所公司全称适配；不得仅凭代码前缀猜测主体",
            )
        if exchange_code == "SZSE":
            detail, detail_source = self._lookup_szse_detail(security_code)
            legal_name = _normalise_name(detail.get("ORGNAME"))
            short_name = _normalise_name(detail.get("SECNAME"))
            english_name = None
            listed_at = None
            raw_status = str(detail.get("F003N") or "") or None
        else:
            detail, detail_source = self._lookup_sse_detail(security_code)
            legal_name = _normalise_name(detail.get("FULL_NAME"))
            short_name = _normalise_name(
                detail.get("COMPANY_ABBR") or detail.get("SEC_NAME_CN")
            )
            english_name = (
                " ".join(
                    str(detail.get("FULL_NAME_IN_ENGLISH") or "")
                    .replace("\u3000", " ")
                    .split()
                )
                or None
            )
            listed_at = self._parse_listing_date(detail.get("LIST_DATE"))
            raw_status = str(detail.get("STATE_CODE") or "") or None
            delist_date = str(detail.get("DELIST_DATE") or "").strip()
            if delist_date not in {"", "-", "None"}:
                raise SecurityMasterError(
                    "unsupported_listing_status",
                    f"证券 {security_code} 已存在退市日期 {delist_date}",
                )
        if str(detail.get("SECCODE") or detail.get("COMPANY_CODE") or "") != security_code:
            raise SecurityMasterError("identity_mismatch", "官方主体详情与请求代码不一致")
        index_short_name = _normalise_name(index_item.get("zwjc"))
        if index_short_name and short_name and index_short_name != short_name:
            raise SecurityMasterError(
                "identity_mismatch", "巨潮代码索引与交易所主体详情的证券简称不一致"
            )
        if not legal_name or not short_name:
            raise SecurityMasterError("missing_company_name", "官方主体详情缺少公司名称")
        exchange, suffix, board = _exchange_metadata(exchange_code, security_code)
        return SecurityMasterRecord(
            security_code=security_code,
            ticker=f"{security_code}.{suffix}",
            entity_id=f"CN-A-{exchange_code}-{security_code}",
            short_name=short_name,
            legal_name=legal_name,
            english_name=english_name,
            exchange_code=exchange_code,
            exchange=exchange,
            board=board,
            listing_status="listed",
            listed_at=listed_at,
            external_org_id=str(index_item.get("orgId") or "").strip(),
            source_urls=[CNINFO_STOCK_INDEX_URL, detail_source],
            source_publisher=(
                "巨潮资讯网/深圳证券交易所" if exchange_code == "SZSE" else "巨潮资讯网/上海证券交易所"
            ),
            retrieved_at=self._clock(),
            raw_status_code=raw_status,
        )

    def _get(self, url: str, *, params: Optional[Dict[str, object]], referer: str) -> object:
        try:
            response = self.session.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json,text/javascript,*/*;q=0.8",
                    "Referer": referer,
                    "User-Agent": "CorporateCreditResearchAgent/3.3 (public-disclosure-research)",
                },
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise SecurityMasterError("official_source_network_error", str(exc)) from exc
        _ensure_official_response(response, ("cninfo.com.cn", "sse.com.cn"))
        return response

    def _lookup_cninfo_index(self, security_code: str) -> Dict[str, object]:
        response = self._get(
            CNINFO_STOCK_INDEX_URL,
            params=None,
            referer=CNINFO_COMPANY_LIST_PAGE,
        )
        try:
            payload = json.loads(_response_text(response))
        except json.JSONDecodeError as exc:
            raise SecurityMasterError("invalid_json", "巨潮公司代码索引无法解析") from exc
        rows = payload.get("stockList") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SecurityMasterError("invalid_payload", "巨潮公司代码索引缺少 stockList")
        matches = [item for item in rows if str(item.get("code")) == security_code]
        if len(matches) != 1:
            raise SecurityMasterError(
                "security_not_found" if not matches else "ambiguous_security",
                f"官方公司代码索引未唯一匹配 {security_code}",
            )
        org_id = str(matches[0].get("orgId") or "").strip()
        if not org_id:
            raise SecurityMasterError("missing_org_id", "官方公司代码索引缺少 orgId")
        return dict(matches[0])

    def _lookup_szse_detail(self, security_code: str) -> tuple[Dict[str, object], str]:
        response = self._get(
            CNINFO_COMPANY_LIST_URL,
            params={
                "type": "cn",
                "pagenum": 1,
                "keyword": security_code,
                "Sortcolumn": "SECCODE",
                "jsonpCallback": "callback",
            },
            referer=CNINFO_COMPANY_LIST_PAGE,
        )
        payload = _parse_json_or_jsonp(_response_text(response))
        rows = payload.get("records")
        if not isinstance(rows, list):
            rows = []
        matches = [item for item in rows if str(item.get("SECCODE")) == security_code]
        if len(matches) != 1:
            raise SecurityMasterError(
                "company_detail_not_found", f"深市官方公司列表未唯一匹配 {security_code}"
            )
        return dict(matches[0]), CNINFO_COMPANY_LIST_PAGE

    def _lookup_sse_detail(self, security_code: str) -> tuple[Dict[str, object], str]:
        stock_type = "8" if security_code.startswith(("688", "689")) else "1"
        response = self._get(
            SSE_STOCK_QUERY_URL,
            params={
                "jsonCallBack": "callback",
                "isPagination": "true",
                "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                "COMPANY_STATUS": "2,4,5,7,8",
                "STOCK_TYPE": stock_type,
                "REG_PROVINCE": "",
                "CSRC_CODE": "",
                "STOCK_CODE": security_code,
                "type": "inParams",
                "pageHelp.cacheSize": 1,
                "pageHelp.beginPage": 1,
                "pageHelp.pageSize": 25,
                "pageHelp.pageNo": 1,
            },
            referer=SSE_STOCK_LIST_PAGE,
        )
        payload = _parse_json_or_jsonp(_response_text(response))
        rows = payload.get("result")
        if not isinstance(rows, list):
            page_help = payload.get("pageHelp")
            rows = page_help.get("data") if isinstance(page_help, dict) else []
        if not isinstance(rows, list):
            rows = []
        matches = [item for item in rows if str(item.get("COMPANY_CODE")) == security_code]
        if len(matches) != 1:
            raise SecurityMasterError(
                "company_detail_not_found", f"上交所官方股票列表未唯一匹配 {security_code}"
            )
        return dict(matches[0]), SSE_STOCK_LIST_PAGE

    @staticmethod
    def _parse_listing_date(value: object) -> Optional[date]:
        text = str(value or "").strip()
        if not text or text == "-":
            return None
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise SecurityMasterError(
                "invalid_listing_date", f"上交所上市日期格式无效：{text}"
            ) from exc


class SecurityMasterCache:
    """按证券代码缓存已核验主数据；缓存文件可离线复用。"""

    def __init__(
        self,
        cache_path: Path,
        provider: SecurityMasterProvider,
        *,
        max_age: timedelta = timedelta(days=7),
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if max_age.total_seconds() <= 0:
            raise ValueError("max_age 必须大于0")
        self.cache_path = Path(cache_path)
        self.provider = provider
        self.max_age = max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def lookup(
        self,
        security_code: str,
        *,
        force_refresh: bool = False,
        allow_stale: bool = True,
    ) -> SecurityMasterLookupResult:
        snapshot = self._read_snapshot()
        cached = self._find(snapshot, security_code)
        if cached is not None and not force_refresh and self.is_record_fresh(cached):
            return SecurityMasterLookupResult(
                record=cached, cache_status=SecurityMasterCacheStatus.FRESH
            )
        try:
            record = self.provider.lookup(security_code)
        except SecurityMasterError:
            if cached is None or not allow_stale:
                raise
            return SecurityMasterLookupResult(
                record=cached,
                cache_status=SecurityMasterCacheStatus.STALE,
                warnings=["官方证券主数据刷新失败，本次显式使用过期缓存"],
            )
        records = {
            item.security_code: item for item in (snapshot.records if snapshot else [])
        }
        records[record.security_code] = record
        updated = SecurityMasterSnapshot(
            updated_at=self._clock(),
            records=sorted(records.values(), key=lambda x: x.security_code),
        )
        self._write_snapshot(updated)
        return SecurityMasterLookupResult(
            record=record, cache_status=SecurityMasterCacheStatus.REFRESHED
        )

    def find_by_name(self, query: str) -> List[SecurityMasterRecord]:
        snapshot = self._read_snapshot()
        if snapshot is None:
            return []
        key = _normalise_name(query).upper()
        return [
            record
            for record in snapshot.records
            if key
            in {
                _normalise_name(record.short_name).upper(),
                _normalise_name(record.legal_name).upper(),
            }
        ]

    def _read_snapshot(self) -> Optional[SecurityMasterSnapshot]:
        if not self.cache_path.is_file():
            return None
        try:
            snapshot = SecurityMasterSnapshot.parse_raw(
                self.cache_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise SecurityMasterError(
                "corrupt_security_master_cache", f"证券主数据缓存损坏：{exc}"
            ) from exc
        codes = [item.security_code for item in snapshot.records]
        if len(codes) != len(set(codes)):
            raise SecurityMasterError("corrupt_security_master_cache", "缓存存在重复证券代码")
        return snapshot

    @staticmethod
    def _find(
        snapshot: Optional[SecurityMasterSnapshot], security_code: str
    ) -> Optional[SecurityMasterRecord]:
        if snapshot is None:
            return None
        return next(
            (item for item in snapshot.records if item.security_code == security_code),
            None,
        )

    def is_record_fresh(self, record: SecurityMasterRecord) -> bool:
        """按单条记录的实际获取时间判断，避免刷新A公司顺带刷新B公司。"""

        retrieved_at = record.retrieved_at
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        age = self._clock() - retrieved_at
        return timedelta(0) <= age <= self.max_age

    def _write_snapshot(self, snapshot: SecurityMasterSnapshot) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = snapshot.json(ensure_ascii=False, indent=2) + "\n"
        _atomic_write_text(self.cache_path, content)


class DynamicCompanyResolver:
    """保留静态回归目录，并对六位A股代码启用官方主数据解析。"""

    CODE_PATTERN = re.compile(r"^(?P<code>\d{6})(?:\.(?P<suffix>SH|SZ|BJ))?$", re.I)

    def __init__(
        self,
        master_cache: SecurityMasterCache,
        *,
        static_resolver: Optional[CompanyResolver] = None,
    ) -> None:
        self.master_cache = master_cache
        self.static_resolver = static_resolver or CompanyResolver.from_default_catalog()

    def resolve(
        self, query: str, *, force_refresh: bool = False
    ) -> CompanyIdentity:
        return self.resolve_with_metadata(query, force_refresh=force_refresh).identity

    def resolve_with_metadata(
        self, query: str, *, force_refresh: bool = False
    ) -> DynamicCompanyResolution:
        if not query or not query.strip():
            raise CompanyResolutionError("企业名称或证券代码不能为空")
        normalised = "".join(query.strip().upper().split())
        match = self.CODE_PATTERN.fullmatch(normalised)
        if match is None:
            cached_matches = self.master_cache.find_by_name(normalised)
            if len(cached_matches) == 1:
                lookup = self.master_cache.lookup(
                    cached_matches[0].security_code,
                    force_refresh=force_refresh,
                )
                result = self._from_lookup(lookup, suffix=None)
                return result.copy(update={"resolution_source": "official_cache_name"})
            if len(cached_matches) > 1:
                raise CompanyResolutionError(f"企业名称存在歧义：{query!r}")
            try:
                static_identity = self.static_resolver.resolve(query)
            except CompanyResolutionError as exc:
                raise CompanyResolutionError(
                    f"无法唯一识别企业：{query!r}。动态首版请优先输入6位A股代码。"
                ) from exc
            try:
                lookup = self.master_cache.lookup(
                    static_identity.security_code, force_refresh=force_refresh
                )
            except SecurityMasterError:
                return DynamicCompanyResolution(
                    identity=static_identity,
                    resolution_source="static_fixture",
                    cache_status=SecurityMasterCacheStatus.STATIC_FALLBACK,
                    warnings=["官方主数据暂不可用，仅返回既有静态身份；不能据此发现新年报"],
                )
            return self._from_lookup(lookup, suffix=None)
        try:
            lookup = self.master_cache.lookup(
                match.group("code"), force_refresh=force_refresh
            )
        except SecurityMasterError as official_error:
            # 仅对旧目录中可精确命中的回归主体降级；未知公司绝不猜测。
            try:
                static_identity = self.static_resolver.resolve(normalised)
            except CompanyResolutionError:
                raise official_error
            return DynamicCompanyResolution(
                identity=static_identity,
                resolution_source="static_fixture",
                cache_status=SecurityMasterCacheStatus.STATIC_FALLBACK,
                warnings=["官方主数据暂不可用，仅返回既有静态身份；不能据此发现新年报"],
            )
        return self._from_lookup(lookup, suffix=match.group("suffix"))

    @staticmethod
    def _from_lookup(
        lookup: SecurityMasterLookupResult, *, suffix: Optional[str]
    ) -> DynamicCompanyResolution:
        actual_suffix = lookup.record.ticker.rsplit(".", 1)[1]
        if suffix and suffix.upper() != actual_suffix:
            raise CompanyResolutionError(
                f"证券代码后缀不匹配：输入 .{suffix.upper()}，官方主数据为 .{actual_suffix}"
            )
        return DynamicCompanyResolution(
            identity=lookup.record.to_company_identity(),
            resolution_source="official_security_master",
            cache_status=lookup.cache_status,
            security_master_record=lookup.record,
            warnings=lookup.warnings,
        )


def _atomic_write_text(path: Path, content: str) -> None:
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


def snapshot_sha256(snapshot: SecurityMasterSnapshot) -> str:
    """供状态文件记录缓存内容哈希，不包含本地文件路径。"""

    payload = snapshot.json(ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

