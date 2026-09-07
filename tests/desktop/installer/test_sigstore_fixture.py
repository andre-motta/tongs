"""Authentic offline Sigstore verification separated from semantic mocks."""

from __future__ import annotations

import hashlib
import json

import pytest
from sigstore.errors import VerificationError
from sigstore.models import Bundle, ClientTrustConfig
from sigstore.verify import Verifier
from sigstore.verify.policy import (
    AllOf,
    Identity,
    OIDCBuildConfigDigest,
    OIDCBuildConfigURI,
    OIDCBuildSignerURI,
    OIDCBuildTrigger,
    OIDCIssuerV2,
    OIDCRunnerEnvironment,
    OIDCSourceRepositoryDigest,
    OIDCSourceRepositoryIdentifier,
    OIDCSourceRepositoryOwnerIdentifier,
    OIDCSourceRepositoryRef,
    OIDCSourceRepositoryURI,
)

from tongs.desktop.installer.metadata import (
    GITHUB_OIDC_ISSUER,
    INTOTO_PAYLOAD_TYPE,
    production_verification_policy,
)

from .helpers import INSTALLER_FIXTURES, identity

FIXTURE_SHA256 = "6b034d6a6046beffec171b4b087001e97029b0bf97b42feea9e3a3deb3fdcffe"
TRUST_SHA256 = "53553bc92bfb7e0d408c01c84a03df573b33b9900b82c9e3062e186f7094c728"
SOURCE_SHA = "181074f4dc11b7e85ef44556e25248ef14fcb554"
REPOSITORY_URL = "https://github.com/sigstore/sigstore-python"
REPOSITORY_ID = "447691086"
REPOSITORY_OWNER_ID = "71096353"
REF = "refs/tags/v4.5.0"
BUILDER = f"{REPOSITORY_URL}/.github/workflows/release.yml@{REF}"


def _verifier_and_bundle() -> tuple[Verifier, Bundle]:
    fixture = INSTALLER_FIXTURES / "sigstore-python-4.5.0.intoto.sigstore.json"
    trust = INSTALLER_FIXTURES / "sigstore-production-client-trust-config.json"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert hashlib.sha256(trust.read_bytes()).hexdigest() == TRUST_SHA256
    config = ClientTrustConfig.from_json(trust.read_text())
    return (
        Verifier(trusted_root=config.trusted_root),
        Bundle.from_json(fixture.read_bytes()),
    )


def _fixture_policy(
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
) -> AllOf:
    return AllOf(
        [
            Identity(identity=BUILDER, issuer=GITHUB_OIDC_ISSUER),
            OIDCIssuerV2(GITHUB_OIDC_ISSUER),
            OIDCRunnerEnvironment("github-hosted"),
            OIDCSourceRepositoryURI(REPOSITORY_URL),
            OIDCSourceRepositoryIdentifier(repository_id),
            OIDCSourceRepositoryOwnerIdentifier(repository_owner_id),
            OIDCSourceRepositoryDigest(SOURCE_SHA),
            OIDCSourceRepositoryRef(REF),
            OIDCBuildSignerURI(BUILDER),
            OIDCBuildConfigURI(BUILDER),
            OIDCBuildConfigDigest(SOURCE_SHA),
            OIDCBuildTrigger("release"),
        ]
    )


def test_authentic_public_bundle_verifies_offline_with_retained_trust_root() -> None:
    verifier, bundle = _verifier_and_bundle()

    payload_type, payload = verifier.verify_dsse(bundle, _fixture_policy())

    assert payload_type == INTOTO_PAYLOAD_TYPE
    statement = json.loads(payload)
    assert statement["subject"][0] == {
        "name": "sigstore-4.5.0-py3-none-any.whl",
        "digest": {
            "sha256": "f045b207f2e12605cf775ec38e89c5eda625d71ffa7830477db65e47ec2bc8b2"
        },
    }


def test_authentic_fixture_is_rejected_by_tongs_production_policy() -> None:
    verifier, bundle = _verifier_and_bundle()

    with pytest.raises(VerificationError):
        verifier.verify_dsse(bundle, production_verification_policy(identity()))


@pytest.mark.parametrize(
    ("repository_id", "repository_owner_id"),
    [
        ("447691087", REPOSITORY_OWNER_ID),
        (REPOSITORY_ID, "71096354"),
    ],
)
def test_authentic_fixture_rejects_wrong_numeric_repository_identity(
    repository_id: str,
    repository_owner_id: str,
) -> None:
    verifier, bundle = _verifier_and_bundle()

    with pytest.raises(VerificationError):
        verifier.verify_dsse(
            bundle,
            _fixture_policy(repository_id, repository_owner_id),
        )
