from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from student_agent.cases import CaseSet
from student_agent.cli import _archive_artifacts, _resume_state
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import _task_deadline_ms
from test_coordinator_router import minimal_output

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def test_resume_preserves_completed_prefix_and_retries_partial_case(tmp_path: Path) -> None:
    contracts = Contracts(SCHEMAS)
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    case_set = CaseSet("test-v1", "l3b", ("CASE_001", "CASE_002"), {})
    output_path = tmp_path / "outputs" / "CASE_001.json"
    output_path.parent.mkdir()
    output_path.write_text(json.dumps(minimal_output()), encoding="utf-8")
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.emit(
        case_id="CASE_001",
        event_type="verification_completed",
        actor="verifier",
        decision_code="PASS",
    )
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    trace.emit(case_id="CASE_002", event_type="case_received", actor="coordinator")
    trace.emit(
        case_id="CASE_002",
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )
    trace.emit(
        case_id="CASE_002",
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="TASK_TIMEOUT",
    )
    before = trace.path.read_text(encoding="utf-8")

    completed, started = _resume_state(tmp_path, case_set, contracts, trace)

    assert completed == {"CASE_001"}
    assert started == {"CASE_001", "CASE_002"}
    assert trace.path.read_text(encoding="utf-8") == before
    assert output_path.exists()


def test_task_deadline_covers_scoped_retries() -> None:
    evidence = SimpleNamespace(timeout_seconds=30, retry_delay_seconds=0.25)

    assert _task_deadline_ms(evidence, 1) > 60_000
    assert _task_deadline_ms(evidence, 2) > 120_000


def test_fresh_run_archives_existing_artifacts_without_deleting_them(tmp_path: Path) -> None:
    originals = {
        "outputs/CASE_001.json": b"output",
        "traces/trace.jsonl": b"trace",
        "dist/submission.zip": b"zip",
    }
    for name, content in originals.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    backup = _archive_artifacts(tmp_path)
    for name, content in originals.items():
        assert (backup / name).read_bytes() == content
        assert not (tmp_path / name).exists()
