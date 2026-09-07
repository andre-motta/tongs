"""Packaged JSON schema contract tests."""

from __future__ import annotations

import json

from tongs.desktop.artifact_contract import install_schema_bytes, release_schema_bytes

from .reference_builder import FIXTURE_ROOT


def test_packaged_schemas_are_strict_versioned_json_documents() -> None:
    release_schema = json.loads(release_schema_bytes())
    install_schema = json.loads(install_schema_bytes())

    assert release_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert install_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert release_schema["additionalProperties"] is False
    assert install_schema["additionalProperties"] is False
    assert release_schema["properties"]["schema_version"]["const"] == 1
    assert install_schema["properties"]["schema_version"]["const"] == 1


def test_schema_examples_match_the_parser_fixture_shapes() -> None:
    release = json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    install = json.loads((FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes())
    release_schema = json.loads(release_schema_bytes())
    install_schema = json.loads(install_schema_bytes())

    assert set(release) == set(release_schema["required"])
    assert set(install) == set(install_schema["required"])
