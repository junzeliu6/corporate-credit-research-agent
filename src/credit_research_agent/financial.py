"""V2.1-V2.3 财务事实、确定性计算与专业分析流程。

本模块坚持一个边界：PDF 原文只负责形成带页码的事实，Python 只基于这些事实
做可复现计算。V2.3 允许形成支持性推断和待核实假设，但二者必须与公司披露
原因分层保存，绝不能回写为 ``FactRecord`` 或冒充年报事实。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic.v1 import BaseModel, Field, root_validator, validator

from credit_research_agent.documents import DownloadResult
from credit_research_agent.evidence import (
    EvidenceBuilder,
    EvidenceSeed,
    EvidenceSeedCatalog,
    FactSeed,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    EvidenceRecord,
    FactCategory,
    FactRecord,
    MissingInformation,
    ModelInference,
    ResearchModule,
    SourceRecord,
)


class FinancialBuildError(RuntimeError):
    """财务构建失败；code 供 CLI、测试和 Skill Runtime 诊断。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _financial_period_sort_key(period: str) -> Tuple[int, int, str]:
    """为财务期间提供稳定排序；标准 ``FY2025`` 按年度数值排序。"""

    match = re.fullmatch(r"FY(\d{4})", period.strip(), flags=re.IGNORECASE)
    if match:
        return (1, int(match.group(1)), period)
    return (0, 0, period)


def sort_financial_periods(periods: Iterable[str]) -> List[str]:
    """去重并排序财务期间，供提取、计算和报告层共享。"""

    return sorted(
        {period for period in periods if period},
        key=_financial_period_sort_key,
    )


def latest_financial_period(periods: Iterable[str]) -> Optional[str]:
    """返回最新可用财务期间；没有期间时返回 ``None``。"""

    ordered = sort_financial_periods(periods)
    return ordered[-1] if ordered else None


class FinancialStatement(str, Enum):
    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    CASH_FLOW_STATEMENT = "cash_flow_statement"


class FinancialItem(str, Enum):
    MONETARY_FUNDS = "monetary_funds"
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    PREPAYMENTS = "prepayments"
    INVENTORY = "inventory"
    FIXED_ASSETS = "fixed_assets"
    SHORT_TERM_BORROWINGS = "short_term_borrowings"
    ACCOUNTS_PAYABLE = "accounts_payable"
    CURRENT_ASSETS = "current_assets"
    TOTAL_ASSETS = "total_assets"
    CURRENT_LIABILITIES = "current_liabilities"
    TOTAL_LIABILITIES = "total_liabilities"
    TOTAL_EQUITY = "total_equity"
    OPERATING_REVENUE = "operating_revenue"
    OPERATING_COST = "operating_cost"
    NET_PROFIT = "net_profit"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CASH_RECEIVED_FROM_SALES = "cash_received_from_sales"


ITEM_LABELS: Dict[FinancialItem, str] = {
    FinancialItem.MONETARY_FUNDS: "货币资金",
    FinancialItem.ACCOUNTS_RECEIVABLE: "应收账款",
    FinancialItem.PREPAYMENTS: "预付款项",
    FinancialItem.INVENTORY: "存货",
    FinancialItem.FIXED_ASSETS: "固定资产",
    FinancialItem.SHORT_TERM_BORROWINGS: "短期借款",
    FinancialItem.ACCOUNTS_PAYABLE: "应付账款",
    FinancialItem.CURRENT_ASSETS: "流动资产合计",
    FinancialItem.TOTAL_ASSETS: "资产总计",
    FinancialItem.CURRENT_LIABILITIES: "流动负债合计",
    FinancialItem.TOTAL_LIABILITIES: "负债合计",
    FinancialItem.TOTAL_EQUITY: "所有者权益合计",
    FinancialItem.OPERATING_REVENUE: "营业收入",
    FinancialItem.OPERATING_COST: "营业成本",
    FinancialItem.NET_PROFIT: "净利润",
    FinancialItem.OPERATING_CASH_FLOW: "经营活动产生的现金流量净额",
    FinancialItem.CASH_RECEIVED_FROM_SALES: "销售商品、提供劳务收到的现金",
}


class FinancialChangeKind(str, Enum):
    COMPARABLE = "comparable"
    ZERO_BASE = "zero_base"
    SIGN_CHANGE = "sign_change"
    LOSS_EXPANSION = "loss_expansion"
    LOSS_NARROWING = "loss_narrowing"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class FinancialExplanationKind(str, Enum):
    """财务变化解释的证据等级。"""

    DISCLOSED_REASON = "disclosed_reason"
    SUPPORTED_INFERENCE = "supported_inference"
    UNVERIFIED_HYPOTHESIS = "unverified_hypothesis"


class ReasonablenessStatus(str, Enum):
    """原因合理性评价，不等同于审计或授信结论。"""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class FinancialSourceInput(BaseModel):
    """一份已下载、解析并与来源目录绑定的年报输入。"""

    source: SourceRecord
    download: DownloadResult
    summary_path: Path
    pages_path: Path


class FinancialLineSeed(BaseModel):
    """人工审阅的报表行；一行可同时承载本期和上期事实。"""

    entity_id: str
    source_id: str
    evidence_id: str
    fact_id_current: Optional[str] = None
    fact_id_prior: Optional[str] = None
    page_number: int = Field(ge=1)
    page_or_section: str
    match_text: str
    item: FinancialItem
    statement: FinancialStatement
    current_period: Optional[str] = None
    current_value: Optional[str] = None
    prior_period: Optional[str] = None
    prior_value: Optional[str] = None
    unit: str = "CNY"
    entity_scope: EntityScope = EntityScope.CONSOLIDATED

    @root_validator
    def validate_period_value_pairs(cls, values):
        for prefix in ("current", "prior"):
            period = values.get(f"{prefix}_period")
            value = values.get(f"{prefix}_value")
            fact_id = values.get(f"fact_id_{prefix}")
            if any(item is not None for item in (period, value, fact_id)) and not all(
                item is not None for item in (period, value, fact_id)
            ):
                raise ValueError(f"{prefix} 的 fact_id、period、value 必须同时提供")
        if values.get("current_value") is None and values.get("prior_value") is None:
            raise ValueError("财务报表行至少需要一个可核实数值")
        return values


class FinancialSeedCatalog(BaseModel):
    entity_id: str
    lines: List[FinancialLineSeed]
    missing_information: List[MissingInformation] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)

    def for_source(self, source_id: str) -> EvidenceSeedCatalog:
        lines = [item for item in self.lines if item.source_id == source_id]
        if not lines:
            raise FinancialBuildError(
                "missing_financial_seeds", f"来源 {source_id} 没有财务事实锚点"
            )
        evidence: List[EvidenceSeed] = []
        facts: List[FactSeed] = []
        for line in lines:
            evidence.append(
                EvidenceSeed(
                    evidence_id=line.evidence_id,
                    entity_id=line.entity_id,
                    source_id=line.source_id,
                    page_number=line.page_number,
                    page_or_section=line.page_or_section,
                    match_text=line.match_text,
                )
            )
            for prefix in ("current", "prior"):
                fact_id = getattr(line, f"fact_id_{prefix}")
                period = getattr(line, f"{prefix}_period")
                value = getattr(line, f"{prefix}_value")
                if fact_id is None or period is None or value is None:
                    continue
                facts.append(
                    FactSeed(
                        fact_id=fact_id,
                        entity_id=line.entity_id,
                        category=FactCategory.FINANCIAL,
                        research_module=ResearchModule.FINANCIAL_ANALYSIS,
                        field_name=line.item.value,
                        value=value,
                        claim=(
                            f"{period}合并口径{ITEM_LABELS[line.item]}为"
                            f"{value}元"
                        ),
                        entity_scope=line.entity_scope,
                        period=period,
                        evidence_ids=[line.evidence_id],
                    )
                )
        return EvidenceSeedCatalog(evidence=evidence, facts=facts)


