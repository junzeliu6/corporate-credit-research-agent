"""V1.4 确定性 Markdown 授信研究报告组装。

报告层只读取已经通过现有构建器校验的结构化事实和股权产物。LLM 分析是
可选增强：只有成功经过 Agent Runtime 事实校验的文字才能进入报告；未运行或
被拒绝时，报告仍使用确定性事实正常生成。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from pydantic.v1 import BaseModel, Field, root_validator

from credit_research_agent.documents import DownloadResult
from credit_research_agent.fundamentals import (
    FundamentalArtifacts,
    build_fundamental_artifacts,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EvidenceRecord,
    EvidenceStatus,
    FactRecord,
    FundamentalResearchBundle,
    MissingInformation,
    ResearchModule,
    SourceRecord,
)
from credit_research_agent.ownership import (
    ControlRelationType,
    OwnershipArtifacts,
    OwnershipCompletenessStatus,
    OwnershipDataset,
    OwnershipGraph,
    OwnershipPercentageValidationResult,
    OwnershipRelationType,
    PercentageOrigin,
    build_ownership_artifacts,
)
from credit_research_agent.web_sources import WebEvidenceExtraction


REPORT_SECTION_COUNT = 8


class ReportBuildError(RuntimeError):
    """报告输入不一致或产物无法安全写入。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AnalysisValidationStatus(str, Enum):
    """可选 LLM 分析在报告层的交付状态。"""

    NOT_RUN = "not_run"
    VALIDATED = "validated"
    REJECTED = "rejected"


class ValidatedAgentAnalysis(BaseModel):
    """报告可消费的 LLM 分析契约。

    `validated` 只能由已经成功返回 `AgentRunResult` 的调用方构造；被事实校验拒绝的
   候选文本不得保存在 `text` 中，防止后续误写入正式报告。
    """

    status: AnalysisValidationStatus = AnalysisValidationStatus.NOT_RUN
    text: Optional[str] = None
    source_ids: List[str] = Field(default_factory=list)
    validation_note: Optional[str] = None

    @root_validator
    def validate_delivery_contract(cls, values):
        status = values.get("status")
        text = values.get("text")
        source_ids = values.get("source_ids") or []
        validation_note = values.get("validation_note")
        if status is AnalysisValidationStatus.VALIDATED:
            if not text or not text.strip():
                raise ValueError("通过校验的 Agent 分析必须包含正文")
            if not source_ids:
                raise ValueError("通过校验的 Agent 分析必须至少关联一个来源")
        else:
            if text:
                raise ValueError("未通过校验的 Agent 候选文本不得进入报告输入")
            if status is AnalysisValidationStatus.REJECTED and not validation_note:
                raise ValueError("被拒绝的 Agent 分析必须记录校验原因")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Agent 分析的 source_ids 不得重复")
        return values

    @classmethod
    def from_successful_agent_result(cls, result: Any) -> "ValidatedAgentAnalysis":
        """把已通过 Agent Runtime 后置校验的结果转换为报告输入。"""

        text = str(getattr(result, "final_output", "")).strip()
        if not text:
            raise ReportBuildError(
                "empty_validated_agent_output", "Agent 成功结果没有可交付正文"
            )
        source_ids = sorted(set(re.findall(r"\[(S\d{2})\]", text)))
        if not source_ids:
            raise ReportBuildError(
                "validated_agent_output_without_source",
                "Agent 成功结果没有 [Sxx] 来源引用，不能纳入正式报告",
            )
        return cls(
            status=AnalysisValidationStatus.VALIDATED,
            text=text,
            source_ids=source_ids,
            validation_note="已通过 Agent Runtime 事实与来源校验。",
        )


