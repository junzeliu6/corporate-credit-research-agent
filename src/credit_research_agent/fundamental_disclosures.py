"""V3.3C 通用年报基本面披露提取。

本模块只从已经下载、解析并完成主体复核的官方年报中提取明确披露句。
它不调用 LLM，也不读取任何公司专属 Seeds。规则的职责是召回带页码的原文，
而不是把宣传语、图片内容或常识改写成企业事实。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic.v1 import BaseModel, Field

from credit_research_agent.documents import (
    DocumentBindingError,
    DownloadResult,
    PageExtractionStatus,
    PdfExtractionSummary,
    validate_download_binding,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.evidence import load_parsed_pages, render_citations
from credit_research_agent.models import (
    EntityScope,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    FundamentalResearchBundle,
    MissingInformation,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
    CompanyIdentity,
)


class FundamentalDisclosureError(RuntimeError):
    """通用基本面来源或提取结果不满足安全约束。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FundamentalExtractionStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ExtractedDisclosure(BaseModel):
    research_module: ResearchModule
    page_number: int = Field(ge=1)
    line_number: int = Field(ge=1)
    section: str
    text: str = Field(min_length=1)
    matched_pattern: str


class FundamentalDisclosureResult(BaseModel):
    schema_version: str = "3.3c.0"
    status: FundamentalExtractionStatus
    identity: CompanyIdentity
    source: SourceRecord
    generated_at: datetime
    disclosures: List[ExtractedDisclosure] = Field(default_factory=list)
    bundle: FundamentalResearchBundle
    limitations: List[str] = Field(default_factory=list)


class GenericFundamentalArtifacts(BaseModel):
    entity_id: str
    status: FundamentalExtractionStatus
    extraction_path: Path
    fundamentals_path: Path
    citations_path: Path
    sources_path: Path
    fact_count: int
    evidence_count: int
    missing_information_count: int
    module_fact_counts: Dict[str, int]


class _ModuleRule(BaseModel):
    module: ResearchModule
    section_label: str
    patterns: Tuple[str, ...]
    page_hints: Tuple[str, ...]
    max_items: int
    min_length: int = 12


