from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from credit_research_agent.ui_service import (
    ResearchMode,
    ResearchRequest,
    UiServiceError,
    UiTaskStatus,
    build_live_command,
    build_mock_command,
    discover_ui_tasks,
    load_ui_task,
    reconcile_interrupted_task,
    submit_research_task,
    _process_is_alive,
)
from credit_research_agent.ui_worker import main as worker_main
from credit_research_agent.ui_worker import run_worker


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='ui-task-test'\n", encoding="utf-8")
    return root


def _request(mode: ResearchMode = ResearchMode.COMPLETE) -> ResearchRequest:
    return ResearchRequest(
        company="002603.SZ",
        task="请完成企业背景、股权结构、行业政策及财务状况的完整授信研究",
        mode=mode,
    )


class FakeProcess:
    pid = 321

    def poll(self) -> None:
        return None


def _submit(root: Path, run_id: str = "mock-task", *, key: str | None = None):
    command = build_mock_command(
        root,
        _request(),
        run_id=run_id,
        python_executable="python-test",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        return FakeProcess()

    submission = submit_research_task(
        command,
        idempotency_key=key,
        process_factory=factory,
        environ={"SAFE": "1", "DEEPSEEK_API_KEY": "not-written"},
    )
    return command, submission, calls


def test_mock_command_never_carries_live_confirmations(tmp_path: Path) -> None:
    command = build_mock_command(
        _project(tmp_path),
        _request(),
        run_id="offline",
        python_executable="python-test",
    )

    assert ("--llm-mode", "mock") == (
        command.argv[command.argv.index("--llm-mode")],
        command.argv[command.argv.index("--llm-mode") + 1],
    )
    assert ("--search-mode", "mock") == (
        command.argv[command.argv.index("--search-mode")],
        command.argv[command.argv.index("--search-mode") + 1],
    )
    assert "--confirm-live" not in command.argv
    assert "--confirm-search-live" not in command.argv


def test_submit_writes_safe_manifest_and_starts_worker_without_shell(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, calls = _submit(root)

    assert submission.record.status is UiTaskStatus.QUEUED
    assert submission.reused is False
    request_text = submission.record.request_path.read_text(encoding="utf-8")
    assert "not-written" not in request_text
    assert calls[0][0][1:3] == ["-m", "credit_research_agent.ui_worker"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == str(root.resolve())
    assert command.run_dir.is_dir()


def test_idempotency_key_reuses_existing_task_without_new_process(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _, first, first_calls = _submit(root, "first", key="session-click-001")
    second_command = build_mock_command(
        root,
        _request(),
        run_id="second",
        python_executable="python-test",
    )
    second_calls: list[Any] = []

    second = submit_research_task(
        second_command,
        idempotency_key="session-click-001",
        process_factory=lambda *args, **kwargs: second_calls.append((args, kwargs)),
    )

    assert first_calls
    assert second.reused is True
    assert second.record.task_id == first.record.task_id
    assert second_calls == []
    assert not second_command.run_dir.exists()


def test_worker_start_failure_is_persisted_as_failed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command = build_mock_command(
        root,
        _request(),
        run_id="worker-start-failed",
        python_executable="python-test",
    )

    def fail_to_start(*args: Any, **kwargs: Any) -> FakeProcess:
        raise OSError("secret detail must not be persisted")

    with pytest.raises(UiServiceError, match="无法启动后台研究任务"):
        submit_research_task(
            command,
            idempotency_key="worker-start-failed",
            process_factory=fail_to_start,
            environ={"DEEPSEEK_API_KEY": "must-not-leak"},
        )

    record = load_ui_task(command.run_dir, project_root=root)
    assert record.status is UiTaskStatus.FAILED
    assert record.finished_at is not None
    assert record.pid is None
    assert record.error_message == "无法启动后台 Worker：OSError"
    status_text = record.status_path.read_text(encoding="utf-8")
    assert "secret detail" not in status_text
    assert "must-not-leak" not in status_text


def test_stale_queued_task_is_reconciled_after_grace_period(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _, submission, _ = _submit(root, "stale-queued")
    created = datetime.fromisoformat(submission.record.created_at)

    recovered = reconcile_interrupted_task(
        submission.record,
        now=created + timedelta(seconds=61),
        queued_grace_seconds=60,
    )

    assert recovered.status is UiTaskStatus.FAILED
    assert recovered.error_message == "后台 Worker 未能启动或已意外中断"


def test_running_task_reconciliation_checks_worker_pid(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, _ = _submit(root, "running-worker")
    status = json.loads(submission.record.status_path.read_text(encoding="utf-8"))
    status.update(
        {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pid": 4321,
        }
    )
    submission.record.status_path.write_text(
        json.dumps(status), encoding="utf-8"
    )
    running = load_ui_task(command.run_dir, project_root=root)

    unchanged = reconcile_interrupted_task(
        running,
        process_alive=lambda pid: pid == 4321,
    )
    assert unchanged.status is UiTaskStatus.RUNNING

    recovered = reconcile_interrupted_task(
        unchanged,
        process_alive=lambda pid: False,
    )
    assert recovered.status is UiTaskStatus.FAILED
    assert recovered.error_message == "后台 Worker 已意外中断"


def test_process_probe_uses_non_destructive_windows_branch(monkeypatch) -> None:
    probed: list[int] = []

    def forbidden_kill(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Windows不得调用os.kill(pid, 0)")

    def windows_probe(pid: int) -> bool:
        probed.append(pid)
        return False

    monkeypatch.setattr("credit_research_agent.ui_service.os.kill", forbidden_kill)

    assert _process_is_alive(
        4321,
        platform_name="win32",
        windows_probe=windows_probe,
    ) is False
    assert probed == [4321]


def test_process_probe_uses_posix_signal_zero(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "credit_research_agent.ui_service.os.kill",
        lambda pid, signal: calls.append((pid, signal)),
    )

    assert _process_is_alive(4321, platform_name="linux") is True
    assert calls == [(4321, 0)]


def test_live_task_lock_is_atomic_across_sessions_and_released_by_worker(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    live_request = ResearchRequest(
        company="002603.SZ",
        task="请完成企业背景、股权结构、行业政策及财务状况的完整授信研究",
        mode=ResearchMode.COMPLETE,
        confirm_deepseek_live=True,
        confirm_baidu_live=True,
    )
    first_command = build_live_command(
        root,
        live_request,
        run_id="live-first",
        python_executable="python-test",
    )
    first = submit_research_task(
        first_command,
        process_factory=lambda *args, **kwargs: FakeProcess(),
    )
    second_command = build_live_command(
        root,
        live_request,
        run_id="live-second",
        python_executable="python-test",
    )

    with pytest.raises(UiServiceError, match="另一个 Live"):
        submit_research_task(
            second_command,
            process_factory=lambda *args, **kwargs: FakeProcess(),
        )
    second = load_ui_task(second_command.run_dir, project_root=root)
    assert second.status is UiTaskStatus.FAILED

    assert (
        run_worker(
            first.record.request_path,
            runner=lambda *args, **kwargs: SimpleNamespace(returncode=0),
        )
        == 0
    )

    third_command = build_live_command(
        root,
        live_request,
        run_id="live-third",
        python_executable="python-test",
    )
    third = submit_research_task(
        third_command,
        process_factory=lambda *args, **kwargs: FakeProcess(),
    )
    assert third.record.status is UiTaskStatus.QUEUED


def test_corrupt_live_lock_is_recovered_only_without_other_active_task(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    lock_path = root / "data" / "streamlit_runs" / ".active_live_task.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{incomplete", encoding="utf-8")
    request = ResearchRequest(
        company="002603.SZ",
        task="请完成完整授信研究",
        mode=ResearchMode.COMPLETE,
        confirm_deepseek_live=True,
        confirm_baidu_live=True,
    )
    command = build_live_command(
        root,
        request,
        run_id="recover-corrupt-lock",
        python_executable="python-test",
    )

    submission = submit_research_task(
        command,
        process_factory=lambda *args, **kwargs: FakeProcess(),
    )

    assert submission.record.status is UiTaskStatus.QUEUED
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock_payload["task_id"] == "recover-corrupt-lock"


def test_worker_executes_only_rebuilt_mock_cli_and_records_success(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, _ = _submit(root)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((argv, kwargs))
        command.artifact_dir.mkdir(parents=True)
        (command.artifact_dir / "credit_research_delivery.json").write_text(
            json.dumps({"status": "partial_success"}),
            encoding="utf-8",
        )
        kwargs["stdout"].write("mock cli completed\n")
        return SimpleNamespace(returncode=0)

    exit_code = run_worker(
        submission.record.request_path,
        runner=runner,
        environ={"SAFE": "1"},
        pid=456,
    )
    record = load_ui_task(command.run_dir, project_root=root)

    assert exit_code == 0
    assert record.status is UiTaskStatus.PARTIAL_SUCCESS
    assert record.pid == 456
    assert record.finished_at is not None
    assert calls[0][1]["shell"] is False
    assert "--confirm-live" not in calls[0][0]
    assert "--confirm-search-live" not in calls[0][0]
    assert command.log_path.read_text(encoding="utf-8") == "mock cli completed\n"


def test_worker_rejects_tampered_argv_before_runner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, _ = _submit(root)
    payload = json.loads(submission.record.request_path.read_text(encoding="utf-8"))
    payload["argv"].extend(["--env-file", "C:/outside/.env"])
    submission.record.request_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    called = False

    def runner(*args: Any, **kwargs: Any) -> int:
        nonlocal called
        called = True
        return 0

    with pytest.raises(UiServiceError, match="白名单"):
        run_worker(submission.record.request_path, runner=runner)
    assert called is False
    assert command.log_path.exists() is False


def test_worker_main_marks_rejected_manifest_failed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, _ = _submit(root, "tampered-worker-main")
    payload = json.loads(submission.record.request_path.read_text(encoding="utf-8"))
    payload["argv"].extend(["--env-file", "C:/outside/.env"])
    submission.record.request_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = worker_main(
        ["--task-file", str(submission.record.request_path)]
    )

    assert exit_code == 2
    record = load_ui_task(command.run_dir, project_root=root)
    assert record.status is UiTaskStatus.FAILED
    assert record.exit_code == 2
    assert record.error_message == "后台任务请求校验失败"


def test_worker_rebuilds_non_default_bound_source_ids(tmp_path: Path) -> None:
    root = _project(tmp_path)
    request = ResearchRequest(
        company="002603.SZ",
        task="分析股权结构和实际控制关系",
        mode=ResearchMode.OWNERSHIP,
        source_id="S02",
        comparison_source_id="S11",
    )
    command = build_mock_command(
        root,
        request,
        run_id="custom-source",
        python_executable="python-test",
    )
    calls: list[list[str]] = []

    def process_factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        return FakeProcess()

    submission = submit_research_task(
        command,
        process_factory=process_factory,
    )

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        command.artifact_dir.mkdir(parents=True)
        (command.artifact_dir / "ownership_summary.md").write_text(
            "# 股权研究", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0)

    assert run_worker(submission.record.request_path, runner=runner) == 0
    assert calls
    assert calls[0][calls[0].index("--agent-source") + 1] == "S02"


def test_failed_cli_with_saved_report_is_partial_success(tmp_path: Path) -> None:
    root = _project(tmp_path)
    command, submission, _ = _submit(root)

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        command.artifact_dir.mkdir(parents=True)
        (command.artifact_dir / "credit_research_report.md").write_text(
            "# 已完成的部分研究", encoding="utf-8"
        )
        return SimpleNamespace(returncode=13)

    assert run_worker(submission.record.request_path, runner=runner) == 13
    record = load_ui_task(command.run_dir, project_root=root)
    assert record.status is UiTaskStatus.PARTIAL_SUCCESS
    assert record.exit_code == 13
    assert record.error_message == "CLI 执行结束，退出码 13"


def test_discovery_skips_corrupt_task_and_sorts_valid_records(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _, first, _ = _submit(root, "first")
    _, second, _ = _submit(root, "second")
    broken = root / "data" / "streamlit_runs" / "broken"
    broken.mkdir()
    (broken / "ui_task_request.json").write_text("{broken", encoding="utf-8")

    records = discover_ui_tasks(root)

    assert {item.task_id for item in records} == {
        first.record.task_id,
        second.record.task_id,
    }


def test_task_paths_cannot_escape_run_directory(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _, submission, _ = _submit(root)
    payload = json.loads(submission.record.request_path.read_text(encoding="utf-8"))
    payload["artifact_dir"] = str(tmp_path / "outside")
    submission.record.request_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UiServiceError):
        run_worker(submission.record.request_path, runner=lambda *args, **kwargs: 0)
