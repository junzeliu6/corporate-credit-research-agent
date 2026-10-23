"""V2.3 财务分析 Function Tool 与 LLM 报告运行入口。

Python 负责财务数据、确定性计算、来源保存和 Markdown 组装；DeepSeek 负责
专业分析与自然语言写作。运行时只检查模型输出能否解析为约定的文档结构，
不会用复杂的自然语言规则筛选正文，也不会用 Python 固定模板替代模型分析。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
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
3. Tool 会加载 financial-analysis/SKILL.md 并执行确定性 Python Tool。所有金额、变化额、比例和比率以 Tool 返回为准，不得自行创造具体财务数据。
4. disclosed_reason 是公司明确披露的原因；supported_inference 是基于数据的分析推断；unverified_hypothesis 是待核实假设。不得把推断或假设写成公司已披露事实。
5. Tool 完成后，你必须亲自撰写完整自然语言报告，并只输出符合 response_contract.final_response_schema 的 JSON。JSON 只是稳定的文档结构，不需要也不得输出 Fact、Metric、Source 等内部技术 ID。
6. 重点采用“上期金额→本期金额→变化额及比例→原因→合理性→授信关注与尽调问题”的分析思路，并进行收入、应收账款、存货、货币资金、短期借款/债务、应付账款和经营现金流的跨指标分析。
7. 对 Tool 未明确标为公司披露的具体原因，使用“可能”“分析判断”“需进一步核实”等措辞；允许专业推断，但不得包装为已经证实的经营事实。
8. 短期借款等缺失数据应写“未取得/无法确认/待补充”，不得把缺失值写成零或推断不存在借款。
9. 不得创造新的金额、比例、期间、股权关系、经营事件或来源。数据不足时明确写未披露、无法确认或待进一步核实。
10. Python 会把你的分析正文直接组装进 Markdown，不会用固定模板替换或重写你的专业分析。
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
    """一段由 LLM 实际撰写的财务分析。

    ``extra="ignore"`` 用于兼容 V2.3 早期已经保存、仍带内部 ID 字段的草稿；
    这些旧字段不会再进入新报告或新模型契约。
    """

    model_config = ConfigDict(extra="ignore")

    topic: str
    heading: str = Field(min_length=1, max_length=80)
    analysis_markdown: str = Field(min_length=1)

    @field_validator("topic")
    @classmethod
    def topic_is_supported(cls, value: str) -> str:
        if value not in FINANCIAL_NARRATIVE_TOPICS:
            raise ValueError(f"不支持的财务分析主题：{value}")
        return value


class FinancialDueDiligenceQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1)


class FinancialNarrativeDraft(BaseModel):
    """DeepSeek 返回的完整财务分析草稿；正文位于 sections。"""

    model_config = ConfigDict(extra="ignore")

    report_title: str = Field(min_length=1)
    sections: List[FinancialNarrativeSection] = Field(min_length=1)
    due_diligence_questions: List[FinancialDueDiligenceQuestion] = Field(
        default_factory=list
    )


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


def _without_internal_ids(value: Any) -> Any:
    """递归移除仅供 Python 内部关联使用的技术标识。

    原始 JSON、来源目录和执行轨迹仍完整保留这些标识；这里只清理发送给 LLM
    的事实包，避免模型把 Fact/Metric/Source 等编号复制进研究正文。
    """

    if isinstance(value, Mapping):
        return {
            key: _without_internal_ids(item)
            for key, item in value.items()
            if not key.endswith("_id")
            and not key.endswith("_ids")
            and key not in {"id", "local_path"}
        }
    if isinstance(value, list):
        return [_without_internal_ids(item) for item in value]
    return value


def _build_financial_analysis_context_from_artifacts(
    artifacts: Mapping[str, Any],
) -> Dict[str, Any]:
    """从确定性产物构造发送给模型的紧凑事实包。"""

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
        if "短期借款" in item.get("topic", "") or "短期借款" in item.get("description", "")
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

    metric_metadata = {
        item["metric_id"]: {
            "financial_item": item.get("item"),
            "item_label": item.get("item_label"),
        }
        for item in selected_metrics
    }
    model_explanations: List[Dict[str, Any]] = []
    for item in explanations:
        clean_item = _without_internal_ids(item)
        clean_item.update(metric_metadata.get(item.get("metric_id"), {}))
        model_explanations.append(clean_item)
    model_hypotheses: List[Dict[str, Any]] = []
    for item in hypotheses:
        clean_item = _without_internal_ids(item)
        clean_item.update(metric_metadata.get(item.get("metric_id"), {}))
        model_hypotheses.append(clean_item)

    return {
        "identity": _without_internal_ids(facts_payload["identity"]),
        "focus_items": list(FOCUS_FINANCIAL_ITEMS),
        "metrics": [
            _without_internal_ids(_metric_with_display(item))
            for item in selected_metrics
        ],
        "ratios": _without_internal_ids(ratios),
        "disclosed_facts": _without_internal_ids(raw_facts),
        "evidence_excerpts": _without_internal_ids(evidence),
        "explanations": model_explanations,
        "inferences": _without_internal_ids(inferences),
        "hypotheses": model_hypotheses,
        "python_analysis_hints": {
            "reasonableness_assessments": _without_internal_ids(assessments),
            "risk_signals": _without_internal_ids(risk_flags),
            "notice": "仅作分析线索，不是公司披露，也不是正式报告正文。",
        },
        "missing_information": _without_internal_ids(missing_information),
        "sources": _without_internal_ids(sources_payload.get("sources", [])),
        "response_contract": {
            "version": "2.3.2-simplified",
            "required_topics": list(topic_requirements),
            "final_response_schema": {
                "report_title": "string",
                "sections": [
                    {
                        "topic": "one required topic",
                        "heading": "string",
                        "analysis_markdown": "DeepSeek撰写的完整分析段落",
                    }
                ],
                "due_diligence_questions": [{"question": "DeepSeek撰写的具体尽调问题"}],
            },
            "python_will_preserve_analysis_markdown": True,
            "python_will_not_generate_fallback_analysis": True,
            "prohibit_llm_arithmetic": True,
            "prohibit_unsupported_financial_facts": True,
        },
    }


def build_financial_snapshot_from_artifacts(
    artifacts: Mapping[str, Any],
) -> Dict[str, Any]:
    """为其他研究模块提供不含内部技术 ID 的紧凑财务快照。

    V2.4 行业研究只需要确定性的财务表现和已披露原因，不需要财务
    Agent 自己的响应契约、证据长文或风险提示模板。这里复用 V2.3 已有
    的选数与计算结果，再采用字段白名单投影，避免把 Fact/Metric/Source
    等内部编号或大段原文重复发送给模型。
    """

    context = _build_financial_analysis_context_from_artifacts(artifacts)
    metric_fields = (
        "item",
        "item_label",
        "prior_period",
        "current_period",
        "prior_value",
        "current_value",
        "absolute_change",
        "yoy_percent",
        "change_kind",
        "change_display",
        "requires_deep_analysis",
        "trigger_reasons",
    )
    ratio_fields = (
        "name",
        "period",
        "value_percent",
        "formula",
    )
    explanation_fields = (
        "financial_item",
        "item_label",
        "kind",
        "statement",
        "limitation",
        "period",
    )
    inference_fields = (
        "kind",
        "statement",
        "conclusion",
        "limitation",
        "status",
        "verification_status",
        "required_evidence",
    )
    missing_fields = ("topic", "description", "reason", "impact")

    def project(items: Any, fields: Sequence[str]) -> List[Dict[str, Any]]:
        if not isinstance(items, list):
            return []
        return [
            {key: item[key] for key in fields if key in item}
            for item in items
            if isinstance(item, Mapping)
        ]

    identity = context.get("identity") or {}
    metrics = project(context.get("metrics"), metric_fields)
    for metric in metrics:
        if metric.get("change_kind") == "sign_change" or metric.get("yoy_percent") is None:
            # 跨正负号时机械百分比没有通常的同比含义，不把该值发送给行业模型。
            metric["yoy_percent"] = None
            metric["change_display"] = "不适用（跨符号或基期不可比）"
        else:
            metric["change_display"] = f"{metric['yoy_percent']}%"

    raw_facts_payload = _json_load(artifacts["facts_path"])
    raw_explanations_payload = _json_load(artifacts["explanations_path"])
    linked_fact_ids = {
        fact_id
        for item in raw_explanations_payload.get("explanations", [])
        for field_name in ("disclosed_fact_ids", "supporting_fact_ids")
        for fact_id in item.get(field_name, [])
    }
    supporting_disclosures = [
        {
            key: item[key]
            for key in ("field_name", "period", "claim", "value")
            if key in item
        }
        for item in raw_facts_payload.get("raw_facts", [])
        if item.get("fact_id") in linked_fact_ids
    ]

    explanations = project(context.get("explanations"), explanation_fields)
    return {
        "company": {
            key: identity[key]
            for key in (
                "legal_name",
                "short_name",
                "ticker",
                "exchange",
                "entity_scope",
            )
            if key in identity
        },
        "scope": {
            "periods": ["FY2024", "FY2025"],
            "currency": "CNY",
            "amount_unit": "yuan",
            "statement_scope": identity.get("entity_scope", "consolidated"),
        },
        "verified_financial_metrics": metrics,
        "verified_ratios": project(context.get("ratios"), ratio_fields),
        "disclosed_reasons": [
            item for item in explanations if item.get("kind") == "disclosed_reason"
        ],
        "supported_inferences": [
            item for item in explanations if item.get("kind") == "supported_inference"
        ],
        "supporting_disclosures": supporting_disclosures,
        "analysis_hypotheses": project(context.get("hypotheses"), inference_fields),
        "missing_information": project(
            context.get("missing_information"), missing_fields
        ),
        "usage_note": (
            "金额、比率和同比变化由Python基于V2.3结构化产物提供；"
            "disclosed_reasons仅包含公司明确披露原因；supported_inferences与"
            "analysis_hypotheses均不是企业披露，使用时必须保留推断或待核实措辞。"
        ),
    }


def _build_financial_analysis_context(result: SkillExecutionResult) -> Dict[str, Any]:
    """构造发送给模型的紧凑事实包，而不是发送完整 PDF 或全量 JSON。"""

    return _build_financial_analysis_context_from_artifacts(result.artifacts)


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
        return FinancialNarrativeDraft.model_validate(raw)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AgentRuntimeError(
            "invalid_financial_narrative_json",
            "模型最终输出不是有效的财务分析草稿JSON",
            rule_id="FIN-NARRATIVE-001",
            rejected_text=stripped[:1000],
        ) from exc


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_INTERNAL_REFERENCE_MARKER = re.compile(
    r"\[(?:FM\d+|FR\d+|RF\d+|FX(?:\d+|-[A-Z0-9-]+)|FH\d+|"
    r"FQ\d+|FS\d+|F\d+|I\d+|S\d+|M\d+)\]"
)


def _remove_internal_reference_markers(text: str) -> str:
    """仅在展示层移除旧草稿中的内部 ID，不改写模型措辞。"""

    cleaned = _INTERNAL_REFERENCE_MARKER.sub("", text)
    # 旧稿偶有“见[M201]”写法；编号移除后同步清理孤立的“见”和重复标点。
    cleaned = re.sub(r"见(?=[，。；、）])", "", cleaned)
    cleaned = re.sub(r"([，。；、])\1+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+([，。；：、])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip()


def _render_core_financial_tables(payload: Mapping[str, Any]) -> List[str]:
    """把 Python 已计算的数据直接渲染为面向读者的简洁表格。"""

    lines = [
        "## 核心财务数据",
        "",
        "> 金额单位：亿元；同比、变化额和财务比率均由 Python 根据结构化财务数据计算。",
        "",
        "| 项目 | 上期 | 本期 | 变化额 | 同比 |",
        "|---|---:|---:|---:|---:|",
    ]
    metric_by_item = {
        str(metric.get("item")): metric for metric in payload.get("metrics", [])
    }
    table_order = (
        "operating_revenue",
        "net_profit",
        "accounts_receivable",
        "inventory",
        "monetary_funds",
        "short_term_borrowings",
        "accounts_payable",
        "operating_cash_flow",
        "prepayments",
    )
    item_labels = {
        "operating_revenue": "营业收入",
        "net_profit": "净利润",
        "accounts_receivable": "应收账款",
        "inventory": "存货",
        "monetary_funds": "货币资金",
        "short_term_borrowings": "短期借款",
        "accounts_payable": "应付账款",
        "operating_cash_flow": "经营活动产生的现金流量净额",
        "prepayments": "预付款项",
    }
    ordered_metrics = [
        metric_by_item[item] for item in table_order if item in metric_by_item
    ]
    ordered_metrics.extend(
        metric
        for item, metric in metric_by_item.items()
        if item not in table_order
    )
    for metric in ordered_metrics:
        display = metric.get("display", {})
        prior = display.get("prior_value", {}).get("hundred_million_yuan", "—")
        current = display.get("current_value", {}).get("hundred_million_yuan", "—")
        change = display.get("absolute_change", {}).get("hundred_million_yuan", "—")
        yoy = display.get("yoy_percent")
        if metric.get("change_kind") == "sign_change":
            # 正负号切换时百分比虽可机械计算，但容易造成经济含义误读。
            yoy_text = "不适用（跨符号）"
        else:
            yoy_text = f"{yoy}%" if yoy is not None else "不适用"
        lines.append(
            f"| {metric.get('item_label', metric.get('item', '未命名项目'))} "
            f"| {metric.get('prior_period', '上期')} {prior} "
            f"| {metric.get('current_period', '本期')} {current} "
            f"| {change} | {yoy_text} |"
        )
    for item in table_order:
        if item not in metric_by_item:
            lines.append(
                f"| {item_labels[item]} | FY2024 未取得 | FY2025 未取得 | — | — |"
            )

    ratio_by_name: Dict[str, Dict[str, Any]] = {}
    for ratio in payload.get("ratios", []):
        ratio_by_name.setdefault(str(ratio.get("name", "未命名比率")), {})[
            str(ratio.get("period", ""))
        ] = ratio
    if ratio_by_name:
        lines.extend(
            [
                "",
                "## 主要财务比率",
                "",
                "| 比率 | FY2024 | FY2025 | 计算口径 |",
                "|---|---:|---:|---|",
            ]
        )
        for name, periods in ratio_by_name.items():
            prior = periods.get("FY2024", {})
            current = periods.get("FY2025", {})
            prior_text = (
                f"{prior['value_percent']}%"
                if prior.get("value_percent") is not None
                else "—"
            )
            current_text = (
                f"{current['value_percent']}%"
                if current.get("value_percent") is not None
                else "—"
            )
            formula = current.get("formula") or prior.get("formula") or "—"
            lines.append(f"| {name} | {prior_text} | {current_text} | {formula} |")
    return lines


def _bound_financial_report_title(
    draft: FinancialNarrativeDraft,
    payload: Mapping[str, Any],
) -> str:
    """用 Python 已绑定主体生成标题，避免模型把报告归到错误企业。"""

    identity = payload.get("identity", {})
    if not isinstance(identity, Mapping):
        return draft.report_title
    legal_name = str(identity.get("legal_name", "")).strip()
    ticker = str(identity.get("ticker", "")).strip()
    if not legal_name or not ticker:
        return draft.report_title
    periods = [
        str(metric.get("current_period", ""))
        for metric in payload.get("metrics", [])
        if isinstance(metric, Mapping)
    ]
    fiscal_years = sorted(
        period[2:]
        for period in periods
        if re.fullmatch(r"FY\d{4}", period)
    )
    year_text = f"{fiscal_years[-1]}年度" if fiscal_years else ""
    return f"{legal_name}（{ticker}）{year_text}财务分析——银行授信视角"


def render_llm_financial_report(
    draft: FinancialNarrativeDraft,
    payload: Mapping[str, Any],
    *,
    model_mode: str = "live",
    human_reviewed_presentation_copy: bool = False,
) -> str:
    """组装 DeepSeek 原文及 Python 数据表，不进行自然语言交付筛选。"""

    lines = [f"# {_bound_financial_report_title(draft, payload)}", ""]
    if model_mode == AgentMode.MOCK.value:
        lines.extend(
            [
                "> 本文来自 ScriptedModel 的离线模拟响应，仅用于验证 Agent Loop 和报告组装；不是 DeepSeek 正式研究成果。",
                "> Python 未改写模拟正文，也不会将其冒充正式报告。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "> AI 生成研究初稿，需由具备相应专业能力的人员复核后使用。",
                "> 可信度说明：Python 数据表属于已核验信息；正文中标注为公司披露的内容仍应在人工复核时对照原始资料。其余原因判断、风险情景和专业推断属于未核验分析，需进一步核实。",
                "> 专业分析正文由 DeepSeek 撰写；Python 仅提供数据、隐藏旧版技术标记并组装 Markdown，未用固定模板替代模型正文。",
            ]
        )
        if human_reviewed_presentation_copy:
            lines.append(
                "> 本报告使用人工审阅展示副本；原始模型输出独立保留，二者差异需人工追溯。"
            )
        lines.append("")
    required_topics = list(
        payload.get("response_contract", {}).get("required_topics", [])
    )
    delivered_topics = {section.topic for section in draft.sections}
    missing_topics = [item for item in required_topics if item not in delivered_topics]
    incomplete_notes: List[str] = []
    if missing_topics:
        incomplete_notes.append("尚未生成章节：" + "、".join(missing_topics))
    if not draft.due_diligence_questions:
        incomplete_notes.append("尚未生成尽调问题")
    if incomplete_notes:
        lines.extend(
            [
                "> 报告完整性：部分完成；" + "；".join(incomplete_notes) + "。",
                "",
            ]
        )
    lines.extend(_render_core_financial_tables(payload))
    lines.append("")
    for section in draft.sections:
        lines.extend(
            [
                f"## {section.heading}",
                "",
                _remove_internal_reference_markers(section.analysis_markdown),
                "",
            ]
        )
    lines.extend(["## 尽调问题", ""])
    if draft.due_diligence_questions:
        lines.extend(
            f"{index}. {_remove_internal_reference_markers(item.question)}"
            for index, item in enumerate(draft.due_diligence_questions, start=1)
        )
    else:
        lines.append("尚未取得模型生成的尽调问题。")
    return "\n".join(lines).rstrip() + "\n"


def render_llm_financial_questions(
    questions: Sequence[FinancialDueDiligenceQuestion],
    *,
    model_mode: str = "live",
) -> str:
    author = "离线 ScriptedModel" if model_mode == AgentMode.MOCK.value else "DeepSeek"
    lines = [f"# 财务尽调问题清单（{author}）", ""]
    if questions:
        lines.extend(
            f"- {_remove_internal_reference_markers(item.question)}"
            for item in questions
        )
    else:
        lines.append(f"- 尚未取得{author}生成的尽调问题。")
    return "\n".join(lines).rstrip() + "\n"


def _build_delivery_manifest(
    draft: FinancialNarrativeDraft,
    payload: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    mode: str,
    human_reviewed_presentation_copy: bool = False,
    editorial_review_scope: str | None = None,
    reviewed_input_path: str | None = None,
) -> Dict[str, Any]:
    required_topics = list(
        payload.get("response_contract", {}).get("required_topics", [])
    )
    delivered_topics = list(dict.fromkeys(item.topic for item in draft.sections))
    missing_topics = [item for item in required_topics if item not in delivered_topics]
    missing_components: List[str] = list(missing_topics)
    if not draft.due_diligence_questions:
        missing_components.append("due_diligence_questions")
    status = "complete" if not missing_components else "partial"
    return {
        "schema_version": "2.3.2-simplified",
        "status": status,
        "status_basis": "required_topics_and_minimum_json_structure",
        "status_note": "complete仅表示必需章节结构完整，不代表逐句事实审计",
        "provider": provider,
        "model": model,
        "mode": mode,
        "section_count": len(draft.sections),
        "question_count": len(draft.due_diligence_questions),
        "delivered_topics": delivered_topics,
        "missing_topics": missing_topics,
        "missing_components": missing_components,
        "llm_authored_report": mode == AgentMode.LIVE.value,
        "python_rewrote_llm_text": False,
        "display_only_internal_markers_removed": True,
        "content_validation_gate": "disabled",
        "automated_content_fact_audit": "not_performed",
        "human_review_required": True,
        "human_reviewed_presentation_copy": human_reviewed_presentation_copy,
        "editorial_review_scope": editorial_review_scope,
        "reviewed_input_path": reviewed_input_path,
        "bound_report_title": _bound_financial_report_title(draft, payload),
    }


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
        _append_event(
            context.trace,
            context.clock,
            AgentExecutionStage.SKILL_RUNTIME_COMPLETED,
            "Registry已确认financial-analysis，Loader已加载SKILL.md，财务Python Tool执行完成",
            {
                "selected_skill": result.selection.selected_skill,
                "loaded_skill_path": result.loaded_skill_path,
                "python_tool": result.tool_name,
                "analysis_metric_count": len(compact["metrics"]),
                "disclosed_fact_count": len(compact["disclosed_facts"]),
                "artifact_count": len(context.trace.artifact_paths),
                "model_context_contains_internal_ids": False,
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
                    "模型不得提供Runtime绑定字段或未声明Tool参数：" + "、".join(unexpected),
                    rule_id="FIN-TOOL-INPUT-001",
                    rejected_text=", ".join(unexpected),
                )
        return await original_invoker(tool_context, raw_input)

    run_financial_research.on_invoke_tool = invoke_with_exact_arguments
    return run_financial_research


def _mock_narrative(context: FinancialAgentContext) -> str:
    """构造离线 ScriptedModel 响应；只用于验证 Agent Loop。"""

    assert context.compact_result is not None
    payload = context.compact_result
    metric_by_item = {item["item"]: item for item in payload["metrics"]}
    explanation_by_item: Dict[str, List[Mapping[str, Any]]] = {}
    for item in payload["explanations"]:
        financial_item = item.get("financial_item")
        if financial_item:
            explanation_by_item.setdefault(str(financial_item), []).append(item)
    headings = {
        "executive_summary": "财务分析摘要",
        "operating_revenue": "营业收入",
        "accounts_receivable": "应收账款",
        "inventory": "存货",
        "monetary_funds": "货币资金",
        "short_term_borrowings_and_debt_structure": "短期借款及债务结构",
        "accounts_payable": "应付账款",
        "operating_cash_flow": "经营活动现金流",
        "cross_metric_analysis": "跨指标分析",
        "other_material_changes": "其他重大变化",
    }
    sections: List[Dict[str, str]] = []
    for topic in payload["response_contract"]["required_topics"]:
        if topic == "executive_summary":
            narrative = "【离线Mock哨兵】本段模拟 DeepSeek 的财务分析正文，仅用于验证" "模型原文不会被 Python 模板替换。"
        elif topic == "short_term_borrowings_and_debt_structure":
            narrative = "FY2024及FY2025短期借款可比数据尚未取得，债务结构无法确认，" "需补充借款及授信明细。"
        elif topic == "cross_metric_analysis":
            labels = [
                metric_by_item[item]["item_label"]
                for item in (
                    "operating_revenue",
                    "accounts_receivable",
                    "operating_cash_flow",
                )
                if item in metric_by_item
            ]
            narrative = (
                f"{'、'.join(labels)}需要联动观察；当前组合显示回款质量可能有所改善，" "但持续性仍需核对客户结构与期后回款。"
            )
        else:
            item_name = TOPIC_ITEM_MAP.get(topic)
            metric = metric_by_item.get(item_name or "")
            if metric is None:
                narrative = "当前结构化数据不足，本节需进一步补充资料。"
            else:
                display = metric["display"]
                narrative = (
                    f"{metric['item_label']}由"
                    f"{display['prior_value']['hundred_million_yuan']}亿元变为"
                    f"{display['current_value']['hundred_million_yuan']}亿元，变化"
                    f"{display['absolute_change']['hundred_million_yuan']}亿元，同比"
                    f"{display['yoy_percent']}%。"
                )
                disclosed = [
                    item
                    for item in explanation_by_item.get(item_name or "", [])
                    if item.get("kind") == "disclosed_reason"
                ]
                if disclosed:
                    narrative += f"公司披露：{disclosed[0]['statement']}"
                narrative += "从授信角度仍需结合明细、期后表现和业务可持续性继续核验。"
        sections.append(
            {
                "topic": topic,
                "heading": headings.get(topic, topic.replace("_", " ")),
                "analysis_markdown": narrative,
            }
        )
    return json.dumps(
        {
            "report_title": f"{payload['identity']['short_name']}财务分析（离线 Mock）",
            "sections": sections,
            "due_diligence_questions": [
                {"question": "请提供重点科目明细、期后回款以及最新借款和授信台账，以验证财务变化的持续性。"}
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
                usage=Usage(
                    requests=1, input_tokens=20, output_tokens=12, total_tokens=32
                ),
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


def restore_financial_report_from_saved_draft(
    draft_path: str | Path,
    *,
    output_path: str | Path | None = None,
    questions_path: str | Path | None = None,
    delivery_manifest_path: str | Path | None = None,
) -> Dict[str, Path]:
    """从已保存的 LLM 草稿和同目录财务 JSON 离线恢复报告。

    该函数不创建模型客户端、不会进行网络请求，也不会重新生成分析文字。
    ``financial_llm_draft.json`` 中的模型原文保持不变。若同目录存在最小化的
    ``financial_llm_reviewed.json``，优先用该人工审阅展示副本恢复；否则回退到
    原始模型草稿。最终 Markdown 的展示层仍会隐藏旧版内部引用标记。
    """

    saved_draft_path = Path(draft_path).resolve()
    entity_dir = saved_draft_path.parent
    saved = _json_load(saved_draft_path)
    if not isinstance(saved, Mapping):
        raise AgentRuntimeError("invalid_saved_financial_draft", "已保存的财务草稿必须是JSON对象")
    reviewed_path = entity_dir / "financial_llm_reviewed.json"
    review_metadata_path = entity_dir / "financial_llm_reviewed.metadata.json"
    human_reviewed_presentation_copy = reviewed_path.is_file()
    editorial_review_scope: str | None = None
    reviewed_input_path: str | None = None
    reviewed_base_draft_sha256: str | None = None
    if human_reviewed_presentation_copy:
        if not review_metadata_path.is_file():
            raise AgentRuntimeError(
                "missing_financial_review_metadata",
                "人工审阅展示副本缺少原始草稿绑定信息",
            )
        review_metadata = _json_load(review_metadata_path)
        expected_draft_sha256 = str(
            review_metadata.get("base_draft_sha256", "")
            if isinstance(review_metadata, Mapping)
            else ""
        ).lower()
        actual_draft_sha256 = _file_sha256(saved_draft_path)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_draft_sha256)
            or expected_draft_sha256 != actual_draft_sha256
        ):
            raise AgentRuntimeError(
                "stale_financial_review_copy",
                "人工审阅展示副本与当前原始模型草稿不匹配，请重新审阅或移除旧副本",
            )
        reviewed_base_draft_sha256 = actual_draft_sha256
        raw_text = reviewed_path.read_text(encoding="utf-8")
        editorial_review_scope = (
            "人工审阅展示副本；Python未执行逐句内容差异或事实审计"
        )
        reviewed_input_path = reviewed_path.name
    else:
        raw_output = saved.get("raw_model_output", saved)
        if isinstance(raw_output, Mapping):
            raw_text = json.dumps(raw_output, ensure_ascii=False)
        elif isinstance(raw_output, str):
            raw_text = raw_output
        else:
            raise AgentRuntimeError(
                "invalid_saved_financial_draft", "草稿缺少可解析的raw_model_output"
            )

    artifacts = {
        "facts_path": entity_dir / "financial_facts.json",
        "metrics_path": entity_dir / "financial_metrics.json",
        "explanations_path": entity_dir / "financial_explanations.json",
        "reasonableness_path": entity_dir / "financial_reasonableness.json",
        "risk_flags_path": entity_dir / "financial_risk_flags.json",
        "sources_path": entity_dir / "financial_sources.json",
    }
    missing_files = [
        str(path) for path in artifacts.values() if not Path(path).is_file()
    ]
    if missing_files:
        raise AgentRuntimeError(
            "missing_saved_financial_artifacts",
            "离线恢复缺少财务结构化产物：" + "、".join(missing_files),
        )

    payload = _build_financial_analysis_context_from_artifacts(artifacts)
    draft = parse_financial_narrative_draft(raw_text)
    mode = str(saved.get("mode", AgentMode.LIVE.value))
    provider = str(saved.get("provider", "unknown"))
    model = str(saved.get("model", "unknown"))
    report_path = (
        Path(output_path).resolve()
        if output_path is not None
        else entity_dir / "financial_analysis.md"
    )
    question_output = (
        Path(questions_path).resolve()
        if questions_path is not None
        else entity_dir / "financial_questions.md"
    )
    manifest_output = (
        Path(delivery_manifest_path).resolve()
        if delivery_manifest_path is not None
        else entity_dir / "financial_llm_delivery.json"
    )
    manifest = _build_delivery_manifest(
        draft,
        payload,
        provider=provider,
        model=model,
        mode=mode,
        human_reviewed_presentation_copy=human_reviewed_presentation_copy,
        editorial_review_scope=editorial_review_scope,
        reviewed_input_path=reviewed_input_path,
    )
    manifest["reviewed_base_draft_sha256"] = reviewed_base_draft_sha256
    _atomic_write(
        report_path,
        render_llm_financial_report(
            draft,
            payload,
            model_mode=mode,
            human_reviewed_presentation_copy=human_reviewed_presentation_copy,
        ),
    )
    _atomic_write(
        question_output,
        render_llm_financial_questions(
            draft.due_diligence_questions,
            model_mode=mode,
        ),
    )
    _atomic_write(
        manifest_output,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "analysis_path": report_path,
        "questions_path": question_output,
        "delivery_manifest_path": manifest_output,
        "draft_path": saved_draft_path,
    }


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
                raise AgentRuntimeError("required_tool_not_called", "模型未调用必需的财务研究Tool")
            candidate_output = str(result.final_output)
            _record_model_candidate(trace, self._redact_secret(candidate_output))
            # 即使后续 JSON 结构解析失败，也保留模型实际用量和 finish_reason。
            trace.usage = _usage_summary(result.context_wrapper.usage)
            _copy_finish_reasons(trace, model)
            analysis_path = Path(context.skill_result.artifacts["analysis_path"])
            questions_path = Path(context.skill_result.artifacts["questions_path"])
            draft_path = analysis_path.with_name("financial_llm_draft.json")
            delivery_manifest_path = analysis_path.with_name(
                "financial_llm_delivery.json"
            )
            _atomic_write(
                draft_path,
                json.dumps(
                    {
                        "schema_version": "2.3.2-simplified",
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
            context.skill_result.artifacts["llm_draft_path"] = str(draft_path.resolve())
            try:
                draft = parse_financial_narrative_draft(candidate_output)
            except AgentRuntimeError as exc:
                _atomic_write(
                    delivery_manifest_path,
                    json.dumps(
                        {
                            "schema_version": "2.3.2-simplified",
                            "status": "failed",
                            "provider": self.config.provider,
                            "model": self.config.model,
                            "mode": self.config.mode.value,
                            "llm_authored_report": self.config.mode is AgentMode.LIVE,
                            "python_rewrote_llm_text": False,
                            "parse_error": str(exc),
                            "content_validation_gate": "disabled",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                context.skill_result.artifacts["llm_delivery_path"] = str(
                    delivery_manifest_path.resolve()
                )
                context.skill_result.artifacts["financial_report_status"] = "failed"
                context.skill_result.artifacts[
                    "generation_mode"
                ] = self.config.mode.value
                context.skill_result.artifacts["llm_authored_report"] = (
                    self.config.mode is AgentMode.LIVE
                )
                trace.artifact_paths = _artifact_paths(context.skill_result.artifacts)
                raise
            delivery_manifest = _build_delivery_manifest(
                draft,
                context.compact_result,
                provider=self.config.provider,
                model=self.config.model,
                mode=self.config.mode.value,
            )
            _atomic_write(
                delivery_manifest_path,
                json.dumps(
                    delivery_manifest,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            context.skill_result.artifacts["llm_delivery_path"] = str(
                delivery_manifest_path.resolve()
            )
            context.skill_result.artifacts[
                "financial_report_status"
            ] = delivery_manifest["status"]
            context.skill_result.artifacts["generation_mode"] = self.config.mode.value
            context.skill_result.artifacts["llm_authored_report"] = delivery_manifest[
                "llm_authored_report"
            ]
            final_output = render_llm_financial_report(
                draft,
                context.compact_result,
                model_mode=self.config.mode.value,
            )
            _atomic_write(analysis_path, final_output)
            _atomic_write(
                questions_path,
                render_llm_financial_questions(
                    draft.due_diligence_questions,
                    model_mode=self.config.mode.value,
                ),
            )
            context.skill_result.artifacts["analysis_path"] = str(
                analysis_path.resolve()
            )
            context.skill_result.artifacts["questions_path"] = str(
                questions_path.resolve()
            )
            trace.artifact_paths = _artifact_paths(context.skill_result.artifacts)
            _append_event(
                trace,
                self.clock,
                AgentExecutionStage.STRUCTURED_OUTPUT_PARSED,
                "DeepSeek财务分析JSON结构已解析，模型正文已直接组装进报告",
                {
                    "financial_report_status": delivery_manifest["status"],
                    "section_count": delivery_manifest["section_count"],
                    "question_count": delivery_manifest["question_count"],
                    "missing_topics": delivery_manifest["missing_topics"],
                    "llm_authored_report": delivery_manifest["llm_authored_report"],
                    "generation_mode": self.config.mode.value,
                    "python_rewrote_llm_text": False,
                    "content_validation_gate": "disabled",
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