class FinancialExplanationLinkSeed(BaseModel):
    """把人工核对的披露事实绑定到一个财务变动项目。"""

    explanation_id: str
    kind: FinancialExplanationKind
    item: FinancialItem
    prior_period: str
    current_period: str
    statement: str = Field(min_length=1)
    fact_ids: List[str] = Field(min_items=1)


class FinancialExplanationSeedCatalog(BaseModel):
    """年报附注/管理层讨论中的原因证据锚点。

    数值主表和原因披露分开维护：前者用于计算，后者只为解释提供原文证据。
    """

    entity_id: str
    evidence: List[EvidenceSeed]
    facts: List[FactSeed]
    links: List[FinancialExplanationLinkSeed]
    limitations: List[str] = Field(default_factory=list)

    def source_ids(self) -> set[str]:
        return {item.source_id for item in self.evidence}

    def for_source(self, source_id: str) -> EvidenceSeedCatalog:
        evidence = [item for item in self.evidence if item.source_id == source_id]
        evidence_ids = {item.evidence_id for item in evidence}
        facts = [
            item
            for item in self.facts
            if set(item.evidence_ids).issubset(evidence_ids)
        ]
        if not evidence:
            raise FinancialBuildError(
                "missing_financial_explanation_seeds",
                f"来源 {source_id} 没有财务原因证据锚点",
            )
        return EvidenceSeedCatalog(evidence=evidence, facts=facts)


class FinancialFact(BaseModel):
    """带数值语义的单来源财务事实，fact_id 沿用 FactRecord。"""

    fact_id: str
    entity_id: str
    item: FinancialItem
    item_label: str
    statement: FinancialStatement
    period: str
    value: Decimal
    unit: str = "CNY"
    entity_scope: EntityScope = EntityScope.CONSOLIDATED
    source_id: str
    evidence_ids: List[str] = Field(min_items=1)
    verification_status: str = "verified"

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class NormalizedFinancialFact(BaseModel):
    """同一主体、指标、期间、口径归一后的事实，可有多份报告佐证。"""

    normalized_fact_id: str
    entity_id: str
    item: FinancialItem
    item_label: str
    statement: FinancialStatement
    period: str
    value: Decimal
    unit: str
    entity_scope: EntityScope
    supporting_fact_ids: List[str]
    source_ids: List[str]
    evidence_ids: List[str]
    verification_status: str = "verified"

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class FinancialValidationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    message: str
    item: Optional[FinancialItem] = None
    period: Optional[str] = None


class FinancialValidationResult(BaseModel):
    is_valid: bool
    issues: List[FinancialValidationIssue]
    required_coverage: Dict[str, Dict[str, bool]]


class FinancialMetric(BaseModel):
    metric_id: str
    entity_id: str
    item: FinancialItem
    item_label: str
    prior_period: str
    current_period: str
    prior_value: Decimal
    current_value: Decimal
    absolute_change: Decimal
    yoy_percent: Optional[Decimal] = None
    change_kind: FinancialChangeKind
    formula: str
    requires_deep_analysis: bool
    trigger_reasons: List[str] = Field(default_factory=list)
    supporting_fact_ids: List[str]
    source_ids: List[str]

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class FinancialRatio(BaseModel):
    ratio_id: str
    name: str
    period: str
    value_percent: Decimal
    formula: str
    supporting_fact_ids: List[str]

    class Config:
        json_encoders = {Decimal: lambda value: format(value, "f")}


class FinancialRiskFlag(BaseModel):
    flag_id: str
    severity: str
    topic: str
    observation: str
    supporting_metric_ids: List[str] = Field(default_factory=list)
    supporting_fact_ids: List[str] = Field(default_factory=list)
    requires_follow_up: bool = True


class FinancialChangeExplanation(BaseModel):
    """一项重大变化的披露原因或支持性推断。"""

    explanation_id: str
    metric_id: str
    kind: FinancialExplanationKind
    statement: str = Field(min_length=1)
    disclosed_fact_ids: List[str] = Field(default_factory=list)
    supporting_fact_ids: List[str] = Field(default_factory=list)
    inference_id: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    limitation: Optional[str] = None

    @root_validator
    def validate_explanation_evidence(cls, values):
        kind = values.get("kind")
        disclosed_fact_ids = values.get("disclosed_fact_ids") or []
        evidence_ids = values.get("evidence_ids") or []
        source_ids = values.get("source_ids") or []
        if kind is FinancialExplanationKind.DISCLOSED_REASON:
            if not disclosed_fact_ids or not evidence_ids or not source_ids:
                raise ValueError("年报披露原因必须关联事实、证据和来源")
        if kind is FinancialExplanationKind.SUPPORTED_INFERENCE:
            if not values.get("supporting_fact_ids") or not values.get("inference_id"):
                raise ValueError("支持性推断必须关联事实和ModelInference")
        return values


class FinancialHypothesis(BaseModel):
    """没有直接证据的待核实假设；禁止作为确定性企业事实输出。"""

    hypothesis_id: str
    metric_id: str
    kind: FinancialExplanationKind = FinancialExplanationKind.UNVERIFIED_HYPOTHESIS
    statement: str = Field(min_length=1)
    required_evidence: List[str] = Field(min_items=1)
    verification_status: str = "unverified"

    @validator("kind")
    def kind_is_hypothesis(cls, value: FinancialExplanationKind):
        if value is not FinancialExplanationKind.UNVERIFIED_HYPOTHESIS:
            raise ValueError("FinancialHypothesis.kind 必须为 unverified_hypothesis")
        return value


class FinancialReasonablenessAssessment(BaseModel):
    """对变化解释的方向一致性和证据完整性作确定性评价。"""

    assessment_id: str
    metric_id: str
    status: ReasonablenessStatus
    conclusion: str = Field(min_length=1)
    explanation_ids: List[str] = Field(default_factory=list)
    supporting_metric_ids: List[str] = Field(default_factory=list)
    supporting_fact_ids: List[str] = Field(default_factory=list)
    contradicting_observations: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    credit_concern: str = Field(min_length=1)
    due_diligence_questions: List[str] = Field(min_items=1)


class FinancialResearchBundle(BaseModel):
    schema_version: str = "2.3.0"
    identity: CompanyIdentity
    generated_at: datetime
    raw_facts: List[FactRecord]
    financial_facts: List[FinancialFact]
    normalized_facts: List[NormalizedFinancialFact]
    evidence: List[EvidenceRecord]
    sources: List[SourceRecord]
    metrics: List[FinancialMetric]
    ratios: List[FinancialRatio]
    risk_flags: List[FinancialRiskFlag]
    explanations: List[FinancialChangeExplanation] = Field(default_factory=list)
    inferences: List[ModelInference] = Field(default_factory=list)
    hypotheses: List[FinancialHypothesis] = Field(default_factory=list)
    reasonableness_assessments: List[FinancialReasonablenessAssessment] = Field(
        default_factory=list
    )
    validation: FinancialValidationResult
    missing_information: List[MissingInformation] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)

    @root_validator
    def validate_analysis_references(cls, values):
        raw_facts = values.get("raw_facts") or []
        evidence = values.get("evidence") or []
        sources = values.get("sources") or []
        metrics = values.get("metrics") or []
        explanations = values.get("explanations") or []
        inferences = values.get("inferences") or []
        hypotheses = values.get("hypotheses") or []
        assessments = values.get("reasonableness_assessments") or []
        fact_ids = {item.fact_id for item in raw_facts}
        evidence_ids = {item.evidence_id for item in evidence}
        source_ids = {item.source_id for item in sources}
        metric_ids = {item.metric_id for item in metrics}
        explanation_ids = {item.explanation_id for item in explanations}

        def require_subset(label: str, referenced, allowed) -> None:
            missing = set(referenced) - set(allowed)
            if missing:
                raise ValueError(f"{label}存在悬空引用：{sorted(missing)}")

        if len(explanation_ids) != len(explanations):
            raise ValueError("explanation_id不得重复")
        if len({item.inference_id for item in inferences}) != len(inferences):
            raise ValueError("inference_id不得重复")
        if len({item.hypothesis_id for item in hypotheses}) != len(hypotheses):
            raise ValueError("hypothesis_id不得重复")
        if len({item.assessment_id for item in assessments}) != len(assessments):
            raise ValueError("assessment_id不得重复")
        for item in explanations:
            require_subset(f"解释{item.explanation_id}的metric", [item.metric_id], metric_ids)
            require_subset(
                f"解释{item.explanation_id}的fact",
                [*item.disclosed_fact_ids, *item.supporting_fact_ids],
                fact_ids,
            )
            require_subset(f"解释{item.explanation_id}的evidence", item.evidence_ids, evidence_ids)
            require_subset(f"解释{item.explanation_id}的source", item.source_ids, source_ids)
        for item in inferences:
            require_subset(f"推断{item.inference_id}的fact", item.supporting_fact_ids, fact_ids)
            require_subset(f"推断{item.inference_id}的evidence", item.evidence_ids, evidence_ids)
        for item in hypotheses:
            require_subset(f"假设{item.hypothesis_id}的metric", [item.metric_id], metric_ids)
        for item in assessments:
            require_subset(f"评价{item.assessment_id}的metric", [item.metric_id], metric_ids)
            require_subset(
                f"评价{item.assessment_id}的支持metric",
                item.supporting_metric_ids,
                metric_ids,
            )
            require_subset(
                f"评价{item.assessment_id}的fact", item.supporting_fact_ids, fact_ids
            )
            require_subset(
                f"评价{item.assessment_id}的解释", item.explanation_ids, explanation_ids
            )
        return values