_RULES: Tuple[_ModuleRule, ...] = (
    _ModuleRule(
        module=ResearchModule.DEVELOPMENT_HISTORY,
        section_label="公司概况与发展历史",
        patterns=(
            r"(?:成立|设立)于?\s*(?:19|20)\d{2}年",
            r"(?:19|20)\d{2}年[^。；]{0,80}(?:成立|设立|整体变更|挂牌|上市)",
            r"前身(?:为|是)",
            r"整体变更(?:设立|为)",
            r"(?:上海|深圳|北京)证券交易所(?:挂牌|上市)",
            # 更名及公司制改制：仅命中披露中明确的“由…变更为…”关系。
            r"(?:公司|发行人|本公司)(?:名称)?由[^。；]{1,100}(?:更名|变更)为",
            r"(?:19|20)\d{2}年[^。；]{0,120}(?:改制为|整体变更为|改制设立)",
            # 并购、重组或吸收合并要有已发生动词，避免把风险提示当成沿革。
            r"(?:19|20)\d{2}年[^。；]{0,140}(?:完成|实施|成功完成)(?:了)?[^。；]{0,60}(?:收购|并购|重大资产重组|资产重组|吸收合并)",
            r"(?:19|20)\d{2}年[^。；]{0,140}(?:收购|并购|吸收合并)[^。；]{0,80}(?:完成|完成交割|实施完毕|纳入合并范围)",
            r"(?:公司|发行人|本公司)[^。；]{0,60}(?:完成|实施)(?:了)?[^。；]{0,60}(?:重大资产重组|资产重组|改制|吸收合并)",
            # 业务转型保留“启动/完成/实施”等历史事件语义。
            r"(?:19|20)\d{2}年[^。；]{0,140}(?:启动|完成|实施|开始)(?:了)?[^。；]{0,80}(?:业务转型|战略转型|产业转型|转型升级)",
            r"(?:19|20)\d{2}年(?![^。；]{0,160}(?:计划|拟|预计|将于|力争))[^。；]{0,140}(?:由|从)[^。；]{1,80}(?:转型为|向[^。；]{1,60}转型)",
            # 只召回明确已建成、投产或投入运营的项目里程碑。
            r"(?:19|20)\d{2}年[^。；]{0,160}(?:项目|基地|产线|工厂)[^。；]{0,80}(?:建成投产|正式投产|建成|竣工|启用|投入运营)",
            r"(?:19|20)\d{2}年[^。；]{0,160}(?:成为|标志着)[^。；]{0,80}(?:里程碑|新阶段|新的发展阶段|发展新阶段)",
        ),
        page_hints=(
            "公司概况",
            "公司简介",
            "公司基本情况",
            "简要工商信息",
            "发展历程",
            "公司发展历程",
            "历史沿革",
            "公司沿革",
        ),
        max_items=8,
    ),
    _ModuleRule(
        module=ResearchModule.MAIN_BUSINESS,
        section_label="主营业务",
        patterns=(
            r"主营业务(?:是|为|涵盖|包括)",
            r"主要从事",
            r"主要业务(?:是|为|涵盖|包括)",
            r"核心业务(?:是|为|涵盖|包括)",
        ),
        page_hints=(
            "报告期内公司从事的业务",
            "报告期内公司从事的主要业务",
            "公司主要业务",
            "主营业务分析",
        ),
        max_items=4,
    ),
    _ModuleRule(
        module=ResearchModule.OPERATING_MODEL,
        section_label="经营模式",
        patterns=(
            r"经营模式",
            r"采购模式",
            r"生产模式",
            r"销售模式",
            r"盈利模式",
            r"业务模式",
            r"服务模式",
            r"全品类采购",
            r"批零一体化",
            r"线上线下相结合",
        ),
        page_hints=(
            "经营模式",
            "报告期内公司从事的业务",
            "报告期内公司从事的主要业务",
            "主营业务分析",
        ),
        max_items=6,
    ),
    _ModuleRule(
        module=ResearchModule.MAIN_PRODUCTS,
        section_label="主要产品",
        patterns=(
            r"主要产品(?:是|为|包括|有)",
            r"核心产品(?:是|为|包括|有)",
            r"产品系列",
            r"产品(?:主要)?包括",
            r"拥有[^。；]{0,100}(?:品牌|品种|品规)",
            r"现有[^。；]{0,120}(?:产品|制剂|药品)",
        ),
        page_hints=(
            "主要产品及其用途",
            "主要产品",
            "产品及用途",
            "主营业务分析",
            "公司从事的业务",
        ),
        max_items=6,
    ),
)


_PAGE_HEADER = re.compile(r"^.+?\s+20\d{2}\s*年年度报告(?:全文)?$")
_PAGE_FOOTER = re.compile(r"^\d+\s*/\s*\d+$")
_SENTENCE_END = re.compile(r"(?<=[。！？；])")
_CHAPTER_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十]+节|[一二三四五六七八九十]+、|"
    r"[（(][一二三四五六七八九十]+[）)]|\d+[、.])"
)


def _model_data(model: BaseModel) -> dict:
    return json.loads(model.json())


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        line = _normalise_space(raw)
        if not line or _PAGE_HEADER.fullmatch(line) or _PAGE_FOOTER.fullmatch(line):
            continue
        lines.append(line)
    return lines


def _sentences(lines: Sequence[str]) -> Iterable[Tuple[int, str]]:
    """把视觉换行恢复成短披露句，同时保留首个物理行号。"""

    buffer: List[str] = []
    first_line = 1
    for index, line in enumerate(lines, start=1):
        if _CHAPTER_HEADING.match(line) and buffer:
            joined = _normalise_space(" ".join(buffer))
            if joined:
                yield first_line, joined
            buffer = []
        if not buffer:
            first_line = index
        buffer.append(line)
        joined = _normalise_space(" ".join(buffer))
        parts = _SENTENCE_END.split(joined)
        complete = parts[:-1]
        remainder = parts[-1]
        for part in complete:
            sentence = part.strip()
            if sentence:
                yield first_line, sentence
            first_line = index
        buffer = [remainder] if remainder else []
        # 年报表格或超长无标点文字不能无限进入模型上下文。
        if buffer and len(buffer[0]) > 700:
            yield first_line, buffer[0][:700].rstrip()
            buffer = []
    if buffer:
        joined = _normalise_space(" ".join(buffer))
        if joined:
            yield first_line, joined


