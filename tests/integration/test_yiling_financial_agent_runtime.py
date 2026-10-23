"""真实S01/S10缓存上的V2.3财务Mock Agent集成测试。"""

import json
from pathlib import Path

import pytest

from credit_research_agent.company import CompanyResolver
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialSourceInput
from credit_research_agent.financial_agent_runtime import FinancialResearchAgentRuntime
from credit_research_agent.llm_config import load_deepseek_config
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _input(source_id: str, catalog: SourceCatalog) -> FinancialSourceInput:
    return FinancialSourceInput(
        source=catalog.get(source_id),
        download=DownloadResult.parse_raw(
            (PROJECT_ROOT / "data" / "cache" / f"{source_id}.metadata.json").read_text(
                encoding="utf-8"
            )
        ),
        summary_path=PROJECT_ROOT / "data" / "parsed" / source_id / "summary.json",
        pages_path=PROJECT_ROOT / "data" / "parsed" / source_id / "pages.jsonl",
    )


@pytest.mark.integration
def test_yiling_mock_agent_writes_scripted_report_and_validation_artifacts(
    tmp_path: Path,
) -> None:
    required = [
        PROJECT_ROOT / "data" / "cache" / f"{source_id}.metadata.json"
        for source_id in ("S01", "S10")
    ] + [
        PROJECT_ROOT / "data" / "parsed" / source_id / name
        for source_id in ("S01", "S10")
        for name in ("summary.json", "pages.jsonl")
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        pytest.skip("本机缺少已验证的财务年报产物：" + ", ".join(missing))

    catalog = SourceCatalog.from_default_catalog()
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    primary = _input("S01", catalog)
    comparison = _input("S10", catalog)
    runtime = FinancialResearchAgentRuntime(
        PROJECT_ROOT,
        load_deepseek_config(PROJECT_ROOT),
    )
    result = runtime.run_sync(
        task="请分析以岭药业财务报表、现金流和重大同比变动",
        identity=identity,
        source=primary.source,
        download=primary.download,
        summary_path=primary.summary_path,
        pages_path=primary.pages_path,
        comparison_inputs=[comparison],
        financial_dir=tmp_path / "financial",
        trace_output=tmp_path / "agent_trace.json",
        skill_trace_output=tmp_path / "skill_trace.json",
    )

    assert result.selected_skill == "financial-analysis"
    assert result.tool_name == "build_financial_artifacts"
    sentinel = (
        "【离线Mock哨兵】本段模拟 DeepSeek 的财务分析正文，"
        "仅用于验证模型原文不会被 Python 模板替换。"
    )
    assert sentinel in result.final_output
    assert "主要变动与银行授信观察" not in result.final_output
    assert "支持性推断（非披露原因）" not in result.final_output
    analysis_path = Path(result.artifacts["analysis_path"])
    assert analysis_path.read_text(encoding="utf-8") == result.final_output
    draft_path = Path(result.artifacts["llm_draft_path"])
    validation_path = Path(result.artifacts["llm_validation_path"])
    assert draft_path.is_file()
    assert validation_path.is_file()
    draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
    validation_payload = json.loads(validation_path.read_text(encoding="utf-8"))
    assert sentinel in draft_payload["raw_model_output"]
    assert validation_payload["status"] == "complete"
    assert validation_payload["generation_mode"] == "mock"
    assert validation_payload["llm_authored_report"] is False
    assert validation_payload["python_rewrote_llm_text"] is False
    assert Path(result.artifacts["data_appendix_path"]).is_file()
    assert result.trace_path.is_file()
    assert result.skill_trace_path.is_file()