class FinancialArtifacts(BaseModel):
    entity_id: str
    facts_path: Path
    metrics_path: Path
    analysis_path: Path
    data_appendix_path: Path
    risk_flags_path: Path
    sources_path: Path
    questions_path: Path
    validation_path: Path
    explanations_path: Path
    reasonableness_path: Path
    fact_count: int
    metric_count: int
    ratio_count: int
    risk_flag_count: int
    material_change_count: int
    explanation_count: int
    hypothesis_count: int
    assessment_count: int


REQUIRED_ITEMS: Tuple[FinancialItem, ...] = (
    FinancialItem.MONETARY_FUNDS,
    FinancialItem.ACCOUNTS_RECEIVABLE,
    FinancialItem.PREPAYMENTS,
    FinancialItem.INVENTORY,
    FinancialItem.FIXED_ASSETS,
    FinancialItem.SHORT_TERM_BORROWINGS,
    FinancialItem.ACCOUNTS_PAYABLE,
    FinancialItem.OPERATING_REVENUE,
    FinancialItem.OPERATING_CASH_FLOW,
)


def load_financial_seed_catalog() -> FinancialSeedCatalog:
    path = resources.files("credit_research_agent.data").joinpath(
        "financial_seeds.json"
    )
    return FinancialSeedCatalog.parse_raw(path.read_text(encoding="utf-8"))


def load_financial_explanation_seed_catalog() -> FinancialExplanationSeedCatalog:
    path = resources.files("credit_research_agent.data").joinpath(
        "financial_explanation_seeds.json"
    )
    return FinancialExplanationSeedCatalog.parse_raw(
        path.read_text(encoding="utf-8")
    )


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise FinancialBuildError(
            "invalid_financial_value", f"无法解析财务数值：{value!r}"
        ) from exc


def calculate_change(
    current: Decimal, prior: Decimal
) -> Tuple[Decimal, Optional[Decimal], FinancialChangeKind]:
    """计算绝对变动和同比；零基期、符号变化单独分类。"""

    absolute = current - prior
    if prior == 0:
        return absolute, None, FinancialChangeKind.ZERO_BASE
    yoy = (absolute / abs(prior) * Decimal("100")).quantize(Decimal("0.01"))
    if current * prior < 0:
        return absolute, yoy, FinancialChangeKind.SIGN_CHANGE
    if current < 0 and prior < 0:
        kind = (
            FinancialChangeKind.LOSS_EXPANSION
            if abs(current) > abs(prior)
            else FinancialChangeKind.LOSS_NARROWING
        )
        return absolute, yoy, kind
    return absolute, yoy, FinancialChangeKind.COMPARABLE


def validate_financial_facts(
    facts: Sequence[FinancialFact],
    *,
    required_periods: Optional[Sequence[str]] = None,
) -> Tuple[List[NormalizedFinancialFact], FinancialValidationResult]:
    """校验单位、口径、主体及跨报告重复值，并形成可比事实。"""

    if required_periods is None:
        required_periods = tuple(
            sort_financial_periods(fact.period for fact in facts)[-2:]
        )

    issues: List[FinancialValidationIssue] = []
    grouped: Dict[Tuple[str, FinancialItem, str, str, EntityScope], List[FinancialFact]] = (
        defaultdict(list)
    )
    for fact in facts:
        grouped[
            (fact.entity_id, fact.item, fact.period, fact.unit, fact.entity_scope)
        ].append(fact)

    # 同一指标/期间出现不同单位或口径，必须显式拒绝，而不能偷偷混算。
    dimensions: Dict[Tuple[str, FinancialItem, str], set[Tuple[str, EntityScope]]] = (
        defaultdict(set)
    )
    for fact in facts:
        dimensions[(fact.entity_id, fact.item, fact.period)].add(
            (fact.unit, fact.entity_scope)
        )
    for (_, item, period), dimension_set in dimensions.items():
        if len(dimension_set) > 1:
            issues.append(
                FinancialValidationIssue(
                    code="incomparable_scope_or_unit",
                    severity=IssueSeverity.ERROR,
                    message=f"{ITEM_LABELS[item]} {period} 存在不同单位或报表口径",
                    item=item,
                    period=period,
                )
            )

    normalized: List[NormalizedFinancialFact] = []
    sorted_keys = sorted(
        grouped,
        key=lambda key: (key[1].value, key[2], key[3], key[4].value),
    )
    for index, key in enumerate(sorted_keys, start=1):
        entity_id, item, period, unit, scope = key
        rows = grouped[key]
        values = {row.value for row in rows}
        if len(values) != 1:
            issues.append(
                FinancialValidationIssue(
                    code="cross_report_value_conflict",
                    severity=IssueSeverity.ERROR,
                    message=(
                        f"{ITEM_LABELS[item]} {period} 在不同年报中数值不一致："
                        + "、".join(sorted(format(value, "f") for value in values))
                    ),
                    item=item,
                    period=period,
                )
            )
            continue
        first = rows[0]
        normalized.append(
            NormalizedFinancialFact(
                normalized_fact_id=f"NF{index:03d}",
                entity_id=entity_id,
                item=item,
                item_label=ITEM_LABELS[item],
                statement=first.statement,
                period=period,
                value=first.value,
                unit=unit,
                entity_scope=scope,
                supporting_fact_ids=sorted(row.fact_id for row in rows),
                source_ids=sorted({row.source_id for row in rows}),
                evidence_ids=sorted(
                    {evidence_id for row in rows for evidence_id in row.evidence_ids}
                ),
            )
        )

    coverage: Dict[str, Dict[str, bool]] = {}
    available = {(item.item, item.period) for item in normalized}
    for item in REQUIRED_ITEMS:
        coverage[item.value] = {
            period: (item, period) in available for period in required_periods
        }
        for period in required_periods:
            if (item, period) not in available:
                issues.append(
                    FinancialValidationIssue(
                        code="required_fact_missing",
                        severity=IssueSeverity.WARNING,
                        message=f"{ITEM_LABELS[item]} {period} 尚未形成可比事实",
                        item=item,
                        period=period,
                    )
                )
    result = FinancialValidationResult(
        is_valid=not any(issue.severity is IssueSeverity.ERROR for issue in issues),
        issues=issues,
        required_coverage=coverage,
    )
    return normalized, result


