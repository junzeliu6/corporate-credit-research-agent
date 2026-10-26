from __future__ import annotations

from credit_research_agent.streamlit_app import (
    _strip_local_image_links,
    split_report_sections,
)


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
