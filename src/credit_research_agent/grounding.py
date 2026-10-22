"""统一的可引用事实视图与现有证据模型适配器。

本模块不替代 ``FactRecord``、``EvidenceRecord`` 或股权数据模型。它只把已有
结构转换为报告生成阶段可消费的只读事实目录，并显式区分披露事实、Python
计算结果、已核验外部事实和未核验候选信息。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from credit_research_agent.models import EvidenceBundle, EvidenceStatus


class FactProvenance(str, Enum):
    DISCLOSED_FACT = "disclosed_fact"
    PYTHON_CALCULATION = "python_calculation"
    VERIFIED_EXTERNAL_FACT = "verified_external_fact"


class FactVerificationStatus(str, Enum):
    VERIFIED = "verified"
    CALCULATED = "calculated"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"


class AnalysisClaimType(str, Enum):
    SUPPORTED_INFERENCE = "supported_inference"
    RISK_SIGNAL = "risk_signal"
    DUE_DILIGENCE_QUESTION = "due_diligence_question"
    UNVERIFIED_HYPOTHESIS = "unverified_hypothesis"


class CitableFact(BaseModel):
    """供报告引用的统一事实；数值和来源由Python渲染。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: str = Field(min_length=1)
    unit: Optional[str] = None
    period: Optional[str] = None
    as_of_date: Optional[date] = None
    source_id: Optional[str] = None
    additional_source_ids: List[str] = Field(default_factory=list)
    source_type: Optional[str] = None
    source_location: Optional[str] = None
    source_url: Optional[str] = None
    derived_from: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    provenance: FactProvenance
    verification_status: FactVerificationStatus

    @model_validator(mode="after")
    def validate_provenance(self) -> "CitableFact":
        if self.provenance is FactProvenance.PYTHON_CALCULATION:
            if self.verification_status is not FactVerificationStatus.CALCULATED:
                raise ValueError("Python计算事实必须标记为calculated")
            if not self.derived_from:
                raise ValueError("Python计算事实必须记录derived_from")
        elif self.verification_status is FactVerificationStatus.CALCULATED:
            raise ValueError("只有Python计算结果可以标记为calculated")
        if self.verification_status in {
            FactVerificationStatus.VERIFIED,
            FactVerificationStatus.CALCULATED,
        } and self.source_id is None:
            raise ValueError("可引用事实必须关联source_id")
        return self

    @property
    def is_citable(self) -> bool:
        return self.verification_status in {
            FactVerificationStatus.VERIFIED,
            FactVerificationStatus.CALCULATED,
        }

    @property
    def source_ids(self) -> List[str]:
        values = ([self.source_id] if self.source_id else []) + list(
            self.additional_source_ids
        )
        return list(dict.fromkeys(values))


class GroundedMissingInformation(BaseModel):
    """资料缺口不是企业事实，只能生成待核查说明或尽调问题。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    missing_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_id: Optional[str] = None
    source_type: Optional[str] = None
    source_location: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)


class GroundedAnalysisClaim(BaseModel):
    """LLM可撰写的分析句；必须显式绑定事实或资料缺口。"""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^A\d{3,}$")
    claim_type: AnalysisClaimType
    statement: str = Field(min_length=2, max_length=500)
    supporting_fact_ids: List[str] = Field(default_factory=list)
    related_missing_ids: List[str] = Field(default_factory=list)


class FactGroundingCatalog(BaseModel):
    """V1.4可复用的事实/缺口读取接口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    facts: List[CitableFact] = Field(default_factory=list)
    missing_information: List[GroundedMissingInformation] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "FactGroundingCatalog":
        fact_ids = [item.fact_id for item in self.facts]
        missing_ids = [item.missing_id for item in self.missing_information]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("统一事实目录中的fact_id不得重复")
        if len(missing_ids) != len(set(missing_ids)):
            raise ValueError("统一事实目录中的missing_id不得重复")
        if set(fact_ids).intersection(missing_ids):
            raise ValueError("fact_id与missing_id不得冲突")
        return self

    def citable_facts(self) -> List[CitableFact]:
        return [item for item in self.facts if item.is_citable]


def _source_location(evidence_items: Sequence[Any]) -> Optional[str]:
    locations: List[str] = []
    for item in evidence_items:
        page = getattr(item, "page_number", None)
        section = getattr(item, "page_or_section", None)
        if page is not None:
            location = f"PDF第{page}页"
            if section:
                location += f" / {section}"
        else:
            location = str(section or "").strip()
        if location and location not in locations:
            locations.append(location)
    return "；".join(locations) or None


