"""V3.3B 通用合并财务报表定位、核心科目提取与 Skill 数据产物。

本模块只处理已经由 V3.3A 下载、解析并完成主体复核的年度报告。它不会联网，
也不会调用 LLM。首版只把授信分析需要的核心科目映射为 ``FinancialItem``，
同时保留每个命中行的原文、单位、期间和 PDF 物理页码。无法唯一判断的空列、
单边数值或单位不会被猜测为 0。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic.v1 import BaseModel, Field

from credit_research_agent.documents import (
    DocumentBindingError,
    DownloadResult,
    PageExtractionStatus,
    PdfExtractionSummary,
    validate_download_binding,
)
from credit_research_agent.downloader import calculate_sha256
from credit_research_agent.evidence import load_parsed_pages
from credit_research_agent.financial import (
    ITEM_LABELS,
    REQUIRED_ITEMS,
    FinancialFact,
    FinancialItem,
    FinancialResearchBundle,
    FinancialSourceInput,
    FinancialStatement,
    FinancialValidationResult,
    build_financial_risk_flags,
    calculate_financial_metrics,
    calculate_financial_ratios,
    render_financial_data_appendix,
    validate_financial_facts,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    MissingInformation,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
)


class FinancialStatementExtractionError(RuntimeError):
    """带稳定错误码的通用报表提取失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StatementExtractionStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ExtractionIssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class FinancialExtractionIssue(BaseModel):
    code: str
    severity: ExtractionIssueSeverity
    message: str
    source_id: Optional[str] = None
    statement: Optional[FinancialStatement] = None
    item: Optional[FinancialItem] = None
    page_number: Optional[int] = None


class ExtractedStatementSection(BaseModel):
    source_id: str
    statement: FinancialStatement
    entity_scope: EntityScope = EntityScope.CONSOLIDATED
    heading: str
    start_page: int
    start_line: int
    end_page: int
    end_line: int
    current_period: str
    prior_period: str
    column_periods: Tuple[str, str]
    currency: str
    currency_basis: str
    original_unit: str
    unit_multiplier: Decimal
    comparison_restated: bool = False

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class ExtractedStatementRow(BaseModel):
    source_id: str
    statement: FinancialStatement
    entity_scope: EntityScope = EntityScope.CONSOLIDATED
    item: FinancialItem
    item_label: str
    original_label: str
    page_number: int
    line_number: int
    quote: str
    current_period: str
    prior_period: str
    current_raw_value: Optional[str] = None
    prior_raw_value: Optional[str] = None
    current_value_cny: Optional[Decimal] = None
    prior_value_cny: Optional[Decimal] = None
    currency: str
    currency_basis: str
    original_unit: str
    unit_multiplier: Decimal
    comparison_restated: bool = False
    extraction_status: str = "mapped"

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class FinancialStatementExtractionResult(BaseModel):
    schema_version: str = "3.3b.0"
    status: StatementExtractionStatus
    identity: CompanyIdentity
    generated_at: datetime
    current_period: Optional[str] = None
    prior_period: Optional[str] = None
    statements_found: List[FinancialStatement] = Field(default_factory=list)
    missing_statements: List[FinancialStatement] = Field(default_factory=list)
    sections: List[ExtractedStatementSection] = Field(default_factory=list)
    rows: List[ExtractedStatementRow] = Field(default_factory=list)
    raw_facts: List[FactRecord] = Field(default_factory=list)
    evidence: List[EvidenceRecord] = Field(default_factory=list)
    financial_facts: List[FinancialFact] = Field(default_factory=list)
    normalized_facts: list = Field(default_factory=list)
    metrics: list = Field(default_factory=list)
    ratios: list = Field(default_factory=list)
    risk_flags: list = Field(default_factory=list)
    sources: List[SourceRecord] = Field(default_factory=list)
    validation: Optional[FinancialValidationResult] = None
    missing_information: List[MissingInformation] = Field(default_factory=list)
    issues: List[FinancialExtractionIssue] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class GenericFinancialArtifacts(BaseModel):
    """通用报表数据产物；不包含 Python 编写的专业分析正文。"""

    entity_id: str
    status: StatementExtractionStatus
    current_period: Optional[str] = None
    prior_period: Optional[str] = None
    extraction_path: Path
    facts_path: Path
    metrics_path: Path
    risk_flags_path: Path
    sources_path: Path
    validation_path: Path
    explanations_path: Path
    reasonableness_path: Path
    data_appendix_path: Path
    fact_count: int
    metric_count: int
    ratio_count: int
    statements_found: List[FinancialStatement]
    missing_statements: List[FinancialStatement]

    def skill_context_paths(self) -> Dict[str, str]:
        """返回现有 financial-analysis 模型上下文构造器所需的路径键。"""

        return {
            "facts_path": str(self.facts_path),
            "metrics_path": str(self.metrics_path),
            "risk_flags_path": str(self.risk_flags_path),
            "sources_path": str(self.sources_path),
            "explanations_path": str(self.explanations_path),
            "reasonableness_path": str(self.reasonableness_path),
        }


class _LineRef(BaseModel):
    page_number: int
    line_number: int
    text: str


class _ItemRule(BaseModel):
    item: FinancialItem
    statement: FinancialStatement
    aliases: Tuple[str, ...]
    blockers: Tuple[str, ...] = ()


STATEMENT_HEADINGS: Mapping[FinancialStatement, str] = {
    FinancialStatement.BALANCE_SHEET: "合并资产负债表",
    FinancialStatement.INCOME_STATEMENT: "合并利润表",
    FinancialStatement.CASH_FLOW_STATEMENT: "合并现金流量表",
}

