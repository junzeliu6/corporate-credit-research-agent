from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from credit_research_agent.reporting import (
    AnalysisValidationStatus,
    ReportBuildError,
    ValidatedAgentAnalysis,
    assemble_report_artifacts,
    render_tiered_web_research_report,
)
from credit_research_agent.company import CompanyResolver
from credit_research_agent.models import (
    EntityScope,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
)
from credit_research_agent.web_sources import (
    WebEvidenceCandidate,
    WebEvidenceExtraction,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNDAMENTALS_PATH = PROJECT_ROOT / "examples" / "S01_fundamentals_sample.json"
OWNERSHIP_DIR = (
    PROJECT_ROOT
    / "examples"
    / "v1_3b_source_bound_live"
    / "CN-A-SZSE-002603"
)
FIXED_TIME = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _build_report(
    tmp_path: Path,
    *,
    analysis: ValidatedAgentAnalysis | None = None,
    fundamentals_path: Path = FUNDAMENTALS_PATH,
    graph_path: Path | None = None,
    web_evidence_path: Path | None = None,
):
    return assemble_report_artifacts(
        fundamentals_path=fundamentals_path,
        ownership_dataset_path=OWNERSHIP_DIR / "ownership.json",
        ownership_graph_path=graph_path or OWNERSHIP_DIR / "ownership_graph.json",
        ownership_validation_path=OWNERSHIP_DIR / "ownership_validation.json",
        ownership_svg_path=OWNERSHIP_DIR / "ownership_structure.svg",
        ownership_png_path=OWNERSHIP_DIR / "ownership_structure.png",
        report_path=tmp_path / "report" / "credit_research_report.md",
        analysis=analysis,
        web_evidence_path=web_evidence_path,
        clock=lambda: FIXED_TIME,
    )


def _tiered_web_extraction() -> WebEvidenceExtraction:
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    sources = [
        SourceRecord(
            source_id="S05",
            entity_id=identity.entity_id,
            title="公司官网企业介绍",
            source_type=SourceType.COMPANY_WEBSITE,
            publisher="石家庄以岭药业股份有限公司",
            url="https://www.yiling.cn/about",
            published_at=date(2026, 1, 1),
            trusted_tier=1,
            status=SourceStatus.PARSED,
        ),
        SourceRecord(
            source_id="S06",
            entity_id=identity.entity_id,
            title="第三方业务报道",
            source_type=SourceType.WEB_PAGE,
            publisher="example.com",
            url="https://example.com/article",
            published_at=date(2026, 1, 2),
            trusted_tier=3,
            status=SourceStatus.PARSED,
        ),
        SourceRecord(
            source_id="S07",
            entity_id=identity.entity_id,
            title="未读取候选页面",
            source_type=SourceType.WEB_PAGE,
            publisher="candidate.example",
            url="https://candidate.example/item",
            published_at=date(2026, 1, 3),
            trusted_tier=3,
            status=SourceStatus.FAILED,
        ),
    ]
    evidence = [
        EvidenceRecord(
            evidence_id="E901",
            source_id="S05",
            source_sha256="a" * 64,
            page_number=1,
            line_number=1,
            page_or_section="网页段落P001",
            quote="公司官网披露主营业务示例。",
            status=EvidenceStatus.VERIFIED,
            verification_method=VerificationMethod.EXACT_TEXT_MATCH,
        ),
        EvidenceRecord(
            evidence_id="E902",
            source_id="S06",
            source_sha256="b" * 64,
            page_number=1,
            line_number=2,
            page_or_section="网页段落P002",
            quote="第三方原文披露主要产品示例。",
            status=EvidenceStatus.SINGLE_SOURCE,
            verification_method=VerificationMethod.EXACT_TEXT_MATCH,
        ),
        EvidenceRecord(
            evidence_id="E903",
            source_id="S07",
            source_sha256="c" * 64,
            page_number=1,
            line_number=1,
            page_or_section="百度搜索结果摘要（未读取原文）",
            quote="某来源搜索摘要中的产品线索。",
            status=EvidenceStatus.UNVERIFIED,
            verification_method=VerificationMethod.SEARCH_SNIPPET,
        ),
    ]
    facts = [
        FactRecord(
            fact_id="F901",
            entity_id=identity.entity_id,
            category=FactCategory.BUSINESS,
            research_module=ResearchModule.MAIN_BUSINESS,
            field_name="verified_business",
            value="公司官网披露主营业务示例。",
            claim="公司官网披露主营业务示例。",
            entity_scope=EntityScope.CONSOLIDATED,
            evidence_ids=["E901"],
        ),
        FactRecord(
            fact_id="F902",
            entity_id=identity.entity_id,
            category=FactCategory.BUSINESS,
            research_module=ResearchModule.MAIN_PRODUCTS,
            field_name="single_source_product",
            value="第三方原文披露主要产品示例。",
            claim="第三方原文披露主要产品示例。",
            entity_scope=EntityScope.CONSOLIDATED,
            evidence_ids=["E902"],
        ),
    ]
    return WebEvidenceExtraction(
        identity=identity,
        research_question="主营业务与主要产品",
        generated_at=FIXED_TIME,
        facts=facts,
        evidence=evidence,
        candidates=[
            WebEvidenceCandidate(
                candidate_id="CAND001",
                entity_id=identity.entity_id,
                source_id="S07",
                evidence_id="E903",
                research_question="主要产品",
                excerpt="某来源搜索摘要中的产品线索。",
                verification_status=EvidenceStatus.UNVERIFIED,
                reason="source_fetch_failed:access_restricted",
            )
        ],
        sources=sources,
    )


def test_report_contains_required_sections_references_and_image(tmp_path: Path) -> None:
    artifacts = _build_report(tmp_path)
    content = artifacts.report_path.read_text(encoding="utf-8")

    assert artifacts.section_count == 8
    assert artifacts.fact_count == 21
    assert artifacts.missing_information_count == 5
    assert artifacts.source_ids == ["S01"]
    assert artifacts.analysis_status is AnalysisValidationStatus.NOT_RUN
    assert artifacts.ownership_svg_path is not None
    assert artifacts.ownership_svg_path.is_file()
    assert artifacts.ownership_png_path is not None
    assert artifacts.ownership_png_path.is_file()
    for heading in (
        "## 1. 企业基本信息",
        "## 2. 企业发展历史",
        "## 3. 主营业务与经营模式",
        "## 4. 主要产品",
        "## 5. 股权结构图",
        "## 6. 主要股东及实际控制关系",
        "## 7. 待核查事项",
        "## 8. 数据来源及引用附录",
    ):
        assert heading in content
    assert "![以岭药业股权结构图](assets/ownership_structure.svg)" in content
    assert "以岭医药科技有限公司：31.53%" in content
    assert "吴以岭、吴相君、吴瑞为石家庄以岭药业股份有限公司的实际控制人" in content
    assert "[S01]" in content
    assert "https://static.cninfo.com.cn/finalpage/2026-04-28/1225205683.PDF" in content
    assert "本次报告未调用 LLM" in content
    assert "财务分析尚未进入 V2" in content


def test_rejected_agent_analysis_does_not_block_or_leak_candidate(
    tmp_path: Path,
) -> None:
    analysis = ValidatedAgentAnalysis(
        status=AnalysisValidationStatus.REJECTED,
        validation_note="ANALYSIS-CONTROL-001：控制关系缺少Fact ID",
    )

    artifacts = _build_report(tmp_path, analysis=analysis)
    content = artifacts.report_path.read_text(encoding="utf-8")

    assert artifacts.analysis_status is AnalysisValidationStatus.REJECTED
    assert "本次 AI 分析未通过事实校验，未纳入正式结论" in content
    assert "ANALYSIS-CONTROL-001" in content
    assert "候选模型正文" not in content
    assert "## 8. 数据来源及引用附录" in content


def test_validated_agent_analysis_can_be_included(tmp_path: Path) -> None:
    analysis = ValidatedAgentAnalysis(
        status=AnalysisValidationStatus.VALIDATED,
        text="经核验的股权分析段落。[S01]",
        source_ids=["S01"],
        validation_note="已通过事实校验。",
    )

    artifacts = _build_report(tmp_path, analysis=analysis)
    content = artifacts.report_path.read_text(encoding="utf-8")

    assert artifacts.analysis_status is AnalysisValidationStatus.VALIDATED
    assert "经核验的股权分析段落。[S01]" in content
    assert "以上 AI 辅助分析已通过事实校验" in content


def test_unknown_agent_analysis_source_is_rejected(tmp_path: Path) -> None:
    analysis = ValidatedAgentAnalysis(
        status=AnalysisValidationStatus.VALIDATED,
        text="经核验的股权分析段落。[S99]",
        source_ids=["S99"],
    )

    with pytest.raises(ReportBuildError) as exc_info:
        _build_report(tmp_path, analysis=analysis)

    assert exc_info.value.code == "unknown_analysis_source"


def test_entity_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(FUNDAMENTALS_PATH.read_text(encoding="utf-8"))
    payload["identity"]["entity_id"] = "CN-A-SZSE-000001"
    mismatched_path = tmp_path / "mismatched_fundamentals.json"
    mismatched_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ReportBuildError) as exc_info:
        _build_report(tmp_path, fundamentals_path=mismatched_path)

    assert exc_info.value.code == "report_entity_mismatch"


