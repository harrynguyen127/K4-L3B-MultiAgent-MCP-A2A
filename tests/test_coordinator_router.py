from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.coordinator_router import TaskMessage, TaskResult
from student_agent.mcp_evidence_collector import ScopedEvidence, ToolSpec
from student_agent.specialist_agents import SpecialistAgent
from student_agent.trace import TraceWriter
from student_agent.verifier_agent import VerificationError, verify_output
from student_agent.workflow import run_case_with_handlers

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
REF = "ev_" + "a" * 20


class FakeGateway:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise TimeoutError("simulated timeout")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": REF,
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": {"order_id": arguments["order_id"], "case_id": case_id},
        }


class SharedOrderGateway(FakeGateway):
    """One server result may be read by two agents but each use needs its own trace."""


def runtime(tmp_path: Path, gateway: FakeGateway) -> tuple[ScopedEvidence, TraceWriter]:
    contracts = Contracts(SCHEMAS)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    evidence = ScopedEvidence(
        case_id="CASE_001",
        gateway=gateway,
        contracts=contracts,
        trace=trace,
        catalog={
            "get_order": ToolSpec("order", frozenset({"order_id"}), frozenset({"entity-agent"}))
        },
        discovered_tools={"get_order"},
        retry_delay_seconds=0,
    )
    return evidence, trace


def minimal_output() -> dict[str, Any]:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "CASE_001",
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.4,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.4,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def test_permission_retry_cache_and_consumption(tmp_path: Path) -> None:
    gateway = FakeGateway(fail_once=True)
    evidence, trace = runtime(tmp_path, gateway)

    async def exercise() -> None:
        with pytest.raises(PermissionError):
            await evidence.call("payment-agent", "get_order", order_id="order-1")
        with pytest.raises(PermissionError):
            await evidence.call("entity-agent", "unknown", order_id="order-1")
        first = await evidence.call("entity-agent", "get_order", order_id="order-1")
        second = await evidence.call("entity-agent", "get_order", order_id="order-1")
        assert first == second
        assert first is not second
        assert gateway.calls == evidence.attempted_calls == 2
        first["evidence_ref"] = "ev_" + "x" * 20
        evidence.consume("entity-agent", "get_order", REF)

    asyncio.run(exercise())
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["tool_result_consumed"]
    assert evidence.consumed_refs == {REF}
    assert evidence.consumed_by("entity-agent") == {REF}


def test_consumption_is_bound_to_actor_that_obtained_evidence(tmp_path: Path) -> None:
    contracts = Contracts(SCHEMAS)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    evidence = ScopedEvidence(
        case_id="CASE_001",
        gateway=SharedOrderGateway(),
        contracts=contracts,
        trace=trace,
        catalog={
            "get_order": ToolSpec(
                "order",
                frozenset({"order_id"}),
                frozenset({"entity-agent", "order-agent"}),
            )
        },
        discovered_tools={"get_order"},
    )

    async def exercise() -> None:
        result = await evidence.call("entity-agent", "get_order", order_id="order-1")
        with pytest.raises(ValueError, match="not obtained"):
            evidence.consume("order-agent", "get_order", result["evidence_ref"])
        await evidence.call("order-agent", "get_order", order_id="order-1")
        evidence.consume("entity-agent", "get_order", REF)
        evidence.consume("order-agent", "get_order", REF)

    asyncio.run(exercise())
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [event["actor"] for event in events] == ["entity-agent", "order-agent"]
    assert all(event["evidence_refs"] == [REF] for event in events)


