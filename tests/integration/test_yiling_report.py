from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from credit_research_agent.reporting import (
    AnalysisValidationStatus,
    ValidatedAgentAnalysis,
    assemble_report_artifacts,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FUNDAMENTALS_PATH = PROJECT_ROOT / "examples" / "S01_fundamentals_sample.json"
OWNERSHIP_DIR = (
    PROJECT_ROOT
    / "examples"
    / "v1_3b_source_bound_live"
    / "CN-A-SZSE-002603"
)


@pytest.mark.integration
def test_yiling_verified_research_artifacts_generate_markdown_report(
    tmp_path: Path,
) -> None:
    required = [
        FUNDAMENTALS_PATH,
        OWNERSHIP_DIR / "ownership.json",
        OWNERSHIP_DIR / "ownership_graph.json",
        OWNERSHIP_DIR / "ownership_validation.json",
        OWNERSHIP_DIR / "ownership_structure.svg",
        OWNERSHIP_DIR / "ownership_structure.png",
    ]
    if not all(path.is_file() for path in required):
        pytest.skip("本机缺少已核验的以岭药业研究产物")

    artifacts = assemble_report_artifacts(
        fundamentals_path=FUNDAMENTALS_PATH,
        ownership_dataset_path=OWNERSHIP_DIR / "ownership.json",
        ownership_graph_path=OWNERSHIP_DIR / "ownership_graph.json",
        ownership_validation_path=OWNERSHIP_DIR / "ownership_validation.json",
        ownership_svg_path=OWNERSHIP_DIR / "ownership_structure.svg",
        ownership_png_path=OWNERSHIP_DIR / "ownership_structure.png",
        report_path=tmp_path / "credit_research_report.md",
        analysis=ValidatedAgentAnalysis(
            status=AnalysisValidationStatus.REJECTED,
            validation_note="ANALYSIS-CONTROL-001：A002缺少CR001支持",
        ),
        clock=lambda: datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )
    content = artifacts.report_path.read_text(encoding="utf-8")

    assert artifacts.entity_id == "CN-A-SZSE-002603"
    assert artifacts.fact_count == 21
    assert artifacts.missing_information_count == 5
    assert artifacts.analysis_status is AnalysisValidationStatus.REJECTED
    assert "石家庄以岭药业股份有限公司公开信息授信研究报告" in content
    assert "公司的主营业务是专利创新中药的研发、生产和销售。 [S01]" in content
    assert "通心络胶囊" in content
    assert "以岭医药科技有限公司：31.53%" in content
    assert "吴以岭、吴相君、吴瑞为石家庄以岭药业股份有限公司的实际控制人" in content
    assert "本次 AI 分析未通过事实校验" in content
    assert artifacts.ownership_svg_path is not None
    assert artifacts.ownership_svg_path.is_file()
