import pytest

from credit_research_agent.company import CompanyResolutionError, CompanyResolver
from credit_research_agent.models import EntityScope


@pytest.fixture(scope="module")
def resolver() -> CompanyResolver:
    return CompanyResolver.from_default_catalog()


@pytest.mark.parametrize(
    "query",
    [
        "以岭药业",
        "石家庄以岭药业股份有限公司",
        "002603",
        "002603.SZ",
        " 002603.sz ",
    ],
)
def test_yiling_aliases_resolve_to_listed_company(
    resolver: CompanyResolver, query: str
) -> None:
    company = resolver.resolve(query)

    assert company.legal_name == "石家庄以岭药业股份有限公司"
    assert company.ticker == "002603.SZ"
    assert company.security_code == "002603"
    assert company.exchange == "深圳证券交易所"
    assert company.entity_scope is EntityScope.CONSOLIDATED


@pytest.mark.parametrize("query", ["", "   ", "600998.SH", "以岭健康"])
def test_unknown_or_empty_company_is_not_guessed(
    resolver: CompanyResolver, query: str
) -> None:
    with pytest.raises(CompanyResolutionError):
        resolver.resolve(query)
