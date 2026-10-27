"""V3.3 A股非金融企业适用范围的保守判定。

本模块不根据公司名称猜测行业。权威证券主数据中的行业字段优先；行业字段
缺失时，只读取年报的主营业务章节和已定位的合并主表，并要求“业务特征 +
金融机构特有报表科目”同时命中后才排除银行、证券或保险企业。

判定门禁的目的只是选择当前通用财务解析器是否适用，不负责撰写行业分析。
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from pydantic.v1 import BaseModel, Field


class CompanyScopeStatus(str, Enum):
    """当前通用研究管线对企业的适用状态。"""

    SUPPORTED_NON_FINANCIAL = "supported_non_financial"
    UNSUPPORTED_FINANCIAL_ENTERPRISE = "unsupported_financial_enterprise"
    ENTERPRISE_SCOPE_UNCONFIRMED = "enterprise_scope_unconfirmed"
    INDUSTRY_EVIDENCE_CONFLICT = "industry_evidence_conflict"


class FinancialEnterpriseType(str, Enum):
    BANK = "bank"
    SECURITIES = "securities"
    INSURANCE = "insurance"


class IndustryMetadata(BaseModel):
    """仅接收已由主数据 Provider 核实的行业元数据。"""

    industry_code: Optional[str] = None
    industry_name: Optional[str] = None
    industry_standard: Optional[str] = None
    verified: bool = False
    source_url: Optional[str] = None


class ScopeSignal(BaseModel):
    signal_type: str
    value: str
    page_number: Optional[int] = None
    excerpt: Optional[str] = None


class CompanyScopeAssessment(BaseModel):
    status: CompanyScopeStatus
    financial_enterprise_type: Optional[FinancialEnterpriseType] = None
    industry_metadata: Optional[IndustryMetadata] = None
    business_signals: List[ScopeSignal] = Field(default_factory=list)
    statement_signals: List[ScopeSignal] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    reviewed_business_pages: List[int] = Field(default_factory=list)
    reviewed_statement_pages: List[int] = Field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        return self.status is CompanyScopeStatus.SUPPORTED_NON_FINANCIAL


_BUSINESS_HEADINGS = (
    "公司从事的主要业务",
    "报告期内公司从事的主要业务",
    "主营业务",
    "主要业务",
    "经营模式",
)

_STATEMENT_HEADINGS = (
    "合并资产负债表",
    "合并利润表",
    "合并现金流量表",
)

_FINANCIAL_PATTERNS = {
    FinancialEnterpriseType.BANK: {
        "business": ("吸收存款", "发放贷款", "商业银行", "存贷款业务"),
        "statement": ("发放贷款和垫款", "吸收存款", "利息净收入"),
    },
    FinancialEnterpriseType.SECURITIES: {
        "business": ("证券经纪", "证券承销", "证券自营", "融资融券"),
        "statement": ("代理买卖证券款", "融出资金", "手续费及佣金净收入"),
    },
    FinancialEnterpriseType.INSURANCE: {
        "business": ("保险业务", "承保", "人身保险", "财产保险"),
        "statement": ("保险合同准备金", "已赚保费", "赔付支出"),
    },
}

_GENERIC_NON_FINANCIAL_STATEMENT_TERMS = (
    "营业收入",
    "营业成本",
    "存货",
    "应收账款",
)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _financial_industry(metadata: IndustryMetadata) -> bool:
    if not metadata.verified:
        return False
    code = _normalise(metadata.industry_code or "")
    name = _normalise(metadata.industry_name or "")
    if code == "J" or code.startswith("J-") or code.startswith("J/"):
        return True
    if re.match(r"^(66|67|68|69)(?:\D|$)", code):
        return True
    return any(
        term in name
        for term in ("金融业", "货币金融服务", "资本市场服务", "保险业")
    )


def _metadata_has_classification(metadata: Optional[IndustryMetadata]) -> bool:
    return bool(
        metadata
        and metadata.verified
        and (metadata.industry_code or metadata.industry_name)
    )


def _load_pages(pages_path: Path) -> List[Mapping[str, object]]:
    pages: List[Mapping[str, object]] = []
    with Path(pages_path).open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"pages.jsonl 第{line_number}行不是合法JSON"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"pages.jsonl 第{line_number}行必须为对象")
            pages.append(payload)
    return pages


def _page_number(page: Mapping[str, object]) -> int:
    value = page.get("page_number")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _page_text(page: Mapping[str, object]) -> str:
    value = page.get("text")
    return value if isinstance(value, str) else ""


def _select_pages(
    pages: Sequence[Mapping[str, object]], headings: Iterable[str]
) -> List[Mapping[str, object]]:
    selected: List[Mapping[str, object]] = []
    seen: set[int] = set()
    for page in pages:
        text = _page_text(page)
        if not any(heading in text for heading in headings):
            continue
        page_no = _page_number(page)
        if page_no in seen:
            continue
        selected.append(page)
        seen.add(page_no)
    return selected


def _find_signals(
    pages: Sequence[Mapping[str, object]],
    patterns: Iterable[str],
    *,
    signal_type: str,
) -> List[ScopeSignal]:
    matches: List[ScopeSignal] = []
    for pattern in patterns:
        for page in pages:
            text = _page_text(page)
            index = text.find(pattern)
            if index < 0:
                continue
            start = max(0, index - 45)
            end = min(len(text), index + len(pattern) + 70)
            matches.append(
                ScopeSignal(
                    signal_type=signal_type,
                    value=pattern,
                    page_number=_page_number(page) or None,
                    excerpt=" ".join(text[start:end].split()),
                )
            )
            break
    return matches


def assess_company_scope(
    *,
    pages_path: Path,
    industry_metadata: Optional[IndustryMetadata] = None,
) -> CompanyScopeAssessment:
    """判定当前通用非金融财务管线是否适用。

    附注页不会被全文扫描；只有包含指定主营业务标题的页面以及包含合并主表
    标题的页面会进入判定。这避免把被投资企业、联营企业或偶发交易中的金融
    词汇误认为上市公司自身主营业务。
    """

    pages = _load_pages(Path(pages_path))
    business_pages = _select_pages(pages, _BUSINESS_HEADINGS)
    statement_pages = _select_pages(pages, _STATEMENT_HEADINGS)

    detected: List[
        tuple[FinancialEnterpriseType, List[ScopeSignal], List[ScopeSignal]]
    ] = []
    for enterprise_type, patterns in _FINANCIAL_PATTERNS.items():
        business = _find_signals(
            business_pages,
            patterns["business"],
            signal_type=f"{enterprise_type.value}_business",
        )
        statement = _find_signals(
            statement_pages,
            patterns["statement"],
            signal_type=f"{enterprise_type.value}_statement",
        )
        # 两类证据必须同时存在；单个关键词不构成排除依据。
        if business and statement:
            detected.append((enterprise_type, business, statement))

    master_classified = _metadata_has_classification(industry_metadata)
    master_financial = bool(
        industry_metadata and _financial_industry(industry_metadata)
    )

    if len(detected) > 1:
        return CompanyScopeAssessment(
            status=CompanyScopeStatus.INDUSTRY_EVIDENCE_CONFLICT,
            industry_metadata=industry_metadata,
            business_signals=[item for _, values, _ in detected for item in values],
            statement_signals=[item for _, _, values in detected for item in values],
            reasons=["年报同时命中多类金融机构特征，需人工确认报表口径"],
            reviewed_business_pages=[_page_number(item) for item in business_pages],
            reviewed_statement_pages=[_page_number(item) for item in statement_pages],
        )

    if detected:
        enterprise_type, business_signals, statement_signals = detected[0]
        if master_classified and not master_financial:
            status = CompanyScopeStatus.INDUSTRY_EVIDENCE_CONFLICT
            reasons = ["权威主数据为非金融行业，但年报业务及主表呈现金融机构特征"]
        else:
            status = CompanyScopeStatus.UNSUPPORTED_FINANCIAL_ENTERPRISE
            reasons = [
                "主营业务章节与合并主表均命中金融机构特有特征，当前通用解析器不适用"
            ]
        return CompanyScopeAssessment(
            status=status,
            financial_enterprise_type=enterprise_type,
            industry_metadata=industry_metadata,
            business_signals=business_signals,
            statement_signals=statement_signals,
            reasons=reasons,
            reviewed_business_pages=[_page_number(item) for item in business_pages],
            reviewed_statement_pages=[_page_number(item) for item in statement_pages],
        )

    if master_financial:
        return CompanyScopeAssessment(
            status=CompanyScopeStatus.UNSUPPORTED_FINANCIAL_ENTERPRISE,
            industry_metadata=industry_metadata,
            reasons=["权威证券主数据将企业归入金融业，当前通用解析器不适用"],
            reviewed_business_pages=[_page_number(item) for item in business_pages],
            reviewed_statement_pages=[_page_number(item) for item in statement_pages],
        )

    generic_statement_signals = _find_signals(
        statement_pages,
        _GENERIC_NON_FINANCIAL_STATEMENT_TERMS,
        signal_type="generic_non_financial_statement",
    )
    if master_classified or (business_pages and len(generic_statement_signals) >= 2):
        reasons = []
        if master_classified:
            reasons.append("权威主数据行业分类未归入金融业")
        if business_pages and len(generic_statement_signals) >= 2:
            reasons.append("年报主营业务章节及通用企业合并主表均已定位")
        return CompanyScopeAssessment(
            status=CompanyScopeStatus.SUPPORTED_NON_FINANCIAL,
            industry_metadata=industry_metadata,
            statement_signals=generic_statement_signals,
            reasons=reasons,
            reviewed_business_pages=[_page_number(item) for item in business_pages],
            reviewed_statement_pages=[_page_number(item) for item in statement_pages],
        )

    return CompanyScopeAssessment(
        status=CompanyScopeStatus.ENTERPRISE_SCOPE_UNCONFIRMED,
        industry_metadata=industry_metadata,
        statement_signals=generic_statement_signals,
        reasons=[
            "缺少已核实行业分类，且未同时定位主营业务章节与足够的通用合并主表科目"
        ],
        reviewed_business_pages=[_page_number(item) for item in business_pages],
        reviewed_statement_pages=[_page_number(item) for item in statement_pages],
    )