def calculate_financial_metrics(
    facts: Sequence[NormalizedFinancialFact],
    *,
    materiality_threshold: Decimal = Decimal("20.00"),
) -> List[FinancialMetric]:
    """按指标逐年计算同比，20%（含）触发重点分析标记。"""

    by_item: Dict[FinancialItem, List[NormalizedFinancialFact]] = defaultdict(list)
    for fact in facts:
        by_item[fact.item].append(fact)
    metrics: List[FinancialMetric] = []
    for item in sorted(by_item, key=lambda value: value.value):
        rows = sorted(
            by_item[item], key=lambda row: _financial_period_sort_key(row.period)
        )
        for prior, current in zip(rows, rows[1:]):
            absolute, yoy, kind = calculate_change(current.value, prior.value)
            reasons: List[str] = []
            if yoy is not None and abs(yoy) >= materiality_threshold:
                reasons.append(f"同比绝对变动达到{materiality_threshold}%阈值")
            if kind is FinancialChangeKind.ZERO_BASE:
                reasons.append("基期为零，常规同比不适用")
            if kind is FinancialChangeKind.SIGN_CHANGE:
                reasons.append("数值发生正负号切换")
            if kind in {
                FinancialChangeKind.LOSS_EXPANSION,
                FinancialChangeKind.LOSS_NARROWING,
            }:
                reasons.append("负值项目需按亏损扩大/收窄解释")
            metrics.append(
                FinancialMetric(
                    metric_id=f"FM{len(metrics) + 1:03d}",
                    entity_id=current.entity_id,
                    item=item,
                    item_label=ITEM_LABELS[item],
                    prior_period=prior.period,
                    current_period=current.period,
                    prior_value=prior.value,
                    current_value=current.value,
                    absolute_change=absolute,
                    yoy_percent=yoy,
                    change_kind=kind,
                    formula="(current - prior) / abs(prior) * 100%",
                    requires_deep_analysis=bool(reasons),
                    trigger_reasons=reasons,
                    supporting_fact_ids=sorted(
                        set(prior.supporting_fact_ids + current.supporting_fact_ids)
                    ),
                    source_ids=sorted(set(prior.source_ids + current.source_ids)),
                )
            )
    return metrics


def calculate_financial_ratios(
    facts: Sequence[NormalizedFinancialFact],
) -> List[FinancialRatio]:
    """计算授信报告常用比率；缺少分子或分母时不强算。"""

    lookup = {(fact.item, fact.period): fact for fact in facts}
    periods = sort_financial_periods(fact.period for fact in facts)
    specifications = (
        ("资产负债率", FinancialItem.TOTAL_LIABILITIES, FinancialItem.TOTAL_ASSETS),
        ("流动比率", FinancialItem.CURRENT_ASSETS, FinancialItem.CURRENT_LIABILITIES),
        ("毛利率", None, None),
        ("销售收现比", FinancialItem.CASH_RECEIVED_FROM_SALES, FinancialItem.OPERATING_REVENUE),
    )
    ratios: List[FinancialRatio] = []
    for period in periods:
        for name, numerator_item, denominator_item in specifications:
            if name == "毛利率":
                revenue = lookup.get((FinancialItem.OPERATING_REVENUE, period))
                cost = lookup.get((FinancialItem.OPERATING_COST, period))
                if revenue is None or cost is None or revenue.value == 0:
                    continue
                value = ((revenue.value - cost.value) / revenue.value * 100).quantize(
                    Decimal("0.01")
                )
                supporting = sorted(
                    set(revenue.supporting_fact_ids + cost.supporting_fact_ids)
                )
                formula = "(营业收入-营业成本)/营业收入*100%"
            else:
                numerator = lookup.get((numerator_item, period))  # type: ignore[arg-type]
                denominator = lookup.get((denominator_item, period))  # type: ignore[arg-type]
                if numerator is None or denominator is None or denominator.value == 0:
                    continue
                value = (numerator.value / denominator.value * 100).quantize(
                    Decimal("0.01")
                )
                supporting = sorted(
                    set(numerator.supporting_fact_ids + denominator.supporting_fact_ids)
                )
                formula = f"{ITEM_LABELS[numerator_item]}/{ITEM_LABELS[denominator_item]}*100%"  # type: ignore[index]
            ratios.append(
                FinancialRatio(
                    ratio_id=f"FR{len(ratios) + 1:03d}",
                    name=name,
                    period=period,
                    value_percent=value,
                    formula=formula,
                    supporting_fact_ids=supporting,
                )
            )
    return ratios


def rank_material_changes(metrics: Sequence[FinancialMetric]) -> List[FinancialMetric]:
    """先展示最近年度，再按变动幅度排序，避免只看绝对金额。"""

    return sorted(
        (metric for metric in metrics if metric.requires_deep_analysis),
        key=lambda metric: (
            metric.current_period,
            abs(metric.yoy_percent) if metric.yoy_percent is not None else Decimal("9999"),
        ),
        reverse=True,
    )


def build_financial_risk_flags(
    metrics: Sequence[FinancialMetric],
    facts: Sequence[NormalizedFinancialFact],
) -> List[FinancialRiskFlag]:
    """输出观察信号，不把信号直接写成风险结论或经营原因。"""

    flags: List[FinancialRiskFlag] = []
    latest_metric_period = latest_financial_period(
        metric.current_period for metric in metrics
    )
    for metric in rank_material_changes(metrics):
        if metric.current_period != latest_metric_period:
            continue
        topic = f"{metric.item_label}重大变动"
        yoy_text = (
            f"同比{metric.yoy_percent}%" if metric.yoy_percent is not None else "基期为零"
        )
        flags.append(
            FinancialRiskFlag(
                flag_id=f"RF{len(flags) + 1:03d}",
                severity="attention",
                topic=topic,
                observation=(
                    f"{metric.item_label}由{metric.prior_period}的"
                    f"{metric.prior_value}元变为{metric.current_period}的"
                    f"{metric.current_value}元，{yoy_text}。该项仅为量化观察，"
                    "原因需结合年报附注和管理层说明核实。"
                ),
                supporting_metric_ids=[metric.metric_id],
                supporting_fact_ids=metric.supporting_fact_ids,
            )
        )
    metric_lookup = {
        (metric.item, metric.current_period): metric for metric in metrics
    }
    for period in sorted({metric.current_period for metric in metrics}):
        receivables = metric_lookup.get((FinancialItem.ACCOUNTS_RECEIVABLE, period))
        revenue = metric_lookup.get((FinancialItem.OPERATING_REVENUE, period))
        if (
            receivables
            and revenue
            and receivables.yoy_percent is not None
            and revenue.yoy_percent is not None
            and receivables.yoy_percent > 0
            and receivables.yoy_percent > revenue.yoy_percent
        ):
            flags.append(
                FinancialRiskFlag(
                    flag_id=f"RF{len(flags) + 1:03d}",
                    severity="attention",
                    topic="应收账款增速高于营业收入",
                    observation=(
                        f"{period}应收账款同比{receivables.yoy_percent}%，高于营业收入"
                        f"同比{revenue.yoy_percent}%；需核查客户账期、逾期和期后回款。"
                    ),
                    supporting_metric_ids=[receivables.metric_id, revenue.metric_id],
                    supporting_fact_ids=sorted(
                        set(
                            receivables.supporting_fact_ids
                            + revenue.supporting_fact_ids
                        )
                    ),
                )
            )
        profit_metric = metric_lookup.get((FinancialItem.NET_PROFIT, period))
        cash_metric = metric_lookup.get((FinancialItem.OPERATING_CASH_FLOW, period))
        if (
            profit_metric
            and cash_metric
            and profit_metric.yoy_percent is not None
            and cash_metric.yoy_percent is not None
            and profit_metric.yoy_percent * cash_metric.yoy_percent < 0
        ):
            flags.append(
                FinancialRiskFlag(
                    flag_id=f"RF{len(flags) + 1:03d}",
                    severity="attention",
                    topic="利润与经营现金流变动方向背离",
                    observation=(
                        f"{period}净利润与经营活动现金流量净额的同比变动方向相反，"
                        "需核查利润的现金保障程度及营运资金变化。"
                    ),
                    supporting_metric_ids=[profit_metric.metric_id, cash_metric.metric_id],
                    supporting_fact_ids=sorted(
                        set(profit_metric.supporting_fact_ids + cash_metric.supporting_fact_ids)
                    ),
                )
            )
    lookup = {(fact.item, fact.period): fact for fact in facts}
    latest_fact_period = latest_financial_period(fact.period for fact in facts)
    profit = lookup.get((FinancialItem.NET_PROFIT, latest_fact_period))
    cash_flow = lookup.get((FinancialItem.OPERATING_CASH_FLOW, latest_fact_period))
    if profit and cash_flow and profit.value != 0 and cash_flow.value * profit.value < 0:
        flags.append(
            FinancialRiskFlag(
                flag_id=f"RF{len(flags) + 1:03d}",
                severity="attention",
                topic="利润与经营现金流方向不一致",
                observation=(
                    f"{latest_fact_period}净利润与经营活动现金流量净额符号相反，"
                    "需核查利润现金保障程度。"
                ),
                supporting_fact_ids=sorted(
                    set(profit.supporting_fact_ids + cash_flow.supporting_fact_ids)
                ),
            )
        )
    return flags


