#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --output-dir DIR\n' "$0" >&2
}

output_dir=""
while (($#)); do
    case "$1" in
        --output-dir)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            output_dir=$2
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done
[[ -n "$output_dir" ]] || { usage; exit 2; }
[[ ${GITHUB_ACTIONS:-false} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || {
    printf 'this RPM harness may run only on a GitHub-hosted disposable runner\n' >&2
    exit 2
}
command -v podman >/dev/null || { printf 'podman is required\n' >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
source_sha=$(git -C "$repo_root" rev-parse HEAD)
requested_sha=${TONGS_HEAD_SHA:-}
[[ -n "$requested_sha" && "$source_sha" == "$requested_sha" ]] || {
    printf 'source revision %s does not match requested head %s\n' "$source_sha" "$requested_sha" >&2
    exit 1
}
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
if find "$output_dir" -mindepth 1 -print -quit | grep -q .; then
    printf 'output directory must be empty: %s\n' "$output_dir" >&2
    exit 2
fi
case "$output_dir/" in
    "$repo_root/"*)
        printf 'output directory must be outside the checkout\n' >&2
        exit 2
        ;;
esac

base_image=registry.fedoraproject.org/fedora:44
builder_image=localhost/tongs-python-rpms:issue-85
prepared="$output_dir/prepared"
rpms="$output_dir/rpms"
install_evidence="$output_dir/install"
mkdir -p -- "$prepared" "$rpms" "$install_evidence"
{
    printf 'GITHUB_RUN_ATTEMPT=%s\n' "${GITHUB_RUN_ATTEMPT:-unknown}"
    printf 'GITHUB_RUN_ID=%s\n' "${GITHUB_RUN_ID:-unknown}"
    printf 'GITHUB_SHA=%s\n' "${GITHUB_SHA:-unknown}"
    printf 'RUNNER_ARCH=%s\n' "${RUNNER_ARCH:-unknown}"
    printf 'RUNNER_ENVIRONMENT=%s\n' "${RUNNER_ENVIRONMENT:-unknown}"
    printf 'source_sha=%s\n' "$source_sha"
} >"$output_dir/runner.env"
podman version --format json >"$output_dir/podman-version.json"
podman info --format json >"$output_dir/podman-info.json"
podman pull --quiet "$base_image" >"$output_dir/base-image.pull.txt"
podman image inspect "$base_image" >"$output_dir/base-image.inspect.json"
podman build \
    --label "org.opencontainers.image.revision=$source_sha" \
    --pull=never \
    --tag "$builder_image" \
    --file "$script_dir/Containerfile" \
    "$script_dir" 2>&1 | tee "$output_dir/builder-image.log"
podman image inspect "$builder_image" >"$output_dir/builder-image.inspect.json"

podman run --rm \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" \
    --volume "$output_dir:/evidence:rw" \
    "$builder_image" \
    python3 /checkout/packaging/rpm/python-dependencies/audit_providers.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --output /evidence/fedora-provider-audit.json

podman run --rm \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" \
    --volume "$prepared:/prepared:rw" \
    "$builder_image" \
    python3 /checkout/packaging/rpm/python-dependencies/prepare_sources.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --output-dir /prepared

podman run --rm \
    --cap-drop=all \
    --network=none \
    --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" \
    --volume "$prepared:/prepared:ro" \
    --volume "$rpms:/rpms:rw" \
    "$builder_image" \
    /checkout/packaging/rpm/python-dependencies/build_rpms.sh \
        --source-dir /prepared \
        --output-dir /rpms

podman run --rm \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" \
    --volume "$rpms:/rpms:ro" \
    --volume "$install_evidence:/evidence:rw" \
    "$base_image" \
    /checkout/packaging/rpm/python-dependencies/install_and_verify.sh \
        --rpm-dir /rpms \
        --evidence-dir /evidence \
        --checkout /checkout

find "$output_dir" -type f -print0 | sort -z | xargs -0 sha256sum \
    | sed "s#${output_dir}/##" >"$output_dir/ALL-SHA256SUMS"
