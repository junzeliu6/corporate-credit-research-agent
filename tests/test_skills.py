"""V1.3 Skill 注册、选择与只读加载测试。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from credit_research_agent.skills import (
    SkillLoadError,
    SkillLoader,
    SkillRegistry,
    SkillRuntimeStatus,
    SkillSelectionError,
    SkillSelector,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_registry_only_contains_allowlisted_project_skills() -> None:
    registrations = SkillRegistry(PROJECT_ROOT).list()

    assert [item.name for item in registrations] == [
        "financial-analysis",
        "ownership-structure",
    ]
    ownership = next(item for item in registrations if item.name == "ownership-structure")
    financial = next(item for item in registrations if item.name == "financial-analysis")
    assert ownership.runtime_status is SkillRuntimeStatus.READY
    assert ownership.tool_names == ["build_ownership_artifacts"]
    assert len(ownership.sha256) == 64
    assert ownership.relative_path == ".agents/skills/ownership-structure/SKILL.md"
    assert financial.runtime_status is SkillRuntimeStatus.READY
    assert financial.version == "2.2.0"
    assert financial.tool_names == ["build_financial_artifacts"]


def test_selector_routes_ownership_and_financial_tasks_deterministically() -> None:
    registry = SkillRegistry(PROJECT_ROOT)
    selector = SkillSelector(registry)

    ownership = selector.select("请生成股权结构图并检查实际控制人")
    financial = selector.select("分析财务报表、应收账款和经营现金流")

    assert ownership.selected_skill == "ownership-structure"
    assert "股权" in ownership.matched_keywords
    assert financial.selected_skill == "financial-analysis"
    assert "财务" in financial.matched_keywords


def test_selector_rejects_unknown_and_ambiguous_tasks() -> None:
    selector = SkillSelector(SkillRegistry(PROJECT_ROOT))

    with pytest.raises(SkillSelectionError, match="未命中") as unknown:
        selector.select("整理公司发展历史")
    assert unknown.value.code == "skill_not_selected"

    with pytest.raises(SkillSelectionError, match="同时命中") as ambiguous:
        selector.select("股权与财务联合分析")
    assert ambiguous.value.code == "ambiguous_skill_selection"


def test_loader_is_read_only_and_verifies_registered_hash() -> None:
    registry = SkillRegistry(PROJECT_ROOT)
    registration = registry.get("ownership-structure")
    skill_path = PROJECT_ROOT / registration.relative_path
    before_bytes = skill_path.read_bytes()
    before_mtime = skill_path.stat().st_mtime_ns

    loaded = SkillLoader(PROJECT_ROOT).load(registration)

    assert loaded.instructions.startswith("---\n")
    assert loaded.character_count == len(loaded.instructions)
    assert skill_path.read_bytes() == before_bytes
    assert skill_path.stat().st_mtime_ns == before_mtime


def test_loader_rejects_skill_modified_after_registration(tmp_path: Path) -> None:
    copied_root = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / ".agents", copied_root / ".agents")
    registry = SkillRegistry(copied_root)
    registration = registry.get("ownership-structure")
    skill_path = copied_root / registration.relative_path
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\n<!-- changed -->\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillLoadError, match="注册后发生变化") as error:
        SkillLoader(copied_root).load(registration)
    assert error.value.code == "skill_hash_mismatch"
