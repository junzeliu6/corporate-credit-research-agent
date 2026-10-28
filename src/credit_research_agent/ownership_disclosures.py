"""V3.3C 从官方年报表格构建通用、可追溯的股权数据。

本模块不会读取 ``ownership_seeds.json``。前十大股东表只被视为部分披露，
默认不计算“其他未单列股东”，也不把百分比大小自动解释为控制关系。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pdfplumber
from pydantic.v1 import BaseModel, Field

from credit_research_agent.documents import (
    DocumentBindingError,
    DownloadResult,
    PageExtractionStatus,
    PdfExtractionSummary,
    validate_download_binding,
)
from credit_research_agent.evidence import load_parsed_pages, render_citations
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.models import (
    CompanyIdentity,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceStatus,
    MissingInformation,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
)
from credit_research_agent.ownership import (
    ControlRelationship,
    ControlRelationType,
    OwnershipArtifacts,
    OwnershipBuildError,
    OwnershipBuildResult,
    OwnershipDataset,
    OwnershipDisplayConfig,
    OwnershipEdge,
    OwnershipNode,
    OwnershipNodeType,
    OwnershipRelationType,
    PercentageOrigin,
    build_ownership_graph,
    render_ownership_graph,
    render_ownership_questions,
    render_ownership_summary,
    validate_ownership_facts,
)


class OwnershipDisclosureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OwnershipDisclosureStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ExtractedShareholderRow(BaseModel):
    name: str
    shareholder_nature: Optional[str] = None
    shares: Optional[int] = Field(default=None, ge=0)
    percentage: Decimal = Field(ge=0, le=100)
    page_number: int = Field(ge=1)
    raw_cells: List[str]

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class OwnershipDisclosureExtraction(BaseModel):
    schema_version: str = "3.3c.0"
    status: OwnershipDisclosureStatus
    identity: CompanyIdentity
    source: SourceRecord
    as_of_date: date
    top_shareholders: List[ExtractedShareholderRow] = Field(default_factory=list)
    controlling_shareholders: List[str] = Field(default_factory=list)
    actual_controllers: List[str] = Field(default_factory=list)
    ultimate_controllers: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class GenericOwnershipArtifacts(OwnershipArtifacts):
    status: OwnershipDisclosureStatus
    formal_ready: bool
    extraction_path: Path


def _model_data(model: BaseModel) -> dict:
    return json.loads(model.json())


def _cell(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _display_cell(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # PDF 表格经常在中文公司名称的视觉换行处插入空格；中文字符之间的
    # 这类空格不是法定名称的一部分。英文名称（例如 ``UBS AG``）仍保留
    # 单词间空格，避免为了修复中文名称而破坏真实英文股东名。
    return re.sub(
        r"(?<=[\u3400-\u9fff（）()])\s+(?=[\u3400-\u9fff（）()])",
        "",
        text,
    )


def _node_id(name: str) -> str:
    digest = hashlib.sha256(_cell(name).encode("utf-8")).hexdigest()[:14].upper()
    return f"HOLDER::{digest}"


def _parse_decimal(value: object) -> Optional[Decimal]:
    text = _cell(value).replace("％", "%").removesuffix("%").replace(",", "")
    if not text or text in {"-", "—", "/"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if Decimal("0") <= number <= Decimal("100") else None


def _parse_shares(value: object) -> Optional[int]:
    text = _cell(value).replace(",", "")
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


_AUXILIARY_SHARE_STATUS_MARKERS = frozenset({"质押", "冻结", "标记"})


def _share_status_markers(row: ExtractedShareholderRow) -> frozenset[str]:
    """返回股东表辅助状态行中的质押、冻结或标记类型。"""

    markers: set[str] = set()
    for raw_cell in row.raw_cells:
        cell = _cell(raw_cell)
        markers.update(
            marker
            for marker in _AUXILIARY_SHARE_STATUS_MARKERS
            if marker in cell
        )
    return frozenset(markers)


def _consolidate_shareholder_status_rows(
    rows: Sequence[ExtractedShareholderRow],
) -> List[ExtractedShareholderRow]:
    """合并同一股东因质押/冻结辅助列而被 PDF 拆成的重复主行。

    只在同名股东的持股比例、持股数量一致，股东性质没有冲突，且每条
    重复行都有互不相同的辅助状态标记时合并。任何数值冲突或无法解释的
    重复仍然报错，避免用无条件按名称去重掩盖真实披露冲突。
    """

    grouped: Dict[str, List[ExtractedShareholderRow]] = {}
    order: List[str] = []
    for row in rows:
        key = _cell(row.name)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    consolidated: List[ExtractedShareholderRow] = []
    for key in order:
        same_name_rows = grouped[key]
        if len(same_name_rows) == 1:
            consolidated.append(same_name_rows[0])
            continue

        percentages = {row.percentage for row in same_name_rows}
        share_counts = {row.shares for row in same_name_rows}
        page_numbers = {row.page_number for row in same_name_rows}
        natures = {
            _cell(row.shareholder_nature)
            for row in same_name_rows
            if _cell(row.shareholder_nature)
        }
        if len(percentages) != 1 or len(share_counts) != 1 or len(natures) > 1:
            raise OwnershipDisclosureError(
                "conflicting_shareholder_rows",
                f"前十大股东表中同名股东 {same_name_rows[0].name} 的持股事实存在冲突",
            )
        if len(page_numbers) != 1:
            raise OwnershipDisclosureError(
                "duplicate_shareholder_rows",
                f"前十大股东表中同名股东 {same_name_rows[0].name} 的跨页重复无法合并为单页证据",
            )

        status_signatures = [_share_status_markers(row) for row in same_name_rows]
        if (
            any(len(markers) != 1 for markers in status_signatures)
            or len(set(status_signatures)) != len(status_signatures)
        ):
            raise OwnershipDisclosureError(
                "duplicate_shareholder_rows",
                f"前十大股东表中同名股东 {same_name_rows[0].name} 的重复行无法由辅助股份状态解释",
            )

        nature = next(
            (
                row.shareholder_nature
                for row in same_name_rows
                if _cell(row.shareholder_nature)
            ),
            None,
        )
        consolidated.append(
            same_name_rows[0].copy(
                update={
                    "shareholder_nature": nature,
                    "page_number": same_name_rows[0].page_number,
                    # 保留每条原始状态行的完整单元格序列，供证据文件复核。
                    "raw_cells": [
                        cell
                        for row in same_name_rows
                        for cell in row.raw_cells
                    ],
                }
            )
        )
    return consolidated


def _year_end(source: SourceRecord) -> date:
    match = re.fullmatch(r"FY(20\d{2})", str(source.report_period or ""))
    if match is None:
        raise OwnershipDisclosureError(
            "missing_report_period",
            f"来源 {source.source_id} 缺少可识别的年度报告期间",
        )
    return date(int(match.group(1)), 12, 31)


def _find_line_number(text: str, name: str) -> int:
    fragments = [item for item in re.split(r"[（(－—\-]", _cell(name)) if len(item) >= 2]
    probe = fragments[0][:8] if fragments else _cell(name)[:8]
    for index, line in enumerate(text.splitlines(), start=1):
        if probe and probe in _cell(line):
            return index
    return 1


def _validate_inputs(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
) -> PdfExtractionSummary:
    if source.entity_id != identity.entity_id:
        raise OwnershipDisclosureError(
            "source_entity_mismatch",
            f"来源 {source.source_id} 不属于研究主体 {identity.entity_id}",
        )
    if source.source_type is not SourceType.ANNUAL_REPORT or source.trusted_tier != 1:
        raise OwnershipDisclosureError(
            "unsupported_source_binding", "通用股权提取只接受已绑定主体的一级年度报告全文"
        )
    try:
        validate_download_binding(source, download)
    except DocumentBindingError as exc:
        raise OwnershipDisclosureError(exc.code, str(exc)) from exc
    try:
        summary = PdfExtractionSummary.parse_raw(
            Path(summary_path).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise OwnershipDisclosureError(
            "invalid_extraction_summary", f"PDF解析摘要无法读取：{exc}"
        ) from exc
    if summary.source_id != source.source_id or summary.source_sha256 != download.sha256:
        raise OwnershipDisclosureError(
            "extraction_source_mismatch", "PDF解析摘要与下载文件不一致"
        )
    if Path(summary.source_file).resolve() != Path(download.file_path).resolve():
        raise OwnershipDisclosureError(
            "extraction_file_mismatch", "PDF解析摘要指向的文件与下载清单不一致"
        )
    if not summary.pages_sha256:
        raise OwnershipDisclosureError(
            "pages_hash_missing", "PDF解析摘要缺少逐页文本哈希"
        )
    if not Path(pages_path).is_file() or calculate_sha256(Path(pages_path)) != summary.pages_sha256:
        raise OwnershipDisclosureError(
            "pages_hash_mismatch", "逐页文本文件与PDF解析摘要哈希不一致"
        )
    return summary


def _table_page_numbers(pages: Dict[int, object]) -> List[int]:
    anchors: List[int] = []
    for number, page in pages.items():
        text = getattr(page, "text", "")
        status = getattr(page, "status", None)
        if status is not PageExtractionStatus.EXTRACTED:
            continue
        compact = _cell(text)
        if any(
            marker in compact
            for marker in (
                "前十名股东持股情况",
                "前10名股东持股情况",
                "持股5%以上的股东或前10名股东持股情况",
            )
        ):
            anchors.append(number)
    result: List[int] = []
    for anchor in anchors:
        for number in range(anchor, anchor + 4):
            if number in pages and number not in result:
                result.append(number)
    return sorted(result)


def _locate_columns(row: Sequence[object]) -> Optional[Tuple[int, int, int, Optional[int]]]:
    cells = [_cell(item) for item in row]
    try:
        name_index = next(index for index, value in enumerate(cells) if "股东名称" in value)
        percentage_index = next(
            index
            for index, value in enumerate(cells)
            if "持股比例" in value or value in {"比例(%)", "比例（%）", "比例"}
        )
    except StopIteration:
        return None
    shares_index = next(
        (
            index
            for index, value in enumerate(cells)
            if "期末持股" in value or "持股数量" in value
        ),
        -1,
    )
    nature_index = next(
        (index for index, value in enumerate(cells) if "股东性质" in value), None
    )
    if shares_index < 0:
        return None
    return name_index, percentage_index, shares_index, nature_index


def _extract_top_shareholders(
    pdf_path: Path,
    pages: Dict[int, object],
) -> List[ExtractedShareholderRow]:
    page_numbers = _table_page_numbers(pages)
    if not page_numbers:
        return []
    rows: List[ExtractedShareholderRow] = []
    schema: Optional[Tuple[int, int, int, Optional[int]]] = None
    stopped = False
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_number in page_numbers:
                if stopped or page_number > len(pdf.pages):
                    break
                for table in pdf.pages[page_number - 1].extract_tables():
                    for raw_row in table or []:
                        joined = _cell("".join(str(item or "") for item in raw_row))
                        if any(
                            marker in joined
                            for marker in (
                                "前十名无限售条件股东",
                                "前10名无限售条件股东",
                                "前十名流通股东",
                                "战略投资者或一般法人",
                            )
                        ):
                            stopped = True
                            break
                        located = _locate_columns(raw_row)
                        if located is not None:
                            schema = located
                            continue
                        if schema is None:
                            continue
                        name_index, percentage_index, shares_index, nature_index = schema
                        if max(name_index, percentage_index, shares_index) >= len(raw_row):
                            continue
                        percentage = _parse_decimal(raw_row[percentage_index])
                        name = _display_cell(raw_row[name_index])
                        if percentage is None:
                            # 跨页表格可能把长名称的尾部单独放在下一页首行。
                            other_values = [
                                _display_cell(item)
                                for index, item in enumerate(raw_row)
                                if index != name_index and _cell(item)
                            ]
                            if rows and name and not other_values:
                                previous = rows[-1]
                                rows[-1] = previous.copy(
                                    update={
                                        "name": previous.name + name,
                                        "raw_cells": [*previous.raw_cells, name],
                                    }
                                )
                            continue
                        if not name or any(
                            marker in name
                            for marker in ("股东名称", "前十名", "前10名", "股份种类")
                        ):
                            continue
                        nature = (
                            _display_cell(raw_row[nature_index])
                            if nature_index is not None and nature_index < len(raw_row)
                            else None
                        )
                        rows.append(
                            ExtractedShareholderRow(
                                name=name,
                                shareholder_nature=nature or None,
                                shares=_parse_shares(raw_row[shares_index]),
                                percentage=percentage,
                                page_number=page_number,
                                raw_cells=[
                                    _display_cell(item) for item in raw_row if _display_cell(item)
                                ],
                            )
                        )
                    if stopped:
                        break
    except Exception as exc:
        raise OwnershipDisclosureError(
            "shareholder_table_extraction_failed", f"前十大股东表解析失败：{exc}"
        ) from exc
    # 年报表格可能因“质押/冻结”辅助状态拆成多行；仅在持股事实完全一致、
    # 状态类型互不相同时合并。其他同名重复仍作为解析冲突处理。
    rows = _consolidate_shareholder_status_rows(rows)
    if len(rows) > 10:
        raise OwnershipDisclosureError(
            "too_many_top_shareholder_rows",
            f"前十大股东表解析得到{len(rows)}行，拒绝静默截断",
        )
    return rows


def _extract_key_value_names(
    pdf_path: Path,
    page_numbers: Iterable[int],
    *,
    accepted_keys: Sequence[str],
) -> List[Tuple[str, int, List[str]]]:
    results: List[Tuple[str, int, List[str]]] = []
    accepted = {_cell(item) for item in accepted_keys}
    with pdfplumber.open(pdf_path) as pdf:
        for page_number in sorted(set(page_numbers)):
            if page_number < 1 or page_number > len(pdf.pages):
                continue
            for table in pdf.pages[page_number - 1].extract_tables():
                pending_tabular_header = False
                for row in table or []:
                    cells = [_cell(item) for item in row]
                    nonempty = [(index, value) for index, value in enumerate(cells) if value]
                    if pending_tabular_header:
                        pending_tabular_header = False
                        raw_value = _display_cell(row[0]) if row else ""
                        value = _cell(raw_value)
                        if value and value not in accepted and value not in {"不适用", "无"}:
                            results.append(
                                (
                                    raw_value,
                                    page_number,
                                    [_display_cell(item) for item in row if _display_cell(item)],
                                )
                            )
                        continue
                    if len(nonempty) < 2 or nonempty[0][1] not in accepted:
                        continue
                    if any(
                        marker in nonempty[1][1]
                        for marker in (
                            "法定代表人",
                            "单位负责人",
                            "与实际控制人关系",
                            "国籍",
                            "成立日期",
                            "组织机构代码",
                        )
                    ):
                        pending_tabular_header = True
                        continue
                    value = _display_cell(row[nonempty[1][0]])
                    if not value or _cell(value) in {"不适用", "无"}:
                        continue
                    results.append(
                        (
                            value,
                            page_number,
                            [_display_cell(item) for item in row if _display_cell(item)],
                        )
                    )
    return results


def _split_subjects(value: str) -> List[str]:
    return [
        item.strip()
        for item in re.split(r"[、，,；;]", value)
        if item.strip() and item.strip() not in {"本人"}
    ]


def _node_type_from_nature(nature: Optional[str]) -> OwnershipNodeType:
    if nature and "自然人" in nature:
        return OwnershipNodeType.NATURAL_PERSON
    return OwnershipNodeType.GENERAL_INSTITUTION


class OwnershipDisclosureExtractor:
    def __init__(
        self, *, clock: Optional[Callable[[], datetime]] = None
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def extract(
        self,
        *,
        identity: CompanyIdentity,
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
        config: Optional[OwnershipDisplayConfig] = None,
    ) -> Tuple[OwnershipDisclosureExtraction, OwnershipBuildResult]:
        summary = _validate_inputs(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )
        pages = load_parsed_pages(pages_path)
        as_of_date = _year_end(source)
        holders = _extract_top_shareholders(Path(download.file_path), pages)
        if not holders:
            raise OwnershipDisclosureError(
                "top_shareholder_table_not_found",
                "未能从官方年报唯一定位前十大股东名称和持股比例",
            )

        controlling_pages = [
            number
            for number, page in pages.items()
            if page.status is PageExtractionStatus.EXTRACTED
            and (
                "控股股东名称" in page.text
                or "(一) 控股股东情况" in page.text
                or "（一）控股股东情况" in page.text
                or "2、公司控股股东情况" in page.text
            )
        ]
        actual_pages = [
            number
            for number, page in pages.items()
            if page.status is PageExtractionStatus.EXTRACTED
            and (
                "实际控制人姓名" in page.text
                or "(二) 实际控制人情况" in page.text
                or "（二）实际控制人情况" in page.text
                or "3、公司实际控制人" in page.text
            )
        ]
        controlling_rows = _extract_key_value_names(
            Path(download.file_path),
            controlling_pages,
            accepted_keys=("控股股东名称", "控股股东姓名"),
        )
        if not controlling_rows:
            controlling_rows = _extract_key_value_names(
                Path(download.file_path),
                controlling_pages,
                accepted_keys=("名称", "姓名"),
            )[:1]
        actual_rows = _extract_key_value_names(
            Path(download.file_path),
            actual_pages,
            accepted_keys=("实际控制人姓名", "实际控制人名称"),
        )
        if not actual_rows:
            actual_rows = _extract_key_value_names(
                Path(download.file_path),
                actual_pages,
                accepted_keys=("姓名", "名称"),
            )[:1]
        controlling = list(
            dict.fromkeys(
                subject
                for value, _page, _cells in controlling_rows
                for subject in _split_subjects(value)
            )
        )
        actual = list(
            dict.fromkeys(
                subject
                for value, _page, _cells in actual_rows
                for subject in _split_subjects(value)
            )
        )

        ultimate: List[str] = []
        ultimate_rows: List[Tuple[str, int, List[str]]] = []
        final_pattern = re.compile(r"本企业最终控制方是\s*([^。；\n]+)")
        for page_number, page in pages.items():
            if page.status is not PageExtractionStatus.EXTRACTED:
                continue
            match = final_pattern.search(page.text)
            if match:
                value = _display_cell(match.group(1))
                for subject in _split_subjects(value):
                    if subject not in ultimate:
                        ultimate.append(subject)
                        ultimate_rows.append(
                            (subject, page_number, [match.group(0).strip()])
                        )

        evidence: List[EvidenceRecord] = []

        def add_evidence(page_number: int, section: str, cells: Sequence[str]) -> str:
            evidence_id = f"E{len(evidence) + 1:03d}"
            page_text = getattr(pages.get(page_number), "text", "")
            quote = " | ".join(item for item in cells if item)[:500].rstrip()
            evidence.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source_id=source.source_id,
                    source_sha256=summary.source_sha256,
                    page_number=page_number,
                    line_number=_find_line_number(page_text, cells[0] if cells else section),
                    page_or_section=section,
                    quote=quote or section,
                    status=EvidenceStatus.VERIFIED,
                    verification_method=VerificationMethod.PDF_TABLE_EXTRACTION,
                )
            )
            return evidence_id

        holder_evidence: Dict[str, str] = {}
        for row in holders:
            holder_evidence[_cell(row.name)] = add_evidence(
                row.page_number, "前十大股东持股情况", row.raw_cells
            )
        controlling_evidence: Dict[str, str] = {}
        for value, page_number, cells in controlling_rows:
            evidence_id = add_evidence(page_number, "控股股东情况", cells)
            controlling_evidence.update(
                {_cell(name): evidence_id for name in _split_subjects(value)}
            )
        actual_evidence: Dict[str, str] = {}
        for value, page_number, cells in actual_rows:
            evidence_id = add_evidence(page_number, "实际控制人情况", cells)
            actual_evidence.update(
                {_cell(name): evidence_id for name in _split_subjects(value)}
            )
        ultimate_evidence: Dict[str, str] = {}
        for value, page_number, cells in ultimate_rows:
            evidence_id = add_evidence(page_number, "最终控制方披露", cells)
            ultimate_evidence[_cell(value)] = evidence_id

        first_evidence = next(iter(holder_evidence.values()))
        nodes: Dict[str, OwnershipNode] = {
            identity.entity_id: OwnershipNode(
                node_id=identity.entity_id,
                name=identity.legal_name,
                node_type=OwnershipNodeType.TARGET_COMPANY,
                source_ids=[source.source_id],
                evidence_ids=[first_evidence],
            )
        }

        def ensure_node(name: str, node_type: OwnershipNodeType, evidence_id: str) -> str:
            key = _cell(name)
            existing = next(
                (node for node in nodes.values() if _cell(node.name) == key), None
            )
            if existing is not None:
                merged_evidence = list(dict.fromkeys([*existing.evidence_ids, evidence_id]))
                nodes[existing.node_id] = existing.copy(
                    update={"evidence_ids": merged_evidence}
                )
                return existing.node_id
            node_id = _node_id(name)
            nodes[node_id] = OwnershipNode(
                node_id=node_id,
                name=name,
                node_type=node_type,
                source_ids=[source.source_id],
                evidence_ids=[evidence_id],
            )
            return node_id

        edges: List[OwnershipEdge] = []
        for index, row in enumerate(holders, start=1):
            evidence_id = holder_evidence[_cell(row.name)]
            holder_id = ensure_node(
                row.name, _node_type_from_nature(row.shareholder_nature), evidence_id
            )
            edges.append(
                OwnershipEdge(
                    edge_id=f"OE{index:03d}",
                    source_node_id=holder_id,
                    target_node_id=identity.entity_id,
                    relation_type=OwnershipRelationType.EQUITY_OWNERSHIP,
                    percentage=row.percentage,
                    percentage_origin=PercentageOrigin.DISCLOSED,
                    capital_scope="total_ordinary_share_capital",
                    as_of_date=as_of_date,
                    source_ids=[source.source_id],
                    evidence_ids=[evidence_id],
                )
            )

        controls: List[ControlRelationship] = []
        relation_index = 1
        for names, relation_type, evidence_map, default_type in (
            (
                controlling,
                ControlRelationType.CONTROLLING_SHAREHOLDER,
                controlling_evidence,
                OwnershipNodeType.GENERAL_INSTITUTION,
            ),
            (
                actual,
                ControlRelationType.ACTUAL_CONTROLLER,
                actual_evidence,
                OwnershipNodeType.NATURAL_PERSON,
            ),
            (
                ultimate,
                ControlRelationType.ULTIMATE_CONTROLLER,
                ultimate_evidence,
                OwnershipNodeType.NATURAL_PERSON,
            ),
        ):
            subject_ids: List[str] = []
            relationship_evidence: List[str] = []
            for name in names:
                evidence_id = evidence_map.get(_cell(name))
                if evidence_id is None:
                    continue
                subject_ids.append(ensure_node(name, default_type, evidence_id))
                relationship_evidence.append(evidence_id)
            if not subject_ids:
                continue
            controls.append(
                ControlRelationship(
                    relationship_id=f"CR{relation_index:03d}",
                    subject_node_ids=subject_ids,
                    controlled_node_id=identity.entity_id,
                    relation_type=relation_type,
                    as_of_date=as_of_date,
                    source_ids=[source.source_id],
                    evidence_ids=list(dict.fromkeys(relationship_evidence)),
                    note="关系来自年度报告明确披露，未由持股比例推断。",
                )
            )
            relation_index += 1

        missing = [
            MissingInformation(
                missing_id="M001",
                entity_id=identity.entity_id,
                research_module=ResearchModule.OWNERSHIP_STRUCTURE,
                topic="完整股东名册",
                description="年报当前只结构化了前十大股东，不代表全部普通股股东。",
                reason="未取得同一时点完整股东名册，未强行计算或命名剩余股东。",
                evidence_ids=[first_evidence],
            ),
            MissingInformation(
                missing_id="M002",
                entity_id=identity.entity_id,
                research_module=ResearchModule.OWNERSHIP_STRUCTURE,
                topic="机构股东上层穿透",
                description="重要机构股东的上层直接股东及比例尚未全部取得。",
                reason="控制关系文字不等同于可核验的逐层直接持股比例。",
            ),
        ]
        if not controls:
            missing.append(
                MissingInformation(
                    missing_id=f"M{len(missing) + 1:03d}",
                    entity_id=identity.entity_id,
                    research_module=ResearchModule.OWNERSHIP_STRUCTURE,
                    topic="控股股东及实际控制人",
                    description="当前文本和表格未形成可唯一识别的控制关系。",
                    reason="未使用持股比例自动推导控制人。",
                )
            )

        parsed_source = source.copy(
            update={
                "status": SourceStatus.PARSED,
                "accessed_at": download.accessed_at,
                "local_path": Path(download.file_path).name,
                "page_or_section": "、".join(
                    f"PDF第{number}页"
                    for number in sorted({item.page_number for item in evidence})
                ),
            }
        )
        limitations = [
            "前十大股东表不是完整股东名册；已披露比例不足100%时默认保持信息不完整。",
            "未核实同一时点完整分母前，不生成“其他未单列股东”差额节点。",
            "年报控制关系方框图若没有可提取文本，不从图片猜测上层股东或比例。",
            "部分股权图只展示已披露直接关系，formal_ready=false，不代表完整穿透。",
        ]
        dataset = OwnershipDataset(
            schema_version="3.3c.0",
            identity=identity,
            as_of_date=as_of_date,
            nodes=list(nodes.values()),
            ownership_edges=edges,
            control_relationships=controls,
            state_control_assessments=[],
            completeness_declarations=[],
            sources=[parsed_source],
            evidence=evidence,
            missing_information=missing,
            limitations=limitations,
        )
        validation = validate_ownership_facts(dataset)
        if not validation.is_valid:
            raise OwnershipDisclosureError(
                "invalid_ownership_disclosure",
                "; ".join(item.message for item in validation.errors),
            )
        graph = build_ownership_graph(dataset, config, require_complete=False)
        extraction = OwnershipDisclosureExtraction(
            status=(
                OwnershipDisclosureStatus.SUCCESS
                if graph.formal_ready
                else OwnershipDisclosureStatus.PARTIAL_SUCCESS
            ),
            identity=identity,
            source=parsed_source,
            as_of_date=as_of_date,
            top_shareholders=holders,
            controlling_shareholders=controlling,
            actual_controllers=actual,
            ultimate_controllers=ultimate,
            limitations=limitations,
        )
        return extraction, OwnershipBuildResult(
            dataset=dataset,
            graph=graph,
            validation=validation,
            percentage_validation=graph.percentage_validation,
        )


def _atomic_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    return path.resolve()


def build_generic_ownership_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    config: Optional[OwnershipDisplayConfig] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> GenericOwnershipArtifacts:
    """生成与现有 ownership Skill 快照兼容的动态公司制品。"""

    extraction, result = OwnershipDisclosureExtractor(clock=clock).extract(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        config=config,
    )
    entity_dir = Path(output_dir) / identity.entity_id / "ownership"
    dataset_path = _atomic_write(
        entity_dir / "ownership.json",
        json.dumps(_model_data(result.dataset), ensure_ascii=False, indent=2) + "\n",
    )
    graph_path = _atomic_write(
        entity_dir / "ownership_graph.json",
        json.dumps(_model_data(result.graph), ensure_ascii=False, indent=2) + "\n",
    )
    extraction_path = _atomic_write(
        entity_dir / "ownership_disclosure_extraction.json",
        json.dumps(_model_data(extraction), ensure_ascii=False, indent=2) + "\n",
    )
    validation_path = _atomic_write(
        entity_dir / "ownership_validation.json",
        json.dumps(
            _model_data(result.percentage_validation), ensure_ascii=False, indent=2
        )
        + "\n",
    )
    sources_path = _atomic_write(
        entity_dir / "ownership_sources.json",
        json.dumps(
            [_model_data(item) for item in result.dataset.sources],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    summary_path_out = _atomic_write(
        entity_dir / "ownership_summary.md", render_ownership_summary(result)
    )
    questions_path = _atomic_write(
        entity_dir / "ownership_questions.md", render_ownership_questions(result)
    )
    citation_bundle = EvidenceBundle(
        identity=result.dataset.identity,
        generated_at=(clock or (lambda: datetime.now(timezone.utc)))(),
        facts=[],
        evidence=result.dataset.evidence,
        sources=result.dataset.sources,
        limitations=result.dataset.limitations,
    )
    citations_path = _atomic_write(
        entity_dir / "citations.md", render_citations(citation_bundle)
    )
    rendered = render_ownership_graph(
        result.graph,
        entity_dir / "ownership_structure.svg",
        allow_incomplete=not result.graph.formal_ready,
    )
    return GenericOwnershipArtifacts(
        entity_id=identity.entity_id,
        status=extraction.status,
        formal_ready=result.graph.formal_ready,
        extraction_path=extraction_path,
        dataset_path=dataset_path,
        graph_path=graph_path,
        svg_path=rendered.svg_path.resolve(),
        png_path=rendered.png_path.resolve(),
        dot_path=rendered.dot_path.resolve(),
        citations_path=citations_path,
        summary_path=summary_path_out,
        validation_path=validation_path,
        sources_path=sources_path,
        questions_path=questions_path,
        render_engine=rendered.engine,
        node_count=len(result.graph.nodes),
        edge_count=len(result.graph.edges),
        control_relationship_count=len(result.dataset.control_relationships),
        missing_information_count=len(result.dataset.missing_information),
    )

