"""V3.3C/D 九州通缓存年报的通用准备与双 Skill 集成验证。

测试不访问网络、不调用LLM；本机没有V3.3A缓存时显式跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from credit_research_agent.annual_reports import CompanyOnboardingResult
from credit_research_agent.generic_research import (
    build_generic_skill_runtime,
    prepare_generic_research_inputs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONBOARDING_PATH = (
    PROJECT_ROOT
    / "data"
    / "onboarding"
    / "CN-A-SSE-600998"
    / "onboarding.json"
)


def _load_cached_onboarding() -> CompanyOnboardingResult:
    if not ONBOARDING_PATH.is_file():
        pytest.skip("本机尚未准备九州通V3.3A年报产物")
    onboarding = CompanyOnboardingResult.parse_raw(
        ONBOARDING_PATH.read_text(encoding="utf-8")
    )
    missing = [
        path
        for item in onboarding.prepared_reports
        for path in (
            item.download.file_path,
            item.extraction.summary_path,
            item.extraction.pages_path,
        )
        if not Path(path).is_file()
    ]
    if missing:
        pytest.skip("九州通缓存产物不完整")
    return onboarding


def test_jiuzhoutong_generic_preparation_and_both_skills_without_company_seeds(
    tmp_path: Path,
) -> None:
    onboarding = _load_cached_onboarding()
    prepared = prepare_generic_research_inputs(
        onboarding=onboarding,
        output_dir=tmp_path / "prepared",
    )

    assert prepared.identity.entity_id == "CN-A-SSE-600998"
    assert prepared.identity.ticker == "600998.SH"
    assert prepared.source.entity_id == prepared.identity.entity_id
    assert prepared.source.report_period == "FY2025"
    assert prepared.scope.status.value == "supported_non_financial"
    assert prepared.fundamentals.fact_count > 0
    assert {item.entity_id for item in prepared.source_catalog.list_all()} == {
        prepared.identity.entity_id
    }

    runtime = build_generic_skill_runtime(PROJECT_ROOT)
    ownership = runtime.execute(
        task="分析九州通股权结构",
        identity=prepared.identity,
        source=prepared.source,
        selected_skill="ownership-structure",
        tool_arguments={
            "download": prepared.primary_report.download,
            "summary_path": prepared.primary_report.extraction.summary_path,
            "pages_path": prepared.primary_report.extraction.pages_path,
            "output_dir": tmp_path / "ownership",
        },
        trace_output=tmp_path / "ownership_trace.json",
    )
    financial = runtime.execute(
        task="分析九州通财务状况",
        identity=prepared.identity,
        source=prepared.source,
        selected_skill="financial-analysis",
        tool_arguments={
            "download": prepared.primary_report.download,
            "summary_path": prepared.primary_report.extraction.summary_path,
            "pages_path": prepared.primary_report.extraction.pages_path,
            "comparison_inputs": prepared.comparison_inputs,
            "output_dir": tmp_path / "financial",
        },
        trace_output=tmp_path / "financial_trace.json",
    )

    assert ownership.tool_name == "build_ownership_artifacts"
    assert ownership.artifacts["entity_id"] == prepared.identity.entity_id
    assert ownership.artifacts["status"] in {"success", "partial_success"}
    assert ownership.artifacts["control_relationship_count"] >= 1
    assert Path(ownership.artifacts["png_path"]).is_file()
    assert financial.tool_name == "build_financial_artifacts"
    assert financial.artifacts["entity_id"] == prepared.identity.entity_id
    assert financial.artifacts["status"] == "success"
    assert financial.artifacts["current_period"] == "FY2025"
    assert financial.artifacts["prior_period"] == "FY2024"
    assert financial.artifacts["fact_count"] >= 30
    assert financial.artifacts["metric_count"] >= 10

    # 回归关键：动态公司结构化产物不得混入以岭药业的主体或证券代码。
    structured_paths = [
        prepared.source_catalog_path,
        prepared.fundamentals.fundamentals_path,
        Path(ownership.artifacts["dataset_path"]),
        Path(ownership.artifacts["graph_path"]),
        Path(financial.artifacts["facts_path"]),
        Path(financial.artifacts["metrics_path"]),
    ]
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in structured_paths
    )
    assert "CN-A-SZSE-002603" not in combined
    assert "002603.SZ" not in combined
    assert "石家庄以岭药业股份有限公司" not in combined
    assert "CN-A-SSE-600998" in combined

    dataset = json.loads(Path(ownership.artifacts["dataset_path"]).read_text("utf-8"))
    node_names = {item["node_id"]: item["name"] for item in dataset["nodes"]}
    controllers = {
        node_names[node_id]
        for relationship in dataset["control_relationships"]
        for node_id in relationship["subject_node_ids"]
    }
    assert {"刘宝林", "楚昌投资集团有限公司"}.issubset(controllers)
