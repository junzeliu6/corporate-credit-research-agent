"""企业授信研究Agent核心包。"""

from credit_research_agent.company import CompanyResolver
from credit_research_agent.evidence import EvidenceBuilder
from credit_research_agent.fundamentals import FundamentalsBuilder
from credit_research_agent.grounding import (
    CitableFact,
    FactGroundingCatalog,
    GroundedAnalysisClaim,
    GroundedMissingInformation,
    build_evidence_bundle_grounding_catalog,
    build_ownership_grounding_catalog,
)
from credit_research_agent.models import (
    CompanyIdentity,
    EvidenceBundle,
    EvidenceRecord,
    FactRecord,
    FundamentalResearchBundle,
    MissingInformation,
    ModelInference,
    ResearchModule,
    SourceRecord,
    StatementType,
)
from credit_research_agent.ownership import (
    OwnershipBuilder,
    OwnershipDataset,
    OwnershipGraph,
    build_ownership_graph,
    validate_ownership_facts,
)
from credit_research_agent.sources import SourceCatalog
from credit_research_agent.skill_runtime import (
    SkillExecutionTrace,
    SkillRuntime,
)
from credit_research_agent.skills import SkillLoader, SkillRegistry, SkillSelector

__all__ = [
    "CompanyIdentity",
    "CompanyResolver",
    "CitableFact",
    "EvidenceBuilder",
    "EvidenceBundle",
    "EvidenceRecord",
    "FactRecord",
    "FactGroundingCatalog",
    "FundamentalResearchBundle",
    "FundamentalsBuilder",
    "GroundedAnalysisClaim",
    "GroundedMissingInformation",
    "MissingInformation",
    "ModelInference",
    "OwnershipBuilder",
    "OwnershipDataset",
    "OwnershipGraph",
    "ResearchModule",
    "SkillExecutionTrace",
    "SkillLoader",
    "SkillRegistry",
    "SkillRuntime",
    "SkillSelector",
    "SourceCatalog",
    "SourceRecord",
    "StatementType",
    "build_ownership_graph",
    "build_evidence_bundle_grounding_catalog",
    "build_ownership_grounding_catalog",
    "validate_ownership_facts",
]

__version__ = "1.3.5"

