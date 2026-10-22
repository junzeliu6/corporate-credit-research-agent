"""统一事实引用接口的离线测试。"""

import json

from datetime import datetime, timezone
from pathlib import Path

from credit_research_agent.company import CompanyResolver
from credit_research_agent.grounding import (
    FactVerificationStatus,
    build_evidence_bundle_grounding_catalog,
    build_ownership_grounding_catalog,
)
from credit_research_agent.models import (
    EntityScope,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceStatus,
    FactCategory,
    FactRecord,
    ResearchModule,
    VerificationMethod,
)
from credit_research_agent.sources import SourceCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_fact_record_and_evidence_record_are_adapted_without_new_pipeline() -> None:
    identity = CompanyResolver.from_default_catalog().resolve("002603.SZ")
    source = SourceCatalog.from_default_catalog().get("S01")
    evidence = EvidenceRecord(
        evidence_id="E999",
        source_id="S01",
        source_sha256="a" * 64,
        page_number=97,
        line_number=1,
        page_or_section="股东情况",
        quote="示例原文",
        status=EvidenceStatus.VERIFIED,
        verification_method=VerificationMethod.EXACT_TEXT_MATCH,
    )
    fact = FactRecord(
        fact_id="F999",
        entity_id=identity.entity_id,
        category=FactCategory.BUSINESS,
        research_module=ResearchModule.OWNERSHIP_STRUCTURE,
        field_name="ownership_note",
        value="经核实披露",
        claim="经核实披露",
        entity_scope=EntityScope.LISTED_COMPANY,
        period="2025",
        evidence_ids=["E999"],
    )
    bundle = EvidenceBundle(
        identity=identity,
        generated_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        facts=[fact],
        evidence=[evidence],
        sources=[source],
    )

    catalog = build_evidence_bundle_grounding_catalog(bundle)
    adapted = catalog.facts[0]

    assert adapted.fact_id == "F999"
    assert adapted.company == identity.legal_name
    assert adapted.metric == "ownership_note"
    assert adapted.period == "2025"
    assert adapted.source_id == "S01"
    assert adapted.source_type == "annual_report"
    assert adapted.source_location.startswith("PDF第97页")
    assert adapted.verification_status is FactVerificationStatus.VERIFIED
    assert adapted.is_citable is True


def test_existing_ownership_json_exposes_disclosed_calculated_and_missing_items() -> None:
    graph_path = PROJECT_ROOT / "examples" / "ownership_graph.json"
    validation_path = PROJECT_ROOT / "examples" / "ownership_validation.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))

    catalog = build_ownership_grounding_catalog(graph, validation)
    by_id = {item.fact_id: item for item in catalog.facts}

    assert by_id["OE001"].value == "31.53"
    assert by_id["OE001"].verification_status.value == "verified"
    residual_id = (
        "RESIDUAL-EDGE::CN-A-SZSE-002603::total_ordinary_share_capital"
    )
    assert by_id[residual_id].value == "45.32"
    assert by_id[residual_id].verification_status.value == "calculated"
    assert by_id[residual_id].derived_from == ["OE001", "OE002", "OE003"]
    assert any(item.missing_id == "M101" for item in catalog.missing_information)
