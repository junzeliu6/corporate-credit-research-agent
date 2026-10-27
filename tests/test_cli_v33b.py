"""V3.3B 通用财务报表提取 CLI 的离线边界测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic.v1 import BaseModel, Field

from credit_research_agent import cli
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialSourceInput, FinancialStatement
from credit_research_agent.financial_statements import (
    FinancialStatementExtractionError,
    GenericFinancialArtifacts,
    StatementExtractionStatus,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    SourceRecord,
    SourceStatus,
    SourceType,
)
from credit_research_agent.security_master import (
    DynamicCompanyResolution,
    SecurityMasterCacheStatus,
)


def _resolution() -> DynamicCompanyResolution:
    return DynamicCompanyResolution(
        identity=CompanyIdentity(
            entity_id="CN-A-SSE-600998",
            legal_name="九州通医药集团股份有限公司",
            short_name="九州通",
            ticker="600998.SH",
            security_code="600998",
            exchange="上海证券交易所",
            country_of_incorporation="中国",
            entity_scope=EntityScope.CONSOLIDATED,
            exchange_code="SSE",
            listing_board="沪市主板",
            listing_status="listed",
            external_org_id="9900015627",
            identity_source_url="https://www.sse.com.cn/assortment/stock/list/share/",
        ),
        resolution_source="official_security_master",
        cache_status=SecurityMasterCacheStatus.FRESH,
    )


class _FakeExtraction(BaseModel):
    summary_path: Path
    pages_path: Path


class _FakePreparedReport(BaseModel):
    source: SourceRecord
    download: DownloadResult
    extraction: _FakeExtraction


class _FakeOnboarding(BaseModel):
    status: str = "success"
    prepared_reports: list[_FakePreparedReport] = Field(default_factory=list)
    failures: list[BaseModel] = Field(default_factory=list)
    manifest_path: Path


class _FakeResolver:
    def __init__(self, calls: dict[str, object]) -> None:
        self.calls = calls

    def resolve_with_metadata(
        self, query: str, *, force_refresh: bool = False
    ) -> DynamicCompanyResolution:
        self.calls["resolve_query"] = query
        self.calls["resolve_force_refresh"] = force_refresh
        return _resolution()


class _FakeOnboardingService:
    def __init__(
        self,
        calls: dict[str, object],
        onboarding: _FakeOnboarding,
    ) -> None:
        self.calls = calls
        self.onboarding = onboarding

    def prepare(
        self,
        resolution: DynamicCompanyResolution,
        output_root: Path,
        *,
        report_count: int,
        force_refresh: bool,
        discover_only: bool,
    ) -> _FakeOnboarding:
        self.calls["prepare_resolution"] = resolution
        self.calls["prepare_output_root"] = output_root
        self.calls["prepare_report_count"] = report_count
        self.calls["prepare_force_refresh"] = force_refresh
        self.calls["prepare_discover_only"] = discover_only
        return self.onboarding


def _prepared_report(tmp_path: Path) -> _FakePreparedReport:
    source = SourceRecord(
        source_id="S01",
        entity_id="CN-A-SSE-600998",
        title="九州通医药集团股份有限公司2025年年度报告",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="巨潮资讯网（上市公司公告）",
        url="https://static.cninfo.com.cn/finalpage/2026-04-28/1225212712.PDF",
        published_at=date(2026, 4, 28),
        report_period="FY2025",
        trusted_tier=1,
        status=SourceStatus.PARSED,
    )
    return _FakePreparedReport(
        source=source,
        download=DownloadResult(
            source_id="S01",
            source_title=source.title,
            source_url=str(source.url),
            published_at=source.published_at,
            report_period=source.report_period,
            file_path=tmp_path / "S01.pdf",
            manifest_path=tmp_path / "S01.metadata.json",
            accessed_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
            final_url=str(source.url),
            http_status=200,
            content_type="application/pdf",
            bytes_written=1024,
            sha256="a" * 64,
        ),
        extraction=_FakeExtraction(
            summary_path=tmp_path / "parsed" / "S01" / "summary.json",
            pages_path=tmp_path / "parsed" / "S01" / "pages.jsonl",
        ),
    )


def _onboarding(
    tmp_path: Path,
    *,
    status: str = "success",
    with_report: bool = True,
) -> _FakeOnboarding:
    reports = [_prepared_report(tmp_path)] if with_report else []
    return _FakeOnboarding(
        status=status,
        prepared_reports=reports,
        manifest_path=tmp_path / "onboarding.json",
    )


def _artifacts(
    tmp_path: Path,
    status: StatementExtractionStatus,
) -> GenericFinancialArtifacts:
    output_dir = tmp_path / "financial_statements" / "CN-A-SSE-600998"
    return GenericFinancialArtifacts(
        entity_id="CN-A-SSE-600998",
        status=status,
        current_period="FY2025",
        prior_period="FY2024",
        extraction_path=output_dir / "financial_statement_extraction.json",
        facts_path=output_dir / "financial_facts.json",
        metrics_path=output_dir / "financial_metrics.json",
        risk_flags_path=output_dir / "financial_risk_flags.json",
        sources_path=output_dir / "financial_sources.json",
        validation_path=output_dir / "financial_validation.json",
        explanations_path=output_dir / "financial_explanations.json",
        reasonableness_path=output_dir / "financial_reasonableness.json",
        data_appendix_path=output_dir / "financial_data_appendix.md",
        fact_count=30,
        metric_count=15,
        ratio_count=4,
        statements_found=list(FinancialStatement),
        missing_statements=[],
    )


def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("V3.3B数据入口不应初始化LLM、百度搜索或Skill Runtime")


def _patch_data_entry(
    monkeypatch,
    calls: dict[str, object],
    onboarding: _FakeOnboarding,
) -> None:
    monkeypatch.setattr(
        cli,
        "_build_dynamic_company_resolver",
        lambda path: _FakeResolver(calls),
    )
    monkeypatch.setattr(
        cli,
        "_build_company_onboarding_service",
        lambda: _FakeOnboardingService(calls, onboarding),
    )
    monkeypatch.setattr(cli, "load_deepseek_config", _fail_if_called)
    monkeypatch.setattr(cli, "load_baidu_search_config", _fail_if_called)
    monkeypatch.setattr(cli, "SkillRegistry", _fail_if_called)
    monkeypatch.setattr(cli, "SkillRuntime", _fail_if_called)


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        (StatementExtractionStatus.SUCCESS, 0),
        (StatementExtractionStatus.PARTIAL_SUCCESS, 0),
        (StatementExtractionStatus.FAILED, 16),
    ],
)
def test_extract_financial_statements_binds_prepared_report_and_status_exit(
    tmp_path,
    monkeypatch,
    capsys,
    status: StatementExtractionStatus,
    expected_exit: int,
) -> None:
    calls: dict[str, object] = {}
    onboarding = _onboarding(tmp_path, status="partial_success")
    _patch_data_entry(monkeypatch, calls, onboarding)

    def fake_build_generic_financial_artifacts(
        *,
        identity: CompanyIdentity,
        source_inputs: list[FinancialSourceInput],
        output_dir: Path,
    ) -> GenericFinancialArtifacts:
        calls["financial_identity"] = identity
        calls["financial_source_inputs"] = source_inputs
        calls["financial_output_dir"] = output_dir
        return _artifacts(tmp_path, status)

    monkeypatch.setattr(
        cli,
        "build_generic_financial_artifacts",
        fake_build_generic_financial_artifacts,
    )
    output_root = tmp_path / "onboarding-root"

    exit_code = cli.main(
        [
            "600998.SH",
            "--extract-financial-statements",
            "--annual-report-count",
            "1",
            "--onboarding-dir",
            str(output_root),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == expected_exit
    assert calls["resolve_query"] == "600998.SH"
    assert calls["prepare_output_root"] == output_root
    assert calls["prepare_report_count"] == 1
    assert calls["prepare_discover_only"] is False
    assert calls["financial_identity"] == _resolution().identity
    assert calls["financial_output_dir"] == output_root
    source_inputs = calls["financial_source_inputs"]
    assert isinstance(source_inputs, list)
    assert len(source_inputs) == 1
    assert isinstance(source_inputs[0], FinancialSourceInput)
    assert source_inputs[0].source.source_id == "S01"
    assert source_inputs[0].summary_path == onboarding.prepared_reports[0].extraction.summary_path
    assert source_inputs[0].pages_path == onboarding.prepared_reports[0].extraction.pages_path
    assert output["financial_statements"]["status"] == status.value
    assert output["onboarding"]["prepared_source_ids"] == ["S01"]
    assert output["external_llm_requests"] == 0
    assert output["external_search_requests"] == 0


def test_extract_financial_statements_without_prepared_report_returns_16(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: dict[str, object] = {}
    _patch_data_entry(
        monkeypatch,
        calls,
        _onboarding(tmp_path, status="partial_success", with_report=False),
    )
    monkeypatch.setattr(cli, "build_generic_financial_artifacts", _fail_if_called)

    exit_code = cli.main(["600998.SH", "--extract-financial-statements"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 16
    assert output["financial_statement_error"] == {
        "code": "no_prepared_annual_report",
        "message": "没有通过下载、解析及主体复核的官方年报，无法提取财务报表",
    }
    assert output["external_llm_requests"] == 0
    assert output["external_search_requests"] == 0


def test_financial_statement_extraction_error_is_structured(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: dict[str, object] = {}
    _patch_data_entry(monkeypatch, calls, _onboarding(tmp_path))

    def fail_extraction(**kwargs):  # type: ignore[no-untyped-def]
        raise FinancialStatementExtractionError(
            "ambiguous_statement_period",
            "模拟报表期间无法唯一确认",
        )

    monkeypatch.setattr(cli, "build_generic_financial_artifacts", fail_extraction)

    exit_code = cli.main(["600998.SH", "--extract-financial-statements"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 16
    assert output["financial_statement_error"] == {
        "code": "ambiguous_statement_period",
        "message": "模拟报表期间无法唯一确认",
    }
    assert output["external_llm_requests"] == 0
    assert output["external_search_requests"] == 0


def test_v33b_action_is_mutually_exclusive_with_other_actions(capsys) -> None:
    with pytest.raises(SystemExit) as captured:
        cli.main(
            [
                "600998.SH",
                "--extract-financial-statements",
                "--prepare-company",
            ]
        )

    assert captured.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
