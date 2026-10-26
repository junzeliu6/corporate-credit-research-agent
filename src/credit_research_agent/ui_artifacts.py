"""V3 Streamlit 界面使用的研究产物适配层。

本模块不依赖 Streamlit，也不触发 Agent、网络或模型调用。它在受控目录中
发现 V2.5 产物、整理展示结构，并可把已确认的 Markdown 本地转换为 Word。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from credit_research_agent.word_export import (
    export_markdown_report,
    inspect_markdown_images,
)


MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_WORD_BYTES = 64 * 1024 * 1024


class ArtifactAccessError(ValueError):
    """产物路径不在允许范围内。"""


class ReportDisplayStatus(str, Enum):
    """面向产品界面的简化运行状态。"""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FinancialTables:
    """已计算财务数据的展示行；不在 UI 层重新计算任何指标。"""

    metric_rows: list[dict[str, Any]] = field(default_factory=list)
    ratio_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ResearchRunArtifact:
    """一次完整研究运行可供界面展示的只读快照。"""

    run_dir: Path
    entity_id: str
    company_name: str
    ticker: str
    research_task: str
    original_status: str
    display_status: ReportDisplayStatus
    status_message: str
    mode: str
    is_live: bool
    recovered: bool
    report_path: Optional[Path]
    word_report_path: Optional[Path]
    ownership_image_path: Optional[Path]
    financial_metrics_path: Optional[Path]
    delivery_path: Optional[Path]
    trace_path: Optional[Path]
    updated_at: datetime
    warnings: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        mode_label = "Live" if self.is_live else "Mock"
        company = self.company_name or self.ticker or self.entity_id
        return f"{company} · {mode_label} · {self.status_message}"


def default_artifact_roots(project_root: Path) -> tuple[Path, ...]:
    """返回界面允许扫描的固定根目录，不接受用户传入任意文件路径。"""

    project_root = Path(project_root).resolve()
    return (
        (project_root / "data").resolve(),
        (project_root / "examples" / "v2_5_complete_mock").resolve(),
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_run_dir(run_dir: Path, allowed_roots: Iterable[Path]) -> Path:
    resolved = Path(run_dir).resolve()
    roots = [Path(root).resolve() for root in allowed_roots]
    if not roots or not any(_is_relative_to(resolved, root) for root in roots):
        raise ArtifactAccessError(f"研究产物目录不在允许范围内：{resolved}")
    if not resolved.is_dir():
        raise ArtifactAccessError(f"研究产物目录不存在：{resolved}")
    return resolved


def _safe_child(run_dir: Path, relative: str) -> Optional[Path]:
    candidate = (run_dir / relative).resolve()
    if not _is_relative_to(candidate, run_dir):
        return None
    return candidate if candidate.is_file() else None


def _read_json(path: Optional[Path], warnings: list[str]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            warnings.append(f"文件过大，未加载：{path.name}")
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            warnings.append(f"JSON 顶层不是对象，未加载：{path.name}")
            return {}
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"无法读取 {path.name}：{exc}")
        return {}


def _status_view(
    original_status: str,
    *,
    recovered: bool,
    has_report: bool,
) -> tuple[ReportDisplayStatus, str]:
    if recovered and has_report:
        return ReportDisplayStatus.PARTIAL_SUCCESS, "已离线恢复，需复核"
    normalized = original_status.strip().lower()
    if normalized in {"success", "succeeded", "completed"}:
        return ReportDisplayStatus.SUCCESS, "研究完成"
    if normalized in {"partial_success", "partial", "degraded"}:
        return ReportDisplayStatus.PARTIAL_SUCCESS, "部分完成"
    if normalized in {"failed", "failure", "error"}:
        return ReportDisplayStatus.FAILED, "研究失败"
    if has_report:
        return ReportDisplayStatus.PARTIAL_SUCCESS, "存在报告，状态待确认"
    return ReportDisplayStatus.UNKNOWN, "状态未知"


def _find_financial_metrics(run_dir: Path) -> Optional[Path]:
    preferred = sorted(
        (run_dir / "modules" / "financial").glob("*/financial_metrics.json")
    )
    if preferred:
        return preferred[0].resolve()
    direct = _safe_child(run_dir, "financial_metrics.json")
    return direct


def load_research_run(
    run_dir: Path,
    *,
    allowed_roots: Iterable[Path],
) -> ResearchRunArtifact:
    """加载一个运行目录；所有实际文件均使用受控的本地固定文件名。"""

    resolved = _validated_run_dir(run_dir, allowed_roots)
    warnings: list[str] = []
    delivery_path = _safe_child(resolved, "credit_research_delivery.json")
    context_path = _safe_child(resolved, "credit_research_context.json")
    trace_path = _safe_child(resolved, "credit_research_execution_trace.json")
    recovery_path = _safe_child(resolved, "credit_research_recovery.json")
    delivery = _read_json(delivery_path, warnings)
    context = _read_json(context_path, warnings)
    trace = _read_json(trace_path, warnings)
    recovery = _read_json(recovery_path, warnings)

    recovered_report = _safe_child(resolved, "credit_research_report_recovered.md")
    standard_report = _safe_child(resolved, "credit_research_report.md")
    recovered = bool(
        recovered_report
        and str(recovery.get("status", "")).lower() == "recovered_offline"
    )
    report_path = recovered_report if recovered else standard_report
    word_report_path = (
        _safe_child(resolved, report_path.with_suffix(".docx").name)
        if report_path is not None
        else None
    )

    original_status = str(
        delivery.get("status")
        or recovery.get("original_run_status")
        or trace.get("status")
        or "unknown"
    )
    display_status, status_message = _status_view(
        original_status,
        recovered=recovered,
        has_report=report_path is not None,
    )

    company = context.get("company") if isinstance(context.get("company"), Mapping) else {}
    assert isinstance(company, Mapping)
    entity_id = str(trace.get("entity_id") or resolved.name)
    company_name = str(
        company.get("legal_name") or company.get("short_name") or ""
    )
    ticker = str(company.get("ticker") or "")
    research_task = str(context.get("research_task") or trace.get("task") or "")
    mode = str(delivery.get("mode") or trace.get("llm_config", {}).get("mode") or "unknown")
    is_live = mode.lower() == "live" or delivery.get("authorship") == "live_llm"

    png_path = _safe_child(resolved, "assets/ownership_structure.png")
    svg_path = _safe_child(resolved, "assets/ownership_structure.svg")
    ownership_image_path = png_path or svg_path
    financial_metrics_path = _find_financial_metrics(resolved)

    timestamp_candidates = [
        path.stat().st_mtime
        for path in (report_path, delivery_path, trace_path)
        if path is not None
    ]
    updated_timestamp = max(timestamp_candidates, default=resolved.stat().st_mtime)
    updated_at = datetime.fromtimestamp(updated_timestamp, tz=timezone.utc)

    return ResearchRunArtifact(
        run_dir=resolved,
        entity_id=entity_id,
        company_name=company_name,
        ticker=ticker,
        research_task=research_task,
        original_status=original_status,
        display_status=display_status,
        status_message=status_message,
        mode=mode,
        is_live=is_live,
        recovered=recovered,
        report_path=report_path,
        word_report_path=word_report_path,
        ownership_image_path=ownership_image_path,
        financial_metrics_path=financial_metrics_path,
        delivery_path=delivery_path,
        trace_path=trace_path,
        updated_at=updated_at,
        warnings=tuple(warnings),
    )


def _candidate_run_dirs(root: Path, *, project_data_root: Optional[Path]) -> set[Path]:
    if not root.is_dir():
        return set()
    candidates: set[Path] = set()
    if project_data_root is not None and root == project_data_root:
        # data 下只扫描完整研究或 Streamlit 创建的受控目录，跳过测试临时目录。
        parents = [
            child
            for child in root.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (
                child.name.startswith("complete_research")
                or child.name.startswith("streamlit_runs")
            )
        ]
    else:
        parents = [root]
    for parent in parents:
        for name in (
            "credit_research_delivery.json",
            "credit_research_recovery.json",
            "credit_research_report.md",
            "credit_research_report_recovered.md",
        ):
            candidates.update(path.parent.resolve() for path in parent.rglob(name))
    return candidates


def discover_research_runs(
    project_root: Path,
    *,
    roots: Optional[Iterable[Path]] = None,
) -> list[ResearchRunArtifact]:
    """发现可展示的完整研究产物，单个损坏目录不会阻断其余结果。"""

    project_root = Path(project_root).resolve()
    allowed_roots = tuple(Path(item).resolve() for item in (roots or default_artifact_roots(project_root)))
    data_root = (project_root / "data").resolve()
    candidates: set[Path] = set()
    for root in allowed_roots:
        candidates.update(
            _candidate_run_dirs(
                root,
                project_data_root=data_root if root == data_root else None,
            )
        )

    runs: list[ResearchRunArtifact] = []
    for candidate in candidates:
        try:
            runs.append(load_research_run(candidate, allowed_roots=allowed_roots))
        except (ArtifactAccessError, OSError):
            continue
    return sorted(runs, key=lambda item: item.updated_at, reverse=True)


def read_report_markdown(artifact: ResearchRunArtifact) -> str:
    """安全读取报告文本；本地图片由 Streamlit 使用独立组件展示。"""

    if artifact.report_path is None:
        return ""
    path = artifact.report_path.resolve()
    if not _is_relative_to(path, artifact.run_dir):
        raise ArtifactAccessError("报告文件超出本次运行目录")
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ArtifactAccessError("报告文件过大，拒绝在界面中直接加载")
    return path.read_text(encoding="utf-8")


def ensure_word_report(artifact: ResearchRunArtifact) -> Path:
    """在当前研究目录内生成或复用 Word，不触发 Agent 或外部 API。

    输出文件名跟随实际展示的 Markdown（恢复版与原始版分别保存），并在
    Markdown 或股权图更新后重新导出。所有输入、输出都必须属于本次运行目录。
    """

    if artifact.report_path is None:
        raise ArtifactAccessError("该运行尚未生成可导出的 Markdown 报告")
    run_dir = artifact.run_dir.resolve()
    report_path = artifact.report_path.resolve()
    if not _is_relative_to(report_path, run_dir) or not report_path.is_file():
        raise ArtifactAccessError("报告文件超出本次运行目录或已不存在")

    image_path = artifact.ownership_image_path
    if image_path is not None:
        image_path = image_path.resolve()
        if not _is_relative_to(image_path, run_dir) or not image_path.is_file():
            raise ArtifactAccessError("股权结构图超出本次运行目录或已不存在")

    output_path = report_path.with_suffix(".docx").resolve()
    if not _is_relative_to(output_path, run_dir):
        raise ArtifactAccessError("Word 输出路径超出本次运行目录")

    image_inspection = inspect_markdown_images(
        report_path,
        allowed_root=run_dir,
        ownership_image_path=image_path,
    )
    dependencies = [report_path, *image_inspection.image_paths]
    newest_input = max(item.stat().st_mtime_ns for item in dependencies)
    if (
        output_path.is_file()
        and output_path.stat().st_mtime_ns >= newest_input
        and output_path.stat().st_size > 0
        and (
            not image_inspection.requires_ownership_image
            or image_inspection.ownership_image_path is not None
        )
    ):
        return output_path

    result = export_markdown_report(
        report_path,
        output_path,
        allowed_root=run_dir,
        ownership_image_path=image_path,
        company_name=artifact.company_name or None,
        ticker=artifact.ticker or None,
        report_date=artifact.updated_at.date(),
        require_ownership_image=image_inspection.requires_ownership_image,
    )
    exported = result.docx_path.resolve()
    if exported != output_path or not _is_relative_to(exported, run_dir):
        raise ArtifactAccessError("Word 导出器返回了非预期输出路径")
    if not exported.is_file() or exported.stat().st_size <= 0:
        raise ArtifactAccessError("Word 导出未生成有效文件")
    return exported


def read_word_report(artifact: ResearchRunArtifact) -> tuple[Path, bytes]:
    """返回安全的 Word 下载载荷；只读取当前研究目录内的导出文件。"""

    path = ensure_word_report(artifact).resolve()
    if not _is_relative_to(path, artifact.run_dir.resolve()):
        raise ArtifactAccessError("Word 文件超出本次运行目录")
    size = path.stat().st_size
    if size <= 0 or size > MAX_WORD_BYTES:
        raise ArtifactAccessError("Word 文件为空或超过界面下载大小限制")
    return path, path.read_bytes()


def load_financial_tables(artifact: ResearchRunArtifact) -> FinancialTables:
    """读取 Python 已计算的财务指标，转换为中文展示列。"""

    path = artifact.financial_metrics_path
    if path is None:
        return FinancialTables()
    resolved = path.resolve()
    if not _is_relative_to(resolved, artifact.run_dir):
        raise ArtifactAccessError("财务数据文件超出本次运行目录")
    warnings: list[str] = []
    payload = _read_json(resolved, warnings)
    if not payload:
        return FinancialTables()

    metric_rows: list[dict[str, Any]] = []
    metrics = payload.get("metrics", [])
    if isinstance(metrics, list):
        for item in metrics:
            if not isinstance(item, Mapping):
                continue
            metric_rows.append(
                {
                    "科目": item.get("item_label") or item.get("item") or "",
                    "上期": item.get("prior_period") or "",
                    "本期": item.get("current_period") or "",
                    "上期金额": item.get("prior_value"),
                    "本期金额": item.get("current_value"),
                    "变动金额": item.get("absolute_change"),
                    "同比(%)": item.get("yoy_percent"),
                    "重点分析": bool(item.get("requires_deep_analysis", False)),
                }
            )

    ratio_rows: list[dict[str, Any]] = []
    ratios = payload.get("ratios", [])
    if isinstance(ratios, list):
        for item in ratios:
            if not isinstance(item, Mapping):
                continue
            ratio_rows.append(
                {
                    "指标": item.get("name") or "",
                    "期间": item.get("period") or "",
                    "数值(%)": item.get("value_percent"),
                    "计算公式": item.get("formula") or "",
                }
            )
    return FinancialTables(metric_rows=metric_rows, ratio_rows=ratio_rows)