_REQUIRED_EVIDENCE_BY_ITEM: Dict[FinancialItem, List[str]] = {
    FinancialItem.MONETARY_FUNDS: ["受限资金明细", "主要银行账户余额", "资金变动构成"],
    FinancialItem.ACCOUNTS_RECEIVABLE: ["客户账龄", "逾期余额", "期后回款", "前五大欠款方"],
    FinancialItem.PREPAYMENTS: ["主要预付对象", "采购合同", "履约进度", "账龄"],
    FinancialItem.INVENTORY: ["存货构成", "库龄", "跌价准备", "期后销售"],
    FinancialItem.FIXED_ASSETS: ["固定资产增减明细", "产能利用率", "减值测试"],
    FinancialItem.SHORT_TERM_BORROWINGS: ["借款明细", "到期安排", "担保抵押", "资金用途"],
    FinancialItem.ACCOUNTS_PAYABLE: ["主要供应商", "采购背景", "合同账期", "逾期情况"],
    FinancialItem.OPERATING_REVENUE: ["分产品销量价格桥接", "重大客户订单", "收入确认资料"],
    FinancialItem.NET_PROFIT: ["利润变动桥接", "毛利变动明细", "期间费用与减值明细"],
    FinancialItem.OPERATING_CASH_FLOW: ["销售回款明细", "营运资金变动表", "大额经营收支"],
    FinancialItem.CURRENT_ASSETS: ["流动资产构成变动", "受限及变现能力说明"],
}


_CREDIT_CONCERN_BY_ITEM: Dict[FinancialItem, str] = {
    FinancialItem.MONETARY_FUNDS: "关注货币资金增加是否可自由支配，以及是否主要来自经营积累。",
    FinancialItem.ACCOUNTS_RECEIVABLE: "关注回款周期、客户集中和减值风险对经营资金占用的影响。",
    FinancialItem.PREPAYMENTS: "关注预付款履约、长期挂账和供应商信用风险。",
    FinancialItem.INVENTORY: "关注库存周转、近效期及跌价风险。",
    FinancialItem.FIXED_ASSETS: "关注资产利用效率、资本开支压力和减值风险。",
    FinancialItem.SHORT_TERM_BORROWINGS: "关注短期债务到期安排与可动用现金的匹配。",
    FinancialItem.ACCOUNTS_PAYABLE: "关注付款节奏变化是否反映采购收缩或供应商账期变化。",
    FinancialItem.OPERATING_REVENUE: "关注收入增长的产品结构、持续性及其与回款的匹配。",
    FinancialItem.NET_PROFIT: "关注盈利修复的来源、持续性及非经常性因素。",
    FinancialItem.OPERATING_CASH_FLOW: "关注销售回款改善的持续性及营运资金变化。",
    FinancialItem.CURRENT_ASSETS: "关注流动资产增长的具体构成和实际变现能力。",
}


def _evidence_ids_for_fact_ids(
    fact_ids: Sequence[str], facts: Sequence[FactRecord]
) -> List[str]:
    fact_by_id = {item.fact_id: item for item in facts}
    return sorted(
        {
            evidence_id
            for fact_id in fact_ids
            for fact in [fact_by_id.get(fact_id)]
            if fact is not None
            for evidence_id in fact.evidence_ids
        }
    )


def _source_ids_for_evidence_ids(
    evidence_ids: Sequence[str], evidence: Sequence[EvidenceRecord]
) -> List[str]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    return sorted(
        {
            evidence_by_id[evidence_id].source_id
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        }
    )


def extract_change_explanations(
    priority_items: Sequence[FinancialMetric],
    disclosures: Sequence[FactRecord],
    evidence: Sequence[EvidenceRecord],
    *,
    links: Sequence[FinancialExplanationLinkSeed] = (),
    all_metrics: Sequence[FinancialMetric] = (),
    ratios: Sequence[FinancialRatio] = (),
) -> Tuple[
    List[FinancialChangeExplanation],
    List[ModelInference],
    List[FinancialHypothesis],
]:
    """把原因分为披露事实、支持性推断和待核实假设。

    该函数不做自由文本搜索或 LLM 因果归因。披露原因只能来自已通过
    ``EvidenceBuilder`` 精确匹配的 ``FactRecord``；支持性推断只使用已验证事实和
    Python 计算结果；剩余项目输出待核实假设。
    """

    metrics = list(all_metrics) or list(priority_items)
    priority_by_key = {
        (item.item, item.prior_period, item.current_period): item
        for item in priority_items
    }
    fact_by_id = {item.fact_id: item for item in disclosures}
    explanations: List[FinancialChangeExplanation] = []
    inferences: List[ModelInference] = []

    for link in links:
        metric = priority_by_key.get((link.item, link.prior_period, link.current_period))
        if metric is None:
            continue
        missing = sorted(set(link.fact_ids) - set(fact_by_id))
        if missing:
            raise FinancialBuildError(
                "explanation_fact_missing",
                f"原因 {link.explanation_id} 引用了不存在的事实：{missing}",
            )
        evidence_ids = _evidence_ids_for_fact_ids(link.fact_ids, disclosures)
        source_ids = _source_ids_for_evidence_ids(evidence_ids, evidence)
        if link.kind is FinancialExplanationKind.DISCLOSED_REASON:
            explanations.append(
                FinancialChangeExplanation(
                    explanation_id=link.explanation_id,
                    metric_id=metric.metric_id,
                    kind=link.kind,
                    statement=link.statement,
                    disclosed_fact_ids=sorted(link.fact_ids),
                    evidence_ids=evidence_ids,
                    source_ids=source_ids,
                )
            )
            continue
        inference_id = f"I{280 + len(inferences):03d}"
        inference = ModelInference(
            inference_id=inference_id,
            entity_id=metric.entity_id,
            research_module=ResearchModule.FINANCIAL_ANALYSIS,
            statement=link.statement,
            supporting_fact_ids=sorted(link.fact_ids),
            evidence_ids=evidence_ids,
            confidence_note="方向性证据支持，但年报未将其表述为该总量变化的唯一直接原因。",
        )
        inferences.append(inference)
        explanations.append(
            FinancialChangeExplanation(
                explanation_id=link.explanation_id,
                metric_id=metric.metric_id,
                kind=FinancialExplanationKind.SUPPORTED_INFERENCE,
                statement=link.statement,
                supporting_fact_ids=inference.supporting_fact_ids,
                inference_id=inference_id,
                evidence_ids=evidence_ids,
                source_ids=source_ids,
                limitation=inference.confidence_note,
            )
        )

    # 净利润由负转正没有一条年报原文给出完整归因。这里仅把收入、成本和毛利率
    # 的确定性变化组合成支持性推断，并明确保留“非完整因果解释”的边界。
    latest_priority_period = latest_financial_period(
        item.current_period for item in priority_items
    )
    priority_profit = next(
        (
            item
            for item in priority_items
            if item.item is FinancialItem.NET_PROFIT
            and item.current_period == latest_priority_period
        ),
        None,
    )
    if priority_profit and not any(
        item.metric_id == priority_profit.metric_id for item in explanations
    ):
        revenue = next(
            (
                item
                for item in metrics
                if item.item is FinancialItem.OPERATING_REVENUE
                and item.prior_period == priority_profit.prior_period
                and item.current_period == priority_profit.current_period
            ),
            None,
        )
        cost = next(
            (
                item
                for item in metrics
                if item.item is FinancialItem.OPERATING_COST
                and item.prior_period == priority_profit.prior_period
                and item.current_period == priority_profit.current_period
            ),
            None,
        )
        gross_margin = {
            item.period: item
            for item in ratios
            if item.name == "毛利率"
            and item.period in {priority_profit.prior_period, priority_profit.current_period}
        }
        if revenue is not None and cost is not None and len(gross_margin) == 2:
            supporting_fact_ids = sorted(
                set(
                    priority_profit.supporting_fact_ids
                    + revenue.supporting_fact_ids
                    + cost.supporting_fact_ids
                    + gross_margin[priority_profit.prior_period].supporting_fact_ids
                    + gross_margin[priority_profit.current_period].supporting_fact_ids
                )
            )
            evidence_ids = _evidence_ids_for_fact_ids(
                supporting_fact_ids, disclosures
            )
            statement = (
                f"营业收入同比{revenue.yoy_percent}%、营业成本同比{cost.yoy_percent}%，"
                f"毛利率由{gross_margin[priority_profit.prior_period].value_percent}%升至"
                f"{gross_margin[priority_profit.current_period].value_percent}%，与净利润由负转正方向一致；"
                "该组合仅构成支持性推断，并非公司披露的完整利润变动原因。"
            )
            inference_id = f"I{280 + len(inferences):03d}"
            inferences.append(
                ModelInference(
                    inference_id=inference_id,
                    entity_id=priority_profit.entity_id,
                    research_module=ResearchModule.FINANCIAL_ANALYSIS,
                    statement=statement,
                    supporting_fact_ids=supporting_fact_ids,
                    evidence_ids=evidence_ids,
                    confidence_note="需补充利润变动桥接、费用、减值及非经常性损益明细。",
                )
            )
            explanations.append(
                FinancialChangeExplanation(
                    explanation_id="FX-NET-PROFIT-001",
                    metric_id=priority_profit.metric_id,
                    kind=FinancialExplanationKind.SUPPORTED_INFERENCE,
                    statement=statement,
                    supporting_fact_ids=supporting_fact_ids,
                    inference_id=inference_id,
                    evidence_ids=evidence_ids,
                    source_ids=_source_ids_for_evidence_ids(evidence_ids, evidence),
                    limitation="不是年报直接归因，不能据此排除其他利润影响因素。",
                )
            )

    explained_metric_ids = {item.metric_id for item in explanations}
    hypotheses: List[FinancialHypothesis] = []
    for index, metric in enumerate(priority_items, start=1):
        if metric.metric_id in explained_metric_ids:
            continue
        required_evidence = _REQUIRED_EVIDENCE_BY_ITEM.get(
            metric.item, ["科目附注明细", "管理层变动说明"]
        )
        hypotheses.append(
            FinancialHypothesis(
                hypothesis_id=f"FH{index:03d}",
                metric_id=metric.metric_id,
                statement=(
                    f"当前证据未形成{metric.item_label}变动的直接原因；"
                    "任何经营成因均为待核实假设，不能作为确定性结论。"
                ),
                required_evidence=required_evidence,
            )
        )
    return explanations, inferences, hypotheses


