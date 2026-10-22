"""V1.3 单 Agent Skill 运行时与确定性 Tool Calling。

运行时负责编排和留痕，不重新实现下载、PDF 解析或股权业务逻辑。LLM/Agents SDK
未来只需调用这一层暴露的受控入口，不应绕过白名单或直接改写研究结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from pydantic.v1 import BaseModel, Field

from credit_research_agent.models import CompanyIdentity, SourceRecord
from credit_research_agent.ownership import OwnershipArtifacts, build_ownership_artifacts
from credit_research_agent.skills import (
    LoadedSkill,
    SkillLoader,
    SkillRegistry,
    SkillRegistryError,
    SkillRuntimeStatus,
    SkillSelection,
    SkillSelectionError,
    SkillSelector,
)


class SkillExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SkillExecutionStage(str, Enum):
    ENTITY_RESOLVED = "entity_resolved"
    SOURCE_VALIDATED = "source_validated"
    SKILL_SELECTED = "skill_selected"
    SKILL_LOADED = "skill_loaded"
    TOOL_CALLED = "tool_called"
    STRUCTURED_RESULT_VALIDATED = "structured_result_validated"
    OUTPUTS_GENERATED = "outputs_generated"
    COMPLETED = "completed"
    FAILED = "failed"


class SkillExecutionEvent(BaseModel):
    sequence: int = Field(ge=1)
    occurred_at: datetime
    stage: SkillExecutionStage
    status: SkillExecutionStatus
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class SkillExecutionTrace(BaseModel):
    schema_version: str = "1.3.0"
    trace_id: str
    task: str
    status: SkillExecutionStatus = SkillExecutionStatus.RUNNING
    entity_id: Optional[str] = None
    source_id: Optional[str] = None
    selected_skill: Optional[str] = None
    skill_version: Optional[str] = None
    skill_sha256: Optional[str] = None
    tool_name: Optional[str] = None
    events: List[SkillExecutionEvent] = Field(default_factory=list)
    result_summary: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class SkillExecutionResult(BaseModel):
    trace_path: Path
    trace: SkillExecutionTrace
    selection: SkillSelection
    loaded_skill_path: str
    tool_name: str
    artifacts: Dict[str, Any]


class SkillRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        trace: Optional[SkillExecutionTrace] = None,
        trace_path: Optional[Path] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace = trace
        self.trace_path = trace_path


@dataclass(frozen=True)
class ToolRegistration:
    """Skill 可调用的确定性 Python Tool。"""

    name: str
    skill_name: str
    executor: Callable[..., BaseModel]


def _model_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"无法序列化 {type(value).__name__}")


class SkillRuntime:
    """按固定顺序选择、加载 Skill，并只调用白名单中的实际 Tool。"""

    def __init__(
        self,
        project_root: Path,
        *,
        tools: Optional[Mapping[str, ToolRegistration]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        trace_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.registry = SkillRegistry(self.project_root)
        self.selector = SkillSelector(self.registry)
        self.loader = SkillLoader(self.project_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace_id_factory = trace_id_factory or (
            lambda: "TR-" + uuid4().hex[:12].upper()
        )
        default_tools = {
            "ownership-structure": ToolRegistration(
                name="build_ownership_artifacts",
                skill_name="ownership-structure",
                executor=build_ownership_artifacts,
            )
        }
        self._tools = dict(tools) if tools is not None else default_tools

    def execute(
        self,
        *,
        task: str,
        identity: CompanyIdentity,
        source: SourceRecord,
        tool_arguments: Mapping[str, Any],
        trace_output: Path,
    ) -> SkillExecutionResult:
        trace = SkillExecutionTrace(trace_id=self._trace_id_factory(), task=task)
        trace_path = Path(trace_output)
        selection: Optional[SkillSelection] = None
        loaded: Optional[LoadedSkill] = None
        try:
            self._validate_identity(identity)
            trace.entity_id = identity.entity_id
            self._append(
                trace,
                SkillExecutionStage.ENTITY_RESOLVED,
                f"已解析上市主体 {identity.ticker}",
                {"ticker": identity.ticker, "legal_name": identity.legal_name},
            )

            self._validate_source(identity, source)
            trace.source_id = source.source_id
            self._append(
                trace,
                SkillExecutionStage.SOURCE_VALIDATED,
                f"已校验来源 {source.source_id}",
                {
                    "publisher": source.publisher,
                    "trusted_tier": source.trusted_tier,
                    "report_period": source.report_period,
                },
            )

            selection = self.selector.select(task)
            registration = self.registry.get(selection.selected_skill)
            trace.selected_skill = registration.name
            trace.skill_version = registration.version
            trace.skill_sha256 = registration.sha256
            self._append(
                trace,
                SkillExecutionStage.SKILL_SELECTED,
                f"已选择 Skill {registration.name}",
                {"matched_keywords": selection.matched_keywords},
            )

            loaded = self.loader.load(registration)
            self._append(
                trace,
                SkillExecutionStage.SKILL_LOADED,
                f"已只读加载 {registration.relative_path}",
                {
                    "version": registration.version,
                    "sha256": registration.sha256,
                    "character_count": loaded.character_count,
                },
            )
            if registration.runtime_status is not SkillRuntimeStatus.READY:
                raise SkillRuntimeError(
                    "skill_not_executable",
                    f"Skill {registration.name} 当前仅有规范，确定性 Tools 尚未实现",
                )

            tool = self._tools.get(registration.name)
            if tool is None or tool.name not in registration.tool_names:
                raise SkillRuntimeError(
                    "tool_not_allowlisted",
                    f"Skill {registration.name} 没有可调用的白名单 Tool",
                )
            if tool.skill_name != registration.name:
                raise SkillRuntimeError(
                    "tool_skill_mismatch", "Tool 与 Skill 注册关系不一致"
                )
            trace.tool_name = tool.name
            self._append(
                trace,
                SkillExecutionStage.TOOL_CALLED,
                f"调用确定性 Tool {tool.name}",
                {"argument_names": sorted(tool_arguments)},
            )
            result = tool.executor(
                identity=identity,
                source=source,
                **dict(tool_arguments),
            )
            if not isinstance(result, BaseModel):
                raise SkillRuntimeError(
                    "invalid_tool_result", "Tool 必须返回 Pydantic 结构化结果"
                )
            artifacts = _model_dict(result)
            artifact_paths = self._validate_artifacts(result)
            self._append(
                trace,
                SkillExecutionStage.STRUCTURED_RESULT_VALIDATED,
                "Tool 返回的结构化结果已通过运行时校验",
                {
                    "result_type": type(result).__name__,
                    "artifact_count": len(artifact_paths),
                },
            )
            self._append(
                trace,
                SkillExecutionStage.OUTPUTS_GENERATED,
                "引用、文字说明和结构化制品已生成",
                {"artifact_paths": artifact_paths},
            )
            trace.result_summary = {
                "result_type": type(result).__name__,
                "artifact_count": len(artifact_paths),
                "render_engine": artifacts.get("render_engine"),
            }
            trace.status = SkillExecutionStatus.SUCCEEDED
            self._append(
                trace,
                SkillExecutionStage.COMPLETED,
                "Skill 运行完成",
            )
            self._write_trace(trace_path, trace)
            return SkillExecutionResult(
                trace_path=trace_path,
                trace=trace,
                selection=selection,
                loaded_skill_path=registration.relative_path,
                tool_name=tool.name,
                artifacts=artifacts,
            )
        except Exception as exc:
            if isinstance(exc, SkillRuntimeError):
                code = exc.code
                message = str(exc)
            elif isinstance(exc, (SkillRegistryError, SkillSelectionError)):
                code = exc.code
                message = str(exc)
            else:
                code = getattr(exc, "code", "tool_execution_failed")
                message = str(exc)
            trace.status = SkillExecutionStatus.FAILED
            trace.error_code = code
            trace.error_message = message
            self._append(
                trace,
                SkillExecutionStage.FAILED,
                "Skill 运行失败",
                {"error_code": code, "error_message": message},
                status=SkillExecutionStatus.FAILED,
            )
            self._write_trace(trace_path, trace)
            raise SkillRuntimeError(
                code,
                message,
                trace=trace,
                trace_path=trace_path,
            ) from exc

    @staticmethod
    def _validate_identity(identity: CompanyIdentity) -> None:
        if not identity.entity_id or not identity.ticker or not identity.legal_name:
            raise SkillRuntimeError(
                "invalid_company_identity", "Skill 运行前必须先唯一解析企业主体"
            )

    @staticmethod
    def _validate_source(identity: CompanyIdentity, source: SourceRecord) -> None:
        if source.entity_id not in (None, identity.entity_id):
            raise SkillRuntimeError(
                "source_entity_mismatch",
                f"来源 {source.source_id} 不属于主体 {identity.ticker}",
            )
        if source.trusted_tier != 1:
            raise SkillRuntimeError(
                "source_not_official",
                f"V1.3 仅允许一级官方来源，当前来源为 tier {source.trusted_tier}",
            )

    @staticmethod
    def _validate_artifacts(result: BaseModel) -> List[str]:
        if isinstance(result, OwnershipArtifacts):
            required_fields = (
                "dataset_path",
                "graph_path",
                "svg_path",
                "png_path",
                "dot_path",
                "citations_path",
                "summary_path",
                "validation_path",
                "sources_path",
                "questions_path",
            )
        else:
            required_fields = tuple(
                name for name in result.__fields__ if name.endswith("_path")
            )
        if not required_fields:
            raise SkillRuntimeError(
                "missing_tool_artifacts", "Tool 结果没有可验证的制品路径"
            )
        artifact_paths: List[str] = []
        for field_name in required_fields:
            value = getattr(result, field_name, None)
            if not isinstance(value, Path) or not value.is_file():
                raise SkillRuntimeError(
                    "missing_tool_artifact",
                    f"Tool 声明的制品不存在：{field_name}={value}",
                )
            artifact_paths.append(str(value))
        return artifact_paths

    def _append(
        self,
        trace: SkillExecutionTrace,
        stage: SkillExecutionStage,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        *,
        status: SkillExecutionStatus = SkillExecutionStatus.SUCCEEDED,
    ) -> None:
        trace.events.append(
            SkillExecutionEvent(
                sequence=len(trace.events) + 1,
                occurred_at=self._clock(),
                stage=stage,
                status=status,
                message=message,
                details=details or {},
            )
        )

    @staticmethod
    def _write_trace(path: Path, trace: SkillExecutionTrace) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = json.dumps(
            _model_dict(trace),
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
