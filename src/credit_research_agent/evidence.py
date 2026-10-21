"""从已校验的逐页文本构建事实、证据与来源引用。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from credit_research_agent.documents import (
    DownloadResult,
    PageExtractionStatus,
    ParsedPage,
    PdfExtractionSummary,
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
    SourceRecord,
    SourceStatus,
    VerificationMethod,
)


class EvidenceBuildError(RuntimeError):
    """带机器可读错误码的证据构建失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceSeed(BaseModel):
    evidence_id: str
    entity_id: str
    source_id: str
    page_number: int
    page_or_section: str
    match_text: str


class FactSeed(BaseModel):
    fact_id: str
    entity_id: str
    category: FactCategory
    field_name: str
    value: str
    claim: str
    entity_scope: EntityScope
    period: Optional[str] = None
    evidence_ids: List[str]


class EvidenceSeedCatalog(BaseModel):
    evidence: List[EvidenceSeed]
    facts: List[FactSeed]


class EvidenceArtifacts(BaseModel):
    entity_id: str
    evidence_path: Path
    citations_path: Path
    fact_count: int
    evidence_count: int
    source_count: int


class EvidenceBuildFailureRecord(BaseModel):
    entity_id: str
    source_id: str
    attempted_at: datetime
    error_code: str
    error_message: str


def _model_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def load_default_seed_catalog() -> EvidenceSeedCatalog:
    path = resources.files("credit_research_agent.data").joinpath(
        "evidence_seeds.json"
    )
    return EvidenceSeedCatalog.parse_raw(path.read_text(encoding="utf-8"))


def load_parsed_pages(path: Path) -> Dict[int, ParsedPage]:
    pages: Dict[int, ParsedPage] = {}
    try:
        with Path(path).open("r", encoding="utf-8") as file_handle:
            for raw_line in file_handle:
                if not raw_line.strip():
                    continue
                page = ParsedPage.parse_raw(raw_line)
                if page.page_number in pages:
                    raise EvidenceBuildError(
                        "duplicate_page", f"重复的 PDF 页码：{page.page_number}"
                    )
                pages[page.page_number] = page
    except EvidenceBuildError:
        raise
    except Exception as exc:
        raise EvidenceBuildError(
            "invalid_pages_artifact", f"无法读取逐页文本：{exc}"
        ) from exc
    if not pages:
        raise EvidenceBuildError("empty_pages_artifact", "逐页文本文件为空")
    return pages


