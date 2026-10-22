"""V1.3B 由 LLM 驱动、但受确定性边界约束的主 Agent。

模型只能调用一个高层股权研究 Tool。该 Tool 不接受文件路径，而是复用调用方已
准备并锁定的主体、官方来源、PDF解析产物和 V1.3A ``SkillRuntime``。这样既能证明
Skill 在应用运行时被加载，也不会向模型开放 Python、Shell 或任意文件访问能力。
"""

from __future__ import annotations

import asyncio
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from credit_research_agent.documents import DownloadResult
from credit_research_agent.llm_config import (
    AgentMode,
    DeepSeekLLMConfig,
    build_live_deepseek_model,
    build_model_settings,
)
from credit_research_agent.models import CompanyIdentity, SourceRecord
from credit_research_agent.skill_runtime import (
    SkillExecutionResult,
    SkillRuntime,
    SkillRuntimeError,
)


OWNERSHIP_FUNCTION_TOOL = "run_ownership_research"
OWNERSHIP_TASK_KEYWORDS = ("股权", "股东", "持股", "控制关系", "穿透图")

MAIN_AGENT_INSTRUCTIONS = """你是企业授信研究 Agent。当前版本只允许研究股权结构。

执行规则：
1. 用户要求股权、股东、控制关系或股权穿透图时，必须调用 run_ownership_research；不得只凭常识回答。
2. Tool 会在运行时通过白名单 Registry 选择 ownership-structure、只读加载 SKILL.md，并调用确定性 Python Tool。
3. 只能使用 Tool 返回的 verified_relations、verified_control_relationships、percentage_validation、sources 和 missing_information。
4. 不得编造股东、比例、实际控制人、来源编号或缺失资料；calculated_residual 必须明确为确定性差额而非具体股东披露。
5. 每项重要结论使用 Tool 返回的 [Sxx] 来源编号。证据不足时明确写“无法确认”并说明待核查事项。
6. 最终回答应简洁说明主要股东、控制关系、比例完整性、图形产物和限制。不要输出内部文件全文或虚构财务分析。
"""


class AgentExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentExecutionStage(str, Enum):
    RUN_STARTED = "run_started"
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    LLM_TOOL_SELECTED = "llm_tool_selected"
    SKILL_RUNTIME_COMPLETED = "skill_runtime_completed"
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
    schema_version: str = "1.3.1"
    trace_id: str
    task: str
    status: AgentExecutionStatus = AgentExecutionStatus.RUNNING
    config: Dict[str, Any]
    entity_id: str
    source_id: str
    llm_call_count: int = 0
    tool_call_count: int = 0
    selected_tool: Optional[str] = None
    selected_skill: Optional[str] = None
    loaded_skill_path: Optional[str] = None
    skill_trace_path: Optional[str] = None
    percentage_validation_passed: Optional[bool] = None
    artifact_paths: List[str] = Field(default_factory=list)
    usage: AgentTokenUsage = Field(default_factory=AgentTokenUsage)
    events: List[AgentExecutionEvent] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class OwnershipToolRequest(BaseModel):
    """模型可见的唯一 Tool 输入；不允许模型提供任何路径。"""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=4, max_length=20)
    source_id: str = Field(pattern=r"^S\d{2,}$")

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()


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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace_path = trace_path


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


def _build_compact_tool_result(result: SkillExecutionResult) -> Dict[str, Any]:
    """把股权制品压缩为模型所需事实，避免发送完整PDF或大JSON。"""

    artifacts = result.artifacts
    graph = _json_load(Path(artifacts["graph_path"]))
    validation = _json_load(Path(artifacts["validation_path"]))
    nodes = {item["node_id"]: item for item in graph["nodes"]}
    evidence_pages: Dict[str, Set[int]] = {}
    for item in graph.get("evidence", []):
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
        "percentage_validation": validation,
        "missing_information": graph.get("missing_information", []),
        "sources": sources,
        "artifacts": {
            name: value
            for name, value in artifacts.items()
            if name.endswith("_path")
        },
        "render_engine": artifacts.get("render_engine"),
    }
    # 该边界显著小于完整图JSON/PDF文本，避免在多轮模型调用中重复传输原文。
    if len(json.dumps(compact, ensure_ascii=False)) > 40_000:
        raise AgentRuntimeError(
            "tool_result_too_large", "压缩后的股权Tool结果超过安全上限"
        )
    return compact


