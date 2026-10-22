"""真实S01产物上的V1.3B Mock Agent端到端集成测试（不访问LLM网络）。"""

from pathlib import Path

import pytest

from credit_research_agent.agent_runtime import CreditResearchAgentRuntime
from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.llm_config import load_deepseek_config
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_MANIFEST = PROJECT_ROOT / "data" / "cache" / "S01.metadata.json"
PARSED_SUMMARY = PROJECT_ROOT / "data" / "parsed" / "S01" / "summary.json"
PARSED_PAGES = PROJECT_ROOT / "data" / "parsed" / "S01" / "pages.jsonl"


@pytest.mark.integration
def test_real_yiling_mock_agent_loads_skill_builds_graph_and_cites_source(
    tmp_path: Path,
) -> None:
    required = [CACHE_MANIFEST, PARSED_SUMMARY, PARSED_PAGES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的S01运行产物：" + ", ".join(missing))

    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    download = DownloadResult.parse_raw(CACHE_MANIFEST.read_text(encoding="utf-8"))
    runtime = CreditResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(PROJECT_ROOT),
    )

    result = runtime.run_sync(
        task="请分析以岭药业（002603.SZ）的股权结构，生成股权穿透图，并简要说明其主要股东与实际控制关系。",
        identity=identity,
        source=source,
        download=download,
        summary_path=PARSED_SUMMARY,
        pages_path=PARSED_PAGES,
        ownership_dir=tmp_path / "ownership",
        trace_output=tmp_path / "agent_execution_trace.json",
        skill_trace_output=tmp_path / "skill_execution_trace.json",
    )

    assert result.selected_skill == "ownership-structure"
    assert "31.53%" in result.final_output
    assert "[S01]" in result.final_output
    assert Path(result.artifacts["svg_path"]).is_file()
    assert Path(result.artifacts["png_path"]).is_file()
    assert result.trace_path.is_file()
    assert result.skill_trace_path.is_file()
