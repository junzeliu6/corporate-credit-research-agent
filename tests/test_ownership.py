"""V1.2 股权确定性内核的离线边界测试。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from credit_research_agent.models import (
    CompanyIdentity,
    EntityScope,
    EvidenceRecord,
    EvidenceStatus,
    MissingInformation,
    ResearchModule,
    SourceRecord,
    SourceStatus,
    SourceType,
    VerificationMethod,
)
from credit_research_agent.ownership import (
    ControlRelationship,
    ControlRelationType,
    OwnershipBuildError,
    OwnershipCompletenessDeclaration,
    OwnershipCompletenessStatus,
    OwnershipDataset,
    OwnershipDisplayConfig,
    OwnershipEdge,
    OwnershipNode,
    OwnershipNodeType,
    StateControlAssessment,
    StateControlClassification,
    aggregate_ownership_holders,
    build_ownership_graph,
    render_ownership_graph,
    validate_ownership_facts,
    validate_ownership_percentages,
)


AS_OF_DATE = date(2025, 12, 31)
TARGET_ID = "CN-A-SZSE-002603"


def _identity() -> CompanyIdentity:
    return CompanyIdentity(
        entity_id=TARGET_ID,
        legal_name="石家庄以岭药业股份有限公司",
        short_name="以岭药业",
        ticker="002603.SZ",
        security_code="002603",
        exchange="SZSE",
        country_of_incorporation="CN",
        entity_scope=EntityScope.CONSOLIDATED,
        evidence_ids=["E001"],
    )


def _source() -> SourceRecord:
    return SourceRecord(
        source_id="S01",
        entity_id=TARGET_ID,
        title="离线测试披露",
        source_type=SourceType.ANNUAL_REPORT,
        publisher="测试用官方披露",
        url="https://static.cninfo.com.cn/test.pdf",
        report_period="FY2025",
        trusted_tier=1,
        status=SourceStatus.PARSED,
    )


def _evidence() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="E001",
        source_id="S01",
        source_sha256="a" * 64,
        page_number=1,
        line_number=1,
        page_or_section="测试页",
        quote="用于离线规则测试的已核验证据",
        status=EvidenceStatus.VERIFIED,
        verification_method=VerificationMethod.EXACT_TEXT_MATCH,
    )


def _node(
    node_id: str,
    name: str,
    node_type: OwnershipNodeType = OwnershipNodeType.GENERAL_INSTITUTION,
) -> OwnershipNode:
    return OwnershipNode(
        node_id=node_id,
        name=name,
        node_type=node_type,
        source_ids=["S01"],
        evidence_ids=["E001"],
    )


def _edge(
    edge_id: str,
    source_node_id: str,
    target_node_id: str,
    percentage: str | None,
    *,
    status: EvidenceStatus = EvidenceStatus.VERIFIED,
    as_of_date: date = AS_OF_DATE,
) -> OwnershipEdge:
    return OwnershipEdge(
        edge_id=edge_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        percentage=percentage,
        as_of_date=as_of_date,
        status=status,
        source_ids=["S01"],
        evidence_ids=["E001"],
    )


def _dataset(
    nodes: list[OwnershipNode],
    edges: list[OwnershipEdge],
    *,
    controls: list[ControlRelationship] | None = None,
    assessments: list[StateControlAssessment] | None = None,
    declarations: list[OwnershipCompletenessDeclaration] | None = None,
) -> OwnershipDataset:
    target = _node(TARGET_ID, "以岭药业", OwnershipNodeType.TARGET_COMPANY)
    return OwnershipDataset(
        identity=_identity(),
        as_of_date=AS_OF_DATE,
        nodes=[target, *nodes],
        ownership_edges=edges,
        control_relationships=controls or [],
        state_control_assessments=assessments or [],
        completeness_declarations=declarations or [],
        sources=[_source()],
        evidence=[_evidence()],
    )


def _control(
    relationship_id: str,
    subjects: list[str],
    relation_type: ControlRelationType,
) -> ControlRelationship:
    return ControlRelationship(
        relationship_id=relationship_id,
        subject_node_ids=subjects,
        controlled_node_id=TARGET_ID,
        relation_type=relation_type,
        as_of_date=AS_OF_DATE,
        source_ids=["S01"],
        evidence_ids=["E001"],
    )


def test_natural_person_thresholds_are_strict() -> None:
    nodes = [
        _node("n2499", "自然人24.99", OwnershipNodeType.NATURAL_PERSON),
        _node("n2500", "自然人25", OwnershipNodeType.NATURAL_PERSON),
        _node("n2501", "自然人25.01", OwnershipNodeType.NATURAL_PERSON),
    ]
    edges = [
        _edge("e2499", "n2499", TARGET_ID, "24.99"),
        _edge("e2500", "n2500", TARGET_ID, "25"),
        _edge("e2501", "n2501", TARGET_ID, "25.01"),
    ]

    result = aggregate_ownership_holders(_dataset(nodes, edges))

    assert len(result.groups) == 1
    assert result.groups[0].member_node_ids == ["n2499", "n2500"]
    aggregate_edge = next(edge for edge in result.ownership_edges if edge.edge_id.startswith("AGG-EDGE"))
    assert str(aggregate_edge.percentage) == "49.99"
    assert any(node.node_id == "n2501" for node in result.nodes)


def test_institution_thresholds_are_strict() -> None:
    nodes = [
        _node("i499", "机构4.99"),
        _node("i500", "机构5"),
        _node("i501", "机构5.01"),
    ]
    edges = [
        _edge("e499", "i499", TARGET_ID, "4.99"),
        _edge("e500", "i500", TARGET_ID, "5"),
        _edge("e501", "i501", TARGET_ID, "5.01"),
    ]

    result = aggregate_ownership_holders(_dataset(nodes, edges))

    assert result.groups[0].member_node_ids == ["i499"]
    assert {node.node_id for node in result.nodes} >= {"i500", "i501"}


def test_key_control_subjects_bypass_aggregation_thresholds() -> None:
    natural = _node("person", "关键自然人", OwnershipNodeType.NATURAL_PERSON)
    institution = _node("holder", "控股机构")
    controls = [
        _control("c1", ["person"], ControlRelationType.ACTUAL_CONTROLLER),
        _control("c2", ["holder"], ControlRelationType.CONTROLLING_SHAREHOLDER),
    ]
    dataset = _dataset(
        [natural, institution],
        [
            _edge("e1", "person", TARGET_ID, "2"),
            _edge("e2", "holder", TARGET_ID, "4.99"),
        ],
        controls=controls,
    )

    result = aggregate_ownership_holders(dataset)

    assert not result.groups
    assert {node.node_id for node in result.nodes} >= {"person", "holder"}


def test_aggregation_never_fills_unknown_residual_to_100() -> None:
    nodes = [
        _node("a", "小股东A", OwnershipNodeType.NATURAL_PERSON),
        _node("b", "小股东B", OwnershipNodeType.NATURAL_PERSON),
    ]
    dataset = _dataset(
        nodes,
        [
            _edge("e1", "a", TARGET_ID, "10"),
            _edge("e2", "b", TARGET_ID, "5", status=EvidenceStatus.UNVERIFIED),
        ],
    )

    result = aggregate_ownership_holders(dataset)
    aggregate_edge = next(edge for edge in result.ownership_edges if edge.edge_id.startswith("AGG-EDGE"))

    assert aggregate_edge.percentage is None
    assert result.groups[0].percentage_calculated is False
    assert all("residual" not in node.node_id.lower() for node in result.nodes)


def test_graph_displays_five_levels_and_truncates_sixth() -> None:
    nodes = [_node(f"level{level}", f"第{level}层") for level in range(2, 7)]
    edges = [_edge("e2", "level2", TARGET_ID, "100")]
    edges.extend(
        _edge(f"e{level}", f"level{level}", f"level{level-1}", "100")
        for level in range(3, 7)
    )

    graph = build_ownership_graph(_dataset(nodes, edges))

    assert {node.level for node in graph.nodes} == {1, 2, 3, 4, 5}
    assert "level6" not in {node.node_id for node in graph.nodes}
    assert graph.truncated_node_ids == ["level5"]
    assert next(node for node in graph.nodes if node.node_id == "level5").is_truncated


def test_graph_can_stop_at_each_available_level_without_false_truncation() -> None:
    for highest_level in range(1, 6):
        nodes = [_node(f"l{level}", f"L{level}") for level in range(2, highest_level + 1)]
        edges = []
        if highest_level >= 2:
            edges.append(_edge("e2", "l2", TARGET_ID, "100"))
        edges.extend(
            _edge(f"e{level}", f"l{level}", f"l{level-1}", "100")
            for level in range(3, highest_level + 1)
        )
        graph = build_ownership_graph(
            _dataset(nodes, edges), require_complete=bool(edges)
        )
        assert max(node.level for node in graph.nodes) == highest_level
        assert not graph.truncated_node_ids


def test_validation_rejects_cycles() -> None:
    nodes = [_node("a", "A"), _node("b", "B")]
    dataset = _dataset(
        nodes,
        [
            _edge("e1", "a", TARGET_ID, "20"),
            _edge("e2", "b", "a", "20"),
            _edge("e3", "a", "b", "20"),
        ],
    )

    result = validate_ownership_facts(dataset)

    assert not result.is_valid
    assert "ownership_cycle" in {issue.code for issue in result.errors}


def test_validation_detects_duplicates_date_conflict_and_conflicting_percentages() -> None:
    duplicate_a = _node("a", "A")
    duplicate_b = _node("a", "A重复")
    dataset = _dataset(
        [duplicate_a, duplicate_b],
        [
            _edge("e1", "a", TARGET_ID, "10"),
            _edge("e2", "a", TARGET_ID, "11"),
            _edge("e3", "a", TARGET_ID, "10", as_of_date=date(2024, 12, 31)),
        ],
    )

    codes = {issue.code for issue in validate_ownership_facts(dataset).errors}

    assert {"duplicate_node", "conflicting_disclosure", "date_conflict"} <= codes


def test_missing_source_prevents_verified_graph_edge() -> None:
    node = _node("a", "A")
    edge = _edge("e1", "a", TARGET_ID, "10").copy(update={"source_ids": []})
    dataset = _dataset([node], [edge])

    validation = validate_ownership_facts(dataset)

    assert "source_missing" in {issue.code for issue in validation.errors}
    with pytest.raises(OwnershipBuildError) as exc_info:
        build_ownership_graph(dataset)
    assert exc_info.value.code == "invalid_ownership_facts"


def test_state_owned_shareholder_does_not_imply_state_control() -> None:
    state_holder = _node(
        "state-holder", "某国资平台", OwnershipNodeType.STATE_OWNED_PLATFORM
    )
    assessment = StateControlAssessment(
        entity_node_id=TARGET_ID,
        classification=StateControlClassification.UNKNOWN,
        as_of_date=AS_OF_DATE,
        note="仅发现国资参股，不能据此推导国资控制",
    )
    dataset = _dataset(
        [state_holder],
        [_edge("e1", "state-holder", TARGET_ID, "10")],
        assessments=[assessment],
    )

    graph = build_ownership_graph(dataset, require_complete=False)

    assert graph.state_control_assessments[0].classification is StateControlClassification.UNKNOWN
    assert not graph.control_relationships
    assert any("不能自动视为最终穿透终点" in warning for warning in graph.warnings)


def test_missing_information_cannot_reference_unknown_evidence() -> None:
    dataset = _dataset([], [])
    invalid = dataset.copy(
        update={
            "missing_information": [
                MissingInformation(
                    missing_id="M001",
                    entity_id=TARGET_ID,
                    research_module=ResearchModule.OWNERSHIP_STRUCTURE,
                    topic="待核实",
                    description="待核实描述",
                    reason="当前证据不足",
                    evidence_ids=["E999"],
                )
            ]
        }
    )

    result = validate_ownership_facts(invalid)

    assert "missing_information_evidence_missing" in {
        issue.code for issue in result.errors
    }


def test_graph_json_edges_keep_sources_and_match_nodes() -> None:
    node = _node("holder", "股东")
    graph = build_ownership_graph(_dataset([node], [_edge("e1", "holder", TARGET_ID, "100")]))
    payload = json.loads(graph.json())

    json_node_ids = {item["node_id"] for item in payload["nodes"]}
    model_node_ids = {item.node_id for item in graph.nodes}
    assert json_node_ids == model_node_ids
    assert all(edge.source_ids for edge in graph.edges)
    assert all(
        edge.source_node_id in json_node_ids and edge.target_node_id in json_node_ids
        for edge in graph.edges
    )


def test_svg_and_dot_are_rendered_from_graph(tmp_path: Path) -> None:
    node = _node("holder", "股东")
    graph = build_ownership_graph(_dataset([node], [_edge("e1", "holder", TARGET_ID, "100")]))

    result = render_ownership_graph(graph, tmp_path / "ownership.svg")

    assert result.svg_path.exists()
    assert result.png_path.exists()
    assert result.dot_path.exists()
    assert result.engine in {"graphviz-dot", "internal-tree-fallback"}
    assert "以岭药业" in result.svg_path.read_text(encoding="utf-8")
    dot = result.dot_path.read_text(encoding="utf-8")
    assert TARGET_ID in dot and "holder" in dot and "100.00%" in dot


def test_svg_keeps_control_relationship_separate_from_equity(tmp_path: Path) -> None:
    node = _node("controller", "控制人", OwnershipNodeType.NATURAL_PERSON)
    dataset = _dataset(
        [node],
        [_edge("e1", "controller", TARGET_ID, "100")],
        controls=[_control("c1", ["controller"], ControlRelationType.ACTUAL_CONTROLLER)],
    )
    graph = build_ownership_graph(dataset)

    result = render_ownership_graph(graph, tmp_path / "control.svg")
    svg = result.svg_path.read_text(encoding="utf-8")

    assert "控制人" in svg
    assert "100.00%" in svg
    for forbidden in ("已核实", "[S01]", "实际控制人", "一致行动人"):
        assert forbidden not in svg
    assert "stroke-dasharray" not in svg
    dot = result.dot_path.read_text(encoding="utf-8")
    assert "style=solid" in dot
    assert "dir=back" in dot


def test_display_configuration_never_allows_more_than_five_levels() -> None:
    with pytest.raises(ValueError):
        OwnershipDisplayConfig(max_display_levels=6)


def test_graphviz_requirement_returns_explicit_error_when_dot_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    node = _node("holder", "股东")
    graph = build_ownership_graph(_dataset([node], [_edge("e1", "holder", TARGET_ID, "100")]))
    monkeypatch.setattr("credit_research_agent.ownership.shutil.which", lambda _: None)

    with pytest.raises(OwnershipBuildError) as exc_info:
        render_ownership_graph(
            graph, tmp_path / "ownership.svg", require_graphviz=True
        )

    assert exc_info.value.code == "graphviz_not_available"
    assert "PATH" in str(exc_info.value)


def test_standard_two_level_penetration_validates_each_investee_to_100() -> None:
    nodes = [_node(node_id, node_id) for node_id in ("A", "B", "C", "D", "E")]
    dataset = _dataset(
        nodes,
        [
            _edge("e-a", "A", TARGET_ID, "40"),
            _edge("e-b", "B", TARGET_ID, "60"),
            _edge("e-c", "C", "B", "80"),
            _edge("e-d", "D", "B", "15"),
            _edge("e-e", "E", "B", "5"),
        ],
    )

    validation = validate_ownership_percentages(dataset, strict=True)
    by_target = {branch.target_node_id: branch for branch in validation.branches}

    assert validation.is_complete
    assert by_target[TARGET_ID].known_total == Decimal("100")
    assert by_target["B"].known_total == Decimal("100")
    assert all(
        branch.status is OwnershipCompletenessStatus.COMPLETE
        for branch in validation.branches
    )


def test_percentage_below_100_stays_incomplete_without_supported_residual() -> None:
    nodes = [_node("A", "A"), _node("B", "B")]
    dataset = _dataset(
        nodes,
        [_edge("e-a", "A", TARGET_ID, "40"), _edge("e-b", "B", TARGET_ID, "45")],
    )

    validation = validate_ownership_percentages(dataset)

    assert validation.branches[0].status is OwnershipCompletenessStatus.INCOMPLETE
    assert validation.branches[0].known_total == Decimal("85")
    assert validation.branches[0].calculated_residual is None
    with pytest.raises(OwnershipBuildError) as exc_info:
        build_ownership_graph(dataset)
    assert exc_info.value.code == "percentage_total_incomplete"


def test_percentage_above_100_prevents_formal_graph() -> None:
    nodes = [_node("A", "A"), _node("B", "B")]
    dataset = _dataset(
        nodes,
        [_edge("e-a", "A", TARGET_ID, "60"), _edge("e-b", "B", TARGET_ID, "45")],
    )

    validation = validate_ownership_percentages(dataset)

    assert validation.branches[0].status is OwnershipCompletenessStatus.OVER_ALLOCATED
    assert validation.branches[0].known_total == Decimal("105")
    with pytest.raises(OwnershipBuildError) as exc_info:
        build_ownership_graph(dataset)
    assert exc_info.value.code == "percentage_total_exceeds_100"


def test_small_holder_aggregation_keeps_raw_facts_and_total_100() -> None:
    nodes = [
        _node("major", "主要股东"),
        _node("other", "另一主要股东"),
        _node("small-1", "小股东1"),
        _node("small-2", "小股东2"),
        _node("small-3", "小股东3"),
    ]
    dataset = _dataset(
        nodes,
        [
            _edge("e-major", "major", TARGET_ID, "60"),
            _edge("e-other", "other", TARGET_ID, "31"),
            _edge("e-small-1", "small-1", TARGET_ID, "2"),
            _edge("e-small-2", "small-2", TARGET_ID, "3"),
            _edge("e-small-3", "small-3", TARGET_ID, "4"),
        ],
    )

    original_edge_ids = {edge.edge_id for edge in dataset.ownership_edges}
    graph = build_ownership_graph(dataset)
    group = graph.aggregation_groups[0]
    aggregate_edge = next(edge for edge in graph.edges if edge.synthetic)

    assert {edge.edge_id for edge in dataset.ownership_edges} == original_edge_ids
    assert len(dataset.ownership_edges) == 5
    assert group.member_node_ids == ["small-1", "small-2", "small-3"]
    assert aggregate_edge.percentage == Decimal("9")
    assert sum(edge.percentage or Decimal("0") for edge in graph.edges) == Decimal("100")


def test_important_natural_person_below_25_remains_visible() -> None:
    person = _node("person", "重要自然人", OwnershipNodeType.NATURAL_PERSON)
    other = _node("other", "其他主要股东")
    dataset = _dataset(
        [person, other],
        [_edge("e-person", "person", TARGET_ID, "10"), _edge("e-other", "other", TARGET_ID, "90")],
        controls=[_control("c1", ["person"], ControlRelationType.ACTUAL_CONTROLLER)],
    )

    graph = build_ownership_graph(dataset)

    assert "person" in {node.node_id for node in graph.nodes}
    assert not any("person" in group.member_node_ids for group in graph.aggregation_groups)
