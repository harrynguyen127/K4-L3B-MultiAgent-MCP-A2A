from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


def _resume_state(
    root: Path, case_set: CaseSet, contracts: Contracts, trace: TraceWriter
) -> tuple[set[str], set[str]]:
    """Validate a completed prefix and retain an unfinished final case for retry."""
    output_root = root / "outputs"
    expected = set(case_set.case_ids)
    actual = {path.stem for path in output_root.glob("*.json")}
    if extra := actual - expected:
        raise ValueError(f"outputs contain cases outside case-set: {sorted(extra)}")
    events_by_case: dict[str, list[dict[str, object]]] = {
        case_id: [] for case_id in case_set.case_ids
    }
    if trace.path.exists():
        for number, line in enumerate(trace.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
            case_id = event["case_id"]
            if case_id not in expected:
                raise ValueError(f"trace line {number} belongs to an unknown case")
            events_by_case[case_id].append(event)
    elif actual:
        raise ValueError("outputs exist but trace is missing; cannot resume safely")

    completed: set[str] = set()
    started: set[str] = set()
    encountered_missing = False
    for index, case_id in enumerate(case_set.case_ids):
        events = events_by_case[case_id]
        types = [event["event_type"] for event in events]
        output_path = output_root / f"{case_id}.json"
        later_events = any(events_by_case[later] for later in case_set.case_ids[index + 1 :])
        if events:
            if types.count("case_received") != 1 or types[0] != "case_received":
                raise ValueError(f"trace for {case_id} has an invalid start")
            started.add(case_id)
        if output_path.exists():
            if encountered_missing:
                raise ValueError("outputs are not a contiguous case-set prefix")
            output = json.loads(output_path.read_text(encoding="utf-8"))
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
            if types.count("verification_completed") != 1 or not any(
                event["event_type"] == "verification_completed"
                and event.get("decision_code") == "PASS"
                for event in events
            ):
                raise ValueError(f"output for {case_id} has no passing verification trace")
            consumed = {
                ref
                for event in events
                if event["event_type"] == "tool_result_consumed"
                for ref in event.get("evidence_refs", [])
            }
            if not set(output["evidence_refs"]) <= consumed:
                raise ValueError(f"output for {case_id} cites unconsumed evidence")
            if not types.count("case_finalized"):
                if later_events:
                    raise ValueError(f"trace for {case_id} lacks finalization before later cases")
                # Recovery from a crash between atomic output replace and trace append.
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            elif types.count("case_finalized") != 1 or types[-1] != "case_finalized":
                raise ValueError(f"trace for {case_id} has an invalid finalization")
            completed.add(case_id)
        else:
            encountered_missing = True
            if "case_finalized" in types or "verification_completed" in types:
                raise ValueError(f"trace for {case_id} is finalized but output is missing")
            if events and later_events:
                raise ValueError("trace contains a case after an unfinished case")
    return completed, started


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _archive_artifacts(root: Path) -> Path:
    """Keep the old submission recoverable before an explicitly requested fresh run."""
    root = root.resolve()
    backup = root / "run-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    targets = list((root / "outputs").glob("*.json"))
    targets += [root / "traces" / "trace.jsonl", root / "dist" / "submission.zip"]
    for target in targets:
        if target.exists() and (target.is_symlink() or not target.resolve().is_relative_to(root)):
            raise ValueError("artifact path is outside the workspace")
    for target in targets:
        if target.is_file():
            destination = backup / target.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            target.replace(destination)
    return backup


async def _run(root: Path, *, fresh: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    if fresh:
        print(f"Previous artifacts archived at {_archive_artifacts(root)}", flush=True)
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(trace_path, contracts)
    completed, started = _resume_state(root, case_set, contracts, trace)
    if completed:
        print(f"Resuming: {len(completed)} completed cases retained")
    if len(completed) == len(case_set.case_ids):
        print(f"OK: all {len(completed)} cases already completed")
        return

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in case_set.case_ids:
            if case_id in completed:
                continue
            case = case_set.cases[case_id]
            if case_id not in started:
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            print(f"{case_id}: {output['assessment']['primary_issue']}", flush=True)


async def _rerun_case(root: Path, case_id: str) -> None:
    """Replace one completed case and its trace, retaining a recoverable backup."""
    cases = load_case_set(root)
    if case_id not in cases.case_ids:
        raise ValueError("case is outside case-set")
    settings, contracts = Settings.load(root), Contracts(root / "contracts" / "schemas")
    target, trace_path = root / "outputs" / f"{case_id}.json", root / "traces" / "trace.jsonl"
    if not target.is_file() or not trace_path.is_file():
        raise ValueError("rerun-case requires an existing completed case")
    old_events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory() as temporary_dir:
        trace = TraceWriter(Path(temporary_dir) / "trace.jsonl", contracts)
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            output = await solve_case(cases.cases[case_id], gateway, trace)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        new_events = [
            json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()
        ]
    merged, inserted = [], False
    for event in old_events:
        if event["case_id"] == case_id:
            if not inserted:
                merged.extend(new_events)
                inserted = True
        else:
            merged.append(event)
    if not inserted:
        raise ValueError("existing output has no case trace")
    backup = root / "run-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for source in (target, trace_path):
        destination = backup / source.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    output_tmp, trace_tmp = target.with_suffix(".json.tmp"), trace_path.with_suffix(".jsonl.tmp")
    output_tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    trace_tmp.write_text(
        "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in merged),
        encoding="utf-8",
    )
    output_tmp.replace(target)
    trace_tmp.replace(trace_path)
    print(f"{case_id}: {output['assessment']['primary_issue']}; backup: {backup}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run or resume the workflow for all cases")
    run.add_argument(
        "--fresh", action="store_true", help="archive old artifacts and fetch new evidence"
    )
    rerun = commands.add_parser("rerun-case", help="replace one completed case with fresh evidence")
    rerun.add_argument("case_id")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, fresh=args.fresh))
        elif args.command == "rerun-case":
            asyncio.run(_rerun_case(root, args.case_id))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
