"""企业、来源、事实与证据的结构化数据模型。"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic.v1 import AnyHttpUrl, BaseModel, Field, root_validator, validator


class EntityScope(str, Enum):
    """研究主体/报表口径。"""

    CONSOLIDATED = "consolidated"
    PARENT = "parent"
    LISTED_COMPANY = "listed_company"


class SourceType(str, Enum):
    """V0.1 需要区分的来源类型。"""

    ANNUAL_REPORT = "annual_report"
    DISCLOSURE_PORTAL = "disclosure_portal"
    EXCHANGE_WEBSITE = "exchange_website"
    COMPANY_WEBSITE = "company_website"
    GOVERNMENT_WEBSITE = "government_website"
    INDUSTRY_ASSOCIATION = "industry_association"
    WEB_PAGE = "web_page"


class SourceStatus(str, Enum):
    """来源当前处理状态，防止把“已登记”误写成“已下载/已解析”。"""

    REGISTERED = "registered"
    DOWNLOADED = "downloaded"
    PARSED = "parsed"
    FAILED = "failed"


class FactCategory(str, Enum):
    IDENTITY = "identity"
    BUSINESS = "business"
    FINANCIAL = "financial"
    INDUSTRY = "industry"


class ResearchModule(str, Enum):
    """事实或信息缺口所属的研究模块。"""

    COMPANY_IDENTITY = "company_identity"
    DEVELOPMENT_HISTORY = "development_history"
    MAIN_BUSINESS = "main_business"
    OPERATING_MODEL = "operating_model"
    MAIN_PRODUCTS = "main_products"
    OWNERSHIP_STRUCTURE = "ownership_structure"
    FINANCIAL_ANALYSIS = "financial_analysis"


class StatementType(str, Enum):
    """区分原始披露、模型推断与缺失信息。"""

    DISCLOSED_FACT = "disclosed_fact"
    MODEL_INFERENCE = "model_inference"
    MISSING_INFO = "missing_info"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    SINGLE_SOURCE = "single_source"
    CONFLICTING = "conflicting"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class VerificationMethod(str, Enum):
    EXACT_TEXT_MATCH = "exact_text_match"
    SEARCH_SNIPPET = "search_snippet"


class CompanyIdentity(BaseModel):
    """经本地登记表唯一解析后的上市公司身份。"""

    entity_id: str
    legal_name: str
    english_name: Optional[str] = None
    short_name: str
    ticker: str
    security_code: str
    exchange: str
    country_of_incorporation: str
    entity_scope: EntityScope = EntityScope.CONSOLIDATED
    evidence_ids: List[str] = Field(default_factory=list)

    class Config:
        allow_mutation = False


class SourceRecord(BaseModel):
    """来源登记记录；它描述出处，不代表文件内容已经核验或解析。"""

    source_id: str
    entity_id: Optional[str] = None
    title: str
    source_type: SourceType
    publisher: str
    url: AnyHttpUrl
    published_at: Optional[date] = None
    report_period: Optional[str] = None
    trusted_tier: int = Field(ge=1, le=3)
    status: SourceStatus = SourceStatus.REGISTERED
    accessed_at: Optional[datetime] = None
    local_path: Optional[str] = None
    page_or_section: Optional[str] = None
    notes: Optional[str] = None

    @validator("source_id")
    def validate_source_id(cls, value: str) -> str:
        if len(value) != 3 or not value.startswith("S") or not value[1:].isdigit():
            raise ValueError("source_id 必须采用 S01 形式")
        return value

    class Config:
        allow_mutation = False


class EvidenceRecord(BaseModel):
    """把规范化事实定位到原始来源的具体页和行。"""

    evidence_id: str
    source_id: str
    source_sha256: str
    page_number: int = Field(ge=1)
    line_number: int = Field(ge=1)
    page_or_section: str
    quote: str = Field(min_length=1, max_length=500)
    status: EvidenceStatus
    verification_method: VerificationMethod

    @validator("evidence_id")
    def validate_evidence_id(cls, value: str) -> str:
        if len(value) != 4 or not value.startswith("E") or not value[1:].isdigit():
            raise ValueError("evidence_id 必须采用 E001 形式")
        return value


class FactRecord(BaseModel):
    """规范化事实；事实本身不保存长原文，只引用 EvidenceRecord。"""

    fact_id: str
    entity_id: str
    category: FactCategory
    research_module: ResearchModule = ResearchModule.COMPANY_IDENTITY
    statement_type: StatementType = StatementType.DISCLOSED_FACT
    field_name: str
    value: str
    claim: str
    entity_scope: EntityScope
    period: Optional[str] = None
    evidence_ids: List[str] = Field(min_items=1)

    @validator("fact_id")
    def validate_fact_id(cls, value: str) -> str:
        if len(value) != 4 or not value.startswith("F") or not value[1:].isdigit():
            raise ValueError("fact_id 必须采用 F001 形式")
        return value

    @validator("statement_type")
    def facts_must_be_disclosed(cls, value: StatementType) -> StatementType:
        if value is not StatementType.DISCLOSED_FACT:
            raise ValueError(
                "FactRecord 只保存年报披露事实或其他有原文证据的披露事实，"
                "推断和缺失信息必须使用独立模型"
            )
        return value


class ModelInference(BaseModel):
    """模型或分析人员基于事实形成的推断；不得混入 FactRecord。"""

    inference_id: str
    entity_id: str
    research_module: ResearchModule
    statement_type: StatementType = StatementType.MODEL_INFERENCE
    statement: str = Field(min_length=1)
    supporting_fact_ids: List[str] = Field(min_items=1)
    evidence_ids: List[str] = Field(min_items=1)
    confidence_note: Optional[str] = None

    @validator("inference_id")
    def validate_inference_id(cls, value: str) -> str:
        if len(value) != 4 or not value.startswith("I") or not value[1:].isdigit():
            raise ValueError("inference_id 必须采用 I001 形式")
        return value

    @validator("statement_type")
    def inference_type_is_fixed(cls, value: StatementType) -> StatementType:
        if value is not StatementType.MODEL_INFERENCE:
            raise ValueError("ModelInference.statement_type 必须为 model_inference")
        return value


class MissingInformation(BaseModel):
    """当前证据无法确认的事项；明确记录原因而不是补造答案。"""

    missing_id: str
    entity_id: str
    research_module: ResearchModule
    statement_type: StatementType = StatementType.MISSING_INFO
    topic: str = Field(min_length=1)
    description: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    related_fact_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)

    @validator("missing_id")
    def validate_missing_id(cls, value: str) -> str:
        if len(value) != 4 or not value.startswith("M") or not value[1:].isdigit():
            raise ValueError("missing_id 必须采用 M001 形式")
        return value

    @validator("statement_type")
    def missing_type_is_fixed(cls, value: StatementType) -> StatementType:
        if value is not StatementType.MISSING_INFO:
            raise ValueError("MissingInformation.statement_type 必须为 missing_info")
        return value


class EvidenceBundle(BaseModel):
    """V0.4 的可复现输出：主体、事实、证据和来源目录。"""

    schema_version: str = "0.4.0"
    identity: CompanyIdentity
    generated_at: datetime
    facts: List[FactRecord]
    evidence: List[EvidenceRecord]
    sources: List[SourceRecord]
    limitations: List[str] = Field(default_factory=list)

    @root_validator
    def validate_references(cls, values):
        facts = values.get("facts") or []
        evidence = values.get("evidence") or []
        sources = values.get("sources") or []
        identity = values.get("identity")

        evidence_ids = [item.evidence_id for item in evidence]
        source_ids = [item.source_id for item in sources]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id 不得重复")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id 不得重复")

        evidence_id_set = set(evidence_ids)
        source_id_set = set(source_ids)
        for item in evidence:
            if item.source_id not in source_id_set:
                raise ValueError(f"证据 {item.evidence_id} 引用了未登记来源")
        for fact in facts:
            missing = set(fact.evidence_ids) - evidence_id_set
            if missing:
                raise ValueError(f"事实 {fact.fact_id} 引用了不存在的证据：{sorted(missing)}")
        if identity is not None:
            missing = set(identity.evidence_ids) - evidence_id_set
            if missing:
                raise ValueError(f"主体引用了不存在的证据：{sorted(missing)}")
        return values

    class Config:
        allow_mutation = False


class FundamentalResearchBundle(EvidenceBundle):
    """V1.1 企业基本面输出，显式隔离事实、推断和缺失信息。"""

    schema_version: str = "1.1.0"
    inferences: List[ModelInference] = Field(default_factory=list)
    missing_information: List[MissingInformation] = Field(default_factory=list)

    @root_validator
    def validate_fundamental_references(cls, values):
        facts = values.get("facts") or []
        evidence = values.get("evidence") or []
        inferences = values.get("inferences") or []
        missing_information = values.get("missing_information") or []

        fact_ids = {item.fact_id for item in facts}
        evidence_ids = {item.evidence_id for item in evidence}

        inference_ids = [item.inference_id for item in inferences]
        missing_ids = [item.missing_id for item in missing_information]
        if len(inference_ids) != len(set(inference_ids)):
            raise ValueError("inference_id 不得重复")
        if len(missing_ids) != len(set(missing_ids)):
            raise ValueError("missing_id 不得重复")

        for item in inferences:
            missing_facts = set(item.supporting_fact_ids) - fact_ids
            missing_evidence = set(item.evidence_ids) - evidence_ids
            if missing_facts or missing_evidence:
                raise ValueError(
                    f"推断 {item.inference_id} 存在悬空引用："
                    f"facts={sorted(missing_facts)}, evidence={sorted(missing_evidence)}"
                )
        for item in missing_information:
            missing_facts = set(item.related_fact_ids) - fact_ids
            missing_evidence = set(item.evidence_ids) - evidence_ids
            if missing_facts or missing_evidence:
                raise ValueError(
                    f"缺失信息 {item.missing_id} 存在悬空引用："
                    f"facts={sorted(missing_facts)}, evidence={sorted(missing_evidence)}"
                )
        return values

    class Config:
        allow_mutation = False

