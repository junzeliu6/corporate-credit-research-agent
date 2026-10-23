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
from credit_research_agent.financial import (
    FinancialBuildError,
    FinancialSourceInput,
    build_financial_artifacts,
)
from credit_research_agent.ownership import (
    OwnershipBuildError,
    build_ownership_artifacts,
)
from credit_research_agent.pdf_parser import PdfExtractionError, extract_to_artifacts
from credit_research_agent.reporting import ReportBuildError, build_report_artifacts
from credit_research_agent.llm_config import AgentMode, LLMConfigError, load_deepseek_config
from credit_research_agent.retrieval_runtime import (
    InformationRetrievalRuntime,
    RetrievalRuntimeError,
    build_mock_retrieval_dependencies,
    load_search_fixture,
)
from credit_research_agent.search import (
    SearchBudget,
    SearchError,
    SearchMode,
    load_baidu_search_config,
)
from credit_research_agent.skill_runtime import SkillRuntime, SkillRuntimeError
from credit_research_agent.skills import (
    SkillRegistry,
    SkillRegistryError,
    SkillSelectionError,
    SkillSelector,
)
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
        description="企业授信研究Agent V2.2：结构化研究、财务分析与可追溯公开资料检索。",
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
        "--build-financial",
        metavar="SOURCE_ID",
        help="执行下载、解析及跨期核验，并生成V2.1/V2.2财务分析产物",
    )
    action_group.add_argument(
        "--build-report",
        metavar="SOURCE_ID",
        help="执行下载、解析及V1.1/V1.2研究，并生成V1.4 Markdown报告",
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
    action_group.add_argument(
        "--run-web-research",
        metavar="TASK",
        help="运行V1.5基础信息检索Agent；默认LLM与百度搜索均为Mock",
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
        "--search-mode",
        choices=[item.value for item in SearchMode],
        default=SearchMode.MOCK.value,
        help="百度搜索运行模式，默认mock；live可能消耗免费额度或产生费用",
    )
    parser.add_argument(
        "--confirm-search-live",
        action="store_true",
        help="确认发送受限的真实百度搜索请求；没有此参数绝不调用百度API",
    )
    parser.add_argument(
        "--search-fixture",
        help="可选的V1.5离线Mock fixture JSON；默认使用包内明确标记的测试夹具",
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
        default=None,
        help="显式限制Agent最大模型轮次，允许2—70；V1.3默认4，V1.5默认70",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=None,
        help="显式限制全部Function Tool总调用次数，允许1—60；V1.3默认1，V1.5默认60",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=800,
        help="单次模型响应最大输出Token，默认800、上限5000；不是Agent总Token预算",
    )
    parser.add_argument(
        "--max-search-rounds",
        type=int,
        default=10,
        help="V1.5最多检索轮次；同一模型响应内并行搜索计一轮，默认10",
    )
    parser.add_argument(
        "--max-search-calls",
        type=int,
        default=10,
        help="V1.5最多search_web Function Tool调用次数，默认10",
    )
    parser.add_argument(
        "--max-search-requests",
        type=int,
        default=10,
        help="单次任务最多真实百度搜索API请求数，默认10；缓存命中不计费请求",
    )
    parser.add_argument(
        "--max-search-results",
        type=int,
        default=5,
        help="每次搜索最多结果数，默认5",
    )
    parser.add_argument(
        "--max-source-fetches",
        "--max-fetch-calls",
        dest="max_source_fetches",
        type=int,
        default=20,
        help="单次任务最多调用fetch_source次数，默认20",
    )
    parser.add_argument(
        "--max-extract-calls",
        type=int,
        default=20,
        help="单次任务最多调用extract_evidence次数，默认20",
    )
    parser.add_argument(
        "--max-total-tool-calls",
        type=int,
        default=60,
        help="V1.5全部Function Tool总预算，默认60；显式--max-tool-calls优先",
    )
    parser.add_argument(
        "--max-supplemental-searches",
        type=int,
        default=None,
        help="兼容参数：显式设置首轮后最多补充搜索调用数；建议改用--max-search-rounds",
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
        "--financial-dir",
        default="data/financial",
        help="财务分析运行产物目录，默认 data/financial",
    )
    parser.add_argument(
        "--financial-comparison-source",
        action="append",
        metavar="SOURCE_ID",
        help="可比年度官方年报来源，可重复；默认使用 S10（2024年报）",
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
    parser.add_argument(
        "--report-dir",
        default="data/reports",
        help="Markdown报告运行产物目录，默认 data/reports",
    )
    parser.add_argument(
        "--report-output",
        help="可选的Markdown报告输出路径",
    )
    parser.add_argument(
        "--web-research-dir",
        default="data/web_research",
        help="V1.5搜索缓存、网页原文、来源和证据产物目录",
    )
    parser.add_argument(
        "--retrieval-trace-output",
        help="可选的V1.5 RetrievalExecutionTrace JSON输出路径",
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
    if args.run_skill or args.run_agent or args.run_web_research:
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
    if args.run_agent or args.run_web_research:
        assert project_root is not None
        try:
            llm_config = load_deepseek_config(
                project_root,
                mode=args.llm_mode,
                env_file=Path(args.env_file) if args.env_file else None,
                max_turns=args.max_agent_turns or 4,
                max_tool_calls=args.max_tool_calls or 1,
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
    search_config = None
    if args.run_web_research:
        assert project_root is not None
        try:
            search_config = load_baidu_search_config(
                project_root,
                mode=args.search_mode,
                env_file=Path(args.env_file) if args.env_file else None,
            )
            output["search_config"] = search_config.sanitized_summary()
        except SearchError as exc:
            output["retrieval_error"] = {"code": exc.code, "message": str(exc)}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 11
        if search_config.mode is SearchMode.LIVE:
            print(
                json.dumps(
                    {"live_search_preview": search_config.sanitized_summary()},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            if not args.confirm_search_live:
                output["retrieval_error"] = {
                    "code": "live_search_confirmation_required",
                    "message": (
                        "百度搜索Live配置已显示；如确认消耗额度，请重新执行并加入"
                        " --confirm-search-live"
                    ),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 11
    requested_source_id = (
        (args.skill_source if args.run_skill else args.agent_source)
        if (args.run_skill or args.run_agent)
        else (
            args.download_source
            or args.parse_source
            or args.build_evidence
            or args.build_fundamentals
            or args.build_ownership
            or args.build_financial
            or args.build_report
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
            or args.build_financial
            or args.build_report
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

        if args.build_financial:
            try:
                comparison_inputs: List[FinancialSourceInput] = []
                comparison_source_ids = args.financial_comparison_source or ["S10"]
                for comparison_source_id in comparison_source_ids:
                    if comparison_source_id == source.source_id:
                        continue
                    comparison_source = catalog.get(comparison_source_id)
                    if comparison_source.entity_id not in (None, company.entity_id):
                        raise FinancialBuildError(
                            "comparison_source_entity_mismatch",
                            f"可比来源 {comparison_source_id} 不属于 {company.ticker}",
                        )
                    comparison_download = OfficialPdfDownloader().download(
                        comparison_source,
                        Path(args.cache_dir),
                        force=args.force_download,
                    )
                    comparison_extraction = extract_to_artifacts(
                        comparison_download,
                        Path(args.parsed_dir),
                    )
                    comparison_inputs.append(
                        FinancialSourceInput(
                            source=comparison_source,
                            download=comparison_download,
                            summary_path=comparison_extraction.summary_path,
                            pages_path=comparison_extraction.pages_path,
                        )
                    )
                financial_artifacts = build_financial_artifacts(
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    comparison_inputs=comparison_inputs,
                    output_dir=Path(args.financial_dir),
                )
                output["financial"] = _as_dict(financial_artifacts)
            except (
                SourceNotFoundError,
                DownloadError,
                PdfExtractionError,
                EvidenceBuildError,
                FinancialBuildError,
            ) as exc:
                output["financial_error"] = {
                    "code": getattr(exc, "code", "financial_build_failed"),
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 12

        if args.build_report:
            try:
                report_artifacts = build_report_artifacts(
                    identity=company,
                    source=source,
                    download=result,
                    summary_path=artifacts.summary_path,
                    pages_path=artifacts.pages_path,
                    fundamentals_dir=Path(args.fundamentals_dir),
                    ownership_dir=Path(args.ownership_dir),
                    report_dir=Path(args.report_dir),
                    report_output=(
                        Path(args.report_output) if args.report_output else None
                    ),
                )
                output["report"] = _as_dict(report_artifacts)
            except (EvidenceBuildError, OwnershipBuildError, ReportBuildError) as exc:
                output["report_error"] = {
                    "code": getattr(exc, "code", "report_build_failed"),
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 10

        if args.run_skill:
            try:
                assert project_root is not None
                runtime = SkillRuntime(project_root)
                selected_skill = SkillSelector(
                    SkillRegistry(project_root)
                ).select(args.run_skill).selected_skill
                if selected_skill == "financial-analysis":
                    comparison_inputs = []
                    for comparison_source_id in (
                        args.financial_comparison_source or ["S10"]
                    ):
                        if comparison_source_id == source.source_id:
                            continue
                        comparison_source = catalog.get(comparison_source_id)
                        comparison_download = OfficialPdfDownloader().download(
                            comparison_source,
                            Path(args.cache_dir),
                            force=args.force_download,
                        )
                        comparison_extraction = extract_to_artifacts(
                            comparison_download,
                            Path(args.parsed_dir),
                        )
                        comparison_inputs.append(
                            FinancialSourceInput(
                                source=comparison_source,
                                download=comparison_download,
                                summary_path=comparison_extraction.summary_path,
                                pages_path=comparison_extraction.pages_path,
                            )
                        )
                    tool_arguments = {
                        "download": result,
                        "summary_path": artifacts.summary_path,
                        "pages_path": artifacts.pages_path,
                        "comparison_inputs": comparison_inputs,
                        "output_dir": Path(args.financial_dir),
                    }
                    default_trace_dir = Path(args.financial_dir)
                else:
                    tool_arguments = {
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
                    }
                    default_trace_dir = Path(args.ownership_dir)
                trace_output = (
                    Path(args.skill_trace_output)
                    if args.skill_trace_output
                    else default_trace_dir
                    / company.entity_id
                    / "skill_execution_trace.json"
                )
                skill_result = runtime.execute(
                    task=args.run_skill,
                    identity=company,
                    source=source,
                    tool_arguments=tool_arguments,
                    trace_output=trace_output,
                )
                output["skill_run"] = _as_dict(skill_result)
            except (
                SkillRuntimeError,
                SkillRegistryError,
                SkillSelectionError,
                SourceNotFoundError,
                DownloadError,
                PdfExtractionError,
                FinancialBuildError,
            ) as exc:
                output["skill_error"] = {
                    "code": getattr(exc, "code", "skill_execution_failed"),
                    "message": str(exc),
                    "trace_path": (
                        str(exc.trace_path)
                        if getattr(exc, "trace_path", None)
                        else None
                    ),
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

    if args.run_web_research:
        assert project_root is not None and llm_config is not None and search_config is not None
        retrieval_dir = Path(args.web_research_dir) / company.entity_id
        trace_output = (
            Path(args.retrieval_trace_output)
            if args.retrieval_trace_output
            else retrieval_dir / "retrieval_execution_trace.json"
        )
        provider_override = None
        fetcher_override = None
        if search_config.mode is SearchMode.MOCK:
            try:
                fixture = load_search_fixture(
                    Path(args.search_fixture) if args.search_fixture else None
                )
                provider_override, fetcher_override = build_mock_retrieval_dependencies(
                    fixture
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                output["retrieval_error"] = {
                    "code": "invalid_search_fixture",
                    "message": str(exc),
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 11
        used_fact_ids: List[str] = []
        used_evidence_ids: List[str] = []
        fundamentals_path = (
            Path(args.fundamentals_dir) / company.entity_id / "fundamentals.json"
        )
        if fundamentals_path.is_file():
            try:
                fundamentals = json.loads(fundamentals_path.read_text(encoding="utf-8"))
                used_fact_ids = [
                    str(item["fact_id"])
                    for item in fundamentals.get("facts", [])
                    if item.get("fact_id")
                ]
                used_evidence_ids = [
                    str(item["evidence_id"])
                    for item in fundamentals.get("evidence", [])
                    if item.get("evidence_id")
                ]
            except (OSError, ValueError, json.JSONDecodeError):
                # 既有产物损坏时不静默复用；由Runtime使用独立ID空间继续并在输出说明。
                output["retrieval_warning"] = "既有fundamentals.json无法读取，未复用其ID"
        try:
            budget = SearchBudget(
                max_search_rounds=args.max_search_rounds,
                max_search_calls=args.max_search_calls,
                max_search_requests=args.max_search_requests,
                max_results_per_request=args.max_search_results,
                max_fetches=args.max_source_fetches,
                max_extractions=args.max_extract_calls,
                max_total_tool_calls=(
                    args.max_tool_calls
                    if args.max_tool_calls is not None
                    else args.max_total_tool_calls
                ),
                max_agent_turns=args.max_agent_turns or 70,
                max_supplemental_rounds=args.max_supplemental_searches,
            )
            runtime = InformationRetrievalRuntime(
                project_root,
                llm_config,
                search_config,
                search_provider_override=provider_override,
                source_fetcher_override=fetcher_override,
            )
            retrieval_result = runtime.run_sync(
                task=args.run_web_research,
                identity=company,
                output_dir=retrieval_dir,
                trace_output=trace_output,
                confirmed_llm_live=args.confirm_live,
                confirmed_search_live=args.confirm_search_live,
                budget=budget,
                used_fact_ids=used_fact_ids,
                used_evidence_ids=used_evidence_ids,
            )
            output["web_research"] = _as_dict(retrieval_result)
        except (RetrievalRuntimeError, SearchError) as exc:
            output["retrieval_error"] = {
                "code": getattr(exc, "code", "retrieval_failed"),
                "message": str(exc),
                "trace_path": str(getattr(exc, "trace_path", "") or "") or None,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 11

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

