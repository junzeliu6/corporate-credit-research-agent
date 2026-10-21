"""年度报告 PDF 的逐页文本提取、质量摘要与定位样例。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

import pdfplumber
from pydantic import BaseModel

from credit_research_agent.documents import (
    DownloadResult,
    ExtractionArtifacts,
    ExtractionSample,
    PageExtractionStatus,
    ParsedDocument,
    ParsedPage,
    PdfExtractionSummary,
    PdfParseFailureRecord,
    TextLocatorMatch,
)
from credit_research_agent.downloader import calculate_sha256


DEFAULT_SAMPLE_QUERIES = ("公司信息", "合并资产负债表", "合并利润表")


class PdfExtractionError(RuntimeError):
    """带机器可读错误码的 PDF 提取失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _model_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def _normalise_page_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


class PdfTextExtractor:
    """使用 pdfplumber 逐页提取文本，页码采用 PDF 物理页的 1-based 编号。"""

    def __init__(
        self,
        *,
        open_pdf: Callable[..., Any] = pdfplumber.open,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._open_pdf = open_pdf
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def parse(self, download: DownloadResult) -> ParsedDocument:
        source_file = Path(download.file_path)
        if not source_file.is_file():
            raise PdfExtractionError(
                "source_file_missing", f"待解析 PDF 不存在：{source_file}"
            )

        actual_sha256 = calculate_sha256(source_file)
        if actual_sha256 != download.sha256:
            raise PdfExtractionError(
                "source_hash_mismatch",
                "PDF 的实际 SHA-256 与下载清单不一致，拒绝解析可能损坏或被替换的文件",
            )

        with source_file.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise PdfExtractionError("invalid_pdf", "文件缺少有效的 PDF 文件头")

        pages: List[ParsedPage] = []
        try:
            with self._open_pdf(str(source_file)) as pdf:
                total_pages = len(pdf.pages)
                if total_pages < 1:
                    raise PdfExtractionError("empty_pdf", "PDF 不包含任何页面")

                for page_number, page in enumerate(pdf.pages, start=1):
                    try:
                        raw_text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                        text = _normalise_page_text(raw_text)
                        status = (
                            PageExtractionStatus.EXTRACTED
                            if text
                            else PageExtractionStatus.EMPTY
                        )
                        pages.append(
                            ParsedPage(
                                page_number=page_number,
                                text=text,
                                char_count=len(text),
                                status=status,
                            )
                        )
                    except Exception as exc:  # 单页失败不应丢失其他 228 页
                        pages.append(
                            ParsedPage(
                                page_number=page_number,
                                text="",
                                char_count=0,
                                status=PageExtractionStatus.FAILED,
                                error_message=str(exc),
                            )
                        )
        except PdfExtractionError:
            raise
        except Exception as exc:
            raise PdfExtractionError(
                "pdf_open_error", f"PDF 打开或解析失败：{exc}"
            ) from exc

        if not any(
            page.status is PageExtractionStatus.EXTRACTED for page in pages
        ):
            raise PdfExtractionError(
                "no_text_extracted",
                "所有页面均未提取到文本，可能需要 OCR 或人工核查",
            )

        return ParsedDocument(
            source_id=download.source_id,
            source_file=source_file.resolve(),
            source_sha256=download.sha256,
            parser_name="pdfplumber",
            parser_version=pdfplumber.__version__,
            parsed_at=self._clock(),
            total_pages=total_pages,
            pages=pages,
        )


def locate_terms(
    document: ParsedDocument,
    queries: Iterable[str],
) -> ExtractionSample:
    """为每个查询词保留第一个精确文本匹配及其页码、行号。"""

    matches: List[TextLocatorMatch] = []
    missing_queries: List[str] = []
    for query in queries:
        match: Optional[TextLocatorMatch] = None
        for page in document.pages:
            if page.status is not PageExtractionStatus.EXTRACTED:
                continue
            for line_number, line in enumerate(page.text.splitlines(), start=1):
                if query in line:
                    match = TextLocatorMatch(
                        query=query,
                        page_number=page.page_number,
                        line_number=line_number,
                        matched_line=line.strip()[:200],
                    )
                    break
            if match is not None:
                break
        if match is None:
            missing_queries.append(query)
        else:
            matches.append(match)

    return ExtractionSample(
        source_id=document.source_id,
        source_sha256=document.source_sha256,
        page_numbering=document.page_numbering,
        created_at=document.parsed_at,
        matches=matches,
        missing_queries=missing_queries,
    )


def build_summary(document: ParsedDocument) -> PdfExtractionSummary:
    pages_with_text = sum(
        page.status is PageExtractionStatus.EXTRACTED for page in document.pages
    )
    empty_pages = sum(
        page.status is PageExtractionStatus.EMPTY for page in document.pages
    )
    failed_pages = sum(
        page.status is PageExtractionStatus.FAILED for page in document.pages
    )
    warnings: List[str] = []
    if empty_pages:
        warnings.append(f"{empty_pages} 页未提取到文本，可能需要 OCR 或人工核查")
    if failed_pages:
        warnings.append(f"{failed_pages} 页提取失败，需检查逐页错误信息")

    return PdfExtractionSummary(
        source_id=document.source_id,
        source_file=document.source_file,
        source_sha256=document.source_sha256,
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        parsed_at=document.parsed_at,
        page_numbering=document.page_numbering,
        total_pages=document.total_pages,
        pages_with_text=pages_with_text,
        empty_pages=empty_pages,
        failed_pages=failed_pages,
        total_characters=sum(page.char_count for page in document.pages),
        text_coverage_ratio=pages_with_text / document.total_pages,
        warnings=warnings,
    )


class ExtractionArtifactWriter:
    """把全文、质量摘要和小型定位样例分别持久化。"""

    def write(
        self,
        document: ParsedDocument,
        output_dir: Path,
        *,
        queries: Sequence[str] = DEFAULT_SAMPLE_QUERIES,
        sample_output: Optional[Path] = None,
    ) -> ExtractionArtifacts:
        source_dir = Path(output_dir) / document.source_id
        source_dir.mkdir(parents=True, exist_ok=True)
        summary_path = source_dir / "summary.json"
        pages_path = source_dir / "pages.jsonl"
        sample_path = Path(sample_output) if sample_output else source_dir / "sample.json"
        sample_path.parent.mkdir(parents=True, exist_ok=True)

        sample = locate_terms(document, queries)
        self._write_json_lines(pages_path, document.pages)
        summary = build_summary(document).copy(
            update={"pages_sha256": calculate_sha256(pages_path)}
        )
        self._write_json(summary_path, summary)
        self._write_json(sample_path, sample)

        return ExtractionArtifacts(
            source_id=document.source_id,
            summary_path=summary_path.resolve(),
            pages_path=pages_path.resolve(),
            sample_path=sample_path.resolve(),
            total_pages=document.total_pages,
            pages_with_text=summary.pages_with_text,
            failed_pages=summary.failed_pages,
        )

    @staticmethod
    def write_failure(path: Path, failure: PdfParseFailureRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ExtractionArtifactWriter._write_json(path, failure)

    @staticmethod
    def _write_json(path: Path, model: BaseModel) -> None:
        content = json.dumps(_model_dict(model), ensure_ascii=False, indent=2) + "\n"
        ExtractionArtifactWriter._atomic_write(path, content)

    @staticmethod
    def _write_json_lines(path: Path, models: Iterable[BaseModel]) -> None:
        content = "".join(
            json.dumps(_model_dict(model), ensure_ascii=False) + "\n"
            for model in models
        )
        ExtractionArtifactWriter._atomic_write(path, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
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
                temp_file.write(content)
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def extract_to_artifacts(
    download: DownloadResult,
    output_dir: Path,
    *,
    sample_output: Optional[Path] = None,
    queries: Sequence[str] = DEFAULT_SAMPLE_QUERIES,
    extractor: Optional[PdfTextExtractor] = None,
    writer: Optional[ExtractionArtifactWriter] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> ExtractionArtifacts:
    """执行完整 V0.3 流程，并把整体失败写入独立记录。"""

    extractor = extractor or PdfTextExtractor(clock=clock)
    writer = writer or ExtractionArtifactWriter()
    source_dir = Path(output_dir) / download.source_id
    failure_path = source_dir / "failure.json"
    failure_clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        document = extractor.parse(download)
        artifacts = writer.write(
            document,
            output_dir,
            queries=queries,
            sample_output=sample_output,
        )
        if failure_path.exists():
            failure_path.unlink()
        return artifacts
    except PdfExtractionError as error:
        failure = PdfParseFailureRecord(
            source_id=download.source_id,
            source_file=download.file_path,
            attempted_at=failure_clock(),
            error_code=error.code,
            error_message=str(error),
        )
        writer.write_failure(failure_path, failure)
        raise