def assess_reasonableness(
    explanations: Sequence[FinancialChangeExplanation],
    priority_items: Sequence[FinancialMetric],
    *,
    all_metrics: Sequence[FinancialMetric] = (),
    hypotheses: Sequence[FinancialHypothesis] = (),
) -> List[FinancialReasonablenessAssessment]:
    """检查原因与已验证数值是否方向一致，并记录证据缺口。"""

    metrics = list(all_metrics) or list(priority_items)
    explanation_by_metric: Dict[str, List[FinancialChangeExplanation]] = defaultdict(list)
    for item in explanations:
        explanation_by_metric[item.metric_id].append(item)
    hypothesis_by_metric = {item.metric_id: item for item in hypotheses}
    metric_by_key = {
        (item.item, item.prior_period, item.current_period): item for item in metrics
    }
    assessments: List[FinancialReasonablenessAssessment] = []
    for index, metric in enumerate(priority_items, start=1):
        linked = explanation_by_metric.get(metric.metric_id, [])
        status = ReasonablenessStatus.INSUFFICIENT_EVIDENCE
        conclusion = "当前没有足够的直接原因证据，无法评价该变化原因是否合理。"
        supporting_metric_ids = [metric.metric_id]
        supporting_fact_ids = list(metric.supporting_fact_ids)
        contradictions: List[str] = []
        missing_evidence = list(
            _REQUIRED_EVIDENCE_BY_ITEM.get(
                metric.item, ["科目附注明细", "管理层变动说明"]
            )
        )

        if metric.item is FinancialItem.OPERATING_CASH_FLOW and linked:
            cash_received = metric_by_key.get(
                (
                    FinancialItem.CASH_RECEIVED_FROM_SALES,
                    metric.prior_period,
                    metric.current_period,
                )
            )
            if cash_received is not None and cash_received.absolute_change > 0:
                status = ReasonablenessStatus.SUPPORTED
                supporting_metric_ids.append(cash_received.metric_id)
                supporting_fact_ids.extend(cash_received.supporting_fact_ids)
                conclusion = (
                    "公司披露经营现金流净额增长主要因销售回款增加；"
                    f"销售商品、提供劳务收到的现金同比{cash_received.yoy_percent}%，"
                    "变动方向一致。该核对支持披露原因，但不代表已解释全部增幅。"
                )
                missing_evidence = ["经营现金流增幅的完整营运资金桥接"]
            elif cash_received is not None:
                status = ReasonablenessStatus.CONTRADICTED
                supporting_metric_ids.append(cash_received.metric_id)
                contradictions.append("销售商品、提供劳务收到的现金未同向增加")
                conclusion = "披露原因与已提取的销售收现数据方向不一致，需优先复核口径和原文。"
        elif linked:
            status = ReasonablenessStatus.PARTIALLY_SUPPORTED
            conclusion = (
                "已有事实与该解释方向一致，但年报未提供完整因果桥接，"
                "因此只能视为支持性推断。"
            )
            for item in linked:
                supporting_fact_ids.extend(item.supporting_fact_ids)
                supporting_fact_ids.extend(item.disclosed_fact_ids)
        elif metric.metric_id in hypothesis_by_metric:
            conclusion = hypothesis_by_metric[metric.metric_id].statement
            missing_evidence = list(
                hypothesis_by_metric[metric.metric_id].required_evidence
            )

        question_items = "、".join(missing_evidence)
        assessments.append(
            FinancialReasonablenessAssessment(
                assessment_id=f"FA{index:03d}",
                metric_id=metric.metric_id,
                status=status,
                conclusion=conclusion,
                explanation_ids=[item.explanation_id for item in linked],
                supporting_metric_ids=sorted(set(supporting_metric_ids)),
                supporting_fact_ids=sorted(set(supporting_fact_ids)),
                contradicting_observations=contradictions,
                missing_evidence=missing_evidence,
                credit_concern=_CREDIT_CONCERN_BY_ITEM.get(
                    metric.item, "关注该变化对偿债能力和经营资金占用的影响。"
                ),
                due_diligence_questions=[
                    f"请说明{metric.item_label}变动的具体原因，并提供{question_items}。"
                ],
            )
        )
    return assessments


def _statement_by_fact_id(catalog: FinancialSeedCatalog) -> Dict[str, FinancialLineSeed]:
    result: Dict[str, FinancialLineSeed] = {}
    for line in catalog.lines:
        for fact_id in (line.fact_id_current, line.fact_id_prior):
            if fact_id:
                result[fact_id] = line
    return result


