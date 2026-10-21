"""企业授信研究Agent核心包。"""

from credit_research_agent.company import CompanyResolver
from credit_research_agent.evidence import EvidenceBuilder
from credit_research_agent.models import (
    CompanyIdentity,
    EvidenceBundle,
    EvidenceRecord,
    FactRecord,
    SourceRecord,
)
from credit_research_agent.sources import SourceCatalog

__all__ = [
    "CompanyIdentity",
    "CompanyResolver",
    "EvidenceBuilder",
    "EvidenceBundle",
    "EvidenceRecord",
    "FactRecord",
    "SourceCatalog",
    "SourceRecord",
]

__version__ = "0.4.0"

