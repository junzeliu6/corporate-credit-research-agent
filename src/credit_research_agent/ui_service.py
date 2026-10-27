"""V3.1 Streamlit 界面的安全执行适配层。

本模块不包含研究逻辑。它只把经过校验的界面输入转换为现有 CLI 的参数列表，
并以 ``shell=False`` 启动独立进程。这样 CLI 与 Agent Runtime 仍是唯一的研究
编排入口，Streamlit 页面也不会获得任意命令执行能力。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from dotenv import dotenv_values

from credit_research_agent.company import CompanyResolver
from credit_research_agent.security_master import SecurityMasterError, parse_a_share_code


# 网页 Live 需要容纳长篇报告生成；这是单次模型请求的等待上限，不是整项任务时限。
WEB_LIVE_REQUEST_TIMEOUT_SECONDS = 600
WEB_LIVE_MAX_MODEL_RETRIES = 0
WEB_COMPLETE_LIVE_BUDGET_ARGS = (
    "--max-agent-turns",
    "70",
    "--max-tool-calls",
    "60",
    "--max-search-rounds",
    "15",
    "--max-search-calls",
    "15",
    "--max-search-requests",
    "15",
    "--max-source-fetches",
    "20",
    "--max-extract-calls",
    "20",
)


class UiServiceError(ValueError):
    """界面输入或执行配置不满足安全约束。"""


class ResearchMode(str, Enum):
    """V3.1 可从界面启动的受控研究类型。"""

    COMPLETE = "complete"
    OWNERSHIP = "ownership"
    FINANCIAL = "financial"
    INDUSTRY = "industry"
    FUNDAMENTALS = "fundamentals"

    @property
    def uses_llm(self) -> bool:
        return self is not ResearchMode.FUNDAMENTALS

    @property
    def uses_search(self) -> bool:
        return self in {ResearchMode.COMPLETE, ResearchMode.INDUSTRY}


class UiExecutionMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"


class UiTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"

    @property
    def is_finished(self) -> bool:
        return self in {
            UiTaskStatus.SUCCEEDED,
            UiTaskStatus.PARTIAL_SUCCESS,
            UiTaskStatus.FAILED,
        }


@dataclass(frozen=True)
class ApiReadiness:
    """只暴露密钥是否已配置，绝不返回密钥内容。"""

    deepseek_configured: bool
    baidu_search_configured: bool


@dataclass(frozen=True)
class ResearchRequest:
    """来自 Streamlit 表单的最小、不可扩展请求。"""

    company: str
    task: str
    mode: ResearchMode
    confirm_deepseek_live: bool = False
    confirm_baidu_live: bool = False
    source_id: str = "S01"
    comparison_source_id: str = "S10"


@dataclass(frozen=True)
class UiRunCommand:
    """已校验且可直接交给 ``subprocess.Popen`` 的命令。"""

    argv: tuple[str, ...]
    project_root: Path
    run_dir: Path
    artifact_dir: Path
    progress_path: Path
    log_path: Path
    mode: ResearchMode
    execution_mode: UiExecutionMode
    company: str
    task: str
    source_id: str
    comparison_source_id: str


@dataclass(frozen=True)
class UiProgressEvent:
    """供页面轮询展示的脱敏进度事件。"""

    sequence: int
    stage: str
    message: str
    occurred_at: Optional[str]
    details: Mapping[str, Any]


@dataclass
class ResearchProcess:
    """后台 CLI 进程句柄及其不可变命令配置。"""

    process: Any
    command: UiRunCommand

    def poll(self) -> Optional[int]:
        return self.process.poll()


@dataclass(frozen=True)
class UiTaskRecord:
    """可跨 Streamlit 刷新恢复的持久化任务状态。"""

    task_id: str
    run_dir: Path
    artifact_dir: Path
    progress_path: Path
    log_path: Path
    request_path: Path
    status_path: Path
    company: str
    task: str
    mode: ResearchMode
    execution_mode: UiExecutionMode
    status: UiTaskStatus
    idempotency_key: Optional[str]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class TaskSubmission:
    record: UiTaskRecord
    process: Optional[Any]
    reused: bool = False


def load_api_readiness(
    project_root: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ApiReadiness:
    """读取环境或 ``.env``，但只返回布尔状态。

    环境变量优先于本地 ``.env``。空字符串视为未配置，返回对象中不存在任何
    密钥字段，因此可以安全地直接用于界面状态展示。
    """

    root = _validated_project_root(project_root)
    values: dict[str, Any] = dict(dotenv_values(root / ".env"))
    values.update(dict(os.environ if environ is None else environ))
    return ApiReadiness(
        deepseek_configured=bool(str(values.get("DEEPSEEK_API_KEY") or "").strip()),
        baidu_search_configured=bool(
            str(values.get("BAIDU_SEARCH_API_KEY") or "").strip()
        ),
    )


def build_research_command(
    project_root: Path,
    request: ResearchRequest,
    *,
    execution_mode: UiExecutionMode = UiExecutionMode.LIVE,
    run_id: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> UiRunCommand:
    """把表单请求转换为白名单 CLI 参数。

    调用者不能传入额外参数或任意输出路径。每次运行均固定写入项目内
    ``data/streamlit_runs`` 下的唯一目录，并继续接受 CLI/Runtime 的第二层 Live
    确认校验。
    """

    root = _validated_project_root(project_root)
    company_query = _clean_text(request.company, "企业名称或证券代码")
    task = _clean_text(request.task, "研究任务")
    if request.mode is ResearchMode.COMPLETE:
        try:
            _ticker, _exchange_code, entity_id = parse_a_share_code(company_query)
        except SecurityMasterError:
            # 首版动态接入以代码为主；保留已登记演示企业的名称入口。
            try:
                entity_id = CompanyResolver.from_default_catalog().resolve(
                    company_query
                ).entity_id
            except Exception as exc:
                raise UiServiceError(
                    "完整研究请输入6位A股代码；公司名称仅支持已登记的历史演示企业"
                ) from exc
    else:
        identity = CompanyResolver.from_default_catalog().resolve(company_query)
        entity_id = identity.entity_id
    _validate_source_id(request.source_id, "主来源")
    _validate_source_id(request.comparison_source_id, "比较来源")

    if execution_mode is UiExecutionMode.LIVE:
        if request.mode.uses_llm and not request.confirm_deepseek_live:
            raise UiServiceError("启动 DeepSeek Live 研究前必须显式勾选确认")
        if request.mode.uses_search and not request.confirm_baidu_live:
            raise UiServiceError("完整或行业研究还必须显式确认百度搜索 Live 调用")

    safe_run_id = _normalise_run_id(run_id or _new_run_id(request.mode))
    run_dir = (root / "data" / "streamlit_runs" / safe_run_id).resolve()
    allowed_root = (root / "data" / "streamlit_runs").resolve()
    _require_descendant(run_dir, allowed_root)
    artifact_dir = (run_dir / entity_id).resolve()
    progress_path = (run_dir / "ui_progress.jsonl").resolve()
    log_path = (run_dir / "cli_output.log").resolve()

    argv = [
        python_executable or sys.executable,
        "-m",
        "credit_research_agent.cli",
        company_query,
    ]
    if request.mode is ResearchMode.COMPLETE:
        argv.extend(
            [
                "--run-complete-research",
                task,
                "--auto-prepare",
                "--annual-report-count",
                "2",
                "--security-master-cache",
                str(root / "data" / "cache" / "security_master.json"),
                "--onboarding-dir",
                str(run_dir / "onboarding"),
                "--complete-dir",
                str(run_dir),
                "--complete-trace-output",
                str(artifact_dir / "credit_research_execution_trace.json"),
            ]
        )
    elif request.mode is ResearchMode.OWNERSHIP:
        argv.extend(
            [
                "--run-agent",
                task,
                "--agent-source",
                request.source_id,
                "--ownership-dir",
                str(run_dir),
                "--agent-trace-output",
                str(artifact_dir / "agent_execution_trace.json"),
                "--skill-trace-output",
                str(artifact_dir / "agent_skill_execution_trace.json"),
            ]
        )
    elif request.mode is ResearchMode.FINANCIAL:
        argv.extend(
            [
                "--run-agent",
                task,
                "--agent-source",
                request.source_id,
                "--financial-comparison-source",
                request.comparison_source_id,
                "--financial-dir",
                str(run_dir),
                "--agent-trace-output",
                str(artifact_dir / "agent_execution_trace.json"),
                "--skill-trace-output",
                str(artifact_dir / "agent_skill_execution_trace.json"),
            ]
        )
    elif request.mode is ResearchMode.INDUSTRY:
        argv.extend(
            [
                "--run-industry-research",
                task,
                "--industry-dir",
                str(run_dir),
                "--industry-trace-output",
                str(artifact_dir / "industry_execution_trace.json"),
            ]
        )
    elif request.mode is ResearchMode.FUNDAMENTALS:
        argv.extend(
            [
                "--build-fundamentals",
                request.source_id,
                "--fundamentals-dir",
                str(run_dir),
            ]
        )
    else:  # pragma: no cover - Enum 已封闭，此分支只用于防御未来改动
        raise UiServiceError(f"不支持的研究类型：{request.mode!r}")

    if request.mode.uses_llm:
        argv.extend(["--llm-mode", execution_mode.value])
        if execution_mode is UiExecutionMode.LIVE:
            argv.extend(
                [
                    "--confirm-live",
                    "--request-timeout-seconds",
                    str(WEB_LIVE_REQUEST_TIMEOUT_SECONDS),
                    "--max-model-retries",
                    str(WEB_LIVE_MAX_MODEL_RETRIES),
                ]
            )
    if request.mode.uses_search:
        argv.extend(["--search-mode", execution_mode.value])
        if execution_mode is UiExecutionMode.LIVE:
            argv.append("--confirm-search-live")
    if (
        execution_mode is UiExecutionMode.LIVE
        and request.mode is ResearchMode.COMPLETE
    ):
        argv.extend(WEB_COMPLETE_LIVE_BUDGET_ARGS)

    # CLI 新增的轻量进度输出只包含阶段、Tool 名称及计数，不记录密钥或正文。
    argv.extend(["--progress-output", str(progress_path)])
    return UiRunCommand(
        argv=tuple(argv),
        project_root=root,
        run_dir=run_dir,
        artifact_dir=artifact_dir,
        progress_path=progress_path,
        log_path=log_path,
        mode=request.mode,
        execution_mode=execution_mode,
        company=company_query,
        task=task,
        source_id=request.source_id,
        comparison_source_id=request.comparison_source_id,
    )


def build_live_command(
    project_root: Path,
    request: ResearchRequest,
    *,
    run_id: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> UiRunCommand:
    return build_research_command(
        project_root,
        request,
        execution_mode=UiExecutionMode.LIVE,
        run_id=run_id,
        python_executable=python_executable,
    )


def build_mock_command(
    project_root: Path,
    request: ResearchRequest,
    *,
    run_id: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> UiRunCommand:
    """构造永不携带 Live 确认参数的离线 Mock 命令。"""

    return build_research_command(
        project_root,
        request,
        execution_mode=UiExecutionMode.MOCK,
        run_id=run_id,
        python_executable=python_executable,
    )


def start_research_process(
    command: UiRunCommand,
    *,
    process_factory: Callable[..., Any] = subprocess.Popen,
    environ: Optional[Mapping[str, str]] = None,
) -> ResearchProcess:
    """以独立进程启动现有 CLI；永远不经过 Shell。"""

    command.run_dir.mkdir(parents=True, exist_ok=False)
    child_env = dict(os.environ if environ is None else environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    with command.log_path.open("w", encoding="utf-8", newline="\n") as log_file:
        process = process_factory(
            list(command.argv),
            cwd=str(command.project_root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=False,
            env=child_env,
        )
    return ResearchProcess(process=process, command=command)


def submit_research_task(
    command: UiRunCommand,
    *,
    idempotency_key: Optional[str] = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    environ: Optional[Mapping[str, str]] = None,
) -> TaskSubmission:
    """提交独立 Worker，并持久化任务身份以支持刷新后继续读取。

    Worker 仅负责监督既有 CLI 并记录状态；它不实现任何 Agent 业务逻辑。
    """

    key = _validate_idempotency_key(idempotency_key)
    if key is not None:
        for existing in discover_ui_tasks(command.project_root):
            if existing.idempotency_key == key:
                return TaskSubmission(record=existing, process=None, reused=True)

    command.run_dir.mkdir(parents=True, exist_ok=False)
    created_at = _now_iso()
    request_path = command.run_dir / "ui_task_request.json"
    status_path = command.run_dir / "ui_task_status.json"
    worker_log_path = command.run_dir / "ui_worker.log"
    request_payload = {
        "schema_version": "3.1.0",
        "task_id": command.run_dir.name,
        "project_root": str(command.project_root),
        "run_dir": str(command.run_dir),
        "artifact_dir": str(command.artifact_dir),
        "progress_path": str(command.progress_path),
        "log_path": str(command.log_path),
        "status_path": str(status_path),
        "company": command.company,
        "task": command.task,
        "mode": command.mode.value,
        "execution_mode": command.execution_mode.value,
        "source_id": command.source_id,
        "comparison_source_id": command.comparison_source_id,
        "idempotency_key": key,
        "created_at": created_at,
        "argv": list(command.argv),
    }
    _atomic_write_json(request_path, request_payload)
    _atomic_write_json(
        status_path,
        {
            "schema_version": "3.1.0",
            "task_id": command.run_dir.name,
            "status": UiTaskStatus.QUEUED.value,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "exit_code": None,
            "error_message": None,
        },
    )
    live_lock_acquired = False
    if command.execution_mode is UiExecutionMode.LIVE:
        try:
            _acquire_active_live_lock(command.project_root, command.run_dir.name)
            live_lock_acquired = True
        except UiServiceError:
            _atomic_write_json(
                status_path,
                {
                    "schema_version": "3.1.0",
                    "task_id": command.run_dir.name,
                    "status": UiTaskStatus.FAILED.value,
                    "created_at": created_at,
                    "started_at": None,
                    "finished_at": _now_iso(),
                    "pid": None,
                    "exit_code": None,
                    "error_message": "已有另一个 Live 研究任务处于活动状态",
                },
            )
            raise
    child_env = dict(os.environ if environ is None else environ)
    child_env["PYTHONUNBUFFERED"] = "1"
    worker_argv = [
        command.argv[0],
        "-m",
        "credit_research_agent.ui_worker",
        "--task-file",
        str(request_path),
    ]
    try:
        with worker_log_path.open("w", encoding="utf-8", newline="\n") as log_file:
            process = process_factory(
                worker_argv,
                cwd=str(command.project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                env=child_env,
            )
    except Exception as exc:
        # 进程尚未创建时也必须结束 queued 状态，否则页面刷新后会把一个
        # 永远不会执行的任务误判为“正在运行”，并持续锁住后续提交。
        _atomic_write_json(
            status_path,
            {
                "schema_version": "3.1.0",
                "task_id": command.run_dir.name,
                "status": UiTaskStatus.FAILED.value,
                "created_at": created_at,
                "started_at": None,
                "finished_at": _now_iso(),
                "pid": None,
                "exit_code": None,
                "error_message": f"无法启动后台 Worker：{type(exc).__name__}",
            },
        )
        if live_lock_acquired:
            _release_active_live_lock(command.project_root, command.run_dir.name)
        raise UiServiceError("无法启动后台研究任务") from exc
    return TaskSubmission(
        record=load_ui_task(command.run_dir, project_root=command.project_root),
        process=process,
        reused=False,
    )


def load_ui_task(run_dir: Path, *, project_root: Path) -> UiTaskRecord:
    root = _validated_project_root(project_root)
    allowed_root = (root / "data" / "streamlit_runs").resolve()
    resolved = Path(run_dir).resolve()
    _require_descendant(resolved, allowed_root)
    request_path = resolved / "ui_task_request.json"
    status_path = resolved / "ui_task_status.json"
    request_payload = _read_json_object(request_path)
    status_payload = _read_json_object(status_path)
    try:
        return UiTaskRecord(
            task_id=str(request_payload["task_id"]),
            run_dir=resolved,
            artifact_dir=_safe_task_path(request_payload["artifact_dir"], resolved),
            progress_path=_safe_task_path(request_payload["progress_path"], resolved),
            log_path=_safe_task_path(request_payload["log_path"], resolved),
            request_path=request_path.resolve(),
            status_path=status_path.resolve(),
            company=str(request_payload["company"]),
            task=str(request_payload["task"]),
            mode=ResearchMode(str(request_payload["mode"])),
            execution_mode=UiExecutionMode(str(request_payload["execution_mode"])),
            status=UiTaskStatus(str(status_payload["status"])),
            idempotency_key=(
                str(request_payload["idempotency_key"])
                if request_payload.get("idempotency_key")
                else None
            ),
            created_at=str(status_payload.get("created_at") or request_payload["created_at"]),
            started_at=_optional_string(status_payload.get("started_at")),
            finished_at=_optional_string(status_payload.get("finished_at")),
            pid=_optional_int(status_payload.get("pid")),
            exit_code=_optional_int(status_payload.get("exit_code")),
            error_message=_optional_string(status_payload.get("error_message")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UiServiceError(f"任务状态文件格式无效：{resolved.name}") from exc


def discover_ui_tasks(project_root: Path) -> list[UiTaskRecord]:
    root = _validated_project_root(project_root)
    task_root = root / "data" / "streamlit_runs"
    if not task_root.is_dir():
        return []
    records: list[UiTaskRecord] = []
    for child in task_root.iterdir():
        if not child.is_dir():
            continue
        try:
            records.append(load_ui_task(child, project_root=root))
        except (UiServiceError, OSError):
            continue
    return sorted(records, key=lambda item: item.created_at, reverse=True)


def reconcile_interrupted_task(
    record: UiTaskRecord,
    *,
    now: Optional[datetime] = None,
    process_alive: Optional[Callable[[int], bool]] = None,
    queued_grace_seconds: int = 60,
) -> UiTaskRecord:
    """把电脑关机或 Worker 崩溃留下的活动状态安全收敛为失败。

    正在运行且 PID 仍存活的任务不会被修改；刚提交的 queued 任务也保留一段
    启动宽限期。该函数只改写任务自身目录中的状态文件，不触碰研究产物。
    """

    if record.status.is_finished:
        if record.execution_mode is UiExecutionMode.LIVE:
            _release_active_live_lock(record.run_dir.parents[2], record.task_id)
        return record
    if queued_grace_seconds < 1:
        raise UiServiceError("queued_grace_seconds 必须大于0")
    current = now or datetime.now(timezone.utc)
    timestamp = _parse_iso_datetime(record.started_at or record.created_at)
    elapsed = max(0.0, (current - timestamp).total_seconds())
    checker = process_alive or _process_is_alive

    if record.status is UiTaskStatus.RUNNING and record.pid is not None:
        if checker(record.pid):
            return record
        reason = "后台 Worker 已意外中断"
    elif elapsed < queued_grace_seconds:
        return record
    else:
        reason = "后台 Worker 未能启动或已意外中断"

    _atomic_write_json(
        record.status_path,
        {
            "schema_version": "3.1.0",
            "task_id": record.task_id,
            "status": UiTaskStatus.FAILED.value,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": current.isoformat(),
            "pid": record.pid,
            "exit_code": record.exit_code,
            "error_message": reason,
        },
    )
    if record.execution_mode is UiExecutionMode.LIVE:
        _release_active_live_lock(record.run_dir.parents[2], record.task_id)
    return load_ui_task(record.run_dir, project_root=record.run_dir.parents[2])


def read_progress_events(path: Path, *, max_events: int = 200) -> list[UiProgressEvent]:
    """容错读取追加式 JSONL；忽略写到一半或格式损坏的行。"""

    if max_events < 1:
        raise UiServiceError("max_events 必须大于0")
    progress_path = Path(path)
    if not progress_path.is_file():
        return []
    events: list[UiProgressEvent] = []
    try:
        lines = progress_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    for raw_line in lines[-max_events:]:
        try:
            item = json.loads(raw_line)
            if not isinstance(item, dict):
                continue
            sequence = int(item.get("sequence", len(events) + 1))
            stage = str(item["stage"])
            message = str(item.get("message") or stage)
            details = item.get("details")
            if not isinstance(details, dict):
                details = {}
            events.append(
                UiProgressEvent(
                    sequence=sequence,
                    stage=stage,
                    message=message,
                    occurred_at=(
                        str(item["occurred_at"]) if item.get("occurred_at") else None
                    ),
                    details=details,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return events


def read_cli_log_tail(path: Path, *, max_chars: int = 4000) -> str:
    """读取有限长度的 CLI 日志尾部，防止界面载入超大输出。"""

    if max_chars < 1:
        raise UiServiceError("max_chars 必须大于0")
    log_path = Path(path)
    if not log_path.is_file():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _validated_project_root(project_root: Path) -> Path:
    root = Path(project_root).resolve()
    if not (root / "pyproject.toml").is_file():
        raise UiServiceError(f"不是有效的项目根目录：{root}")
    return root


def _clean_text(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise UiServiceError(f"{label}不能为空")
    if "\x00" in cleaned:
        raise UiServiceError(f"{label}包含非法空字符")
    return cleaned


def _validate_source_id(value: str, label: str) -> None:
    if len(value) != 3 or not value.startswith("S") or not value[1:].isdigit():
        raise UiServiceError(f"{label}编号必须采用 S01 形式")


def _new_run_id(mode: ResearchMode) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{mode.value}_{uuid4().hex[:8]}"


def _normalise_run_id(value: str) -> str:
    cleaned = str(value).strip()
    if not cleaned or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in cleaned):
        raise UiServiceError("run_id 只能包含字母、数字、连字符和下划线")
    return cleaned


def _validate_idempotency_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or len(cleaned) > 128:
        raise UiServiceError("idempotency_key 长度必须为1—128")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    if any(char not in allowed for char in cleaned):
        raise UiServiceError("idempotency_key 只能包含字母、数字、点、连字符和下划线")
    return cleaned


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise UiServiceError("任务时间戳格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _process_is_alive(
    pid: int,
    *,
    platform_name: Optional[str] = None,
    windows_probe: Optional[Callable[[int], bool]] = None,
) -> bool:
    if pid <= 0:
        return False
    if (platform_name or sys.platform) == "win32":
        # Windows 的 os.kill(pid, 0) 不是 POSIX 式无害探测；Python 会把
        # 非 CTRL 信号交给 TerminateProcess，可能终止正在运行的付费任务。
        return (windows_probe or _windows_process_is_alive)(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        # 无权探测通常意味着目标进程存在。
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    """通过只读进程句柄判断 Windows PID 是否仍在运行。"""

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        # 访问被拒绝说明PID很可能存在；宁可继续显示运行中，也不误判后启动
        # 第二个Live任务。
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _active_live_lock_path(project_root: Path) -> Path:
    return (
        _validated_project_root(project_root)
        / "data"
        / "streamlit_runs"
        / ".active_live_task.lock"
    )


def _acquire_active_live_lock(project_root: Path, task_id: str) -> None:
    """原子占用本机唯一Live槽，防止两个Streamlit会话重复计费。"""

    lock_path = _active_live_lock_path(project_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"task_id": task_id, "created_at": _now_iso()}, ensure_ascii=False
    )
    descriptor: Optional[int] = None
    for attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            break
        except FileExistsError as exc:
            if attempt == 0 and _clear_stale_live_lock(
                project_root, exclude_task_id=task_id
            ):
                continue
            raise UiServiceError("已有另一个 Live 研究任务处于活动状态") from exc
    if descriptor is None:  # pragma: no cover - 上述循环必定创建或抛错
        raise UiServiceError("无法创建 Live 任务锁")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise


def _release_active_live_lock(project_root: Path, task_id: str) -> None:
    """仅由锁中记录的同一任务释放Live槽。"""

    lock_path = _active_live_lock_path(project_root)
    try:
        payload = _read_json_object(lock_path)
        if str(payload.get("task_id") or "") != task_id:
            return
        lock_path.unlink(missing_ok=True)
    except UiServiceError:
        _clear_stale_live_lock(project_root, exclude_task_id=task_id)
    except OSError:
        return


def _clear_stale_live_lock(
    project_root: Path,
    *,
    exclude_task_id: Optional[str] = None,
) -> bool:
    """仅在没有其他活动Live任务时清理完成或损坏的遗留锁。"""

    root = _validated_project_root(project_root)
    lock_path = _active_live_lock_path(root)
    if not lock_path.exists():
        return True
    try:
        payload = _read_json_object(lock_path)
        owner_task_id = str(payload.get("task_id") or "")
    except UiServiceError:
        owner_task_id = ""

    active_live_tasks = [
        record
        for record in discover_ui_tasks(root)
        if record.execution_mode is UiExecutionMode.LIVE
        and not record.status.is_finished
        and record.task_id != exclude_task_id
    ]
    if active_live_tasks:
        return False
    if owner_task_id and owner_task_id != exclude_task_id:
        owner_dir = root / "data" / "streamlit_runs" / owner_task_id
        try:
            owner = load_ui_task(owner_dir, project_root=root)
        except (OSError, UiServiceError):
            owner = None
        if owner is not None and not owner.status.is_finished:
            return False
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path.resolve()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UiServiceError(f"无法读取任务状态文件：{Path(path).name}") from exc
    if not isinstance(payload, dict):
        raise UiServiceError(f"任务状态文件顶层必须为对象：{Path(path).name}")
    return payload


def _safe_task_path(value: Any, run_dir: Path) -> Path:
    path = Path(str(value)).resolve()
    _require_descendant(path, run_dir)
    return path


def _optional_string(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _optional_int(value: Any) -> Optional[int]:
    return int(value) if value not in (None, "") else None


def _require_descendant(path: Path, parent: Path) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise UiServiceError("运行目录必须位于项目 data/streamlit_runs 内") from exc
