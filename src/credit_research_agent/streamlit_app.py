"""企业授信研究 Agent 的 V3.1 本地 Streamlit 界面。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from credit_research_agent.company import CompanyResolutionError, CompanyResolver
from credit_research_agent.ui_artifacts import (
    ArtifactAccessError,
    ReportDisplayStatus,
    ResearchRunArtifact,
    default_artifact_roots,
    discover_research_runs,
    load_financial_tables,
    load_research_run,
    read_report_markdown,
    read_word_report,
)
from credit_research_agent.ui_service import (
    ResearchMode,
    ResearchRequest,
    UiServiceError,
    UiTaskRecord,
    UiTaskStatus,
    build_live_command,
    discover_ui_tasks,
    load_ui_task,
    load_api_readiness,
    read_cli_log_tail,
    read_progress_events,
    reconcile_interrupted_task,
    submit_research_task,
)
from credit_research_agent.word_export import WordExportError


DEFAULT_TASK = (
    "请研究以岭药业的企业背景、股权结构、经营情况、行业政策及财务状况，"
    "并从银行授信角度撰写完整研究报告。"
)
HISTORY_SELECTION_KEY = "v32_selected_history_run"
MODE_TASKS = {
    ResearchMode.COMPLETE: DEFAULT_TASK,
    ResearchMode.OWNERSHIP: (
        "请分析以岭药业（002603.SZ）的股权结构，生成股权穿透图，"
        "并说明主要股东及实际控制关系。"
    ),
    ResearchMode.FINANCIAL: (
        "请从银行授信角度分析以岭药业2025年的财务状况，重点分析收入、"
        "应收账款、存货、债务结构和经营活动现金流，并提出尽调问题。"
    ),
    ResearchMode.INDUSTRY: (
        "请研究以岭药业所处行业的发展趋势、竞争格局及政策监管环境，"
        "并分析其对企业经营和授信风险的潜在影响。"
    ),
    ResearchMode.FUNDAMENTALS: (
        "提取以岭药业的发展历史、主营业务、经营模式和主要产品。"
    ),
}
MODE_LABELS = {
    ResearchMode.COMPLETE: "完整企业授信研究",
    ResearchMode.OWNERSHIP: "股权结构研究",
    ResearchMode.FINANCIAL: "财务分析",
    ResearchMode.INDUSTRY: "行业与政策研究",
    ResearchMode.FUNDAMENTALS: "企业基本面提取（确定性）",
}
STAGE_LABELS = {
    "cli_started": "准备研究环境",
    "run_started": "Agent 开始执行",
    "llm_call_started": "DeepSeek 正在分析",
    "llm_call_completed": "DeepSeek 本轮响应完成",
    "tool_selected": "正在调用研究工具",
    "llm_tool_selected": "正在调用研究工具",
    "search_completed": "搜索完成",
    "source_fetched": "原文获取完成",
    "source_fetch_failed": "部分原文获取失败",
    "evidence_extracted": "证据提取完成",
    "budget_exhausted": "工具预算已停止新增调用",
    "grounded_output_validated": "研究结果已整理",
    "run_completed": "研究交付完成",
    "run_failed": "研究未完成",
}


def find_project_root() -> Path:
    """从包位置向上寻找项目根，避免依赖启动时当前目录。"""

    source = Path(__file__).resolve()
    for candidate in (source.parent, *source.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("无法定位项目根目录")


def _strip_local_image_links(markdown: str) -> str:
    """移除浏览器无法直接访问的本地股权图链接，图片由 st.image 单独呈现。"""

    return re.sub(
        r"^\s*!\[[^\]]*\]\([^\n)]*ownership_structure\.(?:png|svg)\)\s*$",
        "",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE,
    )


def split_report_sections(markdown: str) -> list[tuple[str, str]]:
    """按二级标题切分报告，为长篇授信报告提供章节导航。"""

    lines = markdown.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^##\s+\S", line.strip())
    ]
    sections: list[tuple[str, str]] = [("全文", markdown)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        heading = re.sub(r"^##\s+", "", lines[start].strip()).strip()
        sections.append((heading, "\n".join(lines[start:end]).strip()))
    return sections


def _status_badge(st: Any, artifact: ResearchRunArtifact) -> None:
    text = f"{artifact.status_message}（原始状态：{artifact.original_status}）"
    if artifact.display_status is ReportDisplayStatus.SUCCESS:
        st.success(text)
    elif artifact.display_status is ReportDisplayStatus.PARTIAL_SUCCESS:
        st.warning(text)
    elif artifact.display_status is ReportDisplayStatus.FAILED:
        st.error(text)
    else:
        st.info(text)


def render_artifact(
    st: Any,
    artifact: ResearchRunArtifact,
    *,
    key_prefix: str = "artifact",
) -> None:
    """展示已有产物；Word 仅由本地文件转换，不创建模型或搜索客户端。"""

    _status_badge(st, artifact)
    mode_label = "Live" if artifact.is_live else "Mock / 离线"
    columns = st.columns(3)
    columns[0].metric("研究企业", artifact.company_name or artifact.entity_id)
    columns[1].metric("证券代码", artifact.ticker or "未记录")
    columns[2].metric("运行模式", mode_label)
    if artifact.recovered:
        st.caption("本次展示的是已保存草稿的离线恢复版；原始 Live 运行状态仍保留为失败。")
    if artifact.research_task:
        st.caption(f"研究任务：{artifact.research_task}")
    for warning in artifact.warnings:
        st.warning(warning)

    report = read_report_markdown(artifact)
    report_tab, graph_tab, financial_tab, run_tab = st.tabs(
        ["研究报告", "股权结构图", "财务数据", "运行信息"]
    )
    with report_tab:
        if report:
            download_columns = st.columns(2)
            download_columns[0].download_button(
                "下载 Markdown 报告",
                data=report.encode("utf-8"),
                file_name=artifact.report_path.name if artifact.report_path else "report.md",
                mime="text/markdown",
                key=f"{key_prefix}-download-md-{abs(hash(str(artifact.run_dir)))}",
            )
            try:
                word_path, word_payload = read_word_report(artifact)
            except (ArtifactAccessError, WordExportError, OSError, ValueError) as exc:
                st.warning(f"Word 报告导出失败，Markdown 仍可正常查看：{exc}")
            else:
                download_columns[1].download_button(
                    "下载 Word 报告",
                    data=word_payload,
                    file_name=word_path.name,
                    mime=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    key=f"{key_prefix}-download-docx-{abs(hash(str(artifact.run_dir)))}",
                )
                st.caption("Word 由当前 Markdown 与既有图表在本地转换，不会调用模型或搜索 API。")
            sections = split_report_sections(_strip_local_image_links(report))
            selected_section = st.selectbox(
                "章节导航",
                options=range(len(sections)),
                format_func=lambda index: sections[index][0],
                key=f"{key_prefix}-section-{abs(hash(str(artifact.run_dir)))}",
            )
            st.markdown(sections[int(selected_section)][1], unsafe_allow_html=False)
        else:
            st.info("该运行尚未生成可展示的 Markdown 报告。")
    with graph_tab:
        if artifact.ownership_image_path is not None:
            st.image(
                str(artifact.ownership_image_path),
                caption="股权结构图（由现有 ownership-structure Tool 生成）",
                width="stretch",
            )
        else:
            st.info("该运行没有可展示的股权结构图。")
    with financial_tab:
        tables = load_financial_tables(artifact)
        if tables.metric_rows:
            st.subheader("财务科目变动")
            st.dataframe(tables.metric_rows, width="stretch", hide_index=True)
        if tables.ratio_rows:
            st.subheader("财务比率")
            st.dataframe(tables.ratio_rows, width="stretch", hide_index=True)
        if not tables.metric_rows and not tables.ratio_rows:
            st.info("该运行没有可展示的结构化财务数据。")
    with run_tab:
        st.code(str(artifact.run_dir), language=None)
        if artifact.trace_path is not None:
            st.caption(f"执行轨迹：{artifact.trace_path.name}")
        if artifact.delivery_path is not None:
            st.caption(f"交付状态：{artifact.delivery_path.name}")


def _render_task_progress(st: Any, record: UiTaskRecord) -> None:
    events = read_progress_events(record.progress_path)
    if not events:
        st.info("后台任务已提交，正在准备公司资料与研究环境。")
        return
    latest = events[-1]
    st.info(f"当前环节：{STAGE_LABELS.get(latest.stage, latest.message)}")
    st.caption(latest.message)
    latest_tool: Optional[str] = None
    for event in reversed(events):
        if event.stage in {"tool_selected", "llm_tool_selected"}:
            latest_tool = str(event.details.get("tool_name") or "") or None
            break
        if event.stage in {"llm_call_started", "run_completed", "run_failed"}:
            break
    if latest_tool:
        st.write(f"当前工具调用：{latest_tool}")
    with st.expander("查看最近执行进度", expanded=False):
        st.dataframe(
            [
                {
                    "序号": item.sequence,
                    "阶段": STAGE_LABELS.get(item.stage, item.stage),
                    "说明": item.message,
                    "工具": item.details.get("tool_name", ""),
                }
                for item in events[-20:]
            ],
            width="stretch",
            hide_index=True,
        )


def _render_single_research_output(
    st: Any, mode: ResearchMode, entity_dir: Path
) -> None:
    """展示单项研究的既有 Markdown/图表，不生成或改写分析正文。"""

    report_candidates = {
        ResearchMode.OWNERSHIP: [entity_dir / "ownership_summary.md"],
        ResearchMode.FINANCIAL: [entity_dir / "financial_analysis.md"],
        ResearchMode.INDUSTRY: [entity_dir / "industry_analysis.md"],
        ResearchMode.FUNDAMENTALS: [
            entity_dir / "fundamentals_citations.md",
            entity_dir / "citations.md",
        ],
    }.get(mode, [])
    report_path = _first_existing(report_candidates)
    if report_path is not None:
        st.markdown(report_path.read_text(encoding="utf-8"), unsafe_allow_html=False)
    if mode is ResearchMode.OWNERSHIP:
        image = _first_existing(
            [
                entity_dir / "ownership_structure.png",
                entity_dir / "ownership_structure.svg",
            ]
        )
        if image is not None:
            st.image(str(image), width="stretch")
    if report_path is None and mode is not ResearchMode.OWNERSHIP:
        st.info("该单项运行已保存结构化产物，但没有可直接展示的 Markdown 正文。")


def _render_finished_task(st: Any, project_root: Path, record: UiTaskRecord) -> None:
    if record.status is UiTaskStatus.SUCCEEDED:
        st.success("研究完成")
    elif record.status is UiTaskStatus.PARTIAL_SUCCESS:
        st.warning("研究部分完成，已保留可交付成果")
    else:
        st.error("研究失败，已保留日志和已生成产物")
    if record.error_message:
        st.caption(f"失败信息：{record.error_message}")
    log_tail = read_cli_log_tail(record.log_path)
    if record.status is UiTaskStatus.FAILED and log_tail:
        with st.expander("查看 CLI 错误日志", expanded=False):
            st.code(log_tail, language=None)

    if record.mode is ResearchMode.COMPLETE and record.artifact_dir.is_dir():
        try:
            artifact = load_research_run(
                record.artifact_dir,
                allowed_roots=default_artifact_roots(project_root),
            )
        except (OSError, ValueError) as exc:
            st.warning(f"完整报告产物尚无法加载：{exc}")
        else:
            render_artifact(st, artifact, key_prefix=f"task-{record.task_id}")
    elif record.artifact_dir.is_dir():
        _render_single_research_output(st, record.mode, record.artifact_dir)


def _render_task_monitor(st: Any, project_root: Path, record: UiTaskRecord) -> None:
    st.divider()
    st.subheader("研究任务状态")
    st.caption(
        f"任务 ID：{record.task_id} · {MODE_LABELS[record.mode]} · "
        f"{record.execution_mode.value.upper()}"
    )
    if record.status.is_finished:
        _render_finished_task(st, project_root, record)
        return

    @st.fragment(run_every=1.0)
    def live_status() -> None:
        try:
            current = load_ui_task(record.run_dir, project_root=project_root)
        except (UiServiceError, OSError) as exc:
            st.error(f"无法读取后台任务状态：{exc}")
            return
        if current.status.is_finished:
            st.rerun()
        if current.status is UiTaskStatus.QUEUED:
            st.info("任务已进入后台队列，等待 Worker 启动。")
        else:
            st.info("后台 Agent 正在运行；刷新或关闭页面不会重复提交该任务。")
        _render_task_progress(st, current)

    live_status()


def _render_new_research(st: Any, project_root: Path) -> None:
    st.subheader("启动新研究")
    st.caption("默认不会调用任何 API。只有完成下方显式确认并点击启动，才会进入 Live 模式。")
    readiness = load_api_readiness(project_root)
    tasks = [
        reconcile_interrupted_task(item)
        for item in discover_ui_tasks(project_root)
    ]
    running_tasks = [item for item in tasks if not item.status.is_finished]
    ready_columns = st.columns(2)
    ready_columns[0].write(
        "DeepSeek：" + ("已配置" if readiness.deepseek_configured else "未配置")
    )
    ready_columns[1].write(
        "百度搜索：" + ("已配置" if readiness.baidu_search_configured else "未配置")
    )

    company_query = st.text_input("上市公司名称或股票代码", value="002603.SZ")
    if "v31_research_mode" not in st.session_state:
        st.session_state.v31_research_mode = ResearchMode.COMPLETE
    if "v31_research_task" not in st.session_state:
        st.session_state.v31_research_task = DEFAULT_TASK

    def update_default_task() -> None:
        selected = st.session_state.v31_research_mode
        st.session_state.v31_research_task = MODE_TASKS[selected]

    mode = st.selectbox(
        "研究类型",
        options=list(ResearchMode),
        format_func=lambda item: MODE_LABELS[item],
        key="v31_research_mode",
        on_change=update_default_task,
    )
    task = st.text_area("研究任务", height=120, key="v31_research_task")

    identity = None
    try:
        identity = CompanyResolver.from_default_catalog().resolve(company_query)
        st.success(
            f"已识别：{identity.legal_name}（{identity.ticker}，{identity.exchange}）"
        )
    except CompanyResolutionError as exc:
        st.error(str(exc))

    confirm_llm = False
    confirm_search = False
    if mode.uses_llm:
        confirm_llm = st.checkbox(
            "我确认启动一次真实 DeepSeek Live 研究，可能产生 API 费用"
        )
    else:
        st.info("企业基本面提取使用确定性 PDF 工具，不调用 DeepSeek。")
    if mode.uses_search:
        confirm_search = st.checkbox(
            "我确认允许本次任务调用百度搜索 API，可能消耗额度"
        )

    missing_key = (mode.uses_llm and not readiness.deepseek_configured) or (
        mode.uses_search and not readiness.baidu_search_configured
    )
    confirmed = (not mode.uses_llm or confirm_llm) and (
        not mode.uses_search or confirm_search
    )
    submission_locked = bool(st.session_state.get("v31_submission_locked", False))
    disabled = (
        identity is None
        or missing_key
        or not confirmed
        or bool(running_tasks)
        or submission_locked
    )
    if missing_key:
        st.warning("所选研究类型缺少必要 API Key；仍可在“历史报告”中离线查看已有成果。")
    if running_tasks:
        st.info("已有后台研究任务正在运行。V3.1 为避免重复计费，暂不允许再次提交。")

    if st.button("启动研究", type="primary", disabled=disabled):
        try:
            request = ResearchRequest(
                company=company_query,
                task=task,
                mode=mode,
                confirm_deepseek_live=confirm_llm,
                confirm_baidu_live=confirm_search,
            )
            command = build_live_command(project_root, request)
            submission_key = st.session_state.get("v31_idempotency_key")
            if not submission_key:
                submission_key = "v31-" + uuid4().hex
                st.session_state.v31_idempotency_key = submission_key
            submission = submit_research_task(
                command,
                idempotency_key=submission_key,
            )
            st.session_state.v31_submission_locked = True
            st.session_state.v31_selected_task = str(submission.record.run_dir)
            st.rerun()
        except (UiServiceError, OSError, ValueError) as exc:
            st.error(f"无法启动研究：{exc}")

    refreshed_tasks = [
        reconcile_interrupted_task(item)
        for item in discover_ui_tasks(project_root)
    ]
    if refreshed_tasks:
        labels = {
            str(item.run_dir): (
                f"{item.task_id} · {MODE_LABELS[item.mode]} · {item.status.value}"
            )
            for item in refreshed_tasks
        }
        default_path = st.session_state.get("v31_selected_task")
        options = list(labels)
        default_index = options.index(default_path) if default_path in options else 0
        selected_path = st.selectbox(
            "查看后台研究任务",
            options=options,
            index=default_index,
            format_func=lambda value: labels[value],
        )
        st.session_state.v31_selected_task = selected_path
        selected_record = next(
            item for item in refreshed_tasks if str(item.run_dir) == selected_path
        )
        if selected_record.status.is_finished and st.session_state.get(
            "v31_submission_locked"
        ):
            if st.button("允许提交新的研究任务"):
                st.session_state.v31_submission_locked = False
                st.session_state.pop("v31_idempotency_key", None)
                st.rerun()
        _render_task_monitor(st, project_root, selected_record)


def _first_existing(paths: list[Path]) -> Optional[Path]:
    return next((path for path in paths if path.is_file()), None)


def preferred_history_run_path(runs: list[ResearchRunArtifact]) -> str:
    """优先选择最近一份可展示的Live报告，否则回退到最新本地报告。

    ``discover_research_runs`` 已按更新时间倒序返回，所以第一个匹配项就是
    相应类别中的最新报告。该选择只影响界面默认值，不改变用户手动选择。
    """

    for artifact in runs:
        if artifact.is_live and artifact.report_path is not None:
            return str(artifact.run_dir)
    for artifact in runs:
        if artifact.report_path is not None:
            return str(artifact.run_dir)
    return str(runs[0].run_dir) if runs else ""


def _render_history(st: Any, project_root: Path) -> None:
    st.subheader("历史报告")
    st.caption("这里仅读取本地产物，不检查 API Key，也不会发送网络请求。")
    runs = discover_research_runs(project_root)
    if not runs:
        st.info("尚未发现 V2.5 完整研究报告。")
        return
    runs_by_path = {str(item.run_dir): item for item in runs}
    options = list(runs_by_path)
    current_selection = st.session_state.get(HISTORY_SELECTION_KEY)
    if current_selection not in runs_by_path:
        st.session_state[HISTORY_SELECTION_KEY] = preferred_history_run_path(runs)
    selected_path = st.selectbox(
        "选择报告",
        options=options,
        format_func=lambda path: (
            f"{runs_by_path[path].label} · "
            f"{runs_by_path[path].updated_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
        ),
        key=HISTORY_SELECTION_KEY,
    )
    # Markdown展示和Word导出都接收同一个Artifact，确保两者读取同一report_path。
    render_artifact(st, runs_by_path[selected_path], key_prefix="history")


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:  # pragma: no cover - 仅发生在未安装 ui extra 时
        raise RuntimeError(
            '尚未安装 Streamlit，请运行：python -m pip install -e ".[ui]"'
        ) from exc

    st.set_page_config(
        page_title="企业授信研究 Agent",
        page_icon="🏦",
        layout="wide",
    )
    st.title("企业授信研究 Agent")
    st.caption("公开资料驱动 · DeepSeek 分析撰写 · Python 数据与工具基础设施")
    st.warning("AI 生成研究初稿，需由授信人员结合最新资料进行人工复核。", icon="⚠️")

    project_root = find_project_root()
    new_tab, history_tab = st.tabs(["新建研究", "历史报告"])
    with new_tab:
        _render_new_research(st, project_root)
    with history_tab:
        _render_history(st, project_root)


if __name__ == "__main__":
    main()
