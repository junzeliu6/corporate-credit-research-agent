import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from credit_research_agent import cli
from credit_research_agent.documents import DownloadResult, ExtractionArtifacts
from credit_research_agent.downloader import DownloadError
from credit_research_agent.evidence import EvidenceArtifacts, EvidenceBuildError
from credit_research_agent.fundamentals import FundamentalArtifacts
from credit_research_agent.ownership import OwnershipArtifacts
from credit_research_agent.pdf_parser import PdfExtractionError


def test_cli_resolves_company_and_lists_registered_sources(capsys) -> None:
    exit_code = cli.main(["以岭药业", "--sources"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["company"]["ticker"] == "002603.SZ"
    assert output["company"]["entity_scope"] == "consolidated"
    assert output["sources"][0]["source_id"] == "S01"
    assert output["sources"][0]["status"] == "registered"


def test_cli_returns_nonzero_for_unknown_company(capsys) -> None:
    exit_code = cli.main(["不存在的公司"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert "无法唯一识别企业" in output["error"]


def test_cli_download_option_returns_auditable_result(tmp_path, monkeypatch, capsys) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            assert source.source_id == "S01"
            assert destination_dir == tmp_path
            assert force is True
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                published_at=source.published_at,
                report_period=source.report_period,
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                content_type="application/pdf",
                bytes_written=123,
                sha256="a" * 64,
            )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)

    exit_code = cli.main(
        [
            "002603",
            "--download-source",
            "S01",
            "--cache-dir",
            str(tmp_path),
            "--force-download",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["download"]["source_id"] == "S01"
    assert output["download"]["http_status"] == 200


def test_cli_download_failure_is_explicit(monkeypatch, capsys) -> None:
    class FailingDownloader:
        def download(self, source, destination_dir: Path, *, force=False):
            raise DownloadError("network_error", "模拟网络失败", attempts=3)

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FailingDownloader)

    exit_code = cli.main(["以岭药业", "--download-source", "S01"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert output["download_error"] == {
        "code": "network_error",
        "message": "模拟网络失败",
    }


def test_cli_parse_option_runs_download_then_extraction(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                published_at=source.published_at,
                report_period=source.report_period,
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                content_type="application/pdf",
                bytes_written=123,
                sha256="a" * 64,
                from_cache=True,
            )

    def fake_extract(download, output_dir, *, sample_output=None):
        assert download.source_id == "S01"
        assert output_dir == tmp_path / "parsed"
        assert sample_output == tmp_path / "sample.json"
        return ExtractionArtifacts(
            source_id="S01",
            summary_path=output_dir / "S01" / "summary.json",
            pages_path=output_dir / "S01" / "pages.jsonl",
            sample_path=sample_output,
            total_pages=229,
            pages_with_text=229,
            failed_pages=0,
        )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", fake_extract)

    exit_code = cli.main(
        [
            "以岭药业",
            "--parse-source",
            "S01",
            "--parsed-dir",
            str(tmp_path / "parsed"),
            "--sample-output",
            str(tmp_path / "sample.json"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["download"]["from_cache"] is True
    assert output["extraction"]["total_pages"] == 229


def test_cli_parse_failure_is_explicit(tmp_path, monkeypatch, capsys) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
            )

    def failing_extract(*args, **kwargs):
        raise PdfExtractionError("pdf_open_error", "模拟解析失败")

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", failing_extract)

    exit_code = cli.main(["002603", "--parse-source", "S01"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert output["extraction_error"] == {
        "code": "pdf_open_error",
        "message": "模拟解析失败",
    }


def test_cli_build_evidence_runs_complete_pipeline(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
                from_cache=True,
            )

    extraction = ExtractionArtifacts(
        source_id="S01",
        summary_path=tmp_path / "parsed" / "S01" / "summary.json",
        pages_path=tmp_path / "parsed" / "S01" / "pages.jsonl",
        sample_path=tmp_path / "parsed" / "S01" / "sample.json",
        total_pages=229,
        pages_with_text=229,
        failed_pages=0,
    )

    def fake_extract(*args, **kwargs):
        return extraction

    def fake_build(**kwargs):
        assert kwargs["identity"].ticker == "002603.SZ"
        assert kwargs["summary_path"] == extraction.summary_path
        return EvidenceArtifacts(
            entity_id="CN-A-SZSE-002603",
            evidence_path=tmp_path / "evidence.json",
            citations_path=tmp_path / "citations.md",
            fact_count=4,
            evidence_count=3,
            source_count=1,
        )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", fake_extract)
    monkeypatch.setattr(cli, "build_evidence_artifacts", fake_build)

    exit_code = cli.main(["002603.SZ", "--build-evidence", "S01"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["extraction"]["total_pages"] == 229
    assert output["evidence"]["fact_count"] == 4


def test_cli_evidence_failure_is_explicit(tmp_path, monkeypatch, capsys) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
            )

    def fake_extract(*args, **kwargs):
        return ExtractionArtifacts(
            source_id="S01",
            summary_path=tmp_path / "summary.json",
            pages_path=tmp_path / "pages.jsonl",
            sample_path=tmp_path / "sample.json",
            total_pages=1,
            pages_with_text=1,
            failed_pages=0,
        )

    def failing_build(**kwargs):
        raise EvidenceBuildError("evidence_text_mismatch", "模拟证据不匹配")

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", fake_extract)
    monkeypatch.setattr(cli, "build_evidence_artifacts", failing_build)

    exit_code = cli.main(["以岭药业", "--build-evidence", "S01"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 5
    assert output["evidence_error"] == {
        "code": "evidence_text_mismatch",
        "message": "模拟证据不匹配",
    }


def test_cli_build_fundamentals_runs_reused_document_pipeline(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
                from_cache=True,
            )

    extraction = ExtractionArtifacts(
        source_id="S01",
        summary_path=tmp_path / "parsed" / "S01" / "summary.json",
        pages_path=tmp_path / "parsed" / "S01" / "pages.jsonl",
        sample_path=tmp_path / "parsed" / "S01" / "sample.json",
        total_pages=229,
        pages_with_text=229,
        failed_pages=0,
    )

    def fake_extract(*args, **kwargs):
        return extraction

    def fake_build(**kwargs):
        assert kwargs["identity"].ticker == "002603.SZ"
        assert kwargs["source"].source_id == "S01"
        assert kwargs["pages_path"] == extraction.pages_path
        return FundamentalArtifacts(
            entity_id="CN-A-SZSE-002603",
            fundamentals_path=tmp_path / "fundamentals.json",
            citations_path=tmp_path / "citations.md",
            fact_count=21,
            evidence_count=19,
            inference_count=0,
            missing_information_count=3,
            module_fact_counts={
                "company_identity": 4,
                "development_history": 3,
                "main_business": 2,
                "operating_model": 8,
                "main_products": 4,
            },
            source_count=1,
        )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", fake_extract)
    monkeypatch.setattr(cli, "build_fundamental_artifacts", fake_build)

    exit_code = cli.main(["002603.SZ", "--build-fundamentals", "S01"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["fundamentals"]["fact_count"] == 21
    assert output["fundamentals"]["inference_count"] == 0
    assert output["fundamentals"]["module_fact_counts"]["operating_model"] == 8


def test_cli_build_ownership_runs_reused_document_pipeline(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
                from_cache=True,
            )

    extraction = ExtractionArtifacts(
        source_id="S01",
        summary_path=tmp_path / "parsed" / "S01" / "summary.json",
        pages_path=tmp_path / "parsed" / "S01" / "pages.jsonl",
        sample_path=tmp_path / "parsed" / "S01" / "sample.json",
        total_pages=229,
        pages_with_text=229,
        failed_pages=0,
    )

    def fake_build(**kwargs):
        assert kwargs["identity"].ticker == "002603.SZ"
        assert kwargs["pages_path"] == extraction.pages_path
        assert kwargs["summary_output"] == tmp_path / "ownership_summary.md"
        assert kwargs["validation_output"] == tmp_path / "ownership_validation.json"
        assert kwargs["sources_output"] == tmp_path / "ownership_sources.json"
        assert kwargs["questions_output"] == tmp_path / "ownership_questions.md"
        return OwnershipArtifacts(
            entity_id="CN-A-SZSE-002603",
            dataset_path=tmp_path / "ownership_dataset.json",
            graph_path=tmp_path / "ownership_graph.json",
            svg_path=tmp_path / "ownership.svg",
            png_path=tmp_path / "ownership.png",
            dot_path=tmp_path / "ownership.dot",
            citations_path=tmp_path / "citations.md",
            summary_path=tmp_path / "ownership_summary.md",
            validation_path=tmp_path / "ownership_validation.json",
            sources_path=tmp_path / "ownership_sources.json",
            questions_path=tmp_path / "ownership_questions.md",
            render_engine="internal-tree-fallback",
            node_count=5,
            edge_count=5,
            control_relationship_count=4,
            missing_information_count=2,
        )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", lambda *args, **kwargs: extraction)
    monkeypatch.setattr(cli, "build_ownership_artifacts", fake_build)

    exit_code = cli.main(
        [
            "002603.SZ",
            "--build-ownership",
            "S01",
            "--ownership-summary-output",
            str(tmp_path / "ownership_summary.md"),
            "--ownership-validation-output",
            str(tmp_path / "ownership_validation.json"),
            "--ownership-sources-output",
            str(tmp_path / "ownership_sources.json"),
            "--ownership-questions-output",
            str(tmp_path / "ownership_questions.md"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["ownership"]["node_count"] == 5
    assert output["ownership"]["edge_count"] == 5
    assert output["ownership"]["render_engine"] == "internal-tree-fallback"


def test_cli_run_skill_requires_explicit_source(capsys) -> None:
    exit_code = cli.main(["以岭药业", "--run-skill", "生成股权结构图"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 8
    assert output["skill_error"]["code"] == "missing_skill_source"


def test_cli_run_skill_reuses_document_pipeline_and_runtime(
    tmp_path, monkeypatch, capsys
) -> None:
    class FakeDownloader:
        def download(self, source, destination_dir, *, force=False):
            return DownloadResult(
                source_id="S01",
                source_title=source.title,
                source_url=str(source.url),
                file_path=tmp_path / "S01.pdf",
                manifest_path=tmp_path / "S01.metadata.json",
                accessed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                final_url=str(source.url),
                http_status=200,
                bytes_written=1,
                sha256="a" * 64,
                from_cache=True,
            )

    extraction = ExtractionArtifacts(
        source_id="S01",
        summary_path=tmp_path / "parsed" / "S01" / "summary.json",
        pages_path=tmp_path / "parsed" / "S01" / "pages.jsonl",
        sample_path=tmp_path / "parsed" / "S01" / "sample.json",
        total_pages=229,
        pages_with_text=229,
        failed_pages=0,
    )

    class FakeSkillResult(BaseModel):
        selected_skill: str
        tool_name: str
        trace_path: Path

    class FakeRuntime:
        def __init__(self, project_root):
            assert (project_root / "pyproject.toml").is_file()

        def execute(self, **kwargs):
            assert kwargs["task"] == "生成股权结构图"
            assert kwargs["identity"].ticker == "002603.SZ"
            assert kwargs["source"].source_id == "S01"
            assert kwargs["tool_arguments"]["pages_path"] == extraction.pages_path
            assert kwargs["trace_output"] == tmp_path / "trace.json"
            return FakeSkillResult(
                selected_skill="ownership-structure",
                tool_name="build_ownership_artifacts",
                trace_path=kwargs["trace_output"],
            )

    monkeypatch.setattr(cli, "OfficialPdfDownloader", FakeDownloader)
    monkeypatch.setattr(cli, "extract_to_artifacts", lambda *args, **kwargs: extraction)
    monkeypatch.setattr(cli, "SkillRuntime", FakeRuntime)

    exit_code = cli.main(
        [
            "002603.SZ",
            "--run-skill",
            "生成股权结构图",
            "--skill-source",
            "S01",
            "--skill-trace-output",
            str(tmp_path / "trace.json"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["skill_run"]["selected_skill"] == "ownership-structure"
    assert output["skill_run"]["tool_name"] == "build_ownership_artifacts"
