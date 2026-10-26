"""V3.1 Streamlit 后台任务监督器。

Worker 不实现研究逻辑，只在独立进程中执行由 ui_service 构造并再次校验的
CLI argv，并把生命周期状态原子写入本地任务清单。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from credit_research_agent.ui_service import (
    ResearchMode,
    ResearchRequest,
    UiExecutionMode,
    UiServiceError,
    UiTaskStatus,
    _atomic_write_json,
    _read_json_object,
    _release_active_live_lock,
    _require_descendant,
    _validated_project_root,
    build_research_command,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_task_spec(task_file: Path) -> tuple[dict[str, Any], Path]:
    task_file = Path(task_file).resolve()
    payload = _read_json_object(task_file)
    try:
        project_root = _validated_project_root(Path(str(payload["project_root"])))
        run_dir = Path(str(payload["run_dir"])).resolve()
        task_id = str(payload["task_id"])
        execution_mode = UiExecutionMode(str(payload["execution_mode"]))
        request = ResearchRequest(
            company=str(payload["company"]),
            task=str(payload["task"]),
            mode=ResearchMode(str(payload["mode"])),
            confirm_deepseek_live=execution_mode is UiExecutionMode.LIVE,
            confirm_baidu_live=execution_mode is UiExecutionMode.LIVE,
            source_id=str(payload["source_id"]),
            comparison_source_id=str(payload["comparison_source_id"]),
        )
        argv = tuple(str(item) for item in payload["argv"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UiServiceError("后台任务请求格式无效") from exc

    allowed_root = (project_root / "data" / "streamlit_runs").resolve()
    _require_descendant(task_file, allowed_root)
    _require_descendant(run_dir, allowed_root)
    if task_file.parent != run_dir or run_dir.name != task_id:
        raise UiServiceError("任务文件、目录和 task_id 不一致")
    if len(argv) < 6 or argv[1:3] != ("-m", "credit_research_agent.cli"):
        raise UiServiceError("Worker 只允许调用 credit_research_agent.cli")

    expected = build_research_command(
        project_root,
        request,
        execution_mode=execution_mode,
        run_id=task_id,
        python_executable=argv[0],
    )
    if argv != expected.argv:
        raise UiServiceError("任务命令与白名单构造结果不一致，拒绝执行")
    for field_name, expected_path in (
        ("artifact_dir", expected.artifact_dir),
        ("progress_path", expected.progress_path),
        ("log_path", expected.log_path),
        ("status_path", run_dir / "ui_task_status.json"),
    ):
        actual = Path(str(payload[field_name])).resolve()
        if actual != expected_path.resolve():
            raise UiServiceError(f"任务路径字段不匹配：{field_name}")
        _require_descendant(actual, run_dir)
    return payload, project_root


def _delivery_status(artifact_dir: Path) -> Optional[UiTaskStatus]:
    candidates = [
        artifact_dir / "credit_research_delivery.json",
        artifact_dir / "financial_llm_delivery.json",
        artifact_dir / "industry_delivery.json",
    ]
    candidates.extend(artifact_dir.glob("*delivery*.json"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        status = str(payload.get("status") or "").lower()
        if status in {"success", "succeeded", "completed"}:
            return UiTaskStatus.SUCCEEDED
        if status in {"partial", "partial_success", "degraded", "recovered_offline"}:
            return UiTaskStatus.PARTIAL_SUCCESS
        if status in {"failed", "failure", "error"}:
            return UiTaskStatus.FAILED
    return None


def _has_research_artifacts(artifact_dir: Path) -> bool:
    if not artifact_dir.is_dir():
        return False
    meaningful_names = {
        "credit_research_report.md",
        "ownership_summary.md",
        "financial_analysis.md",
        "industry_analysis.md",
        "fundamentals.json",
        "ownership_structure.svg",
        "ownership_structure.png",
    }
    return any(
        path.name in meaningful_names
        for path in artifact_dir.rglob("*")
        if path.is_file()
    )


def _final_status(exit_code: int, artifact_dir: Path) -> UiTaskStatus:
    delivered = _delivery_status(artifact_dir)
    if delivered is not None:
        return delivered
    if exit_code == 0:
        return UiTaskStatus.SUCCEEDED
    if _has_research_artifacts(artifact_dir):
        return UiTaskStatus.PARTIAL_SUCCESS
    return UiTaskStatus.FAILED


def run_worker(
    task_file: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    environ: Optional[Mapping[str, str]] = None,
    pid: Optional[int] = None,
) -> int:
    """执行一个经白名单重建核对的任务，返回 CLI 退出码。"""

    payload, project_root = _validated_task_spec(Path(task_file))
    status_path = Path(str(payload["status_path"])).resolve()
    artifact_dir = Path(str(payload["artifact_dir"])).resolve()
    log_path = Path(str(payload["log_path"])).resolve()
    base = {
        "schema_version": "3.1.0",
        "task_id": str(payload["task_id"]),
        "created_at": str(payload["created_at"]),
    }
    started_at = _now_iso()
    worker_pid = int(os.getpid() if pid is None else pid)
    _atomic_write_json(
        status_path,
        {
            **base,
            "status": UiTaskStatus.RUNNING.value,
            "started_at": started_at,
            "finished_at": None,
            "pid": worker_pid,
            "exit_code": None,
            "error_message": None,
        },
    )
    child_env = dict(os.environ if environ is None else environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_file:
            completed = runner(
                list(payload["argv"]),
                cwd=str(project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                env=child_env,
            )
        exit_code = int(getattr(completed, "returncode", completed))
        status = _final_status(exit_code, artifact_dir)
        error_message = (
            None if exit_code == 0 else f"CLI 执行结束，退出码 {exit_code}"
        )
    except Exception as exc:
        exit_code = 1
        status = (
            UiTaskStatus.PARTIAL_SUCCESS
            if _has_research_artifacts(artifact_dir)
            else UiTaskStatus.FAILED
        )
        error_message = f"Worker 无法完成 CLI 调用：{type(exc).__name__}"
    try:
        _atomic_write_json(
            status_path,
            {
                **base,
                "status": status.value,
                "started_at": started_at,
                "finished_at": _now_iso(),
                "pid": worker_pid,
                "exit_code": exit_code,
                "error_message": error_message,
            },
        )
    finally:
        if str(payload.get("execution_mode")) == "live":
            _release_active_live_lock(project_root, str(payload["task_id"]))
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V3.1 Streamlit 后台研究任务 Worker")
    parser.add_argument("--task-file", required=True)
    return parser


def _mark_rejected_task_failed(task_file: Path) -> None:
    """尽力结束合法任务目录中的残留 queued 状态。

    这里不信任请求 JSON 中的任何路径，只允许修改可从实际文件位置反推出的
    ``<project>/data/streamlit_runs/<task_id>/ui_task_status.json``。
    """

    resolved = Path(task_file).resolve()
    if resolved.name != "ui_task_request.json":
        return
    project_root = next(
        (parent for parent in resolved.parents if (parent / "pyproject.toml").is_file()),
        None,
    )
    if project_root is None:
        return
    allowed_root = (project_root / "data" / "streamlit_runs").resolve()
    if resolved.parent.parent != allowed_root:
        return
    status_path = resolved.parent / "ui_task_status.json"
    try:
        previous = _read_json_object(status_path)
        created_at = str(previous.get("created_at") or _now_iso())
        _atomic_write_json(
            status_path,
            {
                "schema_version": "3.1.0",
                "task_id": resolved.parent.name,
                "created_at": created_at,
                "status": UiTaskStatus.FAILED.value,
                "started_at": previous.get("started_at"),
                "finished_at": _now_iso(),
                "pid": previous.get("pid"),
                "exit_code": 2,
                "error_message": "后台任务请求校验失败",
            },
        )
        _release_active_live_lock(project_root, resolved.parent.name)
    except (OSError, UiServiceError):
        # 失败状态本身也无法安全写入时，只能由 stderr 保留诊断。
        return


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(Path(args.task_file))
    except UiServiceError as exc:
        _mark_rejected_task_failed(Path(args.task_file))
        print(f"任务请求被拒绝：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
