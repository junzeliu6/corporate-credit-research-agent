"""V1.1 企业基本面结构化提取与证据产物。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic.v1 import BaseModel, Field, ValidationError

from credit_research_agent.documents import DownloadResult
from credit_research_agent.evidence import (
    EvidenceArtifactWriter,
    EvidenceBuildError,
    EvidenceBuildFailureRecord,
    EvidenceBuilder,
    EvidenceSeedCatalog,
    FactSeed,
    EvidenceSeed,
    load_default_seed_catalog,
)
from credit_research_agent.models import (
    CompanyIdentity,
    FundamentalResearchBundle,
    MissingInformation,
    ModelInference,
    SourceRecord,
)


class FundamentalSeedCatalog(EvidenceSeedCatalog):
    """人工审阅的基本面事实锚点，以及显式推断/缺失项。"""

    inferences: List[ModelInference] = Field(default_factory=list)
    missing_information: List[MissingInformation] = Field(default_factory=list)


class FundamentalArtifacts(BaseModel):
    """一次 V1.1 构建成功后的可审计产物。"""

    entity_id: str
    fundamentals_path: Path
    citations_path: Path
    fact_count: int
    evidence_count: int
    inference_count: int
    missing_information_count: int
    module_fact_counts: Dict[str, int]
    source_count: int


def _model_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined,no-any-return]
    return json.loads(model.json())


def load_fundamental_seed_catalog() -> FundamentalSeedCatalog:
    """读取随包发布、经人工核对的 V1.1 年报原文锚点。"""

    path = resources.files("credit_research_agent.data").joinpath(
        "fundamentals_seeds.json"
    )
    return FundamentalSeedCatalog.parse_raw(path.read_text(encoding="utf-8"))


def merge_seed_catalogs(
    identity_seeds: EvidenceSeedCatalog,
    fundamental_seeds: FundamentalSeedCatalog,
) -> EvidenceSeedCatalog:
    """合并 V0 身份证据和 V1.1 基本面证据，并拒绝重复编号。"""

    evidence: List[EvidenceSeed] = [
        *identity_seeds.evidence,
        *fundamental_seeds.evidence,
    ]
    facts: List[FactSeed] = [*identity_seeds.facts, *fundamental_seeds.facts]

    evidence_ids = [item.evidence_id for item in evidence]
    fact_ids = [item.fact_id for item in facts]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceBuildError("duplicate_evidence_seed", "合并后的 evidence_id 存在重复")
    if len(fact_ids) != len(set(fact_ids)):
        raise EvidenceBuildError("duplicate_fact_seed", "合并后的 fact_id 存在重复")
    return EvidenceSeedCatalog(evidence=evidence, facts=facts)


class FundamentalsBuilder:
    """复用 EvidenceBuilder，将已核实事实组织成 V1.1 基本面数据。"""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        identity: CompanyIdentity,
        source: SourceRecord,
        download: DownloadResult,
        summary_path: Path,
        pages_path: Path,
        seeds: Optional[FundamentalSeedCatalog] = None,
    ) -> FundamentalResearchBundle:
        fundamental_seeds = seeds or load_fundamental_seed_catalog()
        merged_seeds = merge_seed_catalogs(
            load_default_seed_catalog(), fundamental_seeds
        )
        evidence_bundle = EvidenceBuilder(clock=self._clock).build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            seeds=merged_seeds,
        )

        payload = _model_dict(evidence_bundle)
        payload.update(
            {
                "schema_version": "1.1.0",
                "inferences": [
                    _model_dict(item) for item in fundamental_seeds.inferences
                ],
                "missing_information": [
                    _model_dict(item)
                    for item in fundamental_seeds.missing_information
                ],
                "limitations": [
                    "企业基本面事实仅来自当前种子目录中已完成精确原文匹配的2025年年报内容。",
                    "本阶段未调用LLM，inferences为空；不得把人工归纳或常识自动写成公司事实。",
                    "missing_information表示当前证据链尚未确认，不等同于年报或企业绝对未披露。",
                    "本结果仅基于公开信息，未经企业尽调确认，不构成授信审批结论。",
                ],
            }
        )
        try:
            return FundamentalResearchBundle.parse_obj(payload)
        except ValidationError as exc:
            raise EvidenceBuildError(
                "invalid_fundamental_bundle", f"企业基本面数据校验失败：{exc}"
            ) from exc


def build_fundamental_artifacts(
    *,
    identity: CompanyIdentity,
    source: SourceRecord,
    download: DownloadResult,
    summary_path: Path,
    pages_path: Path,
    output_dir: Path,
    fundamentals_output: Optional[Path] = None,
    citations_output: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    seeds: Optional[FundamentalSeedCatalog] = None,
) -> FundamentalArtifacts:
    """构建 JSON/引用文件；失败时保留机器可读记录。"""

    builder = FundamentalsBuilder(clock=clock)
    writer = EvidenceArtifactWriter()
    failure_path = Path(output_dir) / identity.entity_id / "failure.json"
    failure_clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        bundle = builder.build(
            identity=identity,
            source=source,
            download=download,
            summary_path=summary_path,
            pages_path=pages_path,
            seeds=seeds,
        )
        default_output = (
            Path(output_dir) / identity.entity_id / "fundamentals.json"
        )
        artifacts = writer.write(
            bundle,
            output_dir,
            evidence_output=(
                Path(fundamentals_output)
                if fundamentals_output is not None
                else default_output
            ),
            citations_output=citations_output,
        )
        if failure_path.exists():
            failure_path.unlink()
        module_counts = Counter(
            fact.research_module.value for fact in bundle.facts
        )
        return FundamentalArtifacts(
            entity_id=identity.entity_id,
            fundamentals_path=artifacts.evidence_path,
            citations_path=artifacts.citations_path,
            fact_count=artifacts.fact_count,
            evidence_count=artifacts.evidence_count,
            inference_count=len(bundle.inferences),
            missing_information_count=len(bundle.missing_information),
            module_fact_counts=dict(sorted(module_counts.items())),
            source_count=artifacts.source_count,
        )
    except EvidenceBuildError as error:
        writer.write_failure(
            failure_path,
            EvidenceBuildFailureRecord(
                entity_id=identity.entity_id,
                source_id=source.source_id,
                attempted_at=failure_clock(),
                error_code=error.code,
                error_message=str(error),
            ),
        )
        raise
