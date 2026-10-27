"""V2.1/V2.2 财务事实、计算边界与来源约束测试。"""

from __future__ import annotations

from decimal import Decimal

from credit_research_agent.financial import (
    FinancialChangeKind,
    FinancialExplanationKind,
    FinancialExplanationLinkSeed,
    FinancialFact,
    FinancialItem,
    FinancialMetric,
    FinancialStatement,
    ReasonablenessStatus,
    assess_reasonableness,
    build_financial_risk_flags,
    calculate_change,
    calculate_financial_metrics,
    extract_change_explanations,
    latest_financial_period,
    sort_financial_periods,
    validate_financial_facts,
)
from credit_research_agent.models import (
    EntityScope,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    ResearchModule,
    VerificationMethod,
)


def _fact(
    fact_id: str,
    item: FinancialItem,
    period: str,
    value: str,
    *,
    source_id: str = "S01",
    unit: str = "CNY",
    scope: EntityScope = EntityScope.CONSOLIDATED,
) -> FinancialFact:
    return FinancialFact(
        fact_id=fact_id,
        entity_id="CN-A-SZSE-002603",
        item=item,
        item_label=item.value,
        statement=FinancialStatement.BALANCE_SHEET,
        period=period,
        value=Decimal(value),
        unit=unit,
        entity_scope=scope,
        source_id=source_id,
        evidence_ids=["E201"],
    )


def test_exactly_twenty_percent_triggers_material_analysis() -> None:
    normalized, validation = validate_financial_facts(
        [
            _fact("F201", FinancialItem.ACCOUNTS_RECEIVABLE, "FY2024", "100"),
            _fact("F202", FinancialItem.ACCOUNTS_RECEIVABLE, "FY2025", "120"),
        ]
    )

    metrics = calculate_financial_metrics(normalized)

    assert validation.is_valid
    assert metrics[0].yoy_percent == Decimal("20.00")
    assert metrics[0].requires_deep_analysis is True


def test_below_twenty_percent_does_not_trigger_without_other_exception() -> None:
    normalized, _ = validate_financial_facts(
        [
            _fact("F201", FinancialItem.INVENTORY, "FY2024", "100"),
            _fact("F202", FinancialItem.INVENTORY, "FY2025", "119.99"),
        ]
    )

    metric = calculate_financial_metrics(normalized)[0]

    assert metric.yoy_percent == Decimal("19.99")
    assert metric.requires_deep_analysis is False


def test_financial_periods_and_latest_risk_flags_are_not_bound_to_fy2025() -> None:
    assert sort_financial_periods(["FY2026", "FY2024", "FY2025", "FY2025"]) == [
        "FY2024",
        "FY2025",
        "FY2026",
    ]
    assert latest_financial_period(["FY2024", "FY2026", "FY2025"]) == "FY2026"

    normalized, validation = validate_financial_facts(
        [
            _fact("F201", FinancialItem.INVENTORY, "FY2025", "100"),
            _fact("F202", FinancialItem.INVENTORY, "FY2026", "130"),
        ]
    )
    metrics = calculate_financial_metrics(normalized)
    flags = build_financial_risk_flags(metrics, normalized)

    assert validation.required_coverage[FinancialItem.INVENTORY.value] == {
        "FY2025": True,
        "FY2026": True,
    }
    assert metrics[0].current_period == "FY2026"
    assert any("FY2026" in flag.observation for flag in flags)
    assert all("FY2025净利润" not in flag.observation for flag in flags)


def test_zero_base_and_sign_change_do_not_use_naive_growth_wording() -> None:
    absolute, yoy, kind = calculate_change(Decimal("10"), Decimal("0"))
    assert absolute == Decimal("10")
    assert yoy is None
    assert kind is FinancialChangeKind.ZERO_BASE

    _, sign_yoy, sign_kind = calculate_change(Decimal("5"), Decimal("-10"))
    assert sign_yoy == Decimal("150.00")
    assert sign_kind is FinancialChangeKind.SIGN_CHANGE


def test_negative_values_are_classified_as_loss_expansion_or_narrowing() -> None:
    assert calculate_change(Decimal("-20"), Decimal("-10"))[2] is FinancialChangeKind.LOSS_EXPANSION
    assert calculate_change(Decimal("-5"), Decimal("-10"))[2] is FinancialChangeKind.LOSS_NARROWING


def test_duplicate_period_values_from_two_reports_are_combined_with_sources() -> None:
    normalized, validation = validate_financial_facts(
        [
            _fact("F201", FinancialItem.MONETARY_FUNDS, "FY2024", "100", source_id="S01"),
            _fact("F202", FinancialItem.MONETARY_FUNDS, "FY2024", "100", source_id="S10"),
        ]
    )

    assert validation.is_valid
    assert len(normalized) == 1
    assert normalized[0].supporting_fact_ids == ["F201", "F202"]
    assert normalized[0].source_ids == ["S01", "S10"]


def test_cross_report_value_conflict_is_rejected() -> None:
    normalized, validation = validate_financial_facts(
        [
            _fact("F201", FinancialItem.MONETARY_FUNDS, "FY2024", "100", source_id="S01"),
            _fact("F202", FinancialItem.MONETARY_FUNDS, "FY2024", "101", source_id="S10"),
        ]
    )

    assert not validation.is_valid
    assert normalized == []
    assert any(issue.code == "cross_report_value_conflict" for issue in validation.issues)