def normalize_financial_items(
    raw_facts: Sequence[FactRecord],
    evidence: Sequence[EvidenceRecord],
    catalog: FinancialSeedCatalog,
) -> List[FinancialFact]:
    """把通用 FactRecord 转为带报表、单位和来源语义的财务事实。"""

    line_by_fact = _statement_by_fact_id(catalog)
    evidence_to_source = {item.evidence_id: item.source_id for item in evidence}
    financial_facts: List[FinancialFact] = []
    for fact in raw_facts:
        line = line_by_fact.get(fact.fact_id)
        if line is None:
            raise FinancialBuildError(
                "financial_line_mapping_missing", f"事实 {fact.fact_id} 缺少财务报表行映射"
            )
        source_ids = {evidence_to_source[item] for item in fact.evidence_ids}
        if len(source_ids) != 1:
            raise FinancialBuildError(
                "fact_source_ambiguous", f"事实 {fact.fact_id} 未唯一绑定来源"
            )
        financial_facts.append(
            FinancialFact(
                fact_id=fact.fact_id,
                entity_id=fact.entity_id,
                item=FinancialItem(fact.field_name),
                item_label=ITEM_LABELS[FinancialItem(fact.field_name)],
                statement=line.statement,
                period=fact.period or "",
                value=_decimal(fact.value),
                unit=line.unit,
                entity_scope=fact.entity_scope,
                source_id=next(iter(source_ids)),
                evidence_ids=fact.evidence_ids,
            )
        )
    return financial_facts


class FinancialBuilder:
    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        identity: CompanyIdentity,
        source_inputs: Sequence[FinancialSourceInput],
        seeds: Optional[FinancialSeedCatalog] = None,
        explanation_seeds: Optional[FinancialExplanationSeedCatalog] = None,
        materiality_threshold: Decimal = Decimal("20.00"),
    ) -> FinancialResearchBundle:
        seeds = seeds or load_financial_seed_catalog()
        explanation_seeds = (
            explanation_seeds or load_financial_explanation_seed_catalog()
        )
        if seeds.entity_id != identity.entity_id:
            raise FinancialBuildError("seed_entity_mismatch", "财务种子主体与解析主体不一致")
        if explanation_seeds.entity_id != identity.entity_id:
            raise FinancialBuildError(
                "explanation_seed_entity_mismatch", "财务原因种子主体与解析主体不一致"
            )
        if not source_inputs:
            raise FinancialBuildError("missing_source_inputs", "至少需要一份官方年报输入")

        raw_facts: List[FactRecord] = []
        evidence: List[EvidenceRecord] = []
        sources: List[SourceRecord] = []
        input_ids = [item.source.source_id for item in source_inputs]
        if len(input_ids) != len(set(input_ids)):
            raise FinancialBuildError("duplicate_source_input", "财务来源输入不得重复")
        for item in source_inputs:
            bundle = EvidenceBuilder(clock=self._clock).build(
                identity=identity,
                source=item.source,
                download=item.download,
                summary_path=item.summary_path,
                pages_path=item.pages_path,
                seeds=seeds.for_source(item.source.source_id),
            )
            raw_facts.extend(bundle.facts)
            evidence.extend(bundle.evidence)
            sources.extend(bundle.sources)

        if len({item.fact_id for item in raw_facts}) != len(raw_facts):
            raise FinancialBuildError("duplicate_fact_id", "财务事实编号重复")
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise FinancialBuildError("duplicate_evidence_id", "财务证据编号重复")

        # 原因披露另走一套精确原文锚点，但继续复用同一个 EvidenceBuilder、
        # SourceRecord / FactRecord / EvidenceRecord 数据管线。
        explanation_facts: List[FactRecord] = []
        explanation_evidence: List[EvidenceRecord] = []
        explanation_source_ids = explanation_seeds.source_ids()
        for item in source_inputs:
            if item.source.source_id not in explanation_source_ids:
                continue
            disclosure_bundle = EvidenceBuilder(clock=self._clock).build(
                identity=identity,
                source=item.source,
                download=item.download,
                summary_path=item.summary_path,
                pages_path=item.pages_path,
                seeds=explanation_seeds.for_source(item.source.source_id),
            )
            explanation_facts.extend(disclosure_bundle.facts)
            explanation_evidence.extend(disclosure_bundle.evidence)

        financial_facts = normalize_financial_items(raw_facts, evidence, seeds)

        available_periods = sort_financial_periods(
            fact.period for fact in financial_facts
        )
        required_periods = tuple(available_periods[-2:])
        normalized, validation = validate_financial_facts(
            financial_facts,
            required_periods=required_periods,
        )
        if not validation.is_valid:
            messages = "；".join(
                issue.message
                for issue in validation.issues
                if issue.severity is IssueSeverity.ERROR
            )
            raise FinancialBuildError("financial_validation_failed", messages)
        metrics = calculate_financial_metrics(
            normalized, materiality_threshold=materiality_threshold
        )
        ratios = calculate_financial_ratios(normalized)
        flags = build_financial_risk_flags(metrics, normalized)
        all_raw_facts = [*raw_facts, *explanation_facts]
        all_evidence = [*evidence, *explanation_evidence]
        if len({item.fact_id for item in all_raw_facts}) != len(all_raw_facts):
            raise FinancialBuildError("duplicate_fact_id", "财务事实及原因事实编号重复")
        if len({item.evidence_id for item in all_evidence}) != len(all_evidence):
            raise FinancialBuildError("duplicate_evidence_id", "财务证据及原因证据编号重复")
        latest_metric_period = latest_financial_period(
            item.current_period for item in metrics
        )
        priority_items = [
            item
            for item in rank_material_changes(metrics)
            if item.current_period == latest_metric_period
        ]
        explanations, inferences, hypotheses = extract_change_explanations(
            priority_items,
            all_raw_facts,
            all_evidence,
            links=explanation_seeds.links,
            all_metrics=metrics,
            ratios=ratios,
        )
        assessments = assess_reasonableness(
            explanations,
            priority_items,
            all_metrics=metrics,
            hypotheses=hypotheses,
        )
        return FinancialResearchBundle(
            identity=identity,
            generated_at=self._clock(),
            raw_facts=all_raw_facts,
            financial_facts=financial_facts,
            normalized_facts=normalized,
            evidence=all_evidence,
            sources=sources,
            metrics=metrics,
            ratios=ratios,
            risk_flags=flags,
            explanations=explanations,
            inferences=inferences,
            hypotheses=hypotheses,
            reasonableness_assessments=assessments,
            validation=validation,
            missing_information=seeds.missing_information,
            limitations=[
                *seeds.limitations,
                *explanation_seeds.limitations,
                "所有同比、比率和重大变动阈值均由确定性 Python 函数计算，未交由 LLM 判断。",
                "重大变动和风险信号仅用于确定进一步核查方向，不等同于授信风险定性结论。",
            ],
        )


def _model_data(model: BaseModel):
    return json.loads(model.json())


