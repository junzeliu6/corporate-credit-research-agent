"""V3.3 通用公司资料准备与现有 Skill Runtime 适配。

本模块不联网，也不创建第二套 Agent Loop。它只消费 V3.3A 已完成下载、解析和
主体复核的年报，把动态企业数据绑定到现有两个 Skill 的白名单 Tool。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from credit_research_agent.annual_reports import (
    CompanyOnboardingResult,
    PreparedAnnualReport,
)
from credit_research_agent.company_scope import (
    CompanyScopeAssessment,
    CompanyScopeStatus,
    assess_company_scope,
)
from credit_research_agent.financial import FinancialSourceInput
from credit_research_agent.financial_statements import (
    build_generic_financial_skill_artifacts,
)
from credit_research_agent.fundamental_disclosures import (
    GenericFundamentalArtifacts,
    build_generic_fundamental_artifacts,
)
from credit_research_agent.models import CompanyIdentity, SourceRecord
from credit_research_agent.ownership_disclosures import (
    build_generic_ownership_artifacts,
)
from credit_research_agent.skill_runtime import SkillRuntime, ToolRegistration
from credit_research_agent.sources import SourceCatalog


class GenericResearchPreparationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GenericResearchPreparation:
    """可直接交给现有 Integrated Runtime 的任务级输入。"""

    identity: CompanyIdentity
    primary_report: PreparedAnnualReport
    comparison_inputs: tuple[FinancialSourceInput, ...]
    fundamentals: GenericFundamentalArtifacts
    scope: CompanyScopeAssessment
    source_catalog: SourceCatalog
    source_catalog_path: Path
    onboarding_manifest_path: Path

    @property
    def source(self) -> SourceRecord:
        return self.primary_report.source


def build_generic_skill_runtime(project_root: Path) -> SkillRuntime:
    """保持既有 Skill/Tool 名称，只替换为无公司 Seeds 的执行器。"""

    return SkillRuntime(
        project_root,
        tools={
            "ownership-structure": ToolRegistration(
                name="build_ownership_artifacts",
                skill_name="ownership-structure",
                executor=build_generic_ownership_artifacts,
            ),
            "financial-analysis": ToolRegistration(
                name="build_financial_artifacts",
                skill_name="financial-analysis",
                executor=build_generic_financial_skill_artifacts,
            ),
        },
    )


def prepare_generic_research_inputs(
    *,
    onboarding: CompanyOnboardingResult,
    output_dir: Path,
) -> GenericResearchPreparation:
    """验证任务级年报并生成通用基本面及 Runtime 绑定信息。"""

    identity = onboarding.resolution.identity
    if not re.fullmatch(r"CN-A-(?:SSE|SZSE|BSE)-\d{6}", identity.entity_id):
        raise GenericResearchPreparationError(
            "unsafe_entity_id", "动态企业标识不符合A股任务目录规则"
        )
    if onboarding.status == "failed" or not onboarding.prepared_reports:
        raise GenericResearchPreparationError(
            "no_prepared_annual_report",
            "没有通过下载、解析和主体复核的官方年度报告全文",
        )
    primary_source_id = onboarding.discovery.primary_source_id
    primary = next(
        (
            item
            for item in onboarding.prepared_reports
            if item.source.source_id == primary_source_id
        ),
        None,
    )
    if primary is None:
        raise GenericResearchPreparationError(
            "primary_annual_report_unavailable",
            f"最新年度报告 {primary_source_id} 未通过准备流程，不能回退到其他企业或摘要",
        )
    prepared = list(onboarding.prepared_reports)
    sources = [item.source for item in prepared]
    if len({item.source_id for item in sources}) != len(sources):
        raise GenericResearchPreparationError(
            "duplicate_task_source_id", "任务级官方来源编号重复"
        )
    if any(item.entity_id != identity.entity_id for item in sources):
        raise GenericResearchPreparationError(
            "task_source_entity_mismatch", "任务级年报来源混入其他企业"
        )

    scope = assess_company_scope(pages_path=primary.extraction.pages_path)
    if scope.status is CompanyScopeStatus.UNSUPPORTED_FINANCIAL_ENTERPRISE:
        raise GenericResearchPreparationError(
            "unsupported_financial_enterprise",
            "年报业务及报表特征表明该主体属于当前暂不支持的金融企业",
        )
    if scope.status is CompanyScopeStatus.INDUSTRY_EVIDENCE_CONFLICT:
        raise GenericResearchPreparationError(
            "industry_evidence_conflict", "企业行业证据冲突，需人工确认后才能进入通用管线"
        )
    if scope.status is CompanyScopeStatus.ENTERPRISE_SCOPE_UNCONFIRMED:
        raise GenericResearchPreparationError(
            "enterprise_scope_unconfirmed",
            "尚不能确认该企业适用非金融企业通用报表管线",
        )

    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    entity_root = (root / identity.entity_id).resolve()
    try:
        entity_root.relative_to(root)
    except ValueError as exc:  # pragma: no cover - 正则门禁后的防御分支
        raise GenericResearchPreparationError(
            "unsafe_output_path", "企业任务目录超出指定输出根目录"
        ) from exc
    source_catalog = SourceCatalog(sources)
    source_catalog_path = source_catalog.write(entity_root / "task_sources.json")
    fundamentals = build_generic_fundamental_artifacts(
        identity=identity,
        source=primary.source,
        download=primary.download,
        summary_path=primary.extraction.summary_path,
        pages_path=primary.extraction.pages_path,
        output_dir=entity_root / "prepared",
    )
    comparisons = tuple(
        FinancialSourceInput(
            source=item.source,
            download=item.download,
            summary_path=item.extraction.summary_path,
            pages_path=item.extraction.pages_path,
        )
        for item in prepared
        if item.source.source_id != primary_source_id
    )
    return GenericResearchPreparation(
        identity=identity,
        primary_report=primary,
        comparison_inputs=comparisons,
        fundamentals=fundamentals,
        scope=scope,
        source_catalog=source_catalog,
        source_catalog_path=source_catalog_path,
        onboarding_manifest_path=Path(onboarding.manifest_path).resolve(),
    )

