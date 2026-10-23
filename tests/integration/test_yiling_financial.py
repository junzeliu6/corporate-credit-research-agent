"""真实年报上的 V2.1/V2.2 财务事实与产物集成测试。"""

from decimal import Decimal
from pathlib import Path

import pytest

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import (
    FinancialItem,
    FinancialSourceInput,
    build_financial_artifacts,
)
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source_input(source_id: str, catalog: SourceCatalog) -> FinancialSourceInput:
    return FinancialSourceInput(
        source=catalog.get(source_id),
        download=DownloadResult.parse_raw(
            (PROJECT_ROOT / "data" / "cache" / f"{source_id}.metadata.json").read_text(
                encoding="utf-8"
            )
        ),
        summary_path=PROJECT_ROOT / "data" / "parsed" / source_id / "summary.json",
        pages_path=PROJECT_ROOT / "data" / "parsed" / source_id / "pages.jsonl",
    )


@pytest.mark.integration
def test_yiling_financial_facts_are_page_grounded_and_artifacts_are_generated(
    tmp_path: Path,
) -> None:
    required = [
        PROJECT_ROOT / "data" / "cache" / f"{sid}.metadata.json"
        for sid in ("S01", "S10")
    ] + [
        PROJECT_ROOT / "data" / "parsed" / sid / name
        for sid in ("S01", "S10")
        for name in ("summary.json", "pages.jsonl")
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的财务年报产物：" + ", ".join(missing))

    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    catalog = SourceCatalog.from_default_catalog()
    primary = _source_input("S01", catalog)
    comparison = _source_input("S10", catalog)
    artifacts = build_financial_artifacts(
        identity=identity,
        source=primary.source,
        download=primary.download,
        summary_path=primary.summary_path,
        pages_path=primary.pages_path,
        comparison_inputs=[comparison],
        output_dir=tmp_path,
    )

    assert artifacts.fact_count == 46
    assert artifacts.metric_count == 30
    assert artifacts.material_change_count > 0
    for field_name in (
        "facts_path",
        "metrics_path",
        "analysis_path",
        "risk_flags_path",
        "sources_path",
        "questions_path",
        "validation_path",
    ):
        assert getattr(artifacts, field_name).is_file()

    import json

    payload = json.loads(artifacts.facts_path.read_text(encoding="utf-8"))
    facts = {
        (item["item"], item["period"]): item
        for item in payload["normalized_facts"]
    }
    assert Decimal(facts[(FinancialItem.OPERATING_REVENUE.value, "FY2025")]["value"]) == Decimal("7830737438.74")
    assert facts[(FinancialItem.OPERATING_REVENUE.value, "FY2024")]["source_ids"] == ["S01", "S10"]
    assert "S01" in artifacts.analysis_path.read_text(encoding="utf-8")
    assert "短期借款" in artifacts.questions_path.read_text(encoding="utf-8")
