"""V2.1/V2.2 财务事实、计算边界与来源约束测试。"""

from __future__ import annotations

from decimal import Decimal

from credit_research_agent.financial import (
    FinancialChangeKind,
    FinancialFact,
    FinancialItem,
    FinancialStatement,
    calculate_change,
    calculate_financial_metrics,
    validate_financial_facts,
)
from credit_research_agent.models import EntityScope


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

