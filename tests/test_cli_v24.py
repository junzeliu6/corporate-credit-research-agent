"""V2.4 命令行入口的离线产物测试。"""

from __future__ import annotations

import json
from pathlib import Path

from credit_research_agent import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = "研究以岭药业行业趋势、竞争格局及政策，并结合财务表现分析授信影响"
FINANCIAL_EXAMPLE_DIR = (
    PROJECT_ROOT
    / "examples"
    / "v2_3_financial_live_narrative_50000_timeout120"
    / "CN-A-SZSE-002603"
)
FUNDAMENTALS_EXAMPLE_PATH = (
    PROJECT_ROOT
    / "examples"
    / "v1_4_report"
    / "research"
    / "fundamentals"
    / "CN-A-SZSE-002603"
    / "fundamentals.json"
)


def test_cli_v24_mock_writes_report_context_sources_and_trace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    output_root = tmp_path / "industry"
    trace_path = tmp_path / "industry-trace.json"

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-industry-research",
            TASK,
            "--llm-mode",
            "mock",
            "--search-mode",
            "mock",
            "--industry-dir",
            str(output_root),
            "--industry-trace-output",
            str(trace_path),
            "--industry-financial-dir",
            str(FINANCIAL_EXAMPLE_DIR),
            "--industry-fundamentals-path",
            str(FUNDAMENTALS_EXAMPLE_PATH),
            "--max-agent-turns",
            "20",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["agent_config"]["mode"] == "mock"
    assert output["search_config"]["mode"] == "mock"
    result = output["industry_research"]
    expected_dir = output_root / "CN-A-SZSE-002603"
    assert Path(result["report_path"]) == (
        expected_dir / "industry_analysis.md"
    ).resolve()
    assert Path(result["context_path"]) == (
        expected_dir / "industry_research_context.json"
    ).resolve()
    assert Path(result["sources_path"]) == (
        expected_dir / "industry_sources.json"
    ).resolve()
    assert Path(result["trace_path"]) == trace_path.resolve()
    assert result["search_api_requests"] == 0
    for path in (
        result["report_path"],
        result["context_path"],
        result["sources_path"],
        result["trace_path"],
        result["draft_path"],
        result["delivery_path"],
    ):
        assert Path(path).is_file()
