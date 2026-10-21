"""上市公司名称与证券代码识别。"""

from __future__ import annotations

import json
from importlib import resources
from typing import Dict, Iterable

from credit_research_agent.models import CompanyIdentity


class CompanyResolutionError(ValueError):
    """企业输入无法被唯一解析。"""


def _normalise_query(query: str) -> str:
    """统一用户输入格式，但不做模糊猜测。"""

    return "".join(query.strip().upper().split())


class CompanyResolver:
    """根据受控登记表解析上市公司。

    V0.1 只登记首测企业。使用受控别名而非模糊搜索，可以避免把控股股东、
    子公司或同名主体错误地当作上市公司。后续可替换为交易所证券主数据。
    """

    def __init__(self, companies: Iterable[CompanyIdentity]) -> None:
        self._companies: Dict[str, CompanyIdentity] = {}
        self._aliases: Dict[str, str] = {}

        for company in companies:
            if company.entity_id in self._companies:
                raise ValueError(f"重复的 entity_id: {company.entity_id}")
            self._companies[company.entity_id] = company

            aliases = {
                company.legal_name,
                company.short_name,
                company.security_code,
                company.ticker,
            }
            for alias in aliases:
                key = _normalise_query(alias)
                existing = self._aliases.get(key)
                if existing and existing != company.entity_id:
                    raise ValueError(f"企业别名存在歧义: {alias}")
                self._aliases[key] = company.entity_id

    @classmethod
    def from_default_catalog(cls) -> "CompanyResolver":
        catalog = resources.files("credit_research_agent.data").joinpath("companies.json")
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        companies = [CompanyIdentity.parse_obj(item) for item in payload]
        return cls(companies)

    def resolve(self, query: str) -> CompanyIdentity:
        if not query or not query.strip():
            raise CompanyResolutionError("企业名称或证券代码不能为空")

        entity_id = self._aliases.get(_normalise_query(query))
        if entity_id is None:
            raise CompanyResolutionError(
                f"无法唯一识别企业：{query!r}。V0.1 当前仅支持以岭药业（002603.SZ）。"
            )
        return self._companies[entity_id]

