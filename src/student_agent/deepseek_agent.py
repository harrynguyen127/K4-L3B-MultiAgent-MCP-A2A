"""DeepSeek-backed reasoning used by every business agent.

The model never receives credentials and never calls MCP directly.  It reviews only
case-scoped input/evidence already fetched by the Python control plane and must return
JSON.  Server-issued evidence references remain outside the model boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2


class AgentModel(Protocol):
    async def plan(self, case: dict[str, Any]) -> tuple[str, ...]: ...

    async def review_facts(
        self, role: str, case: dict[str, Any], evidence: Any, candidate: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def review_policy(
        self, case: dict[str, Any], facts: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def verify(self, case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DeepSeekAgentModel:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: float = 60
    max_tokens: int = 4096

    async def _json(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        async with httpx2.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
        value = response.json()
        try:
            content = value["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek returned an invalid JSON response") from exc
        if not isinstance(result, dict):
            raise ValueError("DeepSeek response must be a JSON object")
        return result

    async def review_facts(
        self, role: str, case: dict[str, Any], evidence: Any, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._json(
            f"""You are the {role} in an ecommerce dispute multi-agent system.
Use only the supplied case and MCP evidence. Review the candidate facts, correct any
semantic error, and return JSON with exactly one key named facts. Do not invent IDs,
money, timestamps, or evidence references. Do not include reasoning or prose.""",
            {"case": case, "mcp_evidence": evidence, "candidate_facts": candidate},
        )
        facts = result.get("facts")
        if not isinstance(facts, dict):
            raise ValueError(f"DeepSeek {role} did not return facts")
        return facts

    async def plan(self, case: dict[str, Any]) -> tuple[str, ...]:
        result = await self._json(
            """You are the coordinator of an ecommerce dispute multi-agent system.
Select the minimum specialist agents needed to investigate the claims. Return JSON
only as {"agents": [names]}. Allowed names are order-agent, shipment-agent, and
payment-agent. Always include order-agent. Include shipment-agent for delivery claims
and payment-agent for payment, cancellation, unavailable-order, or refund claims.""",
            {"case": case},
        )
        agents = result.get("agents")
        allowed = {"order-agent", "shipment-agent", "payment-agent"}
        if (
            not isinstance(agents, list)
            or any(not isinstance(agent, str) or agent not in allowed for agent in agents)
            or "order-agent" not in agents
        ):
            raise ValueError("DeepSeek coordinator returned an invalid agent plan")
        return tuple(dict.fromkeys(agents))

    async def review_policy(
        self, case: dict[str, Any], facts: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._json(
            """You are the policy-agent in an ecommerce dispute multi-agent system.
Use only the supplied facts and policy evidence. Return JSON with exactly the keys
assessment, root_cause_analysis, financial_resolution, resolution_actions. Preserve
the candidate shape and allowed enum values. Never recommend more refund than the
supported refundable amount. Do not include reasoning or prose.""",
            {"case": case, "facts": facts, "candidate_decision": candidate},
        )
        required = {
            "assessment",
            "root_cause_analysis",
            "financial_resolution",
            "resolution_actions",
        }
        if set(result) != required:
            raise ValueError("DeepSeek policy-agent returned an invalid decision shape")
        return result

    async def verify(self, case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        result = await self._json(
            """You are the verifier agent. Review the proposed ecommerce dispute output
for semantic contradictions. Return JSON only as {"approved": boolean, "issues":
[short strings]}. Do not rewrite the output and do not include reasoning.""",
            {"case": case, "output": output},
        )
        if not isinstance(result.get("approved"), bool) or not isinstance(
            result.get("issues"), list
        ):
            raise ValueError("DeepSeek verifier returned an invalid review")
        return result
