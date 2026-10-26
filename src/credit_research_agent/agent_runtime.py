"""V1.3B 由 LLM 驱动、但受确定性边界约束的主 Agent。

模型只能调用一个高层股权研究 Tool。该 Tool 不接受文件路径，而是复用调用方已
准备并锁定的主体、官方来源、PDF解析产物和 V1.3A ``SkillRuntime``。这样既能证明
Skill 在应用运行时被加载，也不会向模型开放 Python、Shell 或任意文件访问能力。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set
from uuid import uuid4

from agents import (
    Agent,
    Model,
    RunConfig,
    RunContextWrapper,
    Runner,
    Usage,
    function_tool,
)
from agents.exceptions import MaxTurnsExceeded
from agents.lifecycle import RunHooksBase
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from credit_research_agent.documents import DownloadResult
from credit_research_agent.grounding import (
    AnalysisClaimType,
    CitableFact,
    FactGroundingCatalog,
    FactProvenance,
    FactVerificationStatus,
    GroundedAnalysisClaim,
    GroundedMissingInformation,
    build_ownership_grounding_catalog,
)
from credit_research_agent.llm_config import (
    AgentMode,
    DeepSeekLLMConfig,
    LLMConfigError,
    build_live_deepseek_model,
    build_model_settings,
)
from credit_research_agent.models import CompanyIdentity, SourceRecord
from credit_research_agent.skill_runtime import (
    SkillExecutionResult,
    SkillRuntime,
    SkillRuntimeError,
)
from credit_research_agent.sources import SourceCatalog, SourceNotFoundError


OWNERSHIP_FUNCTION_TOOL = "run_ownership_research"
OWNERSHIP_TASK_KEYWORDS = ("股权", "股东", "持股", "控制关系", "穿透图")
OWNERSHIP_RESPONSE_CONTRACT_VERSION = "2.0"
CONTROL_CLAIM_KEYWORDS = {
    "controlling_shareholder": "控股股东",
    "actual_controller": "实际控制人",
    "acting_in_concert": "一致行动",
    "ultimate_controller": "最终控制主体",
}
CONTROL_UNCERTAINTY_MARKERS = ("无法确认", "不能确认", "未能确认", "证据不足")

MAIN_AGENT_INSTRUCTIONS = """你是企业授信研究 Agent。当前版本只允许研究股权结构。

