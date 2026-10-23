"""V1.3 Skill 白名单、任务选择与只读加载。

这里的 Skill 是业务操作规范，不是可执行代码。运行时只允许读取项目内经过批准的
``SKILL.md``，真正的计算和文件生成仍由经过测试的 Python Tool 完成。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from pydantic.v1 import BaseModel, Field


class SkillRuntimeStatus(str, Enum):
    """Skill 在当前里程碑中的可执行状态。"""

    READY = "ready"
    SPEC_ONLY = "spec_only"


class SkillRegistration(BaseModel):
    """白名单中的一项 Skill；路径使用项目根目录下的相对路径。"""

    name: str
    description: str
    relative_path: str
    version: str
    sha256: str = Field(min_length=64, max_length=64)
    runtime_status: SkillRuntimeStatus
    tool_names: List[str] = Field(default_factory=list)

    class Config:
        allow_mutation = False


class SkillSelection(BaseModel):
    task: str
    selected_skill: str
    matched_keywords: List[str]
    rationale: str

    class Config:
        allow_mutation = False


class LoadedSkill(BaseModel):
    """只读加载后的 Skill 内容及完整性信息。"""

    registration: SkillRegistration
    instructions: str
    character_count: int = Field(ge=1)

    class Config:
        allow_mutation = False


class SkillRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillSelectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillLoadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ApprovedSkill:
    name: str
    relative_path: str
    version: str
    runtime_status: SkillRuntimeStatus
    tool_names: Tuple[str, ...]
    keywords: Tuple[str, ...]


APPROVED_SKILLS: Tuple[_ApprovedSkill, ...] = (
    _ApprovedSkill(
        name="ownership-structure",
        relative_path=".agents/skills/ownership-structure/SKILL.md",
        version="1.3.2",
        runtime_status=SkillRuntimeStatus.READY,
        tool_names=("build_ownership_artifacts",),
        keywords=(
            "股权",
            "股东",
            "持股",
            "穿透",
            "控股股东",
            "实际控制人",
            "一致行动",
            "国资背景",
            "ownership",
            "shareholder",
        ),
    ),
    _ApprovedSkill(
        name="financial-analysis",
        relative_path=".agents/skills/financial-analysis/SKILL.md",
        version="2.3.0",
        runtime_status=SkillRuntimeStatus.READY,
        tool_names=("build_financial_artifacts",),
        keywords=(
            "财务",
            "报表",
            "偿债",
            "现金流",
            "应收账款",
            "短期借款",
            "同比",
            "financial",
            "cash flow",
        ),
    ),
)


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_frontmatter(content: str, path: Path) -> Mapping[str, str]:
    """读取本项目 Skill 所需的最小 YAML frontmatter 字段。

    为避免仅解析两个固定字段却引入完整 YAML 依赖，这里只解析 ``name`` 和折叠的
    ``description``。一旦格式不符合预期就显式失败，不猜测元数据。
    """

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRegistryError(
            "invalid_skill_frontmatter", f"{path} 缺少起始 frontmatter"
        )
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillRegistryError(
            "invalid_skill_frontmatter", f"{path} 缺少结束 frontmatter"
        ) from exc

    metadata: Dict[str, str] = {}
    frontmatter = lines[1:closing]
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        if ":" not in line or line.startswith((" ", "\t")):
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value in {">", "|"}:
            parts: List[str] = []
            index += 1
            while index < len(frontmatter) and frontmatter[index].startswith((" ", "\t")):
                parts.append(frontmatter[index].strip())
                index += 1
            metadata[key] = " ".join(part for part in parts if part)
            continue
        metadata[key] = raw_value.strip("\"'")
        index += 1

    if not metadata.get("name") or not metadata.get("description"):
        raise SkillRegistryError(
            "invalid_skill_frontmatter", f"{path} 必须包含 name 和 description"
        )
    return metadata


class SkillRegistry:
    """只登记代码内批准的本地 Skill，绝不从网络发现或执行 Skill。"""

    def __init__(
        self,
        project_root: Path,
        *,
        approved_skills: Sequence[_ApprovedSkill] = APPROVED_SKILLS,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.skills_root = (self.project_root / ".agents" / "skills").resolve()
        self._approved = {item.name: item for item in approved_skills}

    def list(self) -> List[SkillRegistration]:
        return [self._register(self._approved[name]) for name in sorted(self._approved)]

    def get(self, name: str) -> SkillRegistration:
        try:
            approved = self._approved[name]
        except KeyError as exc:
            raise SkillRegistryError(
                "skill_not_allowlisted", f"Skill {name!r} 不在运行时白名单中"
            ) from exc
        return self._register(approved)

    def _register(self, approved: _ApprovedSkill) -> SkillRegistration:
        path = (self.project_root / approved.relative_path).resolve()
        try:
            path.relative_to(self.skills_root)
        except ValueError as exc:
            raise SkillRegistryError(
                "skill_path_outside_root",
                f"Skill 路径越出批准目录：{approved.relative_path}",
            ) from exc
        if not path.is_file():
            raise SkillRegistryError(
                "skill_file_missing", f"Skill 文件不存在：{approved.relative_path}"
            )
        content = path.read_text(encoding="utf-8")
        metadata = _parse_frontmatter(content, path)
        if metadata["name"] != approved.name:
            raise SkillRegistryError(
                "skill_name_mismatch",
                f"白名单名称 {approved.name!r} 与文件名称 {metadata['name']!r} 不一致",
            )
        return SkillRegistration(
            name=approved.name,
            description=metadata["description"],
            relative_path=approved.relative_path.replace("\\", "/"),
            version=approved.version,
            sha256=_sha256_text(content),
            runtime_status=approved.runtime_status,
            tool_names=list(approved.tool_names),
        )


class SkillSelector:
    """基于任务关键词的确定性路由器，便于离线测试和审计。"""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        approved_skills: Sequence[_ApprovedSkill] = APPROVED_SKILLS,
    ) -> None:
        self._registry = registry
        self._keywords = {item.name: item.keywords for item in approved_skills}

    def select(self, task: str) -> SkillSelection:
        normalized = " ".join(task.casefold().split())
        if not normalized:
            raise SkillSelectionError("empty_task", "任务描述不能为空")

        matches: Dict[str, List[str]] = {
            name: [keyword for keyword in keywords if keyword.casefold() in normalized]
            for name, keywords in self._keywords.items()
        }
        matches = {name: values for name, values in matches.items() if values}
        if not matches:
            raise SkillSelectionError(
                "skill_not_selected", "任务未命中任何已批准 Skill 的触发条件"
            )
        highest_score = max(len(values) for values in matches.values())
        winners = sorted(
            name for name, values in matches.items() if len(values) == highest_score
        )
        if len(winners) != 1:
            raise SkillSelectionError(
                "ambiguous_skill_selection",
                "任务同时命中多个 Skill，需拆分或明确任务：" + "、".join(winners),
            )

        selected_name = winners[0]
        registration = self._registry.get(selected_name)
        keywords = matches[selected_name]
        return SkillSelection(
            task=task,
            selected_skill=selected_name,
            matched_keywords=keywords,
            rationale=(
                f"任务命中 {selected_name} 的触发词：" + "、".join(keywords)
                + f"；当前状态为 {registration.runtime_status.value}。"
            ),
        )


class SkillLoader:
    """只读加载注册时的同一份文件，并校验目录和 SHA-256。"""

    def __init__(self, project_root: Path, *, max_characters: int = 200_000) -> None:
        self.project_root = Path(project_root).resolve()
        self.skills_root = (self.project_root / ".agents" / "skills").resolve()
        self.max_characters = max_characters

    def load(self, registration: SkillRegistration) -> LoadedSkill:
        path = (self.project_root / registration.relative_path).resolve()
        try:
            path.relative_to(self.skills_root)
        except ValueError as exc:
            raise SkillLoadError(
                "skill_path_outside_root",
                f"拒绝读取批准目录外的 Skill：{registration.relative_path}",
            ) from exc
        if not path.is_file():
            raise SkillLoadError(
                "skill_file_missing", f"Skill 文件不存在：{registration.relative_path}"
            )
        content = path.read_text(encoding="utf-8")
        if len(content) > self.max_characters:
            raise SkillLoadError(
                "skill_file_too_large",
                f"Skill 文件超过 {self.max_characters} 字符限制：{registration.name}",
            )
        actual_hash = _sha256_text(content)
        if actual_hash != registration.sha256:
            raise SkillLoadError(
                "skill_hash_mismatch",
                f"Skill {registration.name} 在注册后发生变化，拒绝加载",
            )
        metadata = _parse_frontmatter(content, path)
        if metadata["name"] != registration.name:
            raise SkillLoadError(
                "skill_name_mismatch", "Skill 文件名称与注册记录不一致"
            )
        return LoadedSkill(
            registration=registration,
            instructions=content,
            character_count=len(content),
        )
