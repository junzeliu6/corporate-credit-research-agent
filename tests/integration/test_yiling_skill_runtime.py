"""真实 S01 产物上的 V1.3 Skill 运行时集成测试。"""

from pathlib import Path

import pytest

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.skill_runtime import SkillExecutionStage, SkillRuntime
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_MANIFEST = PROJECT_ROOT / "data" / "cache" / "S01.metadata.json"
PARSED_SUMMARY = PROJECT_ROOT / "data" / "parsed" / "S01" / "summary.json"
PARSED_PAGES = PROJECT_ROOT / "data" / "parsed" / "S01" / "pages.jsonl"


@pytest.mark.integration
def test_real_yiling_task_selects_skill_loads_it_and_calls_ownership_tool(
    tmp_path: Path,
) -> None:
    required = [CACHE_MANIFEST, PARSED_SUMMARY, PARSED_PAGES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的S01运行产物：" + ", ".join(missing))

    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    download = DownloadResult.parse_raw(CACHE_MANIFEST.read_text(encoding="utf-8"))
    output_dir = tmp_path / "ownership"
    trace_path = tmp_path / "skill_execution_trace.json"

    result = SkillRuntime(PROJECT_ROOT).execute(
        task="生成以岭药业股权结构图并核验直接股东持股比例",
        identity=identity,
        source=source,
        tool_arguments={
            "download": download,
            "summary_path": PARSED_SUMMARY,
            "pages_path": PARSED_PAGES,
            "output_dir": output_dir,
        },
        trace_output=trace_path,
    )

    stages = [event.stage for event in result.trace.events]
    assert result.selection.selected_skill == "ownership-structure"
    assert result.loaded_skill_path == ".agents/skills/ownership-structure/SKILL.md"
    assert result.tool_name == "build_ownership_artifacts"
    assert SkillExecutionStage.TOOL_CALLED in stages
    assert SkillExecutionStage.STRUCTURED_RESULT_VALIDATED in stages
    assert result.artifacts["entity_id"] == identity.entity_id
    assert result.artifacts["node_count"] == 6
    assert result.artifacts["edge_count"] == 5
    assert Path(result.artifacts["svg_path"]).is_file()
    assert Path(result.artifacts["png_path"]).is_file()
    assert trace_path.is_file()
