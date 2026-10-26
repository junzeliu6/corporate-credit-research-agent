from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from credit_research_agent import cli


def test_cli_exports_existing_markdown_without_research_or_network(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    report = tmp_path / "credit_research_report.md"
    output = tmp_path / "credit_research_report.docx"
    report.write_text(
        "# 企业授信研究报告\n\n## 财务分析\n\n营业收入保持增长。\n",
        encoding="utf-8",
    )

    class ForbiddenDownloader:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("Word导出不得创建下载器")

    monkeypatch.setattr(cli, "OfficialPdfDownloader", ForbiddenDownloader)

    exit_code = cli.main(
        [
            "002603.SZ",
            "--export-word",
            str(report),
            "--word-output",
            str(output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["word_export"]["external_api_requests"] == 0
    assert Path(payload["word_export"]["docx_path"]) == output.resolve()
    assert output.is_file()
    assert "营业收入保持增长。" in "\n".join(
        paragraph.text for paragraph in Document(output).paragraphs
    )


def test_cli_word_options_require_export_action(capsys) -> None:
    exit_code = cli.main(["002603.SZ", "--word-output", "report.docx"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 14
    assert payload["word_export_error"]["code"] == "orphan_word_export_option"


def test_cli_word_export_reports_missing_markdown(capsys, tmp_path: Path) -> None:
    exit_code = cli.main(
        ["002603.SZ", "--export-word", str(tmp_path / "missing.md")]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 14
    assert payload["word_export_error"]["code"] == "markdown_not_found"