PARENT_HEADINGS: Mapping[FinancialStatement, str] = {
    FinancialStatement.BALANCE_SHEET: "母公司资产负债表",
    FinancialStatement.INCOME_STATEMENT: "母公司利润表",
    FinancialStatement.CASH_FLOW_STATEMENT: "母公司现金流量表",
}

ITEM_RULES: Tuple[_ItemRule, ...] = (
    _ItemRule(item=FinancialItem.MONETARY_FUNDS, statement=FinancialStatement.BALANCE_SHEET, aliases=("货币资金",)),
    _ItemRule(item=FinancialItem.ACCOUNTS_RECEIVABLE, statement=FinancialStatement.BALANCE_SHEET, aliases=("应收账款",)),
    _ItemRule(item=FinancialItem.PREPAYMENTS, statement=FinancialStatement.BALANCE_SHEET, aliases=("预付款项", "预付账款")),
    _ItemRule(item=FinancialItem.INVENTORY, statement=FinancialStatement.BALANCE_SHEET, aliases=("存货",)),
    _ItemRule(item=FinancialItem.FIXED_ASSETS, statement=FinancialStatement.BALANCE_SHEET, aliases=("固定资产",), blockers=("固定资产清理",)),
    _ItemRule(item=FinancialItem.SHORT_TERM_BORROWINGS, statement=FinancialStatement.BALANCE_SHEET, aliases=("短期借款",)),
    _ItemRule(item=FinancialItem.ACCOUNTS_PAYABLE, statement=FinancialStatement.BALANCE_SHEET, aliases=("应付账款",)),
    _ItemRule(item=FinancialItem.CURRENT_ASSETS, statement=FinancialStatement.BALANCE_SHEET, aliases=("流动资产合计", "流动资产总计"), blockers=("非流动资产合计", "非流动资产总计")),
    _ItemRule(item=FinancialItem.TOTAL_ASSETS, statement=FinancialStatement.BALANCE_SHEET, aliases=("资产总计", "资产合计"), blockers=("流动资产合计", "流动资产总计", "非流动资产合计", "非流动资产总计")),
    _ItemRule(item=FinancialItem.CURRENT_LIABILITIES, statement=FinancialStatement.BALANCE_SHEET, aliases=("流动负债合计", "流动负债总计"), blockers=("非流动负债合计", "非流动负债总计")),
    _ItemRule(item=FinancialItem.TOTAL_LIABILITIES, statement=FinancialStatement.BALANCE_SHEET, aliases=("负债合计", "负债总计"), blockers=("流动负债合计", "流动负债总计", "非流动负债合计", "非流动负债总计", "负债和所有者权益")),
    _ItemRule(item=FinancialItem.OPERATING_REVENUE, statement=FinancialStatement.INCOME_STATEMENT, aliases=("营业收入",), blockers=("营业总收入",)),
    _ItemRule(item=FinancialItem.OPERATING_COST, statement=FinancialStatement.INCOME_STATEMENT, aliases=("营业成本",), blockers=("营业总成本",)),
    _ItemRule(item=FinancialItem.NET_PROFIT, statement=FinancialStatement.INCOME_STATEMENT, aliases=("净利润",), blockers=("持续经营净利润", "终止经营净利润", "归属于母公司", "少数股东")),
    _ItemRule(item=FinancialItem.OPERATING_CASH_FLOW, statement=FinancialStatement.CASH_FLOW_STATEMENT, aliases=("经营活动产生的现金流量净额",)),
    _ItemRule(item=FinancialItem.CASH_RECEIVED_FROM_SALES, statement=FinancialStatement.CASH_FLOW_STATEMENT, aliases=("销售商品提供劳务收到的现金",)),
)


_HEADING_PREFIX = r"(?:\d+|[一二三四五六七八九十]+)?[、.]?"
_AMOUNT_TOKEN = re.compile(
    r"(?<![、\d])(?:[（(]?[+\-−－]?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[）)]?|[-—–－])"
    r"(?=\s|$)"
)
_UNIT_MULTIPLIERS: Mapping[str, Decimal] = {
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
}


def _model_data(model: BaseModel) -> dict:
    return json.loads(model.json())


def _compact(value: str) -> str:
    return re.sub(r"[\s：:，,、。．·“”‘’()（）\[\]【】]", "", value)


def parse_financial_amount(value: str) -> Optional[Decimal]:
    """解析年报金额；破折号表示缺失而不是零。"""

    token = str(value).strip().replace(" ", "")
    if token in {"", "-", "—", "–", "－"}:
        return None
    negative = False
    if (token.startswith("(") and token.endswith(")")) or (
        token.startswith("（") and token.endswith("）")
    ):
        negative = True
        token = token[1:-1]
    token = token.replace("−", "-").replace("－", "-").replace(",", "")
    try:
        amount = Decimal(token)
    except InvalidOperation as exc:
        raise FinancialStatementExtractionError(
            "invalid_financial_amount", f"无法解析财务金额：{value!r}"
        ) from exc
    return -abs(amount) if negative else amount


