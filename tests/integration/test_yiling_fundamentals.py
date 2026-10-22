from pathlib import Path

import pytest

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.fundamentals import FundamentalsBuilder
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_MANIFEST = PROJECT_ROOT / "data" / "cache" / "S01.metadata.json"
PARSED_SUMMARY = PROJECT_ROOT / "data" / "parsed" / "S01" / "summary.json"
PARSED_PAGES = PROJECT_ROOT / "data" / "parsed" / "S01" / "pages.jsonl"


@pytest.mark.integration
def test_real_2025_annual_report_builds_traceable_yiling_fundamentals() -> None:
    required = [CACHE_MANIFEST, PARSED_SUMMARY, PARSED_PAGES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的S01运行产物：" + ", ".join(missing))

    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    download = DownloadResult.parse_raw(CACHE_MANIFEST.read_text(encoding="utf-8"))

    bundle = FundamentalsBuilder().build(
        identity=identity,
        source=source,
        download=download,
        summary_path=PARSED_SUMMARY,
        pages_path=PARSED_PAGES,
    )

    facts = {item.fact_id: item for item in bundle.facts}
    evidence = {item.evidence_id: item for item in bundle.evidence}

    assert bundle.identity.ticker == "002603.SZ"
    assert len(bundle.facts) == 21
    assert len(bundle.evidence) == 19
    assert facts["F006"].value == "2011 年 7 月 28 日"
    assert facts["F008"].value == "专利创新中药的研发、生产和销售"
    assert facts["F015"].value == "络病理论指导下的专业化推广模式"
    assert facts["F021"].value == "芪苈强心胶囊"
    assert evidence["E007"].page_number == 10
    assert evidence["E009"].page_number == 27
    assert evidence["E015"].page_number == 28
    assert evidence["E018"].page_number == 128
    assert bundle.inferences == []
    assert len(bundle.missing_information) == 3
    assert all(item.source_sha256 == download.sha256 for item in bundle.evidence)
