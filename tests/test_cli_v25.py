"""V2.5 完整授信研究 CLI 的离线绑定与 Live 确认测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from credit_research_agent import cli
from credit_research_agent.agent_runtime import AgentTokenUsage
from credit_research_agent.documents import DownloadResult, ExtractionArtifacts
from credit_research_agent.fundamentals import FundamentalArtifacts
from credit_research_agent.integrated_agent_runtime import CompleteResearchResult
from credit_research_agent.retrieval_runtime import RetrievalStatus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK = "请对以岭药业（002603.SZ）进行完整的企业授信研究"


def _download_result(source: Any, root: Path) -> DownloadResult:
    """构造只用于 CLI 绑定测试的离线下载结果。"""

    return DownloadResult(
        source_id=source.source_id,
        source_title=source.title,
        source_url=str(source.url),
        published_at=source.published_at,
        report_period=source.report_period,
        file_path=root / f"{source.source_id}.pdf",
        manifest_path=root / f"{source.source_id}.metadata.json",
        accessed_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        final_url=str(source.url),
        http_status=200,
        content_type="application/pdf",
        bytes_written=1024,
        sha256="a" * 64,
        from_cache=True,
    )


def _extraction_artifacts(download: DownloadResult, root: Path) -> ExtractionArtifacts:
    return ExtractionArtifacts(
        source_id=download.source_id,
        summary_path=root / download.source_id / "summary.json",
        pages_path=root / download.source_id / "pages.jsonl",
        sample_path=root / download.source_id / "sample.json",
        total_pages=1,
        pages_with_text=1,
        failed_pages=0,
    )


def test_parser_accepts_run_complete_research_with_default_source() -> None:
    args = cli.build_parser().parse_args(
        ["002603.SZ", "--run-complete-research", TASK]
    )

    assert args.run_complete_research == TASK
    assert args.complete_source == "S01"
    assert args.agent_source is None
def test_cli_v25_mock_binds_s01_builds_fundamentals_and_s10_comparison(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Mock 入口不读真 PDF，但完整验证 CLI 的运行时绑定。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_find_project_root", lambda: PROJECT_ROOT)

    downloads: list[str] = []
    extractions: list[str] = []
    fundamental_calls: list[dict[str, Any]] = []
    runtime_initialization: dict[str, Any] = {}
    runtime_calls: list[dict[str, Any]] = []

    class FakeDownloader:
        def download(
            self, source: Any, destination_dir: Path, *, force: bool = False
        ) -> DownloadResult:
            downloads.append(source.source_id)
            return _download_result(source, Path(destination_dir))

    def fake_extract(
        download: DownloadResult,
        output_dir: Path,
        *,
        sample_output: Path | None = None,
    ) -> ExtractionArtifacts:
        del sample_output
        extractions.append(download.source_id)
        return _extraction_artifacts(download, Path(output_dir))

    def fake_build_fundamentals(**kwargs: Any) -> FundamentalArtifacts:
        fundamental_calls.append(kwargs)
        entity_dir = Path(kwargs["output_dir"]) / kwargs["identity"].entity_id
        return FundamentalArtifacts(
            entity_id=kwargs["identity"].entity_id,
            fundamentals_path=entity_dir / "fundamentals.json",
            citations_path=entity_dir / "citations.md",
            fact_count=4,
            evidence_count=4,
            inference_count=0,
            missing_information_count=1,
            module_fact_counts={"main_business": 2, "main_products": 2},
            source_count=1,
        )

    class FakeIntegratedRuntime:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            runtime_initialization["args"] = args
            runtime_initialization["kwargs"] = kwargs

        def run_sync(self, **kwargs: Any) -> CompleteResearchResult:
            runtime_calls.append(kwargs)
            output_dir = Path(kwargs["output_dir"])
            return CompleteResearchResult(
                status=RetrievalStatus.SUCCEEDED,
                report_path=output_dir / "credit_research_report.md",
                context_path=output_dir / "complete_research_context.json",
                sources_path=output_dir / "credit_research_sources.json",
                trace_path=Path(kwargs["trace_output"]),
                draft_path=output_dir / "complete_llm_draft.json",
                delivery_path=output_dir / "complete_delivery.json",
                ownership_svg_path=output_dir / "assets" / "ownership_structure.svg",
                ownership_png_path=output_dir / "assets" / "ownership_structure.png",
                delivered_topics=["company_background_and_ownership"],
                missing_topics=[],
                module_status={"fundamentals": "succeeded"},
                limitations=[],
                usage=AgentTokenUsage(),
                search_api_requests=0,
                estimated_search_cost_cny=0.0,
                llm_config={"mode": "mock"},
                search_config={"mode": "mock"},
                budget_limits={"max_total_tool_calls": 60},
            )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", fake_extract)
    monkeypatch.setattr(cli, "build_fundamental_artifacts", fake_build_fundamentals)
    monkeypatch.setattr(cli, "IntegratedCreditResearchAgentRuntime", FakeIntegratedRuntime)
    monkeypatch.setattr(cli, "load_industry_search_fixture", lambda path: {"results": []})
    monkeypatch.setattr(
        cli,
        "build_mock_retrieval_dependencies",
        lambda fixture: ("mock-provider", "mock-fetcher"),
    )

    output_root = tmp_path / "complete"
    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-complete-research",
            TASK,
            "--complete-dir",
            str(output_root),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--parsed-dir",
            str(tmp_path / "parsed"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert "agent_error" not in output
    assert output["complete_research"]["status"] == "succeeded"
    assert downloads == ["S01", "S10"]
    assert extractions == ["S01", "S10"]
    assert len(fundamental_calls) == 1
    assert fundamental_calls[0]["source"].source_id == "S01"
    assert runtime_initialization["kwargs"]["search_provider_override"] == "mock-provider"
    assert runtime_initialization["kwargs"]["source_fetcher_override"] == "mock-fetcher"

    assert len(runtime_calls) == 1
    runtime_call = runtime_calls[0]
    assert runtime_call["source"].source_id == "S01"
    assert runtime_call["download"].source_id == "S01"
    assert [item.source.source_id for item in runtime_call["comparison_inputs"]] == [
        "S10"
    ]
    assert runtime_call["fundamentals_path"].name == "fundamentals.json"
    assert runtime_call["confirmed_llm_live"] is False
    assert runtime_call["confirmed_search_live"] is False


def test_cli_v25_live_requires_llm_confirmation_before_runtime(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_find_project_root", lambda: PROJECT_ROOT)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("BAIDU_SEARCH_API_KEY", "test-baidu-key")
    monkeypatch.setenv("SEARCH_PROVIDER", "baidu")
    monkeypatch.setattr(
        cli,
        "IntegratedCreditResearchAgentRuntime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Live确认前不得创建Runtime")
        ),
    )

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-complete-research",
            TASK,
            "--llm-mode",
            "live",
            "--search-mode",
            "live",
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 9
    assert output["agent_error"]["code"] == "live_confirmation_required"
    assert "test-deepseek-key" not in captured.out
    assert "test-deepseek-key" not in captured.err


def test_cli_v25_live_requires_search_confirmation_before_runtime(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_find_project_root", lambda: PROJECT_ROOT)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("BAIDU_SEARCH_API_KEY", "test-baidu-key")
    monkeypatch.setenv("SEARCH_PROVIDER", "baidu")
    monkeypatch.setattr(
        cli,
        "IntegratedCreditResearchAgentRuntime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("百度Live确认前不得创建Runtime")
        ),
    )

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-complete-research",
            TASK,
            "--llm-mode",
            "live",
            "--confirm-live",
            "--search-mode",
            "live",
        ]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 11
    assert (
        output["retrieval_error"]["code"]
        == "live_search_confirmation_required"
    )
    assert "test-deepseek-key" not in captured.out
    assert "test-baidu-key" not in captured.out
    assert "test-deepseek-key" not in captured.err
    assert "test-baidu-key" not in captured.err


def test_complete_source_does_not_trigger_legacy_orphan_agent_source_rule() -> None:
    args = cli.build_parser().parse_args(
        [
            "002603.SZ",
            "--run-complete-research",
            TASK,
            "--complete-source",
            "S01",
        ]
    )

    assert args.complete_source == "S01"
    assert args.agent_source is None