def build_evidence_bundle_grounding_catalog(
    bundle: EvidenceBundle,
) -> FactGroundingCatalog:
    """把V0/V1.1事实证据包转换为统一引用接口。"""

    evidence = {item.evidence_id: item for item in bundle.evidence}
    sources = {item.source_id: item for item in bundle.sources}
    facts: List[CitableFact] = []
    for item in bundle.facts:
        linked_evidence = [
            evidence[evidence_id]
            for evidence_id in item.evidence_ids
            if evidence_id in evidence
        ]
        source_ids = list(
            dict.fromkeys(record.source_id for record in linked_evidence)
        )
        primary_source_id = source_ids[0] if source_ids else None
        source = sources.get(primary_source_id) if primary_source_id else None
        statuses = {record.status for record in linked_evidence}
        if statuses == {EvidenceStatus.VERIFIED}:
            verification = FactVerificationStatus.VERIFIED
        elif EvidenceStatus.CONFLICTING in statuses:
            verification = FactVerificationStatus.CONFLICTING
        else:
            verification = FactVerificationStatus.UNVERIFIED
        facts.append(
            CitableFact(
                fact_id=item.fact_id,
                company=bundle.identity.legal_name,
                subject=bundle.identity.legal_name,
                metric=item.field_name,
                value=item.value,
                period=item.period,
                source_id=primary_source_id,
                additional_source_ids=source_ids[1:],
                source_type=(source.source_type.value if source else None),
                source_location=_source_location(linked_evidence),
                source_url=(str(source.url) if source else None),
                evidence_ids=list(item.evidence_ids),
                provenance=FactProvenance.DISCLOSED_FACT,
                verification_status=verification,
            )
        )
    return FactGroundingCatalog(facts=facts)


