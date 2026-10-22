"""V1.2 股权结构的确定性模型、比例校验、聚合、建图与 SVG/PNG 渲染。

原始披露数据（``OwnershipDataset``）与展示图（``OwnershipGraph``）分离：
聚合和五层裁剪只影响展示，不会改写原始事实或证据引用。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, validator

from credit_research_agent.models import (
    CompanyIdentity,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceStatus,
    MissingInformation,
    SourceRecord,
)
from credit_research_agent.documents import DownloadResult
from credit_research_agent.evidence import (
    EvidenceArtifactWriter,
    EvidenceBuildError,
    EvidenceBuildFailureRecord,
    EvidenceBuilder,
    EvidenceSeed,
    EvidenceSeedCatalog,
    load_default_seed_catalog,
    render_citations,
)


class OwnershipNodeType(str, Enum):
    TARGET_COMPANY = "target_company"
    NATURAL_PERSON = "natural_person"
    GENERAL_INSTITUTION = "general_institution"
    STATE_OWNED_PLATFORM = "state_owned_platform"
    GOVERNMENT_AUTHORITY = "government_authority"
    AGGREGATED = "aggregated"


class OwnershipRelationType(str, Enum):
    EQUITY_OWNERSHIP = "equity_ownership"
    VOTING_RIGHTS = "voting_rights"


class ControlRelationType(str, Enum):
    CONTROLLING_SHAREHOLDER = "controlling_shareholder"
    ACTUAL_CONTROLLER = "actual_controller"
    ULTIMATE_CONTROLLER = "ultimate_controller"
    ACTING_IN_CONCERT = "acting_in_concert"
    KEY_CONTROL_SUBJECT = "key_control_subject"


class StateControlClassification(str, Enum):
    UNKNOWN = "unknown"
    NON_STATE_CONTROLLED = "non_state_controlled"
    STATE_PARTICIPATED = "state_participated"
    STATE_CONTROLLED = "state_controlled"
    STATE_ACTUAL_CONTROLLED = "state_actual_controlled"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class PercentageOrigin(str, Enum):
    DISCLOSED = "disclosed"
    AGGREGATED = "aggregated"
    CALCULATED_RESIDUAL = "calculated_residual"


class OwnershipCompletenessStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_CALCULATED_RESIDUAL = "complete_with_calculated_residual"
    INCOMPLETE = "incomplete"
    OVER_ALLOCATED = "over_allocated"
    INCOMPARABLE_SCOPE = "incomparable_scope"
    UNVERIFIABLE = "unverifiable"


class OwnershipNode(BaseModel):
    node_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    node_type: OwnershipNodeType
    status: EvidenceStatus = EvidenceStatus.VERIFIED
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)

    class Config:
        allow_mutation = False


class OwnershipEdge(BaseModel):
    """股东/权利人指向被投资企业；持股与表决权不得混为一条边。"""

    edge_id: str = Field(min_length=1)
    source_node_id: str
    target_node_id: str
    relation_type: OwnershipRelationType = OwnershipRelationType.EQUITY_OWNERSHIP
    percentage: Optional[Decimal] = None
    percentage_origin: PercentageOrigin = PercentageOrigin.DISCLOSED
    capital_scope: str = Field(default="total_equity", min_length=1)
    as_of_date: date
    status: EvidenceStatus = EvidenceStatus.VERIFIED
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)

    @validator("percentage", pre=True)
    def validate_percentage(cls, value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        percentage = Decimal(str(value))
        if percentage < 0 or percentage > 100:
            raise ValueError("持股/表决权比例必须在0到100之间")
        return percentage

    class Config:
        allow_mutation = False


class ControlRelationship(BaseModel):
    """控制关系独立建模；程序不会由持股比例自动推导实际控制人。"""

    relationship_id: str = Field(min_length=1)
    subject_node_ids: List[str] = Field(min_items=1)
    controlled_node_id: str
    relation_type: ControlRelationType
    as_of_date: date
    status: EvidenceStatus = EvidenceStatus.VERIFIED
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    note: Optional[str] = None

    class Config:
        allow_mutation = False


class StateControlAssessment(BaseModel):
    entity_node_id: str
    classification: StateControlClassification = StateControlClassification.UNKNOWN
    as_of_date: date
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    note: Optional[str] = None

    class Config:
        allow_mutation = False


class OwnershipCompletenessDeclaration(BaseModel):
    """允许按差额生成“其他未单列股东”的人工核验声明。"""

    target_node_id: str
    as_of_date: date
    capital_scope: str = Field(min_length=1)
    allow_calculated_residual: bool = False
    residual_label: str = "其他未单列股东"
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    note: str = Field(min_length=1)

    class Config:
        allow_mutation = False


class OwnershipDataset(BaseModel):
    schema_version: str = "1.2.0"
    identity: CompanyIdentity
    as_of_date: date
    nodes: List[OwnershipNode]
    ownership_edges: List[OwnershipEdge]
    control_relationships: List[ControlRelationship] = Field(default_factory=list)
    state_control_assessments: List[StateControlAssessment] = Field(default_factory=list)
    completeness_declarations: List[OwnershipCompletenessDeclaration] = Field(
        default_factory=list
    )
    sources: List[SourceRecord]
    evidence: List[EvidenceRecord]
    missing_information: List[MissingInformation] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)

    class Config:
        allow_mutation = False


class OwnershipDisplayConfig(BaseModel):
    max_display_levels: int = Field(default=5, ge=1, le=5)
    natural_person_individual_threshold: Decimal = Decimal("25")
    institution_individual_threshold: Decimal = Decimal("5")
    percentage_tolerance: Decimal = Decimal("0.01")

    @validator(
        "natural_person_individual_threshold",
        "institution_individual_threshold",
        "percentage_tolerance",
        pre=True,
    )
    def threshold_is_percentage(cls, value: Any) -> Decimal:
        threshold = Decimal(str(value))
        if threshold < 0 or threshold > 100:
            raise ValueError("比例阈值/容差必须在0到100之间")
        return threshold

    class Config:
        allow_mutation = False


class OwnershipValidationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    message: str
    subject_id: Optional[str] = None

    class Config:
        allow_mutation = False


class OwnershipValidationResult(BaseModel):
    issues: List[OwnershipValidationIssue] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> List[OwnershipValidationIssue]:
        return [issue for issue in self.issues if issue.severity is IssueSeverity.ERROR]

    @property
    def warnings(self) -> List[OwnershipValidationIssue]:
        return [issue for issue in self.issues if issue.severity is IssueSeverity.WARNING]


class OwnershipBranchValidation(BaseModel):
    target_node_id: str
    capital_scope: Optional[str] = None
    known_total: Decimal
    tolerance: Decimal
    status: OwnershipCompletenessStatus
    edge_ids: List[str] = Field(default_factory=list)
    calculated_residual: Optional[Decimal] = None
    residual_label: Optional[str] = None
    source_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    message: str

    @property
    def is_complete(self) -> bool:
        return self.status in {
            OwnershipCompletenessStatus.COMPLETE,
            OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL,
        }

    class Config:
        allow_mutation = False


class OwnershipPercentageValidationResult(BaseModel):
    tolerance: Decimal
    branches: List[OwnershipBranchValidation]

    @property
    def is_complete(self) -> bool:
        return bool(self.branches) and all(item.is_complete for item in self.branches)

    class Config:
        allow_mutation = False


class AggregationGroup(BaseModel):
    aggregate_node_id: str
    aggregate_edge_id: str
    target_node_id: str
    category: str
    capital_scope: str
    member_node_ids: List[str]
    source_edge_ids: List[str]
    percentage_calculated: bool

    class Config:
        allow_mutation = False


class OwnershipAggregationResult(BaseModel):
    nodes: List[OwnershipNode]
    ownership_edges: List[OwnershipEdge]
    groups: List[AggregationGroup] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    class Config:
        allow_mutation = False


class OwnershipGraphNode(BaseModel):
    node_id: str
    name: str
    node_type: OwnershipNodeType
    level: int = Field(ge=1, le=5)
    status: EvidenceStatus
    source_ids: List[str]
    evidence_ids: List[str]
    control_roles: List[ControlRelationType] = Field(default_factory=list)
    aggregated_member_ids: List[str] = Field(default_factory=list)
    is_truncated: bool = False
    ownership_percentage: Optional[Decimal] = None
    percentage_origin: Optional[PercentageOrigin] = None

    class Config:
        allow_mutation = False


class OwnershipGraphEdge(OwnershipEdge):
    synthetic: bool = False
    aggregated_from_edge_ids: List[str] = Field(default_factory=list)


class OwnershipGraph(BaseModel):
    schema_version: str = "1.2.0"
    identity: CompanyIdentity
    as_of_date: date
    config: OwnershipDisplayConfig
    nodes: List[OwnershipGraphNode]
    edges: List[OwnershipGraphEdge]
    control_relationships: List[ControlRelationship] = Field(default_factory=list)
    state_control_assessments: List[StateControlAssessment] = Field(default_factory=list)
    aggregation_groups: List[AggregationGroup] = Field(default_factory=list)
    truncated_node_ids: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    sources: List[SourceRecord]
    evidence: List[EvidenceRecord]
    missing_information: List[MissingInformation] = Field(default_factory=list)
    percentage_validation: OwnershipPercentageValidationResult
    formal_ready: bool

    class Config:
        allow_mutation = False


class OwnershipRenderResult(BaseModel):
    svg_path: Path
    png_path: Path
    dot_path: Path
    engine: str


class OwnershipSeedCatalog(BaseModel):
    """人工审阅的真实股权结构锚点；不是模型生成结果。"""

    entity_id: str
    as_of_date: date
    evidence: List[EvidenceSeed]
    nodes: List[OwnershipNode]
    ownership_edges: List[OwnershipEdge]
    control_relationships: List[ControlRelationship] = Field(default_factory=list)
    state_control_assessments: List[StateControlAssessment] = Field(default_factory=list)
    completeness_declarations: List[OwnershipCompletenessDeclaration] = Field(
        default_factory=list
    )
    missing_information: List[MissingInformation] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class OwnershipBuildResult(BaseModel):
    dataset: OwnershipDataset
    graph: OwnershipGraph
    validation: OwnershipValidationResult
    percentage_validation: OwnershipPercentageValidationResult


class OwnershipArtifacts(BaseModel):
    entity_id: str
    dataset_path: Path
    graph_path: Path
    svg_path: Path
    png_path: Path
    dot_path: Path
    citations_path: Path
    summary_path: Path
    validation_path: Path
    sources_path: Path
    questions_path: Path
    render_engine: str
    node_count: int
    edge_count: int
    control_relationship_count: int
    missing_information_count: int


class OwnershipBuildError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _issue(
    code: str,
    severity: IssueSeverity,
    message: str,
    subject_id: Optional[str] = None,
) -> OwnershipValidationIssue:
    return OwnershipValidationIssue(
        code=code, severity=severity, message=message, subject_id=subject_id
    )


def _duplicates(values: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    duplicated: Set[str] = set()
    for value in values:
        if value in seen:
            duplicated.add(value)
        seen.add(value)
    return duplicated


def _find_cycles(dataset: OwnershipDataset, node_ids: Set[str]) -> List[List[str]]:
    """从目标公司向上穿透，检测 shareholder -> investee 关系中的环。"""

    incoming: Dict[str, List[str]] = defaultdict(list)
    for edge in dataset.ownership_edges:
        if edge.relation_type is OwnershipRelationType.EQUITY_OWNERSHIP:
            incoming[edge.target_node_id].append(edge.source_node_id)

    cycles: List[List[str]] = []
    visited: Set[str] = set()
    active: List[str] = []

    def visit(node_id: str) -> None:
        if node_id in active:
            start = active.index(node_id)
            cycles.append([*active[start:], node_id])
            return
        if node_id in visited or node_id not in node_ids:
            return
        active.append(node_id)
        for shareholder_id in incoming.get(node_id, []):
            visit(shareholder_id)
        active.pop()
        visited.add(node_id)

    visit(dataset.identity.entity_id)
    return cycles


def validate_ownership_facts(dataset: OwnershipDataset) -> OwnershipValidationResult:
    """校验引用、口径、日期、重复关系、比例合计和穿透环路。"""

    issues: List[OwnershipValidationIssue] = []
    node_ids = [item.node_id for item in dataset.nodes]
    edge_ids = [item.edge_id for item in dataset.ownership_edges]
    control_ids = [item.relationship_id for item in dataset.control_relationships]
    source_ids = [item.source_id for item in dataset.sources]
    evidence_ids = [item.evidence_id for item in dataset.evidence]

    for value, label, code in (
        (node_ids, "节点", "duplicate_node"),
        (edge_ids, "股权边", "duplicate_edge"),
        (control_ids, "控制关系", "duplicate_control_relationship"),
        (source_ids, "来源", "duplicate_source"),
        (evidence_ids, "证据", "duplicate_evidence"),
    ):
        for duplicate in sorted(_duplicates(value)):
            issues.append(_issue(code, IssueSeverity.ERROR, f"{label}编号重复：{duplicate}", duplicate))

    node_id_set = set(node_ids)
    source_id_set = set(source_ids)
    evidence_by_id = {item.evidence_id: item for item in dataset.evidence}
    target_nodes = [
        node for node in dataset.nodes if node.node_type is OwnershipNodeType.TARGET_COMPANY
    ]
    if len(target_nodes) != 1 or target_nodes[0].node_id != dataset.identity.entity_id:
        issues.append(
            _issue(
                "invalid_target_node",
                IssueSeverity.ERROR,
                "必须且只能有一个与 CompanyIdentity.entity_id 一致的目标公司节点",
                dataset.identity.entity_id,
            )
        )

    for evidence in dataset.evidence:
        if evidence.source_id not in source_id_set:
            issues.append(
                _issue(
                    "evidence_source_missing",
                    IssueSeverity.ERROR,
                    f"证据 {evidence.evidence_id} 引用了未登记来源 {evidence.source_id}",
                    evidence.evidence_id,
                )
            )

    for evidence_id in sorted(
        set(dataset.identity.evidence_ids) - set(evidence_by_id)
    ):
        issues.append(
            _issue(
                "identity_evidence_missing",
                IssueSeverity.ERROR,
                f"主体引用了不存在证据 {evidence_id}",
                dataset.identity.entity_id,
            )
        )

    def check_references(
        subject_id: str,
        status: EvidenceStatus,
        subject_source_ids: Sequence[str],
        subject_evidence_ids: Sequence[str],
    ) -> None:
        if not subject_source_ids:
            issues.append(
                _issue("source_missing", IssueSeverity.ERROR, f"{subject_id} 缺少来源编号", subject_id)
            )
        if not subject_evidence_ids:
            issues.append(
                _issue("evidence_missing", IssueSeverity.ERROR, f"{subject_id} 缺少证据编号", subject_id)
            )
        for source_id in sorted(set(subject_source_ids) - source_id_set):
            issues.append(
                _issue(
                    "source_reference_missing",
                    IssueSeverity.ERROR,
                    f"{subject_id} 引用了未登记来源 {source_id}",
                    subject_id,
                )
            )
        for evidence_id in sorted(set(subject_evidence_ids) - set(evidence_by_id)):
            issues.append(
                _issue(
                    "evidence_reference_missing",
                    IssueSeverity.ERROR,
                    f"{subject_id} 引用了不存在证据 {evidence_id}",
                    subject_id,
                )
            )
        for evidence_id in set(subject_evidence_ids) & set(evidence_by_id):
            evidence = evidence_by_id[evidence_id]
            if evidence.source_id not in subject_source_ids:
                issues.append(
                    _issue(
                        "evidence_source_mismatch",
                        IssueSeverity.ERROR,
                        f"{subject_id} 的证据 {evidence_id} 与来源编号不一致",
                        subject_id,
                    )
                )
            if status is EvidenceStatus.VERIFIED and evidence.status is not EvidenceStatus.VERIFIED:
                issues.append(
                    _issue(
                        "unverified_evidence",
                        IssueSeverity.ERROR,
                        f"{subject_id} 标为已核实，但证据 {evidence_id} 未核实",
                        subject_id,
                    )
                )

    for node in dataset.nodes:
        check_references(node.node_id, node.status, node.source_ids, node.evidence_ids)

    relation_signatures: Dict[
        Tuple[str, str, OwnershipRelationType, date, str], List[OwnershipEdge]
    ] = defaultdict(list)
    for edge in dataset.ownership_edges:
        for endpoint in (edge.source_node_id, edge.target_node_id):
            if endpoint not in node_id_set:
                issues.append(
                    _issue(
                        "edge_node_missing",
                        IssueSeverity.ERROR,
                        f"股权边 {edge.edge_id} 引用了不存在节点 {endpoint}",
                        edge.edge_id,
                    )
                )
        if edge.source_node_id == edge.target_node_id:
            issues.append(
                _issue("self_loop", IssueSeverity.ERROR, f"股权边 {edge.edge_id} 为自环", edge.edge_id)
            )
        if edge.as_of_date != dataset.as_of_date:
            issues.append(
                _issue(
                    "date_conflict",
                    IssueSeverity.ERROR,
                    f"股权边 {edge.edge_id} 基准日 {edge.as_of_date} 与数据集 {dataset.as_of_date} 不一致",
                    edge.edge_id,
                )
            )
        check_references(edge.edge_id, edge.status, edge.source_ids, edge.evidence_ids)
        relation_signatures[
            (
                edge.source_node_id,
                edge.target_node_id,
                edge.relation_type,
                edge.as_of_date,
                edge.capital_scope,
            )
        ].append(edge)

    for signature_edges in relation_signatures.values():
        percentages = {edge.percentage for edge in signature_edges}
        if len(percentages) > 1:
            ids = ", ".join(edge.edge_id for edge in signature_edges)
            issues.append(
                _issue(
                    "conflicting_disclosure",
                    IssueSeverity.ERROR,
                    f"同一关系存在冲突比例：{ids}",
                    signature_edges[0].target_node_id,
                )
            )
        elif len(signature_edges) > 1:
            issues.append(
                _issue(
                    "duplicate_relationship",
                    IssueSeverity.WARNING,
                    f"同一关系重复披露：{', '.join(edge.edge_id for edge in signature_edges)}",
                    signature_edges[0].target_node_id,
                )
            )

    declaration_targets = [
        declaration.target_node_id
        for declaration in dataset.completeness_declarations
    ]
    for duplicate in sorted(_duplicates(declaration_targets)):
        issues.append(
            _issue(
                "duplicate_completeness_declaration",
                IssueSeverity.ERROR,
                f"同一被持股主体存在重复完整性声明：{duplicate}",
                duplicate,
            )
        )
    for declaration in dataset.completeness_declarations:
        if declaration.target_node_id not in node_id_set:
            issues.append(
                _issue(
                    "declaration_node_missing",
                    IssueSeverity.ERROR,
                    f"完整性声明引用不存在节点 {declaration.target_node_id}",
                    declaration.target_node_id,
                )
            )
        if declaration.as_of_date != dataset.as_of_date:
            issues.append(
                _issue(
                    "date_conflict",
                    IssueSeverity.ERROR,
                    f"完整性声明 {declaration.target_node_id} 基准日不一致",
                    declaration.target_node_id,
                )
            )
        check_references(
            f"completeness:{declaration.target_node_id}",
            EvidenceStatus.VERIFIED,
            declaration.source_ids,
            declaration.evidence_ids,
        )

    for relationship in dataset.control_relationships:
        missing_nodes = (
            set(relationship.subject_node_ids) | {relationship.controlled_node_id}
        ) - node_id_set
        for node_id in sorted(missing_nodes):
            issues.append(
                _issue(
                    "control_node_missing",
                    IssueSeverity.ERROR,
                    f"控制关系 {relationship.relationship_id} 引用了不存在节点 {node_id}",
                    relationship.relationship_id,
                )
            )
        if relationship.as_of_date != dataset.as_of_date:
            issues.append(
                _issue(
                    "date_conflict",
                    IssueSeverity.ERROR,
                    f"控制关系 {relationship.relationship_id} 基准日不一致",
                    relationship.relationship_id,
                )
            )
        check_references(
            relationship.relationship_id,
            relationship.status,
            relationship.source_ids,
            relationship.evidence_ids,
        )

    for assessment in dataset.state_control_assessments:
        if assessment.entity_node_id not in node_id_set:
            issues.append(
                _issue(
                    "assessment_node_missing",
                    IssueSeverity.ERROR,
                    f"国资判断引用不存在节点 {assessment.entity_node_id}",
                    assessment.entity_node_id,
                )
            )
        if assessment.as_of_date != dataset.as_of_date:
            issues.append(
                _issue(
                    "date_conflict",
                    IssueSeverity.ERROR,
                    f"国资判断 {assessment.entity_node_id} 基准日不一致",
                    assessment.entity_node_id,
                )
            )
        if assessment.classification is not StateControlClassification.UNKNOWN:
            check_references(
                f"state:{assessment.entity_node_id}",
                assessment.status,
                assessment.source_ids,
                assessment.evidence_ids,
            )

    for item in dataset.missing_information:
        for evidence_id in sorted(set(item.evidence_ids) - set(evidence_by_id)):
            issues.append(
                _issue(
                    "missing_information_evidence_missing",
                    IssueSeverity.ERROR,
                    f"缺失信息 {item.missing_id} 引用了不存在证据 {evidence_id}",
                    item.missing_id,
                )
            )

    for cycle in _find_cycles(dataset, node_id_set):
        issues.append(
            _issue(
                "ownership_cycle",
                IssueSeverity.ERROR,
                f"股权穿透出现环路：{' -> '.join(cycle)}",
                cycle[0],
            )
        )

    return OwnershipValidationResult(issues=issues)


def validate_ownership_percentages(
    dataset: OwnershipDataset,
    *,
    tolerance: Decimal | str | float = Decimal("0.01"),
    strict: bool = False,
) -> OwnershipPercentageValidationResult:
    """逐个被持股主体校验直接股东比例，不跨分支或跨股本口径加总。"""

    tolerance_value = Decimal(str(tolerance))
    if tolerance_value < 0 or tolerance_value > 100:
        raise ValueError("股权比例容差必须在0到100之间")

    structural = validate_ownership_facts(dataset)
    if not structural.is_valid:
        raise OwnershipBuildError(
            "invalid_ownership_facts",
            "; ".join(issue.message for issue in structural.errors),
        )

    equity_by_target: Dict[str, List[OwnershipEdge]] = defaultdict(list)
    for edge in dataset.ownership_edges:
        if edge.relation_type is OwnershipRelationType.EQUITY_OWNERSHIP:
            equity_by_target[edge.target_node_id].append(edge)

    expanded_node_ids = {dataset.identity.entity_id} | set(equity_by_target)
    declaration_by_target = {
        item.target_node_id: item for item in dataset.completeness_declarations
    }
    branches: List[OwnershipBranchValidation] = []
    for target_node_id in sorted(expanded_node_ids):
        edges = equity_by_target.get(target_node_id, [])
        edge_ids = sorted(edge.edge_id for edge in edges)
        source_ids = sorted({value for edge in edges for value in edge.source_ids})
        evidence_ids = sorted({value for edge in edges for value in edge.evidence_ids})
        scopes = {edge.capital_scope for edge in edges}
        known_total = sum(
            (edge.percentage or Decimal("0") for edge in edges), Decimal("0")
        )

        if not edges:
            branch = OwnershipBranchValidation(
                target_node_id=target_node_id,
                known_total=Decimal("0"),
                tolerance=tolerance_value,
                status=OwnershipCompletenessStatus.INCOMPLETE,
                message="目标企业没有可核验的直接持股关系，无法生成正式股权图。",
            )
        elif len(scopes) != 1:
            branch = OwnershipBranchValidation(
                target_node_id=target_node_id,
                known_total=known_total,
                tolerance=tolerance_value,
                status=OwnershipCompletenessStatus.INCOMPARABLE_SCOPE,
                edge_ids=edge_ids,
                source_ids=source_ids,
                evidence_ids=evidence_ids,
                message=(
                    f"{target_node_id} 的直接持股关系包含不同股本口径："
                    f"{sorted(scopes)}，不得直接加总。"
                ),
            )
        elif any(
            edge.percentage is None or edge.status is not EvidenceStatus.VERIFIED
            for edge in edges
        ):
            branch = OwnershipBranchValidation(
                target_node_id=target_node_id,
                capital_scope=next(iter(scopes)),
                known_total=known_total,
                tolerance=tolerance_value,
                status=OwnershipCompletenessStatus.UNVERIFIABLE,
                edge_ids=edge_ids,
                source_ids=source_ids,
                evidence_ids=evidence_ids,
                message=(
                    f"{target_node_id} 存在比例未知或未核实的直接持股关系，"
                    "不能判定100%完整性。"
                ),
            )
        elif known_total > Decimal("100") + tolerance_value:
            branch = OwnershipBranchValidation(
                target_node_id=target_node_id,
                capital_scope=next(iter(scopes)),
                known_total=known_total,
                tolerance=tolerance_value,
                status=OwnershipCompletenessStatus.OVER_ALLOCATED,
                edge_ids=edge_ids,
                source_ids=source_ids,
                evidence_ids=evidence_ids,
                message=(
                    f"{target_node_id} 的直接股东持股比例合计 {known_total}%，"
                    f"超过100%且超出±{tolerance_value}个百分点容差；"
                    "请检查重复股东、股本口径、披露日期或解析错误。"
                ),
            )
        elif known_total < Decimal("100") - tolerance_value:
            scope = next(iter(scopes))
            declaration = declaration_by_target.get(target_node_id)
            if (
                declaration is not None
                and declaration.allow_calculated_residual
                and declaration.capital_scope == scope
                and declaration.as_of_date == dataset.as_of_date
            ):
                residual = Decimal("100") - known_total
                branch = OwnershipBranchValidation(
                    target_node_id=target_node_id,
                    capital_scope=scope,
                    known_total=known_total,
                    tolerance=tolerance_value,
                    status=OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL,
                    edge_ids=edge_ids,
                    calculated_residual=residual,
                    residual_label=declaration.residual_label,
                    source_ids=sorted(
                        set(source_ids) | set(declaration.source_ids)
                    ),
                    evidence_ids=sorted(
                        set(evidence_ids) | set(declaration.evidence_ids)
                    ),
                    message=(
                        f"{target_node_id} 已披露直接持股合计 {known_total}%；"
                        f"依据同一时点、同一口径声明计算剩余 {residual}%为"
                        f"“{declaration.residual_label}”。"
                    ),
                )
            else:
                branch = OwnershipBranchValidation(
                    target_node_id=target_node_id,
                    capital_scope=scope,
                    known_total=known_total,
                    tolerance=tolerance_value,
                    status=OwnershipCompletenessStatus.INCOMPLETE,
                    edge_ids=edge_ids,
                    source_ids=source_ids,
                    evidence_ids=evidence_ids,
                    message=(
                        f"{target_node_id} 的已知直接股东持股比例合计 {known_total}%，"
                        f"缺口为 {Decimal('100') - known_total}%；没有同一时点、同一股本口径的"
                        "可计算剩余股份声明，不得强行补足100%。"
                    ),
                )
        else:
            branch = OwnershipBranchValidation(
                target_node_id=target_node_id,
                capital_scope=next(iter(scopes)),
                known_total=known_total,
                tolerance=tolerance_value,
                status=OwnershipCompletenessStatus.COMPLETE,
                edge_ids=edge_ids,
                source_ids=source_ids,
                evidence_ids=evidence_ids,
                message=(
                    f"{target_node_id} 的直接股东持股比例合计 {known_total}%，"
                    f"通过100%完整性校验（容差±{tolerance_value}个百分点）。"
                ),
            )
        branches.append(branch)

    result = OwnershipPercentageValidationResult(
        tolerance=tolerance_value,
        branches=branches,
    )
    if strict and not result.is_complete:
        failed = [item for item in result.branches if not item.is_complete]
        first = failed[0]
        code = {
            OwnershipCompletenessStatus.OVER_ALLOCATED: "percentage_total_exceeds_100",
            OwnershipCompletenessStatus.INCOMPARABLE_SCOPE: "percentage_scope_conflict",
            OwnershipCompletenessStatus.UNVERIFIABLE: "percentage_unverifiable",
            OwnershipCompletenessStatus.INCOMPLETE: "percentage_total_incomplete",
        }.get(first.status, "percentage_validation_failed")
        raise OwnershipBuildError(code, "; ".join(item.message for item in failed))
    return result


def _key_control_node_ids(dataset: OwnershipDataset) -> Set[str]:
    return {
        node_id
        for relationship in dataset.control_relationships
        for node_id in relationship.subject_node_ids
        if relationship.relation_type
        in {
            ControlRelationType.CONTROLLING_SHAREHOLDER,
            ControlRelationType.ACTUAL_CONTROLLER,
            ControlRelationType.ULTIMATE_CONTROLLER,
            ControlRelationType.ACTING_IN_CONCERT,
            ControlRelationType.KEY_CONTROL_SUBJECT,
        }
    }


def _aggregation_category(
    node: OwnershipNode,
    edge: OwnershipEdge,
    key_node_ids: Set[str],
    config: OwnershipDisplayConfig,
) -> Optional[str]:
    if (
        edge.relation_type is not OwnershipRelationType.EQUITY_OWNERSHIP
        or node.node_id in key_node_ids
        or edge.percentage is None
    ):
        return None
    if node.node_type is OwnershipNodeType.NATURAL_PERSON:
        # 严格大于25%单列；恰好25%仍是聚合候选。
        if edge.percentage <= config.natural_person_individual_threshold:
            return "natural_person"
        return None
    if node.node_type in {
        OwnershipNodeType.GENERAL_INSTITUTION,
        OwnershipNodeType.STATE_OWNED_PLATFORM,
    }:
        # 严格低于5%才聚合；恰好5%必须单列。
        if edge.percentage < config.institution_individual_threshold:
            return "institution"
    return None


def _merge_status(items: Sequence[EvidenceStatus]) -> EvidenceStatus:
    if any(item is EvidenceStatus.CONFLICTING for item in items):
        return EvidenceStatus.CONFLICTING
    if all(item is EvidenceStatus.VERIFIED for item in items):
        return EvidenceStatus.VERIFIED
    return EvidenceStatus.UNVERIFIED


def aggregate_ownership_holders(
    dataset: OwnershipDataset,
    config: Optional[OwnershipDisplayConfig] = None,
) -> OwnershipAggregationResult:
    """按 Skill 阈值聚合非关键股东；不会自行补造股东使合计达到100%。"""

    config = config or OwnershipDisplayConfig()
    validation = validate_ownership_facts(dataset)
    if not validation.is_valid:
        raise OwnershipBuildError(
            "invalid_ownership_facts",
            "; ".join(issue.message for issue in validation.errors),
        )

    node_by_id = {node.node_id: node for node in dataset.nodes}
    key_node_ids = _key_control_node_ids(dataset)
    grouped: Dict[
        Tuple[str, OwnershipRelationType, str, str], List[OwnershipEdge]
    ] = defaultdict(list)
    kept_edges: List[OwnershipEdge] = []
    aggregated_member_ids: Set[str] = set()

    for edge in dataset.ownership_edges:
        node = node_by_id[edge.source_node_id]
        category = _aggregation_category(node, edge, key_node_ids, config)
        if category is None:
            kept_edges.append(edge)
        else:
            grouped[
                (
                    edge.target_node_id,
                    edge.relation_type,
                    category,
                    edge.capital_scope,
                )
            ].append(edge)
            aggregated_member_ids.add(edge.source_node_id)

    generated_nodes: List[OwnershipNode] = []
    generated_edges: List[OwnershipEdge] = []
    groups: List[AggregationGroup] = []
    warnings: List[str] = []
    for (target_id, relation_type, category, capital_scope), edges in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1].value, item[0][2], item[0][3]),
    ):
        aggregate_node_id = (
            f"AGG::{target_id}::{relation_type.value}::{category}::{capital_scope}"
        )
        member_ids = sorted(edge.source_node_id for edge in edges)
        all_complete = (
            all(edge.percentage is not None for edge in edges)
            and all(edge.status is EvidenceStatus.VERIFIED for edge in edges)
            and len({edge.as_of_date for edge in edges}) == 1
        )
        percentage = (
            sum((edge.percentage or Decimal("0") for edge in edges), Decimal("0"))
            if all_complete
            else None
        )
        source_ids = sorted({value for edge in edges for value in edge.source_ids})
        evidence_ids = sorted({value for edge in edges for value in edge.evidence_ids})
        label = (
            "其他未超过25%的自然人股东"
            if category == "natural_person"
            else "其他未超过5%的股东"
        )
        generated_nodes.append(
            OwnershipNode(
                node_id=aggregate_node_id,
                name=label,
                node_type=OwnershipNodeType.AGGREGATED,
                status=_merge_status([edge.status for edge in edges]),
                source_ids=source_ids,
                evidence_ids=evidence_ids,
            )
        )
        synthetic_edge_id = (
            f"AGG-EDGE::{target_id}::{relation_type.value}::{category}::{capital_scope}"
        )
        generated_edges.append(
            OwnershipEdge(
                edge_id=synthetic_edge_id,
                source_node_id=aggregate_node_id,
                target_node_id=target_id,
                relation_type=relation_type,
                percentage=percentage,
                percentage_origin=PercentageOrigin.AGGREGATED,
                capital_scope=capital_scope,
                as_of_date=dataset.as_of_date,
                status=_merge_status([edge.status for edge in edges]),
                source_ids=source_ids,
                evidence_ids=evidence_ids,
            )
        )
        groups.append(
            AggregationGroup(
                aggregate_node_id=aggregate_node_id,
                aggregate_edge_id=synthetic_edge_id,
                target_node_id=target_id,
                category=category,
                capital_scope=capital_scope,
                member_node_ids=member_ids,
                source_edge_ids=sorted(edge.edge_id for edge in edges),
                percentage_calculated=all_complete,
            )
        )
        if not all_complete:
            warnings.append(f"{label}存在比例、日期或核验状态不完整，聚合比例显示为待核实")

    kept_node_ids = set(node_by_id) - aggregated_member_ids
    nodes = [node for node in dataset.nodes if node.node_id in kept_node_ids]
    nodes.extend(generated_nodes)
    return OwnershipAggregationResult(
        nodes=nodes,
        ownership_edges=[*kept_edges, *generated_edges],
        groups=groups,
        warnings=warnings,
    )


def _add_calculated_residuals(
    aggregated: OwnershipAggregationResult,
    percentage_validation: OwnershipPercentageValidationResult,
    as_of_date: date,
) -> OwnershipAggregationResult:
    """仅依据已核验完整性声明，把计算差额加入展示层，不改写原始数据。"""

    nodes = list(aggregated.nodes)
    edges = list(aggregated.ownership_edges)
    groups = list(aggregated.groups)
    for branch in percentage_validation.branches:
        if (
            branch.status
            is not OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL
            or branch.calculated_residual is None
            or branch.residual_label is None
            or branch.capital_scope is None
        ):
            continue
        node_id = f"RESIDUAL::{branch.target_node_id}::{branch.capital_scope}"
        edge_id = f"RESIDUAL-EDGE::{branch.target_node_id}::{branch.capital_scope}"
        nodes.append(
            OwnershipNode(
                node_id=node_id,
                name=branch.residual_label,
                node_type=OwnershipNodeType.AGGREGATED,
                status=EvidenceStatus.VERIFIED,
                source_ids=branch.source_ids,
                evidence_ids=branch.evidence_ids,
            )
        )
        edges.append(
            OwnershipEdge(
                edge_id=edge_id,
                source_node_id=node_id,
                target_node_id=branch.target_node_id,
                relation_type=OwnershipRelationType.EQUITY_OWNERSHIP,
                percentage=branch.calculated_residual,
                percentage_origin=PercentageOrigin.CALCULATED_RESIDUAL,
                capital_scope=branch.capital_scope,
                as_of_date=as_of_date,
                status=EvidenceStatus.VERIFIED,
                source_ids=branch.source_ids,
                evidence_ids=branch.evidence_ids,
            )
        )
        groups.append(
            AggregationGroup(
                aggregate_node_id=node_id,
                aggregate_edge_id=edge_id,
                target_node_id=branch.target_node_id,
                category="calculated_residual",
                capital_scope=branch.capital_scope,
                member_node_ids=[],
                source_edge_ids=branch.edge_ids,
                percentage_calculated=True,
            )
        )
    return OwnershipAggregationResult(
        nodes=nodes,
        ownership_edges=edges,
        groups=groups,
        warnings=aggregated.warnings,
    )


def build_ownership_graph(
    dataset: OwnershipDataset,
    config: Optional[OwnershipDisplayConfig] = None,
    *,
    require_complete: bool = True,
) -> OwnershipGraph:
    """从目标公司向股东方向穿透，目标为第1层，第5层后停止。"""

    config = config or OwnershipDisplayConfig()
    validation = validate_ownership_facts(dataset)
    if not validation.is_valid:
        raise OwnershipBuildError(
            "invalid_ownership_facts",
            "; ".join(issue.message for issue in validation.errors),
        )
    percentage_validation = validate_ownership_percentages(
        dataset,
        tolerance=config.percentage_tolerance,
        strict=require_complete,
    )
    aggregated = _add_calculated_residuals(
        aggregate_ownership_holders(dataset, config),
        percentage_validation,
        dataset.as_of_date,
    )
    node_by_id = {node.node_id: node for node in aggregated.nodes}
    incoming: Dict[str, List[OwnershipEdge]] = defaultdict(list)
    for edge in aggregated.ownership_edges:
        if edge.relation_type is OwnershipRelationType.EQUITY_OWNERSHIP:
            incoming[edge.target_node_id].append(edge)

    levels: Dict[str, int] = {dataset.identity.entity_id: 1}
    queue: deque[str] = deque([dataset.identity.entity_id])
    truncated: Set[str] = set()
    warnings = [issue.message for issue in validation.warnings]
    warnings.extend(aggregated.warnings)
    while queue:
        target_id = queue.popleft()
        target_level = levels[target_id]
        upstream = incoming.get(target_id, [])
        if target_level >= config.max_display_levels:
            if upstream:
                truncated.add(target_id)
            continue
        for edge in upstream:
            candidate_level = target_level + 1
            existing = levels.get(edge.source_node_id)
            if existing is None or candidate_level < existing:
                levels[edge.source_node_id] = candidate_level
                queue.append(edge.source_node_id)

    group_by_node = {group.aggregate_node_id: group for group in aggregated.groups}
    roles_by_node: Dict[str, Set[ControlRelationType]] = defaultdict(set)
    for relationship in dataset.control_relationships:
        for subject_id in relationship.subject_node_ids:
            roles_by_node[subject_id].add(relationship.relation_type)

    equity_edges_by_holder: Dict[str, List[OwnershipEdge]] = defaultdict(list)
    for edge in aggregated.ownership_edges:
        if edge.relation_type is OwnershipRelationType.EQUITY_OWNERSHIP:
            equity_edges_by_holder[edge.source_node_id].append(edge)

    graph_nodes: List[OwnershipGraphNode] = []
    for node_id, level in sorted(levels.items(), key=lambda item: (item[1], item[0])):
        node = node_by_id.get(node_id)
        if node is None:
            # 聚合移除了关键节点属于程序错误；不静默生成。
            raise OwnershipBuildError("graph_node_missing", f"展示节点不存在：{node_id}")
        group = group_by_node.get(node_id)
        holder_edges = equity_edges_by_holder.get(node_id, [])
        display_edge = holder_edges[0] if len(holder_edges) == 1 else None
        if len(holder_edges) > 1:
            warnings.append(
                f"{node.name}同时持有多个展示主体，节点无法用单一比例表达；"
                "完整关系保留在JSON。"
            )
        graph_nodes.append(
            OwnershipGraphNode(
                node_id=node.node_id,
                name=node.name,
                node_type=node.node_type,
                level=level,
                status=node.status,
                source_ids=node.source_ids,
                evidence_ids=node.evidence_ids,
                control_roles=sorted(roles_by_node[node_id], key=lambda item: item.value),
                aggregated_member_ids=group.member_node_ids if group else [],
                is_truncated=node_id in truncated,
                ownership_percentage=(
                    display_edge.percentage if display_edge is not None else None
                ),
                percentage_origin=(
                    display_edge.percentage_origin
                    if display_edge is not None
                    else None
                ),
            )
        )

    displayed_node_ids = set(levels)
    graph_edges: List[OwnershipGraphEdge] = []
    group_by_edge = {group.aggregate_edge_id: group for group in aggregated.groups}
    for edge in aggregated.ownership_edges:
        if edge.relation_type is not OwnershipRelationType.EQUITY_OWNERSHIP:
            continue
        if edge.source_node_id not in displayed_node_ids or edge.target_node_id not in displayed_node_ids:
            continue
        group = group_by_edge.get(edge.edge_id)
        graph_edges.append(
            OwnershipGraphEdge(
                **edge.dict(),
                synthetic=group is not None,
                aggregated_from_edge_ids=group.source_edge_ids if group else [],
            )
        )

    visible_controls = [
        relationship
        for relationship in dataset.control_relationships
        if relationship.controlled_node_id in displayed_node_ids
        and all(subject_id in displayed_node_ids for subject_id in relationship.subject_node_ids)
    ]
    if truncated:
        warnings.append(
            "股权结构最多展示5层；以下节点仍有上层股东但已截断："
            + "、".join(sorted(truncated))
        )
    unpierced_state_platforms = [
        node.name
        for node in graph_nodes
        if node.node_type is OwnershipNodeType.STATE_OWNED_PLATFORM
        and node.node_id not in incoming
    ]
    if unpierced_state_platforms:
        warnings.append(
            "国资平台不能自动视为最终穿透终点，仍待核实政府/国资监管主体："
            + "、".join(sorted(unpierced_state_platforms))
        )
    return OwnershipGraph(
        identity=dataset.identity,
        as_of_date=dataset.as_of_date,
        config=config,
        nodes=graph_nodes,
        edges=graph_edges,
        control_relationships=visible_controls,
        state_control_assessments=dataset.state_control_assessments,
        aggregation_groups=aggregated.groups,
        truncated_node_ids=sorted(truncated),
        warnings=warnings,
        sources=dataset.sources,
        evidence=dataset.evidence,
        missing_information=dataset.missing_information,
        percentage_validation=percentage_validation,
        formal_ready=percentage_validation.is_complete,
    )


def ownership_graph_to_dot(graph: OwnershipGraph) -> str:
    """生成简洁的 Graphviz DOT；边只表达“股东持有企业股份”。"""

    if not graph.formal_ready:
        raise OwnershipBuildError(
            "ownership_not_formal_ready",
            "股权比例完整性校验未通过，不能生成正式股权结构图",
        )
    lines = [
        "digraph ownership {",
        '  graph [rankdir=TB, bgcolor="white", pad=0.25, nodesep=0.55, '
        'ranksep=0.95, splines=polyline, ordering=out, newrank=true, dpi=200];',
        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei", '
        'fontsize=11, color="#9CA3AF", penwidth=1.0, margin="0.16,0.10"];',
        '  edge [color="#4B5563", penwidth=1.25, arrowsize=0.72];',
    ]
    for node in graph.nodes:
        label = _graphviz_node_label(node)
        lines.append(
            f'  {json.dumps(node.node_id)} [label=<{label}>, '
            f'fillcolor="{_node_fill(node)}"];'
        )
    for level in range(1, graph.config.max_display_levels + 1):
        ids = [json.dumps(node.node_id) for node in graph.nodes if node.level == level]
        if ids:
            lines.append("  { rank=same; " + "; ".join(ids) + "; }")
    for edge in graph.edges:
        # 声明 target -> shareholder 仅为保持由上向下布局；dir=back 使箭头实际
        # 指向上方被持股企业，即“下方股东持有上方企业股份”。
        lines.append(
            f'  {json.dumps(edge.target_node_id)} -> {json.dumps(edge.source_node_id)} '
            '[dir=back, arrowtail=normal, arrowhead=none, style=solid];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _format_percentage(value: Optional[Decimal]) -> str:
    return "" if value is None else f"{value.quantize(Decimal('0.01'))}%"


def _wrap_name(name: str, *, width: int = 13) -> List[str]:
    """按字符宽度换行但不截断名称；英文空格优先作为自然断点。"""

    if len(name) <= width:
        return [name]
    lines: List[str] = []
    remaining = name
    while len(remaining) > width:
        split_at = remaining.rfind(" ", 0, width + 1)
        if split_at <= 0:
            split_at = width
        lines.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        lines.append(remaining)
    return lines


def _node_fill(node: OwnershipGraphNode) -> str:
    return {
        OwnershipNodeType.TARGET_COMPANY: "#E8F1FB",
        OwnershipNodeType.NATURAL_PERSON: "#FFF8E7",
        OwnershipNodeType.STATE_OWNED_PLATFORM: "#EAF5EE",
        OwnershipNodeType.GOVERNMENT_AUTHORITY: "#EAF5EE",
        OwnershipNodeType.AGGREGATED: "#F3F4F6",
    }.get(node.node_type, "#F8FAFC")


def _graphviz_node_label(node: OwnershipGraphNode) -> str:
    rows = [
        f'<TR><TD>{escape(line)}</TD></TR>' for line in _wrap_name(node.name)
    ]
    percentage = _format_percentage(node.ownership_percentage)
    if percentage:
        rows.append(
            f'<TR><TD><FONT POINT-SIZE="10" COLOR="#374151">'
            f'{escape(percentage)}</FONT></TD></TR>'
        )
    return (
        '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1">'
        + "".join(rows)
        + "</TABLE>"
    )


def _render_internal_svg(graph: OwnershipGraph, output_path: Path) -> None:
    """Graphviz 不可用时按分支宽度绘制树状 SVG，避免跨分支混排。"""

    width, height, positions = _calculate_tree_layout(graph)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><marker id="arrow" markerWidth="9" markerHeight="9" '
        'refX="8" refY="4.5" orient="auto"><path d="M0,0 L0,9 L9,4.5 z" '
        'fill="#4B5563"/></marker></defs>',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    for edge in graph.edges:
        source_x, source_y, _, _ = positions[edge.source_node_id]
        target_x, target_y, _, target_height = positions[edge.target_node_id]
        start_y = source_y
        end_y = target_y + target_height
        middle_y = (start_y + end_y) / 2
        parts.append(
            f'<path d="M {source_x} {start_y} V {middle_y} H {target_x} V {end_y}" '
            'fill="none" stroke="#4B5563" stroke-width="1.6" '
            'stroke-linejoin="round" marker-end="url(#arrow)"/>'
        )
    for node in graph.nodes:
        x, y, node_width, node_height = positions[node.node_id]
        parts.append(
            f'<rect x="{x-node_width/2}" y="{y}" width="{node_width}" '
            f'height="{node_height}" rx="8" fill="{_node_fill(node)}" '
            'stroke="#9CA3AF" stroke-width="1"/>'
        )
        text_y = y + 25
        for line in _wrap_name(node.name):
            parts.append(
                f'<text x="{x}" y="{text_y}" text-anchor="middle" font-size="14" '
                'font-family="Microsoft YaHei, sans-serif" fill="#1F2937">'
                f'{escape(line)}</text>'
            )
            text_y += 21
        percentage = _format_percentage(node.ownership_percentage)
        if percentage:
            parts.append(
                f'<text x="{x}" y="{text_y+3}" text-anchor="middle" font-size="13" '
                'font-family="Microsoft YaHei, sans-serif" fill="#374151">'
                f'{escape(percentage)}</text>'
            )
    parts.append("</svg>")
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _calculate_tree_layout(
    graph: OwnershipGraph,
) -> Tuple[int, int, Dict[str, Tuple[float, float, float, float]]]:
    """按子树占用宽度排布，保证同一直接持股分支保持在父节点下方。"""

    node_by_id = {node.node_id: node for node in graph.nodes}
    children: Dict[str, List[str]] = defaultdict(list)
    for edge in graph.edges:
        children[edge.target_node_id].append(edge.source_node_id)
    for target_id in children:
        children[target_id].sort(key=lambda node_id: (-len(children.get(node_id, [])), node_id))

    horizontal_step = 290.0
    left_margin = 155.0
    next_leaf = 0
    x_by_id: Dict[str, float] = {}
    visiting: Set[str] = set()

    def assign(node_id: str) -> float:
        nonlocal next_leaf
        if node_id in x_by_id:
            return x_by_id[node_id]
        if node_id in visiting:
            raise OwnershipBuildError("graph_layout_cycle", f"绘图分支存在环：{node_id}")
        visiting.add(node_id)
        child_ids = children.get(node_id, [])
        if child_ids:
            child_positions = [assign(child_id) for child_id in child_ids]
            x = (child_positions[0] + child_positions[-1]) / 2
        else:
            x = left_margin + next_leaf * horizontal_step
            next_leaf += 1
        visiting.remove(node_id)
        x_by_id[node_id] = x
        return x

    assign(graph.identity.entity_id)
    for node in sorted(graph.nodes, key=lambda item: (item.level, item.node_id)):
        if node.node_id not in x_by_id:
            assign(node.node_id)
    min_x = min(x_by_id.values(), default=left_margin)
    if min_x < left_margin:
        offset = left_margin - min_x
        x_by_id = {node_id: x + offset for node_id, x in x_by_id.items()}
    max_x = max(x_by_id.values(), default=left_margin)
    width = int(max(900.0, max_x + left_margin))
    max_level = max((node.level for node in graph.nodes), default=1)
    height = int(max(250.0, 55.0 + max_level * 180.0))
    positions: Dict[str, Tuple[float, float, float, float]] = {}
    for node in graph.nodes:
        line_count = len(_wrap_name(node.name))
        has_percentage = node.ownership_percentage is not None
        node_height = 30.0 + line_count * 21.0 + (24.0 if has_percentage else 5.0)
        positions[node.node_id] = (
            x_by_id[node.node_id],
            38.0 + (node.level - 1) * 180.0,
            250.0,
            node_height,
        )
    return width, height, positions


def _load_png_font(size: int):
    from PIL import ImageFont

    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_internal_png(graph: OwnershipGraph, output_path: Path) -> None:
    """用 Pillow 生成适合 Word 报告的高分辨率 PNG。"""

    try:
        from PIL import Image, ImageDraw
    except ImportError as error:  # pragma: no cover - 项目依赖中显式声明 Pillow
        raise OwnershipBuildError(
            "png_renderer_not_available", "缺少 Pillow，无法生成 PNG"
        ) from error
    width, height, positions = _calculate_tree_layout(graph)
    scale = 2
    image = Image.new("RGB", (width * scale, height * scale), "white")
    draw = ImageDraw.Draw(image)
    font_name = _load_png_font(14 * scale)
    font_percentage = _load_png_font(13 * scale)
    for edge in graph.edges:
        source_x, source_y, _, _ = positions[edge.source_node_id]
        target_x, target_y, _, target_height = positions[edge.target_node_id]
        points = [
            (source_x, source_y),
            (source_x, (source_y + target_y + target_height) / 2),
            (target_x, (source_y + target_y + target_height) / 2),
            (target_x, target_y + target_height + 4),
        ]
        scaled = [(int(x * scale), int(y * scale)) for x, y in points]
        draw.line(scaled, fill="#4B5563", width=3, joint="curve")
        tip_x, tip_y = int(target_x * scale), int((target_y + target_height) * scale)
        draw.polygon(
            [
                (tip_x, tip_y),
                (tip_x - 6 * scale, tip_y + 9 * scale),
                (tip_x + 6 * scale, tip_y + 9 * scale),
            ],
            fill="#4B5563",
        )
    for node in graph.nodes:
        x, y, node_width, node_height = positions[node.node_id]
        box = (
            int((x - node_width / 2) * scale),
            int(y * scale),
            int((x + node_width / 2) * scale),
            int((y + node_height) * scale),
        )
        draw.rounded_rectangle(
            box,
            radius=8 * scale,
            fill=_node_fill(node),
            outline="#9CA3AF",
            width=2,
        )
        text_y = int((y + 10) * scale)
        for line in _wrap_name(node.name):
            bounds = draw.textbbox((0, 0), line, font=font_name)
            text_width = bounds[2] - bounds[0]
            draw.text(
                (int(x * scale - text_width / 2), text_y),
                line,
                font=font_name,
                fill="#1F2937",
            )
            text_y += 21 * scale
        percentage = _format_percentage(node.ownership_percentage)
        if percentage:
            bounds = draw.textbbox((0, 0), percentage, font=font_percentage)
            text_width = bounds[2] - bounds[0]
            draw.text(
                (int(x * scale - text_width / 2), text_y + 2 * scale),
                percentage,
                font=font_percentage,
                fill="#374151",
            )
    image.save(output_path, format="PNG", dpi=(200, 200))


def render_ownership_graph(
    graph: OwnershipGraph,
    output_path: Path,
    *,
    require_graphviz: bool = False,
) -> OwnershipRenderResult:
    """输出 DOT、SVG 与 PNG；正式输出前必须通过逐分支比例校验。"""

    output_path = Path(output_path)
    if output_path.suffix.lower() != ".svg":
        raise OwnershipBuildError("unsupported_render_format", "V1.2 仅支持 SVG 输出")
    if not graph.formal_ready:
        raise OwnershipBuildError(
            "ownership_not_formal_ready",
            "股权比例完整性校验未通过，不能生成正式股权结构图",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dot_path = output_path.with_suffix(".dot")
    png_path = output_path.with_suffix(".png")
    dot_path.write_text(ownership_graph_to_dot(graph), encoding="utf-8")
    executable = shutil.which("dot")
    if executable:
        for format_name, destination in (("svg", output_path), ("png", png_path)):
            completed = subprocess.run(
                [
                    executable,
                    f"-T{format_name}",
                    "-Gdpi=200",
                    str(dot_path),
                    "-o",
                    str(destination),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise OwnershipBuildError(
                    "graphviz_render_failed",
                    completed.stderr.strip() or f"Graphviz {format_name.upper()} 渲染失败",
                )
        return OwnershipRenderResult(
            svg_path=output_path,
            png_path=png_path,
            dot_path=dot_path,
            engine="graphviz-dot",
        )
    if require_graphviz:
        raise OwnershipBuildError(
            "graphviz_not_available",
            "未找到 Graphviz dot；请安装 Graphviz 并将 dot 加入 PATH",
        )
    _render_internal_svg(graph, output_path)
    _render_internal_png(graph, png_path)
    return OwnershipRenderResult(
        svg_path=output_path,
        png_path=png_path,
        dot_path=dot_path,
        engine="internal-tree-fallback",
    )


def load_ownership_seed_catalog() -> OwnershipSeedCatalog:
    """读取随包分发、经人工逐页核对的以岭药业股权锚点。"""

    path = resources.files("credit_research_agent.data").joinpath(
        "ownership_seeds.json"
    )
    return OwnershipSeedCatalog.parse_raw(path.read_text(encoding="utf-8"))


def _model_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


class OwnershipBuilder:
    """复用 V0 证据构建器，将真实披露组织为 V1.2 原始数据和展示图。"""

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        identity: CompanyIdentity,
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
        seeds: Optional[OwnershipSeedCatalog] = None,
        config: Optional[OwnershipDisplayConfig] = None,
    ) -> OwnershipBuildResult:
        ownership_seeds = seeds or load_ownership_seed_catalog()
        if ownership_seeds.entity_id != identity.entity_id:
            raise EvidenceBuildError(
                "ownership_entity_mismatch",
                f"股权种子属于 {ownership_seeds.entity_id}，当前主体为 {identity.entity_id}",
            )
        identity_seeds = load_default_seed_catalog()
        evidence_ids = [
            item.evidence_id
            for item in [*identity_seeds.evidence, *ownership_seeds.evidence]
        ]
        duplicates = _duplicates(evidence_ids)
        if duplicates:
            raise EvidenceBuildError(
                "duplicate_evidence_seed",
                f"合并后的 evidence_id 存在重复：{sorted(duplicates)}",
            )
        evidence_bundle = EvidenceBuilder(clock=self._clock).build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            seeds=EvidenceSeedCatalog(
                evidence=[*identity_seeds.evidence, *ownership_seeds.evidence],
                facts=identity_seeds.facts,
            ),
        )
        dataset = OwnershipDataset(
            identity=evidence_bundle.identity,
            as_of_date=ownership_seeds.as_of_date,
            nodes=ownership_seeds.nodes,
            ownership_edges=ownership_seeds.ownership_edges,
            control_relationships=ownership_seeds.control_relationships,
            state_control_assessments=ownership_seeds.state_control_assessments,
            completeness_declarations=ownership_seeds.completeness_declarations,
            sources=evidence_bundle.sources,
            evidence=evidence_bundle.evidence,
            missing_information=ownership_seeds.missing_information,
            limitations=ownership_seeds.limitations,
        )
        validation = validate_ownership_facts(dataset)
        if not validation.is_valid:
            raise OwnershipBuildError(
                "invalid_ownership_facts",
                "; ".join(issue.message for issue in validation.errors),
            )
        graph = build_ownership_graph(dataset, config)
        return OwnershipBuildResult(
            dataset=dataset,
            graph=graph,
            validation=validation,
            percentage_validation=graph.percentage_validation,
        )


def _control_relation_text(relation_type: ControlRelationType) -> str:
    return {
        ControlRelationType.CONTROLLING_SHAREHOLDER: "控股股东",
        ControlRelationType.ACTUAL_CONTROLLER: "实际控制人",
        ControlRelationType.ULTIMATE_CONTROLLER: "最终控制主体",
        ControlRelationType.ACTING_IN_CONCERT: "一致行动关系",
        ControlRelationType.KEY_CONTROL_SUBJECT: "重要控制主体",
    }[relation_type]


def render_ownership_summary(result: OwnershipBuildResult) -> str:
    """把图外控制信息、证据来源和比例校验整理为可审计文字说明。"""

    node_by_id = {node.node_id: node for node in result.dataset.nodes}
    graph_node_by_id = {node.node_id: node for node in result.graph.nodes}
    lines = [
        f"# {result.dataset.identity.short_name}股权结构说明",
        "",
        f"- 数据时点：{result.dataset.as_of_date.isoformat()}",
        f"- 证券代码：{result.dataset.identity.ticker}",
        "- 图中仅展示主体名称、对其直接持股对象的持股比例及实线箭头；控制关系和证据在本说明中列示。",
        "",
        "## 图示股权关系",
        "",
    ]
    for edge in result.graph.edges:
        holder = graph_node_by_id[edge.source_node_id].name
        investee = graph_node_by_id[edge.target_node_id].name
        origin = {
            PercentageOrigin.DISCLOSED: "披露值",
            PercentageOrigin.AGGREGATED: "已披露股东聚合值",
            PercentageOrigin.CALCULATED_RESIDUAL: "同口径差额计算值",
        }[edge.percentage_origin]
        refs = " ".join(f"[{source_id}]" for source_id in edge.source_ids)
        lines.append(
            f"- {holder} → {investee}：{_format_percentage(edge.percentage)}（{origin}）{refs}"
        )
    lines.extend(["", "## 控制关系", ""])
    if result.dataset.control_relationships:
        for relationship in result.dataset.control_relationships:
            subjects = "、".join(
                node_by_id[node_id].name for node_id in relationship.subject_node_ids
            )
            controlled = node_by_id[relationship.controlled_node_id].name
            refs = " ".join(f"[{source_id}]" for source_id in relationship.source_ids)
            lines.append(
                f"- {subjects}：{controlled}的{_control_relation_text(relationship.relation_type)}。{refs}"
            )
    else:
        lines.append("- 当前资料未形成经核实的控制关系记录。")
    lines.extend(["", "## 股权比例完整性校验", ""])
    for branch in result.percentage_validation.branches:
        target = node_by_id.get(branch.target_node_id)
        target_name = target.name if target else branch.target_node_id
        status_text = {
            OwnershipCompletenessStatus.COMPLETE: "通过",
            OwnershipCompletenessStatus.COMPLETE_WITH_CALCULATED_RESIDUAL: "通过（含差额计算节点）",
            OwnershipCompletenessStatus.INCOMPLETE: "信息不完整",
            OwnershipCompletenessStatus.OVER_ALLOCATED: "合计超过100%",
            OwnershipCompletenessStatus.INCOMPARABLE_SCOPE: "口径不可比",
            OwnershipCompletenessStatus.UNVERIFIABLE: "无法核验",
        }[branch.status]
        lines.append(
            f"- {target_name}：已知合计{_format_percentage(branch.known_total)}，"
            f"口径 `{branch.capital_scope or '待核实'}`，结论：{status_text}。"
        )
    lines.extend(["", "## 来源", ""])
    for source in result.dataset.sources:
        lines.append(f"- [{source.source_id}] {source.title}：{source.url}")
    lines.extend(["", "## 局限与待核查", ""])
    for item in result.dataset.limitations:
        lines.append(f"- {item}")
    for item in result.dataset.missing_information:
        lines.append(f"- {item.topic}：{item.description}（原因：{item.reason}）")
    return "\n".join(lines) + "\n"


def render_ownership_questions(result: OwnershipBuildResult) -> str:
    """生成待核查事项，不用推断补齐未披露关系。"""

    node_by_id = {node.node_id: node for node in result.dataset.nodes}
    lines = ["# 股权结构待核查事项", ""]
    for branch in result.percentage_validation.branches:
        if branch.is_complete:
            continue
        target = node_by_id.get(branch.target_node_id)
        name = target.name if target else branch.target_node_id
        lines.append(f"- 核实{name}全部直接股东、基准日及股本口径：{branch.message}")
    for item in result.dataset.missing_information:
        lines.append(f"- {item.topic}：{item.description}（{item.reason}）")
    for node_id in result.graph.truncated_node_ids:
        node = node_by_id.get(node_id)
        lines.append(f"- {node.name if node else node_id}达到五层展示上限，需在图外继续核实上层关系。")
    if len(lines) == 2:
        lines.append("- 当前完整展开分支均已通过比例校验；仍需按最新工商登记和公告滚动复核。")
    return "\n".join(lines) + "\n"


def build_ownership_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    dataset_output: Optional[Path] = None,
    graph_output: Optional[Path] = None,
    svg_output: Optional[Path] = None,
    citations_output: Optional[Path] = None,
    summary_output: Optional[Path] = None,
    validation_output: Optional[Path] = None,
    sources_output: Optional[Path] = None,
    questions_output: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    seeds: Optional[OwnershipSeedCatalog] = None,
    config: Optional[OwnershipDisplayConfig] = None,
) -> OwnershipArtifacts:
    """生成完整 JSON、图、说明、比例校验和待核查事项。"""

    entity_dir = Path(output_dir) / identity.entity_id
    entity_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = Path(dataset_output) if dataset_output else entity_dir / "ownership.json"
    graph_path = Path(graph_output) if graph_output else entity_dir / "ownership_graph.json"
    svg_path = Path(svg_output) if svg_output else entity_dir / "ownership_structure.svg"
    citations_path = Path(citations_output) if citations_output else entity_dir / "citations.md"
    summary_result_path = (
        Path(summary_output) if summary_output else entity_dir / "ownership_summary.md"
    )
    validation_path = (
        Path(validation_output)
        if validation_output
        else entity_dir / "ownership_validation.json"
    )
    sources_path = (
        Path(sources_output) if sources_output else entity_dir / "ownership_sources.json"
    )
    questions_path = (
        Path(questions_output)
        if questions_output
        else entity_dir / "ownership_questions.md"
    )
    failure_path = entity_dir / "failure.json"
    writer = EvidenceArtifactWriter()
    failure_clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        result = OwnershipBuilder(clock=clock).build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            seeds=seeds,
            config=config,
        )
        for path in (
            dataset_path,
            graph_path,
            svg_path,
            citations_path,
            summary_result_path,
            validation_path,
            sources_path,
            questions_path,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        writer._atomic_write(  # 复用 V0 的原子写入，避免中断后留下半个 JSON。
            dataset_path,
            json.dumps(_model_dict(result.dataset), ensure_ascii=False, indent=2) + "\n",
        )
        writer._atomic_write(
            graph_path,
            json.dumps(_model_dict(result.graph), ensure_ascii=False, indent=2) + "\n",
        )
        # 引用渲染器只依赖 identity/evidence/sources；用原始证据包兼容模型构造。
        citation_bundle = EvidenceBundle(
            identity=result.dataset.identity,
            generated_at=failure_clock(),
            facts=[],
            evidence=result.dataset.evidence,
            sources=result.dataset.sources,
            limitations=result.dataset.limitations,
        )
        writer._atomic_write(citations_path, render_citations(citation_bundle))
        writer._atomic_write(summary_result_path, render_ownership_summary(result))
        writer._atomic_write(
            validation_path,
            json.dumps(
                _model_dict(result.percentage_validation), ensure_ascii=False, indent=2
            )
            + "\n",
        )
        writer._atomic_write(
            sources_path,
            json.dumps(
                [_model_dict(source_item) for source_item in result.dataset.sources],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        writer._atomic_write(questions_path, render_ownership_questions(result))
        rendered = render_ownership_graph(result.graph, svg_path)
        if failure_path.exists():
            failure_path.unlink()
        return OwnershipArtifacts(
            entity_id=identity.entity_id,
            dataset_path=dataset_path.resolve(),
            graph_path=graph_path.resolve(),
            svg_path=rendered.svg_path.resolve(),
            png_path=rendered.png_path.resolve(),
            dot_path=rendered.dot_path.resolve(),
            citations_path=citations_path.resolve(),
            summary_path=summary_result_path.resolve(),
            validation_path=validation_path.resolve(),
            sources_path=sources_path.resolve(),
            questions_path=questions_path.resolve(),
            render_engine=rendered.engine,
            node_count=len(result.graph.nodes),
            edge_count=len(result.graph.edges),
            control_relationship_count=len(result.graph.control_relationships),
            missing_information_count=len(result.graph.missing_information),
        )
    except (EvidenceBuildError, OwnershipBuildError) as error:
        writer.write_failure(
            failure_path,
            EvidenceBuildFailureRecord(
                entity_id=identity.entity_id,
                source_id=source.source_id,
                attempted_at=failure_clock(),
                error_code=getattr(error, "code", "ownership_build_failed"),
                error_message=str(error),
            ),
        )
        raise
