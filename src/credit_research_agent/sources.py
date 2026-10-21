"""官方公开资料来源目录。"""

from __future__ import annotations

import json
from importlib import resources
from typing import Dict, List

from credit_research_agent.models import SourceRecord


class SourceNotFoundError(KeyError):
    """来源编号不在登记表中。"""


class SourceCatalog:
    """只读来源目录。

    V0.1 的目录来自随包分发的 JSON，因此离线测试稳定；目录中的 ``registered``
    仅表示已登记元数据，不表示 URL 在本次运行中已成功访问。
    """

    def __init__(self, sources: List[SourceRecord]) -> None:
        self._sources: Dict[str, SourceRecord] = {}
        for source in sources:
            if source.source_id in self._sources:
                raise ValueError(f"重复的 source_id: {source.source_id}")
            self._sources[source.source_id] = source

    @classmethod
    def from_default_catalog(cls) -> "SourceCatalog":
        catalog = resources.files("credit_research_agent.data").joinpath("sources.json")
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        return cls([SourceRecord.parse_obj(item) for item in payload])

    def get(self, source_id: str) -> SourceRecord:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise SourceNotFoundError(f"未登记来源：{source_id}") from exc

    def list_for_entity(
        self, entity_id: str, *, include_general: bool = True
    ) -> List[SourceRecord]:
        """返回企业专属来源，并可附带通用官方检索入口。"""

        return [
            source
            for source in self._sources.values()
            if source.entity_id == entity_id
            or (include_general and source.entity_id is None)
        ]

    def list_all(self) -> List[SourceRecord]:
        return list(self._sources.values())