def test_scope_or_unit_conflict_is_rejected_before_calculation() -> None:
    _, validation = validate_financial_facts(
        [
            _fact("F201", FinancialItem.TOTAL_ASSETS, "FY2025", "100", unit="CNY"),
            _fact(
                "F202",
                FinancialItem.TOTAL_ASSETS,
                "FY2025",
                "0.01",
                unit="CNY_10K",
            ),
        ]
    )

    assert not validation.is_valid
    assert any(issue.code == "incomparable_scope_or_unit" for issue in validation.issues)


def _metric(
    metric_id: str,
    item: FinancialItem,
    prior: str,
    current: str,
    *,
    yoy: str,
) -> FinancialMetric:
    return FinancialMetric(
        metric_id=metric_id,
        entity_id="CN-A-SZSE-002603",
        item=item,
        item_label=item.value,
        prior_period="FY2024",
        current_period="FY2025",
        prior_value=Decimal(prior),
        current_value=Decimal(current),
        absolute_change=Decimal(current) - Decimal(prior),
        yoy_percent=Decimal(yoy),
        change_kind=FinancialChangeKind.COMPARABLE,
        formula="fixture",
        requires_deep_analysis=True,
        trigger_reasons=["fixture"],
        supporting_fact_ids=["F201", "F202"],
        source_ids=["S01"],
    )


def test_disclosed_reason_is_separate_from_hypothesis_and_has_provenance() -> None:
    metric = _metric(
        "FM020", FinancialItem.OPERATING_CASH_FLOW, "100", "200", yoy="100.00"
    )
    fact = FactRecord(
        fact_id="F280",
        entity_id="CN-A-SZSE-002603",
        category=FactCategory.FINANCIAL,
        research_module=ResearchModule.FINANCIAL_ANALYSIS,
        field_name="operating_cash_flow_change_reason",
        value="销售回款增加",
        claim="公司披露经营现金流增长主要因销售回款增加。",
        entity_scope=EntityScope.CONSOLIDATED,
        period="FY2025",
        evidence_ids=["E280"],
    )
    evidence = EvidenceRecord(
        evidence_id="E280",
        source_id="S01",
        source_sha256="a" * 64,
        page_number=58,
        line_number=20,
        page_or_section="现金流",
        quote="销售回款增加",
        status=EvidenceStatus.VERIFIED,
        verification_method=VerificationMethod.EXACT_TEXT_MATCH,
    )
    link = FinancialExplanationLinkSeed(
        explanation_id="FX001",
        kind=FinancialExplanationKind.DISCLOSED_REASON,
        item=FinancialItem.OPERATING_CASH_FLOW,
        prior_period="FY2024",
        current_period="FY2025",
        statement="公司披露经营现金流增长主要因销售回款增加。",
        fact_ids=["F280"],
    )

    explanations, inferences, hypotheses = extract_change_explanations(
        [metric], [fact], [evidence], links=[link]
    )

    assert explanations[0].kind is FinancialExplanationKind.DISCLOSED_REASON
    assert explanations[0].disclosed_fact_ids == ["F280"]
    assert explanations[0].source_ids == ["S01"]
    assert not inferences
    assert not hypotheses


def test_missing_reason_stays_unverified_and_does_not_become_fact() -> None:
    metric = _metric(
        "FM026", FinancialItem.PREPAYMENTS, "100", "130", yoy="30.00"
    )

    explanations, inferences, hypotheses = extract_change_explanations(
        [metric], [], []
    )
    assessments = assess_reasonableness(
        explanations, [metric], hypotheses=hypotheses
    )

    assert not explanations
    assert not inferences
    assert hypotheses[0].verification_status == "unverified"
    assert assessments[0].status is ReasonablenessStatus.INSUFFICIENT_EVIDENCE
    assert "不能作为确定性结论" in assessments[0].conclusion


def test_disclosed_cash_collection_reason_is_checked_against_cash_received() -> None:
    operating_cash = _metric(
        "FM020", FinancialItem.OPERATING_CASH_FLOW, "100", "200", yoy="100.00"
    )
    cash_received = _metric(
        "FM006", FinancialItem.CASH_RECEIVED_FROM_SALES, "1000", "1100", yoy="10.00"
    )
    explanation = extract_change_explanations(
        [operating_cash], [], []
    )[0]
    # 用明确的结构化对象验证合理性函数，不依赖自然语言关键词匹配。
    from credit_research_agent.financial import FinancialChangeExplanation

    direct = FinancialChangeExplanation(
        explanation_id="FX001",
        metric_id="FM020",
        kind=FinancialExplanationKind.DISCLOSED_REASON,
        statement="销售回款增加",
        disclosed_fact_ids=["F280"],
        evidence_ids=["E280"],
        source_ids=["S01"],
    )
    assessments = assess_reasonableness(
        [direct], [operating_cash], all_metrics=[operating_cash, cash_received]
    )

    assert explanation == []
    assert assessments[0].status is ReasonablenessStatus.SUPPORTED
    assert assessments[0].supporting_metric_ids == ["FM006", "FM020"]