class ReportArtifacts(BaseModel):
    """一次 V1.4 报告构建的可审计产物。"""

    entity_id: str
    report_path: Path
    ownership_svg_path: Optional[Path] = None
    ownership_png_path: Optional[Path] = None
    fundamentals_path: Optional[Path] = None
    ownership_dataset_path: Optional[Path] = None
    ownership_graph_path: Optional[Path] = None
    ownership_validation_path: Optional[Path] = None
    source_ids: List[str]
    section_count: int = REPORT_SECTION_COUNT
    fact_count: int
    missing_information_count: int
    analysis_status: AnalysisValidationStatus


def _load_model(path: Path, model_type):
    try:
        return model_type.parse_raw(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportBuildError(
            "invalid_report_input",
            f"无法读取或校验报告输入 {path}：{exc}",
        ) from exc


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8", newline="\n")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ReportBuildError("missing_report_asset", f"报告图形产物不存在：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copyfile(source, temp_path)
        temp_path.replace(destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _source_identity(source: SourceRecord) -> tuple:
    return (
        source.entity_id,
        source.title,
        source.source_type.value,
        source.publisher,
        str(source.url),
        source.published_at,
        source.report_period,
    )


def _merge_sources(*groups: Sequence[SourceRecord]) -> Dict[str, SourceRecord]:
    merged: Dict[str, SourceRecord] = {}
    for source in (item for group in groups for item in group):
        current = merged.get(source.source_id)
        if current is not None and _source_identity(current) != _source_identity(source):
            raise ReportBuildError(
                "conflicting_report_source",
                f"来源 {source.source_id} 在不同研究产物中的元数据不一致",
            )
        merged[source.source_id] = source
    return dict(sorted(merged.items()))


def _evidence_map(*groups: Sequence[EvidenceRecord]) -> Dict[str, EvidenceRecord]:
    result: Dict[str, EvidenceRecord] = {}
    for evidence in (item for group in groups for item in group):
        current = result.get(evidence.evidence_id)
        if current is not None and current.dict() != evidence.dict():
            raise ReportBuildError(
                "conflicting_report_evidence",
                f"证据 {evidence.evidence_id} 在不同研究产物中的内容不一致",
            )
        result[evidence.evidence_id] = evidence
    return result


def _source_ids_for_evidence(
    evidence_ids: Iterable[str], evidence_by_id: Dict[str, EvidenceRecord]
) -> List[str]:
    source_ids: Set[str] = set()
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ReportBuildError(
                "dangling_report_evidence", f"报告事实引用了不存在的证据 {evidence_id}"
            )
        source_ids.add(evidence.source_id)
    return sorted(source_ids)


def _source_markers(source_ids: Iterable[str]) -> str:
    return " ".join(f"[{source_id}]" for source_id in sorted(set(source_ids)))


def _fact_markers(
    fact: FactRecord, evidence_by_id: Dict[str, EvidenceRecord]
) -> str:
    return _source_markers(_source_ids_for_evidence(fact.evidence_ids, evidence_by_id))


def _format_percentage(value: Optional[Decimal]) -> str:
    if value is None:
        return "比例未披露"
    return f"{value.quantize(Decimal('0.01'))}%"


def _normalise_sentence(value: str) -> str:
    return " ".join(value.split())


def _module_facts(
    bundle: FundamentalResearchBundle, module: ResearchModule
) -> List[FactRecord]:
    return [fact for fact in bundle.facts if fact.research_module is module]


def _render_fact_list(
    facts: Sequence[FactRecord], evidence_by_id: Dict[str, EvidenceRecord]
) -> List[str]:
    if not facts:
        return ["- 当前已核验证据未覆盖该项，待补充公开资料。"]
    return [
        f"- {_normalise_sentence(fact.claim)} {_fact_markers(fact, evidence_by_id)}"
        for fact in facts
    ]


def _fact_evidence_status(
    fact: FactRecord, evidence_by_id: Dict[str, EvidenceRecord]
) -> EvidenceStatus:
    statuses = {
        evidence_by_id[evidence_id].status
        for evidence_id in fact.evidence_ids
        if evidence_id in evidence_by_id
    }
    if EvidenceStatus.CONFLICTING in statuses:
        return EvidenceStatus.CONFLICTING
    if EvidenceStatus.UNSUPPORTED in statuses or not statuses:
        return EvidenceStatus.UNSUPPORTED
    if EvidenceStatus.UNVERIFIED in statuses:
        return EvidenceStatus.UNVERIFIED
    if EvidenceStatus.SINGLE_SOURCE in statuses:
        return EvidenceStatus.SINGLE_SOURCE
    return EvidenceStatus.VERIFIED


def render_tiered_web_research_section(
    extraction: WebEvidenceExtraction,
) -> List[str]:
    """按可信度确定性渲染V1.5资料，不让LLM自行提升核验等级。"""

    evidence_by_id = {item.evidence_id: item for item in extraction.evidence}
    verified: List[FactRecord] = []
    single_source: List[FactRecord] = []
    for fact in extraction.facts:
        status = _fact_evidence_status(fact, evidence_by_id)
        if status is EvidenceStatus.VERIFIED:
            verified.append(fact)
        elif status is EvidenceStatus.SINGLE_SOURCE:
            single_source.append(fact)

    lines = ["### 4.1 外部公开资料补充（按核验状态）", ""]
    lines.extend(["#### 已核验", ""])
    if verified:
        lines.extend(
            f"- 【已核验】{_normalise_sentence(item.claim)} "
            f"{_fact_markers(item, evidence_by_id)}"
            for item in verified
        )
    else:
        lines.append("- 无新增已核验外部事实。")

    lines.extend(["", "#### 单一来源", ""])
    if single_source:
        lines.extend(
            f"- 【单一来源，待交叉核实】{_normalise_sentence(item.claim)} "
            f"{_fact_markers(item, evidence_by_id)}"
            for item in single_source
        )
    else:
        lines.append("- 无。")

    lines.extend(["", "#### 未核验候选信息", ""])
    unverified_candidates = [
        item
        for item in extraction.candidates
        if item.verification_status is EvidenceStatus.UNVERIFIED
    ]
    if unverified_candidates:
        for item in unverified_candidates:
            if item.reason == "prompt_injection_detected":
                statement = "该页面包含疑似指令注入内容，正文未采信。"
            else:
                statement = f"某来源称：{_normalise_sentence(item.excerpt)}"
            lines.append(
                f"- 【未核验】{statement} [{item.source_id}]"
                f"（Candidate ID: {item.candidate_id}；{item.reason}）"
            )
    else:
        lines.append("- 无。")

    lines.extend(["", "#### 来源冲突", ""])
    if extraction.conflicts:
        for item in extraction.conflicts:
            paired = "；".join(
                f"[{source_id}] {value}"
                for source_id, value in zip(item.source_ids, item.values)
            )
            lines.append(
                f"- 【来源冲突】{item.field_name}：{paired}；未选择任一值作为事实。"
            )
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "> 完全缺乏来源或由模型自行创造的 unsupported 内容不会写入报告正文。",
        ]
    )
    return lines


