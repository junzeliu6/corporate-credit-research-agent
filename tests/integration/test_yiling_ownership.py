from pathlib import Path

import pytest

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.ownership import (
    OwnershipBuilder,
    OwnershipCompletenessStatus,
    PercentageOrigin,
)
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_MANIFEST = PROJECT_ROOT / "data" / "cache" / "S01.metadata.json"
PARSED_SUMMARY = PROJECT_ROOT / "data" / "parsed" / "S01" / "summary.json"
PARSED_PAGES = PROJECT_ROOT / "data" / "parsed" / "S01" / "pages.jsonl"


@pytest.mark.integration
def test_real_2025_annual_report_builds_traceable_yiling_ownership_graph() -> None:
    required = [CACHE_MANIFEST, PARSED_SUMMARY, PARSED_PAGES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的S01运行产物：" + ", ".join(missing))

    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    download = DownloadResult.parse_raw(CACHE_MANIFEST.read_text(encoding="utf-8"))

    result = OwnershipBuilder().build(
        identity=identity,
        source=source,
        download=download,
        summary_path=PARSED_SUMMARY,
        pages_path=PARSED_PAGES,
    )

    nodes = {item.node_id: item for item in result.graph.nodes}
    edges = {item.edge_id: item for item in result.graph.edges}
    evidence = {item.evidence_id: item for item in result.dataset.evidence}

    assert result.validation.is_valid
    assert len(result.dataset.nodes) == 5
    assert len(result.dataset.ownership_edges) == 5
    assert len(result.dataset.evidence) == 16
    assert len(result.graph.control_relationships) == 4
    assert str(edges["OE001"].percentage) == "31.53"
    assert str(edges["OE002"].percentage) == "20.81"
    assert str(edges["OE003"].percentage) == "2.34"
    assert str(edges["OE004"].percentage) == "100.00"
    assert "OE005" not in edges  # 表决权保留在原始JSON，不与持股线重复绘制。
    assert any(edge.edge_id == "OE005" for edge in result.dataset.ownership_edges)
    residual = next(
        edge
        for edge in result.graph.edges
        if edge.percentage_origin is PercentageOrigin.CALCULATED_RESIDUAL
    )
    assert str(residual.percentage) == "45.32"
    assert residual.source_node_id.startswith("RESIDUAL::")
    assert nodes["CN-A-SZSE-002603"].level == 1
    assert nodes["PERSON-WU-YILING"].level == 3
    assert evidence["E020"].page_number == 217
    assert evidence["E022"].page_number == 97
    assert evidence["E026"].page_number == 100
    assert all(edge.source_ids == ["S01"] for edge in result.graph.edges)
    validations = {
        branch.target_node_id: branch for branch in result.percentage_validation.branches
    }
    assert result.graph.formal_ready
    assert (
        validations["CN-A-SZSE-002603"].status
        is OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL
    )
    assert validations["ORG-YILING-MEDTECH"].status is OwnershipCompletenessStatus.COMPLETE
    assert len(result.graph.missing_information) == 2
    assert all(item.source_sha256 == download.sha256 for item in result.dataset.evidence)
