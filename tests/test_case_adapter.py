from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.case_adapter import CATALOG
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import DiscoveredTool
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict[str, Any] = {}

    async def describe_tools(self) -> list[DiscoveredTool]:
        return [
            DiscoveredTool(
                name,
                {
                    "required": sorted(spec.required_arguments | {"case_id"}),
                    "type": "object",
                },
            )
            for name, spec in CATALOG.items()
        ]

    async def call(self, name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((name, case_id))
        data: dict[str, Any] = {
            "get_order": {
                "order_id": "order-1",
                "order_status": "delivered",
                "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
                "order_delivered_carrier_date": "2018-01-02T09:00:00-03:00",
                "order_delivered_customer_date": "2018-01-03T09:00:00-03:00",
                "order_estimated_delivery_date": "2018-01-04T09:00:00-03:00",
            },
            "get_customer_history": {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": "order-1"}],
            },
            "get_order_items": [
                {
                    "order_id": "order-1",
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "price": "80.00",
                    "freight_value": "10.00",
                }
            ],
            "get_shipment_summary": {
                "order_id": "order-1",
                "delivered_carrier_at": "2018-01-02T09:00:00-03:00",
                "delivered_customer_at": "2018-01-03T09:00:00-03:00",
                "estimated_delivery_at": "2018-01-04T09:00:00-03:00",
                "events": [],
            },
            "get_payment_timeline": {
                "order_id": "order-1",
                "events": [
                    {
                        "event_type": "captured",
                        "status": "confirmed",
                        "amount_brl": "90.00",
                        "event_at": "2018-01-01T10:00:00-03:00",
                    }
                ],
            },
            "get_policy": {"policy_version": "EC_POLICY_V2", "rules": {}},
            "get_refund_timeline": {"order_id": "order-1", "events": []},
            "get_product_context": [{"order_item_id": "item-1", "product_id": "product-1"}],
        }[name]
        data = self.overrides.get(name, data)
        index = list(CATALOG).index(name)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{index:020d}",
            "result_hash": "sha256:" + f"{index:064x}",
            "domain": CATALOG[name].domain,
            "data": data,
        }


def test_solve_case_runs_reviewed_route_with_real_evidence_refs(tmp_path: Path) -> None:
    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "late_delivery_logistics"}],
        },
        "candidate_order_ids": ["order-1", "candidate-1"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
    }
    contracts = Contracts(SCHEMAS)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway()
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")

    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]

    assert output["entity_resolution"]["status"] == "resolved"
    assert output["shipment_analysis"]["verdict"] == "on_time"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert len(output["evidence_refs"]) == 6
    assert len(gateway.calls) == 6
    contracts.validate_output(output, "adapter output")
