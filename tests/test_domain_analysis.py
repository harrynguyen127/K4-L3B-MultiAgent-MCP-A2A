from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.domain_analysis import (
    analyze_payment,
    analyze_refund,
    reconcile_ledger,
    select_order,
)
from student_agent.trace import TraceWriter
from student_agent.verifier_agent import VerificationError, check_evidence_coverage
from student_agent.workflow import solve_case
from test_case_adapter import SCHEMAS, FakeGateway


@pytest.mark.parametrize(
    "issue,refund",
    [
        ("canceled_order_paid", 90),
        ("unavailable_order_paid", 90),
        ("late_delivery_seller", 10),
        ("late_delivery_logistics", 10),
        ("valid_split_payment", 0),
        ("payment_mismatch", 35),
        ("duplicate_charge", 90),
        ("refund_pending", 0),
        ("refund_failed", 90),
        ("unsupported_claim", 0),
    ],
)
def test_evidence_drives_all_issue_families(tmp_path: Path, issue: str, refund: float) -> None:
    gateway = FakeGateway()
    order = dict(
        order_id="order-1",
        order_status="delivered",
        order_purchase_timestamp="2018-01-01T09:00:00-03:00",
        order_delivered_carrier_date="2018-01-02T09:00:00-03:00",
        order_delivered_customer_date="2018-01-03T09:00:00-03:00",
        order_estimated_delivery_date="2018-01-04T09:00:00-03:00",
    )
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        order["order_status"] = "canceled" if issue.startswith("canceled") else "unavailable"
    if issue.startswith("late_delivery"):
        order["order_delivered_customer_date"] = "2018-01-08T09:00:00-03:00"
        if issue.endswith("seller"):
            order["order_delivered_carrier_date"] = "2018-01-06T09:00:00-03:00"
    gateway.overrides["get_order"] = order
    gateway.overrides["get_shipment_summary"] = dict(
        order_id="order-1",
        delivered_customer_at=order["order_delivered_customer_date"],
        events=[],
        shipping_limits=[dict(shipping_limit_at="2018-01-03T09:00:00-03:00", seller_id="seller-1")],
    )
    captures = (
        [45, 45]
        if issue == "valid_split_payment"
        else [90, 90]
        if issue == "duplicate_charge"
        else [35]
        if issue == "payment_mismatch"
        else [90]
    )
    events = [
        dict(
            event_type="captured",
            status="confirmed",
            amount_brl=a,
            event_at=f"2018-01-01T{10 + i}:00:00-03:00",
        )
        for i, a in enumerate(captures)
    ]
    if issue == "payment_mismatch":
        events.append(
            dict(
                event_type="reconciliation_mismatch",
                status="open",
                event_at="2018-01-01T12:00:00-03:00",
            )
        )
    gateway.overrides["get_payment_timeline"] = dict(order_id="order-1", events=events)
    if issue.startswith("refund_"):
        gateway.overrides["get_refund_timeline"] = dict(
            order_id="order-1",
            events=[
                dict(
                    event_type="refund_requested",
                    status="failed" if issue == "refund_failed" else "pending",
                    amount_brl=90,
                    event_at="2018-01-02T10:00:00-03:00",
                )
            ],
        )
    party = (
        "seller"
        if issue.endswith("seller")
        else "logistics_provider"
        if issue.endswith("logistics")
        else "customer"
        if issue in {"valid_split_payment", "unsupported_claim"}
        else "platform"
        if issue in {"canceled_order_paid", "unavailable_order_paid"}
        else "payment_provider"
    )
    action = (
        "document_no_action"
        if party == "customer"
        else "monitor_refund"
        if issue == "refund_pending"
        else "refund_freight"
        if issue.startswith("late_delivery")
        else "issue_refund"
    )
    gateway.overrides["get_policy"] = dict(
        policy_version="EC_POLICY_V2",
        rules={
            issue: dict(
                case_status="no_action"
                if party == "customer"
                else "needs_investigation"
                if issue == "refund_pending"
                else "action_required",
                recommended_action=action,
                refund_brl=refund,
                responsible_parties=[dict(party_type=party, party_id=None)],
            )
        },
    )
    case = dict(
        case_id="CASE_001",
        opened_at="2018-01-02T09:00:00-03:00",
        candidate_order_ids=["order-1", "wrong-order"],
        customer_unique_id_hint="customer-1",
        policy_version="EC_POLICY_V2",
        customer_request=dict(
            claimed_order_id="order-1",
            claims=[
                dict(claim_id="claim-1", topic=issue),
                dict(claim_id="claim-2", topic="requested_full_refund"),
            ],
        ),
    )
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(SCHEMAS))
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == refund
    assert output["entity_resolution"]["rejected_candidates"] == ["wrong-order"]
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert len(gateway.calls) <= 8


