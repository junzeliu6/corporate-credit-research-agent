"""V1.5 通用网页搜索接口与百度搜索 Provider。

搜索摘要只用于发现候选来源，不能直接转为企业事实。真实请求默认关闭；Live
模式必须显式配置密钥并由上层 Runtime 再次确认。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


BAIDU_SEARCH_PROVIDER = "baidu"
BAIDU_SEARCH_BASE_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
BAIDU_SEARCH_API_KEY_ENV = "BAIDU_SEARCH_API_KEY"
BAIDU_SEARCH_BASE_URL_ENV = "BAIDU_SEARCH_BASE_URL"
BAIDU_SEARCH_AUTH_HEADER_ENV = "BAIDU_SEARCH_AUTH_HEADER"
SEARCH_PROVIDER_ENV = "SEARCH_PROVIDER"
BAIDU_SEARCH_CALL_PRICE_CNY = 0.036


class SearchMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"


class SearchRecency(str, Enum):
    WEEK = "week"
    MONTH = "month"
    SEMIYEAR = "semiyear"
    YEAR = "year"


class BaiduAuthHeader(str, Enum):
    """百度官方文档存在两种示例，因此一次请求只能显式选择其中一种。"""

    AUTHORIZATION = "Authorization"
    X_APPBUILDER_AUTHORIZATION = "X-Appbuilder-Authorization"


class SearchError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BaiduSearchConfig(BaseModel):
    """可安全记录的百度搜索配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SearchMode = SearchMode.MOCK
    provider: Literal["baidu"] = BAIDU_SEARCH_PROVIDER
    base_url: str = BAIDU_SEARCH_BASE_URL
    api_key_env: Literal["BAIDU_SEARCH_API_KEY"] = BAIDU_SEARCH_API_KEY_ENV
    api_key: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    auth_header: BaiduAuthHeader = BaiduAuthHeader.AUTHORIZATION
    edition: Literal["standard", "lite"] = "standard"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_live_config(self) -> "BaiduSearchConfig":
        if self.base_url != BAIDU_SEARCH_BASE_URL:
            parsed = urlsplit(self.base_url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise ValueError("百度搜索 Base URL 必须是有效的 HTTPS URL")
        if self.mode is SearchMode.LIVE and self.api_key is None:
            raise ValueError(
                f"百度搜索 Live 模式缺少环境变量 {self.api_key_env}"
            )
        return self

    def sanitized_summary(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "provider": self.provider,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": self.api_key is not None,
            "auth_header": self.auth_header.value,
            "edition": self.edition,
            "request_timeout_seconds": self.request_timeout_seconds,
        }


def load_baidu_search_config(
    project_root: Path,
    *,
    mode: SearchMode | str = SearchMode.MOCK,
    env_file: Optional[Path] = None,
    **overrides: Any,
) -> BaiduSearchConfig:
    """从环境变量或项目 ``.env`` 读取百度搜索配置。

    Mock 模式故意不读取密钥，保证离线测试不会把真实凭证带入运行对象。
    环境变量优先于 ``.env``。
    """

    resolved_mode = SearchMode(mode)
    file_path = Path(env_file) if env_file else Path(project_root) / ".env"
    file_values = dotenv_values(file_path) if file_path.is_file() else {}

    provider = os.environ.get(SEARCH_PROVIDER_ENV) or file_values.get(
        SEARCH_PROVIDER_ENV, BAIDU_SEARCH_PROVIDER
    )
    if provider != BAIDU_SEARCH_PROVIDER:
        raise SearchError(
            "unsupported_search_provider",
            "V1.5 当前只实现 SEARCH_PROVIDER=baidu",
        )

    base_url = str(
        os.environ.get(BAIDU_SEARCH_BASE_URL_ENV)
        or file_values.get(BAIDU_SEARCH_BASE_URL_ENV)
        or BAIDU_SEARCH_BASE_URL
    ).strip()
    raw_header = str(
        os.environ.get(BAIDU_SEARCH_AUTH_HEADER_ENV)
        or file_values.get(BAIDU_SEARCH_AUTH_HEADER_ENV)
        or BaiduAuthHeader.AUTHORIZATION.value
    ).strip()
    api_key: Optional[SecretStr] = None
    if resolved_mode is SearchMode.LIVE:
        raw_key = os.environ.get(BAIDU_SEARCH_API_KEY_ENV) or file_values.get(
            BAIDU_SEARCH_API_KEY_ENV
        )
        if not raw_key:
            raise SearchError(
                "missing_search_api_key",
                f"百度搜索 Live 模式需要环境变量或 .env 中的 {BAIDU_SEARCH_API_KEY_ENV}",
            )
        api_key = SecretStr(str(raw_key).strip())

    try:
        return BaiduSearchConfig(
            mode=resolved_mode,
            base_url=base_url,
            auth_header=BaiduAuthHeader(raw_header),
            api_key=api_key,
            **overrides,
        )
    except (ValueError, TypeError) as exc:
        raise SearchError("invalid_search_config", str(exc)) from exc


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=5)
    domain_filter: List[str] = Field(default_factory=list, max_length=20)
    recency: Optional[SearchRecency] = None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("搜索关键词不能为空")
        return normalized

    @field_validator("domain_filter")
    @classmethod
    def normalize_domains(cls, values: List[str]) -> List[str]:
        normalized: List[str] = []
        for value in values:
            candidate = value.strip().lower().rstrip(".")
            if "://" in candidate or "/" in candidate or not candidate:
                raise ValueError("domain_filter 只能填写域名，不能填写完整 URL")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str
    title: str
    url: str
    snippet: str
    source_domain: str
    published_at: Optional[datetime] = None
    searched_at: datetime
    provider: Literal["baidu"] = BAIDU_SEARCH_PROVIDER
    provider_reference_id: Optional[int] = None
    authority_score: Optional[float] = Field(default=None, ge=0, le=1)
    rerank_score: Optional[float] = Field(default=None, ge=0, le=1)
    is_search_snippet: bool = True


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: SearchQuery
    results: List[SearchResult]
    request_id: Optional[str] = None
    provider: Literal["baidu"] = BAIDU_SEARCH_PROVIDER
    searched_at: datetime
    from_cache: bool = False


