"""Published schema versions must not change in place."""

import hashlib
from pathlib import Path

LOCKED_SHA256 = {
    "l3a-output-v2.schema.json": (
        "4d8fc2535c5cf72c13ffd9e8ee75448942ca349c8db7dbfc712ee8512ce4622c"
    ),
    "l3b-output-v2.schema.json": (
        "363df08dc244894f459b4cf559af5e8469e94e60fd53eb38676e305dde6ae7bf"
    ),
    "trace-event-v1.schema.json": (
        "d30468d07526b7694ceaa82194bfcf52ecb01c0b1ab03ffb632e081353679a75"
    ),
    "submission-manifest-v2.schema.json": (
        "4e835cc528519faa274be3ed6c0ba31e130025a809d761c4fc17fb3b3c67ed61"
    ),
    "mcp-evidence-response-v1.schema.json": (
        "fe504f5e68e3de12b666f8c63f7739ea7a51a05ac24fb3fceb06fa893c63ec85"
    ),
}


def test_published_schemas_are_unchanged() -> None:
    schemas = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    assert {path.name for path in schemas.glob("*.schema.json")} == set(LOCKED_SHA256)
    for name, expected in LOCKED_SHA256.items():
        assert hashlib.sha256((schemas / name).read_bytes()).hexdigest() == expected, name
