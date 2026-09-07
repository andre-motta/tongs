#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --output-dir DIR [--inject-failure]\n' "$0" >&2
}

output_dir=""
inject_failure=()
while (($#)); do
    case "$1" in
        --output-dir)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            output_dir=$2
            shift 2
            ;;
        --inject-failure)
            inject_failure=(--inject-failure)
            shift
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[[ -n "$output_dir" ]] || { usage; exit 2; }
command -v git >/dev/null || { printf 'git is required\n' >&2; exit 2; }
command -v podman >/dev/null || { printf 'podman is required\n' >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)

case "$output_dir/" in
    "$repo_root/"*)
        printf 'output directory must be outside the checkout: %s\n' "$repo_root" >&2
        exit 2
        ;;
esac
if find "$output_dir" -mindepth 1 -print -quit | grep -q .; then
    printf 'output directory must be empty: %s\n' "$output_dir" >&2
    exit 2
fi

source_sha=$(git -C "$repo_root" rev-parse HEAD)
image_ref=registry.fedoraproject.org/fedora:44
image_iid_file="$output_dir/harness-image.id"

printf '%q ' "$0" --output-dir "$output_dir" "${inject_failure[@]}" >"$output_dir/invocation.txt"
printf '\n' >>"$output_dir/invocation.txt"
{
    printf 'GITHUB_ACTIONS=%s\n' "${GITHUB_ACTIONS:-false}"
    printf 'GITHUB_REPOSITORY=%s\n' "${GITHUB_REPOSITORY:-unknown}"
    printf 'GITHUB_RUN_ATTEMPT=%s\n' "${GITHUB_RUN_ATTEMPT:-unknown}"
    printf 'GITHUB_RUN_ID=%s\n' "${GITHUB_RUN_ID:-unknown}"
    printf 'GITHUB_SHA=%s\n' "${GITHUB_SHA:-unknown}"
    printf 'GITHUB_WORKFLOW=%s\n' "${GITHUB_WORKFLOW:-unknown}"
    printf 'ImageOS=%s\n' "${ImageOS:-unknown}"
    printf 'ImageVersion=%s\n' "${ImageVersion:-unknown}"
    printf 'RUNNER_ARCH=%s\n' "${RUNNER_ARCH:-unknown}"
    printf 'RUNNER_ENVIRONMENT=%s\n' "${RUNNER_ENVIRONMENT:-unknown}"
    printf 'source_sha=%s\n' "$source_sha"
    printf 'uname=%s\n' "$(uname -a)"
} >"$output_dir/runner.env"
podman version --format json >"$output_dir/podman-version.json"
podman info --format json >"$output_dir/podman-info.json"

podman pull --quiet "$image_ref" >"$output_dir/base-image.pull.txt"
podman image inspect "$image_ref" >"$output_dir/base-image.inspect.json"
podman build \
    --iidfile "$image_iid_file" \
    --label "org.opencontainers.image.revision=$source_sha" \
    --pull=never \
    --tag localhost/tongs-fedora44-probe:issue-27 \
    --file "$script_dir/Containerfile" \
    "$script_dir" 2>&1 | tee "$output_dir/harness-image.build.log"

image_id=$(<"$image_iid_file")
podman image inspect "$image_id" >"$output_dir/harness-image.inspect.json"

podman run --rm \
    --cap-drop=all \
    --network=none \
    --read-only \
    --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=2g \
    --userns=keep-id \
    --env "TONGS_SOURCE_SHA=$source_sha" \
    --volume "$repo_root:/checkout:ro" \
    --volume "$output_dir:/output:rw" \
    "$image_id" "${inject_failure[@]}"
