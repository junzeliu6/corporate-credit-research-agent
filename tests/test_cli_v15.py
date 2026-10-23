from __future__ import annotations

import json
from pathlib import Path

from credit_research_agent import cli


TASK = "请搜索以岭药业官方网站，补充主营业务、主要产品和经营模式，并提供来源。"


def test_cli_v15_mock_runs_full_retrieval_without_network_or_keys(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    output_dir = tmp_path / "web-research"
    trace_path = tmp_path / "trace.json"

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-web-research",
            TASK,
            "--web-research-dir",
            str(output_dir),
            "--retrieval-trace-output",
            str(trace_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0, json.dumps(output, ensure_ascii=False, indent=2)
    assert output["agent_config"]["mode"] == "mock"
    assert output["search_config"]["mode"] == "mock"
    assert output["web_research"]["search_api_requests"] == 0
    assert output["web_research"]["estimated_search_cost_cny"] == 0
    assert output["web_research"]["selected_fact_ids"] == ["F801", "F802"]
    assert Path(output["web_research"]["report_path"]).is_file()
    assert output["web_research"]["budget_limits"]["max_agent_turns"] == 70
    assert output["web_research"]["budget_limits"]["max_total_tool_calls"] == 60
    assert trace_path.is_file()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert [item["tool_name"] for item in trace["tool_calls"]] == [
        "search_web",
        "fetch_source",
        "extract_evidence",
    ]
    assert trace["bound_source_ids"] == ["S05"]


def test_cli_explicit_max_tool_calls_caps_all_v15_function_tools(
    tmp_path: Path, capsys
) -> None:
    trace_path = tmp_path / "trace.json"
    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-web-research",
            TASK,
            "--max-tool-calls",
            "2",
            "--web-research-dir",
            str(tmp_path / "research"),
            "--retrieval-trace-output",
            str(trace_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert output["web_research"]["status"] == "failed"
    assert output["web_research"]["budget_limits"]["max_total_tool_calls"] == 2
    assert trace["tool_call_count"] == 2
    assert any(
        item.get("error_code") == "total_tool_budget_exceeded"
        for item in trace["tool_calls"]
    )
    assert Path(output["web_research"]["report_path"]).is_file()


def test_cli_v15_live_search_requires_key_before_runtime(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    empty_env = tmp_path / ".env.empty"
    empty_env.write_text("SEARCH_PROVIDER=baidu\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-web-research",
            TASK,
            "--search-mode",
            "live",
            "--env-file",
            str(empty_env),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 11
    assert output["retrieval_error"]["code"] == "missing_search_api_key"


def test_cli_v15_rejects_invalid_mock_fixture(tmp_path: Path, capsys) -> None:
    fixture = tmp_path / "broken.json"
    fixture.write_text("{not-json", encoding="utf-8")

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-web-research",
            TASK,
            "--search-fixture",
            str(fixture),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 11
    assert output["retrieval_error"]["code"] == "invalid_search_fixture"
