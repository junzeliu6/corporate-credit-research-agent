"""受控的官方年度报告 PDF 下载、缓存和审计元数据。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

import requests
from pydantic.v1 import BaseModel, ValidationError

from credit_research_agent.documents import DownloadFailureRecord, DownloadResult
from credit_research_agent.models import SourceRecord, SourceType


class DownloadError(RuntimeError):
    """带机器可读错误码的下载失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts


@dataclass(frozen=True)
class DownloadConfig:
    """下载边界；默认值适用于单份上市公司年报。"""

    timeout_seconds: float = 30.0
    max_bytes: int = 100 * 1024 * 1024
    max_attempts: int = 3
    retry_backoff_seconds: float = 0.5
    chunk_size: int = 64 * 1024
    allowed_domains: Sequence[str] = (
        "cninfo.com.cn",
        "szse.cn",
        "yiling.cn",
    )
    user_agent: str = "CorporateCreditResearchAgent/0.2 (public-disclosure-research)"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes 必须大于 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")


def _model_json(model: BaseModel) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json(indent=2)  # type: ignore[attr-defined,no-any-return]
    return model.json(ensure_ascii=False, indent=2)


def calculate_sha256(path: Path, chunk_size: int = 64 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OfficialPdfDownloader:
    """只下载登记过的官方年度报告 PDF。

    安全边界包括 HTTPS、官方域名白名单、重定向复核、超时、大小限制、
    PDF 文件头验证与临时文件原子替换。成功和失败均保留结构化记录。
    """

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        config: Optional[DownloadConfig] = None,
        session: Optional[requests.Session] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or DownloadConfig()
        self.session = session or requests.Session()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep

    def download(
        self,
        source: SourceRecord,
        destination_dir: Path,
        *,
        force: bool = False,
    ) -> DownloadResult:
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        file_path = destination_dir / f"{source.source_id}.pdf"
        manifest_path = destination_dir / f"{source.source_id}.metadata.json"
        failure_path = destination_dir / f"{source.source_id}.failure.json"

        try:
            self._validate_source(source)
        except DownloadError as error:
            self._write_failure(source, failure_path, error)
            raise

        if not force:
            cached = self._read_valid_cache(source, file_path, manifest_path)
            if cached is not None:
                return cached

        last_error: Optional[DownloadError] = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                result = self._download_once(source, file_path, manifest_path)
                if failure_path.exists():
                    failure_path.unlink()
                return result
            except requests.RequestException as exc:
                last_error = DownloadError(
                    "network_error",
                    f"网络请求失败：{exc}",
                    retryable=True,
                    attempts=attempt,
                )
            except OSError as exc:
                last_error = DownloadError(
                    "filesystem_error",
                    f"缓存文件读写失败：{exc}",
                    attempts=attempt,
                )
            except DownloadError as exc:
                exc.attempts = attempt
                last_error = exc

            if not last_error.retryable or attempt == self.config.max_attempts:
                break
            self._sleep(self.config.retry_backoff_seconds * attempt)

        assert last_error is not None
        self._write_failure(source, failure_path, last_error)
        raise last_error

    def _download_once(
        self,
        source: SourceRecord,
        file_path: Path,
        manifest_path: Path,
    ) -> DownloadResult:
        response = self.session.get(
            str(source.url),
            headers={
                "Accept": "application/pdf",
                "User-Agent": self.config.user_agent,
            },
            timeout=self.config.timeout_seconds,
            stream=True,
            allow_redirects=True,
        )
        temp_path: Optional[Path] = None
        try:
            status_code = response.status_code
            if not 200 <= status_code < 300:
                raise DownloadError(
                    "http_error",
                    f"下载返回 HTTP {status_code}",
                    retryable=status_code in self.RETRYABLE_STATUS_CODES,
                )

            final_url = str(response.url)
            self._validate_url(final_url, context="重定向后的 URL")

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > self.config.max_bytes:
                    raise DownloadError(
                        "file_too_large",
                        f"文件声明大小 {declared_size} 字节，超过限制 {self.config.max_bytes} 字节",
                    )

            digest = hashlib.sha256()
            bytes_written = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{source.source_id}-",
                suffix=".part",
                dir=file_path.parent,
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > self.config.max_bytes:
                        raise DownloadError(
                            "file_too_large",
                            f"实际下载大小超过限制 {self.config.max_bytes} 字节",
                        )
                    temp_file.write(chunk)
                    digest.update(chunk)

            with temp_path.open("rb") as downloaded_file:
                if downloaded_file.read(5) != b"%PDF-":
                    raise DownloadError(
                        "invalid_pdf",
                        "响应内容不是有效的 PDF 文件头，可能是错误页或访问限制页面",
                    )

            temp_path.replace(file_path)
            temp_path = None
            content_type = response.headers.get("Content-Type")
            result = DownloadResult(
                source_id=source.source_id,
                source_title=source.title,
                source_url=str(source.url),
                published_at=source.published_at,
                report_period=source.report_period,
                file_path=file_path.resolve(),
                manifest_path=manifest_path.resolve(),
                accessed_at=self._clock(),
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                bytes_written=bytes_written,
                sha256=digest.hexdigest(),
                from_cache=False,
            )
            self._write_model(manifest_path, result)
            return result
        finally:
            response.close()
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _read_valid_cache(
        self,
        source: SourceRecord,
        file_path: Path,
        manifest_path: Path,
    ) -> Optional[DownloadResult]:
        if not file_path.is_file() or not manifest_path.is_file():
            return None
        try:
            manifest = DownloadResult.parse_raw(manifest_path.read_text(encoding="utf-8"))
            cache_is_valid = (
                manifest.source_id == source.source_id
                and manifest.source_title == source.title
                and manifest.source_url == str(source.url)
                and manifest.sha256 == calculate_sha256(file_path)
            )
        except (OSError, ValidationError, ValueError, json.JSONDecodeError):
            return None
        if not cache_is_valid:
            return None

        return manifest.copy(
            update={
                "file_path": file_path.resolve(),
                "manifest_path": manifest_path.resolve(),
                "from_cache": True,
            }
        )

    def _validate_source(self, source: SourceRecord) -> None:
        if source.source_type is not SourceType.ANNUAL_REPORT:
            raise DownloadError(
                "unsupported_source_type",
                f"V0.2 仅下载年度报告，当前类型为 {source.source_type.value}",
            )
        self._validate_url(str(source.url), context="来源 URL")

    def _validate_url(self, url: str, *, context: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            raise DownloadError("unsafe_url", f"{context} 必须使用 HTTPS")
        if parsed.username or parsed.password:
            raise DownloadError("unsafe_url", f"{context} 不得包含认证信息")
        host = (parsed.hostname or "").lower()
        allowed = any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.config.allowed_domains
        )
        if not allowed:
            raise DownloadError("untrusted_domain", f"{context} 不在官方域名白名单：{host}")

    def _write_failure(
        self,
        source: SourceRecord,
        failure_path: Path,
        error: DownloadError,
    ) -> None:
        record = DownloadFailureRecord(
            source_id=source.source_id,
            source_title=source.title,
            source_url=str(source.url),
            attempted_at=self._clock(),
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )
        self._write_model(failure_path, record)

    @staticmethod
    def _write_model(path: Path, model: BaseModel) -> None:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(_model_json(model))
                temp_file.write("\n")
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

