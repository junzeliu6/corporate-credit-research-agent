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
def test_yiling_mock_agent_writes_scripted_report_and_delivery_artifacts(
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
    delivery_path = Path(result.artifacts["llm_delivery_path"])
    assert draft_path.is_file()
    assert delivery_path.is_file()
    draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
    delivery_payload = json.loads(delivery_path.read_text(encoding="utf-8"))
    assert sentinel in draft_payload["raw_model_output"]
    assert delivery_payload["status"] == "complete"
    assert delivery_payload["mode"] == "mock"
    assert delivery_payload["llm_authored_report"] is False
    assert delivery_payload["python_rewrote_llm_text"] is False
    assert delivery_payload["content_validation_gate"] == "disabled"
    assert delivery_payload["status_basis"] == (
        "required_topics_and_minimum_json_structure"
    )
    assert "## 核心财务数据" in result.final_output
    assert "[FM" not in result.final_output
    assert "[S01]" not in result.final_output
    assert Path(result.artifacts["data_appendix_path"]).is_file()
    assert result.trace_path.is_file()
    assert result.skill_trace_path.is_file()


@pytest.mark.integration
def test_checked_in_live_report_is_reader_facing_and_complete() -> None:
    artifact_dir = (
        PROJECT_ROOT
        / "examples"
        / "v2_3_financial_live_narrative_50000_timeout120"
        / "CN-A-SZSE-002603"
    )
    report = (artifact_dir / "financial_analysis.md").read_text(encoding="utf-8")
    delivery = json.loads(
        (artifact_dir / "financial_llm_delivery.json").read_text(encoding="utf-8")
    )

    expected_headings = (
        "一、授信结论摘要",
        "二、营业收入",
        "三、应收账款",
        "四、存货",
        "五、货币资金",
        "六、短期借款及债务结构",
        "七、应付账款",
        "八、经营活动现金流",
        "九、跨指标综合分析与潜在风险判断",
    )
    assert all(f"## {heading}" in report for heading in expected_headings)
    assert sum(
        line.startswith(f"{index}. ")
        for index in range(1, 10)
        for line in report.splitlines()
    ) == 9
    assert "不适用（跨符号）" in report
    assert "短期借款 | FY2024 未取得 | FY2025 未取得" in report
    assert "专业分析正文由 DeepSeek 撰写" in report
    assert not any(
        marker in report
        for marker in (
            "[FM",
            "[FR",
            "[RF",
            "[F2",
            "[S01]",
            "工具标记",
            "工具未提供",
            "本次工具未形成",
            "自动化风险信号",
        )
    )
    assert delivery["section_count"] == 9
    assert delivery["question_count"] == 9
    assert delivery["status_basis"] == (
        "required_topics_and_minimum_json_structure"
    )
    assert delivery["human_reviewed_presentation_copy"] is True