def render_tiered_web_research_report(
    extraction: WebEvidenceExtraction,
    *,
    title: str = "公开资料可信度分层示例报告",
    run_status: Optional[str] = None,
    limitations: Sequence[str] = (),
) -> str:
    """生成可独立查看的Markdown示例/局部报告。"""

    lines = [
        f"# {title}",
        "",
        "> 本报告按证据状态分层；单一来源和未核验候选不得等同于已核验事实。",
        "",
    ]
    if run_status:
        lines.extend([f"- 执行状态：`{run_status}`", ""])
    lines.extend(render_tiered_web_research_section(extraction))
    lines.extend(["", "## 待核实事项与未完成检索", ""])
    combined_limitations = list(
        dict.fromkeys([*extraction.warnings, *[item for item in limitations if item]])
    )
    if not extraction.facts and not extraction.candidates:
        lines.append("- 当前没有可用事实或候选信息，资料不足，不能形成企业事实结论。")
    if combined_limitations:
        lines.extend(f"- {item}" for item in combined_limitations)
    elif extraction.facts or extraction.candidates:
        lines.append("- 无新增待核实事项。")
    lines.extend(["", "## 来源", ""])
    for source in sorted(extraction.sources, key=lambda item: item.source_id):
        lines.append(
            f"- [{source.source_id}] {source.title}；发布者：{source.publisher}；"
            f"状态：{source.status.value}；原文：{source.url}"
        )
    if not extraction.sources:
        lines.append("- 无。")
    return "\n".join(lines) + "\n"


