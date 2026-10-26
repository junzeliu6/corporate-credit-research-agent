import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from credit_research_agent.ui_artifacts import (
    ArtifactAccessError,
    ReportDisplayStatus,
    discover_research_runs,
    ensure_word_report,
    load_financial_tables,
    load_research_run,
    read_report_markdown,
    read_word_report,
)
from credit_research_agent.word_export import WordExportError


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


def test_word_export_is_confined_to_the_artifact_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path)
    assets = run / "assets"
    assets.mkdir()
    image = assets / "ownership_structure.png"
    image.write_bytes(b"png")
    artifact = load_research_run(run, allowed_roots=[tmp_path])
    calls: list[dict[str, object]] = []

    def fake_export(markdown_path: Path, output_path: Path, **kwargs: object) -> object:
        calls.append(
            {
                "markdown_path": markdown_path,
                "output_path": output_path,
                **kwargs,
            }
        )
        Path(output_path).write_bytes(b"valid-docx-payload")
        return SimpleNamespace(docx_path=Path(output_path).resolve())

    monkeypatch.setattr(
        "credit_research_agent.ui_artifacts.export_markdown_report", fake_export
    )

    path, payload = read_word_report(artifact)

    assert path == (run / "credit_research_report.docx").resolve()
    assert payload == b"valid-docx-payload"
    assert len(calls) == 1
    assert calls[0]["allowed_root"] == run.resolve()
    assert calls[0]["ownership_image_path"] == image.resolve()
    assert calls[0]["require_ownership_image"] is True
    assert Path(calls[0]["output_path"]).parent == run.resolve()


def test_fresh_word_export_is_reused_without_running_converter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path)
    artifact = load_research_run(run, allowed_roots=[tmp_path])
    output = run / "credit_research_report.docx"
    output.write_bytes(b"existing-docx")
    report_time = artifact.report_path.stat().st_mtime_ns if artifact.report_path else 0
    os.utime(output, ns=(report_time + 1_000_000, report_time + 1_000_000))

    def unexpected_export(*args: object, **kwargs: object) -> object:
        raise AssertionError("页面刷新不应重复导出未变更的 Word")

    monkeypatch.setattr(
        "credit_research_agent.ui_artifacts.export_markdown_report", unexpected_export
    )

    assert ensure_word_report(artifact) == output.resolve()


def test_word_export_rejects_report_outside_run_directory(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    artifact = load_research_run(run, allowed_roots=[tmp_path])
    outside = tmp_path / "other.md"
    outside.write_text("# outside", encoding="utf-8")

    with pytest.raises(ArtifactAccessError, match="超出本次运行目录"):
        ensure_word_report(replace(artifact, report_path=outside.resolve()))


def test_existing_recovered_word_file_is_discovered_separately(tmp_path: Path) -> None:
    run = _make_run(tmp_path, status="failed")
    (run / "credit_research_report_recovered.md").write_text(
        "# 恢复后的 DeepSeek 报告", encoding="utf-8"
    )
    recovered_docx = run / "credit_research_report_recovered.docx"
    recovered_docx.write_bytes(b"recovered")
    _write_json(
        run / "credit_research_recovery.json",
        {"status": "recovered_offline", "original_run_status": "failed"},
    )

    artifact = load_research_run(run, allowed_roots=[tmp_path])

    assert artifact.word_report_path == recovered_docx.resolve()


def test_cached_word_is_invalidated_when_another_markdown_chart_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path)
    assets = run / "assets"
    assets.mkdir()
    chart = assets / "financial_trend.png"
    chart.write_bytes(b"chart-v1")
    report = run / "credit_research_report.md"
    report.write_text(
        "# 报告\n\n## 财务趋势\n\n![财务趋势](assets/financial_trend.png)\n",
        encoding="utf-8",
    )
    artifact = load_research_run(run, allowed_roots=[tmp_path])
    output = run / "credit_research_report.docx"
    output.write_bytes(b"old-docx")
    report_time = report.stat().st_mtime_ns
    os.utime(output, ns=(report_time + 1_000_000, report_time + 1_000_000))
    os.utime(chart, ns=(report_time + 2_000_000, report_time + 2_000_000))
    calls = 0

    def fake_export(markdown_path: Path, output_path: Path, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        Path(output_path).write_bytes(b"new-docx")
        return SimpleNamespace(docx_path=Path(output_path).resolve())

    monkeypatch.setattr(
        "credit_research_agent.ui_artifacts.export_markdown_report", fake_export
    )

    assert ensure_word_report(artifact) == output.resolve()
    assert calls == 1
    assert output.read_bytes() == b"new-docx"


def test_ownership_chart_heading_cannot_reuse_or_export_a_missing_chart(
    tmp_path: Path,
) -> None:
    run = _make_run(tmp_path)
    report = run / "credit_research_report.md"
    report.write_text("# 报告\n\n## 股权结构图\n\n图表待补充。\n", encoding="utf-8")
    output = run / "credit_research_report.docx"
    output.write_bytes(b"stale-docx-without-chart")
    artifact = load_research_run(run, allowed_roots=[tmp_path])

    with pytest.raises(WordExportError) as error:
        ensure_word_report(artifact)

    assert error.value.code == "ownership_image_required"
