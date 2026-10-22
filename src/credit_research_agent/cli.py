"""命令行入口：识别企业、处理年报并构建可追溯研究数据。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from credit_research_agent.company import CompanyResolutionError, CompanyResolver
from credit_research_agent.downloader import DownloadError, OfficialPdfDownloader
from credit_research_agent.evidence import EvidenceBuildError, build_evidence_artifacts
from credit_research_agent.fundamentals import build_fundamental_artifacts
from credit_research_agent.ownership import (
    OwnershipBuildError,
    build_ownership_artifacts,
)
from credit_research_agent.pdf_parser import PdfExtractionError, extract_to_artifacts
from credit_research_agent.skill_runtime import SkillRuntime, SkillRuntimeError
from credit_research_agent.sources import SourceCatalog, SourceNotFoundError


def _as_dict(model: BaseModel) -> Dict[str, Any]:
    """兼容当前 Pydantic 1.x 和后续 2.x。"""

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credit-research-agent",
        description="企业授信研究Agent V1.3：识别主体、处理年报并以可追踪 Skill 调用确定性 Tools。",
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
    if args.run_skill and not args.skill_source:
        output["skill_error"] = {
            "code": "missing_skill_source",
            "message": "--run-skill 必须通过 --skill-source 指定已登记官方来源",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 8
    requested_source_id = (
        args.skill_source
        if args.run_skill
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
                runtime = SkillRuntime(Path(__file__).resolve().parents[2])
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

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