执行规则：
1. 用户要求股权、股东、控制关系或股权穿透图时，必须调用 run_ownership_research；不得只凭常识回答。
2. Tool 只需要填写ticker。官方来源由Python Runtime从已验证运行上下文绑定，禁止自行提供、修改或猜测source_id。
3. Tool 会在运行时通过白名单 Registry 选择 ownership-structure、只读加载 SKILL.md，并调用确定性 Python Tool。
4. 企业实际持股比例只能引用 Tool 返回的已验证事实；不得把 Skill 中的5%、25%等绘图/聚合阈值当成企业实际持股比例。
5. 不得自行加总、相减或推导未经 Tool 验证的股权数据；Python计算值只能通过 Tool 返回的Fact ID引用。
6. 控股股东、实际控制人、一致行动或最终控制主体只能引用 response_contract 中对应的控制关系Fact ID及来源，不得扩大或改写主体范围。
7. 最终研究说明以企业实际情况为主，不主动解释绘图阈值、聚合规则或其他内部技术规则。
8. Tool调用完成后，只输出符合response_contract.final_response_schema的JSON对象。fact_ids和missing_information_ids必须完整复制契约中的必需ID；不得在分析句中自行填写数字、百分比、年份、来源编号、链接或新的主体事实。
9. 你可以在analysis_claims中撰写分析判断、风险关注、待核实假设或尽调问题。每条判断必须通过supporting_fact_ids关联已核实企业事实或Python计算；尽调问题必须通过related_missing_ids关联资料缺口。不得用未核验网页候选信息支持确定性结论。
10. Python将验证Fact ID、分析类型、支持关系和语义边界，再负责渲染具体名称、数值、期间、来源和资料限制。校验失败的分析文字不得进入正式输出，但已生成的股权图和JSON继续保留。
"""


class AgentExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentExecutionStage(str, Enum):
    RUN_STARTED = "run_started"
    RUNTIME_SOURCE_BOUND = "runtime_source_bound"
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    LLM_TOOL_SELECTED = "llm_tool_selected"
    SKILL_RUNTIME_COMPLETED = "skill_runtime_completed"
    STRUCTURED_OUTPUT_PARSED = "structured_output_parsed"
    GROUNDED_OUTPUT_VALIDATED = "grounded_output_validated"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class AgentExecutionEvent(BaseModel):
    sequence: int = Field(ge=1)
    occurred_at: datetime
    stage: AgentExecutionStage
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class AgentTokenUsage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: Optional[float] = None
    cost_note: str = (
        "SDK未返回可作为账单依据的金额；实际费用以DeepSeek控制台账单为准。"
    )


class AgentExecutionTrace(BaseModel):
    schema_version: str = "1.3.5"
    trace_id: str
    task: str
    status: AgentExecutionStatus = AgentExecutionStatus.RUNNING
    config: Dict[str, Any]
    entity_id: str
    source_id: str
    bound_source_id: Optional[str] = None
    llm_call_count: int = 0
    tool_call_count: int = 0
    selected_tool: Optional[str] = None
    selected_skill: Optional[str] = None
    loaded_skill_path: Optional[str] = None
    skill_trace_path: Optional[str] = None
    percentage_validation_passed: Optional[bool] = None
    artifact_paths: List[str] = Field(default_factory=list)
    tool_call_arguments: List[Dict[str, Any]] = Field(default_factory=list)
    returned_fact_ids: List[str] = Field(default_factory=list)
    selected_fact_ids: List[str] = Field(default_factory=list)
    returned_missing_information_ids: List[str] = Field(default_factory=list)
    selected_missing_information_ids: List[str] = Field(default_factory=list)
    selected_analysis_claim_ids: List[str] = Field(default_factory=list)
    deterministic_artifacts_preserved: bool = False
    finish_reasons: List[Optional[str]] = Field(default_factory=list)
    model_candidate_output: Optional[str] = None
    model_candidate_output_sha256: Optional[str] = None
    model_candidate_output_truncated: bool = False
    rejected_sentence: Optional[str] = None
    validator_rule_id: Optional[str] = None
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    events: List[AgentExecutionEvent] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class OwnershipToolRequest(BaseModel):
    """模型可见的唯一 Tool 输入；来源和路径均由Runtime绑定。"""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=4, max_length=20)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()


class GroundedPercentageFact(BaseModel):
    """允许模型在最终回答中复述的一项比例。"""

    fact_id: str
    value: Decimal
    fact_type: str
    description: str
    origin: str
    semantic_class: str
    subject: Optional[str] = None
    company: Optional[str] = None
    as_of_date: Optional[str] = None
    allowed_in_final: bool = True
    source_ids: List[str] = Field(default_factory=list)


class GroundedControlClaim(BaseModel):
    """允许模型复述的一项已核实控制关系。"""

    claim_id: str
    relation_type: str
    subjects: List[str]
    controlled_entity: str
    source_ids: List[str]


class GroundedFactReference(BaseModel):
    """供模型选择、由Python渲染的最小事实引用。"""

    fact_id: str
    fact_class: str
    source_ids: List[str] = Field(default_factory=list)
    required_for_final: bool = True


class PercentageRuleReference(BaseModel):
    """Skill/校验规则中的比例，不是企业实际持股事实。"""

    rule_id: str
    value: Decimal
    rule_class: str
    description: str
    allowed_in_final: bool = False


class OwnershipResponseContract(BaseModel):
    """Tool 到 LLM 的最小事实白名单，由 Python 确定性构造。"""

    version: str = OWNERSHIP_RESPONSE_CONTRACT_VERSION
    allowed_source_ids: List[str]
    allowed_entity_names: List[str]
    allowed_percentages: List[GroundedPercentageFact]
    allowed_control_claims: List[GroundedControlClaim]
    grounded_facts: List[CitableFact]
    grounded_missing_information: List[GroundedMissingInformation]
    fact_catalog: List[GroundedFactReference]
    required_fact_ids: List[str]
    required_missing_information_ids: List[str]
    percentage_rules: List[PercentageRuleReference]
    final_response_schema: Dict[str, Any]
    prohibit_unlisted_percentages: bool = True
    prohibit_llm_arithmetic: bool = True


class OwnershipFactSelection(BaseModel):
    """LLM选择事实ID，并可提交绑定事实的分析文字。"""

    model_config = ConfigDict(extra="forbid")

    fact_ids: List[str] = Field(min_length=1)
    missing_information_ids: List[str] = Field(default_factory=list)
    analysis_claims: List[GroundedAnalysisClaim] = Field(default_factory=list)

    @field_validator("fact_ids", "missing_information_ids")
    @classmethod
    def validate_unique_fact_ids(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("引用ID不得重复")
        return value

    @model_validator(mode="after")
    def validate_unique_claim_ids(self) -> "OwnershipFactSelection":
        claim_ids = [item.claim_id for item in self.analysis_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("analysis_claims.claim_id不得重复")
        return self


class AgentRunResult(BaseModel):
    final_output: str
    trace_path: Path
    skill_trace_path: Path
    selected_skill: str
    tool_name: str
    artifacts: Dict[str, Any]
    usage: AgentTokenUsage
    config: Dict[str, Any]


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        trace_path: Optional[Path] = None,
        rule_id: Optional[str] = None,
        rejected_text: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace_path = trace_path
        self.rule_id = rule_id
        self.rejected_text = rejected_text


@dataclass
class OwnershipAgentContext:
    project_root: Path
    task: str
    identity: CompanyIdentity
    source: SourceRecord
    download: DownloadResult
    summary_path: Path
    pages_path: Path
    ownership_dir: Path
    skill_trace_path: Path
    max_tool_calls: int
    trace: AgentExecutionTrace
    clock: Callable[[], datetime]
    tool_output_overrides: Mapping[str, Any] = field(default_factory=dict)
    skill_runtime_factory: Callable[[Path], SkillRuntime] = SkillRuntime
    tool_call_count: int = 0
    skill_result: Optional[SkillExecutionResult] = None
    compact_result: Optional[Dict[str, Any]] = None


def _json_load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifact_paths(artifacts: Mapping[str, Any]) -> List[str]:
    return sorted(
        str(value)
        for name, value in artifacts.items()
        if name.endswith("_path") and value
    )


def _percentage_value(value: Any) -> Decimal:
    """把 Tool 数值统一到百分点后两位，供契约和校验共用。"""

    return Decimal(str(value)).quantize(Decimal("0.01"))


def build_ownership_response_contract(
    payload: Mapping[str, Any],
) -> OwnershipResponseContract:
    """从已核实 Tool 结果构造 LLM 可复述的事实白名单。

    LLM 不参与任何比例计算。关系比例来自已核实边；分支合计和差额来自
    ``validate_ownership_percentages`` 的确定性输出。
    """

    raw_catalog = payload.get("grounding_catalog")
    if raw_catalog is not None:
        grounding_catalog = FactGroundingCatalog.model_validate(raw_catalog)
    else:
        # 兼容直接构造的测试/调用载荷；正式Tool路径使用ownership适配器。
        fallback_facts: List[CitableFact] = []
        branches = list(payload["percentage_validation"]["branches"])
        for item in payload["verified_relations"]:
            calculated = item["percentage_origin"] != "disclosed"
            derived_from: List[str] = []
            if calculated:
                for branch in branches:
                    if branch.get("target_node_id") == item.get("target_node_id"):
                        derived_from = list(branch.get("edge_ids", []))
                        break
                if not derived_from:
                    derived_from = list(item.get("derived_from", []))
            source_ids = list(item.get("source_ids", []))
            fallback_facts.append(
                CitableFact(
                    fact_id=str(item["fact_id"]),
                    company=str(item["held_entity"]),
                    subject=str(item["holder"]),
                    metric="equity_ownership_percentage",
                    value=str(item["percentage"]),
                    unit="%",
                    as_of_date=item.get("as_of_date") or payload.get("as_of_date"),
                    source_id=source_ids[0] if source_ids else None,
                    additional_source_ids=source_ids[1:],
                    source_type=item.get("source_type"),
                    source_location=item.get("source_location"),
                    derived_from=derived_from,
                    evidence_ids=list(item.get("evidence_ids", [])),
                    provenance=(
                        FactProvenance.PYTHON_CALCULATION
                        if calculated
                        else FactProvenance.DISCLOSED_FACT
                    ),
                    verification_status=(
                        FactVerificationStatus.CALCULATED
                        if calculated
                        else FactVerificationStatus.VERIFIED
                    ),
                )
            )
        for item in payload["verified_control_relationships"]:
            source_ids = list(item.get("source_ids", []))
            fallback_facts.append(
                CitableFact(
                    fact_id=str(item["claim_id"]),
                    company=str(item["controlled_entity"]),
                    subject="、".join(item["subjects"]),
                    metric=f"control_relationship.{item['relation_type']}",
                    value="已核实",
                    as_of_date=item.get("as_of_date") or payload.get("as_of_date"),
                    source_id=source_ids[0] if source_ids else None,
                    additional_source_ids=source_ids[1:],
                    source_type=item.get("source_type"),
                    source_location=item.get("source_location"),
                    evidence_ids=list(item.get("evidence_ids", [])),
                    provenance=FactProvenance.DISCLOSED_FACT,
                    verification_status=FactVerificationStatus.VERIFIED,
                )
            )
        for index, branch in enumerate(branches, start=1):
            source_ids = list(branch.get("source_ids", []))
            fallback_facts.append(
                CitableFact(
                    fact_id=str(branch.get("validation_id") or f"PV{index:03d}"),
                    company=str(branch.get("target_name") or branch["target_node_id"]),
                    subject=str(branch.get("target_name") or branch["target_node_id"]),
                    metric="known_direct_holding_total",
                    value=str(branch["known_total"]),
                    unit="%",
                    as_of_date=payload.get("as_of_date"),
                    source_id=source_ids[0] if source_ids else None,
                    additional_source_ids=source_ids[1:],
                    source_type=branch.get("source_type"),
                    source_location=branch.get("source_location"),
                    derived_from=list(branch.get("edge_ids", [])),
                    evidence_ids=list(branch.get("evidence_ids", [])),
                    provenance=FactProvenance.PYTHON_CALCULATION,
                    verification_status=FactVerificationStatus.CALCULATED,
                )
            )
        fallback_missing = []
        for index, item in enumerate(payload.get("missing_information", []), start=1):
            source_ids = list(item.get("source_ids", []))
            fallback_missing.append(
                GroundedMissingInformation(
                    missing_id=str(item.get("missing_id") or f"M{index:03d}"),
                    company=str(payload["identity"]["legal_name"]),
                    subject=str(payload["identity"]["legal_name"]),
                    topic=str(item["topic"]),
                    description=str(item["description"]),
                    source_id=source_ids[0] if source_ids else None,
                    source_type=item.get("source_type"),
                    source_location=item.get("source_location"),
                    evidence_ids=list(item.get("evidence_ids", [])),
                )
            )
        grounding_catalog = FactGroundingCatalog(
            facts=fallback_facts, missing_information=fallback_missing
        )

    citable_facts = grounding_catalog.citable_facts()
    percentage_facts: List[GroundedPercentageFact] = []
    for index, item in enumerate(payload["verified_relations"], start=1):
        fact_id = str(item.get("fact_id") or f"holding-{index:03d}")
        is_calculated = item["percentage_origin"] != "disclosed"
        percentage_facts.append(
            GroundedPercentageFact(
                fact_id=fact_id,
                value=_percentage_value(item["percentage"]),
                fact_type="holding_percentage",
                description=f"{item['holder']}持有{item['held_entity']}的比例",
                origin=str(item["percentage_origin"]),
                semantic_class=(
                    "python_calculation" if is_calculated else "enterprise_fact"
                ),
                subject=str(item["holder"]),
                company=str(item["held_entity"]),
                as_of_date=str(item.get("as_of_date") or payload.get("as_of_date")),
                source_ids=list(item["source_ids"]),
            )
        )

    for index, branch in enumerate(
        payload["percentage_validation"]["branches"], start=1
    ):
        validation_id = str(branch.get("validation_id") or f"PV{index:03d}")
        percentage_facts.append(
            GroundedPercentageFact(
                fact_id=f"{validation_id}-known-total",
                value=_percentage_value(branch["known_total"]),
                fact_type="branch_known_total",
                description=(
                    f"节点{branch['target_node_id']}同一口径的已知直接持股比例合计"
                ),
                origin="calculated_sum",
                semantic_class="python_calculation",
                subject=str(branch.get("target_name") or branch["target_node_id"]),
                company=str(branch.get("target_name") or branch["target_node_id"]),
                as_of_date=str(payload.get("as_of_date")),
                source_ids=list(branch.get("source_ids", [])),
            )
        )
        if branch.get("calculated_residual") is not None:
            percentage_facts.append(
                GroundedPercentageFact(
                    fact_id=f"{validation_id}-residual",
                    value=_percentage_value(branch["calculated_residual"]),
                    fact_type="calculated_residual",
                    description=(
                        f"节点{branch['target_node_id']}为达到完整股本口径计算的差额"
                    ),
                    origin="calculated_residual",
                    semantic_class="python_calculation",
                    subject=str(branch.get("target_name") or branch["target_node_id"]),
                    company=str(branch.get("target_name") or branch["target_node_id"]),
                    as_of_date=str(payload.get("as_of_date")),
                    source_ids=list(branch.get("source_ids", [])),
                )
            )

    # 100%只表示完整性校验目标，不代表任何股东的持股事实。
    percentage_facts.append(
        GroundedPercentageFact(
            fact_id="validation-target-100",
            value=Decimal("100.00"),
            fact_type="validation_target",
            description="单个被展开主体的直接股东比例完整性校验目标",
            origin="validation_rule",
            semantic_class="validation_rule",
            source_ids=[],
        )
    )

    control_claims = [
        GroundedControlClaim(
            claim_id=str(item.get("claim_id") or f"control-{index:03d}"),
            relation_type=item["relation_type"],
            subjects=list(item["subjects"]),
            controlled_entity=item["controlled_entity"],
            source_ids=list(item["source_ids"]),
        )
        for index, item in enumerate(
            payload["verified_control_relationships"], start=1
        )
    ]
    fact_catalog = [
        GroundedFactReference(
            fact_id=item.fact_id,
            fact_class=item.provenance.value,
            source_ids=item.source_ids,
        )
        for item in citable_facts
    ]
    entity_names = {
        payload["identity"]["legal_name"],
        payload["identity"]["short_name"],
    }
    for item in payload["verified_relations"]:
        entity_names.update((item["holder"], item["held_entity"]))
    for item in payload["verified_control_relationships"]:
        entity_names.update(item["subjects"])
        entity_names.add(item["controlled_entity"])

    return OwnershipResponseContract(
        allowed_source_ids=sorted(
            {item["source_id"] for item in payload["sources"]}
        ),
        allowed_entity_names=sorted(entity_names),
        allowed_percentages=percentage_facts,
        allowed_control_claims=control_claims,
        grounded_facts=grounding_catalog.facts,
        grounded_missing_information=grounding_catalog.missing_information,
        fact_catalog=fact_catalog,
        required_fact_ids=[item.fact_id for item in fact_catalog],
        required_missing_information_ids=[
            item.missing_id for item in grounding_catalog.missing_information
        ],
        percentage_rules=[
            PercentageRuleReference(
                rule_id="RULE-MINOR-INSTITUTION-DISPLAY-THRESHOLD",
                value=Decimal("5.00"),
                rule_class="skill_visualization_rule",
                description="一般机构股东低于该阈值时才可能成为展示聚合候选",
            ),
            PercentageRuleReference(
                rule_id="RULE-INDIVIDUAL-DISPLAY-THRESHOLD",
                value=Decimal("25.00"),
                rule_class="skill_visualization_rule",
                description="自然人股东严格超过该阈值时原则上单列",
            ),
        ],
        final_response_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "fact_ids",
                "missing_information_ids",
                "analysis_claims",
            ],
            "properties": {
                "fact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "完整复制required_fact_ids",
                },
                "missing_information_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "完整复制required_missing_information_ids",
                },
                "analysis_claims": {
                    "type": "array",
                    "description": "每条分析必须绑定支持事实或资料缺口，正文不得自行填写数字或来源",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "claim_id",
                            "claim_type",
                            "statement",
                            "supporting_fact_ids",
                            "related_missing_ids",
                        ],
                        "properties": {
                            "claim_id": {"type": "string"},
                            "claim_type": {
                                "enum": [item.value for item in AnalysisClaimType]
                            },
                            "statement": {"type": "string"},
                            "supporting_fact_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "related_missing_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    )


def build_ownership_snapshot_from_artifacts(
    artifacts: Mapping[str, Any],
) -> Dict[str, Any]:
    """构造供跨模块研究使用的轻量股权上下文。

    ``ownership.json`` 和 ``ownership_graph.json`` 继续完整保存 Fact、Evidence、
    Source 及图节点/边 ID；本函数只把模型撰写报告实际需要的业务语义暴露给
    V2.5 主 Agent，避免把内部技术编号和旧版 ``response_contract`` 带入正文。
    表决权关系也不会混入持股比例关系。
    """

    graph_path = artifacts.get("graph_path")
    validation_path = artifacts.get("validation_path")
    if not graph_path or not validation_path:
        raise AgentRuntimeError(
            "missing_ownership_artifact",
            "构造股权快照需要 graph_path 和 validation_path",
        )

    graph = _json_load(Path(graph_path))
    validation = _json_load(Path(validation_path))
    nodes = {
        str(item["node_id"]): item
        for item in graph.get("nodes", [])
        if item.get("node_id") and item.get("name")
    }

    def node_name(node_id: Any) -> str:
        node = nodes.get(str(node_id))
        if node is None:
            raise AgentRuntimeError(
                "invalid_ownership_artifact",
                "股权制品中的关系引用了不存在的主体",
            )
        return str(node["name"])

    ownership_relations: List[Dict[str, Any]] = []
    for edge in graph.get("edges", []):
        if edge.get("relation_type", "equity_ownership") != "equity_ownership":
            continue
        ownership_relations.append(
            {
                "holder": node_name(edge.get("source_node_id")),
                "held_entity": node_name(edge.get("target_node_id")),
                "percentage": edge.get("percentage"),
                "percentage_origin": edge.get("percentage_origin"),
                "synthetic": bool(edge.get("synthetic", False)),
                "capital_scope": edge.get("capital_scope"),
                "as_of_date": edge.get("as_of_date") or graph.get("as_of_date"),
                "verification_status": edge.get("status"),
            }
        )

    control_relationships: List[Dict[str, Any]] = []
    for relationship in graph.get("control_relationships", []):
        control_relationships.append(
            {
                "subjects": [
                    node_name(item)
                    for item in relationship.get("subject_node_ids", [])
                ],
                "controlled_entity": node_name(
                    relationship.get("controlled_node_id")
                ),
                "relation_type": relationship.get("relation_type"),
                "as_of_date": relationship.get("as_of_date")
                or graph.get("as_of_date"),
                "verification_status": relationship.get("status"),
                "note": relationship.get("note"),
            }
        )

    complete_statuses = {"complete", "complete_with_calculated_residual"}
    branches: List[Dict[str, Any]] = []
    for branch in validation.get("branches", []):
        status = branch.get("status")
        branches.append(
            {
                "held_entity": node_name(branch.get("target_node_id")),
                "capital_scope": branch.get("capital_scope"),
                "known_total": branch.get("known_total"),
                "calculated_residual": branch.get("calculated_residual"),
                "residual_label": branch.get("residual_label"),
                "tolerance": branch.get("tolerance", validation.get("tolerance")),
                "status": status,
                "is_complete": status in complete_statuses,
            }
        )

    missing_information = [
        {
            "topic": item.get("topic"),
            "description": item.get("description"),
            "reason": item.get("reason"),
        }
        for item in graph.get("missing_information", [])
    ]

    limitations: List[str] = []
    dataset_path = artifacts.get("dataset_path")
    if dataset_path:
        try:
            dataset = _json_load(Path(dataset_path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            dataset = {}
        if isinstance(dataset, Mapping):
            limitations.extend(str(item) for item in dataset.get("limitations", []))
    limitations.extend(str(item) for item in graph.get("warnings", []))
    # 同一限制可能同时来自原始数据集和展示图；保持原顺序去重。
    limitations = list(dict.fromkeys(limitations))

    identity = graph.get("identity", {})
    return {
        "subject": {
            "legal_name": identity.get("legal_name"),
            "short_name": identity.get("short_name"),
            "ticker": identity.get("ticker"),
        },
        "as_of_date": graph.get("as_of_date"),
        "formal_ready": bool(graph.get("formal_ready")),
        "ownership_relations": ownership_relations,
        "control_relationships": control_relationships,
        "percentage_validation": {
            "tolerance": validation.get("tolerance"),
            "all_complete": bool(branches)
            and all(item["is_complete"] for item in branches),
            "branches": branches,
        },
        "missing_information": missing_information,
        "limitations": limitations,
        "render_engine": artifacts.get("render_engine"),
        "graph_paths": {
            "svg": str(artifacts["svg_path"]) if artifacts.get("svg_path") else None,
            "png": str(artifacts["png_path"]) if artifacts.get("png_path") else None,
            "dot": str(artifacts["dot_path"]) if artifacts.get("dot_path") else None,
        },
    }


def _build_compact_tool_result(result: SkillExecutionResult) -> Dict[str, Any]:
    """把股权制品压缩为模型所需事实，避免发送完整PDF或大JSON。"""

    artifacts = result.artifacts
    graph = _json_load(Path(artifacts["graph_path"]))
    validation = _json_load(Path(artifacts["validation_path"]))
    nodes = {item["node_id"]: item for item in graph["nodes"]}
    evidence_pages: Dict[str, Set[int]] = {}
    evidence_sources: Dict[str, str] = {}
    for item in graph.get("evidence", []):
        if item.get("evidence_id") and item.get("source_id"):
            evidence_sources[str(item["evidence_id"])] = str(item["source_id"])
        if item.get("page_number") is not None:
            evidence_pages.setdefault(item["source_id"], set()).add(
                int(item["page_number"])
            )

    verified_relations = []
    for edge in graph["edges"]:
        if edge.get("status") != "verified":
            continue
        verified_relations.append(
            {
                "fact_id": edge.get("edge_id") or f"OE{len(verified_relations) + 1:03d}",
                "holder": nodes[edge["source_node_id"]]["name"],
                "held_entity": nodes[edge["target_node_id"]]["name"],
                "percentage": edge["percentage"],
                "percentage_origin": edge["percentage_origin"],
                "as_of_date": edge["as_of_date"],
                "capital_scope": edge["capital_scope"],
                "source_ids": edge["source_ids"],
                "evidence_ids": edge["evidence_ids"],
            }
        )

    verified_controls = []
    for relationship in graph.get("control_relationships", []):
        if relationship.get("status") != "verified":
            continue
        verified_controls.append(
            {
                "claim_id": relationship.get("relationship_id")
                or f"CR{len(verified_controls) + 1:03d}",
                "subjects": [nodes[item]["name"] for item in relationship["subject_node_ids"]],
                "controlled_entity": nodes[relationship["controlled_node_id"]]["name"],
                "relation_type": relationship["relation_type"],
                "as_of_date": relationship["as_of_date"],
                "source_ids": relationship["source_ids"],
                "evidence_ids": relationship["evidence_ids"],
                "note": relationship.get("note"),
            }
        )

    sources = [
        {
            "source_id": item["source_id"],
            "title": item["title"],
            "publisher": item["publisher"],
            "url": item["url"],
            "published_at": item.get("published_at"),
            "pages": sorted(evidence_pages.get(item["source_id"], set())),
        }
        for item in graph.get("sources", [])
    ]
    compact_validation = dict(validation)
    compact_validation["branches"] = []
    for index, branch in enumerate(validation.get("branches", []), start=1):
        compact_branch = dict(branch)
        compact_branch["validation_id"] = f"PV{index:03d}"
        compact_branch["target_name"] = nodes.get(
            branch["target_node_id"], {"name": branch["target_node_id"]}
        )["name"]
        compact_validation["branches"].append(compact_branch)

    compact_missing_information = []
    for index, item in enumerate(graph.get("missing_information", []), start=1):
        source_ids = sorted(
            {
                evidence_sources[evidence_id]
                for evidence_id in item.get("evidence_ids", [])
                if evidence_id in evidence_sources
            }
        )
        compact_missing_information.append(
            {
                "missing_id": item.get("missing_id") or f"M{index:03d}",
                "topic": item["topic"],
                "description": item["description"],
                # 原始reason继续保留在ownership.json；不把含5%等披露/绘图阈值的
                # 内部诊断文字发送给模型，避免其被误写成企业实际持股事实。
                "source_ids": source_ids,
                "evidence_ids": list(item.get("evidence_ids", [])),
            }
        )
    compact = {
        "selected_skill": result.selection.selected_skill,
        "loaded_skill": {
            "path": result.loaded_skill_path,
            "version": result.trace.skill_version,
            "sha256": result.trace.skill_sha256,
        },
        "python_tool": result.tool_name,
        "identity": {
            "legal_name": graph["identity"]["legal_name"],
            "short_name": graph["identity"]["short_name"],
            "ticker": graph["identity"]["ticker"],
        },
        "as_of_date": graph["as_of_date"],
        "formal_ready": bool(graph["formal_ready"]),
        "verified_relations": verified_relations,
        "verified_control_relationships": verified_controls,
        "percentage_validation": compact_validation,
        "missing_information": compact_missing_information,
        "sources": sources,
        "artifacts": {
            name: value
            for name, value in artifacts.items()
            if name.endswith("_path")
        },
        "render_engine": artifacts.get("render_engine"),
    }
    # 事实目录只作为构建响应契约的输入，不在 Tool 结果顶层重复发送。
    # response_contract 已包含模型需要的可引用事实与资料缺口。
    contract_input = dict(compact)
    contract_input["grounding_catalog"] = build_ownership_grounding_catalog(
        graph, validation
    ).model_dump(mode="json")
    compact["response_contract"] = build_ownership_response_contract(
        contract_input
    ).model_dump(mode="json")
    # 该边界显著小于完整图JSON/PDF文本，避免在多轮模型调用中重复传输原文。
    if len(json.dumps(compact, ensure_ascii=False)) > 40_000:
        raise AgentRuntimeError(
            "tool_result_too_large", "压缩后的股权Tool结果超过安全上限"
        )
    return compact


def _mock_final_selection(context: OwnershipAgentContext) -> str:
    """Mock模型回传Fact ID和绑定事实的分析，最终数值仍由Python渲染。"""

    if context.compact_result is None:
        raise AgentRuntimeError("missing_tool_result", "Mock模型未收到Tool结果")
    contract = context.compact_result["response_contract"]
    grounded_facts = list(contract["grounded_facts"])
    holding = next(
        (
            item
            for item in grounded_facts
            if item["metric"] == "equity_ownership_percentage"
            and item["provenance"] == "disclosed_fact"
            and item["verification_status"] == "verified"
        ),
        None,
    )
    control = next(
        (
            item
            for item in grounded_facts
            if item["metric"].startswith("control_relationship.")
            and item["verification_status"] == "verified"
        ),
        None,
    )
    analysis_claims = []
    if holding and control:
        analysis_claims.append(
            {
                "claim_id": "A001",
                "claim_type": "risk_signal",
                "statement": (
                    "股权与控制关系具有一定集中性，需持续关注关键控制主体变动"
                    "对公司治理稳定性的影响。"
                ),
                "supporting_fact_ids": [holding["fact_id"], control["fact_id"]],
                "related_missing_ids": [],
            }
        )
    required_missing = list(contract["required_missing_information_ids"])
    if required_missing:
        analysis_claims.append(
            {
                "claim_id": f"A{len(analysis_claims) + 1:03d}",
                "claim_type": "due_diligence_question",
                "statement": "建议在尽调阶段补充核验公开资料尚未完整披露的股东信息。",
                "supporting_fact_ids": [],
                "related_missing_ids": [required_missing[0]],
            }
        )
    return json.dumps(
        {
            "fact_ids": contract["required_fact_ids"],
            "missing_information_ids": required_missing,
            "analysis_claims": analysis_claims,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _strip_json_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


def parse_ownership_fact_selection(
    text: str, payload: Mapping[str, Any]
) -> OwnershipFactSelection:
    """校验模型选择的Fact ID、缺口ID及每条分析的证据绑定。"""

    contract = payload.get("response_contract")
    if not isinstance(contract, Mapping):
        raise AgentRuntimeError(
            "missing_response_contract",
            "Tool结果缺少最终回答事实白名单",
            rule_id="FACT-CONTRACT-001",
        )
    try:
        raw = json.loads(_strip_json_code_fence(text))
        selection = OwnershipFactSelection.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AgentRuntimeError(
            "invalid_fact_selection",
            "模型最终响应不符合Fact-Grounded JSON契约",
            rule_id="FACT-SELECTION-001",
            rejected_text=text.strip(),
        ) from exc

    allowed = {
        str(item["fact_id"]) for item in contract.get("fact_catalog", [])
    }
    required = {str(item) for item in contract.get("required_fact_ids", [])}
    selected = set(selection.fact_ids)
    unknown = selected - allowed
    if unknown:
        raise AgentRuntimeError(
            "unknown_fact_id",
            f"模型引用了Tool未返回的Fact ID：{sorted(unknown)}",
            rule_id="FACT-SELECTION-002",
            rejected_text=", ".join(sorted(unknown)),
        )
    missing = required - selected
    if missing:
        raise AgentRuntimeError(
            "missing_required_fact_id",
            f"模型遗漏必需Fact ID：{sorted(missing)}",
            rule_id="FACT-SELECTION-003",
            rejected_text=", ".join(sorted(missing)),
        )
    allowed_missing = {
        str(item["missing_id"])
        for item in contract.get("grounded_missing_information", [])
    }
    required_missing = {
        str(item)
        for item in contract.get("required_missing_information_ids", [])
    }
    selected_missing = set(selection.missing_information_ids)
    unknown_missing = selected_missing - allowed_missing
    if unknown_missing:
        raise AgentRuntimeError(
            "unknown_missing_information_id",
            f"模型引用了Tool未返回的资料缺口ID：{sorted(unknown_missing)}",
            rule_id="MISSING-SELECTION-001",
            rejected_text=", ".join(sorted(unknown_missing)),
        )
    omitted_missing = required_missing - selected_missing
    if omitted_missing:
        raise AgentRuntimeError(
            "missing_required_information_id",
            f"模型遗漏必需资料缺口ID：{sorted(omitted_missing)}",
            rule_id="MISSING-SELECTION-002",
            rejected_text=", ".join(sorted(omitted_missing)),
        )
    _validate_analysis_claims(selection, contract)
    return selection


def _validate_analysis_claims(
    selection: OwnershipFactSelection, contract: Mapping[str, Any]
) -> None:
    """验证LLM分析句的事实支持关系，不尝试用字符串白名单替代语义边界。"""

    facts = {
        str(item["fact_id"]): item for item in contract.get("grounded_facts", [])
    }
    missing_items = {
        str(item["missing_id"]): item
        for item in contract.get("grounded_missing_information", [])
    }
    selected_fact_ids = set(selection.fact_ids)
    selected_missing_ids = set(selection.missing_information_ids)
    rule_percentages = {
        _percentage_value(item["value"])
        for item in contract.get("percentage_rules", [])
    }
    for claim in selection.analysis_claims:
        support_ids = set(claim.supporting_fact_ids)
        related_missing_ids = set(claim.related_missing_ids)
        unknown_support = support_ids - set(facts)
        if unknown_support:
            raise AgentRuntimeError(
                "unknown_analysis_fact_id",
                f"分析{claim.claim_id}引用了未知Fact ID：{sorted(unknown_support)}",
                rule_id="ANALYSIS-FACT-001",
                rejected_text=claim.statement,
            )
        unverified_support = {
            fact_id
            for fact_id in support_ids
            if facts[fact_id].get("verification_status")
            not in {"verified", "calculated"}
        }
        if unverified_support:
            raise AgentRuntimeError(
                "unverified_analysis_fact",
                f"分析{claim.claim_id}引用了未经核实的候选事实：{sorted(unverified_support)}",
                rule_id="ANALYSIS-FACT-002",
                rejected_text=claim.statement,
            )
        if support_ids - selected_fact_ids:
            raise AgentRuntimeError(
                "analysis_fact_not_selected",
                f"分析{claim.claim_id}的支持事实未进入报告事实集合",
                rule_id="ANALYSIS-FACT-003",
                rejected_text=claim.statement,
            )
        unknown_missing = related_missing_ids - set(missing_items)
        if unknown_missing or related_missing_ids - selected_missing_ids:
            raise AgentRuntimeError(
                "invalid_analysis_missing_reference",
                f"分析{claim.claim_id}引用了无效或未选择的资料缺口",
                rule_id="ANALYSIS-MISSING-001",
                rejected_text=claim.statement,
            )

        if claim.claim_type in {
            AnalysisClaimType.SUPPORTED_INFERENCE,
            AnalysisClaimType.RISK_SIGNAL,
        } and not support_ids:
            raise AgentRuntimeError(
                "analysis_support_required",
                f"分析{claim.claim_id}缺少支持Fact ID",
                rule_id="ANALYSIS-SUPPORT-001",
                rejected_text=claim.statement,
            )
        if claim.claim_type is AnalysisClaimType.DUE_DILIGENCE_QUESTION and not (
            related_missing_ids
        ):
            raise AgentRuntimeError(
                "due_diligence_missing_reference_required",
                f"尽调问题{claim.claim_id}必须关联资料缺口",
                rule_id="ANALYSIS-MISSING-002",
                rejected_text=claim.statement,
            )
        if claim.claim_type is AnalysisClaimType.UNVERIFIED_HYPOTHESIS and not (
            support_ids or related_missing_ids
        ):
            raise AgentRuntimeError(
                "hypothesis_reference_required",
                f"待核实假设{claim.claim_id}必须关联事实或资料缺口",
                rule_id="ANALYSIS-HYPOTHESIS-001",
                rejected_text=claim.statement,
            )

        statement = claim.statement.strip()
        if re.search(r"\[(?:S\d{2,})\]", statement) or re.search(
            r"https?://", statement, flags=re.IGNORECASE
        ):
            raise AgentRuntimeError(
                "analysis_embedded_source",
                "分析文字不得自行填写来源编号或网页链接，来源由Python渲染",
                rule_id="ANALYSIS-SOURCE-001",
                rejected_text=statement,
            )
        percentage_matches = list(
            re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", statement)
        )
        if percentage_matches:
            values = {
                _percentage_value(item.group(1)) for item in percentage_matches
            }
            if values.intersection(rule_percentages):
                raise AgentRuntimeError(
                    "analysis_business_rule_as_fact",
                    "分析文字不得把Skill阈值写成企业事实",
                    rule_id="ANALYSIS-RULE-001",
                    rejected_text=statement,
                )
            raise AgentRuntimeError(
                "analysis_embedded_numeric_fact",
                "分析文字不得自行填写比例，具体数值由Python依据Fact ID渲染",
                rule_id="ANALYSIS-NUMERIC-001",
                rejected_text=statement,
            )
        if re.search(r"\d", statement):
            raise AgentRuntimeError(
                "analysis_embedded_numeric_fact",
                "分析文字不得自行填写数值或年份，具体数据由Python渲染",
                rule_id="ANALYSIS-NUMERIC-002",
                rejected_text=statement,
            )
        if any(
            keyword in statement
            for keyword in ("营业收入", "利润", "现金流", "偿债", "行业政策", "市场份额")
        ):
            raise AgentRuntimeError(
                "analysis_scope_mismatch",
                "ownership-structure分析不得引用尚未开放的财务或行业事实",
                rule_id="ANALYSIS-SCOPE-001",
                rejected_text=statement,
            )
        if any(keyword in statement for keyword in CONTROL_CLAIM_KEYWORDS.values()):
            has_control_support = any(
                str(facts[fact_id].get("metric", "")).startswith(
                    "control_relationship."
                )
                for fact_id in support_ids
            )
            if not has_control_support:
                raise AgentRuntimeError(
                    "analysis_control_support_required",
                    f"分析{claim.claim_id}的控制关系表述缺少控制关系Fact ID",
                    rule_id="ANALYSIS-CONTROL-001",
                    rejected_text=statement,
                )


def _source_markers(source_ids: Sequence[str]) -> str:
    return "".join(f"[{source_id}]" for source_id in source_ids)


def render_ownership_fact_selection(
    selection: OwnershipFactSelection, payload: Mapping[str, Any]
) -> str:
    """依据已校验Fact ID确定性渲染最终说明，不使用模型生成数字。"""

    selected = set(selection.fact_ids)
    selected_missing = set(selection.missing_information_ids)
    target = payload["identity"]["legal_name"]
    lines = [f"截至{payload['as_of_date']}，{target}的股权结构研究结果如下："]

    lines.append("\n主要持股关系：")
    for item in payload["verified_relations"]:
        if item["fact_id"] not in selected:
            continue
        percentage = _percentage_value(item["percentage"])
        source_markers = _source_markers(item["source_ids"])
        if item["percentage_origin"] == "calculated_residual":
            lines.append(
                f"- {item['holder']}为同一时点、同一股本口径下的计算差额"
                f"{percentage:.2f}%，不代表年报披露了具体股东身份{source_markers}"
            )
        else:
            lines.append(
                f"- {item['holder']}持有{item['held_entity']}"
                f"{percentage:.2f}%{source_markers}"
            )

    relation_labels = {
        "controlling_shareholder": "控股股东",
        "actual_controller": "实际控制人",
        "acting_in_concert": "一致行动人",
        "ultimate_controller": "最终控制主体",
    }
    lines.append("\n控制关系：")
    for item in payload["verified_control_relationships"]:
        if item["claim_id"] not in selected:
            continue
        label = relation_labels.get(item["relation_type"], item["relation_type"])
        lines.append(
            f"- {'、'.join(item['subjects'])}为{item['controlled_entity']}的{label}"
            f"{_source_markers(item['source_ids'])}"
        )

    lines.append("\n比例完整性：")
    for branch in payload["percentage_validation"]["branches"]:
        if branch["validation_id"] not in selected:
            continue
        source_markers = _source_markers(branch.get("source_ids", []))
        known_total = _percentage_value(branch["known_total"])
        if branch["status"] == "complete_with_calculated_residual":
            residual = _percentage_value(branch["calculated_residual"])
            lines.append(
                f"- {branch['target_name']}已核实直接持股合计{known_total:.2f}%，"
                f"基于同口径证据计算未单列股东差额{residual:.2f}%，"
                f"合计通过100%完整性校验{source_markers}"
            )
        elif branch["status"] == "complete":
            lines.append(
                f"- {branch['target_name']}直接股东持股合计{known_total:.2f}%，"
                f"通过100%完整性校验{source_markers}"
            )
        else:
            lines.append(
                f"- {branch['target_name']}比例信息不完整，不能视为完整股权结构"
                f"{source_markers}"
            )

    if selection.analysis_claims:
        fact_by_id = {
            str(item["fact_id"]): item
            for item in payload["response_contract"].get("grounded_facts", [])
        }
        missing_by_id = {
            str(item["missing_id"]): item
            for item in payload["response_contract"].get(
                "grounded_missing_information", []
            )
        }
        section_labels = {
            AnalysisClaimType.SUPPORTED_INFERENCE: "分析判断",
            AnalysisClaimType.RISK_SIGNAL: "风险关注",
            AnalysisClaimType.DUE_DILIGENCE_QUESTION: "尽调问题",
            AnalysisClaimType.UNVERIFIED_HYPOTHESIS: "待核实假设",
        }
        for claim_type, section_label in section_labels.items():
            claims = [
                item
                for item in selection.analysis_claims
                if item.claim_type is claim_type
            ]
            if not claims:
                continue
            lines.append(f"\n{section_label}：")
            for claim in claims:
                source_ids: List[str] = []
                for fact_id in claim.supporting_fact_ids:
                    fact = fact_by_id[fact_id]
                    if fact.get("source_id"):
                        source_ids.append(str(fact["source_id"]))
                    source_ids.extend(
                        str(item)
                        for item in fact.get("additional_source_ids", [])
                    )
                for missing_id in claim.related_missing_ids:
                    missing_item = missing_by_id[missing_id]
                    if missing_item.get("source_id"):
                        source_ids.append(str(missing_item["source_id"]))
                lines.append(
                    f"- {claim.statement}{_source_markers(list(dict.fromkeys(source_ids)))}"
                )

    if payload["missing_information"]:
        lines.append("\n资料限制与待核查事项：")
        for item in payload["missing_information"]:
            if item["missing_id"] in selected_missing:
                lines.append(
                    f"- {item['topic']}：{item['description']}"
                    f"{_source_markers(item.get('source_ids', []))}"
                )
    lines.append(f"\n股权结构图：{payload['artifacts']['svg_path']}")
    return "\n".join(lines)


def _bind_runtime_source(
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
) -> SourceRecord:
    """从只读来源目录绑定本次来源，不接受模型或调用方改写登记元数据。"""

    try:
        registered = SourceCatalog.from_default_catalog().get(source.source_id)
    except SourceNotFoundError as exc:
        raise AgentRuntimeError(
            "runtime_source_not_registered",
            f"运行上下文来源未登记：{source.source_id}",
        ) from exc

    immutable_fields = (
        "entity_id",
        "title",
        "source_type",
        "publisher",
        "url",
        "published_at",
        "report_period",
    )
    mismatched_fields = [
        field_name
        for field_name in immutable_fields
        if str(getattr(source, field_name, None))
        != str(getattr(registered, field_name, None))
    ]
    if mismatched_fields:
        raise AgentRuntimeError(
            "runtime_source_record_mismatch",
            "运行上下文来源与登记目录不一致："
            + "、".join(mismatched_fields),
        )
    if registered.entity_id not in (None, identity.entity_id):
        raise AgentRuntimeError(
            "runtime_source_entity_mismatch",
            f"来源{registered.source_id}不属于当前主体{identity.entity_id}",
        )
    if download.source_id != registered.source_id:
        raise AgentRuntimeError(
            "runtime_download_source_mismatch",
            f"下载产物来源{download.source_id}与运行来源{registered.source_id}不一致",
        )
    return registered


def create_ownership_function_tool():
    """创建唯一白名单 Function Tool。"""

    @function_tool(
        name_override=OWNERSHIP_FUNCTION_TOOL,
        description_override=(
            "加载 ownership-structure Skill，调用既有确定性股权分析、比例校验和Graphviz绘图。"
            "只接受已解析证券代码；官方来源由Python Runtime从已验证上下文绑定。"
            "不接受来源编号、文件路径或其他覆盖参数。"
        ),
        strict_mode=True,
        failure_error_function=None,
        timeout=300,
    )
    async def run_ownership_research(
        wrapper: RunContextWrapper[OwnershipAgentContext],
        ticker: str,
    ) -> str:
        """研究上市公司股权结构并生成带来源的结构化产物。"""

        context = wrapper.context
        request = OwnershipToolRequest(ticker=ticker)
        if request.ticker != context.identity.ticker:
            raise AgentRuntimeError(
                "tool_entity_mismatch",
                f"Tool参数证券代码与已解析主体不一致：{request.ticker}",
            )
        if context.tool_call_count >= context.max_tool_calls:
            raise AgentRuntimeError(
                "max_tool_calls_exceeded",
                f"Tool调用次数超过限制 {context.max_tool_calls}",
            )
        context.tool_call_count += 1
        context.trace.tool_call_count = context.tool_call_count
        context.trace.tool_call_arguments.append(
            {
                "tool_name": OWNERSHIP_FUNCTION_TOOL,
                "model_arguments": {"ticker": request.ticker},
                "runtime_bound": {"source_id": context.source.source_id},
            }
        )

        tool_arguments: Dict[str, Any] = {
            "download": context.download,
            "summary_path": context.summary_path,
            "pages_path": context.pages_path,
            "output_dir": context.ownership_dir,
        }
        tool_arguments.update(dict(context.tool_output_overrides))
        runtime = context.skill_runtime_factory(context.project_root)
        result = runtime.execute(
            task=context.task,
            identity=context.identity,
            source=context.source,
            tool_arguments=tool_arguments,
            trace_output=context.skill_trace_path,
        )
        compact = _build_compact_tool_result(result)
        context.skill_result = result
        context.compact_result = compact
        context.trace.selected_skill = result.selection.selected_skill
        context.trace.loaded_skill_path = result.loaded_skill_path
        context.trace.skill_trace_path = str(result.trace_path)
        context.trace.percentage_validation_passed = bool(compact["formal_ready"])
        context.trace.artifact_paths = _artifact_paths(result.artifacts)
        context.trace.deterministic_artifacts_preserved = bool(
            context.trace.artifact_paths
        )
        context.trace.returned_fact_ids = list(
            compact["response_contract"]["required_fact_ids"]
        )
        context.trace.returned_missing_information_ids = list(
            compact["response_contract"]["required_missing_information_ids"]
        )
        _append_event(
            context.trace,
            context.clock,
            AgentExecutionStage.SKILL_RUNTIME_COMPLETED,
            "Registry已确认Skill，Loader已加载SKILL.md，确定性Python Tool执行完成",
            {
                "selected_skill": result.selection.selected_skill,
                "loaded_skill_path": result.loaded_skill_path,
                "python_tool": result.tool_name,
                "percentage_validation_passed": bool(compact["formal_ready"]),
                "artifact_count": len(context.trace.artifact_paths),
                "response_contract_version": compact["response_contract"]["version"],
                "allowed_percentage_count": len(
                    compact["response_contract"]["allowed_percentages"]
                ),
                "returned_fact_count": len(context.trace.returned_fact_ids),
                "returned_missing_information_count": len(
                    context.trace.returned_missing_information_ids
                ),
            },
        )
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    # SDK 0.21 的动态参数模型默认会忽略多余字段；在调用包装器最外层检查
    # 原始 JSON，确保模型无法通过 source_id 或其他未声明字段尝试覆盖上下文。
    original_invoker = run_ownership_research.on_invoke_tool

    async def invoke_with_exact_arguments(tool_context, raw_input: str):
        try:
            raw_arguments = json.loads(raw_input)
        except json.JSONDecodeError:
            return await original_invoker(tool_context, raw_input)
        if isinstance(raw_arguments, dict):
            unexpected = sorted(set(raw_arguments) - {"ticker"})
            if unexpected:
                error_code = (
                    "tool_source_override_forbidden"
                    if "source_id" in unexpected
                    else "tool_unexpected_argument"
                )
                raise AgentRuntimeError(
                    error_code,
                    "模型不得提供Runtime绑定字段或未声明Tool参数："
                    + "、".join(unexpected),
                    rule_id="TOOL-INPUT-001",
                    rejected_text=", ".join(unexpected),
                )
        return await original_invoker(tool_context, raw_input)

    run_ownership_research.on_invoke_tool = invoke_with_exact_arguments
    return run_ownership_research


class _AgentTraceHooks(RunHooksBase[OwnershipAgentContext, Agent[OwnershipAgentContext]]):
    def __init__(
        self,
        clock: Callable[[], datetime],
        event_sink: Optional[Callable[[AgentExecutionEvent], None]] = None,
    ) -> None:
        self.clock = clock
        self.event_sink = event_sink

    def _record(
        self,
        trace: AgentExecutionTrace,
        stage: AgentExecutionStage,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = _append_event(trace, self.clock, stage, message, details)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                # UI进度属于旁路，不得影响Agent主流程。
                pass

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        trace = context.context.trace
        trace.llm_call_count += 1
        self._record(
            trace,
            AgentExecutionStage.LLM_CALL_STARTED,
            "开始一次模型调用",
            {"llm_call_number": trace.llm_call_count, "input_item_count": len(input_items)},
        )

    async def on_llm_end(self, context, agent, response) -> None:
        usage = response.usage
        trace = context.context.trace
        # 在每次响应结束时累计，确保后续证据校验失败时仍保留真实用量。
        trace.usage.requests += usage.requests
        trace.usage.input_tokens += usage.input_tokens
        trace.usage.output_tokens += usage.output_tokens
        trace.usage.total_tokens += usage.total_tokens
        self._record(
            trace,
            AgentExecutionStage.LLM_CALL_COMPLETED,
            "模型调用完成",
            {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
        )

    async def on_tool_start(self, context, agent, tool) -> None:
        trace = context.context.trace
        trace.selected_tool = tool.name
        self._record(
            trace,
            AgentExecutionStage.LLM_TOOL_SELECTED,
            "LLM决定调用白名单Function Tool",
            {"tool_name": tool.name},
        )


def _append_event(
    trace: AgentExecutionTrace,
    clock: Callable[[], datetime],
    stage: AgentExecutionStage,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> AgentExecutionEvent:
    event = AgentExecutionEvent(
            sequence=len(trace.events) + 1,
            occurred_at=clock(),
            stage=stage,
            message=message,
            details=details or {},
    )
    trace.events.append(event)
    return event


def _write_trace(path: Path, trace: AgentExecutionTrace) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _record_model_candidate(
    trace: AgentExecutionTrace, text: str, *, max_characters: int = 20_000
) -> None:
    """记录可复现、已脱敏的模型最终候选响应及完整内容摘要。"""

    trace.model_candidate_output_sha256 = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    trace.model_candidate_output_truncated = len(text) > max_characters
    trace.model_candidate_output = text[:max_characters]


def _copy_finish_reasons(trace: AgentExecutionTrace, model: Optional[Model]) -> None:
    """从支持审计的模型复制finish_reason；Mock模型则安全留空。"""

    if model is None:
        return
    raw_reasons = getattr(model, "finish_reasons", None)
    if isinstance(raw_reasons, list):
        trace.finish_reasons = [
            str(item) if item is not None else None for item in raw_reasons
        ]


def _usage_summary(usage: Usage) -> AgentTokenUsage:
    return AgentTokenUsage(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
    )


def _find_nested_runtime_error(exc: BaseException) -> Optional[AgentRuntimeError]:
    """SDK可能包装Tool异常；沿异常链找回本项目的机器可读错误码。"""

    current: Optional[BaseException] = exc
    visited: Set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, AgentRuntimeError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _percentage_facts_by_value(
    contract: Mapping[str, Any],
) -> Dict[Decimal, List[Mapping[str, Any]]]:
    facts: Dict[Decimal, List[Mapping[str, Any]]] = {}
    for item in contract.get("allowed_percentages", []):
        if not item.get("allowed_in_final", True):
            continue
        facts.setdefault(_percentage_value(item["value"]), []).append(item)
    return facts


def _percentage_rules_by_value(
    contract: Mapping[str, Any],
) -> Dict[Decimal, List[Mapping[str, Any]]]:
    rules: Dict[Decimal, List[Mapping[str, Any]]] = {}
    for item in contract.get("percentage_rules", []):
        rules.setdefault(_percentage_value(item["value"]), []).append(item)
    return rules


def _clause_containing(text: str, start: int, end: int) -> str:
    left = max(text.rfind(separator, 0, start) for separator in ("。", "；", ";", "\n"))
    right_candidates = [
        position
        for separator in ("。", "；", ";", "\n")
        if (position := text.find(separator, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right].strip()


def _percentage_fact_matches_clause(
    fact: Mapping[str, Any], clause: str
) -> bool:
    semantic_class = fact.get("semantic_class")
    stated_years = set(re.findall(r"(?<!\d)(20\d{2})(?:年|[-/])", clause))
    fact_as_of = str(fact.get("as_of_date") or "")
    if stated_years and fact_as_of[:4] not in stated_years:
        return False
    if semantic_class == "enterprise_fact":
        subject = str(fact.get("subject") or "")
        company = str(fact.get("company") or "")
        return bool(subject and company and subject in clause and company in clause)
    if semantic_class == "python_calculation":
        subject = str(fact.get("subject") or "")
        company = str(fact.get("company") or "")
        return (
            any(marker in clause for marker in ("计算", "差额", "合计", "校验"))
            and (not subject or subject in clause or company in clause)
        )
    if semantic_class == "validation_rule":
        return any(marker in clause for marker in ("完整性", "校验"))
    return False


def _validate_control_claims(text: str, contract: Mapping[str, Any]) -> None:
    """校验明确控制结论中的主体和同句来源。

    对“无法确认”等保守表述不作正向事实校验；一旦模型明确声称某类控制关系，
    该句必须包含对应白名单中的全部主体及至少一个匹配来源编号。
    """

    claims = list(contract.get("allowed_control_claims", []))
    clauses = [item.strip() for item in re.split(r"[。；;\n]+", text) if item.strip()]
    for relation_type, keyword in CONTROL_CLAIM_KEYWORDS.items():
        compatible = [
            item for item in claims if item.get("relation_type") == relation_type
        ]
        for clause in clauses:
            if keyword not in clause or any(
                marker in clause for marker in CONTROL_UNCERTAINTY_MARKERS
            ):
                continue
            matched = [
                item
                for item in compatible
                if all(subject in clause for subject in item.get("subjects", []))
            ]
            if not matched:
                raise AgentRuntimeError(
                    "ungrounded_control_claim",
                    f"模型回答中的{keyword}主体未匹配Tool白名单",
                    rule_id="CONTROL-FACT-001",
                    rejected_text=clause,
                )
            clause_sources = set(re.findall(r"\[(S\d{2,})\]", clause))
            if not any(
                clause_sources.intersection(item.get("source_ids", []))
                for item in matched
            ):
                raise AgentRuntimeError(
                    "missing_control_claim_citation",
                    f"模型回答中的{keyword}结论缺少匹配来源编号",
                    rule_id="CONTROL-SOURCE-001",
                    rejected_text=clause,
                )


def validate_grounded_output(text: str, payload: Mapping[str, Any]) -> None:
    """拒绝无来源或包含未由Tool返回比例的模型结论。"""

    contract = payload.get("response_contract")
    if not isinstance(contract, Mapping):
        raise AgentRuntimeError(
            "missing_response_contract", "Tool结果缺少最终回答事实白名单"
        )
    allowed_sources = set(contract.get("allowed_source_ids", []))
    cited_sources = set(re.findall(r"\[(S\d{2,})\]", text))
    if not cited_sources:
        raise AgentRuntimeError(
            "missing_source_citation",
            "模型回答没有来源编号",
            rule_id="SOURCE-001",
            rejected_text=text.strip(),
        )
    unknown_sources = cited_sources - allowed_sources
    if unknown_sources:
        raise AgentRuntimeError(
            "unknown_source_citation",
            f"模型回答引用了Tool未返回的来源：{sorted(unknown_sources)}",
            rule_id="SOURCE-002",
            rejected_text=text.strip(),
        )
    facts_by_value = _percentage_facts_by_value(contract)
    rules_by_value = _percentage_rules_by_value(contract)
    for match in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", text):
        raw = match.group(1)
        clause = _clause_containing(text, match.start(), match.end())
        try:
            value = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise AgentRuntimeError(
                "invalid_percentage",
                f"无法解析比例 {raw}%",
                rule_id="PERCENTAGE-001",
                rejected_text=clause,
            ) from exc
        matching_facts = facts_by_value.get(value, [])
        semantic_matches = [
            fact
            for fact in matching_facts
            if _percentage_fact_matches_clause(fact, clause)
        ]
        clause_sources = set(re.findall(r"\[(S\d{2,})\]", clause))
        if any(
            not fact.get("source_ids")
            or clause_sources.intersection(fact.get("source_ids", []))
            for fact in semantic_matches
        ):
            continue
        if semantic_matches:
            raise AgentRuntimeError(
                "percentage_fact_source_mismatch",
                f"比例 {raw}% 的同句来源与支持该事实的来源不匹配",
                rule_id="PERCENTAGE-SOURCE-001",
                rejected_text=clause,
            )
        matching_rules = rules_by_value.get(value, [])
        if matching_rules and any(
            marker in clause
            for marker in (
                "规则",
                "阈值",
                "合并",
                "展示",
                "披露范围",
                "以上股东",
                "低于",
                "超过",
                "恰好",
            )
        ):
            raise AgentRuntimeError(
                "business_rule_percentage_in_final_output",
                f"最终企业说明不应输出内部规则或披露阈值 {raw}%",
                rule_id="PERCENTAGE-RULE-001",
                rejected_text=clause,
            )
        if matching_facts:
            raise AgentRuntimeError(
                "misclassified_percentage",
                f"模型回答未按Tool事实类型表述比例 {raw}%",
                rule_id="PERCENTAGE-FACT-002",
                rejected_text=clause,
            )
        raise AgentRuntimeError(
            "ungrounded_percentage",
            f"模型回答包含Tool未返回的企业比例 {raw}%",
            rule_id="PERCENTAGE-FACT-001",
            rejected_text=clause,
        )
    _validate_control_claims(text, contract)


def _default_mock_model(context: OwnershipAgentContext) -> ScriptedModel:
    usage = Usage(requests=1, input_tokens=20, output_tokens=12, total_tokens=32)

    def final_step(_call) -> ModelStep:
        return ModelStep(
            output=[assistant_message(_mock_final_selection(context))],
            usage=Usage(
                requests=1,
                input_tokens=40,
                output_tokens=80,
                total_tokens=120,
            ),
        )

    return ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        OWNERSHIP_FUNCTION_TOOL,
                        {"ticker": context.identity.ticker},
                        call_id="mock-ownership-call-1",
                    )
                ],
                usage=usage,
            ),
            ModelStep.respond(final_step),
        ]
    )


class CreditResearchAgentRuntime:
    """单 Agent 运行入口；默认 ScriptedModel，Live 必须双重显式授权。"""

    def __init__(
        self,
        project_root: Path,
        config: DeepSeekLLMConfig,
        *,
        model_override: Optional[Model] = None,
        skill_runtime_factory: Callable[[Path], SkillRuntime] = SkillRuntime,
        clock: Optional[Callable[[], datetime]] = None,
        trace_id_factory: Optional[Callable[[], str]] = None,
        progress_sink: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config
        self.model_override = model_override
        self.skill_runtime_factory = skill_runtime_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.trace_id_factory = trace_id_factory or (
            lambda: "AR-" + uuid4().hex[:12].upper()
        )
        self.progress_sink = progress_sink

    async def run(
        self,
        *,
        task: str,
        identity: CompanyIdentity,
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
        ownership_dir: Path,
        trace_output: Path,
        skill_trace_output: Path,
        confirmed_live: bool = False,
        tool_output_overrides: Optional[Mapping[str, Any]] = None,
    ) -> AgentRunResult:
        if not any(keyword in task for keyword in OWNERSHIP_TASK_KEYWORDS):
            raise AgentRuntimeError(
                "unsupported_agent_task",
                "V1.3B 当前只开放 ownership-structure 股权研究任务",
                trace_path=Path(trace_output),
            )
        if self.config.mode is AgentMode.LIVE and not confirmed_live:
            raise AgentRuntimeError(
                "live_confirmation_required",
                "Live 模式必须显式确认后才能发送付费请求",
                trace_path=Path(trace_output),
            )

        trace = AgentExecutionTrace(
            trace_id=self.trace_id_factory(),
            task=task,
            config=self.config.sanitized_summary(),
            entity_id=identity.entity_id,
            source_id=source.source_id,
        )
        _append_event(
            trace,
            self.clock,
            AgentExecutionStage.RUN_STARTED,
            "主Agent运行开始",
            {"mode": self.config.mode.value, "provider": self.config.provider},
        )
        try:
            bound_source = _bind_runtime_source(identity, source, download)
        except AgentRuntimeError as exc:
            trace.status = AgentExecutionStatus.FAILED
            trace.error_code = exc.code
            trace.error_message = str(exc)
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.RUN_FAILED,
                "Runtime来源绑定失败",
                {"error_code": exc.code, "error_message": str(exc)},
            )
            _write_trace(Path(trace_output), trace)
            raise AgentRuntimeError(
                exc.code,
                str(exc),
                trace_path=Path(trace_output),
            ) from exc
        trace.source_id = bound_source.source_id
        trace.bound_source_id = bound_source.source_id
        _append_event(
            trace,
            self.clock,
            AgentExecutionStage.RUNTIME_SOURCE_BOUND,
            "Python Runtime已从SourceCatalog绑定官方来源",
            {
                "source_id": bound_source.source_id,
                "source_entity_id": bound_source.entity_id,
                "research_entity_id": identity.entity_id,
            },
        )
        context = OwnershipAgentContext(
            project_root=self.project_root,
            task=task,
            identity=identity,
            source=bound_source,
            download=download,
            summary_path=Path(summary_path),
            pages_path=Path(pages_path),
            ownership_dir=Path(ownership_dir),
            skill_trace_path=Path(skill_trace_output),
            max_tool_calls=self.config.max_tool_calls,
            trace=trace,
            clock=self.clock,
            tool_output_overrides=tool_output_overrides or {},
            skill_runtime_factory=self.skill_runtime_factory,
        )
        client: Optional[AsyncOpenAI] = None
        model: Optional[Model] = None
        try:
            if self.model_override is not None:
                model = self.model_override
            elif self.config.mode is AgentMode.MOCK:
                model = _default_mock_model(context)
            else:
                model, client = build_live_deepseek_model(self.config)

            agent = Agent[OwnershipAgentContext](
                name="企业授信研究Agent",
                instructions=MAIN_AGENT_INSTRUCTIONS,
                model=model,
                tools=[create_ownership_function_tool()],
                model_settings=build_model_settings(self.config),
            )
            result = await Runner.run(
                agent,
                task,
                context=context,
                max_turns=self.config.max_turns,
                hooks=_AgentTraceHooks(self.clock, self.progress_sink),
                run_config=RunConfig(
                    workflow_name="Corporate Credit Research V1.3B",
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                ),
            )
            if context.skill_result is None or context.compact_result is None:
                raise AgentRuntimeError(
                    "required_tool_not_called",
                    "模型未调用必需的股权研究Tool",
                )
            candidate_output = str(result.final_output)
            _record_model_candidate(
                trace,
                self._redact_secret(candidate_output),
            )
            selection = parse_ownership_fact_selection(
                candidate_output, context.compact_result
            )
            trace.selected_fact_ids = list(selection.fact_ids)
            trace.selected_missing_information_ids = list(
                selection.missing_information_ids
            )
            trace.selected_analysis_claim_ids = [
                item.claim_id for item in selection.analysis_claims
            ]
            final_output = render_ownership_fact_selection(
                selection, context.compact_result
            )
            validate_grounded_output(final_output, context.compact_result)
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.GROUNDED_OUTPUT_VALIDATED,
                "模型Fact ID已校验，最终说明由Python渲染并通过事实校验",
                {
                    "selected_fact_count": len(trace.selected_fact_ids),
                    "selected_missing_information_count": len(
                        trace.selected_missing_information_ids
                    ),
                    "selected_analysis_claim_count": len(
                        trace.selected_analysis_claim_ids
                    ),
                },
            )
            trace.usage = _usage_summary(result.context_wrapper.usage)
            _copy_finish_reasons(trace, model)
            trace.status = AgentExecutionStatus.SUCCEEDED
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.RUN_COMPLETED,
                "主Agent运行完成",
                {"total_tokens": trace.usage.total_tokens},
            )
            _write_trace(Path(trace_output), trace)
            return AgentRunResult(
                final_output=final_output,
                trace_path=Path(trace_output).resolve(),
                skill_trace_path=Path(skill_trace_output).resolve(),
                selected_skill=context.skill_result.selection.selected_skill,
                tool_name=context.skill_result.tool_name,
                artifacts=context.skill_result.artifacts,
                usage=trace.usage,
                config=self.config.sanitized_summary(),
            )
        except Exception as exc:
            nested_runtime_error = _find_nested_runtime_error(exc)
            if nested_runtime_error is not None:
                code = nested_runtime_error.code
                message = str(nested_runtime_error)
                trace.validator_rule_id = nested_runtime_error.rule_id
                if nested_runtime_error.rejected_text:
                    trace.rejected_sentence = self._redact_secret(
                        nested_runtime_error.rejected_text
                    )
            elif isinstance(exc, MaxTurnsExceeded):
                code = "max_turns_exceeded"
                message = f"Agent超过最大运行轮次 {self.config.max_turns}"
            elif isinstance(exc, SkillRuntimeError):
                code = f"skill_runtime_{exc.code}"
                message = str(exc)
            elif isinstance(exc, LLMConfigError):
                code = exc.code
                message = str(exc)
            else:
                code = "agent_run_failed"
                message = str(exc) or type(exc).__name__
            message = self._redact_secret(message)
            _copy_finish_reasons(trace, model)
            trace.status = AgentExecutionStatus.FAILED
            trace.error_code = code
            trace.error_message = message
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.RUN_FAILED,
                "主Agent运行失败",
                {"error_code": code, "error_message": message},
            )
            _write_trace(Path(trace_output), trace)
            raise AgentRuntimeError(
                code,
                message,
                trace_path=Path(trace_output),
                rule_id=trace.validator_rule_id,
                rejected_text=trace.rejected_sentence,
            ) from exc
        finally:
            if client is not None:
                await client.close()

    def run_sync(self, **kwargs: Any) -> AgentRunResult:
        return asyncio.run(self.run(**kwargs))

    def _redact_secret(self, message: str) -> str:
        if self.config.api_key is None:
            return message
        secret = self.config.api_key.get_secret_value()
        return message.replace(secret, "[REDACTED]") if secret else message
