"""官方公开资料来源目录。"""

from __future__ import annotations

import json
import tempfile
from importlib import resources
from pathlib import Path
from typing import Dict, List

from credit_research_agent.models import SourceRecord


class SourceNotFoundError(KeyError):
    """来源编号不在登记表中。"""


class SourceCatalog:
    """来源目录的读取、筛选、显式合并与任务级持久化。

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

    @classmethod
    def from_path(cls, path: Path) -> "SourceCatalog":
        """读取任务级来源目录；不与包内静态来源隐式混合。"""

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("来源目录JSON必须为数组")
        return cls([SourceRecord.parse_obj(item) for item in payload])

    @classmethod
    def merged(cls, *catalogs: "SourceCatalog") -> "SourceCatalog":
        """显式合并来源目录，并拒绝编号或URL指向不同记录。"""

        records: Dict[str, SourceRecord] = {}
        url_to_id: Dict[str, str] = {}
        for catalog in catalogs:
            for source in catalog.list_all():
                existing = records.get(source.source_id)
                if existing is not None and existing.dict() != source.dict():
                    raise ValueError(f"来源编号冲突: {source.source_id}")
                url = str(source.url)
                existing_id = url_to_id.get(url)
                if existing_id is not None and existing_id != source.source_id:
                    raise ValueError(
                        f"同一来源URL被分配不同编号: {existing_id}/{source.source_id}"
                    )
                records[source.source_id] = source
                url_to_id[url] = source.source_id
        return cls(list(records.values()))

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

    def write(self, path: Path) -> Path:
        """原子写入任务级来源目录，供后续Runtime显式加载。"""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            [source.dict() for source in self.list_all()],
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
            temp_path.replace(path)
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        return path.resolve()

