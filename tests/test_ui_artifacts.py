import json
from pathlib import Path

import pytest

from credit_research_agent.ui_artifacts import (
    ArtifactAccessError,
    ReportDisplayStatus,
    discover_research_runs,
    load_financial_tables,
    load_research_run,
    read_report_markdown,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_run(root: Path, *, status: str = "partial_success") -> Path:
    run = root / "batch" / "CN-A-SZSE-002603"
    run.mkdir(parents=True)
    _write_json(
        run / "credit_research_delivery.json",
        {"status": status, "mode": "live", "authorship": "live_llm"},
    )
    _write_json(
        run / "credit_research_context.json",
        {
            "research_task": "授信研究",
            "company": {
                "legal_name": "石家庄以岭药业股份有限公司",
                "ticker": "002603.SZ",
            },
        },
    )
    (run / "credit_research_report.md").write_text("# 原始报告", encoding="utf-8")
    return run


def test_loads_tracked_mock_artifact_without_network() -> None:
    runs = discover_research_runs(
        PROJECT_ROOT,
        roots=[PROJECT_ROOT / "examples" / "v2_5_complete_mock"],
    )

    assert runs
    artifact = next(item for item in runs if item.ticker == "002603.SZ")
    assert artifact.display_status is ReportDisplayStatus.PARTIAL_SUCCESS
    assert artifact.mode == "mock"
    assert artifact.is_live is False
    assert artifact.ownership_image_path is not None
    assert artifact.ownership_image_path.suffix == ".png"
    assert "企业授信研究" in read_report_markdown(artifact)

    tables = load_financial_tables(artifact)
    assert tables.metric_rows
    assert tables.ratio_rows
    assert {"科目", "上期", "本期", "同比(%)"}.issubset(tables.metric_rows[0])


def test_recovered_report_is_preferred_but_original_failure_is_preserved(tmp_path: Path) -> None:
    run = _make_run(tmp_path, status="failed")
    (run / "credit_research_report_recovered.md").write_text(
        "# 恢复后的 DeepSeek 报告", encoding="utf-8"
    )
    _write_json(
        run / "credit_research_recovery.json",
        {"status": "recovered_offline", "original_run_status": "failed"},
    )

    artifact = load_research_run(run, allowed_roots=[tmp_path])

    assert artifact.recovered is True
    assert artifact.original_status == "failed"
    assert artifact.display_status is ReportDisplayStatus.PARTIAL_SUCCESS
    assert artifact.status_message == "已离线恢复，需复核"
    assert artifact.report_path is not None
    assert artifact.report_path.name == "credit_research_report_recovered.md"
    assert "恢复后的" in read_report_markdown(artifact)


def test_png_precedes_svg_and_financial_rows_are_read(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    assets = run / "assets"
    assets.mkdir()
    (assets / "ownership_structure.svg").write_text("<svg/>", encoding="utf-8")
    (assets / "ownership_structure.png").write_bytes(b"png")
    metrics = run / "modules" / "financial" / run.name / "financial_metrics.json"
    _write_json(
        metrics,
        {
            "metrics": [
                {
                    "item_label": "营业收入",
                    "prior_period": "FY2024",
                    "current_period": "FY2025",
                    "prior_value": "10",
                    "current_value": "12",
                    "absolute_change": "2",
                    "yoy_percent": "20.00",
                    "requires_deep_analysis": True,
                }
            ],
            "ratios": [
                {"name": "流动比率", "period": "FY2025", "value_percent": "235.76", "formula": "a/b"}
            ],
        },
    )

    artifact = load_research_run(run, allowed_roots=[tmp_path])
    tables = load_financial_tables(artifact)

    assert artifact.ownership_image_path == (assets / "ownership_structure.png").resolve()
    assert tables.metric_rows[0]["科目"] == "营业收入"
    assert tables.metric_rows[0]["重点分析"] is True
    assert tables.ratio_rows[0]["指标"] == "流动比率"


def test_corrupt_optional_json_degrades_without_crashing(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    (run / "credit_research_context.json").write_text("{broken", encoding="utf-8")

    artifact = load_research_run(run, allowed_roots=[tmp_path])

    assert artifact.report_path is not None
    assert artifact.company_name == ""
    assert any("无法读取 credit_research_context.json" in item for item in artifact.warnings)


def test_run_directory_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ArtifactAccessError):
        load_research_run(outside, allowed_roots=[allowed])


def test_discovery_is_sorted_and_deduplicated(tmp_path: Path) -> None:
    first = _make_run(tmp_path / "root-a")
    second = _make_run(tmp_path / "root-b")
    (first / "credit_research_report.md").touch()
    (second / "credit_research_report.md").touch()

    runs = discover_research_runs(
        PROJECT_ROOT,
        roots=[tmp_path / "root-a", tmp_path / "root-b"],
    )

    assert len(runs) == 2
    assert {item.run_dir for item in runs} == {first.resolve(), second.resolve()}