def test_snapshot_selection_and_payment_window_exclude_other_purchase() -> None:
    old = dict(
        order_id="order-1",
        order_purchase_timestamp="2018-01-01T09:00:00Z",
        order_status="delivered",
    )
    future = dict(
        order_id="order-1", order_purchase_timestamp="2018-05-01T09:00:00Z", order_status="canceled"
    )
    selected, end, conflicts = select_order(future, [old, future], "2018-01-15T09:00:00Z")
    assert selected == old
    assert conflicts[0]["selected_source"] == "get_customer_history"
    facts = dict(period_start=selected["order_purchase_timestamp"], period_end=end)
    data = {
        "events": [
            dict(
                event_at="2018-01-01T10:00:00Z",
                event_type="captured",
                status="confirmed",
                amount_brl=20,
            ),
            dict(
                event_at="2018-05-01T10:00:00Z",
                event_type="captured",
                status="confirmed",
                amount_brl=90,
            ),
        ]
    }
    assert analyze_payment(data, facts)["captured_total_brl"] == 20


def test_refund_status_transition_does_not_double_count_request() -> None:
    events = [
        dict(refund_id="refund-1", event_at=f"2018-01-0{i}T10:00:00Z", status=status, amount_brl=20)
        for i, status in [(2, "pending"), (3, "completed")]
    ]
    result = analyze_refund({"events": events}, {"period_start": "2018-01-01T00:00:00Z"})
    assert result["refunded_total_brl"] == 20
    assert result["refund_verdict"] == "refunded"


def test_refund_conclusion_requires_refund_domain_evidence() -> None:
    from test_coordinator_router import minimal_output

    output = minimal_output()
    output["assessment"]["primary_issue"] = "refund_failed"
    output["evidence_refs"] = ["order-ref", "policy-ref", "payment-ref"]
    domains = {"order-ref": "order", "policy-ref": "policy", "payment-ref": "payment"}
    with pytest.raises(VerificationError, match="refund"):
        check_evidence_coverage(output, domains)


def test_equal_timestamp_conflicting_identity_is_not_resolved() -> None:
    direct = dict(
        order_id="order-1",
        order_purchase_timestamp="2018-01-01T00:00:00Z",
        customer_id="a",
        order_status="delivered",
    )
    history = [{**direct, "customer_id": "b"}]
    selected, _, conflicts = select_order(direct, history, "2018-01-02T00:00:00Z")
    assert selected == {}
    assert conflicts[0]["selected_source"] is None


def test_alternative_ledger_versions_are_not_all_counted_as_captures() -> None:
    facts = dict(
        order_total_brl=89,
        capture_amounts=[52, 44.5, 44.5],
        payment_rows=[
            dict(payment_sequential="1", payment_type="credit_card", payment_value=52),
            dict(payment_sequential="1", payment_type="credit_card", payment_value=44.5),
            dict(payment_sequential="2", payment_type="voucher", payment_value=44.5),
        ],
    )
    result = reconcile_ledger(facts)
    assert result["captured_total_brl"] == 89
    assert result["capture_count"] == 2
    assert result["payment_ambiguous"] is False
    assert result["payment_conflicts"][0]["resolution_code"] == "UNIQUE_LEDGER_RECONCILIATION"


def test_historical_ledger_rows_cannot_make_current_capture_ambiguous() -> None:
    facts = dict(
        order_total_brl=90,
        capture_amounts=[52],
        payment_rows=[
            dict(payment_sequential="1", payment_type="credit_card", payment_value=52),
            dict(payment_sequential="1", payment_type="credit_card", payment_value=45),
            dict(payment_sequential="2", payment_type="voucher", payment_value=45),
        ],
    )
    assert reconcile_ledger(facts) == {}


def test_multiple_matching_ledger_versions_remain_ambiguous() -> None:
    facts = dict(
        order_total_brl=90,
        capture_amounts=[30, 40, 50, 60],
        payment_rows=[
            dict(payment_sequential="1", payment_type="card", payment_value=30),
            dict(payment_sequential="1", payment_type="card", payment_value=40),
            dict(payment_sequential="2", payment_type="voucher", payment_value=50),
            dict(payment_sequential="2", payment_type="voucher", payment_value=60),
        ],
    )
    result = reconcile_ledger(facts)
    assert result["payment_ambiguous"] is True
    assert "captured_total_brl" not in result
