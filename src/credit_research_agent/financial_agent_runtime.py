"""V2.3 财务分析 Function Tool 与 LLM 报告运行入口。

Python 负责财务数据、计算、来源和逐段校验；DeepSeek 负责真正的专业分析与
自然语言写作。运行时只机械组装通过校验的模型原文，不会用 Python 固定模板
替换、改写或“润色”模型正文。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from agents import Agent, Model, RunConfig, RunContextWrapper, Runner, Usage, function_tool
from agents.exceptions import MaxTurnsExceeded
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from credit_research_agent.agent_runtime import (
    AgentExecutionStage,
    AgentExecutionStatus,
    AgentExecutionTrace,
    AgentRunResult,
    AgentRuntimeError,
    _AgentTraceHooks,
    _append_event,
    _artifact_paths,
    _bind_runtime_source,
    _copy_finish_reasons,
    _find_nested_runtime_error,
    _record_model_candidate,
    _usage_summary,
    _write_trace,
)
from credit_research_agent.documents import DownloadResult
from credit_research_agent.financial import FinancialSourceInput
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


FINANCIAL_FUNCTION_TOOL = "run_financial_research"
FINANCIAL_TASK_KEYWORDS = (
    "财务",
    "报表",
    "偿债",
    "现金流",
    "应收账款",
    "短期借款",
    "同比",
)

FINANCIAL_AGENT_INSTRUCTIONS = """你是企业授信研究 Agent，当前任务是财务分析。