def write_tiered_web_research_report(
    extraction: WebEvidenceExtraction,
    *,
    report_path: Path,
    title: str,
    run_status: str,
    limitations: Sequence[str] = (),
) -> Path:
    """确定性写入V1.5局部Markdown；即使资料不足也保留报告框架。"""

    report_path = Path(report_path)
    _atomic_write_text(
        report_path,
        render_tiered_web_research_report(
            extraction,
            title=title,
            run_status=run_status,
            limitations=limitations,
        ),
    )
    return report_path.resolve()


def _control_relation_label(value: ControlRelationType) -> str:
    return {
        ControlRelationType.CONTROLLING_SHAREHOLDER: "控股股东",
        ControlRelationType.ACTUAL_CONTROLLER: "实际控制人",
        ControlRelationType.ULTIMATE_CONTROLLER: "最终控制方",
        ControlRelationType.ACTING_IN_CONCERT: "一致行动关系",
        ControlRelationType.KEY_CONTROL_SUBJECT: "重要控制主体",
    }[value]


def _branch_status_label(value: OwnershipCompletenessStatus) -> str:
    return {
        OwnershipCompletenessStatus.COMPLETE: "通过",
        OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL: "通过（含经核验差额计算）",
        OwnershipCompletenessStatus.INCOMPLETE: "信息不完整",
        OwnershipCompletenessStatus.OVER_ALLOCATED: "合计超过100%",
        OwnershipCompletenessStatus.INCOMPARABLE_SCOPE: "口径不可比",
        OwnershipCompletenessStatus.UNVERIFIABLE: "无法核验",
    }[value]


def _relative_markdown_path(target: Path, report_path: Path) -> str:
    return Path(os.path.relpath(target, report_path.parent)).as_posix()


