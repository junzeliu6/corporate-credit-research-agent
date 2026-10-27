"""PDF 下载与后续解析实现之间的接口契约。

V0.2 实现下载结果与下载器接口，PDF 文本与页码提取仍属于 V0.3。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Protocol

from pydantic.v1 import BaseModel, Field

from credit_research_agent.models import SourceRecord


class DownloadResult(BaseModel):
    """一次成功下载的可审计结果。"""

    source_id: str
    source_title: str
    source_url: str
    published_at: date | None = None
    report_period: str | None = None
    file_path: Path
    manifest_path: Path
    accessed_at: datetime
    final_url: str
    http_status: int
    content_type: str | None = None
    bytes_written: int = Field(ge=0)
    sha256: str
    from_cache: bool = False


class DocumentBindingError(ValueError):
    """来源目录、下载清单或本地文件不是同一份文档。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_download_binding(
    source: SourceRecord,
    download: DownloadResult,
    *,
    verify_file_hash: bool = True,
) -> None:
    """校验任务来源与下载文件，防止跨企业复用同名 ``S01``。

    Source ID 只是任务内编号，因此必须同时核对来源元数据与文件内容。调用方仍需
    单独校验 ``source.entity_id`` 与当前研究主体。
    """

    comparisons = (
        ("source_id", download.source_id, source.source_id),
        ("source_title", download.source_title, source.title),
        ("source_url", download.source_url, str(source.url)),
        ("published_at", download.published_at, source.published_at),
        ("report_period", download.report_period, source.report_period),
    )
    for field_name, actual, expected in comparisons:
        if actual != expected:
            raise DocumentBindingError(
                "download_source_metadata_mismatch",
                f"下载结果的 {field_name} 与来源目录不一致",
            )
    file_path = Path(download.file_path)
    if not file_path.is_file():
        raise DocumentBindingError("download_file_missing", "下载清单对应的本地文件不存在")
    if verify_file_hash:
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != download.sha256:
            raise DocumentBindingError(
                "download_file_hash_mismatch",
                "本地PDF内容与下载清单哈希不一致",
            )


class DownloadFailureRecord(BaseModel):
    """失败也要落盘，便于区分未执行与执行失败。"""

    source_id: str
    source_title: str
    source_url: str
    attempted_at: datetime
    error_code: str
    error_message: str
    attempts: int = Field(ge=1)


class ParsedPage(BaseModel):
    """保留页码的单页文本，为后续证据定位提供基础。"""

    page_number: int = Field(ge=1)
    text: str
    char_count: int = Field(ge=0)
    status: "PageExtractionStatus"
    error_message: Optional[str] = None


class PageExtractionStatus(str, Enum):
    EXTRACTED = "extracted"
    EMPTY = "empty"
    FAILED = "failed"


class ParsedDocument(BaseModel):
    """解析结果；parser_name 用于复现实验和排查解析差异。"""

    source_id: str
    source_file: Path
    source_sha256: str
    parser_name: str
    parser_version: str
    parsed_at: datetime
    page_numbering: str = "pdf_physical_page_1_based"
    total_pages: int = Field(ge=1)
    pages: List[ParsedPage]


class TextLocatorMatch(BaseModel):
    """可回到原 PDF 核对的最小章节定位。"""

    query: str
    page_number: int = Field(ge=1)
    line_number: int = Field(ge=1)
    matched_line: str


class ExtractionSample(BaseModel):
    source_id: str
    source_sha256: str
    page_numbering: str
    created_at: datetime
    matches: List[TextLocatorMatch]
    missing_queries: List[str]


class PdfExtractionSummary(BaseModel):
    source_id: str
    source_file: Path
    source_sha256: str
    pages_sha256: Optional[str] = None
    parser_name: str
    parser_version: str
    parsed_at: datetime
    page_numbering: str
    total_pages: int = Field(ge=1)
    pages_with_text: int = Field(ge=0)
    empty_pages: int = Field(ge=0)
    failed_pages: int = Field(ge=0)
    total_characters: int = Field(ge=0)
    text_coverage_ratio: float = Field(ge=0, le=1)
    warnings: List[str]


class ExtractionArtifacts(BaseModel):
    source_id: str
    summary_path: Path
    pages_path: Path
    sample_path: Path
    total_pages: int
    pages_with_text: int
    failed_pages: int


class PdfParseFailureRecord(BaseModel):
    source_id: str
    source_file: Path
    attempted_at: datetime
    error_code: str
    error_message: str


class DocumentDownloader(Protocol):
    """V0.2 下载器应实现的同步接口。"""

    def download(
        self,
        source: SourceRecord,
        destination_dir: Path,
        *,
        force: bool = False,
    ) -> DownloadResult:
        ...


class DocumentParser(Protocol):
    """V0.3 PDF 解析器应实现的接口。"""

    def parse(self, download: DownloadResult) -> ParsedDocument:
        ...


ParsedPage.update_forward_refs(PageExtractionStatus=PageExtractionStatus)

