"""命令行入口：识别企业、处理年报并构建可追溯研究数据。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic.v1 import BaseModel

from credit_research_agent.agent_runtime import (
    AgentRuntimeError,
    CreditResearchAgentRuntime,
)
from credit_research_agent.company import CompanyResolutionError, CompanyResolver
from credit_research_agent.downloader import DownloadError, OfficialPdfDownloader
from credit_research_agent.evidence import EvidenceBuildError, build_evidence_artifacts
from credit_research_agent.fundamentals import build_fundamental_artifacts
from credit_research_agent.ownership import (
    OwnershipBuildError,
    build_ownership_artifacts,
)
from credit_research_agent.pdf_parser import PdfExtractionError, extract_to_artifacts
from credit_research_agent.llm_config import AgentMode, LLMConfigError, load_deepseek_config
from credit_research_agent.skill_runtime import SkillRuntime, SkillRuntimeError
from credit_research_agent.sources import SourceCatalog, SourceNotFoundError


def _as_dict(model: BaseModel) -> Dict[str, Any]:
    """兼容当前 Pydantic 1.x 和后续 2.x。"""

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def _find_project_root() -> Path:
    """定位包含本地Skill目录的项目根。

    editable安装时源码路径位于仓库内，普通wheel安装时则位于site-packages；因此优先
    从当前工作目录向上查找，再回退到源码位置。只接受同时含pyproject和Skill目录的
    路径，避免把模型指向任意目录。
    """

    candidates = [Path.cwd(), *Path.cwd().parents]
    source_path = Path(__file__).resolve()
    candidates.extend([source_path.parent, *source_path.parents])
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (
            (resolved / "pyproject.toml").is_file()
            and (resolved / ".agents" / "skills").is_dir()
        ):
            return resolved
    raise RuntimeError(
        "无法定位包含 pyproject.toml 与 .agents/skills 的项目根；请在项目目录运行CLI"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credit-research-agent",
        description="企业授信研究Agent V1.3B：确定性Skill Runtime与受控DeepSeek Agent Loop。",
    )
    parser.add_argument("company", help="上市公司名称、证券代码或带交易所后缀的代码")
    parser.add_argument(
        "--sources",
        action="store_true",
        help="同时输出该企业及通用官方来源登记记录",
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--download-source",
        metavar="SOURCE_ID",
        help="下载已登记的年度报告，例如 S01",
    )
    action_group.add_argument(
        "--parse-source",
        metavar="SOURCE_ID",
        help="下载或复用缓存，并逐页提取年度报告文本",
    )
    action_group.add_argument(
        "--build-evidence",
        metavar="SOURCE_ID",
        help="执行下载、解析，并生成事实证据 JSON 与来源引用",
    )
    action_group.add_argument(
        "--build-fundamentals",
        metavar="SOURCE_ID",
        help="执行下载、解析，并生成V1.1企业基本面JSON与来源引用",
    )
    action_group.add_argument(
        "--build-ownership",
        metavar="SOURCE_ID",
        help="执行下载、解析，并生成V1.2股权数据、展示图JSON、DOT和SVG",
    )
    action_group.add_argument(
        "--run-skill",
        metavar="TASK",
        help="按任务选择并运行白名单 Skill；V1.3 当前可执行股权结构任务",
    )
    action_group.add_argument(
        "--run-agent",
        metavar="TASK",
        help="运行V1.3B主Agent；默认Mock，仅开放ownership-structure",
    )
    parser.add_argument(
        "--skill-source",
        metavar="SOURCE_ID",
        help="--run-skill 使用的已登记官方来源，例如 S01",
    )
    parser.add_argument(
        "--skill-trace-output",
        help="可选的 SkillExecutionTrace JSON 输出路径",
    )
    parser.add_argument(
        "--agent-source",
        metavar="SOURCE_ID",
        help="--run-agent 使用的已登记官方来源，例如 S01",
    )
    parser.add_argument(
        "--llm-mode",
        choices=[item.value for item in AgentMode],
        default=AgentMode.MOCK.value,
        help="LLM运行模式，默认mock；live会产生真实API费用",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="确认发送一次受限的真实DeepSeek请求；没有此参数绝不调用Live模型",
    )
    parser.add_argument(
        "--env-file",
        help="可选的本地.env路径；默认读取项目根目录/.env",
    )
    parser.add_argument(
        "--agent-trace-output",
        help="可选的AgentExecutionTrace JSON输出路径",
    )
    parser.add_argument(
        "--max-agent-turns",
        type=int,
        default=4,
        help="Agent最大模型轮次，默认4，允许2—8",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=1,
        help="单次Agent运行最大Tool调用次数，默认1，允许1—3",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=800,
        help="单次模型响应最大输出Token，默认800、上限5000；不是Agent总Token预算",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/cache",
        help="下载缓存目录，默认 data/cache",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="忽略有效缓存并重新下载",
    )
    parser.add_argument(
        "--parsed-dir",
        default="data/parsed",
        help="完整解析产物目录，默认 data/parsed",
    )
    parser.add_argument(
        "--sample-output",
        help="可选的轻量章节定位样例输出路径",
    )
    parser.add_argument(
        "--evidence-dir",
        default="data/evidence",
        help="证据构建运行产物目录，默认 data/evidence",
    )
    parser.add_argument(
        "--evidence-output",
        help="可选的证据 JSON 输出路径",
    )
    parser.add_argument(
        "--citations-output",
        help="可选的 [Sxx] Markdown 来源引用输出路径",
    )
    parser.add_argument(
        "--fundamentals-dir",
        default="data/fundamentals",
        help="企业基本面运行产物目录，默认 data/fundamentals",
    )
    parser.add_argument(
        "--fundamentals-output",
        help="可选的企业基本面 JSON 输出路径",
    )
    parser.add_argument(
        "--ownership-dir",
        default="data/ownership",
        help="股权结构运行产物目录，默认 data/ownership",
    )
    parser.add_argument(
        "--ownership-dataset-output",
        help="可选的原始股权数据 JSON 输出路径",
    )
    parser.add_argument(
        "--ownership-graph-output",
        help="可选的股权展示图 JSON 输出路径",
    )
    parser.add_argument(
        "--ownership-svg-output",
        help="可选的股权结构 SVG 输出路径（同目录同名生成 PNG 和 DOT）",
    )
    parser.add_argument(
        "--ownership-summary-output",
        help="可选的股权结构文字说明 Markdown 输出路径",
    )
    parser.add_argument(
        "--ownership-validation-output",
        help="可选的逐分支比例校验 JSON 输出路径",
    )
    parser.add_argument(
        "--ownership-sources-output",
        help="可选的股权结构来源目录 JSON 输出路径",
    )
    parser.add_argument(
        "--ownership-questions-output",
        help="可选的股权结构待核查事项 Markdown 输出路径",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 在输出被重定向时可能沿用旧代码页；优先输出 UTF-8 JSON。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    resolver = CompanyResolver.from_default_catalog()

    try:
        company = resolver.resolve(args.company)
    except CompanyResolutionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    output: Dict[str, Any] = {"company": _as_dict(company)}
    if args.skill_source and not args.run_skill:
        output["skill_error"] = {
            "code": "orphan_skill_source",
            "message": "--skill-source 只能与 --run-skill 一起使用",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 8
    if args.agent_source and not args.run_agent:
        output["agent_error"] = {
            "code": "orphan_agent_source",
            "message": "--agent-source 只能与 --run-agent 一起使用",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 9
    if args.run_skill and not args.skill_source:
        output["skill_error"] = {
            "code": "missing_skill_source",
            "message": "--run-skill 必须通过 --skill-source 指定已登记官方来源",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 8
    if args.run_agent and not args.agent_source:
        output["agent_error"] = {
            "code": "missing_agent_source",
            "message": "--run-agent 必须通过 --agent-source 指定已登记官方来源",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 9

    project_root: Optional[Path] = None
    if args.run_skill or args.run_agent:
        try:
            project_root = _find_project_root()
        except RuntimeError as exc:
            output["agent_error"] = {
                "code": "project_root_not_found",
                "message": str(exc),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 9
    llm_config = None
    if args.run_agent:
        assert project_root is not None
        try:
            llm_config = load_deepseek_config(
                project_root,
                mode=args.llm_mode,
                env_file=Path(args.env_file) if args.env_file else None,
                max_turns=args.max_agent_turns,
                max_tool_calls=args.max_tool_calls,
                max_output_tokens=args.max_output_tokens,
            )
            output["agent_config"] = llm_config.sanitized_summary()
        except LLMConfigError as exc:
            output["agent_error"] = {"code": exc.code, "message": str(exc)}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 9
        if llm_config.mode is AgentMode.LIVE:
            # 先把脱敏配置写到stderr供人工确认；绝不显示密钥。
            print(
                json.dumps(
                    {"live_request_preview": llm_config.sanitized_summary()},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            if not args.confirm_live:
                output["agent_error"] = {
                    "code": "live_confirmation_required",
                    "message": "Live配置已显示；如确认产生费用，请重新执行并加入 --confirm-live",
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 9
    requested_source_id = (
        (args.skill_source if args.run_skill else args.agent_source)
        if (args.run_skill or args.run_agent)
        else (
            args.download_source
            or args.parse_source
            or args.build_evidence
            or args.build_fundamentals
            or args.build_ownership
        )
    )
    if args.sources or requested_source_id:
        catalog = SourceCatalog.from_default_catalog()
    if args.sources:
        output["sources"] = [
            _as_dict(source)
            for source in catalog.list_for_entity(company.entity_id)
        ]

    if requested_source_id:
        try:
            source = catalog.get(requested_source_id)
            if source.entity_id not in (None, company.entity_id):
                raise DownloadError(
                    "entity_mismatch",
                    f"来源 {source.source_id} 不属于当前企业 {company.ticker}",
                )
            result = OfficialPdfDownloader().download(
                source,
                Path(args.cache_dir),
                force=args.force_download,
            )
            output["download"] = _as_dict(result)
        except (SourceNotFoundError, DownloadError) as exc:
            error_code = getattr(exc, "code", "source_not_found")
            output["download_error"] = {
                "code": error_code,
                "message": str(exc),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 3

        if (
            args.parse_source
            or args.build_evidence
            or args.build_fundamentals
            or args.build_ownership
            or args.run_skill
            or args.run_agent
        ):
            try:
                artifacts = extract_to_artifacts(
                    result,
                    Path(args.parsed_dir),
                    sample_output=(
                        Path(args.sample_output) if args.sample_output else None
                    ),
                )
                output["extraction"] = _as_dict(artifacts)
            except PdfExtractionError as exc:
                output["extraction_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 4

        if args.build_evidence:
            try:
                evidence_artifacts = build_evidence_artifacts(
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    output_dir=Path(args.evidence_dir),
                    evidence_output=(
                        Path(args.evidence_output) if args.evidence_output else None
                    ),
                    citations_output=(
                        Path(args.citations_output)
                        if args.citations_output
                        else None
                    ),
                )
                output["evidence"] = _as_dict(evidence_artifacts)
            except EvidenceBuildError as exc:
                output["evidence_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 5

        if args.build_fundamentals:
            try:
                fundamental_artifacts = build_fundamental_artifacts(
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    output_dir=Path(args.fundamentals_dir),
                    fundamentals_output=(
                        Path(args.fundamentals_output)
                        if args.fundamentals_output
                        else None
                    ),
                    citations_output=(
                        Path(args.citations_output)
                        if args.citations_output
                        else None
                    ),
                )
                output["fundamentals"] = _as_dict(fundamental_artifacts)
            except EvidenceBuildError as exc:
                output["fundamentals_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 6

        if args.build_ownership:
            try:
                ownership_artifacts = build_ownership_artifacts(
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    output_dir=Path(args.ownership_dir),
                    dataset_output=(
                        Path(args.ownership_dataset_output)
                        if args.ownership_dataset_output
                        else None
                    ),
                    graph_output=(
                        Path(args.ownership_graph_output)
                        if args.ownership_graph_output
                        else None
                    ),
                    svg_output=(
                        Path(args.ownership_svg_output)
                        if args.ownership_svg_output
                        else None
                    ),
                    citations_output=(
                        Path(args.citations_output)
                        if args.citations_output
                        else None
                    ),
                    summary_output=(
                        Path(args.ownership_summary_output)
                        if args.ownership_summary_output
                        else None
                    ),
                    validation_output=(
                        Path(args.ownership_validation_output)
                        if args.ownership_validation_output
                        else None
                    ),
                    sources_output=(
                        Path(args.ownership_sources_output)
                        if args.ownership_sources_output
                        else None
                    ),
                    questions_output=(
                        Path(args.ownership_questions_output)
                        if args.ownership_questions_output
                        else None
                    ),
                )
                output["ownership"] = _as_dict(ownership_artifacts)
            except (EvidenceBuildError, OwnershipBuildError) as exc:
                output["ownership_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 7

        if args.run_skill:
            trace_output = (
                Path(args.skill_trace_output)
                if args.skill_trace_output
                else Path(args.ownership_dir)
                / company.entity_id
                / "skill_execution_trace.json"
            )
            try:
                assert project_root is not None
                runtime = SkillRuntime(project_root)
                skill_result = runtime.execute(
                    task=args.run_skill,
                    identity=company,
                    source=source,
                    tool_arguments={
                        "download": result,
                        "summary_path": artifacts.summary_path,
                        "pages_path": artifacts.pages_path,
                        "output_dir": Path(args.ownership_dir),
                        "dataset_output": (
                            Path(args.ownership_dataset_output)
                            if args.ownership_dataset_output
                            else None
                        ),
                        "graph_output": (
                            Path(args.ownership_graph_output)
                            if args.ownership_graph_output
                            else None
                        ),
                        "svg_output": (
                            Path(args.ownership_svg_output)
                            if args.ownership_svg_output
                            else None
                        ),
                        "citations_output": (
                            Path(args.citations_output)
                            if args.citations_output
                            else None
                        ),
                        "summary_output": (
                            Path(args.ownership_summary_output)
                            if args.ownership_summary_output
                            else None
                        ),
                        "validation_output": (
                            Path(args.ownership_validation_output)
                            if args.ownership_validation_output
                            else None
                        ),
                        "sources_output": (
                            Path(args.ownership_sources_output)
                            if args.ownership_sources_output
                            else None
                        ),
                        "questions_output": (
                            Path(args.ownership_questions_output)
                            if args.ownership_questions_output
                            else None
                        ),
                    },
                    trace_output=trace_output,
                )
                output["skill_run"] = _as_dict(skill_result)
            except SkillRuntimeError as exc:
                output["skill_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                    "trace_path": str(exc.trace_path) if exc.trace_path else None,
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 8

        if args.run_agent:
            agent_trace_output = (
                Path(args.agent_trace_output)
                if args.agent_trace_output
                else Path(args.ownership_dir)
                / company.entity_id
                / "agent_execution_trace.json"
            )
            agent_skill_trace_output = (
                Path(args.skill_trace_output)
                if args.skill_trace_output
                else Path(args.ownership_dir)
                / company.entity_id
                / "agent_skill_execution_trace.json"
            )
            try:
                assert project_root is not None and llm_config is not None
                runtime = CreditResearchAgentRuntime(project_root, llm_config)
                agent_result = runtime.run_sync(
                    task=args.run_agent,
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    ownership_dir=Path(args.ownership_dir),
                    trace_output=agent_trace_output,
                    skill_trace_output=agent_skill_trace_output,
                    confirmed_live=args.confirm_live,
                    tool_output_overrides={
                        "dataset_output": (
                            Path(args.ownership_dataset_output)
                            if args.ownership_dataset_output
                            else None
                        ),
                        "graph_output": (
                            Path(args.ownership_graph_output)
                            if args.ownership_graph_output
                            else None
                        ),
                        "svg_output": (
                            Path(args.ownership_svg_output)
                            if args.ownership_svg_output
                            else None
                        ),
                        "citations_output": (
                            Path(args.citations_output)
                            if args.citations_output
                            else None
                        ),
                        "summary_output": (
                            Path(args.ownership_summary_output)
                            if args.ownership_summary_output
                            else None
                        ),
                        "validation_output": (
                            Path(args.ownership_validation_output)
                            if args.ownership_validation_output
                            else None
                        ),
                        "sources_output": (
                            Path(args.ownership_sources_output)
                            if args.ownership_sources_output
                            else None
                        ),
                        "questions_output": (
                            Path(args.ownership_questions_output)
                            if args.ownership_questions_output
                            else None
                        ),
                    },
                )
                output["agent_run"] = _as_dict(agent_result)
            except AgentRuntimeError as exc:
                output["agent_error"] = {
                    "code": exc.code,
                    "message": str(exc),
                    "trace_path": str(exc.trace_path) if exc.trace_path else None,
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 9

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