def _heading_pattern(title: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{_HEADING_PREFIX}\s*{re.escape(title)}\s*$")


def _period_year(source: SourceRecord) -> Optional[int]:
    match = re.search(r"(?:FY)?(20\d{2})", source.report_period or "")
    return int(match.group(1)) if match else None


def _normalise_quote(lines: Sequence[_LineRef]) -> str:
    quote = "\n".join(item.text.strip() for item in lines if item.text.strip()).strip()
    if len(quote) <= 500:
        return quote
    return quote[:497] + "..."


def _row_label(lines: Sequence[_LineRef], amount_matches: Sequence[re.Match[str]]) -> str:
    combined = "\n".join(item.text for item in lines)
    first = amount_matches[-2]
    second = amount_matches[-1]
    return (combined[: first.start()] + combined[second.end() :]).strip()


def _match_rule(
    label: str,
    statement: FinancialStatement,
    *,
    prefix_before_values: Optional[str] = None,
) -> Optional[_ItemRule]:
    compact = _compact(label)
    compact_prefix = _compact(prefix_before_values if prefix_before_values is not None else label)
    for rule in ITEM_RULES:
        if rule.statement is not statement:
            continue
        if any(_compact(marker) in compact for marker in rule.blockers):
            continue
        # 绝大多数科目名必须出现在两期金额之前，防止一个窗口先包含上一行
        # 的金额、后包含下一行科目名时把上一行金额错误绑定给下一科目。现金流
        # 长科目可能被PDF拆为“名称前半 → 两期金额 → 净额/金”，仅这两项允许
        # 使用金额后的短后缀完成名称匹配。
        searchable = (
            compact
            if rule.item
            in {
                FinancialItem.OPERATING_CASH_FLOW,
                FinancialItem.CASH_RECEIVED_FROM_SALES,
            }
            else compact_prefix
        )
        if any(_compact(alias) in searchable for alias in rule.aliases):
            return rule
    return None


def _looks_like_page_noise(value: str) -> bool:
    stripped = value.strip()
    return bool(
        re.fullmatch(r"\d+\s*/\s*\d+", stripped)
        or "年度报告" in stripped
        or stripped.startswith("公司负责人：")
    )


class FinancialStatementExtractor:
    """从 V3.3A 的逐页文本中确定性提取合并三表授信重点科目。"""

    def __init__(
        self, *, clock: Optional[Callable[[], datetime]] = None, max_row_lines: int = 4
    ) -> None:
        if max_row_lines < 1:
            raise ValueError("max_row_lines 必须为正数")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_row_lines = max_row_lines

    def extract(
        self,
        *,
        identity: CompanyIdentity,
        source_inputs: Sequence[FinancialSourceInput],
    ) -> FinancialStatementExtractionResult:
        if not source_inputs:
            raise FinancialStatementExtractionError(
                "missing_source_inputs", "至少需要一份已解析的官方年度报告"
            )
        source_ids = [item.source.source_id for item in source_inputs]
        if len(source_ids) != len(set(source_ids)):
            raise FinancialStatementExtractionError(
                "duplicate_source_input", "财务来源输入不得重复"
            )

        issues: List[FinancialExtractionIssue] = []
        sections: List[ExtractedStatementSection] = []
        rows: List[ExtractedStatementRow] = []
        source_hashes: Dict[str, str] = {}
        processed_sources: List[SourceRecord] = []

        for source_input in source_inputs:
            source = source_input.source
            if not source.entity_id:
                raise FinancialStatementExtractionError(
                    "source_entity_missing",
                    f"来源 {source.source_id} 未绑定研究主体，不能进入通用财务提取",
                )
            if source.entity_id != identity.entity_id:
                raise FinancialStatementExtractionError(
                    "source_entity_mismatch",
                    f"来源 {source.source_id} 的主体为 {source.entity_id}，与研究主体 {identity.entity_id} 不一致",
                )
            summary, line_refs = self._load_source(source_input)
            source_hashes[source.source_id] = summary.source_sha256
            source_sections, source_rows, source_issues = self._extract_source(
                identity=identity,
                source=source,
                lines=line_refs,
            )
            sections.extend(source_sections)
            rows.extend(source_rows)
            issues.extend(source_issues)
            pages = sorted({row.page_number for row in source_rows})
            processed_sources.append(
                source.copy(
                    update={
                        "status": SourceStatus.PARSED,
                        "accessed_at": source_input.download.accessed_at,
                        "local_path": Path(source_input.download.file_path).name,
                        "page_or_section": (
                            "PDF第" + "、".join(str(page) for page in pages) + "页"
                            if pages
                            else "未形成可用财务行"
                        ),
                    }
                )
            )

        rows, duplicate_issues = self._remove_conflicting_rows(rows)
        issues.extend(duplicate_issues)
        raw_facts, evidence, financial_facts = self._build_records(
            identity=identity,
            rows=rows,
            source_hashes=source_hashes,
        )
        financial_facts, restatement_issues = self._select_authoritative_facts(
            financial_facts=financial_facts,
            rows=rows,
            sources=processed_sources,
        )
        issues.extend(restatement_issues)
        periods = sorted(
            {fact.period for fact in financial_facts},
            key=lambda value: int(re.search(r"\d{4}", value).group()),  # type: ignore[union-attr]
        )
        normalized, validation = validate_financial_facts(
            financial_facts,
            required_periods=tuple(periods[-2:]) if periods else (),
        )
        metrics = calculate_financial_metrics(normalized)
        ratios = calculate_financial_ratios(normalized)
        risk_flags = build_financial_risk_flags(metrics, normalized)
        missing_information = self._missing_information(
            identity=identity,
            normalized=normalized,
            periods=periods[-2:],
        )

        found = sorted(
            {section.statement for section in sections}, key=lambda item: item.value
        )
        missing_statements = [
            statement for statement in FinancialStatement if statement not in found
        ]
        missing_required = bool(missing_information)
        if not found or not financial_facts:
            status = StatementExtractionStatus.FAILED
        elif (
            missing_statements
            or missing_required
            or not validation.is_valid
            or any(issue.severity is ExtractionIssueSeverity.ERROR for issue in issues)
        ):
            status = StatementExtractionStatus.PARTIAL_SUCCESS
        else:
            status = StatementExtractionStatus.SUCCESS
        current_period = periods[-1] if periods else None
        prior_period = periods[-2] if len(periods) >= 2 else None

        limitations = [
            "首版只结构化 financial-analysis Skill 所需的授信重点科目，不声称还原全部报表行。",
            "仅接受合并报表中能够唯一识别两期列的金额；单边空值、列错位或歧义行不会被猜测为零。",
            "财务报表附注中的变化原因尚未在V3.3B自动提取，后续分析必须标明资料限制。",
        ]
        if any(section.comparison_restated for section in sections):
            limitations.append("至少一张报表包含重述/调整后比较口径，已保留标记，分析时需核对口径变化。")
        if any(
            issue.code == "restated_comparative_value_selected" for issue in issues
        ):
            limitations.append(
                "跨年报比较期冲突已采用较新年报明确标注的调整后数值；原始差异仍保留供复核。"
            )
        return FinancialStatementExtractionResult(
            status=status,
            identity=identity,
            generated_at=self._clock(),
            current_period=current_period,
            prior_period=prior_period,
            statements_found=found,
            missing_statements=missing_statements,
            sections=sections,
            rows=rows,
            raw_facts=raw_facts,
            evidence=evidence,
            financial_facts=financial_facts,
            normalized_facts=normalized,
            metrics=metrics,
            ratios=ratios,
            risk_flags=risk_flags,
            sources=processed_sources,
            validation=validation,
            missing_information=missing_information,
            issues=issues,
            limitations=limitations,
        )

    @staticmethod
    def _load_source(
        source_input: FinancialSourceInput,
    ) -> Tuple[PdfExtractionSummary, List[_LineRef]]:
        try:
            validate_download_binding(source_input.source, source_input.download)
        except DocumentBindingError as exc:
            raise FinancialStatementExtractionError(exc.code, str(exc)) from exc
        try:
            summary = PdfExtractionSummary.parse_raw(
                Path(source_input.summary_path).read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise FinancialStatementExtractionError(
                "invalid_extraction_summary", f"无法读取解析摘要：{exc}"
            ) from exc
        source_id = source_input.source.source_id
        if (
            source_input.source.entity_id is None
            or source_input.source.source_type is not SourceType.ANNUAL_REPORT
            or source_input.source.trusted_tier != 1
        ):
            raise FinancialStatementExtractionError(
                "unsupported_source_binding",
                "通用财务提取只接受已绑定主体的一级年度报告全文",
            )
        if summary.source_id != source_id or source_input.download.source_id != source_id:
            raise FinancialStatementExtractionError(
                "source_id_mismatch", "来源编号在下载、解析和目录中不一致"
            )
        if summary.source_sha256 != source_input.download.sha256:
            raise FinancialStatementExtractionError(
                "source_hash_mismatch", "解析摘要与下载清单的PDF哈希不一致"
            )
        if Path(summary.source_file).resolve() != Path(source_input.download.file_path).resolve():
            raise FinancialStatementExtractionError(
                "extraction_file_mismatch", "PDF解析摘要指向的文件与下载清单不一致"
            )
        if not summary.pages_sha256 or calculate_sha256(Path(source_input.pages_path)) != summary.pages_sha256:
            raise FinancialStatementExtractionError(
                "pages_hash_mismatch", "pages.jsonl与解析摘要记录的哈希不一致"
            )
        pages = load_parsed_pages(source_input.pages_path)
        lines: List[_LineRef] = []
        for page_number in sorted(pages):
            page = pages[page_number]
            if page.status is not PageExtractionStatus.EXTRACTED:
                continue
            for line_number, text in enumerate(page.text.splitlines(), start=1):
                lines.append(
                    _LineRef(
                        page_number=page_number,
                        line_number=line_number,
                        text=text.strip(),
                    )
                )
        return summary, lines

    def _extract_source(
        self,
        *,
        identity: CompanyIdentity,
        source: SourceRecord,
        lines: Sequence[_LineRef],
    ) -> Tuple[List[ExtractedStatementSection], List[ExtractedStatementRow], List[FinancialExtractionIssue]]:
        del identity  # 主体已由 V3.3A 及入口处的 source.entity_id 完成绑定。
        sections: List[ExtractedStatementSection] = []
        rows: List[ExtractedStatementRow] = []
        issues: List[FinancialExtractionIssue] = []
        source_currency = self._detect_source_currency(lines)
        for statement, heading in STATEMENT_HEADINGS.items():
            located = self._locate_section(lines, statement, heading)
            if located is None:
                issues.append(
                    FinancialExtractionIssue(
                        code="statement_not_found",
                        severity=ExtractionIssueSeverity.WARNING,
                        message=f"未定位{heading}",
                        source_id=source.source_id,
                        statement=statement,
                    )
                )
                continue
            start_index, end_index = located
            section_lines = lines[start_index:end_index]
            try:
                section = self._parse_section_header(
                    source=source,
                    statement=statement,
                    heading=heading,
                    lines=section_lines,
                    source_currency=source_currency,
                )
            except FinancialStatementExtractionError as exc:
                issues.append(
                    FinancialExtractionIssue(
                        code=exc.code,
                        severity=ExtractionIssueSeverity.ERROR,
                        message=str(exc),
                        source_id=source.source_id,
                        statement=statement,
                        page_number=section_lines[0].page_number,
                    )
                )
                continue
            sections.append(section)
            section_rows, row_issues = self._parse_section_rows(section, section_lines)
            rows.extend(section_rows)
            issues.extend(row_issues)
        return sections, rows, issues

    @staticmethod
    def _detect_source_currency(lines: Sequence[_LineRef]) -> Optional[str]:
        """从年报会计政策或明确币种字段核实合并报表编报货币。"""

        for line in lines:
            compact = _compact(line.text).upper()
            if "币种人民币" in compact or "币种CNY" in compact:
                return "CNY"
            if re.search(r"本公司(?:以人民币为记账本位币|.*编制.*财务报表.*货币为人民币)", compact):
                return "CNY"
        return None

    @staticmethod
    def _locate_section(
        lines: Sequence[_LineRef], statement: FinancialStatement, heading: str
    ) -> Optional[Tuple[int, int]]:
        heading_re = _heading_pattern(heading)
        candidates: List[int] = []
        for index, line in enumerate(lines):
            if not heading_re.fullmatch(line.text):
                continue
            nearby = "\n".join(item.text for item in lines[index : index + 12])
            if "单位" in nearby and ("项目" in nearby or len(re.findall(r"20\d{2}", nearby)) >= 2):
                candidates.append(index)
        if not candidates:
            return None
        start = candidates[0]
        parent_re = _heading_pattern(PARENT_HEADINGS[statement])
        any_heading = re.compile(
            rf"^\s*{_HEADING_PREFIX}\s*(?:合并|母公司)(?:资产负债表|利润表|现金流量表)\s*$"
        )
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if parent_re.fullmatch(lines[index].text):
                end = index
                break
            if index > start + 10 and any_heading.fullmatch(lines[index].text):
                end = index
                break
        return start, end

    @staticmethod
    def _parse_section_header(
        *,
        source: SourceRecord,
        statement: FinancialStatement,
        heading: str,
        lines: Sequence[_LineRef],
        source_currency: Optional[str],
    ) -> ExtractedStatementSection:
        header = lines[:14]
        header_text = "\n".join(item.text for item in header)
        unit_match = re.search(
            r"单位\s*[：:]\s*(?:人民币)?\s*(亿元|百万元|万元|千元|元)"
            r"(?![A-Za-z\u4e00-\u9fff])",
            header_text,
        )
        if not unit_match:
            raise FinancialStatementExtractionError(
                "statement_unit_not_found", f"{heading}未找到可确认的金额单位"
            )
        original_unit = unit_match.group(1)
        unit_multiplier = _UNIT_MULTIPLIERS[original_unit]
        explicit_currency = re.search(r"币种\s*[：:]\s*([^\s，,；;]+)", header_text)
        if explicit_currency and explicit_currency.group(1).upper() not in {
            "人民币",
            "人民币元",
            "CNY",
            "RMB",
        }:
            raise FinancialStatementExtractionError(
                "unsupported_statement_currency",
                f"{heading}币种为{explicit_currency.group(1)}，首版只接受人民币合并报表",
            )
        if "美元" in unit_match.group(0) or "港币" in unit_match.group(0):
            raise FinancialStatementExtractionError(
                "unsupported_statement_currency", f"{heading}不是人民币报表"
            )
        header_has_cny = bool(
            "人民币" in unit_match.group(0)
            or (
                explicit_currency
                and explicit_currency.group(1).upper()
                in {"人民币", "人民币元", "CNY", "RMB"}
            )
        )
        if header_has_cny:
            currency = "CNY"
            currency_basis = "statement_header_explicit"
        elif source_currency == "CNY":
            currency = "CNY"
            currency_basis = "annual_report_accounting_policy"
        else:
            raise FinancialStatementExtractionError(
                "statement_currency_not_found",
                f"{heading}只披露金额单位，但未在表头或年报会计政策中核实人民币币种",
            )
        years: List[int] = []
        for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", header_text):
            year = int(match.group(1))
            if year not in years:
                years.append(year)
        report_year = _period_year(source)
        column_years: Optional[List[int]] = None
        for line in header:
            line_years: List[int] = []
            for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", line.text):
                year = int(match.group(1))
                if year not in line_years:
                    line_years.append(year)
            if len(line_years) > 2:
                raise FinancialStatementExtractionError(
                    "ambiguous_statement_period_columns",
                    f"{heading}列头包含两个以上可比年度，首版不猜测列对应关系",
                )
            if len(line_years) == 2:
                if column_years is not None and column_years != line_years:
                    raise FinancialStatementExtractionError(
                        "ambiguous_statement_period_columns",
                        f"{heading}存在冲突的年度列顺序",
                    )
                column_years = line_years
        if report_year is not None and report_year not in years:
            years.insert(0, report_year)
        if column_years is None and report_year is not None and (
            "期末" in header_text
            or "期初" in header_text
            or "本期" in header_text
            or "上期" in header_text
            or "本年" in header_text
            or "上年" in header_text
        ):
            column_years = [report_year, report_year - 1]
        if column_years is None and len(years) == 2:
            column_years = list(years)
        if column_years is None or len(column_years) != 2:
            raise FinancialStatementExtractionError(
                "statement_periods_not_found", f"{heading}未找到两期可比年度"
            )
        if report_year in column_years:
            current_year = report_year  # type: ignore[assignment]
            prior_candidates = [year for year in column_years if year != current_year]
            prior_year = prior_candidates[0] if prior_candidates else current_year - 1
        else:
            current_year, prior_year = column_years[0], column_years[1]
        return ExtractedStatementSection(
            source_id=source.source_id,
            statement=statement,
            heading=heading,
            start_page=lines[0].page_number,
            start_line=lines[0].line_number,
            end_page=lines[-1].page_number,
            end_line=lines[-1].line_number,
            current_period=f"FY{current_year}",
            prior_period=f"FY{prior_year}",
            column_periods=tuple(f"FY{year}" for year in column_years),
            currency=currency,
            currency_basis=currency_basis,
            original_unit=original_unit,
            unit_multiplier=unit_multiplier,
            comparison_restated=bool(re.search(r"重述|调整后|追溯调整", header_text)),
        )

    def _parse_section_rows(
        self,
        section: ExtractedStatementSection,
        lines: Sequence[_LineRef],
    ) -> Tuple[List[ExtractedStatementRow], List[FinancialExtractionIssue]]:
        rules = [rule for rule in ITEM_RULES if rule.statement is section.statement]
        found: Dict[FinancialItem, ExtractedStatementRow] = {}
        issues: List[FinancialExtractionIssue] = []
        for index, start in enumerate(lines):
            if _looks_like_page_noise(start.text):
                continue
            for width in range(1, self.max_row_lines + 1):
                window = list(lines[index : index + width])
                if len(window) != width or any(item.page_number != start.page_number for item in window):
                    break
                combined = "\n".join(item.text for item in window)
                amount_matches = list(_AMOUNT_TOKEN.finditer(combined))
                label = _row_label(window, amount_matches) if len(amount_matches) >= 2 else combined
                prefix_before_values = (
                    combined[: amount_matches[-2].start()]
                    if len(amount_matches) >= 2
                    else combined
                )
                rule = _match_rule(
                    label,
                    section.statement,
                    prefix_before_values=prefix_before_values,
                )
                if rule is None or rule not in rules:
                    continue
                if len(amount_matches) < 2:
                    if width < self.max_row_lines:
                        continue
                    issues.append(
                        FinancialExtractionIssue(
                            code="ambiguous_amount_columns",
                            severity=ExtractionIssueSeverity.WARNING,
                            message=f"{ITEM_LABELS[rule.item]}未唯一识别两期金额，不猜测空列",
                            source_id=section.source_id,
                            statement=section.statement,
                            item=rule.item,
                            page_number=start.page_number,
                        )
                    )
                    break
                value_matches = self._value_column_matches(combined, amount_matches)
                if len(value_matches) != 2:
                    if width < self.max_row_lines:
                        continue
                    issues.append(
                        FinancialExtractionIssue(
                            code="ambiguous_amount_columns",
                            severity=ExtractionIssueSeverity.WARNING,
                            message=f"{ITEM_LABELS[rule.item]}存在无法唯一对应期间的金额列",
                            source_id=section.source_id,
                            statement=section.statement,
                            item=rule.item,
                            page_number=start.page_number,
                        )
                    )
                    break
                values_by_period = {
                    section.column_periods[0]: value_matches[0].group(),
                    section.column_periods[1]: value_matches[1].group(),
                }
                current_raw = values_by_period[section.current_period]
                prior_raw = values_by_period[section.prior_period]
                current = parse_financial_amount(current_raw)
                prior = parse_financial_amount(prior_raw)
                row = ExtractedStatementRow(
                    source_id=section.source_id,
                    statement=section.statement,
                    item=rule.item,
                    item_label=ITEM_LABELS[rule.item],
                    original_label=label.replace("\n", " ").strip(),
                    page_number=start.page_number,
                    line_number=start.line_number,
                    quote=_normalise_quote(window),
                    current_period=section.current_period,
                    prior_period=section.prior_period,
                    current_raw_value=current_raw,
                    prior_raw_value=prior_raw,
                    current_value_cny=(current * section.unit_multiplier if current is not None else None),
                    prior_value_cny=(prior * section.unit_multiplier if prior is not None else None),
                    currency=section.currency,
                    currency_basis=section.currency_basis,
                    original_unit=section.original_unit,
                    unit_multiplier=section.unit_multiplier,
                    comparison_restated=section.comparison_restated,
                    extraction_status=("mapped" if current is not None and prior is not None else "incomplete"),
                )
                existing = found.get(rule.item)
                if existing is None:
                    found[rule.item] = row
                elif (
                    existing.current_value_cny != row.current_value_cny
                    or existing.prior_value_cny != row.prior_value_cny
                ):
                    issues.append(
                        FinancialExtractionIssue(
                            code="duplicate_item_conflict",
                            severity=ExtractionIssueSeverity.ERROR,
                            message=f"{ITEM_LABELS[rule.item]}在同一合并报表中出现冲突数值",
                            source_id=section.source_id,
                            statement=section.statement,
                            item=rule.item,
                            page_number=start.page_number,
                        )
                    )
                    found.pop(rule.item, None)
                break
        for rule in rules:
            if rule.item not in found:
                issues.append(
                    FinancialExtractionIssue(
                        code="financial_item_not_found",
                        severity=ExtractionIssueSeverity.WARNING,
                        message=f"{section.heading}未形成{ITEM_LABELS[rule.item]}的两期确定性数据",
                        source_id=section.source_id,
                        statement=section.statement,
                        item=rule.item,
                        page_number=section.start_page,
                    )
                )
        # 滑动窗口会在真正科目行之前经过“上一行金额 + 当前科目”组合。
        # 若该科目稍后已由唯一两列成功解析，早期窗口的歧义只是搜索过程噪声，
        # 不应进入最终审计结果；真正未解析的多列科目仍保留警告。
        issues = [
            issue
            for issue in issues
            if not (
                issue.code == "ambiguous_amount_columns"
                and issue.item in found
            )
        ]
        return list(found.values()), issues

    @staticmethod
    def _value_column_matches(
        combined: str, amount_matches: Sequence[re.Match[str]]
    ) -> List[re.Match[str]]:
        """排除窗口中的年度标签；金额列多于两列时交由上层拒绝。"""

        values: List[re.Match[str]] = []
        for match in amount_matches:
            token = match.group().strip("()（）+-−－").replace(",", "")
            next_text = combined[match.end() : match.end() + 2].lstrip()
            if token.isdigit() and 2000 <= int(token) <= 2099 and next_text.startswith("年"):
                continue
            values.append(match)
        return values[-2:] if len(values) == 2 else values

    @staticmethod
    def _remove_conflicting_rows(
        rows: Sequence[ExtractedStatementRow],
    ) -> Tuple[List[ExtractedStatementRow], List[FinancialExtractionIssue]]:
        grouped: Dict[Tuple[str, FinancialItem, str], List[ExtractedStatementRow]] = {}
        for row in rows:
            grouped.setdefault((row.source_id, row.item, row.current_period), []).append(row)
        kept: List[ExtractedStatementRow] = []
        issues: List[FinancialExtractionIssue] = []
        for (_, item, _), values in grouped.items():
            unique = {(value.current_value_cny, value.prior_value_cny) for value in values}
            if len(unique) > 1:
                first = values[0]
                issues.append(
                    FinancialExtractionIssue(
                        code="cross_section_item_conflict",
                        severity=ExtractionIssueSeverity.ERROR,
                        message=f"{ITEM_LABELS[item]}在同一来源出现冲突数据",
                        source_id=first.source_id,
                        statement=first.statement,
                        item=item,
                        page_number=first.page_number,
                    )
                )
                continue
            kept.append(values[0])
        return sorted(kept, key=lambda row: (row.source_id, row.page_number, row.line_number, row.item.value)), issues

    @staticmethod
    def _build_records(
        *,
        identity: CompanyIdentity,
        rows: Sequence[ExtractedStatementRow],
        source_hashes: Mapping[str, str],
    ) -> Tuple[List[FactRecord], List[EvidenceRecord], List[FinancialFact]]:
        raw_facts: List[FactRecord] = []
        evidence: List[EvidenceRecord] = []
        financial_facts: List[FinancialFact] = []
        for row in rows:
            evidence_id = f"E{len(evidence) + 1:03d}"
            evidence.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source_id=row.source_id,
                    source_sha256=source_hashes[row.source_id],
                    page_number=row.page_number,
                    line_number=row.line_number,
                    page_or_section=f"PDF第{row.page_number}页，{STATEMENT_HEADINGS[row.statement]}",
                    quote=row.quote,
                    status=EvidenceStatus.VERIFIED,
                    verification_method=VerificationMethod.EXACT_TEXT_MATCH,
                )
            )
            for period, value in (
                (row.current_period, row.current_value_cny),
                (row.prior_period, row.prior_value_cny),
            ):
                if value is None:
                    continue
                fact_id = f"F{len(raw_facts) + 1:03d}"
                value_text = format(value, "f")
                raw_facts.append(
                    FactRecord(
                        fact_id=fact_id,
                        entity_id=identity.entity_id,
                        subject=identity.legal_name,
                        category=FactCategory.FINANCIAL,
                        research_module=ResearchModule.FINANCIAL_ANALYSIS,
                        field_name=row.item.value,
                        value=value_text,
                        claim=f"{period}合并口径{ITEM_LABELS[row.item]}为{value_text}元",
                        entity_scope=EntityScope.CONSOLIDATED,
                        period=period,
                        evidence_ids=[evidence_id],
                    )
                )
                financial_facts.append(
                    FinancialFact(
                        fact_id=fact_id,
                        entity_id=identity.entity_id,
                        item=row.item,
                        item_label=ITEM_LABELS[row.item],
                        statement=row.statement,
                        period=period,
                        value=value,
                        unit="CNY",
                        entity_scope=EntityScope.CONSOLIDATED,
                        source_id=row.source_id,
                        evidence_ids=[evidence_id],
                    )
                )
        return raw_facts, evidence, financial_facts

    @staticmethod
    def _select_authoritative_facts(
        *,
        financial_facts: Sequence[FinancialFact],
        rows: Sequence[ExtractedStatementRow],
        sources: Sequence[SourceRecord],
    ) -> Tuple[List[FinancialFact], List[FinancialExtractionIssue]]:
        """仅在较新年报明确标注比较列已调整时解决跨报告数值冲突。

        未标记为重述/调整后的冲突仍交给既有校验器拒绝，避免把解析错误
        或口径差异静默覆盖成一个看似确定的数值。
        """

        source_years = {source.source_id: _period_year(source) for source in sources}
        restated_keys = {
            (row.source_id, row.item, row.prior_period)
            for row in rows
            if row.comparison_restated
        }
        grouped: Dict[
            Tuple[str, FinancialItem, str, str, EntityScope], List[FinancialFact]
        ] = {}
        for fact in financial_facts:
            grouped.setdefault(
                (fact.entity_id, fact.item, fact.period, fact.unit, fact.entity_scope),
                [],
            ).append(fact)

        selected: List[FinancialFact] = []
        issues: List[FinancialExtractionIssue] = []
        for (_, item, period, _, _), facts in grouped.items():
            values = {fact.value for fact in facts}
            if len(values) <= 1:
                selected.extend(facts)
                continue
            restated = [
                fact
                for fact in facts
                if (fact.source_id, fact.item, fact.period) in restated_keys
                and source_years.get(fact.source_id) is not None
            ]
            if not restated:
                selected.extend(facts)
                continue
            latest_year = max(source_years[fact.source_id] for fact in restated)
            latest = [
                fact
                for fact in restated
                if source_years[fact.source_id] == latest_year
            ]
            latest_values = {fact.value for fact in latest}
            if len(latest_values) != 1:
                selected.extend(facts)
                continue
            authoritative_value = next(iter(latest_values))
            selected.extend(
                fact for fact in facts if fact.value == authoritative_value
            )
            chosen = latest[0]
            issues.append(
                FinancialExtractionIssue(
                    code="restated_comparative_value_selected",
                    severity=ExtractionIssueSeverity.WARNING,
                    message=(
                        f"{ITEM_LABELS[item]} {period}存在跨年报差异；"
                        f"采用较新年报 {chosen.source_id} 明确标注的调整后比较值，"
                        "原始冲突仍保留在提取记录中"
                    ),
                    source_id=chosen.source_id,
                    statement=chosen.statement,
                    item=item,
                )
            )
        return selected, issues

    @staticmethod
    def _missing_information(
        *, identity: CompanyIdentity, normalized: Sequence, periods: Sequence[str]
    ) -> List[MissingInformation]:
        available = {(fact.item, fact.period) for fact in normalized}
        missing: List[MissingInformation] = []
        for item in REQUIRED_ITEMS:
            absent_periods = [period for period in periods if (item, period) not in available]
            if not absent_periods:
                continue
            missing.append(
                MissingInformation(
                    missing_id=f"M{len(missing) + 1:03d}",
                    entity_id=identity.entity_id,
                    research_module=ResearchModule.FINANCIAL_ANALYSIS,
                    topic=ITEM_LABELS[item],
                    description=f"{ITEM_LABELS[item]}缺少可比期间：{'、'.join(absent_periods)}",
                    reason="合并财务报表未形成可唯一定位的两期金额，未猜测空列或补零。",
                )
            )
        return missing


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


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def build_generic_financial_artifacts(
    *,
    identity: CompanyIdentity,
    source_inputs: Sequence[FinancialSourceInput],
    output_dir: Path,
    clock: Optional[Callable[[], datetime]] = None,
) -> GenericFinancialArtifacts:
    """把自动抽取结果写成现有 financial-analysis 上下文可消费的数据文件。"""

    result = FinancialStatementExtractor(clock=clock).extract(
        identity=identity, source_inputs=source_inputs
    )
    entity_dir = Path(output_dir) / identity.entity_id / "financial"
    extraction_path = entity_dir / "financial_statement_extraction.json"
    facts_path = entity_dir / "financial_facts.json"
    metrics_path = entity_dir / "financial_metrics.json"
    risk_flags_path = entity_dir / "financial_risk_flags.json"
    sources_path = entity_dir / "financial_sources.json"
    validation_path = entity_dir / "financial_validation.json"
    explanations_path = entity_dir / "financial_explanations.json"
    reasonableness_path = entity_dir / "financial_reasonableness.json"
    data_appendix_path = entity_dir / "financial_data_appendix.md"

    extraction_payload = _model_data(result)
    _atomic_write_json(extraction_path, extraction_payload)
    _atomic_write_json(
        facts_path,
        {
            "schema_version": "3.3b.0",
            "identity": _model_data(identity),
            "raw_facts": [_model_data(item) for item in result.raw_facts],
            "financial_facts": [_model_data(item) for item in result.financial_facts],
            "normalized_facts": [_model_data(item) for item in result.normalized_facts],
            "evidence": [_model_data(item) for item in result.evidence],
            "missing_information": [_model_data(item) for item in result.missing_information],
            "limitations": result.limitations,
        },
    )
    _atomic_write_json(
        metrics_path,
        {
            "metrics": [_model_data(item) for item in result.metrics],
            "ratios": [_model_data(item) for item in result.ratios],
        },
    )
    _atomic_write_json(risk_flags_path, [_model_data(item) for item in result.risk_flags])
    _atomic_write_json(
        sources_path,
        {
            "sources": [_model_data(item) for item in result.sources],
            "evidence": [_model_data(item) for item in result.evidence],
        },
    )
    _atomic_write_json(validation_path, _model_data(result.validation) if result.validation else {})
    _atomic_write_json(
        explanations_path,
        {
            "explanations": [],
            "inferences": [],
            "hypotheses": [],
            "limitations": ["V3.3B尚未自动提取财务附注变化原因。"],
        },
    )
    _atomic_write_json(reasonableness_path, [])

    bundle = FinancialResearchBundle(
        schema_version="3.3b.0",
        identity=identity,
        generated_at=result.generated_at,
        raw_facts=result.raw_facts,
        financial_facts=result.financial_facts,
        normalized_facts=result.normalized_facts,
        evidence=result.evidence,
        sources=result.sources,
        metrics=result.metrics,
        ratios=result.ratios,
        risk_flags=result.risk_flags,
        validation=result.validation
        or FinancialValidationResult(is_valid=False, issues=[], required_coverage={}),
        missing_information=result.missing_information,
        limitations=result.limitations,
    )
    _atomic_write_text(data_appendix_path, render_financial_data_appendix(bundle))
    return GenericFinancialArtifacts(
        entity_id=identity.entity_id,
        status=result.status,
        current_period=result.current_period,
        prior_period=result.prior_period,
        extraction_path=extraction_path.resolve(),
        facts_path=facts_path.resolve(),
        metrics_path=metrics_path.resolve(),
        risk_flags_path=risk_flags_path.resolve(),
        sources_path=sources_path.resolve(),
        validation_path=validation_path.resolve(),
        explanations_path=explanations_path.resolve(),
        reasonableness_path=reasonableness_path.resolve(),
        data_appendix_path=data_appendix_path.resolve(),
        fact_count=len(result.normalized_facts),
        metric_count=len(result.metrics),
        ratio_count=len(result.ratios),
        statements_found=result.statements_found,
        missing_statements=result.missing_statements,
    )


def build_generic_financial_skill_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    comparison_inputs: Sequence[FinancialSourceInput] = (),
    clock: Optional[Callable[[], datetime]] = None,
) -> GenericFinancialArtifacts:
    """适配现有 ``financial-analysis`` Skill Tool 调用契约。

    旧 Tool 从公司专属 Seeds 构造数据；通用生产路径把 Runtime 已绑定的主年报
    和可比年报转成同一组 ``FinancialSourceInput``，再调用确定性通用提取器。
    """

    primary = FinancialSourceInput(
        source=source,
        download=download,
        summary_path=Path(summary_path),
        pages_path=Path(pages_path),
    )
    return build_generic_financial_artifacts(
        identity=identity,
        source_inputs=[primary, *list(comparison_inputs)],
        output_dir=output_dir,
        clock=clock,
    )