def _mock_final_output(context: OwnershipAgentContext) -> str:
    """从 Tool 结果确定性生成 Mock 最终回复，不假装模型推理成功。"""

    if context.compact_result is None:
        raise AgentRuntimeError("missing_tool_result", "Mock模型未收到Tool结果")
    payload = context.compact_result
    source_id = payload["sources"][0]["source_id"]
    target = payload["identity"]["legal_name"]
    direct = [
        item
        for item in payload["verified_relations"]
        if item["held_entity"] == target
    ]
    lines = [
        f"截至{payload['as_of_date']}，{target}的主要直接持股关系如下："
    ]
    for item in direct:
        origin = (
            "（差额计算值，不代表具体股东）"
            if item["percentage_origin"] == "calculated_residual"
            else ""
        )
        lines.append(
            f"- {item['holder']}持股{Decimal(str(item['percentage'])):.2f}%{origin}[{source_id}]"
        )
    for item in payload["verified_control_relationships"]:
        if item["relation_type"] == "actual_controller":
            lines.append(
                f"- 年报披露实际控制人为{'、'.join(item['subjects'])}[{source_id}]"
            )
        elif item["relation_type"] == "ultimate_controller":
            lines.append(
                f"- 年报披露最终控制主体为{'、'.join(item['subjects'])}[{source_id}]"
            )
    statuses = [item["status"] for item in payload["percentage_validation"]["branches"]]
    lines.append(
        "- 各已展开分支均通过100%完整性校验。"
        if all(status in {"complete", "complete_with_calculated_residual"} for status in statuses)
        else "- 存在未通过100%完整性校验的分支，不能视为完整股权图。"
    )
    lines.append(
        f"- 股权图：{payload['artifacts']['svg_path']}；来源为[{source_id}]。"
    )
    if payload["missing_information"]:
        topics = "、".join(item["topic"] for item in payload["missing_information"])
        lines.append(f"- 仍待核查：{topics}；公开资料不足部分不作推断。")
    return "\n".join(lines)


def create_ownership_function_tool():
    """创建唯一白名单 Function Tool。"""

    @function_tool(
        name_override=OWNERSHIP_FUNCTION_TOOL,
        description_override=(
            "加载 ownership-structure Skill，调用既有确定性股权分析、比例校验和Graphviz绘图。"
            "只接受已解析证券代码和官方来源编号，不接受文件路径。"
        ),
        strict_mode=True,
        failure_error_function=None,
        timeout=300,
    )
    async def run_ownership_research(
        wrapper: RunContextWrapper[OwnershipAgentContext],
        ticker: str,
        source_id: str,
    ) -> str:
        """研究上市公司股权结构并生成带来源的结构化产物。"""

        context = wrapper.context
        request = OwnershipToolRequest(ticker=ticker, source_id=source_id)
        if request.ticker != context.identity.ticker:
            raise AgentRuntimeError(
                "tool_entity_mismatch",
                f"Tool参数证券代码与已解析主体不一致：{request.ticker}",
            )
        if request.source_id != context.source.source_id:
            raise AgentRuntimeError(
                "tool_source_mismatch",
                f"Tool参数来源与已校验来源不一致：{request.source_id}",
            )
        if context.tool_call_count >= context.max_tool_calls:
            raise AgentRuntimeError(
                "max_tool_calls_exceeded",
                f"Tool调用次数超过限制 {context.max_tool_calls}",
            )
        context.tool_call_count += 1
        context.trace.tool_call_count = context.tool_call_count

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
            },
        )
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    return run_ownership_research