def _decimal_text(value: Any) -> str:
    decimal = Decimal(str(value))
    normalized = format(decimal.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _mapping_source_location(
    evidence_ids: Sequence[str], evidence: Mapping[str, Mapping[str, Any]]
) -> Optional[str]:
    locations: List[str] = []
    for evidence_id in evidence_ids:
        item = evidence.get(evidence_id)
        if not item:
            continue
        page = item.get("page_number")
        section = item.get("page_or_section")
        location = f"PDF第{page}页" if page is not None else ""
        if section:
            location += (" / " if location else "") + str(section)
        if location and location not in locations:
            locations.append(location)
    return "；".join(locations) or None


def build_ownership_grounding_catalog(
    graph: Mapping[str, Any], validation: Mapping[str, Any]
) -> FactGroundingCatalog:
    """从现有ownership图和比例校验结果创建统一事实目录。"""

    identity = graph["identity"]
    company = str(identity["legal_name"])
    nodes = {str(item["node_id"]): item for item in graph["nodes"]}
    sources = {str(item["source_id"]): item for item in graph.get("sources", [])}
    evidence = {
        str(item["evidence_id"]): item
        for item in graph.get("evidence", [])
        if item.get("evidence_id")
    }
    branches = list(validation.get("branches", []))

    def source_fields(source_ids: Sequence[str]) -> Dict[str, Any]:
        primary_id = str(source_ids[0]) if source_ids else None
        primary = sources.get(primary_id, {}) if primary_id else {}
        source_type = primary.get("source_type")
        if isinstance(source_type, Enum):
            source_type = source_type.value
        return {
            "source_id": primary_id,
            "additional_source_ids": [str(item) for item in source_ids[1:]],
            "source_type": str(source_type) if source_type else None,
            "source_url": str(primary.get("url")) if primary.get("url") else None,
        }

    facts: List[CitableFact] = []
    for edge in graph.get("edges", []):
        source_ids = [str(item) for item in edge.get("source_ids", [])]
        evidence_ids = [str(item) for item in edge.get("evidence_ids", [])]
        origin = str(edge.get("percentage_origin", "disclosed"))
        calculated = origin != "disclosed"
        derived_from: List[str] = []
        if calculated:
            for branch in branches:
                if (
                    branch.get("target_node_id") == edge.get("target_node_id")
                    and branch.get("capital_scope") == edge.get("capital_scope")
                ):
                    derived_from = [str(item) for item in branch.get("edge_ids", [])]
                    break
        status = str(edge.get("status", "unverified"))
        verification = (
            FactVerificationStatus.CALCULATED
            if calculated
            else (
                FactVerificationStatus.VERIFIED
                if status == "verified"
                else FactVerificationStatus.UNVERIFIED
            )
        )
        relation_type = str(edge.get("relation_type", "equity_ownership"))
        facts.append(
            CitableFact(
                fact_id=str(edge["edge_id"]),
                company=str(nodes[str(edge["target_node_id"])]["name"]),
                subject=str(nodes[str(edge["source_node_id"])]["name"]),
                metric=(
                    "voting_rights_percentage"
                    if relation_type == "voting_rights"
                    else "equity_ownership_percentage"
                ),
                value=_decimal_text(edge["percentage"]),
                unit="%",
                as_of_date=edge.get("as_of_date"),
                source_location=_mapping_source_location(evidence_ids, evidence),
                derived_from=derived_from,
                evidence_ids=evidence_ids,
                provenance=(
                    FactProvenance.PYTHON_CALCULATION
                    if calculated
                    else FactProvenance.DISCLOSED_FACT
                ),
                verification_status=verification,
                **source_fields(source_ids),
            )
        )

    for relationship in graph.get("control_relationships", []):
        source_ids = [str(item) for item in relationship.get("source_ids", [])]
        evidence_ids = [str(item) for item in relationship.get("evidence_ids", [])]
        status = str(relationship.get("status", "unverified"))
        subjects = [
            str(nodes[str(node_id)]["name"])
            for node_id in relationship.get("subject_node_ids", [])
        ]
        facts.append(
            CitableFact(
                fact_id=str(relationship["relationship_id"]),
                company=str(nodes[str(relationship["controlled_node_id"])]["name"]),
                subject="、".join(subjects),
                metric=f"control_relationship.{relationship['relation_type']}",
                value="已核实",
                as_of_date=relationship.get("as_of_date"),
                source_location=_mapping_source_location(evidence_ids, evidence),
                evidence_ids=evidence_ids,
                provenance=FactProvenance.DISCLOSED_FACT,
                verification_status=(
                    FactVerificationStatus.VERIFIED
                    if status == "verified"
                    else FactVerificationStatus.UNVERIFIED
                ),
                **source_fields(source_ids),
            )
        )

    for index, branch in enumerate(branches, start=1):
        source_ids = [str(item) for item in branch.get("source_ids", [])]
        evidence_ids = [str(item) for item in branch.get("evidence_ids", [])]
        target = str(branch["target_node_id"])
        facts.append(
            CitableFact(
                fact_id=f"PV{index:03d}",
                company=str(nodes.get(target, {"name": target})["name"]),
                subject=str(nodes.get(target, {"name": target})["name"]),
                metric="known_direct_holding_total",
                value=_decimal_text(branch["known_total"]),
                unit="%",
                as_of_date=graph.get("as_of_date"),
                source_location=_mapping_source_location(evidence_ids, evidence),
                derived_from=[str(item) for item in branch.get("edge_ids", [])],
                evidence_ids=evidence_ids,
                provenance=FactProvenance.PYTHON_CALCULATION,
                verification_status=FactVerificationStatus.CALCULATED,
                **source_fields(source_ids),
            )
        )

    missing_items: List[GroundedMissingInformation] = []
    for index, item in enumerate(graph.get("missing_information", []), start=1):
        evidence_ids = [str(value) for value in item.get("evidence_ids", [])]
        source_ids = list(
            dict.fromkeys(
                str(evidence[evidence_id]["source_id"])
                for evidence_id in evidence_ids
                if evidence_id in evidence and evidence[evidence_id].get("source_id")
            )
        )
        source = sources.get(source_ids[0], {}) if source_ids else {}
        source_type = source.get("source_type")
        if isinstance(source_type, Enum):
            source_type = source_type.value
        missing_items.append(
            GroundedMissingInformation(
                missing_id=str(item.get("missing_id") or f"M{index:03d}"),
                company=company,
                subject=company,
                topic=str(item["topic"]),
                description=str(item["description"]),
                source_id=source_ids[0] if source_ids else None,
                source_type=str(source_type) if source_type else None,
                source_location=_mapping_source_location(evidence_ids, evidence),
                evidence_ids=evidence_ids,
            )
        )

    return FactGroundingCatalog(facts=facts, missing_information=missing_items)
