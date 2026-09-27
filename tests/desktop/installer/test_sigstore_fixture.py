"""Authentic offline Sigstore verification separated from semantic mocks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
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
    OFFICIAL_REPOSITORY_ID,
    OFFICIAL_REPOSITORY_OWNER_ID,
    OFFICIAL_REPOSITORY_URL,
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


def _fixture_policy() -> AllOf:
    return AllOf(
        [
            Identity(identity=BUILDER, issuer=GITHUB_OIDC_ISSUER),
            OIDCIssuerV2(GITHUB_OIDC_ISSUER),
            OIDCRunnerEnvironment("github-hosted"),
            OIDCSourceRepositoryURI(REPOSITORY_URL),
            OIDCSourceRepositoryIdentifier(REPOSITORY_ID),
            OIDCSourceRepositoryOwnerIdentifier(REPOSITORY_OWNER_ID),
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


def _der_utf8(value: str) -> bytes:
    encoded = value.encode()
    assert len(encoded) < 128
    return bytes([0x0C, len(encoded)]) + encoded


def _signing_certificate(
    repository_id: str = OFFICIAL_REPOSITORY_ID,
    repository_owner_id: str = OFFICIAL_REPOSITORY_OWNER_ID,
) -> x509.Certificate:
    """A self-signed leaf carrying the Fulcio claims of an official release."""
    release = identity()
    claims = {
        "1.3.6.1.4.1.57264.1.8": release.issuer,
        "1.3.6.1.4.1.57264.1.11": "github-hosted",
        "1.3.6.1.4.1.57264.1.12": OFFICIAL_REPOSITORY_URL,
        "1.3.6.1.4.1.57264.1.15": repository_id,
        "1.3.6.1.4.1.57264.1.17": repository_owner_id,
        "1.3.6.1.4.1.57264.1.13": release.source_commit,
        "1.3.6.1.4.1.57264.1.14": release.ref,
        "1.3.6.1.4.1.57264.1.9": release.builder_id,
        "1.3.6.1.4.1.57264.1.18": release.builder_id,
        "1.3.6.1.4.1.57264.1.19": release.source_commit,
        "1.3.6.1.4.1.57264.1.20": release.event,
    }
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2026, 1, 2, tzinfo=UTC))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(release.builder_id)]
            ),
            critical=False,
        )
        .add_extension(
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.1"),
                release.issuer.encode(),
            ),
            critical=False,
        )
    )
    for oid, value in claims.items():
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), _der_utf8(value)),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256())


def test_production_policy_accepts_the_official_release_identity() -> None:
    production_verification_policy(identity()).verify(_signing_certificate())


@pytest.mark.parametrize(
    ("repository_id", "repository_owner_id"),
    [
        (str(int(OFFICIAL_REPOSITORY_ID) + 1), OFFICIAL_REPOSITORY_OWNER_ID),
        (OFFICIAL_REPOSITORY_ID, str(int(OFFICIAL_REPOSITORY_OWNER_ID) + 1)),
    ],
    ids=["repository-id", "owner-id"],
)
def test_production_policy_pins_the_numeric_repository_identity(
    repository_id: str,
    repository_owner_id: str,
) -> None:
    """A recreated repository under the same name must not sign releases."""
    certificate = _signing_certificate(repository_id, repository_owner_id)

    with pytest.raises(VerificationError):
        production_verification_policy(identity()).verify(certificate)
