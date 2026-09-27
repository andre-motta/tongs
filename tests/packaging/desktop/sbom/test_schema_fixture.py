"""Identity check for the vendored official SPDX 2.3 schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft7Validator

ROOT = Path(__file__).parents[4]
SCHEMA_ROOT = ROOT / "tests/packaging/desktop/sbom/schema"


def test_official_schema_has_its_pinned_identity() -> None:
    schema_bytes = (SCHEMA_ROOT / "spdx-2.3.schema.json").read_bytes()
    assert hashlib.sha256(schema_bytes).hexdigest() == (
        "239208b7ac287b3cf5d9a9af23f9d69863971102a5e1587a27a398b43490b89b"
    )
    schema = json.loads(schema_bytes)
    assert schema["$id"] == "http://spdx.org/rdf/terms/2.3"
    Draft7Validator.check_schema(schema)
