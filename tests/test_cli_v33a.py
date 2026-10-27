"""V3.3A 独立CLI入口的离线回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic.v1 import BaseModel

from credit_research_agent import cli
from credit_research_agent.models import CompanyIdentity, EntityScope
from credit_research_agent.security_master import (
    DynamicCompanyResolution,
    SecurityMasterCacheStatus,
    SecurityMasterError,
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


class _FakeOnboarding(BaseModel):
    status: str = "discovered"
    marker: str = "offline"


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
    def __init__(self, calls: dict[str, object], *, status: str = "discovered") -> None:
        self.calls = calls
        self.status = status

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
        return _FakeOnboarding(status=self.status)


def _fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("V3.3A独立入口不应初始化LLM、搜索或其他运行时")


def test_resolve_company_returns_before_llm_search_and_onboarding(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: dict[str, object] = {}
    expected_cache = tmp_path / "security-master.json"

    def build_resolver(path: Path) -> _FakeResolver:
        calls["cache_path"] = path
        return _FakeResolver(calls)

    monkeypatch.setattr(cli, "_build_dynamic_company_resolver", build_resolver)
    monkeypatch.setattr(cli, "_build_company_onboarding_service", _fail_if_called)
    monkeypatch.setattr(cli, "load_deepseek_config", _fail_if_called)
    monkeypatch.setattr(cli, "load_baidu_search_config", _fail_if_called)

    exit_code = cli.main(
        [
            "600998.SH",
            "--resolve-company",
            "--security-master-cache",
            str(expected_cache),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls == {
        "cache_path": expected_cache,
        "resolve_query": "600998.SH",
        "resolve_force_refresh": False,
    }
    assert output["company"]["ticker"] == "600998.SH"
    assert output["dynamic_resolution"]["resolution_source"] == "official_security_master"
    assert output["external_llm_requests"] == 0
    assert output["external_search_requests"] == 0


def test_discover_annual_reports_passes_discovery_only_and_default_count(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_build_dynamic_company_resolver",
        lambda path: _FakeResolver(calls),
    )
    monkeypatch.setattr(
        cli,
        "_build_company_onboarding_service",
        lambda: _FakeOnboardingService(calls),
    )

    exit_code = cli.main(
        [
            "600998",
            "--discover-annual-reports",
            "--onboarding-dir",
            str(tmp_path / "onboarding"),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls["prepare_output_root"] == tmp_path / "onboarding"
    assert calls["prepare_report_count"] == 2
    assert calls["prepare_force_refresh"] is False
    assert calls["prepare_discover_only"] is True
    assert output["onboarding"]["status"] == "discovered"


def test_prepare_company_passes_explicit_count_and_refresh(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_build_dynamic_company_resolver",
        lambda path: _FakeResolver(calls),
    )
    monkeypatch.setattr(
        cli,
        "_build_company_onboarding_service",
        lambda: _FakeOnboardingService(calls, status="success"),
    )

    exit_code = cli.main(
        [
            "600998.SH",
            "--prepare-company",
            "--onboarding-dir",
            str(tmp_path / "prepared"),
            "--annual-report-count",
            "3",
            "--refresh-official-data",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert calls["resolve_force_refresh"] is True
    assert calls["prepare_output_root"] == tmp_path / "prepared"
    assert calls["prepare_report_count"] == 3
    assert calls["prepare_force_refresh"] is True
    assert calls["prepare_discover_only"] is False
    assert output["onboarding"]["status"] == "success"


def test_failed_onboarding_returns_exit_15(monkeypatch, capsys) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_build_dynamic_company_resolver",
        lambda path: _FakeResolver(calls),
    )
    monkeypatch.setattr(
        cli,
        "_build_company_onboarding_service",
        lambda: _FakeOnboardingService(calls, status="failed"),
    )

    exit_code = cli.main(["600998.SH", "--prepare-company"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 15
    assert output["onboarding"]["status"] == "failed"


def test_dynamic_resolution_error_is_structured_and_does_not_fall_through(
    monkeypatch, capsys
) -> None:
    class FailingResolver:
        def resolve_with_metadata(self, query: str, *, force_refresh: bool = False):
            raise SecurityMasterError("official_source_unavailable", "模拟官方源不可用")

    monkeypatch.setattr(
        cli,
        "_build_dynamic_company_resolver",
        lambda path: FailingResolver(),
    )
    monkeypatch.setattr(cli, "_build_company_onboarding_service", _fail_if_called)

    exit_code = cli.main(["600998.SH", "--resolve-company"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 15
    assert output["onboarding_error"] == {
        "code": "official_source_unavailable",
        "message": "模拟官方源不可用",
    }
    assert output["external_llm_requests"] == 0
    assert output["external_search_requests"] == 0


def test_v33a_actions_are_mutually_exclusive(capsys) -> None:
    with pytest.raises(SystemExit) as captured:
        cli.main(
            [
                "600998.SH",
                "--resolve-company",
                "--prepare-company",
            ]
        )

    assert captured.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err