def test_non_formal_graph_generates_partial_report_without_image(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        (OWNERSHIP_DIR / "ownership_graph.json").read_text(encoding="utf-8")
    )
    payload["formal_ready"] = False
    graph_path = tmp_path / "partial_graph.json"
    graph_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    artifacts = _build_report(tmp_path, graph_path=graph_path)
    content = artifacts.report_path.read_text(encoding="utf-8")

    assert artifacts.ownership_svg_path is None
    assert artifacts.ownership_png_path is None
    assert "当前股权数据未达到正式出图条件" in content


def test_successful_agent_result_adapter_requires_source_markers() -> None:
    analysis = ValidatedAgentAnalysis.from_successful_agent_result(
        SimpleNamespace(final_output="经事实校验的说明。[S01]")
    )

    assert analysis.status is AnalysisValidationStatus.VALIDATED
    assert analysis.source_ids == ["S01"]

    with pytest.raises(ReportBuildError) as exc_info:
        ValidatedAgentAnalysis.from_successful_agent_result(
            SimpleNamespace(final_output="没有来源引用的文字")
        )
    assert exc_info.value.code == "validated_agent_output_without_source"


def test_rejected_analysis_cannot_carry_candidate_text() -> None:
    with pytest.raises(ValueError, match="候选文本不得进入报告输入"):
        ValidatedAgentAnalysis(
            status=AnalysisValidationStatus.REJECTED,
            text="候选模型正文",
            validation_note="未通过事实校验",
        )


def test_tiered_web_report_keeps_verified_single_and_unverified_separate(
    tmp_path: Path,
) -> None:
    extraction = _tiered_web_extraction()
    markdown = render_tiered_web_research_report(extraction)

    assert "【已核验】公司官网披露主营业务示例" in markdown
    assert "【单一来源，待交叉核实】第三方原文披露主要产品示例" in markdown
    assert "【未核验】某来源称：某来源搜索摘要中的产品线索" in markdown
    assert "unsupported 内容不会写入报告正文" in markdown

    web_path = tmp_path / "web_evidence.json"
    web_path.write_text(extraction.json(ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = _build_report(tmp_path, web_evidence_path=web_path)
    content = artifacts.report_path.read_text(encoding="utf-8")
    assert "### 4.1 外部公开资料补充（按核验状态）" in content
    assert "【已核验】公司官网披露主营业务示例" in content
    assert "【单一来源，待交叉核实】" in content
    assert "【未核验】某来源称" in content
    assert {"S05", "S06", "S07"}.issubset(artifacts.source_ids)