def _format_amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_financial_data_appendix(bundle: FinancialResearchBundle) -> str:
    """渲染确定性的财务数据附录，不撰写专业分析结论。

    Python 可以机械呈现已经核验的金额、同比、比率、数据缺口和来源，但正式
    财务分析正文必须由 LLM 基于这些数据撰写。该附录不得作为 LLM 分析失败时
    的替代报告。
    """

    facts = {(item.item, item.period): item for item in bundle.normalized_facts}
    periods = sort_financial_periods(
        item.period for item in bundle.normalized_facts
    )
    table_items = [
        FinancialItem.MONETARY_FUNDS,
        FinancialItem.ACCOUNTS_RECEIVABLE,
        FinancialItem.PREPAYMENTS,
        FinancialItem.INVENTORY,
        FinancialItem.FIXED_ASSETS,
        FinancialItem.SHORT_TERM_BORROWINGS,
        FinancialItem.ACCOUNTS_PAYABLE,
        FinancialItem.OPERATING_REVENUE,
        FinancialItem.NET_PROFIT,
        FinancialItem.OPERATING_CASH_FLOW,
    ]
    lines = [
        f"# {bundle.identity.short_name}财务数据附录",
        "",
        "> 本附录仅呈现 Python 校验或计算的数据，不包含专业分析结论。",
        "> 口径：合并报表；金额单位：人民币元。同比和比率均由 Python 确定性计算。",
        "",
        "## 可比财务事实",
        "",
        "| 项目 | " + " | ".join(periods) + " | 来源 |",
        "|---|" + "|".join("---:" for _ in periods) + "|---|",
    ]
    for item in table_items:
        values: List[str] = []
        source_ids: set[str] = set()
        for period in periods:
            fact = facts.get((item, period))
            if fact is None:
                values.append("未形成可比事实")
                continue
            values.append(_format_amount(fact.value))
            source_ids.update(fact.source_ids)
        source_text = " ".join(f"[{value}]" for value in sorted(source_ids))
        lines.append(f"| {ITEM_LABELS[item]} | {' | '.join(values)} | {source_text} |")

    lines.extend(["", "## 重大变动（阈值：同比绝对值≥20%）", ""])
    latest_metric_period = latest_financial_period(
        metric.current_period for metric in bundle.metrics
    )
    material = [
        metric
        for metric in rank_material_changes(bundle.metrics)
        if metric.current_period == latest_metric_period
    ]
    if material:
        for metric in material:
            yoy = (
                f"{metric.yoy_percent}%" if metric.yoy_percent is not None else "不适用（零基期）"
            )
            lines.append(
                f"- {metric.item_label}：{metric.prior_period} {_format_amount(metric.prior_value)}元，"
                f"{metric.current_period} {_format_amount(metric.current_value)}元，同比 {yoy}；"
                f"确定性变动分类：{metric.change_kind.value}。（{metric.metric_id}）"
            )
    else:
        lines.append("- 当前已提取项目未触发重大变动阈值。")

    lines.extend(["", "## 财务比率", ""])
    ratio_periods = set(periods[-2:])
    for ratio in bundle.ratios:
        if ratio.period in ratio_periods:
            lines.append(
                f"- {ratio.period} {ratio.name}：{ratio.value_percent}%（{ratio.formula}）"
            )

    lines.extend(["", "## 数据完整性与限制", ""])
    for missing in bundle.missing_information:
        lines.append(f"- {missing.topic}：{missing.description}（{missing.reason}）")
    for issue in bundle.validation.issues:
        if issue.severity is IssueSeverity.WARNING:
            lines.append(f"- {issue.message}")
    lines.extend(f"- {item}" for item in bundle.limitations)
    lines.extend(["", "## 来源", ""])
    for source in sorted(bundle.sources, key=lambda item: item.source_id):
        pages = sorted(
            {
                item.page_number
                for item in bundle.evidence
                if item.source_id == source.source_id
            }
        )
        lines.append(
            f"- [{source.source_id}] {source.title}，PDF第"
            f"{'、'.join(str(page) for page in pages)}页，[原文]({source.url})。"
        )
    return "\n".join(lines) + "\n"


def render_financial_analysis(bundle: FinancialResearchBundle) -> str:
    """兼容旧调用：返回数据附录，而非 Python 模板分析正文。"""

    return render_financial_data_appendix(bundle)


def generate_financial_section(bundle: FinancialResearchBundle) -> str:
    """只生成“等待 LLM 分析”的明确占位页。

    该函数故意不再根据 Python 规则拼装专业分析文字，避免在 LLM 失败时把固定
    模板误作正式研究结论。
    """

    return (
        f"# {bundle.identity.short_name}财务分析（未完成）\n\n"
        "> 结构化财务数据、计算指标和内部来源记录已经生成，但尚未取得 "
        "DeepSeek 分析正文。\n\n"
        "正式报告不得以 Python 固定模板替代模型撰写；请运行 V2.3 财务 Agent。\n"
    )


def render_financial_questions(bundle: FinancialResearchBundle) -> str:
    """生成尽调问题占位页；具体问题由 DeepSeek 撰写。"""

    lines = ["# 财务尽调问题清单（未完成）", ""]
    lines.append("> 尚未取得 DeepSeek 尽调问题。以下仅列示内部数据缺口。")
    lines.append("")
    for missing in bundle.missing_information:
        lines.append(f"- 待补充：{missing.topic}——{missing.description}")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def build_financial_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    comparison_inputs: Sequence[FinancialSourceInput] = (),
    seeds: Optional[FinancialSeedCatalog] = None,
    explanation_seeds: Optional[FinancialExplanationSeedCatalog] = None,
    materiality_threshold: Decimal = Decimal("20.00"),
    clock: Optional[Callable[[], datetime]] = None,
) -> FinancialArtifacts:
    """financial-analysis Skill 的高层白名单 Tool。"""

    inputs = [
        FinancialSourceInput(
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
        ),
        *list(comparison_inputs),
    ]
    bundle = FinancialBuilder(clock=clock).build(
        identity=identity,
        source_inputs=inputs,
        seeds=seeds,
        explanation_seeds=explanation_seeds,
        materiality_threshold=materiality_threshold,
    )
    entity_dir = Path(output_dir) / identity.entity_id
    facts_path = entity_dir / "financial_facts.json"
    metrics_path = entity_dir / "financial_metrics.json"
    analysis_path = entity_dir / "financial_analysis.md"
    data_appendix_path = entity_dir / "financial_data_appendix.md"
    risk_flags_path = entity_dir / "financial_risk_flags.json"
    sources_path = entity_dir / "financial_sources.json"
    questions_path = entity_dir / "financial_questions.md"
    validation_path = entity_dir / "financial_validation.json"
    explanations_path = entity_dir / "financial_explanations.json"
    reasonableness_path = entity_dir / "financial_reasonableness.json"
    _atomic_write(
        facts_path,
        json.dumps(
            {
                "schema_version": bundle.schema_version,
                "identity": _model_data(bundle.identity),
                "raw_facts": [_model_data(item) for item in bundle.raw_facts],
                "financial_facts": [_model_data(item) for item in bundle.financial_facts],
                "normalized_facts": [_model_data(item) for item in bundle.normalized_facts],
                "evidence": [_model_data(item) for item in bundle.evidence],
                "missing_information": [
                    _model_data(item) for item in bundle.missing_information
                ],
                "limitations": bundle.limitations,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write(
        metrics_path,
        json.dumps(
            {
                "metrics": [_model_data(item) for item in bundle.metrics],
                "ratios": [_model_data(item) for item in bundle.ratios],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write(analysis_path, generate_financial_section(bundle))
    _atomic_write(data_appendix_path, render_financial_data_appendix(bundle))
    _atomic_write(
        risk_flags_path,
        json.dumps(
            [_model_data(item) for item in bundle.risk_flags],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write(
        sources_path,
        json.dumps(
            {
                "sources": [_model_data(item) for item in bundle.sources],
                "evidence": [_model_data(item) for item in bundle.evidence],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write(questions_path, render_financial_questions(bundle))
    _atomic_write(
        validation_path,
        json.dumps(_model_data(bundle.validation), ensure_ascii=False, indent=2)
        + "\n",
    )
    _atomic_write(
        explanations_path,
        json.dumps(
            {
                "explanations": [_model_data(item) for item in bundle.explanations],
                "inferences": [_model_data(item) for item in bundle.inferences],
                "hypotheses": [_model_data(item) for item in bundle.hypotheses],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write(
        reasonableness_path,
        json.dumps(
            [_model_data(item) for item in bundle.reasonableness_assessments],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return FinancialArtifacts(
        entity_id=identity.entity_id,
        facts_path=facts_path.resolve(),
        metrics_path=metrics_path.resolve(),
        analysis_path=analysis_path.resolve(),
        data_appendix_path=data_appendix_path.resolve(),
        risk_flags_path=risk_flags_path.resolve(),
        sources_path=sources_path.resolve(),
        questions_path=questions_path.resolve(),
        validation_path=validation_path.resolve(),
        explanations_path=explanations_path.resolve(),
        reasonableness_path=reasonableness_path.resolve(),
        fact_count=len(bundle.normalized_facts),
        metric_count=len(bundle.metrics),
        ratio_count=len(bundle.ratios),
        risk_flag_count=len(bundle.risk_flags),
        material_change_count=len(rank_material_changes(bundle.metrics)),
        explanation_count=len(bundle.explanations),
        hypothesis_count=len(bundle.hypotheses),
        assessment_count=len(bundle.reasonableness_assessments),
    )