class SearchProvider(Protocol):
    def search(self, query: SearchQuery) -> SearchResponse:
        ...


class SearchBudget(BaseModel):
    """由 Python Runtime 强制执行的本地调用预算。"""

    model_config = ConfigDict(validate_assignment=True)

    max_search_rounds: int = Field(default=10, ge=1, le=10)
    max_search_calls: int = Field(default=10, ge=1, le=10)
    max_search_requests: int = Field(default=10, ge=1, le=10)
    max_results_per_request: int = Field(default=5, ge=1, le=10)
    max_fetches: int = Field(default=20, ge=1, le=20)
    max_extractions: int = Field(default=20, ge=1, le=20)
    max_total_tool_calls: int = Field(default=60, ge=1, le=60)
    max_agent_turns: int = Field(default=70, ge=2, le=70)
    # 兼容旧CLI显式参数：提供时仍表示“首轮之后最多补充多少次搜索调用”。
    # 新代码使用max_search_rounds和max_search_calls分别约束轮次与调用数。
    max_supplemental_rounds: Optional[int] = Field(default=None, ge=0, le=9)
    search_rounds: int = 0
    search_tool_calls: int = 0
    search_api_requests: int = 0
    fetch_tool_calls: int = 0
    extraction_tool_calls: int = 0
    total_tool_calls: int = 0
    last_search_round_id: Optional[int] = Field(default=None, exclude=True)

    def begin_tool_call(self, tool_name: str) -> None:
        """在任何Function Tool执行前统一强制总预算。"""

        if self.total_tool_calls >= self.max_total_tool_calls:
            raise SearchError(
                "total_tool_budget_exceeded",
                f"单次任务最多允许 {self.max_total_tool_calls} 次Function Tool调用",
            )
        self.total_tool_calls += 1

    def record_search_round(self, round_id: int) -> None:
        """同一模型响应内的并行搜索计为一轮。"""

        if self.last_search_round_id == round_id:
            return
        if self.search_rounds >= self.max_search_rounds:
            raise SearchError(
                "search_round_limit_exceeded",
                f"单次任务最多允许 {self.max_search_rounds} 轮检索",
            )
        self.search_rounds += 1
        self.last_search_round_id = round_id

    def check_query(
        self, query: SearchQuery, *, cache_hit: bool, external_request: bool = True
    ) -> None:
        if query.max_results > self.max_results_per_request:
            raise SearchError(
                "search_result_limit_exceeded",
                f"单次最多返回 {self.max_results_per_request} 条搜索结果",
            )
        if self.search_tool_calls >= self.max_search_calls:
            raise SearchError(
                "search_call_limit_exceeded",
                f"单次任务最多允许 {self.max_search_calls} 次search_web调用",
            )
        next_tool_calls = self.search_tool_calls + 1
        if (
            self.max_supplemental_rounds is not None
            and next_tool_calls - 1 > self.max_supplemental_rounds
        ):
            raise SearchError(
                "supplemental_search_limit_exceeded",
                f"最多允许 {self.max_supplemental_rounds} 轮补充检索",
            )
        if (
            not cache_hit
            and external_request
            and self.search_api_requests >= self.max_search_requests
        ):
            raise SearchError(
                "search_budget_exceeded",
                f"单次任务最多允许 {self.max_search_requests} 次百度搜索 API 请求",
            )

    def record_search(
        self, *, cache_hit: bool, external_request: bool = True
    ) -> None:
        self.search_tool_calls += 1
        if not cache_hit and external_request:
            self.search_api_requests += 1

    def record_fetch(self, *, cache_hit: bool) -> None:
        # 即使命中缓存，也限制模型反复调用相同工具。
        if self.fetch_tool_calls >= self.max_fetches:
            raise SearchError(
                "fetch_budget_exceeded",
                f"单次任务最多读取 {self.max_fetches} 个网页或 PDF",
            )
        self.fetch_tool_calls += 1

    def record_extraction(self) -> None:
        if self.extraction_tool_calls >= self.max_extractions:
            raise SearchError(
                "evidence_extraction_budget_exceeded",
                f"单次任务最多执行 {self.max_extractions} 次证据提取",
            )
        self.extraction_tool_calls += 1

    @property
    def estimated_search_cost_cny(self) -> float:
        return round(self.search_api_requests * BAIDU_SEARCH_CALL_PRICE_CNY, 6)


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return url.strip()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


class SearchCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    @staticmethod
    def _key(query: SearchQuery) -> str:
        payload = query.model_dump(mode="json")
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def path_for(self, query: SearchQuery) -> Path:
        return self.cache_dir / f"{self._key(query)}.json"

    def get(self, query: SearchQuery) -> Optional[SearchResponse]:
        path = self.path_for(query)
        if not path.is_file():
            return None
        try:
            cached = SearchResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if cached.query != query:
            return None
        return cached.model_copy(update={"from_cache": True})

    def put(self, response: SearchResponse) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(response.query)
        content = response.model_copy(update={"from_cache": False}).model_dump_json(
            indent=2
        ) + "\n"
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
        return path


class SearchService:
    """统一执行缓存、预算与 Provider 调用。"""

    def __init__(
        self,
        provider: SearchProvider,
        cache: SearchCache,
        budget: SearchBudget,
        *,
        external_requests: bool = True,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.budget = budget
        self.external_requests = external_requests

    def search(self, query: SearchQuery) -> SearchResponse:
        cached = self.cache.get(query)
        self.budget.check_query(
            query,
            cache_hit=cached is not None,
            external_request=self.external_requests,
        )
        if cached is not None:
            self.budget.record_search(
                cache_hit=True, external_request=self.external_requests
            )
            return cached
        # 请求发出前即消耗本地预算；鉴权失败、限流和超时也可能
        # 占用供应商配额，不得因未取得成功响应而重复绕过预算。
        self.budget.record_search(
            cache_hit=False, external_request=self.external_requests
        )
        response = self.provider.search(query)
        self.cache.put(response)
        return response


class BaiduSearchProvider:
    """百度智能云官方百度搜索 API Provider。"""

    RETRYABLE_STATUS_CODES = {408, 429, 500, 501, 502, 503, 504}

    def __init__(
        self,
        config: BaiduSearchConfig,
        *,
        session: Optional[requests.Session] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if config.mode is not SearchMode.LIVE:
            raise SearchError(
                "live_provider_in_mock_mode",
                "Mock 配置不得创建真实百度搜索网络 Provider",
            )
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build_request(self, query: SearchQuery) -> tuple[Dict[str, str], Dict[str, Any]]:
        if self.config.api_key is None:
            raise SearchError("missing_search_api_key", "百度搜索 API Key 未配置")
        headers = {
            "Content-Type": "application/json",
            self.config.auth_header.value: (
                "Bearer " + self.config.api_key.get_secret_value()
            ),
        }
        payload: Dict[str, Any] = {
            "messages": [{"role": "user", "content": query.query}],
            "edition": self.config.edition,
            "search_source": "baidu_search_v2",
            "resource_type_filter": [
                {"type": "web", "top_k": query.max_results}
            ],
        }
        if query.domain_filter:
            payload["search_filter"] = {
                "match": {"site": list(query.domain_filter)}
            }
        if query.recency is not None:
            payload["search_recency_filter"] = query.recency.value
        return headers, payload

    def search(self, query: SearchQuery) -> SearchResponse:
        headers, payload = self.build_request(query)
        try:
            response = self.session.post(
                self.config.base_url,
                headers=headers,
                json=payload,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.Timeout as exc:
            raise SearchError("search_timeout", "百度搜索请求超时", retryable=True) from exc
        except requests.RequestException as exc:
            raise SearchError(
                "search_network_error", "百度搜索网络请求失败", retryable=True
            ) from exc

        try:
            if response.status_code in (401, 403):
                raise SearchError("search_authentication_failed", "百度搜索鉴权失败")
            if response.status_code == 429:
                raise SearchError("search_rate_limited", "百度搜索请求被限流", retryable=True)
            if not 200 <= response.status_code < 300:
                raise SearchError(
                    "search_http_error",
                    f"百度搜索返回 HTTP {response.status_code}",
                    retryable=response.status_code in self.RETRYABLE_STATUS_CODES,
                )
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise SearchError("invalid_search_response", "百度搜索响应不是有效 JSON") from exc
        finally:
            response.close()

        if body.get("code") not in (None, 0, "0"):
            code_text = str(body.get("code"))
            raise SearchError(
                "search_provider_error",
                f"百度搜索返回错误码 {code_text}：{body.get('message') or '未提供错误说明'}",
            )
        return self._convert_response(query, body)

    def _convert_response(
        self, query: SearchQuery, payload: Dict[str, Any]
    ) -> SearchResponse:
        searched_at = self.clock()
        results: List[SearchResult] = []
        seen_urls: set[str] = set()
        for item in payload.get("references") or []:
            if item.get("type") != "web":
                continue
            raw_url = str(item.get("url") or "").strip()
            canonical_url = canonicalize_url(raw_url)
            parsed = urlsplit(canonical_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                continue
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            published_at = self._parse_date(item.get("date"))
            results.append(
                SearchResult(
                    result_id=f"R{len(results) + 1:03d}",
                    title=str(item.get("title") or item.get("web_anchor") or parsed.hostname),
                    url=canonical_url,
                    snippet=str(item.get("content") or "")[:2000],
                    source_domain=parsed.hostname.lower(),
                    published_at=published_at,
                    searched_at=searched_at,
                    provider_reference_id=item.get("id"),
                    authority_score=item.get("authority_score"),
                    rerank_score=item.get("rerank_score"),
                )
            )
            if len(results) >= query.max_results:
                break
        return SearchResponse(
            query=query,
            results=results,
            request_id=payload.get("request_id"),
            searched_at=searched_at,
        )

    @staticmethod
    def _parse_date(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


class FixtureSearchProvider:
    """离线 Mock Provider；只返回显式传入的 fixture，不访问网络。"""

    def __init__(
        self,
        results: List[Dict[str, Any]],
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.results = results
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.call_count = 0

    def search(self, query: SearchQuery) -> SearchResponse:
        self.call_count += 1
        searched_at = self.clock()
        converted: List[SearchResult] = []
        seen: set[str] = set()
        for item in self.results:
            url = canonicalize_url(str(item["url"]))
            domain = (urlsplit(url).hostname or "").lower()
            if query.domain_filter and not any(
                domain == allowed or domain.endswith(f".{allowed}")
                for allowed in query.domain_filter
            ):
                continue
            if url in seen:
                continue
            seen.add(url)
            raw_published = item.get("published_at")
            published_at = (
                BaiduSearchProvider._parse_date(raw_published)
                if raw_published
                else None
            )
            converted.append(
                SearchResult(
                    result_id=f"R{len(converted) + 1:03d}",
                    title=str(item.get("title") or domain),
                    url=url,
                    snippet=str(item.get("snippet") or "")[:2000],
                    source_domain=domain,
                    published_at=published_at,
                    searched_at=searched_at,
                    provider_reference_id=item.get("provider_reference_id"),
                    authority_score=item.get("authority_score"),
                    rerank_score=item.get("rerank_score"),
                )
            )
            if len(converted) >= query.max_results:
                break
        return SearchResponse(
            query=query,
            results=converted,
            request_id=f"MOCK-{self.call_count:03d}",
            searched_at=searched_at,
        )
