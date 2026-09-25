from __future__ import annotations

import json
from pathlib import Path

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.policy_agent import calibrate_confidence, decide_policy
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.verifier_agent import VerificationError, verify_output
from test_coordinator_router import REF, minimal_output

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def test_policy_canceled_paid_computes_remaining_refund() -> None:
    decision = decide_policy(
        {
            "order_id": "order-1",
            "order_status": "canceled",
            "captured_total_brl": "125.40",
            "refunded_total_brl": "25.40",
            "entity_confidence": 0.96,
            "evidence_coverage": 0.92,
            "analysis_complete": True,
        }
    )
    assert decision["assessment"] == {
        "primary_issue": "canceled_order_paid",
        "secondary_issues": [],
        "case_status": "action_required",
        "confidence": 0.92,
    }
    assert decision["financial_resolution"]["recommended_refund_brl"] == 100.0
    assert decision["financial_resolution"]["refund_lines"] == [
        {
            "reason_code": "CANCELED_ORDER_PAID",
            "amount_brl": 100.0,
            "entity_id": "order-1",
        }
    ]
    assert decision["resolution_actions"] == ["issue_refund"]


def test_policy_seller_delay_never_assigns_logistics() -> None:
    decision = decide_policy(
        {
            "shipment_verdict": "seller_delay",
            "late_seller_ids": ["seller-1"],
            "entity_confidence": 0.9,
            "evidence_coverage": 0.9,
            "analysis_complete": True,
        }
    )
    assert decision["assessment"]["primary_issue"] == "late_delivery_seller"
    assert decision["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-1"}
    ]
    assert decision["financial_resolution"]["recommended_refund_brl"] == 0.0


def test_confidence_caps_conflicts_and_incomplete_analysis() -> None:
    assert (
        calibrate_confidence(
            entity_confidence=1.0,
            evidence_coverage=1.0,
            has_unresolved_conflict=True,
        )
        == 0.5
    )
    assert (
        calibrate_confidence(
            entity_confidence=1.0,
            evidence_coverage=1.0,
            has_resolved_conflict=True,
            analysis_complete=False,
        )
        == 0.75
    )


def test_verifier_rejects_seller_issue_with_logistics_responsibility(tmp_path: Path) -> None:
    contracts = Contracts(SCHEMAS)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = minimal_output()
    output["assessment"].update(
        {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 0.7,
        }
    )
    output["entity_resolution"].update(
        {"status": "resolved", "resolved_order_ids": ["order-1"], "confidence": 0.9}
    )
    output["shipment_analysis"].update({"verdict": "seller_delay", "timeline_complete": True})
    output["root_cause_analysis"] = {
        "ranked_causes": [{"cause_code": "SELLER_DELAY", "rank": 1}],
        "responsible_parties": [{"party_type": "logistics_provider", "party_id": "carrier-1"}],
    }
    output["resolution_actions"] = ["review_seller_fulfillment"]
    output["evidence_refs"] = [REF]

    with pytest.raises(VerificationError, match="responsible party conflicts"):
        verify_output(
            output,
            case_id="CASE_001",
            consumed_refs=frozenset({REF}),
            contracts=contracts,
            trace=trace,
        )
    event = json.loads(trace.path.read_text(encoding="utf-8"))
    assert event["decision_code"] == "FAIL"


def test_artifact_validation_requires_full_lifecycle(tmp_path: Path) -> None:
    contracts = Contracts(SCHEMAS)
    output = minimal_output()
    output["evidence_refs"] = [REF]
    output_path = tmp_path / "outputs" / "CASE_001.json"
    output_path.parent.mkdir()
    output_path.write_text(json.dumps(output), encoding="utf-8")
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.emit(case_id="CASE_001", event_type="task_assigned", actor="coordinator")
    trace.emit(
        case_id="CASE_001",
        event_type="tool_result_consumed",
        actor="entity-agent",
        tool_name="get_order",
        evidence_refs=[REF],
    )
    trace.emit(case_id="CASE_001", event_type="handoff", actor="entity-agent")
    trace.emit(
        case_id="CASE_001",
        event_type="verification_completed",
        actor="verifier",
        decision_code="PASS",
    )
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    case_set = CaseSet("test-v1", "l3b", ("CASE_001",), {})

    with pytest.raises(ValueError, match="policy_decided"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_server_policy_refund_is_capped_by_captured_balance() -> None:
    facts = dict(
        order_status="canceled",
        captured_total_brl=90,
        refunded_total_brl=20,
        policy_rules={
            "canceled_order_paid": dict(
                refund_brl=999,
                recommended_action="issue_refund",
                case_status="action_required",
                responsible_parties=[dict(party_type="platform", party_id=None)],
            )
        },
    )
    decision = decide_policy(facts)
    assert decision["financial_resolution"]["recommended_refund_brl"] == 70


def test_readonly_verifier_rejects_tampering_after_output_was_generated() -> None:
    output = minimal_output()
    output["financial_resolution"]["recommended_refund_brl"] = 10
    with pytest.raises(VerificationError, match="refund lines do not sum"):
        verify_output(
            output,
            case_id="CASE_001",
            consumed_refs=frozenset(),
            contracts=Contracts(SCHEMAS),
            trace=None,
        )
