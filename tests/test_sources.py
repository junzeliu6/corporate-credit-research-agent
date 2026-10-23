from datetime import date

import pytest

from credit_research_agent.models import SourceStatus, SourceType
from credit_research_agent.sources import SourceCatalog, SourceNotFoundError


@pytest.fixture(scope="module")
def catalog() -> SourceCatalog:
    return SourceCatalog.from_default_catalog()


def test_2025_annual_report_seed_is_registered_but_not_claimed_downloaded(
    catalog: SourceCatalog,
) -> None:
    source = catalog.get("S01")

    assert source.entity_id == "CN-A-SZSE-002603"
    assert source.title == "石家庄以岭药业股份有限公司2025年年度报告"
    assert source.source_type is SourceType.ANNUAL_REPORT
    assert str(source.url) == (
        "https://static.cninfo.com.cn/finalpage/2026-04-28/1225205683.PDF"
    )
    assert source.published_at == date(2026, 4, 28)
    assert source.report_period == "FY2025"
    assert source.trusted_tier == 1
    assert source.status is SourceStatus.REGISTERED
    assert source.accessed_at is None
    assert source.local_path is None


def test_official_source_seeds_are_available(catalog: SourceCatalog) -> None:
    urls = {str(source.url) for source in catalog.list_all()}

    assert "https://www.cninfo.com.cn/" in urls
    assert "https://www.szse.cn/" in urls
    assert "https://www.yiling.cn/" in urls


def test_entity_catalog_contains_report_and_general_official_portals(
    catalog: SourceCatalog,
) -> None:
    sources = catalog.list_for_entity("CN-A-SZSE-002603")
    assert {source.source_id for source in sources} == {
        "S01",
        "S02",
        "S03",
        "S04",
        "S10",
        "S11",
    }


def test_unknown_source_id_fails_explicitly(catalog: SourceCatalog) -> None:
    with pytest.raises(SourceNotFoundError, match="未登记来源"):
        catalog.get("S99")