class EvidenceBuilder:
    """用预先审阅的精确文本锚点生成最小事实证据链。"""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        identity: CompanyIdentity,
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
        seeds: Optional[EvidenceSeedCatalog] = None,
    ) -> EvidenceBundle:
        seeds = seeds or load_default_seed_catalog()
        summary = self._load_and_validate_summary(
            source, download, summary_path, pages_path
        )
        pages = load_parsed_pages(pages_path)

        evidence_records: List[EvidenceRecord] = []
        relevant_evidence_seeds = [
            item
            for item in seeds.evidence
            if item.entity_id == identity.entity_id and item.source_id == source.source_id
        ]
        if not relevant_evidence_seeds:
            raise EvidenceBuildError(
                "missing_evidence_seeds", "当前主体和来源没有证据锚点配置"
            )

        for seed in relevant_evidence_seeds:
            page = pages.get(seed.page_number)
            if page is None:
                raise EvidenceBuildError(
                    "page_not_found", f"证据 {seed.evidence_id} 指向不存在的第 {seed.page_number} 页"
                )
            if page.status is not PageExtractionStatus.EXTRACTED:
                raise EvidenceBuildError(
                    "page_not_extracted",
                    f"证据 {seed.evidence_id} 所在第 {seed.page_number} 页未成功提取",
                )

            matched_line = None
            matched_line_number = None
            for line_number, line in enumerate(page.text.splitlines(), start=1):
                if line.strip() == seed.match_text:
                    matched_line = line.strip()
                    matched_line_number = line_number
                    break
            if matched_line is None or matched_line_number is None:
                raise EvidenceBuildError(
                    "evidence_text_mismatch",
                    f"证据 {seed.evidence_id} 未在 PDF 第 {seed.page_number} 页找到预期原文",
                )

            evidence_records.append(
                EvidenceRecord(
                    evidence_id=seed.evidence_id,
                    source_id=source.source_id,
                    source_sha256=summary.source_sha256,
                    page_number=seed.page_number,
                    line_number=matched_line_number,
                    page_or_section=seed.page_or_section,
                    quote=matched_line,
                    status=EvidenceStatus.VERIFIED,
                    verification_method=VerificationMethod.EXACT_TEXT_MATCH,
                )
            )

        evidence_by_id = {item.evidence_id: item for item in evidence_records}
        fact_records: List[FactRecord] = []
        for seed in seeds.facts:
            if seed.entity_id != identity.entity_id:
                continue
            identity_value = getattr(identity, seed.field_name, None)
            if identity_value != seed.value:
                raise EvidenceBuildError(
                    "identity_value_mismatch",
                    f"事实 {seed.fact_id} 的值与主体登记字段 {seed.field_name} 不一致",
                )
            missing = set(seed.evidence_ids) - set(evidence_by_id)
            if missing:
                raise EvidenceBuildError(
                    "missing_evidence_reference",
                    f"事实 {seed.fact_id} 缺少证据：{sorted(missing)}",
                )
            if not any(
                seed.value in evidence_by_id[evidence_id].quote
                for evidence_id in seed.evidence_ids
            ):
                raise EvidenceBuildError(
                    "fact_value_not_in_evidence",
                    f"事实 {seed.fact_id} 的值未出现在对应证据原文中",
                )
            fact_records.append(
                FactRecord(
                    fact_id=seed.fact_id,
                    entity_id=seed.entity_id,
                    category=seed.category,
                    field_name=seed.field_name,
                    value=seed.value,
                    claim=seed.claim,
                    entity_scope=seed.entity_scope,
                    period=seed.period,
                    evidence_ids=seed.evidence_ids,
                )
            )

        referenced_evidence_ids = sorted(
            {evidence_id for fact in fact_records for evidence_id in fact.evidence_ids}
        )
        verified_identity = identity.copy(
            update={"evidence_ids": referenced_evidence_ids}
        )
        processed_source = source.copy(
            update={
                "status": SourceStatus.PARSED,
                "accessed_at": download.accessed_at,
                "local_path": Path(download.file_path).name,
                "page_or_section": "PDF第6页",
            }
        )
        return EvidenceBundle(
            identity=verified_identity,
            generated_at=self._clock(),
            facts=fact_records,
            evidence=evidence_records,
            sources=[processed_source],
            limitations=[
                "当前仅验证上市主体身份字段，尚未构建业务、股权、行业或财务事实。",
                "verified 表示与指定 PDF 页面的提取原文精确匹配，不代表替代人工尽调或审计确认。",
            ],
        )

    @staticmethod
    def _load_and_validate_summary(
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
    ) -> PdfExtractionSummary:
        try:
            summary = PdfExtractionSummary.parse_raw(
                Path(summary_path).read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise EvidenceBuildError(
                "invalid_extraction_summary", f"无法读取解析摘要：{exc}"
            ) from exc
        if summary.source_id != source.source_id or download.source_id != source.source_id:
            raise EvidenceBuildError("source_id_mismatch", "来源编号在下载、解析和目录中不一致")
        if summary.source_sha256 != download.sha256:
            raise EvidenceBuildError("source_hash_mismatch", "解析摘要与下载清单的 PDF 哈希不一致")
        if not summary.pages_sha256:
            raise EvidenceBuildError(
                "missing_pages_hash", "解析摘要缺少 pages.jsonl 的 SHA-256，请重新执行 V0.3"
            )
        if calculate_sha256(Path(pages_path)) != summary.pages_sha256:
            raise EvidenceBuildError(
                "pages_hash_mismatch", "pages.jsonl 与解析摘要记录的哈希不一致"
            )
        return summary


def render_citations(bundle: EvidenceBundle) -> str:
    """按来源编号渲染可直接放入报告附录的 Markdown 引用。"""

    lines = ["# 来源引用", ""]
    for source in sorted(bundle.sources, key=lambda item: item.source_id):
        pages = sorted(
            {
                item.page_number
                for item in bundle.evidence
                if item.source_id == source.source_id
            }
        )
        page_text = "、".join(str(page) for page in pages)
        date_text = source.published_at.isoformat() if source.published_at else "日期未登记"
        accessed_text = (
            source.accessed_at.date().isoformat()
            if source.accessed_at
            else "访问日期未登记"
        )
        lines.append(
            f"[{source.source_id}] {source.title}，{source.publisher}，"
            f"披露日期 {date_text}，访问日期 {accessed_text}，"
            f"[原文]({source.url})；PDF 第 {page_text} 页。"
        )
    lines.extend(
        [
            "",
            "> 注：页码为从 1 开始的 PDF 物理页码；引用仅表示来源定位，不代表授信审批结论。",
            "",
        ]
    )
    return "\n".join(lines)


class EvidenceArtifactWriter:
    def write(
        self,
        bundle: EvidenceBundle,
        output_dir: Path,
        *,
        evidence_output: Optional[Path] = None,
        citations_output: Optional[Path] = None,
    ) -> EvidenceArtifacts:
        entity_dir = Path(output_dir) / bundle.identity.entity_id
        entity_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = Path(evidence_output) if evidence_output else entity_dir / "evidence.json"
        citations_path = (
            Path(citations_output) if citations_output else entity_dir / "citations.md"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        citations_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            evidence_path,
            json.dumps(_model_dict(bundle), ensure_ascii=False, indent=2) + "\n",
        )
        self._atomic_write(citations_path, render_citations(bundle))
        return EvidenceArtifacts(
            entity_id=bundle.identity.entity_id,
            evidence_path=evidence_path.resolve(),
            citations_path=citations_path.resolve(),
            fact_count=len(bundle.facts),
            evidence_count=len(bundle.evidence),
            source_count=len(bundle.sources),
        )

    def write_failure(self, path: Path, failure: EvidenceBuildFailureRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            path,
            json.dumps(_model_dict(failure), ensure_ascii=False, indent=2) + "\n",
        )

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


def build_evidence_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    evidence_output: Optional[Path] = None,
    citations_output: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    seeds: Optional[EvidenceSeedCatalog] = None,
) -> EvidenceArtifacts:
    builder = EvidenceBuilder(clock=clock)
    writer = EvidenceArtifactWriter()
    failure_path = Path(output_dir) / identity.entity_id / "failure.json"
    failure_clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        bundle = builder.build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            seeds=seeds,
        )
        artifacts = writer.write(
            bundle,
            output_dir,
            evidence_output=evidence_output,
            citations_output=citations_output,
        )
        if failure_path.exists():
            failure_path.unlink()
        return artifacts
    except EvidenceBuildError as error:
        writer.write_failure(
            failure_path,
            EvidenceBuildFailureRecord(
                entity_id=identity.entity_id,
                source_id=source.source_id,
                attempted_at=failure_clock(),
                error_code=error.code,
                error_message=str(error),
            ),
        )
        raise
