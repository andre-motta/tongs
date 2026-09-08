"""Identity and license checks for the vendored official SPDX 2.3 schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft7Validator

ROOT = Path(__file__).parents[4]
SCHEMA_ROOT = ROOT / "tests/packaging/desktop/sbom/schema"


def test_official_schema_and_license_have_pinned_identities() -> None:
    schema_bytes = (SCHEMA_ROOT / "spdx-2.3.schema.json").read_bytes()
    license_bytes = (SCHEMA_ROOT / "SPDX-SCHEMA-LICENSE.txt").read_bytes()
    assert hashlib.sha256(schema_bytes).hexdigest() == (
        "239208b7ac287b3cf5d9a9af23f9d69863971102a5e1587a27a398b43490b89b"
    )
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "017e38491cccbd2bdb6da0a32a33db9ec245b5dab30fdcd09f2c742c975e5b35"
    )
    schema = json.loads(schema_bytes)
    assert schema["$id"] == "http://spdx.org/rdf/terms/2.3"
    assert schema["title"] == "SPDX 2.3"
    Draft7Validator.check_schema(schema)
    assert license_bytes.startswith(b"Creative Commons Attribution 3.0 Unported\n")
