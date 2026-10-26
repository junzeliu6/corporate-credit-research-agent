from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from credit_research_agent.agent_runtime import (
    AgentExecutionStage,
    AgentExecutionTrace,
    _AgentTraceHooks,
)
from credit_research_agent.retrieval_runtime import (
    RetrievalExecutionTrace,
    RetrievalRunHooks,
    RetrievalStage,
)
from credit_research_agent.ui_progress import ProgressJsonlWriter, read_progress_events


FIXED_TIME = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _trace() -> RetrievalExecutionTrace:
    return RetrievalExecutionTrace(
        trace_id="UI-PROGRESS-001",
        task="离线进度测试",
        entity_id="CN-A-SZSE-002603",
        llm_config={"mode": "mock"},
        search_config={"mode": "mock"},
        budget_limits={},
    )


def test_progress_writer_keeps_only_safe_fields(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    writer = ProgressJsonlWriter(path)

    writer.write(
        "tool_selected",
        "调用工具",
        {
            "tool_name": "run_financial_research",
            "api_key": "must-not-appear",
            "raw_model_output": "must-not-appear",
        },
    )

    events = read_progress_events(path)
    assert events[0]["sequence"] == 1
    assert events[0]["details"] == {"tool_name": "run_financial_research"}
    assert "must-not-appear" not in path.read_text(encoding="utf-8")


def test_retrieval_hook_forwards_real_tool_event(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    writer = ProgressJsonlWriter(path)
    trace = _trace()
    hook = RetrievalRunHooks(lambda: FIXED_TIME, writer)

    hook._record(
        trace,
        RetrievalStage.TOOL_SELECTED,
        "LLM决定调用白名单检索Tool",
        {"tool_name": "search_web"},
    )

    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["stage"] == "tool_selected"
    assert payload["details"]["tool_name"] == "search_web"
    assert trace.events[0].stage is RetrievalStage.TOOL_SELECTED


def test_single_skill_agent_hook_forwards_tool_event(tmp_path: Path) -> None:
    path = tmp_path / "single-skill-progress.jsonl"
    writer = ProgressJsonlWriter(path)
    trace = AgentExecutionTrace(
        trace_id="UI-SINGLE-001",
        task="股权结构研究",
        config={"mode": "mock"},
        entity_id="CN-A-SZSE-002603",
        source_id="S01",
    )
    hook = _AgentTraceHooks(lambda: FIXED_TIME, writer)

    hook._record(
        trace,
        AgentExecutionStage.LLM_TOOL_SELECTED,
        "LLM决定调用白名单Function Tool",
        {"tool_name": "run_ownership_research"},
    )

    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["stage"] == "llm_tool_selected"
    assert payload["details"] == {"tool_name": "run_ownership_research"}
    assert trace.events[0].stage is AgentExecutionStage.LLM_TOOL_SELECTED


def test_progress_reader_ignores_incomplete_lines(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    path.write_text(
        '{"stage":"run_started","message":"开始"}\n{"stage":',
        encoding="utf-8",
    )

    assert [item["stage"] for item in read_progress_events(path)] == ["run_started"]