执行规则：
1. 用户要求财务报表、同比、现金流、偿债或重点科目分析时，必须调用 run_financial_research。
2. Tool 只填写 ticker；来源编号、PDF路径、输出路径和可比年报均由 Python Runtime 绑定，禁止自行填写或覆盖。
3. Tool 会加载 financial-analysis/SKILL.md 并执行确定性 Python Tool。所有金额、变化额、比例和比率以 Tool 返回为准，不得自行计算或创造。
4. disclosed_reason 是公司披露原因；supported_inference 只是有事实支持的分析推断；unverified_hypothesis 只是待核实事项。不得混淆三者。
5. Tool 完成后，你必须亲自撰写完整的自然语言分析段落，并只输出符合 response_contract.final_response_schema 的 JSON。专业分析文字写在 analysis_markdown；结构化 ID 只承担引用和校验，不得替代正文。
6. 每段必须在 analysis_markdown 中使用 [FMxxx]、[Fxxx]、[FXxxx]、[Sxx] 或 [Mxxx] 引用所依赖的指标、事实、披露原因、来源或缺失项；JSON 的引用数组必须与正文引用一致。
7. 重点采用“上期金额→本期金额→变化额及比例→原因→合理性→授信关注与尽调问题”的分析思路，并进行收入、应收账款、存货、货币资金、短期借款/债务、应付账款和经营现金流的跨指标分析。
8. 只能把 Tool 标为 disclosed_reason 的内容写成公司/年报披露原因；其他判断必须明确写成“分析判断/推断/待核实”。不得将 Python 的风险信号或合理性提示冒充公司披露。
9. 短期借款等缺失数据只能引用对应 Mxxx 并写“未取得/无法确认/待补充”，不得写成零或推断不存在借款。
10. 不得创造新的金额、比例、期间、事实、原因或来源；数据不足时明确写未披露、无法确认或待进一步核实。
11. Python 只核对关键数字、引用和不支持的事实陈述，并原样组装通过校验的 analysis_markdown；它不会替你写报告，也不会用固定模板替换你的分析。
"""


class FinancialToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=4, max_length=20)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()


FINANCIAL_NARRATIVE_TOPICS = (
    "executive_summary",
    "operating_revenue",
    "accounts_receivable",
    "inventory",
    "monetary_funds",
    "short_term_borrowings_and_debt_structure",
    "accounts_payable",
    "operating_cash_flow",
    "cross_metric_analysis",
    "other_material_changes",
)


class FinancialNarrativeSection(BaseModel):
    """一段由 LLM 实际撰写、并带结构化证据引用的财务分析。"""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(pattern=r"^FS[0-9]{3}$")
    topic: str
    heading: str = Field(min_length=1, max_length=80)
    analysis_markdown: str = Field(min_length=1)
    metric_ids: List[str] = Field(default_factory=list)
    ratio_ids: List[str] = Field(default_factory=list)
    fact_ids: List[str] = Field(default_factory=list)
    explanation_ids: List[str] = Field(default_factory=list)
    inference_ids: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    missing_information_ids: List[str] = Field(default_factory=list)

    @field_validator("topic")
    @classmethod
    def topic_is_supported(cls, value: str) -> str:
        if value not in FINANCIAL_NARRATIVE_TOPICS:
            raise ValueError(f"不支持的财务分析主题：{value}")
        return value

    @field_validator(
        "metric_ids",
        "ratio_ids",
        "fact_ids",
        "explanation_ids",
        "inference_ids",
        "source_ids",
        "missing_information_ids",
    )
    @classmethod
    def ids_are_unique(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("结构化引用ID不得重复")
        return value


class FinancialDueDiligenceQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^FQ[0-9]{3}$")
    question: str = Field(min_length=1)
    supporting_metric_ids: List[str] = Field(default_factory=list)
    supporting_fact_ids: List[str] = Field(default_factory=list)
    related_missing_information_ids: List[str] = Field(default_factory=list)


class FinancialNarrativeDraft(BaseModel):
    """DeepSeek 返回的完整财务分析草稿；正文位于 sections。"""

    model_config = ConfigDict(extra="forbid")

    report_title: str = Field(min_length=1)
    sections: List[FinancialNarrativeSection] = Field(default_factory=list)
    due_diligence_questions: List[FinancialDueDiligenceQuestion] = Field(
        default_factory=list
    )
    structural_section_errors: List[Dict[str, Any]] = Field(
        default_factory=list, exclude=True
    )
    structural_question_errors: List[Dict[str, Any]] = Field(
        default_factory=list, exclude=True
    )


class FinancialSectionValidation(BaseModel):
    section_id: str
    topic: str
    accepted: bool
    rule_ids: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)


class FinancialNarrativeValidation(BaseModel):
    status: str
    generation_mode: str = "unknown"
    accepted_section_ids: List[str] = Field(default_factory=list)
    rejected_sections: List[FinancialSectionValidation] = Field(default_factory=list)
    missing_topics: List[str] = Field(default_factory=list)
    accepted_question_ids: List[str] = Field(default_factory=list)
    rejected_questions: List[Dict[str, Any]] = Field(default_factory=list)
    llm_authored_report: bool = True
    python_rewrote_llm_text: bool = False


@dataclass
class FinancialAgentContext:
    project_root: Path
    task: str
    identity: CompanyIdentity
    source: SourceRecord
    download: DownloadResult
    summary_path: Path
    pages_path: Path
    comparison_inputs: Sequence[FinancialSourceInput]
    financial_dir: Path
    skill_trace_path: Path
    max_tool_calls: int
    trace: AgentExecutionTrace
    clock: Callable[[], datetime]
    tool_output_overrides: Mapping[str, Any] = field(default_factory=dict)
    skill_runtime_factory: Callable[[Path], SkillRuntime] = SkillRuntime
    tool_call_count: int = 0
    skill_result: Optional[SkillExecutionResult] = None
    compact_result: Optional[Dict[str, Any]] = None


def _json_load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


FOCUS_FINANCIAL_ITEMS = (
    "operating_revenue",
    "accounts_receivable",
    "inventory",
    "monetary_funds",
    "short_term_borrowings",
    "accounts_payable",
    "operating_cash_flow",
)

TOPIC_ITEM_MAP = {
    "operating_revenue": "operating_revenue",
    "accounts_receivable": "accounts_receivable",
    "inventory": "inventory",
    "monetary_funds": "monetary_funds",
    "accounts_payable": "accounts_payable",
    "operating_cash_flow": "operating_cash_flow",
}


def _amount_display(value: Any) -> Dict[str, str]:
    amount = Decimal(str(value))
    return {
        "yuan": f"{amount:,.2f}",
        "ten_thousand_yuan": f"{amount / Decimal('10000'):.2f}",
        "hundred_million_yuan": f"{amount / Decimal('100000000'):.2f}",
    }


def _metric_with_display(metric: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(metric)
    result["display"] = {
        "prior_value": _amount_display(metric["prior_value"]),
        "current_value": _amount_display(metric["current_value"]),
        "absolute_change": _amount_display(metric["absolute_change"]),
        "yoy_percent": (
            None
            if metric.get("yoy_percent") is None
            else f"{Decimal(str(metric['yoy_percent'])):.2f}"
        ),
    }
    return result


def _build_financial_analysis_context(result: SkillExecutionResult) -> Dict[str, Any]:
    """构造发送给模型的紧凑事实包，而不是发送完整 PDF 或全量 JSON。"""

    artifacts = result.artifacts
    facts_payload = _json_load(artifacts["facts_path"])
    metrics_payload = _json_load(artifacts["metrics_path"])
    explanations_payload = _json_load(artifacts["explanations_path"])
    assessments = _json_load(artifacts["reasonableness_path"])
    risk_flags = _json_load(artifacts["risk_flags_path"])
    sources_payload = _json_load(artifacts["sources_path"])

    all_metrics = metrics_payload.get("metrics", [])
    selected_metrics = [
        item
        for item in all_metrics
        if item.get("current_period") == "FY2025"
        and (
            item.get("item") in FOCUS_FINANCIAL_ITEMS
            or bool(item.get("requires_deep_analysis"))
        )
    ]
    selected_metrics.sort(key=lambda item: item["metric_id"])
    metric_ids = {item["metric_id"] for item in selected_metrics}
    ratios = [
        item
        for item in metrics_payload.get("ratios", [])
        if item.get("period") in {"FY2024", "FY2025"}
    ]

    explanations = [
        item
        for item in explanations_payload.get("explanations", [])
        if item.get("metric_id") in metric_ids
    ]
    inference_ids = {
        item.get("inference_id") for item in explanations if item.get("inference_id")
    }
    inferences = [
        item
        for item in explanations_payload.get("inferences", [])
        if item.get("inference_id") in inference_ids
    ]
    hypotheses = [
        item
        for item in explanations_payload.get("hypotheses", [])
        if item.get("metric_id") in metric_ids
    ]

    referenced_fact_ids = {
        fact_id
        for metric in selected_metrics
        for fact_id in metric.get("supporting_fact_ids", [])
    }
    for ratio in ratios:
        referenced_fact_ids.update(ratio.get("supporting_fact_ids", []))
    for explanation in explanations:
        referenced_fact_ids.update(explanation.get("disclosed_fact_ids", []))
        referenced_fact_ids.update(explanation.get("supporting_fact_ids", []))
    for inference in inferences:
        referenced_fact_ids.update(inference.get("supporting_fact_ids", []))

    raw_facts = [
        item
        for item in facts_payload.get("raw_facts", [])
        if item.get("fact_id") in referenced_fact_ids
    ]
    evidence_ids = {
        evidence_id
        for fact in raw_facts
        for evidence_id in fact.get("evidence_ids", [])
    }
    for explanation in explanations:
        evidence_ids.update(explanation.get("evidence_ids", []))
    for inference in inferences:
        evidence_ids.update(inference.get("evidence_ids", []))
    evidence = [
        item
        for item in facts_payload.get("evidence", sources_payload.get("evidence", []))
        if item.get("evidence_id") in evidence_ids
    ]

    missing_information = facts_payload.get("missing_information", [])
    short_term_missing_ids = [
        item["missing_id"]
        for item in missing_information
        if "短期借款" in item.get("topic", "")
        or "短期借款" in item.get("description", "")
    ]
    metric_by_item = {item["item"]: item["metric_id"] for item in selected_metrics}
    topic_requirements: Dict[str, Dict[str, List[str]]] = {
        topic: {"metric_ids": [metric_by_item[item]], "missing_information_ids": []}
        for topic, item in TOPIC_ITEM_MAP.items()
        if item in metric_by_item
    }
    topic_requirements["short_term_borrowings_and_debt_structure"] = {
        "metric_ids": (
            [metric_by_item["short_term_borrowings"]]
            if "short_term_borrowings" in metric_by_item
            else []
        ),
        "missing_information_ids": short_term_missing_ids,
    }
    cross_ids = [
        metric_by_item[item]
        for item in ("operating_revenue", "accounts_receivable", "operating_cash_flow")
        if item in metric_by_item
    ]
    topic_requirements["cross_metric_analysis"] = {
        "metric_ids": cross_ids,
        "missing_information_ids": [],
    }
    topic_requirements["executive_summary"] = {
        "metric_ids": [],
        "missing_information_ids": [],
    }

    allowed_ids = {
        "metric_ids": sorted(metric_ids),
        "ratio_ids": sorted(item["ratio_id"] for item in ratios),
        "fact_ids": sorted(item["fact_id"] for item in raw_facts),
        "explanation_ids": sorted(item["explanation_id"] for item in explanations),
        "inference_ids": sorted(item["inference_id"] for item in inferences),
        "source_ids": sorted(
            item["source_id"] for item in sources_payload.get("sources", [])
        ),
        "missing_information_ids": sorted(
            item["missing_id"] for item in missing_information
        ),
    }
    return {
        "identity": facts_payload["identity"],
        "focus_items": list(FOCUS_FINANCIAL_ITEMS),
        "metrics": [_metric_with_display(item) for item in selected_metrics],
        "ratios": ratios,
        "facts": raw_facts,
        "evidence": evidence,
        "explanations": explanations,
        "inferences": inferences,
        "hypotheses": hypotheses,
        "python_analysis_hints": {
            "reasonableness_assessments": assessments,
            "risk_signals": risk_flags,
            "notice": "仅作分析线索，不是公司披露，也不是正式报告正文。",
        },
        "missing_information": missing_information,
        "sources": sources_payload.get("sources", []),
        "artifacts": {
            # 模型只需要逻辑文件名；绝对路径保留在本地 SkillExecutionResult。
            key: Path(str(value)).name
            for key, value in artifacts.items()
            if key.endswith("_path")
        },
        "response_contract": {
            "version": "2.3.1",
            "required_topics": list(topic_requirements),
            "topic_requirements": topic_requirements,
            "allowed_ids": allowed_ids,
            "final_response_schema": {
                "report_title": "string",
                "sections": [
                    {
                        "section_id": "FS001",
                        "topic": "one required topic",
                        "heading": "string",
                        "analysis_markdown": "DeepSeek撰写的完整分析段落，内含[ID]引用",
                        "metric_ids": ["FMxxx"],
                        "ratio_ids": ["FRxxx"],
                        "fact_ids": ["Fxxx"],
                        "explanation_ids": ["FXxxx"],
                        "inference_ids": ["Ixxx"],
                        "source_ids": ["Sxx"],
                        "missing_information_ids": ["Mxxx"],
                    }
                ],
                "due_diligence_questions": [
                    {
                        "question_id": "FQ001",
                        "question": "DeepSeek撰写的具体尽调问题",
                        "supporting_metric_ids": ["FMxxx"],
                        "supporting_fact_ids": ["Fxxx"],
                        "related_missing_information_ids": ["Mxxx"],
                    }
                ],
            },
            "python_will_preserve_accepted_analysis_markdown_verbatim": True,
            "python_will_not_generate_fallback_analysis": True,
            "prohibit_llm_arithmetic": True,
            "prohibit_unsupported_financial_facts": True,
        },
    }


# 保留旧私有名，避免现有调用方在迁移期中断。
_build_financial_compact_result = _build_financial_analysis_context


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def parse_financial_narrative_draft(text: str) -> FinancialNarrativeDraft:
    stripped = _strip_json_fence(text)
    try:
        raw = json.loads(stripped)
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是JSON对象")
        unexpected = sorted(
            set(raw) - {"report_title", "sections", "due_diligence_questions"}
        )
        if unexpected:
            raise ValueError(f"顶层包含未声明字段：{unexpected}")
        report_title = str(raw.get("report_title", "")).strip()
        if not report_title:
            raise ValueError("report_title不能为空")
        raw_sections = raw.get("sections", [])
        raw_questions = raw.get("due_diligence_questions", [])
        if not isinstance(raw_sections, list) or not isinstance(raw_questions, list):
            raise ValueError("sections和due_diligence_questions必须是数组")
        sections: List[FinancialNarrativeSection] = []
        section_errors: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_sections, start=1):
            try:
                sections.append(FinancialNarrativeSection.model_validate(item))
            except (ValidationError, ValueError) as exc:
                raw_item = item if isinstance(item, dict) else {}
                section_errors.append(
                    {
                        "section_id": raw_item.get("section_id", f"INVALID-{index}"),
                        "topic": raw_item.get("topic", "unknown"),
                        "reason": str(exc),
                    }
                )
        questions: List[FinancialDueDiligenceQuestion] = []
        question_errors: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_questions, start=1):
            try:
                questions.append(FinancialDueDiligenceQuestion.model_validate(item))
            except (ValidationError, ValueError) as exc:
                raw_item = item if isinstance(item, dict) else {}
                question_errors.append(
                    {
                        "question_id": raw_item.get("question_id", f"INVALID-{index}"),
                        "reasons": [str(exc)],
                    }
                )
        return FinancialNarrativeDraft(
            report_title=report_title,
            sections=sections,
            due_diligence_questions=questions,
            structural_section_errors=section_errors,
            structural_question_errors=question_errors,
        )
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AgentRuntimeError(
            "invalid_financial_narrative_json",
            "模型最终输出不是有效的财务分析草稿JSON",
            rule_id="FIN-NARRATIVE-001",
            rejected_text=stripped[:1000],
        ) from exc


def _decimal_text(value: Any) -> Decimal:
    return Decimal(str(value).replace(",", "").strip())


def _numeric_allowlist(
    section: FinancialNarrativeSection, payload: Mapping[str, Any]
) -> tuple[List[Decimal], List[Decimal]]:
    metric_by_id = {item["metric_id"]: item for item in payload["metrics"]}
    fact_by_id = {item["fact_id"]: item for item in payload["facts"]}
    ratio_by_id = {item["ratio_id"]: item for item in payload["ratios"]}
    explanation_by_id = {
        item["explanation_id"]: item for item in payload["explanations"]
    }
    inference_by_id = {item["inference_id"]: item for item in payload["inferences"]}
    allowed_amounts: List[Decimal] = []
    allowed_percentages: List[Decimal] = []
    for metric_id in section.metric_ids:
        metric = metric_by_id.get(metric_id)
        if metric is None:
            continue
        allowed_amounts.extend(
            _decimal_text(metric[name])
            for name in ("prior_value", "current_value", "absolute_change")
        )
        if metric.get("yoy_percent") is not None:
            allowed_percentages.append(_decimal_text(metric["yoy_percent"]))
    for fact_id in section.fact_ids:
        fact = fact_by_id.get(fact_id)
        if fact is None:
            continue
        if fact.get("value") not in (None, ""):
            try:
                allowed_amounts.append(_decimal_text(fact["value"]))
            except InvalidOperation:
                pass
    for ratio_id in section.ratio_ids:
        ratio = ratio_by_id.get(ratio_id)
        if ratio is not None:
            allowed_percentages.append(_decimal_text(ratio["value_percent"]))
    support_texts = [
        explanation_by_id[item]["statement"]
        for item in section.explanation_ids
        if item in explanation_by_id
    ] + [
        inference_by_id[item]["statement"]
        for item in section.inference_ids
        if item in inference_by_id
    ]
    for support_text in support_texts:
        for raw in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%", support_text):
            allowed_percentages.append(_decimal_text(raw))
    return allowed_amounts, allowed_percentages


def _matches_amount(value: Decimal, unit: str, allowed: Sequence[Decimal]) -> bool:
    multiplier = {
        "元": Decimal("1"),
        "万元": Decimal("10000"),
        "亿元": Decimal("100000000"),
    }[unit]
    yuan_value = value * multiplier
    # 两位展示值允许半个最小展示单位的舍入误差。
    tolerance = multiplier * Decimal("0.0051")
    return any(abs(yuan_value - candidate) <= tolerance for candidate in allowed)


def _signed_value_from_context(text: str, start: int, value: Decimal) -> Decimal:
    """把“下降10%/减少2亿元”的无符号写法还原为负变化值。

    其他场景严格保留模型写出的正负号，避免把真实增长误写为负增长后仍因
    绝对值相等而通过。这里仅处理紧邻数字的明确下降语义，不推断更复杂含义。
    """

    if value < 0:
        return value
    prefix = text[max(0, start - 12) : start]
    if re.search(r"(?:下降|减少|降低|下滑|收窄)(?:了|约|为)?\s*$", prefix):
        return -value
    return value


def _validate_section_numbers(
    section: FinancialNarrativeSection, payload: Mapping[str, Any]
) -> List[str]:
    reasons: List[str] = []
    allowed_amounts, allowed_percentages = _numeric_allowlist(section, payload)
    for match in re.finditer(
        r"(?<![A-Za-z0-9])([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元)",
        section.analysis_markdown,
    ):
        raw, unit = match.groups()
        try:
            value = _signed_value_from_context(
                section.analysis_markdown, match.start(1), _decimal_text(raw)
            )
        except InvalidOperation:
            reasons.append(f"无法解析金额 {raw}{unit}")
            continue
        if not _matches_amount(value, unit, allowed_amounts):
            reasons.append(f"金额 {raw}{unit} 未被本段引用事实支持")
    for match in re.finditer(
        r"(?<![A-Za-z0-9])([+-]?\d+(?:\.\d+)?)\s*%",
        section.analysis_markdown,
    ):
        raw = match.group(1)
        try:
            value = _signed_value_from_context(
                section.analysis_markdown, match.start(1), _decimal_text(raw)
            )
        except InvalidOperation:
            reasons.append(f"无法解析比例 {raw}%")
            continue
        if not any(
            abs(value - candidate) <= Decimal("0.01")
            for candidate in allowed_percentages
        ):
            reasons.append(f"比例 {raw}% 未被本段引用指标、比率或解释支持")

    metric_by_id = {item["metric_id"]: item for item in payload["metrics"]}
    fact_by_id = {item["fact_id"]: item for item in payload["facts"]}
    period_amounts: Dict[str, List[Decimal]] = {}
    for metric_id in section.metric_ids:
        metric = metric_by_id.get(metric_id)
        if metric is None:
            continue
        period_amounts.setdefault(metric["prior_period"], []).append(
            _decimal_text(metric["prior_value"])
        )
        period_amounts.setdefault(metric["current_period"], []).append(
            _decimal_text(metric["current_value"])
        )
    for fact_id in section.fact_ids:
        fact = fact_by_id.get(fact_id)
        if fact is None:
            continue
        if fact.get("period") and fact.get("value") not in (None, ""):
            try:
                period_amounts.setdefault(fact["period"], []).append(
                    _decimal_text(fact["value"])
                )
            except InvalidOperation:
                pass
    # 只检查“某年度为/达到某金额”这种明确绑定，避免把跨期变化句误判为错期。
    period_amount_pattern = re.compile(
        r"(?:FY)?(20\d{2})(?:年|年度|年末)?(?:[^，,。；;\n]{0,16})?"
        r"(?:为|达到|余额为|金额为)\s*"
        r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元)"
    )
    for year, raw, unit in period_amount_pattern.findall(section.analysis_markdown):
        period = f"FY{year}"
        if period not in period_amounts:
            reasons.append(f"{period} 金额 {raw}{unit} 缺少本段事实支持")
            continue
        if not _matches_amount(_decimal_text(raw), unit, period_amounts[period]):
            reasons.append(f"{period} 金额 {raw}{unit} 与引用事实期间不匹配")
    return reasons


def _validate_metric_semantics(
    section: FinancialNarrativeSection, payload: Mapping[str, Any]
) -> List[str]:
    """在单一科目分句内核对金额、同比和变化方向，避免科目间串值。"""

    reasons: List[str] = []
    metric_by_id = {item["metric_id"]: item for item in payload["metrics"]}
    referenced_metrics = [
        metric_by_id[item]
        for item in section.metric_ids
        if item in metric_by_id
    ]
    clauses = re.split(r"[，,。；;\n]", section.analysis_markdown)
    for clause in clauses:
        mentions: List[tuple[int, int, Mapping[str, Any]]] = []
        for metric in referenced_metrics:
            for match in re.finditer(re.escape(metric["item_label"]), clause):
                mentions.append((match.start(), match.end(), metric))
        mentions.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        if not mentions:
            continue
        # 同一分句出现多个科目时，以“当前科目标签到下一科目标签”的局部文本
        # 绑定邻近数字。无法清晰绑定的“分别为……”写法宁可拒绝，也不允许串值。
        for index, (_, label_end, metric) in enumerate(mentions):
            segment_end = mentions[index + 1][0] if index + 1 < len(mentions) else len(clause)
            segment = clause[label_end:segment_end]
            metric_amounts = [
                _decimal_text(metric[name])
                for name in ("prior_value", "current_value", "absolute_change")
            ]
            for match in re.finditer(
                r"(?<![A-Za-z0-9])([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元)",
                segment,
            ):
                raw, unit = match.groups()
                value = _signed_value_from_context(
                    segment, match.start(1), _decimal_text(raw)
                )
                if not _matches_amount(value, unit, metric_amounts):
                    reasons.append(
                        f"{metric['item_label']}分句中的金额 {raw}{unit} 与 {metric['metric_id']} 不匹配"
                    )
            percentages = list(
                re.finditer(
                    r"(?<![A-Za-z0-9])([+-]?\d+(?:\.\d+)?)\s*%", segment
                )
            )
            if percentages and metric.get("yoy_percent") is not None:
                expected = _decimal_text(metric["yoy_percent"])
                for match in percentages:
                    raw = match.group(1)
                    value = _signed_value_from_context(
                        segment, match.start(1), _decimal_text(raw)
                    )
                    if abs(value - expected) > Decimal("0.01"):
                        reasons.append(
                            f"{metric['item_label']}分句中的比例 {raw}% 与 {metric['metric_id']} 不匹配"
                        )
            yoy = metric.get("yoy_percent")
            if yoy is not None:
                direction = _decimal_text(yoy)
                if direction < 0 and any(
                    marker in segment for marker in ("增长", "增加", "上升")
                ):
                    reasons.append(f"{metric['item_label']}变化方向与 {metric['metric_id']} 不匹配")
                if direction > 0 and any(
                    marker in segment for marker in ("下降", "减少", "下滑")
                ):
                    reasons.append(f"{metric['item_label']}变化方向与 {metric['metric_id']} 不匹配")
    return reasons


def validate_financial_narrative(
    draft: FinancialNarrativeDraft, payload: Mapping[str, Any]
) -> tuple[List[FinancialNarrativeSection], List[FinancialDueDiligenceQuestion], FinancialNarrativeValidation]:
    contract = payload["response_contract"]
    allowed = {name: set(values) for name, values in contract["allowed_ids"].items()}
    metric_by_id = {item["metric_id"]: item for item in payload["metrics"]}
    ratio_by_id = {item["ratio_id"]: item for item in payload["ratios"]}
    fact_by_id = {item["fact_id"]: item for item in payload["facts"]}
    evidence_source = {
        item["evidence_id"]: item["source_id"] for item in payload.get("evidence", [])
    }
    explanation_by_id = {
        item["explanation_id"]: item for item in payload["explanations"]
    }
    inference_by_id = {item["inference_id"]: item for item in payload["inferences"]}
    accepted: List[FinancialNarrativeSection] = []
    rejected: List[FinancialSectionValidation] = [
        FinancialSectionValidation(
            section_id=str(item["section_id"]),
            topic=str(item["topic"]),
            accepted=False,
            rule_ids=["FIN-NARRATIVE-002"],
            reasons=[f"段落结构不符合契约：{item['reason']}"],
        )
        for item in draft.structural_section_errors
    ]
    seen_section_ids: set[str] = set()
    accepted_topics: set[str] = set()

    id_fields = (
        "metric_ids",
        "ratio_ids",
        "fact_ids",
        "explanation_ids",
        "inference_ids",
        "source_ids",
        "missing_information_ids",
    )
    for section in draft.sections:
        reasons: List[str] = []
        rules: List[str] = []
        if section.section_id in seen_section_ids:
            reasons.append(f"section_id 重复：{section.section_id}")
            rules.append("FIN-NARRATIVE-002")
        seen_section_ids.add(section.section_id)
        for field_name in id_fields:
            unknown = sorted(set(getattr(section, field_name)) - allowed[field_name])
            if unknown:
                reasons.append(f"{field_name} 含未登记引用：{unknown}")
                rules.append("FIN-NARRATIVE-003")

        declared_ids = {
            item
            for field_name in id_fields
            for item in getattr(section, field_name)
        }
        inline_ids = set(
            re.findall(r"\[((?:FM|FR|FX|FQ|FS|F|I|S|M)\d+)\]", section.analysis_markdown)
        )
        missing_inline = sorted(declared_ids - inline_ids)
        undeclared_inline = sorted(inline_ids - declared_ids)
        if missing_inline:
            reasons.append(f"正文缺少已声明引用标记：{missing_inline}")
            rules.append("FIN-NARRATIVE-004")
        if undeclared_inline:
            reasons.append(f"正文含未在结构化字段声明的引用：{undeclared_inline}")
            rules.append("FIN-NARRATIVE-005")

        requirements = contract["topic_requirements"].get(section.topic, {})
        required_metrics = set(requirements.get("metric_ids", []))
        required_missing = set(requirements.get("missing_information_ids", []))
        if not required_metrics.issubset(section.metric_ids):
            reasons.append(
                f"主题 {section.topic} 缺少必需指标 {sorted(required_metrics-set(section.metric_ids))}"
            )
            rules.append("FIN-NARRATIVE-006")
        if not required_missing.issubset(section.missing_information_ids):
            reasons.append(
                f"主题 {section.topic} 缺少数据缺口 {sorted(required_missing-set(section.missing_information_ids))}"
            )
            rules.append("FIN-NARRATIVE-007")
        if required_missing:
            uncertainty_markers = ("未取得", "无法确认", "未披露", "待补充", "尚未")
            if not any(
                marker in section.analysis_markdown
                for marker in uncertainty_markers
            ):
                reasons.append(
                    f"主题 {section.topic} 存在数据缺口，却未明确标记无法确认或待补充"
                )
                rules.append("FIN-NARRATIVE-018")
            unsupported_zero_claims = (
                "无短期借款",
                "短期借款为零",
                "不存在短期借款",
                "没有短期借款",
            )
            if any(
                marker in section.analysis_markdown
                for marker in unsupported_zero_claims
            ):
                reasons.append("缺失数据不得被表述为零或不存在短期借款")
                rules.append("FIN-NARRATIVE-019")

        for metric_id in section.metric_ids:
            if metric_id not in metric_by_id:
                continue
            metric = metric_by_id[metric_id]
            if not set(metric.get("supporting_fact_ids", [])) & set(section.fact_ids):
                reasons.append(f"指标 {metric_id} 未引用其支持 Fact ID")
                rules.append("FIN-NARRATIVE-008")
            if not set(metric.get("source_ids", [])) & set(section.source_ids):
                reasons.append(f"指标 {metric_id} 未引用其来源")
                rules.append("FIN-NARRATIVE-009")
        for ratio_id in section.ratio_ids:
            if ratio_id not in ratio_by_id:
                continue
            required_ratio_facts = set(
                ratio_by_id[ratio_id].get("supporting_fact_ids", [])
            )
            if not required_ratio_facts.issubset(section.fact_ids):
                reasons.append(
                    f"计算指标 {ratio_id} 缺少 derived_from Fact ID "
                    f"{sorted(required_ratio_facts-set(section.fact_ids))}"
                )
                rules.append("FIN-NARRATIVE-020")
        for fact_id in section.fact_ids:
            if fact_id not in fact_by_id:
                continue
            linked_sources = {
                evidence_source[evidence_id]
                for evidence_id in fact_by_id[fact_id].get("evidence_ids", [])
                if evidence_id in evidence_source
            }
            if linked_sources and not linked_sources & set(section.source_ids):
                reasons.append(f"Fact {fact_id} 与本段来源不匹配")
                rules.append("FIN-NARRATIVE-010")
        for explanation_id in section.explanation_ids:
            if explanation_id not in explanation_by_id:
                continue
            explanation = explanation_by_id[explanation_id]
            if explanation.get("metric_id") not in section.metric_ids:
                reasons.append(
                    f"解释 {explanation_id} 对应指标 {explanation.get('metric_id')} "
                    "未被本段引用"
                )
                rules.append("FIN-NARRATIVE-022")
            required_explanation_facts = set(
                explanation.get("disclosed_fact_ids", [])
            ) | set(explanation.get("supporting_fact_ids", []))
            required_explanation_sources = set(explanation.get("source_ids", []))
            if not required_explanation_facts.issubset(section.fact_ids):
                reasons.append(
                    f"解释 {explanation_id} 缺少支持 Fact ID "
                    f"{sorted(required_explanation_facts-set(section.fact_ids))}"
                )
                rules.append("FIN-NARRATIVE-015")
            if not required_explanation_sources.issubset(section.source_ids):
                reasons.append(
                    f"解释 {explanation_id} 缺少来源 "
                    f"{sorted(required_explanation_sources-set(section.source_ids))}"
                )
                rules.append("FIN-NARRATIVE-016")
        for inference_id in section.inference_ids:
            if inference_id not in inference_by_id:
                continue
            required_inference_facts = set(
                inference_by_id[inference_id].get("supporting_fact_ids", [])
            )
            if not required_inference_facts.issubset(section.fact_ids):
                reasons.append(
                    f"推断 {inference_id} 缺少支持 Fact ID "
                    f"{sorted(required_inference_facts-set(section.fact_ids))}"
                )
                rules.append("FIN-NARRATIVE-017")

        causal_markers = ("主要因", "原因是", "受益于", "源于")
        claims_disclosed_reason = (
            "披露原因" in section.analysis_markdown
            or "明确披露原" in section.analysis_markdown
            or (
                any(
                    marker in section.analysis_markdown
                    for marker in ("公司披露", "年报披露")
                )
                and any(
                    marker in section.analysis_markdown for marker in causal_markers
                )
            )
        )
        if claims_disclosed_reason:
            disclosed = [
                explanation_by_id[item]
                for item in section.explanation_ids
                if item in explanation_by_id
                and explanation_by_id[item].get("kind") == "disclosed_reason"
            ]
            if not disclosed:
                reasons.append("正文声称公司/年报披露原因，但未引用 disclosed_reason")
                rules.append("FIN-NARRATIVE-011")

        if (
            any(marker in section.analysis_markdown for marker in causal_markers)
            and not section.explanation_ids
            and not section.inference_ids
        ):
            reasons.append("正文提出具体因果解释，但未引用披露原因或支持性推断")
            rules.append("FIN-NARRATIVE-014")

        numeric_reasons = _validate_section_numbers(section, payload)
        if numeric_reasons:
            reasons.extend(numeric_reasons)
            rules.extend(["FIN-NARRATIVE-012"] * len(numeric_reasons))
        semantic_reasons = _validate_metric_semantics(section, payload)
        if semantic_reasons:
            reasons.extend(semantic_reasons)
            rules.extend(["FIN-NARRATIVE-021"] * len(semantic_reasons))

        if reasons:
            rejected.append(
                FinancialSectionValidation(
                    section_id=section.section_id,
                    topic=section.topic,
                    accepted=False,
                    rule_ids=list(dict.fromkeys(rules)),
                    reasons=reasons,
                )
            )
        else:
            accepted.append(section)
            accepted_topics.add(section.topic)

    accepted_questions: List[FinancialDueDiligenceQuestion] = []
    rejected_questions: List[Dict[str, Any]] = list(
        draft.structural_question_errors
    )
    seen_question_ids: set[str] = set()
    for question in draft.due_diligence_questions:
        reasons: List[str] = []
        if question.question_id in seen_question_ids:
            reasons.append("question_id 重复")
        seen_question_ids.add(question.question_id)
        unknown_metrics = set(question.supporting_metric_ids) - allowed["metric_ids"]
        unknown_facts = set(question.supporting_fact_ids) - allowed["fact_ids"]
        unknown_missing = (
            set(question.related_missing_information_ids)
            - allowed["missing_information_ids"]
        )
        if unknown_metrics or unknown_facts or unknown_missing:
            reasons.append("尽调问题存在未登记引用")
        if not (
            question.supporting_metric_ids
            or question.supporting_fact_ids
            or question.related_missing_information_ids
        ):
            reasons.append("尽调问题没有关联任何已验证指标、事实或缺失项")
        if reasons:
            rejected_questions.append(
                {"question_id": question.question_id, "reasons": reasons}
            )
        else:
            accepted_questions.append(question)

    missing_topics = [
        topic
        for topic in contract["required_topics"]
        if topic not in accepted_topics
    ]
    if (
        accepted
        and not missing_topics
        and not rejected
        and accepted_questions
        and not rejected_questions
    ):
        status = "complete"
    elif accepted:
        status = "partial"
    else:
        status = "failed"
    validation = FinancialNarrativeValidation(
        status=status,
        accepted_section_ids=[item.section_id for item in accepted],
        rejected_sections=rejected,
        missing_topics=missing_topics,
        accepted_question_ids=[item.question_id for item in accepted_questions],
        rejected_questions=rejected_questions,
    )
    return accepted, accepted_questions, validation


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _source_appendix(payload: Mapping[str, Any]) -> List[str]:
    lines = ["## 数据来源附录", ""]
    for source in payload.get("sources", []):
        source_id = source.get("source_id", "")
        title = source.get("title") or source.get("publisher") or "已登记来源"
        url = source.get("url")
        if url:
            lines.append(f"- [{source_id}] {title}：[原文]({url})")
        else:
            lines.append(f"- [{source_id}] {title}")
    return lines


def render_llm_financial_report(
    draft: FinancialNarrativeDraft,
    accepted_sections: Sequence[FinancialNarrativeSection],
    validation: FinancialNarrativeValidation,
    payload: Mapping[str, Any],
    *,
    model_mode: str = "live",
) -> str:
    """机械组装已通过校验的 DeepSeek 原文，不改写分析段落。"""

    lines = [f"# {draft.report_title}", ""]
    if model_mode == AgentMode.MOCK.value:
        lines.extend(
            [
                f"> 离线 Mock 财务分析状态：{validation.status.upper()}。",
                "> 本文来自 ScriptedModel 的离线模拟响应，仅用于验证 Agent Loop、引用校验和原文保留；不是 DeepSeek 正式研究成果。",
                "> Python 未改写模拟正文，也不会将其冒充正式报告。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"> LLM 财务分析状态：{validation.status.upper()}。",
                "> 专业分析正文由 DeepSeek 撰写；Python 仅校验关键数字、引用和不支持的事实陈述。",
                "",
            ]
        )
    topic_order = {topic: index for index, topic in enumerate(FINANCIAL_NARRATIVE_TOPICS)}
    for section in sorted(accepted_sections, key=lambda item: topic_order[item.topic]):
        lines.extend([f"## {section.heading}", "", section.analysis_markdown.strip(), ""])
    if validation.missing_topics or validation.rejected_sections:
        lines.extend(["## 未完成章节", ""])
        for topic in validation.missing_topics:
            lines.append(f"- {topic}：尚未取得通过校验的 DeepSeek 分析正文。")
        for item in validation.rejected_sections:
            lines.append(
                f"- {item.topic}（{item.section_id}）：校验未通过；"
                + "；".join(item.reasons)
            )
        lines.append("")
    data_appendix = payload["artifacts"].get("data_appendix_path")
    if data_appendix:
        lines.extend(
            [
                "## 结构化数据",
                "",
                f"- [Python 财务数据附录]({Path(str(data_appendix)).name})",
                "",
            ]
        )
    lines.extend(_source_appendix(payload))
    return "\n".join(lines).rstrip() + "\n"


def render_llm_financial_questions(
    questions: Sequence[FinancialDueDiligenceQuestion],
    validation: FinancialNarrativeValidation,
    *,
    model_mode: str = "live",
) -> str:
    author = "离线 ScriptedModel" if model_mode == AgentMode.MOCK.value else "DeepSeek"
    lines = [f"# 财务尽调问题清单（{author}）", ""]
    if questions:
        # 问题文本同样原样保留，Python 不把分析提示改写成固定问题。
        lines.extend(f"- {item.question}" for item in questions)
    else:
        lines.append(f"- 尚未取得通过校验的{author}尽调问题。")
    if validation.rejected_questions:
        lines.extend(["", "## 未完成项", ""])
        for item in validation.rejected_questions:
            lines.append(
                f"- {item['question_id']}：" + "；".join(item["reasons"])
            )
    return "\n".join(lines).rstrip() + "\n"


def create_financial_function_tool():
    @function_tool(
        name_override=FINANCIAL_FUNCTION_TOOL,
        description_override=(
            "加载 financial-analysis Skill，调用确定性财务事实、同比计算、"
            "原因证据整理和数据附录生成，并返回供模型分析的事实包。只接受已解析证券代码；"
            "来源、文件与可比年报由Python Runtime绑定。"
        ),
        strict_mode=True,
        failure_error_function=None,
        timeout=300,
    )
    async def run_financial_research(
        wrapper: RunContextWrapper[FinancialAgentContext], ticker: str
    ) -> str:
        context = wrapper.context
        request = FinancialToolRequest(ticker=ticker)
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
                "tool_name": FINANCIAL_FUNCTION_TOOL,
                "model_arguments": {"ticker": request.ticker},
                "runtime_bound": {
                    "source_id": context.source.source_id,
                    "comparison_source_ids": [
                        item.source.source_id for item in context.comparison_inputs
                    ],
                },
            }
        )
        tool_arguments: Dict[str, Any] = {
            "download": context.download,
            "summary_path": context.summary_path,
            "pages_path": context.pages_path,
            "comparison_inputs": context.comparison_inputs,
            "output_dir": context.financial_dir,
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
        compact = _build_financial_analysis_context(result)
        context.skill_result = result
        context.compact_result = compact
        context.trace.selected_skill = result.selection.selected_skill
        context.trace.loaded_skill_path = result.loaded_skill_path
        context.trace.skill_trace_path = str(result.trace_path)
        context.trace.artifact_paths = _artifact_paths(result.artifacts)
        context.trace.deterministic_artifacts_preserved = bool(
            context.trace.artifact_paths
        )
        contract = compact["response_contract"]
        allowed_ids = contract["allowed_ids"]
        context.trace.returned_fact_ids = list(allowed_ids["fact_ids"])
        context.trace.returned_missing_information_ids = list(
            allowed_ids["missing_information_ids"]
        )
        _append_event(
            context.trace,
            context.clock,
            AgentExecutionStage.SKILL_RUNTIME_COMPLETED,
            "Registry已确认financial-analysis，Loader已加载SKILL.md，财务Python Tool执行完成",
            {
                "selected_skill": result.selection.selected_skill,
                "loaded_skill_path": result.loaded_skill_path,
                "python_tool": result.tool_name,
                "analysis_metric_count": len(allowed_ids["metric_ids"]),
                "fact_count": len(allowed_ids["fact_ids"]),
                "artifact_count": len(context.trace.artifact_paths),
            },
        )
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    original_invoker = run_financial_research.on_invoke_tool

    async def invoke_with_exact_arguments(tool_context, raw_input: str):
        try:
            raw_arguments = json.loads(raw_input)
        except json.JSONDecodeError:
            return await original_invoker(tool_context, raw_input)
        if isinstance(raw_arguments, dict):
            unexpected = sorted(set(raw_arguments) - {"ticker"})
            if unexpected:
                raise AgentRuntimeError(
                    "tool_runtime_override_forbidden",
                    "模型不得提供Runtime绑定字段或未声明Tool参数："
                    + "、".join(unexpected),
                    rule_id="FIN-TOOL-INPUT-001",
                    rejected_text=", ".join(unexpected),
                )
        return await original_invoker(tool_context, raw_input)

    run_financial_research.on_invoke_tool = invoke_with_exact_arguments
    return run_financial_research


def _mock_narrative(context: FinancialAgentContext) -> str:
    """构造离线 ScriptedModel 响应；只用于验证 Agent Loop，不冒充真实分析。"""

    assert context.compact_result is not None
    payload = context.compact_result
    contract = payload["response_contract"]
    metric_by_id = {item["metric_id"]: item for item in payload["metrics"]}
    explanation_by_metric: Dict[str, List[Mapping[str, Any]]] = {}
    for item in payload["explanations"]:
        explanation_by_metric.setdefault(item["metric_id"], []).append(item)
    sections: List[Dict[str, Any]] = []
    next_index = 1
    for topic in contract["required_topics"]:
        requirements = contract["topic_requirements"][topic]
        metric_ids = list(requirements.get("metric_ids", []))
        missing_ids = list(requirements.get("missing_information_ids", []))
        fact_ids: List[str] = []
        source_ids: List[str] = []
        explanation_ids: List[str] = []
        sentence_parts: List[str] = []
        if topic == "executive_summary":
            sentence_parts.append(
                "【离线Mock哨兵】本段模拟 DeepSeek 的财务分析正文，仅用于验证模型原文不会被 Python 模板替换。"
            )
        elif topic == "short_term_borrowings_and_debt_structure":
            markers = " ".join(f"[{item}]" for item in missing_ids)
            sentence_parts.append(
                f"FY2024及FY2025短期借款可比数据尚未取得，债务结构无法确认，需补充借款及授信明细。{markers}"
            )
        elif topic == "cross_metric_analysis":
            labels: List[str] = []
            for metric_id in metric_ids:
                metric = metric_by_id[metric_id]
                linked_facts = metric.get("supporting_fact_ids", [])
                linked_sources = metric.get("source_ids", [])
                if linked_facts:
                    fact_ids.append(linked_facts[0])
                if linked_sources:
                    source_ids.append(linked_sources[0])
                labels.append(metric["item_label"])
            markers = " ".join(
                f"[{item}]"
                for item in [*metric_ids, *fact_ids, *dict.fromkeys(source_ids)]
            )
            sentence_parts.append(
                f"{ '、'.join(labels) }需要联动观察；当前组合显示回款质量有所改善，但持续性仍需核对客户结构与期后回款。{markers}"
            )
        else:
            for metric_id in metric_ids:
                metric = metric_by_id[metric_id]
                linked_facts = metric.get("supporting_fact_ids", [])
                linked_sources = metric.get("source_ids", [])
                linked_fact = linked_facts[0] if linked_facts else None
                linked_source = linked_sources[0] if linked_sources else None
                if linked_fact:
                    fact_ids.append(linked_fact)
                if linked_source:
                    source_ids.append(linked_source)
                display = metric["display"]
                yoy = display["yoy_percent"]
                markers = " ".join(
                    f"[{item}]"
                    for item in (metric_id, linked_fact, linked_source)
                    if item
                )
                sentence_parts.append(
                    f"{metric['item_label']}由{display['prior_value']['hundred_million_yuan']}亿元"
                    f"变为{display['current_value']['hundred_million_yuan']}亿元，"
                    f"变化{display['absolute_change']['hundred_million_yuan']}亿元，"
                    f"同比{yoy}%。{markers}"
                )
                disclosed = [
                    item
                    for item in explanation_by_metric.get(metric_id, [])
                    if item.get("kind") == "disclosed_reason"
                ]
                if disclosed:
                    explanation = disclosed[0]
                    explanation_ids.append(explanation["explanation_id"])
                    for fact_id in explanation.get("disclosed_fact_ids", []):
                        if fact_id not in fact_ids:
                            fact_ids.append(fact_id)
                    for source_id in explanation.get("source_ids", []):
                        if source_id not in source_ids:
                            source_ids.append(source_id)
                    citations = " ".join(
                        f"[{item}]"
                        for item in [
                            explanation["explanation_id"],
                            *explanation.get("disclosed_fact_ids", []),
                            *explanation.get("source_ids", []),
                        ]
                    )
                    sentence_parts.append(
                        f"公司披露：{explanation['statement']}{citations}"
                    )
                sentence_parts.append(
                    "从授信角度仍需结合明细、期后表现和业务可持续性继续核验。"
                )
        sections.append(
            {
                "section_id": f"FS{next_index:03d}",
                "topic": topic,
                "heading": topic.replace("_", " "),
                "analysis_markdown": "\n\n".join(sentence_parts),
                "metric_ids": metric_ids,
                "ratio_ids": [],
                "fact_ids": list(dict.fromkeys(fact_ids)),
                "explanation_ids": explanation_ids,
                "inference_ids": [],
                "source_ids": list(dict.fromkeys(source_ids)),
                "missing_information_ids": missing_ids,
            }
        )
        next_index += 1
    question_metric = contract["allowed_ids"]["metric_ids"][:1]
    question_fact = contract["allowed_ids"]["fact_ids"][:1]
    question_missing = contract["allowed_ids"]["missing_information_ids"][:1]
    return json.dumps(
        {
            "report_title": f"{payload['identity']['short_name']}财务分析（离线 Mock）",
            "sections": sections,
            "due_diligence_questions": [
                {
                    "question_id": "FQ001",
                    "question": "请提供重点科目明细、期后回款以及最新借款和授信台账，以验证财务变化的持续性。",
                    "supporting_metric_ids": question_metric,
                    "supporting_fact_ids": question_fact,
                    "related_missing_information_ids": question_missing,
                }
            ],
        },
        ensure_ascii=False,
    )


def _default_mock_model(context: FinancialAgentContext) -> ScriptedModel:
    return ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        FINANCIAL_FUNCTION_TOOL,
                        {"ticker": context.identity.ticker},
                        call_id="mock-financial-call-1",
                    )
                ],
                usage=Usage(requests=1, input_tokens=20, output_tokens=12, total_tokens=32),
            ),
            ModelStep.respond(
                lambda _call: ModelStep(
                    output=[assistant_message(_mock_narrative(context))],
                    usage=Usage(
                        requests=1,
                        input_tokens=60,
                        output_tokens=80,
                        total_tokens=140,
                    ),
                )
            ),
        ]
    )


class FinancialResearchAgentRuntime:
    """V2.3 财务 Agent；默认 Mock，Live 仍需显式授权。"""

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
            lambda: "AF-" + uuid4().hex[:12].upper()
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
        comparison_inputs: Sequence[FinancialSourceInput],
        financial_dir: Path,
        trace_output: Path,
        skill_trace_output: Path,
        confirmed_live: bool = False,
        tool_output_overrides: Optional[Mapping[str, Any]] = None,
    ) -> AgentRunResult:
        if not any(keyword in task for keyword in FINANCIAL_TASK_KEYWORDS):
            raise AgentRuntimeError(
                "unsupported_financial_agent_task",
                "V2.3财务Agent只接受财务分析任务",
                trace_path=Path(trace_output),
            )
        if not comparison_inputs:
            raise AgentRuntimeError(
                "financial_comparison_source_required",
                "V2.3财务Agent至少需要一份可比官方年报",
                trace_path=Path(trace_output),
            )
        if self.config.mode is AgentMode.LIVE and not confirmed_live:
            raise AgentRuntimeError(
                "live_confirmation_required",
                "Live模式必须显式确认后才能发送付费请求",
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
            "财务主Agent运行开始",
            {"mode": self.config.mode.value, "provider": self.config.provider},
        )
        try:
            bound_source = _bind_runtime_source(identity, source, download)
            for item in comparison_inputs:
                _bind_runtime_source(identity, item.source, item.download)
        except AgentRuntimeError as exc:
            trace.status = AgentExecutionStatus.FAILED
            trace.error_code = exc.code
            trace.error_message = str(exc)
            _write_trace(Path(trace_output), trace)
            raise AgentRuntimeError(
                exc.code, str(exc), trace_path=Path(trace_output)
            ) from exc
        trace.bound_source_id = bound_source.source_id
        _append_event(
            trace,
            self.clock,
            AgentExecutionStage.RUNTIME_SOURCE_BOUND,
            "Python Runtime已绑定主报告与可比官方来源",
            {
                "source_id": bound_source.source_id,
                "comparison_source_ids": [
                    item.source.source_id for item in comparison_inputs
                ],
            },
        )
        context = FinancialAgentContext(
            project_root=self.project_root,
            task=task,
            identity=identity,
            source=bound_source,
            download=download,
            summary_path=Path(summary_path),
            pages_path=Path(pages_path),
            comparison_inputs=list(comparison_inputs),
            financial_dir=Path(financial_dir),
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

            agent = Agent[FinancialAgentContext](
                name="企业授信研究Agent-财务分析",
                instructions=FINANCIAL_AGENT_INSTRUCTIONS,
                model=model,
                tools=[create_financial_function_tool()],
                model_settings=build_model_settings(self.config),
            )
            result = await Runner.run(
                agent,
                task,
                context=context,
                max_turns=self.config.max_turns,
                hooks=_AgentTraceHooks(self.clock),
                run_config=RunConfig(
                    workflow_name="Corporate Credit Research V2.3",
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                ),
            )
            if context.skill_result is None or context.compact_result is None:
                raise AgentRuntimeError(
                    "required_tool_not_called", "模型未调用必需的财务研究Tool"
                )
            candidate_output = str(result.final_output)
            _record_model_candidate(trace, self._redact_secret(candidate_output))
            # 即使后续事实校验失败，也保留本次模型实际用量和 finish_reason。
            trace.usage = _usage_summary(result.context_wrapper.usage)
            _copy_finish_reasons(trace, model)
            analysis_path = Path(context.skill_result.artifacts["analysis_path"])
            questions_path = Path(context.skill_result.artifacts["questions_path"])
            draft_path = analysis_path.with_name("financial_llm_draft.json")
            narrative_validation_path = analysis_path.with_name(
                "financial_llm_validation.json"
            )
            _atomic_write(
                draft_path,
                json.dumps(
                    {
                        "schema_version": "2.3.1",
                        "provider": self.config.provider,
                        "model": self.config.model,
                        "mode": self.config.mode.value,
                        "raw_model_output": self._redact_secret(candidate_output),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            context.skill_result.artifacts["llm_draft_path"] = str(
                draft_path.resolve()
            )
            try:
                draft = parse_financial_narrative_draft(candidate_output)
            except AgentRuntimeError as exc:
                _atomic_write(
                    narrative_validation_path,
                    json.dumps(
                        {
                            "status": "failed",
                            "generation_mode": self.config.mode.value,
                            "llm_authored_report": self.config.mode is AgentMode.LIVE,
                            "python_rewrote_llm_text": False,
                            "parse_error": str(exc),
                            "rule_id": exc.rule_id,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                context.skill_result.artifacts["llm_validation_path"] = str(
                    narrative_validation_path.resolve()
                )
                context.skill_result.artifacts["financial_report_status"] = "failed"
                context.skill_result.artifacts["generation_mode"] = (
                    self.config.mode.value
                )
                context.skill_result.artifacts["llm_authored_report"] = (
                    self.config.mode is AgentMode.LIVE
                )
                trace.artifact_paths = _artifact_paths(
                    context.skill_result.artifacts
                )
                raise
            accepted_sections, accepted_questions, narrative_validation = (
                validate_financial_narrative(draft, context.compact_result)
            )
            narrative_validation.generation_mode = self.config.mode.value
            narrative_validation.llm_authored_report = (
                self.config.mode is AgentMode.LIVE
            )
            _atomic_write(
                narrative_validation_path,
                json.dumps(
                    narrative_validation.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            context.skill_result.artifacts["llm_validation_path"] = str(
                narrative_validation_path.resolve()
            )
            context.skill_result.artifacts["financial_report_status"] = (
                narrative_validation.status
            )
            context.skill_result.artifacts["generation_mode"] = (
                self.config.mode.value
            )
            context.skill_result.artifacts["llm_authored_report"] = (
                narrative_validation.llm_authored_report
            )
            final_output = render_llm_financial_report(
                draft,
                accepted_sections,
                narrative_validation,
                context.compact_result,
                model_mode=self.config.mode.value,
            )
            _atomic_write(analysis_path, final_output)
            _atomic_write(
                questions_path,
                render_llm_financial_questions(
                    accepted_questions,
                    narrative_validation,
                    model_mode=self.config.mode.value,
                ),
            )
            context.skill_result.artifacts["analysis_path"] = str(
                analysis_path.resolve()
            )
            context.skill_result.artifacts["questions_path"] = str(
                questions_path.resolve()
            )
            trace.selected_fact_ids = sorted(
                {
                    fact_id
                    for section in accepted_sections
                    for fact_id in section.fact_ids
                }
            )
            trace.selected_missing_information_ids = sorted(
                {
                    missing_id
                    for section in accepted_sections
                    for missing_id in section.missing_information_ids
                }
            )
            trace.selected_analysis_claim_ids = [
                section.section_id for section in accepted_sections
            ]
            trace.artifact_paths = _artifact_paths(context.skill_result.artifacts)
            if narrative_validation.status == "failed":
                raise AgentRuntimeError(
                    "financial_narrative_validation_failed",
                    "DeepSeek返回的财务分析段落均未通过事实与来源校验；"
                    "结构化数据和未完成报告已保留",
                    rule_id="FIN-NARRATIVE-013",
                    rejected_text=candidate_output[:1000],
                )
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.GROUNDED_OUTPUT_VALIDATED,
                "DeepSeek财务分析正文已逐段校验；通过段落原样写入报告，Python未改写正文",
                {
                    "financial_report_status": narrative_validation.status,
                    "accepted_section_ids": narrative_validation.accepted_section_ids,
                    "rejected_section_count": len(
                        narrative_validation.rejected_sections
                    ),
                    "missing_topics": narrative_validation.missing_topics,
                    "llm_authored_report": narrative_validation.llm_authored_report,
                    "generation_mode": self.config.mode.value,
                    "python_rewrote_llm_text": False,
                },
            )
            trace.status = AgentExecutionStatus.SUCCEEDED
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.RUN_COMPLETED,
                "财务主Agent运行完成",
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
            nested = _find_nested_runtime_error(exc)
            if nested is not None:
                code = nested.code
                message = str(nested)
                trace.validator_rule_id = nested.rule_id
                trace.rejected_sentence = nested.rejected_text
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
                code = "financial_agent_run_failed"
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
                "财务主Agent运行失败",
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