def test_specialist_fetch_and_use_preserves_server_reference(tmp_path: Path) -> None:
    evidence, trace = runtime(tmp_path, FakeGateway())

    async def exercise() -> None:
        agent = SpecialistAgent("entity-agent", evidence)
        observation = await agent.fetch("get_order", order_id="order-1")
        data = observation.data
        data["order_id"] = "locally-mutated"
        assert observation.evidence_ref == REF
        assert agent.use(observation)["order_id"] == "order-1"

    asyncio.run(exercise())
    event = json.loads(trace.path.read_text(encoding="utf-8"))
    assert event["event_type"] == "tool_result_consumed"
    assert event["actor"] == "entity-agent"
    assert event["tool_name"] == "get_order"
    assert event["evidence_refs"] == [REF]


def test_a2a_route_with_unresolved_entity(tmp_path: Path) -> None:
    gateway = FakeGateway()
    evidence, trace = runtime(tmp_path, gateway)
    seen: list[TaskMessage] = []

    async def entity_handler(message: TaskMessage) -> TaskResult:
        seen.append(message)
        result = await evidence.call("entity-agent", "get_order", order_id="missing-order")
        evidence.consume("entity-agent", "get_order", result["evidence_ref"])
        return TaskResult(
            message.case_id,
            message.message_id,
            "entity-agent",
            "completed",
            {"status": "not_found"},
            (REF,),
        )

    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")

    def unresolved_output(_case: dict[str, Any], _results: dict[str, TaskResult]) -> dict[str, Any]:
        output = minimal_output()
        output["evidence_refs"] = [REF]
        return output

    output = asyncio.run(
        run_case_with_handlers(
            {"case_id": "CASE_001"},
            handlers={"entity-agent": entity_handler},
            build_output=unresolved_output,
            evidence=evidence,
            contracts=Contracts(SCHEMAS),
            trace=trace,
        )
    )
    assert output["case_id"] == "CASE_001"
    assert len(seen) == 1
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    ]


def test_a2a_route_runs_three_specialists_then_policy(tmp_path: Path) -> None:
    evidence, trace = runtime(tmp_path, FakeGateway())
    visited: list[str] = []

    async def handler(message: TaskMessage) -> TaskResult:
        visited.append(message.target)
        if message.target == "entity-agent":
            result = await evidence.call("entity-agent", "get_order", order_id="order-1")
            evidence.consume("entity-agent", "get_order", result["evidence_ref"])
            return TaskResult(
                message.case_id,
                message.message_id,
                message.target,
                "completed",
                {"status": "resolved"},
                (REF,),
            )
        return TaskResult(message.case_id, message.message_id, message.target, "completed")

    def build_output(_case: dict[str, Any], _results: dict[str, TaskResult]) -> dict[str, Any]:
        assert _results["policy"].facts["assessment"]["primary_issue"] == (
            "insufficient_evidence"
        )
        output = minimal_output()
        output["entity_resolution"].update(
            {"status": "resolved", "resolved_order_ids": ["order-1"], "confidence": 0.9}
        )
        output["affected_entities"]["order_ids"] = ["order-1"]
        output["evidence_refs"] = [REF]
        return output

    handlers = {
        actor: handler
        for actor in (
            "entity-agent",
            "order-agent",
            "shipment-agent",
            "payment-agent",
            "policy-agent",
        )
    }
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    asyncio.run(
        run_case_with_handlers(
            {"case_id": "CASE_001"},
            handlers=handlers,
            build_output=build_output,
            evidence=evidence,
            contracts=Contracts(SCHEMAS),
            trace=trace,
        )
    )
    assert visited[0] == "entity-agent"
    assert set(visited[1:4]) == {"order-agent", "shipment-agent", "payment-agent"}
    assert visited[-1] == "policy-agent"


def test_verifier_rejects_unconsumed_evidence(tmp_path: Path) -> None:
    _evidence, trace = runtime(tmp_path, FakeGateway())
    output = minimal_output()
    output["evidence_refs"] = [REF]
    with pytest.raises(VerificationError, match="not consumed"):
        verify_output(
            output,
            case_id="CASE_001",
            consumed_refs=frozenset(),
            contracts=Contracts(SCHEMAS),
            trace=trace,
        )