class _AgentTraceHooks(RunHooksBase[OwnershipAgentContext, Agent[OwnershipAgentContext]]):
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        trace = context.context.trace
        trace.llm_call_count += 1
        _append_event(
            trace,
            self.clock,
            AgentExecutionStage.LLM_CALL_STARTED,
            "开始一次模型调用",
            {"llm_call_number": trace.llm_call_count, "input_item_count": len(input_items)},
        )

    async def on_llm_end(self, context, agent, response) -> None:
        usage = response.usage
        _append_event(
            context.context.trace,
            self.clock,
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
        _append_event(
            trace,
            self.clock,
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
) -> None:
    trace.events.append(
        AgentExecutionEvent(
            sequence=len(trace.events) + 1,
            occurred_at=clock(),
            stage=stage,
            message=message,
            details=details or {},
        )
    )


def _write_trace(path: Path, trace: AgentExecutionTrace) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def _allowed_percentages(payload: Mapping[str, Any]) -> Set[Decimal]:
    allowed: Set[Decimal] = set()
    for item in payload["verified_relations"]:
        allowed.add(Decimal(str(item["percentage"])).quantize(Decimal("0.01")))
    for item in payload["percentage_validation"]["branches"]:
        allowed.add(Decimal(str(item["known_total"])).quantize(Decimal("0.01")))
        if item.get("calculated_residual") is not None:
            allowed.add(
                Decimal(str(item["calculated_residual"])).quantize(Decimal("0.01"))
            )
    allowed.add(Decimal("100.00"))  # 完整性校验规则本身允许表述为100%。
    return allowed


def validate_grounded_output(text: str, payload: Mapping[str, Any]) -> None:
    """拒绝无来源或包含未由Tool返回比例的模型结论。"""

    allowed_sources = {item["source_id"] for item in payload["sources"]}
    cited_sources = set(re.findall(r"\[(S\d{2,})\]", text))
    if not cited_sources:
        raise AgentRuntimeError("missing_source_citation", "模型回答没有来源编号")
    unknown_sources = cited_sources - allowed_sources
    if unknown_sources:
        raise AgentRuntimeError(
            "unknown_source_citation",
            f"模型回答引用了Tool未返回的来源：{sorted(unknown_sources)}",
        )
    allowed = _allowed_percentages(payload)
    for raw in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", text):
        try:
            value = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise AgentRuntimeError("invalid_percentage", f"无法解析比例 {raw}%") from exc
        if value not in allowed:
            raise AgentRuntimeError(
                "ungrounded_percentage",
                f"模型回答包含Tool未返回的比例 {raw}%",
            )


def _default_mock_model(context: OwnershipAgentContext) -> ScriptedModel:
    usage = Usage(requests=1, input_tokens=20, output_tokens=12, total_tokens=32)

    def final_step(_call) -> ModelStep:
        return ModelStep(
            output=[assistant_message(_mock_final_output(context))],
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
                        {
                            "ticker": context.identity.ticker,
                            "source_id": context.source.source_id,
                        },
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
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config
        self.model_override = model_override
        self.skill_runtime_factory = skill_runtime_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.trace_id_factory = trace_id_factory or (
            lambda: "AR-" + uuid4().hex[:12].upper()
        )

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
        context = OwnershipAgentContext(
            project_root=self.project_root,
            task=task,
            identity=identity,
            source=source,
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
                hooks=_AgentTraceHooks(self.clock),
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
            final_output = str(result.final_output)
            validate_grounded_output(final_output, context.compact_result)
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.GROUNDED_OUTPUT_VALIDATED,
                "最终回答的来源编号与比例已和Tool结果交叉校验",
            )
            trace.usage = _usage_summary(result.context_wrapper.usage)
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
            elif isinstance(exc, MaxTurnsExceeded):
                code = "max_turns_exceeded"
                message = f"Agent超过最大运行轮次 {self.config.max_turns}"
            elif isinstance(exc, SkillRuntimeError):
                code = f"skill_runtime_{exc.code}"
                message = str(exc)
            else:
                code = "agent_run_failed"
                message = str(exc) or type(exc).__name__
            message = self._redact_secret(message)
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