def _page_score(text: str, hints: Sequence[str]) -> int:
    compact = "".join(text.split())
    return sum(3 for hint in hints if "".join(hint.split()) in compact)


def _candidate_page_numbers(
    rule: _ModuleRule, pages: Mapping[int, object]
) -> List[int]:
    """按章节标题而不是固定页码确定有限扫描窗口。"""

    strong_markers: Mapping[ResearchModule, Tuple[str, ...]] = {
        ResearchModule.DEVELOPMENT_HISTORY: (
            "公司基本情况",
            "公司概况",
            "公司简介",
            "公司简要工商信息",
            "发展历程",
            "公司发展历程",
            "历史沿革",
            "公司沿革",
        ),
        ResearchModule.MAIN_BUSINESS: (
            "报告期内公司从事的主要业务",
            "报告期内公司从事的业务情况",
            "公司主营业务",
            "公司主要业务",
        ),
        ResearchModule.OPERATING_MODEL: (
            "（三）经营模式",
            "(三)经营模式",
            "主营业务及经营模式",
            "经营模式转型升级",
        ),
        ResearchModule.MAIN_PRODUCTS: (
            "主要产品及其用途",
            "主要产品",
            "产品及用途",
        ),
    }
    markers = strong_markers[rule.module]
    anchors = [
        number
        for number, page in sorted(pages.items())
        if any(marker in getattr(page, "text", "") for marker in markers)
    ]
    if not anchors:
        anchors = [
            number
            for number, page in sorted(pages.items())
            if _page_score(getattr(page, "text", ""), rule.page_hints) > 0
        ]
    window = 2 if rule.module is ResearchModule.DEVELOPMENT_HISTORY else 3
    candidates: List[int] = []
    for anchor in anchors:
        for number in range(anchor, anchor + window):
            if number in pages and number not in candidates:
                candidates.append(number)
    return candidates


def _sentence_noise(value: str) -> bool:
    if len(value) < 8:
        return True
    if value.count("|") > 2:
        return True
    if "图如下" in value or re.match(r"^图\s*\d+", value):
        return True
    visible = re.sub(r"[\d\W_]", "", value)
    return len(visible) < 6


def _is_subsidiary_specific_statement(
    value: str, module: ResearchModule
) -> bool:
    """避免把明确描述某一家子公司的业务误记为上市公司自身事实。"""

    if module not in {ResearchModule.MAIN_BUSINESS, ResearchModule.MAIN_PRODUCTS}:
        return False
    compact = "".join(value.split())
    return bool(
        re.search(
            r"(?:旗下|全资子公司|控股子公司|子公司)[^。；]{0,80}"
            r"(?:主要从事|主营业务|主要产品|核心产品)",
            compact,
        )
    )


