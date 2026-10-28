from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from credit_research_agent.streamlit_app import (
    DEFAULT_TASK,
    _render_markdown_with_ownership_image,
    _strip_local_image_links,
    preferred_history_run_path,
    split_report_sections,
)


def test_default_complete_research_task_is_company_and_period_neutral() -> None:
    assert "以岭药业" not in DEFAULT_TASK
    assert "002603" not in DEFAULT_TASK
    assert "2025" not in DEFAULT_TASK
    assert "股权结构" in DEFAULT_TASK
    assert "财务" in DEFAULT_TASK


def test_report_sections_include_full_text_and_each_chapter() -> None:
    report = "# 报告\n\n导语\n\n## 第一章\n\n内容一\n\n## 第二章\n\n内容二\n"

    sections = split_report_sections(report)

    assert [title for title, _ in sections] == ["全文", "第一章", "第二章"]
    assert sections[0][1] == report
    assert "内容一" in sections[1][1]
    assert "内容二" not in sections[1][1]


def test_local_ownership_image_is_removed_from_markdown_only() -> None:
    report = (
        "## 股权结构\n\n"
        "![股权图](assets/ownership_structure.svg)\n\n"
        "股权分析正文。\n"
    )

    cleaned = _strip_local_image_links(report)

    assert "ownership_structure.svg" not in cleaned
    assert "股权分析正文" in cleaned


class _FakeStreamlit:
    def __init__(self) -> None:
        self.markdown_calls: list[str] = []
        self.image_calls: list[dict[str, object]] = []

    def markdown(self, content: str, *, unsafe_allow_html: bool) -> None:
        assert unsafe_allow_html is False
        self.markdown_calls.append(content)

    def image(self, path: str, *, caption: str, width: str) -> None:
        self.image_calls.append({"path": path, "caption": caption, "width": width})


def test_report_renders_local_ownership_image_at_markdown_position() -> None:
    st = _FakeStreamlit()
    image = Path("run/assets/ownership_structure.png")
    report = (
        "## 第一章\n\n企业历史。\n\n"
        "### 股权结构图\n\n"
        "![股权图](assets/ownership_structure.svg)\n\n"
        "## 第二章\n\n经营情况。\n"
    )

    _render_markdown_with_ownership_image(st, report, image)

    assert len(st.image_calls) == 1
    assert st.image_calls[0] == {
        "path": str(image),
        "caption": "股权结构图",
        "width": "stretch",
    }
    assert "企业历史" in st.markdown_calls[0]
    assert "经营情况" in st.markdown_calls[1]
    assert all("ownership_structure" not in item for item in st.markdown_calls)


def test_report_without_ownership_marker_remains_single_markdown_block() -> None:
    st = _FakeStreamlit()
    report = "## 第一章\n\n企业历史。\n"

    _render_markdown_with_ownership_image(st, report, None)

    assert st.markdown_calls == [report]
    assert st.image_calls == []


def _history_run(name: str, *, is_live: bool, has_report: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        run_dir=Path(name),
        is_live=is_live,
        report_path=Path(name) / "report.md" if has_report else None,
    )


def test_history_default_prefers_latest_live_over_newer_mock() -> None:
    runs = [
        _history_run("newer-mock", is_live=False),
        _history_run("latest-live", is_live=True),
        _history_run("older-live", is_live=True),
    ]

    assert preferred_history_run_path(runs) == str(Path("latest-live"))


def test_history_default_skips_live_without_report() -> None:
    runs = [
        _history_run("live-without-report", is_live=True, has_report=False),
        _history_run("valid-live", is_live=True),
    ]

    assert preferred_history_run_path(runs) == str(Path("valid-live"))


def test_history_default_falls_back_to_latest_available_mock() -> None:
    runs = [
        _history_run("mock-without-report", is_live=False, has_report=False),
        _history_run("latest-valid-mock", is_live=False),
        _history_run("older-valid-mock", is_live=False),
    ]

    assert preferred_history_run_path(runs) == str(Path("latest-valid-mock"))
