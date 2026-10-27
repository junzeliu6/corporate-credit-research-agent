"""V3.1 Streamlit 执行适配层的纯离线测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from credit_research_agent.ui_service import (
    ResearchMode,
    ResearchRequest,
    UiServiceError,
    build_live_command,
    build_mock_command,
    load_api_readiness,
    read_cli_log_tail,
    read_progress_events,
    start_research_process,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = "请对以岭药业进行完整企业授信研究"


def _request(mode: ResearchMode, **overrides: Any) -> ResearchRequest:
    values: dict[str, Any] = {
        "company": "002603.SZ",
        "task": TASK,
        "mode": mode,
        "confirm_deepseek_live": mode.uses_llm,
        "confirm_baidu_live": mode.uses_search,
    }
    values.update(overrides)
    return ResearchRequest(**values)


@pytest.mark.parametrize(
    ("mode", "action", "directory_option"),
    [
        (ResearchMode.COMPLETE, "--run-complete-research", "--complete-dir"),
        (ResearchMode.OWNERSHIP, "--run-agent", "--ownership-dir"),
        (ResearchMode.FINANCIAL, "--run-agent", "--financial-dir"),
        (ResearchMode.INDUSTRY, "--run-industry-research", "--industry-dir"),
        (ResearchMode.FUNDAMENTALS, "--build-fundamentals", "--fundamentals-dir"),
    ],
)
def test_build_live_command_maps_only_whitelisted_cli_options(
    mode: ResearchMode, action: str, directory_option: str
) -> None:
    command = build_live_command(
        PROJECT_ROOT,
        _request(mode),
        run_id=f"test-{mode.value}",
        python_executable="python-test",
    )

    assert command.argv[:4] == (
        "python-test",
        "-m",
        "credit_research_agent.cli",
        "002603.SZ",
    )
    assert action in command.argv
    assert directory_option in command.argv
    assert command.argv[-2:] == ("--progress-output", str(command.progress_path))
    assert command.run_dir.parent == PROJECT_ROOT / "data" / "streamlit_runs"
    assert command.artifact_dir.name == "CN-A-SZSE-002603"
    assert "shell" not in " ".join(command.argv).lower()
    if mode.uses_llm:
        timeout_index = command.argv.index("--request-timeout-seconds")
        retry_index = command.argv.index("--max-model-retries")
        assert command.argv[timeout_index + 1] == "600"
        assert command.argv[retry_index + 1] == "0"
    else:
        assert "--request-timeout-seconds" not in command.argv
        assert "--max-model-retries" not in command.argv


def test_mock_command_does_not_override_live_timeout_or_retry_policy() -> None:
    command = build_mock_command(
        PROJECT_ROOT,
        _request(ResearchMode.COMPLETE),
        run_id="mock-keeps-cli-defaults",
        python_executable="python-test",
    )

    assert "--request-timeout-seconds" not in command.argv
    assert "--max-model-retries" not in command.argv
    assert "--max-agent-turns" not in command.argv
    assert "--max-tool-calls" not in command.argv


def test_complete_live_uses_conservative_hidden_budget() -> None:
    command = build_live_command(
        PROJECT_ROOT,
        _request(ResearchMode.COMPLETE),
        run_id="complete-live-budget",
        python_executable="python-test",
    )
    expected = {
        "--max-agent-turns": "30",
        "--max-tool-calls": "40",
        "--max-search-rounds": "6",
        "--max-search-calls": "6",
        "--max-search-requests": "6",
        "--max-source-fetches": "10",
        "--max-extract-calls": "10",
    }

    for option, value in expected.items():
        option_index = command.argv.index(option)
        assert command.argv[option_index + 1] == value


def test_complete_command_accepts_unregistered_a_share_and_enables_auto_prepare() -> None:
    """新企业只需合法A股代码；前端只做离线语法路由，主数据校验由CLI启动阶段执行。"""

    command = build_mock_command(
        PROJECT_ROOT,
        _request(
            ResearchMode.COMPLETE,
            company="600998.SH",
            confirm_deepseek_live=False,
            confirm_baidu_live=False,
        ),
        run_id="dynamic-jiuzhoutong",
        python_executable="python-test",
    )

    assert command.argv[:4] == (
        "python-test",
        "-m",
        "credit_research_agent.cli",
        "600998.SH",
    )
    assert "--auto-prepare" in command.argv
    assert "--annual-report-count" in command.argv
    assert command.argv[command.argv.index("--annual-report-count") + 1] == "2"
    assert command.argv[command.argv.index("--onboarding-dir") + 1] == str(
        command.run_dir / "onboarding"
    )
    assert command.artifact_dir == command.run_dir / "CN-A-SSE-600998"
    # 动态完整研究的Source ID由运行时绑定，不把演示公司S01/S10透传给CLI。
    assert "--agent-source" not in command.argv
    assert "--financial-comparison-source" not in command.argv


@pytest.mark.parametrize(
    "mode",
    [ResearchMode.OWNERSHIP, ResearchMode.FINANCIAL, ResearchMode.INDUSTRY],
)
def test_non_complete_live_modes_do_not_receive_complete_budget(
    mode: ResearchMode,
) -> None:
    command = build_live_command(
        PROJECT_ROOT,
        _request(mode),
        run_id=f"no-complete-budget-{mode.value}",
        python_executable="python-test",
    )

    assert "--max-agent-turns" not in command.argv
    assert "--max-tool-calls" not in command.argv


def test_complete_live_requires_both_explicit_confirmations() -> None:
    with pytest.raises(UiServiceError, match="DeepSeek"):
        build_live_command(
            PROJECT_ROOT,
            _request(ResearchMode.COMPLETE, confirm_deepseek_live=False),
            run_id="missing-llm-confirmation",
        )
    with pytest.raises(UiServiceError, match="百度"):
        build_live_command(
            PROJECT_ROOT,
            _request(ResearchMode.COMPLETE, confirm_baidu_live=False),
            run_id="missing-search-confirmation",
        )


def test_ownership_does_not_require_or_enable_search_live() -> None:
    command = build_live_command(
        PROJECT_ROOT,
        _request(ResearchMode.OWNERSHIP, confirm_baidu_live=False),
        run_id="ownership-only",
    )

    assert "--llm-mode" in command.argv
    assert "--confirm-live" in command.argv
    assert "--search-mode" not in command.argv
    assert "--confirm-search-live" not in command.argv


def test_fundamentals_is_deterministic_and_does_not_require_live_keys() -> None:
    command = build_live_command(
        PROJECT_ROOT,
        _request(
            ResearchMode.FUNDAMENTALS,
            confirm_deepseek_live=False,
            confirm_baidu_live=False,
        ),
        run_id="fundamentals-only",
    )

    assert "--llm-mode" not in command.argv
    assert "--confirm-live" not in command.argv
    assert "--search-mode" not in command.argv


def test_argv_list_keeps_task_text_as_one_argument_without_shell_execution() -> None:
    malicious = '研究任务; Remove-Item -Recurse C:\\important && echo "x"'
    command = build_live_command(
        PROJECT_ROOT,
        _request(ResearchMode.OWNERSHIP, task=malicious),
        run_id="safe-argv",
    )

    assert malicious in command.argv
    assert command.argv.count(malicious) == 1


def test_invalid_run_id_and_unknown_company_are_rejected() -> None:
    with pytest.raises(UiServiceError, match="run_id"):
        build_live_command(
            PROJECT_ROOT,
            _request(ResearchMode.COMPLETE),
            run_id="../escape",
        )
    with pytest.raises(UiServiceError, match="6位A股代码"):
        build_live_command(
            PROJECT_ROOT,
            _request(ResearchMode.COMPLETE, company="虚构企业"),
            run_id="unknown-company",
        )


def test_api_readiness_returns_booleans_not_secret_values(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=should-never-be-returned\nBAIDU_SEARCH_API_KEY=\n",
        encoding="utf-8",
    )

    readiness = load_api_readiness(tmp_path, environ={})

    assert readiness.deepseek_configured is True
    assert readiness.baidu_search_configured is False
    assert "should-never-be-returned" not in repr(readiness)


def test_start_process_uses_shell_false_and_fixed_workdir(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    # CompanyResolver 使用包内受控登记表，不依赖临时项目数据。
    command = build_live_command(
        project_root,
        _request(ResearchMode.OWNERSHIP),
        run_id="start-process",
        python_executable="python-test",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        def poll(self) -> int | None:
            return None

    def fake_factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        kwargs["stdout"].write("started\n")
        return FakeProcess()

    handle = start_research_process(
        command,
        process_factory=fake_factory,
        environ={"SAFE": "1"},
    )

    assert handle.poll() is None
    assert calls[0][0] == list(command.argv)
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == str(project_root.resolve())
    assert calls[0][1]["env"]["PYTHONUNBUFFERED"] == "1"
    assert command.log_path.read_text(encoding="utf-8") == "started\n"


def test_progress_reader_skips_partial_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "progress.jsonl"
    valid_one = {
        "sequence": 1,
        "stage": "run_started",
        "message": "开始",
        "occurred_at": "2026-10-26T10:00:00Z",
        "details": {},
    }
    valid_two = {
        "sequence": 2,
        "stage": "tool_selected",
        "message": "调用工具",
        "details": {"tool_name": "run_financial_research"},
    }
    path.write_text(
        json.dumps(valid_one, ensure_ascii=False)
        + "\nnot-json\n"
        + json.dumps(valid_two, ensure_ascii=False)
        + '\n{"sequence": 3,',
        encoding="utf-8",
    )

    events = read_progress_events(path)

    assert [item.stage for item in events] == ["run_started", "tool_selected"]
    assert events[-1].details["tool_name"] == "run_financial_research"


def test_log_tail_is_bounded_and_missing_files_are_empty(tmp_path: Path) -> None:
    path = tmp_path / "cli.log"
    path.write_text("0123456789", encoding="utf-8")

    assert read_cli_log_tail(path, max_chars=4) == "6789"
    assert read_cli_log_tail(tmp_path / "missing.log") == ""
    assert read_progress_events(tmp_path / "missing.jsonl") == []