class MarkdownReportBuilder:
    """把 V1.1/V1.2 的结构化产物组装为八章节 Markdown 报告。"""

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        fundamentals: FundamentalResearchBundle,
        ownership: OwnershipDataset,
        graph: OwnershipGraph,
        percentage_validation: OwnershipPercentageValidationResult,
        ownership_svg_path: Path,
        ownership_png_path: Optional[Path],
        report_path: Path,
        analysis: Optional[ValidatedAgentAnalysis] = None,
        web_research: Optional[WebEvidenceExtraction] = None,
    ) -> ReportArtifacts:
        analysis = analysis or ValidatedAgentAnalysis()
        identity = fundamentals.identity
        self._validate_inputs(
            identity=identity,
            ownership=ownership,
            graph=graph,
            percentage_validation=percentage_validation,
            analysis=analysis,
            web_research=web_research,
        )

        sources = _merge_sources(
            fundamentals.sources,
            ownership.sources,
            graph.sources,
            web_research.sources if web_research else [],
        )
        evidence_by_id = _evidence_map(
            fundamentals.evidence,
            ownership.evidence,
            graph.evidence,
            web_research.evidence if web_research else [],
        )
        unknown_analysis_sources = set(analysis.source_ids) - set(sources)
        if unknown_analysis_sources:
            raise ReportBuildError(
                "unknown_analysis_source",
                f"Agent 分析引用了报告中不存在的来源：{sorted(unknown_analysis_sources)}",
            )

        report_path = Path(report_path)
        copied_svg: Optional[Path] = None
        copied_png: Optional[Path] = None
        if graph.formal_ready:
            copied_svg = report_path.parent / "assets" / "ownership_structure.svg"
            _atomic_copy(Path(ownership_svg_path), copied_svg)
            if ownership_png_path is not None:
                copied_png = report_path.parent / "assets" / "ownership_structure.png"
                _atomic_copy(Path(ownership_png_path), copied_png)

        content = self._render(
            identity=identity,
            fundamentals=fundamentals,
            ownership=ownership,
            graph=graph,
            percentage_validation=percentage_validation,
            sources=sources,
            evidence_by_id=evidence_by_id,
            report_path=report_path,
            copied_svg=copied_svg,
            analysis=analysis,
            web_research=web_research,
        )
        _atomic_write_text(report_path, content)
        missing_count = len(fundamentals.missing_information) + len(
            ownership.missing_information
        )
        return ReportArtifacts(
            entity_id=identity.entity_id,
            report_path=report_path.resolve(),
            ownership_svg_path=copied_svg.resolve() if copied_svg else None,
            ownership_png_path=copied_png.resolve() if copied_png else None,
            fundamentals_path=None,
            ownership_dataset_path=None,
            ownership_graph_path=None,
            ownership_validation_path=None,
            source_ids=list(sources),
            fact_count=len(fundamentals.facts)
            + len(web_research.facts if web_research else []),
            missing_information_count=missing_count,
            analysis_status=analysis.status,
        )

    @staticmethod
    def _validate_inputs(
        *,
        identity: CompanyIdentity,
        ownership: OwnershipDataset,
        graph: OwnershipGraph,
        percentage_validation: OwnershipPercentageValidationResult,
        analysis: ValidatedAgentAnalysis,
        web_research: Optional[WebEvidenceExtraction],
    ) -> None:
        entity_ids = {
            identity.entity_id,
            ownership.identity.entity_id,
            graph.identity.entity_id,
        }
        if len(entity_ids) != 1:
            raise ReportBuildError(
                "report_entity_mismatch", f"报告输入主体不一致：{sorted(entity_ids)}"
            )
        if web_research is not None and web_research.identity.entity_id != identity.entity_id:
            raise ReportBuildError(
                "report_entity_mismatch",
                "外部检索证据与报告主体不一致",
            )
        if ownership.as_of_date != graph.as_of_date:
            raise ReportBuildError(
                "ownership_date_mismatch", "股权原始数据与展示图基准日不一致"
            )
        if graph.percentage_validation.dict() != percentage_validation.dict():
            raise ReportBuildError(
                "ownership_validation_mismatch", "股权图与比例校验产物不一致"
            )
        if graph.formal_ready and not percentage_validation.is_complete:
            raise ReportBuildError(
                "formal_graph_without_complete_validation",
                "股权比例未完整通过时不能在正式报告中嵌入正式图",
            )
        if analysis.status is AnalysisValidationStatus.VALIDATED and not analysis.text:
            raise ReportBuildError(
                "invalid_validated_analysis", "通过状态的 Agent 分析缺少正文"
            )

    def _render(
        self,
        *,
        identity: CompanyIdentity,
        fundamentals: FundamentalResearchBundle,
        ownership: OwnershipDataset,
        graph: OwnershipGraph,
        percentage_validation: OwnershipPercentageValidationResult,
        sources: Dict[str, SourceRecord],
        evidence_by_id: Dict[str, EvidenceRecord],
        report_path: Path,
        copied_svg: Optional[Path],
        analysis: ValidatedAgentAnalysis,
        web_research: Optional[WebEvidenceExtraction],
    ) -> str:
        generated_at = self._clock()
        identity_facts = _module_facts(fundamentals, ResearchModule.COMPANY_IDENTITY)
        identity_sources = sorted(
            {
                source_id
                for fact in identity_facts
                for source_id in _source_ids_for_evidence(
                    fact.evidence_ids, evidence_by_id
                )
            }
        )
        identity_refs = _source_markers(identity_sources)
        lines = [
            f"# {identity.legal_name}公开信息授信研究报告（初稿）",
            "",
            f"> 证券代码：`{identity.ticker}`　|　报告生成时间：{generated_at.isoformat()}",
            "> 本报告仅基于公开信息，未经企业尽调确认，不构成授信审批、额度或定价建议。",
            "> 当前版本不包含财务分析和行业政策研究；相关内容将在 V2 阶段补充。",
            "",
            "## 1. 企业基本信息",
            "",
            "| 项目 | 内容 |",
            "| --- | --- |",
            f"| 上市主体 | {identity.legal_name} {identity_refs} |",
            f"| 证券简称 | {identity.short_name} {identity_refs} |",
            f"| 证券代码 | {identity.ticker} {identity_refs} |",
            f"| 上市地点 | {identity.exchange} {identity_refs} |",
            "| 研究口径 | 上市公司合并口径 |",
            "",
            "## 2. 企业发展历史",
            "",
            *_render_fact_list(
                _module_facts(fundamentals, ResearchModule.DEVELOPMENT_HISTORY),
                evidence_by_id,
            ),
            "",
            "## 3. 主营业务与经营模式",
            "",
            "### 3.1 主营业务",
            "",
            *_render_fact_list(
                _module_facts(fundamentals, ResearchModule.MAIN_BUSINESS),
                evidence_by_id,
            ),
            "",
            "### 3.2 经营模式",
            "",
            *_render_fact_list(
                _module_facts(fundamentals, ResearchModule.OPERATING_MODEL),
                evidence_by_id,
            ),
            "",
            "## 4. 主要产品",
            "",
            *_render_fact_list(
                _module_facts(fundamentals, ResearchModule.MAIN_PRODUCTS),
                evidence_by_id,
            ),
            "",
        ]
        if web_research is not None:
            lines.extend([*render_tiered_web_research_section(web_research), ""])
        lines.extend(
            [
                "## 5. 股权结构图",
                "",
                f"股权结构基准日为 **{ownership.as_of_date.isoformat()}**。",
                "",
            ]
        )
        if copied_svg is not None:
            relative_svg = _relative_markdown_path(copied_svg, report_path)
            lines.extend(
                [
                    f"![{identity.short_name}股权结构图]({relative_svg})",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "> 当前股权数据未达到正式出图条件，报告保留结构化事实和待核查说明。",
                    "",
                ]
            )
        lines.extend(["### 股权比例完整性校验", ""])
        node_by_id = {node.node_id: node for node in ownership.nodes}
        for branch in percentage_validation.branches:
            target = node_by_id.get(branch.target_node_id)
            target_name = target.name if target else branch.target_node_id
            refs = _source_markers(branch.source_ids)
            lines.append(
                f"- {target_name}：已知比例合计 {_format_percentage(branch.known_total)}；"
                f"{_branch_status_label(branch.status)}。{refs}"
            )

        lines.extend(["", "## 6. 主要股东及实际控制关系", ""])
        graph_node_by_id = {node.node_id: node for node in graph.nodes}
        direct_edges = [
            edge
            for edge in graph.edges
            if edge.target_node_id == identity.entity_id
            and edge.relation_type is OwnershipRelationType.EQUITY_OWNERSHIP
            and edge.status is EvidenceStatus.VERIFIED
        ]
        direct_edges.sort(
            key=lambda item: item.percentage if item.percentage is not None else Decimal("-1"),
            reverse=True,
        )
        if direct_edges:
            lines.append("### 6.1 直接持股情况")
            lines.append("")
            for edge in direct_edges:
                shareholder = graph_node_by_id.get(edge.source_node_id)
                shareholder_name = shareholder.name if shareholder else edge.source_node_id
                calculation_note = (
                    "（Python差额计算值）"
                    if edge.percentage_origin is PercentageOrigin.CALCULATED_RESIDUAL
                    else ""
                )
                lines.append(
                    f"- {shareholder_name}：{_format_percentage(edge.percentage)}"
                    f"{calculation_note} {_source_markers(edge.source_ids)}"
                )
        else:
            lines.append("- 当前资料未形成可在报告中列示的经核实直接持股关系。")

        lines.extend(["", "### 6.2 控制关系", ""])
        verified_controls = [
            item
            for item in ownership.control_relationships
            if item.status is EvidenceStatus.VERIFIED
        ]
        if verified_controls:
            for relationship in verified_controls:
                subjects = "、".join(
                    node_by_id[node_id].name
                    for node_id in relationship.subject_node_ids
                    if node_id in node_by_id
                )
                controlled = node_by_id.get(relationship.controlled_node_id)
                controlled_name = (
                    controlled.name if controlled else relationship.controlled_node_id
                )
                if relationship.relation_type is ControlRelationType.ACTING_IN_CONCERT:
                    statement = (
                        f"{subjects}之间存在一致行动关系，控制对象为{controlled_name}。"
                    )
                else:
                    statement = (
                        f"{subjects}为{controlled_name}的"
                        f"{_control_relation_label(relationship.relation_type)}。"
                    )
                lines.append(
                    f"- {statement}{_source_markers(relationship.source_ids)}"
                )
        else:
            lines.append("- 当前资料未形成经核实的控制关系结论。")

        lines.extend(["", "### 6.3 AI 辅助分析状态", ""])
        if analysis.status is AnalysisValidationStatus.VALIDATED:
            lines.extend(
                [
                    analysis.text.strip(),
                    "",
                    f"> 以上 AI 辅助分析已通过事实校验，关联来源：{_source_markers(analysis.source_ids)}。",
                ]
            )
        elif analysis.status is AnalysisValidationStatus.REJECTED:
            lines.append(
                "> 本次 AI 分析未通过事实校验，未纳入正式结论。"
                f"校验记录：{analysis.validation_note}"
            )
        else:
            lines.append(
                "> 本次报告未调用 LLM；正文由 Python 根据已验证结构化事实确定性生成。"
            )

        lines.extend(["", "## 7. 待核查事项", ""])
        pending_items: List[str] = []
        for item in [
            *fundamentals.missing_information,
            *ownership.missing_information,
        ]:
            pending_items.append(
                f"{item.topic}：{item.description}（原因：{item.reason}）"
            )
        for branch in percentage_validation.branches:
            if not branch.is_complete:
                pending_items.append(branch.message)
        for node_id in graph.truncated_node_ids:
            node = node_by_id.get(node_id)
            pending_items.append(
                f"{node.name if node else node_id}达到五层展示上限，需继续核实上层关系。"
            )
        pending_items.extend(fundamentals.limitations)
        pending_items.extend(ownership.limitations)
        pending_items.extend(
            [
                "财务分析尚未进入 V2，当前报告不能据此判断偿债能力或现金流风险。",
                "行业发展及政策研究尚未进入 V2，当前报告未评价行业周期和政策影响。",
            ]
        )
        seen_pending: Set[str] = set()
        for item in pending_items:
            normalised = _normalise_sentence(item)
            if normalised and normalised not in seen_pending:
                lines.append(f"- {normalised}")
                seen_pending.add(normalised)

        lines.extend(["", "## 8. 数据来源及引用附录", ""])
        pages_by_source: Dict[str, Set[int]] = {source_id: set() for source_id in sources}
        sections_by_source: Dict[str, Set[str]] = {
            source_id: set() for source_id in sources
        }
        for evidence in evidence_by_id.values():
            if evidence.source_id in pages_by_source:
                pages_by_source[evidence.source_id].add(evidence.page_number)
                sections_by_source[evidence.source_id].add(evidence.page_or_section)
        for source_id, source in sources.items():
            parts = [f"[{source_id}] {source.title}", f"发布者：{source.publisher}"]
            if source.published_at:
                parts.append(f"披露日期：{source.published_at.isoformat()}")
            if source.report_period:
                parts.append(f"报告期：{source.report_period}")
            pages = sorted(pages_by_source[source_id])
            if pages:
                parts.append("PDF页码：" + "、".join(str(page) for page in pages))
            sections = sorted(sections_by_source[source_id])
            if sections:
                parts.append("定位：" + "；".join(sections))
            parts.append(f"原文：{source.url}")
            lines.append("- " + "；".join(parts))
        lines.extend(
            [
                "",
                "---",
                "",
                "本报告为 V1.4 初版，信息范围与限制以上述来源、基准日和待核查事项为准。",
                "",
            ]
        )
        return "\n".join(lines)


