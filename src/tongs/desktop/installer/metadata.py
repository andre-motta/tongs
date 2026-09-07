"""Fixed-repository release discovery and Sigstore provenance verification."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from typing import Never, cast
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version
from sigstore.errors import VerificationError
from sigstore.models import Bundle, InvalidBundle
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
    OIDCSourceRepositoryRef,
    OIDCSourceRepositoryURI,
)

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    DesktopPlatform,
    PackageKind,
    TargetArchitecture,
    TargetOperatingSystem,
    parse_release_manifest,
    select_release_artifact,
)
from tongs.desktop.installer.download import (
    GITHUB_API_ORIGIN,
    download_asset_bytes,
    download_bytes,
)
from tongs.desktop.installer.models import (
    BuildIdentity,
    Clock,
    DSSEVerifier,
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
    ReleaseAsset,
    ReleaseRecord,
    VerifiedReleaseMetadata,
)

OFFICIAL_REPOSITORY = "andre-motta/tongs"
OFFICIAL_REPOSITORY_URL = f"https://github.com/{OFFICIAL_REPOSITORY}"
OFFICIAL_WORKFLOW_PATH = ".github/workflows/release-desktop.yml"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
RELEASE_TAG_PREFIX = "desktop-v"
RELEASE_MANIFEST_NAME = "desktop-manifest-v1.json"
RELEASE_BUNDLE_NAME = "desktop-manifest-v1.sigstore.json"
INTOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
INTOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
GITHUB_WORKFLOW_BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"
_RELEASE_RE = re.compile(
    r"^desktop-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_JSON_DEPTH = 32
_MAX_JSON_VALUES = 50_000
_MAX_JSON_STRING_BYTES = 512 * 1024
_ARCHITECTURE_ALIASES = {
    "x86_64": TargetArchitecture.X86_64,
    "amd64": TargetArchitecture.X86_64,
    "x86-64": TargetArchitecture.X86_64,
    "aarch64": TargetArchitecture.AARCH64,
    "arm64": TargetArchitecture.AARCH64,
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


async def discover_release(
    client: httpx.AsyncClient,
    request: InstallRequest,
    *,
    limits: InstallerLimits,
    clock: Clock,
) -> ReleaseRecord:
    """Enumerate immutable stable desktop releases and select one exact version."""
    now = clock()
    if now.tzinfo is None:
        _metadata_error("Installer clock must include a timezone.")
    releases: list[ReleaseRecord] = []
    seen_versions: set[str] = set()
    exhausted = False
    for page in range(1, limits.max_release_pages + 1):
        url = (
            f"{GITHUB_API_ORIGIN}/repos/{OFFICIAL_REPOSITORY}/releases"
            f"?per_page={limits.releases_per_page}&page={page}"
        )
        document = await download_bytes(
            client,
            url,
            limits=limits,
            maximum_bytes=limits.max_metadata_bytes,
        )
        value = _decode_json(document, limits.max_metadata_bytes, "release list")
        if not isinstance(value, list):
            _metadata_error("GitHub returned an invalid desktop release list.")
        if len(value) > limits.releases_per_page:
            _limit_error("GitHub returned too many releases in one page.")
        for raw_release in value:
            if not isinstance(raw_release, dict):
                _metadata_error("GitHub returned invalid desktop release metadata.")
            tag = raw_release.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith(RELEASE_TAG_PREFIX):
                continue
            if (
                raw_release.get("draft") is True
                or raw_release.get("prerelease") is True
            ):
                continue
            release = _parse_release(raw_release, now)
            if release.version in seen_versions:
                _metadata_error("Desktop release versions are ambiguous.")
            seen_versions.add(release.version)
            releases.append(release)
        if len(value) < limits.releases_per_page:
            exhausted = True
            break
    if not exhausted:
        _limit_error("Desktop release enumeration exceeded its page bound.")
    if request.version is not None:
        requested = _validate_requested_version(request.version)
        matches = [release for release in releases if release.version == requested]
        if len(matches) != 1:
            _metadata_error("The requested desktop release is unavailable.")
        return matches[0]
    if not releases:
        _metadata_error("No immutable stable desktop release is available.")
    return max(releases, key=lambda item: Version(item.version))


async def resolve_tag_commit(
    client: httpx.AsyncClient,
    tag: str,
    *,
    limits: InstallerLimits,
) -> str:
    """Resolve lightweight or annotated tag indirection through fixed GitHub APIs."""
    encoded = quote(tag, safe="")
    ref_url = f"{GITHUB_API_ORIGIN}/repos/{OFFICIAL_REPOSITORY}/git/ref/tags/{encoded}"
    root = await _download_json_object(client, ref_url, limits)
    if root.get("ref") != f"refs/tags/{tag}":
        _metadata_error("GitHub returned a mismatched desktop release tag.")
    object_value = _require_object(root.get("object"), "tag object")
    object_type = _require_string(object_value.get("type"), "tag object type")
    sha = _require_git_sha(object_value.get("sha"), "tag object commit")
    visited: set[str] = set()
    for _depth in range(limits.max_tag_indirections + 1):
        if object_type == "commit":
            return sha
        if object_type != "tag" or sha in visited:
            _metadata_error("Desktop release tag indirection is invalid.")
        visited.add(sha)
        tag_url = f"{GITHUB_API_ORIGIN}/repos/{OFFICIAL_REPOSITORY}/git/tags/{sha}"
        tag_object = await _download_json_object(client, tag_url, limits)
        nested = _require_object(tag_object.get("object"), "annotated tag object")
        object_type = _require_string(nested.get("type"), "tag object type")
        sha = _require_git_sha(nested.get("sha"), "tag object commit")
    _limit_error("Desktop release tag indirection exceeds its bound.")


async def verify_release_metadata(
    client: httpx.AsyncClient,
    verifier: DSSEVerifier,
    release: ReleaseRecord,
    platform: DesktopPlatform,
    *,
    core_version: str,
    rpc_api_major: int,
    plugin_api_major: int,
    limits: InstallerLimits,
) -> VerifiedReleaseMetadata:
    """Download, verify, and select the signed manifest for one release."""
    _require_supported_platform(platform)
    manifest_asset = _required_asset(release, RELEASE_MANIFEST_NAME)
    bundle_asset = _required_asset(release, RELEASE_BUNDLE_NAME)
    manifest_document = await download_asset_bytes(
        client,
        manifest_asset,
        limits=limits,
        maximum_bytes=limits.max_metadata_bytes,
        expected_sha256=manifest_asset.sha256,
    )
    bundle_document = await download_asset_bytes(
        client,
        bundle_asset,
        limits=limits,
        maximum_bytes=limits.max_bundle_bytes,
        expected_sha256=bundle_asset.sha256,
    )
    try:
        manifest = parse_release_manifest(manifest_document)
        artifact = select_release_artifact(manifest, platform, PackageKind.USER_ARCHIVE)
    except ArtifactContractError as error:
        raise InstallerError(
            InstallerErrorCode.INVALID_METADATA,
            "The desktop release manifest is invalid or incompatible.",
        ) from error
    if manifest.release_version != release.version:
        _metadata_error("Desktop release tag and manifest version disagree.")
    _validate_compatibility(
        manifest.compatibility.core_minimum,
        manifest.compatibility.core_maximum_exclusive,
        manifest.compatibility.rpc_api_major,
        manifest.compatibility.plugin_api_major,
        core_version,
        rpc_api_major,
        plugin_api_major,
    )
    source_commit = await resolve_tag_commit(client, release.tag, limits=limits)
    if manifest.source_commit != source_commit:
        _provenance_error("Desktop manifest source commit does not match its tag.")
    artifact_asset = _required_asset(release, artifact.name)
    if (
        artifact_asset.byte_count != artifact.byte_count
        or artifact_asset.sha256 != artifact.sha256
    ):
        _metadata_error("Desktop artifact metadata disagrees with its manifest.")
    manifest_sha256 = hashlib.sha256(manifest_document).hexdigest()
    identity = _build_identity(release.tag, source_commit)
    _verify_production_attestation(
        verifier,
        bundle_document,
        identity,
        {
            RELEASE_MANIFEST_NAME: manifest_sha256,
            artifact.name: artifact.sha256,
        },
        limits,
    )
    return VerifiedReleaseMetadata(
        release=release,
        manifest=manifest,
        artifact=artifact,
        artifact_asset=artifact_asset,
        manifest_sha256=manifest_sha256,
        manifest_byte_count=len(manifest_document),
        source_commit=source_commit,
        build_identity=identity,
    )


def normalize_platform_identity(
    operating_system: str,
    architecture: str,
    distribution: str,
    distribution_version: str,
    abi: str,
) -> DesktopPlatform:
    """Normalize only documented architecture aliases, then require exact target."""
    try:
        normalized_architecture = _ARCHITECTURE_ALIASES[architecture]
    except KeyError:
        raise InstallerError(
            InstallerErrorCode.UNSUPPORTED_PLATFORM,
            "This desktop architecture is unsupported.",
        ) from None
    if operating_system != "linux":
        raise InstallerError(
            InstallerErrorCode.UNSUPPORTED_PLATFORM,
            "This desktop operating system is unsupported.",
        )
    platform = DesktopPlatform(
        TargetOperatingSystem.LINUX,
        normalized_architecture,
        distribution,
        distribution_version,
        abi,
    )
    _require_supported_platform(platform)
    return platform


def _parse_release(value: JsonObject, now: datetime) -> ReleaseRecord:
    release_id = _require_positive_int(value.get("id"), "release ID")
    tag = _require_string(value.get("tag_name"), "release tag")
    match = _RELEASE_RE.fullmatch(tag)
    if match is None:
        _metadata_error("Desktop release tag is invalid.")
    version = ".".join(match.groups())
    if value.get("draft") is not False or value.get("prerelease") is not False:
        _metadata_error("Desktop release must be stable and published.")
    if value.get("immutable") is not True:
        _metadata_error("Desktop release metadata is not immutable.")
    published_at = _parse_timestamp(value.get("published_at"))
    if published_at > now + timedelta(minutes=5):
        _metadata_error("Desktop release publication time is invalid.")
    raw_assets = value.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        _metadata_error("Desktop release assets are missing.")
    assets = tuple(_parse_asset(item) for item in raw_assets)
    if len({item.asset_id for item in assets}) != len(assets):
        _metadata_error("Desktop release asset IDs are ambiguous.")
    if len({item.name.casefold() for item in assets}) != len(assets):
        _metadata_error("Desktop release asset names are ambiguous.")
    _required_asset_from(assets, RELEASE_MANIFEST_NAME)
    _required_asset_from(assets, RELEASE_BUNDLE_NAME)
    return ReleaseRecord(release_id, tag, version, published_at, assets)


def _parse_asset(value: JsonValue) -> ReleaseAsset:
    item = _require_object(value, "release asset")
    asset_id = _require_positive_int(item.get("id"), "release asset ID")
    name = _require_string(item.get("name"), "release asset name")
    if (
        name in {".", ".."}
        or len(name.encode("utf-8")) > 255
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        _metadata_error("Desktop release asset name is invalid.")
    byte_count = _require_positive_int(item.get("size"), "release asset size")
    digest = _require_string(item.get("digest"), "release asset digest")
    if not digest.startswith("sha256:") or _SHA256_RE.fullmatch(digest[7:]) is None:
        _metadata_error("Desktop release asset digest is invalid.")
    if item.get("state") != "uploaded":
        _metadata_error("Desktop release asset is not complete.")
    api_url = (
        f"{GITHUB_API_ORIGIN}/repos/{OFFICIAL_REPOSITORY}/releases/assets/{asset_id}"
    )
    if item.get("url") != api_url:
        _metadata_error("Desktop release asset source is invalid.")
    return ReleaseAsset(asset_id, name, byte_count, digest[7:], api_url)


def _verify_production_attestation(
    verifier: DSSEVerifier,
    bundle_document: bytes,
    identity: BuildIdentity,
    subjects: dict[str, str],
    limits: InstallerLimits,
) -> None:
    _decode_json(bundle_document, limits.max_bundle_bytes, "Sigstore bundle")
    try:
        bundle = Bundle.from_json(bundle_document)
        payload_type, payload = verifier.verify_dsse(
            bundle, production_verification_policy(identity)
        )
    except (InvalidBundle, VerificationError, ValueError, TypeError):
        _provenance_error("Desktop release Sigstore verification failed.")
    if payload_type != INTOTO_PAYLOAD_TYPE:
        _provenance_error("Desktop release attestation content type is invalid.")
    statement = _decode_json(payload, limits.max_bundle_bytes, "attestation")
    if not isinstance(statement, dict):
        _provenance_error("Desktop release attestation statement is invalid.")
    _validate_statement(statement, identity, subjects)


def production_verification_policy(identity: BuildIdentity) -> AllOf:
    """Build the fixed production identity policy for one resolved release."""
    return AllOf(
        [
            Identity(identity=identity.builder_id, issuer=identity.issuer),
            OIDCIssuerV2(identity.issuer),
            OIDCRunnerEnvironment("github-hosted"),
            OIDCSourceRepositoryURI(OFFICIAL_REPOSITORY_URL),
            OIDCSourceRepositoryDigest(identity.source_commit),
            OIDCSourceRepositoryRef(identity.ref),
            OIDCBuildSignerURI(identity.builder_id),
            OIDCBuildConfigURI(identity.builder_id),
            OIDCBuildConfigDigest(identity.source_commit),
            OIDCBuildTrigger(identity.event),
        ]
    )


def _validate_statement(
    statement: JsonObject,
    identity: BuildIdentity,
    subjects: dict[str, str],
) -> None:
    if set(statement) != {"_type", "subject", "predicateType", "predicate"}:
        _provenance_error("Desktop release attestation fields are invalid.")
    if statement.get("_type") != INTOTO_STATEMENT_TYPE:
        _provenance_error("Desktop release attestation statement type is invalid.")
    if statement.get("predicateType") != SLSA_PREDICATE_TYPE:
        _provenance_error("Desktop release attestation predicate type is invalid.")
    raw_subjects = statement.get("subject")
    if not isinstance(raw_subjects, list) or len(raw_subjects) != len(subjects):
        _provenance_error("Desktop release attestation subjects are invalid.")
    parsed_subjects: dict[str, str] = {}
    for value in raw_subjects:
        item = _require_object(value, "attestation subject")
        if set(item) != {"name", "digest"}:
            _provenance_error("Desktop release attestation subject is invalid.")
        name = _require_string(item.get("name"), "attestation subject name")
        digest = _require_object(item.get("digest"), "attestation subject digest")
        if set(digest) != {"sha256"}:
            _provenance_error("Desktop release attestation digest is invalid.")
        sha256 = _require_string(digest.get("sha256"), "attestation digest")
        if name in parsed_subjects or _SHA256_RE.fullmatch(sha256) is None:
            _provenance_error("Desktop release attestation subjects are ambiguous.")
        parsed_subjects[name] = sha256
    if parsed_subjects != subjects:
        _provenance_error("Desktop release attestation subjects do not match.")

    predicate = _require_object(statement.get("predicate"), "attestation predicate")
    if set(predicate) != {"buildDefinition", "runDetails"}:
        _provenance_error("Desktop release attestation predicate is invalid.")
    definition = _require_object(
        predicate.get("buildDefinition"), "attestation build definition"
    )
    if set(definition) != {
        "buildType",
        "externalParameters",
        "internalParameters",
        "resolvedDependencies",
    }:
        _provenance_error("Desktop release build definition is invalid.")
    if definition.get("buildType") != GITHUB_WORKFLOW_BUILD_TYPE:
        _provenance_error("Desktop release build type is invalid.")
    external = _require_object(
        definition.get("externalParameters"), "attestation external parameters"
    )
    workflow = _require_object(external.get("workflow"), "attestation workflow")
    if set(external) != {"workflow"} or workflow != {
        "ref": identity.ref,
        "repository": OFFICIAL_REPOSITORY_URL,
        "path": identity.workflow_path,
    }:
        _provenance_error("Desktop release workflow parameters are invalid.")
    internal = _require_object(
        definition.get("internalParameters"), "attestation internal parameters"
    )
    github = _require_object(internal.get("github"), "attestation GitHub parameters")
    if set(internal) != {"github"} or set(github) != {
        "event_name",
        "repository_id",
        "repository_owner_id",
        "runner_environment",
    }:
        _provenance_error("Desktop release build parameters are invalid.")
    if github.get("event_name") != identity.event:
        _provenance_error("Desktop release build event is invalid.")
    if github.get("runner_environment") != "github-hosted":
        _provenance_error("Desktop release runner environment is invalid.")
    for identifier in (github.get("repository_id"), github.get("repository_owner_id")):
        if (
            not isinstance(identifier, str)
            or not identifier.isascii()
            or not identifier.isdigit()
            or identifier.startswith("0")
        ):
            _provenance_error("Desktop release repository identity is invalid.")
    dependencies = definition.get("resolvedDependencies")
    if not isinstance(dependencies, list) or len(dependencies) != 1:
        _provenance_error("Desktop release source dependencies are invalid.")
    expected_uri = f"git+{OFFICIAL_REPOSITORY_URL}@{identity.ref}"
    dependency = dependencies[0]
    if not isinstance(dependency, dict) or dependency != {
        "uri": expected_uri,
        "digest": {"gitCommit": identity.source_commit},
    }:
        _provenance_error("Desktop release source dependency is invalid.")
    run_details = _require_object(
        predicate.get("runDetails"), "attestation run details"
    )
    if set(run_details) != {"builder", "metadata"}:
        _provenance_error("Desktop release run details are invalid.")
    builder = _require_object(run_details.get("builder"), "attestation builder")
    if builder != {"id": identity.builder_id}:
        _provenance_error("Desktop release builder identity is invalid.")
    metadata = _require_object(run_details.get("metadata"), "attestation run metadata")
    invocation_id = metadata.get("invocationId")
    prefix = f"{OFFICIAL_REPOSITORY_URL}/actions/runs/"
    if (
        set(metadata) != {"invocationId"}
        or not isinstance(invocation_id, str)
        or not invocation_id.startswith(prefix)
        or re.fullmatch(
            r"[1-9][0-9]*/attempts/[1-9][0-9]*",
            invocation_id.removeprefix(prefix),
        )
        is None
    ):
        _provenance_error("Desktop release workflow invocation is invalid.")


def _build_identity(tag: str, source_commit: str) -> BuildIdentity:
    ref = f"refs/tags/{tag}"
    builder_id = f"{OFFICIAL_REPOSITORY_URL}/{OFFICIAL_WORKFLOW_PATH}@{ref}"
    return BuildIdentity(
        issuer=GITHUB_OIDC_ISSUER,
        repository=OFFICIAL_REPOSITORY,
        workflow_path=OFFICIAL_WORKFLOW_PATH,
        ref=ref,
        source_commit=source_commit,
        event="push",
        builder_id=builder_id,
    )


async def _download_json_object(
    client: httpx.AsyncClient, url: str, limits: InstallerLimits
) -> JsonObject:
    document = await download_bytes(
        client,
        url,
        limits=limits,
        maximum_bytes=limits.max_metadata_bytes,
    )
    value = _decode_json(document, limits.max_metadata_bytes, "GitHub metadata")
    if not isinstance(value, dict):
        _metadata_error("GitHub returned invalid desktop release metadata.")
    return value


def _decode_json(document: bytes, maximum_bytes: int, label: str) -> JsonValue:
    if not document or len(document) > maximum_bytes:
        _limit_error(f"{label} exceeds its size bound.")
    try:
        text = document.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except InstallerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _metadata_error(f"{label} is not valid JSON.")
    _validate_json_bounds(value, label)
    return cast(JsonValue, value)


def _object_without_duplicates(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            _metadata_error("Installer metadata contains duplicate object keys.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Never:
    _metadata_error(f"Installer metadata contains invalid number {value}.")


def _validate_json_bounds(value: object, label: str) -> None:
    remaining = [_MAX_JSON_VALUES]

    def visit(item: object, depth: int) -> None:
        if depth > _MAX_JSON_DEPTH:
            _limit_error(f"{label} nesting exceeds its bound.")
        remaining[0] -= 1
        if remaining[0] < 0:
            _limit_error(f"{label} value count exceeds its bound.")
        if isinstance(item, str):
            try:
                size = len(item.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                _metadata_error(f"{label} contains invalid Unicode.")
            if size > _MAX_JSON_STRING_BYTES:
                _limit_error(f"{label} string exceeds its bound.")
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, float):
            if not math.isfinite(item):
                _metadata_error(f"{label} contains an invalid number.")
        elif item is not None and not isinstance(item, (bool, int)):
            _metadata_error(f"{label} contains an invalid value.")

    visit(value, 0)


def _required_asset(release: ReleaseRecord, name: str) -> ReleaseAsset:
    return _required_asset_from(release.assets, name)


def _required_asset_from(assets: tuple[ReleaseAsset, ...], name: str) -> ReleaseAsset:
    matches = [asset for asset in assets if asset.name == name]
    if len(matches) != 1:
        _metadata_error("Desktop release required asset is missing or ambiguous.")
    return matches[0]


def _validate_requested_version(value: str) -> str:
    tag_match = _RELEASE_RE.fullmatch(f"{RELEASE_TAG_PREFIX}{value}")
    if tag_match is None:
        _metadata_error("Requested desktop version is invalid.")
    return ".".join(tag_match.groups())


def _validate_compatibility(
    minimum: str,
    maximum_exclusive: str,
    expected_rpc: int,
    expected_plugin: int,
    core_version: str,
    rpc_api_major: int,
    plugin_api_major: int,
) -> None:
    try:
        current = Version(core_version)
        compatible = Version(minimum) <= current < Version(maximum_exclusive)
    except InvalidVersion:
        compatible = False
    if (
        not compatible
        or type(rpc_api_major) is not int
        or type(plugin_api_major) is not int
        or expected_rpc != rpc_api_major
        or expected_plugin != plugin_api_major
    ):
        raise InstallerError(
            InstallerErrorCode.INCOMPATIBLE,
            "This desktop release is incompatible with the installed core.",
        )


def _require_supported_platform(platform: DesktopPlatform) -> None:
    expected = DesktopPlatform(
        TargetOperatingSystem.LINUX,
        TargetArchitecture.X86_64,
        "fedora",
        "44",
        "gnu",
    )
    if platform != expected:
        raise InstallerError(
            InstallerErrorCode.UNSUPPORTED_PLATFORM,
            "Only the Fedora 44 x86_64 per-user desktop archive is supported.",
        )


def _parse_timestamp(value: JsonValue | object) -> datetime:
    if not isinstance(value, str):
        _metadata_error("Desktop release publication time is invalid.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _metadata_error("Desktop release publication time is invalid.")
    if parsed.tzinfo is None:
        _metadata_error("Desktop release publication time is invalid.")
    return parsed


def _require_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        _metadata_error(f"Desktop {label} is invalid.")
    return cast(JsonObject, value)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        _metadata_error(f"Desktop {label} is invalid.")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        _metadata_error(f"Desktop {label} is invalid.")
    return cast(int, value)


def _require_git_sha(value: object, label: str) -> str:
    text = _require_string(value, label)
    if _GIT_SHA_RE.fullmatch(text) is None:
        _metadata_error(f"Desktop {label} is invalid.")
    return text


def _metadata_error(message: str) -> Never:
    raise InstallerError(InstallerErrorCode.INVALID_METADATA, message)


def _provenance_error(message: str) -> Never:
    raise InstallerError(InstallerErrorCode.PROVENANCE_FAILED, message)


def _limit_error(message: str) -> Never:
    raise InstallerError(InstallerErrorCode.LIMIT_EXCEEDED, message)


__all__ = [
    "GITHUB_OIDC_ISSUER",
    "GITHUB_WORKFLOW_BUILD_TYPE",
    "INTOTO_PAYLOAD_TYPE",
    "INTOTO_STATEMENT_TYPE",
    "OFFICIAL_REPOSITORY",
    "OFFICIAL_REPOSITORY_URL",
    "OFFICIAL_WORKFLOW_PATH",
    "RELEASE_BUNDLE_NAME",
    "RELEASE_MANIFEST_NAME",
    "RELEASE_TAG_PREFIX",
    "SLSA_PREDICATE_TYPE",
    "discover_release",
    "normalize_platform_identity",
    "production_verification_policy",
    "resolve_tag_commit",
    "verify_release_metadata",
]
