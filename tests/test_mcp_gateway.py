from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult, Tool

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
REF = "ev_" + "a" * 20


class FakeSession:
    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        self.calls.append((tool_name, arguments))
        return CallToolResult(content=[], structured_content=self.evidence)

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                Tool(name="get_shipment", input_schema={"type": "object"}),
                Tool(
                    name="get_order",
                    input_schema={
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                    },
                ),
            ]
        )


def test_gateway_describes_mcp_v2_tools() -> None:
    session = FakeSession({})
    gateway = EvidenceGateway(session, Contracts(SCHEMAS))  # type: ignore[arg-type]

    tools = asyncio.run(gateway.describe_tools())

    assert [tool.name for tool in tools] == ["get_order", "get_shipment"]
    assert tools[0].input_schema["properties"] == {
        "order_id": {"type": "string"}
    }


def test_gateway_forwards_exact_case_and_preserves_server_envelope() -> None:
    envelope = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": REF,
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }
    session = FakeSession(envelope)
    gateway = EvidenceGateway(session, Contracts(SCHEMAS))  # type: ignore[arg-type]

    result = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))

    assert session.calls == [
        ("get_order", {"case_id": "CASE_001", "order_id": "order-1"})
    ]
    assert result == envelope
    assert result["evidence_ref"] == REF


def test_gateway_rejects_invalid_case_before_mcp_call() -> None:
    session = FakeSession({})
    gateway = EvidenceGateway(session, Contracts(SCHEMAS))  # type: ignore[arg-type]

    async def exercise() -> None:
        with pytest.raises(ValueError, match="valid case_id"):
            await gateway.call("get_order", case_id="", order_id="order-1")

    asyncio.run(exercise())

    assert session.calls == []