def assemble_report_artifacts(
    *,
    fundamentals_path: Path,
    ownership_dataset_path: Path,
    ownership_graph_path: Path,
    ownership_validation_path: Path,
    ownership_svg_path: Path,
    ownership_png_path: Optional[Path],
    report_path: Path,
    analysis: Optional[ValidatedAgentAnalysis] = None,
    web_evidence_path: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> ReportArtifacts:
    """读取已有结构化产物并生成报告，不重新执行抽取或 LLM。"""

    fundamentals = _load_model(fundamentals_path, FundamentalResearchBundle)
    ownership = _load_model(ownership_dataset_path, OwnershipDataset)
    graph = _load_model(ownership_graph_path, OwnershipGraph)
    validation = _load_model(
        ownership_validation_path, OwnershipPercentageValidationResult
    )
    web_research = (
        _load_model(web_evidence_path, WebEvidenceExtraction)
        if web_evidence_path is not None
        else None
    )
    artifacts = MarkdownReportBuilder(clock=clock).build(
        fundamentals=fundamentals,
        ownership=ownership,
        graph=graph,
        percentage_validation=validation,
        ownership_svg_path=ownership_svg_path,
        ownership_png_path=ownership_png_path,
        report_path=report_path,
        analysis=analysis,
        web_research=web_research,
    )
    return artifacts.copy(
        update={
            "fundamentals_path": Path(fundamentals_path).resolve(),
            "ownership_dataset_path": Path(ownership_dataset_path).resolve(),
            "ownership_graph_path": Path(ownership_graph_path).resolve(),
            "ownership_validation_path": Path(ownership_validation_path).resolve(),
        }
    )


def build_report_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    fundamentals_dir: Path,
    ownership_dir: Path,
    report_dir: Path,
    report_output: Optional[Path] = None,
    analysis: Optional[ValidatedAgentAnalysis] = None,
    web_evidence_path: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> ReportArtifacts:
    """运行现有 V1.1/V1.2 构建器，并确定性组装 V1.4 报告。"""

    fundamental_artifacts: FundamentalArtifacts = build_fundamental_artifacts(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        output_dir=fundamentals_dir,
        clock=clock,
    )
    ownership_artifacts: OwnershipArtifacts = build_ownership_artifacts(
        identity=identity,
        source=source,
        download=download,
        summary_path=summary_path,
        pages_path=pages_path,
        output_dir=ownership_dir,
        clock=clock,
    )
    output_path = (
        Path(report_output)
        if report_output is not None
        else Path(report_dir) / identity.entity_id / "credit_research_report.md"
    )
    return assemble_report_artifacts(
        fundamentals_path=fundamental_artifacts.fundamentals_path,
        ownership_dataset_path=ownership_artifacts.dataset_path,
        ownership_graph_path=ownership_artifacts.graph_path,
        ownership_validation_path=ownership_artifacts.validation_path,
        ownership_svg_path=ownership_artifacts.svg_path,
        ownership_png_path=ownership_artifacts.png_path,
        report_path=output_path,
        analysis=analysis,
        web_evidence_path=web_evidence_path,
        clock=clock,
    )