def _validate_source_input(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
) -> PdfExtractionSummary:
    if source.entity_id != identity.entity_id:
        raise FundamentalDisclosureError(
            "source_entity_mismatch",
            f"来源 {source.source_id} 不属于研究主体 {identity.entity_id}",
        )
    if source.source_type is not SourceType.ANNUAL_REPORT or source.trusted_tier != 1:
        raise FundamentalDisclosureError(
            "unsupported_source_binding", "通用基本面提取只接受已绑定主体的一级年度报告全文"
        )
    try:
        validate_download_binding(source, download)
    except DocumentBindingError as exc:
        raise FundamentalDisclosureError(exc.code, str(exc)) from exc
    try:
        summary = PdfExtractionSummary.parse_raw(
            Path(summary_path).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise FundamentalDisclosureError(
            "invalid_extraction_summary", f"PDF解析摘要无法读取：{exc}"
        ) from exc
    if summary.source_id != source.source_id or summary.source_sha256 != download.sha256:
        raise FundamentalDisclosureError(
            "extraction_source_mismatch", "PDF解析摘要与下载文件不一致"
        )
    if Path(summary.source_file).resolve() != Path(download.file_path).resolve():
        raise FundamentalDisclosureError(
            "extraction_file_mismatch", "PDF解析摘要指向的文件与下载清单不一致"
        )
    if not summary.pages_sha256:
        raise FundamentalDisclosureError(
            "pages_hash_missing", "PDF解析摘要缺少逐页文本哈希"
        )
    if not Path(pages_path).is_file() or calculate_sha256(Path(pages_path)) != summary.pages_sha256:
        raise FundamentalDisclosureError(
            "pages_hash_mismatch", "逐页文本文件与PDF解析摘要哈希不一致"
        )
    return summary


class FundamentalDisclosureExtractor:
    """以标题提示缩小范围，再从明确披露句中生成可回溯事实。"""

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
    ) -> FundamentalDisclosureResult:
        summary = _validate_source_input(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        )
        pages = load_parsed_pages(pages_path)
        available_pages = {
            number: page
            for number, page in pages.items()
            if page.status is PageExtractionStatus.EXTRACTED and page.text.strip()
        }
        if not available_pages:
            raise FundamentalDisclosureError(
                "no_extracted_pages", "年报逐页文本没有可用正文"
            )

        disclosures: List[ExtractedDisclosure] = []
        evidence: List[EvidenceRecord] = []
        facts: List[FactRecord] = []
        module_counts: Dict[ResearchModule, int] = {}
        used_text: set[Tuple[ResearchModule, str]] = set()

        for rule in _RULES:
            priority = _candidate_page_numbers(rule, available_pages)

            compiled = [(pattern, re.compile(pattern)) for pattern in rule.patterns]
            for page_number in priority:
                if module_counts.get(rule.module, 0) >= rule.max_items:
                    break
                page = available_pages[page_number]
                lines = _clean_lines(page.text)
                for line_number, sentence in _sentences(lines):
                    if module_counts.get(rule.module, 0) >= rule.max_items:
                        break
                    if (
                        len(sentence) < rule.min_length
                        or len(sentence) > 500
                        or _sentence_noise(sentence)
                        or _is_subsidiary_specific_statement(sentence, rule.module)
                    ):
                        continue
                    searchable_sentence = "".join(sentence.split())
                    matched = next(
                        (
                            pattern
                            for pattern, expression in compiled
                            if expression.search(searchable_sentence)
                        ),
                        None,
                    )
                    if matched is None:
                        continue
                    clean = sentence.strip()
                    signature = (rule.module, "".join(clean.split()))
                    if signature in used_text:
                        continue
                    used_text.add(signature)
                    evidence_id = f"E{len(evidence) + 1:03d}"
                    fact_id = f"F{len(facts) + 1:03d}"
                    quote = clean
                    disclosure = ExtractedDisclosure(
                        research_module=rule.module,
                        page_number=page_number,
                        line_number=line_number,
                        section=rule.section_label,
                        text=clean,
                        matched_pattern=matched,
                    )
                    disclosures.append(disclosure)
                    evidence.append(
                        EvidenceRecord(
                            evidence_id=evidence_id,
                            source_id=source.source_id,
                            source_sha256=summary.source_sha256,
                            page_number=page_number,
                            line_number=line_number,
                            page_or_section=rule.section_label,
                            quote=quote,
                            status=EvidenceStatus.VERIFIED,
                            verification_method=VerificationMethod.NORMALIZED_PDF_TEXT,
                        )
                    )
                    index = module_counts.get(rule.module, 0) + 1
                    facts.append(
                        FactRecord(
                            fact_id=fact_id,
                            entity_id=identity.entity_id,
                            subject=identity.legal_name,
                            category=FactCategory.BUSINESS,
                            research_module=rule.module,
                            field_name=f"{rule.module.value}_disclosure_{index:02d}",
                            value=clean,
                            claim=clean,
                            entity_scope=EntityScope.LISTED_COMPANY,
                            period=source.report_period,
                            evidence_ids=[evidence_id],
                        )
                    )
                    module_counts[rule.module] = index

        missing: List[MissingInformation] = []
        for rule in _RULES:
            if module_counts.get(rule.module, 0):
                continue
            missing.append(
                MissingInformation(
                    missing_id=f"M{len(missing) + 1:03d}",
                    entity_id=identity.entity_id,
                    research_module=rule.module,
                    topic=rule.section_label,
                    description=f"当前年报未形成可确定性抽取的{rule.section_label}披露句。",
                    reason="未命中明确标题和披露措辞；未使用模型或其他企业Seeds补造内容。",
                )
            )

        parsed_source = source.copy(
            update={
                "status": SourceStatus.PARSED,
                "accessed_at": download.accessed_at,
                "local_path": Path(download.file_path).name,
                "page_or_section": "、".join(
                    f"PDF第{number}页" for number in sorted({item.page_number for item in disclosures})
                )
                or "未形成可用基本面披露句",
            }
        )
        limitations = [
            "通用首版只保存官方年报中可定位的明确披露句，不从图片、宣传语或常识补造企业事实。",
            "没有明确披露的采购、生产、销售或盈利模式保持缺失，不强行套用固定四段式。",
            "产品图片中的标签若未进入PDF文本层，不会被识别为产品事实。",
            "专业分析正文仍由DeepSeek在后续Agent阶段撰写，Python仅提供结构化原文。",
        ]
        bundle = FundamentalResearchBundle(
            schema_version="3.3c.0",
            identity=identity,
            generated_at=self._clock(),
            facts=facts,
            evidence=evidence,
            sources=[parsed_source],
            limitations=limitations,
            inferences=[],
            missing_information=missing,
        )
        populated = sum(1 for rule in _RULES if module_counts.get(rule.module, 0))
        status = (
            FundamentalExtractionStatus.SUCCESS
            if populated == len(_RULES)
            else FundamentalExtractionStatus.PARTIAL_SUCCESS
            if facts
            else FundamentalExtractionStatus.FAILED
        )
        return FundamentalDisclosureResult(
            status=status,
            identity=identity,
            source=parsed_source,
            generated_at=bundle.generated_at,
            disclosures=disclosures,
            bundle=bundle,
            limitations=limitations,
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


def build_generic_fundamental_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    clock: Optional[Callable[[], datetime]] = None,
) -> GenericFundamentalArtifacts:
    """生成现有 Agent ``build_fundamental_snapshot`` 可直接消费的产物。"""

    result = FundamentalDisclosureExtractor(clock=clock).extract(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
    )
    entity_dir = Path(output_dir) / identity.entity_id / "fundamentals"
    extraction_path = _atomic_write(
        entity_dir / "fundamental_disclosure_extraction.json",
        json.dumps(_model_data(result), ensure_ascii=False, indent=2) + "\n",
    )
    fundamentals_path = _atomic_write(
        entity_dir / "fundamentals.json",
        result.bundle.json(ensure_ascii=False, indent=2) + "\n",
    )
    citation_bundle = EvidenceBundle(
        identity=result.bundle.identity,
        generated_at=result.bundle.generated_at,
        facts=result.bundle.facts,
        evidence=result.bundle.evidence,
        sources=result.bundle.sources,
        limitations=result.bundle.limitations,
    )
    citations_path = _atomic_write(
        entity_dir / "citations.md", render_citations(citation_bundle)
    )
    sources_path = _atomic_write(
        entity_dir / "fundamental_sources.json",
        json.dumps(
            [_model_data(item) for item in result.bundle.sources],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    counts: Dict[str, int] = {}
    for fact in result.bundle.facts:
        counts[fact.research_module.value] = counts.get(fact.research_module.value, 0) + 1
    return GenericFundamentalArtifacts(
        entity_id=identity.entity_id,
        status=result.status,
        extraction_path=extraction_path,
        fundamentals_path=fundamentals_path,
        citations_path=citations_path,
        sources_path=sources_path,
        fact_count=len(result.bundle.facts),
        evidence_count=len(result.bundle.evidence),
        missing_information_count=len(result.bundle.missing_information),
        module_fact_counts=dict(sorted(counts.items())),
    )

