"""V2.1/V2.2 财务事实、确定性计算与制品生成。

本模块坚持一个边界：PDF 原文只负责形成带页码的事实，Python 只基于这些事实
做可复现计算。任何经营原因、风险成因或预测均不在本模块中补造。
"""

from __future__ import annotations

import json
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
    ResearchModule,
    SourceRecord,
)


class FinancialBuildError(RuntimeError):
    """财务构建失败；code 供 CLI、测试和 Skill Runtime 诊断。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


class FinancialResearchBundle(BaseModel):
    schema_version: str = "2.2.0"
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
    validation: FinancialValidationResult
    missing_information: List[MissingInformation] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class FinancialArtifacts(BaseModel):
    entity_id: str
    facts_path: Path
    metrics_path: Path
    analysis_path: Path
    risk_flags_path: Path
    sources_path: Path
    questions_path: Path
    validation_path: Path
    fact_count: int
    metric_count: int
    ratio_count: int
    risk_flag_count: int
    material_change_count: int


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
    required_periods: Sequence[str] = ("FY2024", "FY2025"),
) -> Tuple[List[NormalizedFinancialFact], FinancialValidationResult]:
    """校验单位、口径、主体及跨报告重复值，并形成可比事实。"""

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
        rows = sorted(by_item[item], key=lambda row: row.period)
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
    periods = sorted({fact.period for fact in facts})
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
    for metric in rank_material_changes(metrics):
        if metric.current_period != "FY2025":
            continue
        topic = f"{metric.item_label}重大变动"
        yoy_text = (
            f"同比{metric.yoy_percent}%" if metric.yoy_percent is not None else "基期为零"
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
    lookup = {(fact.item, fact.period): fact for fact in facts}
    profit = lookup.get((FinancialItem.NET_PROFIT, "FY2025"))
    cash_flow = lookup.get((FinancialItem.OPERATING_CASH_FLOW, "FY2025"))
    if profit and cash_flow and profit.value != 0 and cash_flow.value * profit.value < 0:
        flags.append(
            FinancialRiskFlag(
                flag_id=f"RF{len(flags) + 1:03d}",
                severity="attention",
                topic="利润与经营现金流方向不一致",
                observation="FY2025净利润与经营活动现金流量净额符号相反，需核查利润现金保障程度。",
                supporting_fact_ids=sorted(
                    set(profit.supporting_fact_ids + cash_flow.supporting_fact_ids)
                ),
            )
        )
    return flags


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
        materiality_threshold: Decimal = Decimal("20.00"),
    ) -> FinancialResearchBundle:
        seeds = seeds or load_financial_seed_catalog()
        if seeds.entity_id != identity.entity_id:
            raise FinancialBuildError("seed_entity_mismatch", "财务种子主体与解析主体不一致")
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

        financial_facts = normalize_financial_items(raw_facts, evidence, seeds)

        normalized, validation = validate_financial_facts(financial_facts)
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
        return FinancialResearchBundle(
            identity=identity,
            generated_at=self._clock(),
            raw_facts=raw_facts,
            financial_facts=financial_facts,
            normalized_facts=normalized,
            evidence=evidence,
            sources=sources,
            metrics=metrics,
            ratios=ratios,
            risk_flags=flags,
            validation=validation,
            missing_information=seeds.missing_information,
            limitations=[
                *seeds.limitations,
                "所有同比、比率和重大变动阈值均由确定性 Python 函数计算，未交由 LLM 判断。",
                "重大变动和风险信号仅用于确定进一步核查方向，不等同于授信风险定性结论。",
            ],
        )


def _model_data(model: BaseModel):
    return json.loads(model.json())


def _format_amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_financial_analysis(bundle: FinancialResearchBundle) -> str:
    facts = {(item.item, item.period): item for item in bundle.normalized_facts}
    source_by_evidence = {item.evidence_id: item.source_id for item in bundle.evidence}
    periods = ["FY2023", "FY2024", "FY2025"]
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
        f"# {bundle.identity.short_name}财务分析",
        "",
        "> 口径：合并报表；金额单位：人民币元。同比和比率均由 Python 确定性计算。",
        "",
        "## 可比财务事实",
        "",
        "| 项目 | FY2023 | FY2024 | FY2025 | 来源 |",
        "|---|---:|---:|---:|---|",
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
    material = [
        metric
        for metric in rank_material_changes(bundle.metrics)
        if metric.current_period == "FY2025"
    ]
    if material:
        for metric in material:
            yoy = (
                f"{metric.yoy_percent}%" if metric.yoy_percent is not None else "不适用（零基期）"
            )
            lines.append(
                f"- {metric.item_label}：{metric.prior_period} {_format_amount(metric.prior_value)}元，"
                f"{metric.current_period} {_format_amount(metric.current_value)}元，同比 {yoy}；"
                f"需结合附注核实原因。（{metric.metric_id}）"
            )
    else:
        lines.append("- 当前已提取项目未触发重大变动阈值。")

    lines.extend(["", "## 财务比率", ""])
    for ratio in bundle.ratios:
        if ratio.period in {"FY2024", "FY2025"}:
            lines.append(
                f"- {ratio.period} {ratio.name}：{ratio.value_percent}%（{ratio.formula}）"
            )

    lines.extend(["", "## 风险观察", ""])
    if bundle.risk_flags:
        lines.extend(f"- {flag.observation}" for flag in bundle.risk_flags)
    else:
        lines.append("- 当前确定性规则未产生额外风险信号；不代表不存在其他风险。")

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


def render_financial_questions(bundle: FinancialResearchBundle) -> str:
    lines = ["# 财务尽调问题清单", ""]
    lines.extend(
        [
            "- 请说明营业收入、净利润及经营活动现金流重大变动的业务原因，并提供订单、回款及费用明细支持。",
            "- 请提供货币资金受限情况、银行账户清单及最新银行对账资料。",
            "- 请提供短期借款、长期借款、授信额度、抵质押和担保明细；当前年报主表未形成FY2024/FY2025短期借款可比事实。",
            "- 请说明应收账款账龄、前五大客户、逾期及期后回款情况，并核查收入确认和回款匹配。",
            "- 请说明存货构成、库龄、跌价准备、周转变化及主要产品备货安排。",
            "- 请说明固定资产重大项目、产能利用率、在建工程转固及减值测试情况。",
            "- 请逐项解释本报告中同比绝对变动达到20%的指标，并提供年报附注或管理层材料。",
        ]
    )
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
        materiality_threshold=materiality_threshold,
    )
    entity_dir = Path(output_dir) / identity.entity_id
    facts_path = entity_dir / "financial_facts.json"
    metrics_path = entity_dir / "financial_metrics.json"
    analysis_path = entity_dir / "financial_analysis.md"
    risk_flags_path = entity_dir / "financial_risk_flags.json"
    sources_path = entity_dir / "financial_sources.json"
    questions_path = entity_dir / "financial_questions.md"
    validation_path = entity_dir / "financial_validation.json"
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
    _atomic_write(analysis_path, render_financial_analysis(bundle))
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
    return FinancialArtifacts(
        entity_id=identity.entity_id,
        facts_path=facts_path.resolve(),
        metrics_path=metrics_path.resolve(),
        analysis_path=analysis_path.resolve(),
        risk_flags_path=risk_flags_path.resolve(),
        sources_path=sources_path.resolve(),
        questions_path=questions_path.resolve(),
        validation_path=validation_path.resolve(),
        fact_count=len(bundle.normalized_facts),
        metric_count=len(bundle.metrics),
        ratio_count=len(bundle.ratios),
        risk_flag_count=len(bundle.risk_flags),
        material_change_count=len(rank_material_changes(bundle.metrics)),
    )
