#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --output-dir DIR [--accepted-dir DIR --payload-manifest FILE]\n' "$0" >&2
}
output_dir=""
accepted_input=""
payload_manifest=""
while (($#)); do
    case "$1" in
        --output-dir) output_dir=$2; shift 2 ;;
        --accepted-dir) accepted_input=$2; shift 2 ;;
        --payload-manifest) payload_manifest=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$output_dir" ]] || { usage; exit 2; }
if [[ -n "$accepted_input" || -n "$payload_manifest" ]]; then
    [[ -d "$accepted_input" && -f "$payload_manifest" ]] || { usage; exit 2; }
fi
[[ ${GITHUB_ACTIONS:-false} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || {
    printf 'this RPM harness may run only on a GitHub-hosted disposable runner\n' >&2
    exit 2
}
command -v podman >/dev/null || { printf 'podman is required\n' >&2; exit 2; }
command -v gh >/dev/null || { printf 'gh is required\n' >&2; exit 2; }

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
    "$repo_root/"*) printf 'output directory must be outside the checkout\n' >&2; exit 2 ;;
esac

base_image=registry.fedoraproject.org/fedora:44
source_builder=localhost/tongs-desktop-rpm-source:issue-52
dependency_builder=localhost/tongs-python-rpms:issue-85
accepted="$output_dir/accepted-download"
dependency_prepared="$output_dir/dependency-prepared"
dependency_srpms="$output_dir/dependency-srpms"
dependency_rpms="$output_dir/dependency-rpms"
companion_consumer_rpms="$output_dir/companion-consumer-rpms"
prepared="$output_dir/prepared"
srpms="$output_dir/srpms"
rpms="$output_dir/rpms"
install_evidence="$output_dir/install"
test_plugin_rpms="$output_dir/test-plugin-rpms"
previous_repo="$output_dir/install-repo/previous"
final_repo="$output_dir/install-repo/final"
mkdir -p -- "$accepted" "$dependency_prepared" "$dependency_srpms" \
    "$dependency_rpms" "$companion_consumer_rpms" "$prepared" "$srpms" "$rpms" \
    "$install_evidence" "$test_plugin_rpms" "$previous_repo" "$final_repo"
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
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --entrypoint /bin/bash "$base_image" -euo pipefail -c '
        required=(bash cat cut dnf find grep install mkdir rpm sed sha256sum sort stat tee)
        for command in "${required[@]}"; do
            resolved=$(command -v "$command")
            test -n "$resolved"
            printf "%s|%s\n" "$command" "$resolved"
        done
    ' >"$output_dir/base-prerequisite-probe.txt"

fixture_arguments=()
if [[ -n "$accepted_input" ]]; then
    accepted_archive=$(cd -- "$accepted_input" && pwd)
else
    payload_manifest="$script_dir/manifest.json"
    artifact_name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accepted_desktop"]["artifact_name"])' "$payload_manifest")
    artifact_run=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accepted_desktop"]["run_id"])' "$payload_manifest")
    gh run download "$artifact_run" --repo andre-motta/tongs --name "$artifact_name" --dir "$accepted"
    accepted_archive="$accepted/archive"
    [[ -d "$accepted_archive" ]] || { printf 'downloaded issue 51 artifact lacks archive directory\n' >&2; exit 1; }
    fixture_arguments=(--allow-reviewed-fixture)
fi
cp -- "$payload_manifest" "$output_dir/payload-input-contract.json"

podman build --pull=never --tag "$source_builder" --file "$script_dir/Containerfile" "$script_dir" \
    2>&1 | tee "$output_dir/source-builder.log"
podman image inspect "$source_builder" >"$output_dir/source-builder.inspect.json"

podman run --rm --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$output_dir:/evidence:rw" \
    "$source_builder" python3 /checkout/packaging/rpm/desktop/audit_providers.py \
        --manifest /checkout/packaging/rpm/desktop/manifest.json \
        --output /evidence/fedora-provider-audit.json
cp -- "$output_dir/fedora-provider-audit.json" \
    "$install_evidence/fedora-provider-audit.json"

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$accepted_archive:/accepted:ro" \
    --volume "$prepared:/prepared:rw" \
    --volume "$output_dir/payload-input-contract.json:/payload-contract.json:ro" \
    "$source_builder" \
    python3 /checkout/packaging/rpm/desktop/prepare_sources.py \
        --checkout /checkout --accepted-dir /accepted --output-dir /prepared \
        --manifest /payload-contract.json "${fixture_arguments[@]}"
core_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["pep440_version"])' \
    "$prepared/prepared-inputs.json")
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$prepared:/prepared:ro" \
    --volume "$output_dir:/evidence:rw" "$source_builder" \
    /checkout/packaging/rpm/desktop/preflight_core_version.sh \
        --prepared-dir /prepared --expected-version "$core_version" \
        --output /evidence/core-version-preflight.log

podman build --pull=never --tag "$dependency_builder" \
    --file "$repo_root/packaging/rpm/python-dependencies/Containerfile" \
    "$repo_root/packaging/rpm/python-dependencies" 2>&1 | tee "$output_dir/dependency-builder.log"
podman image inspect "$dependency_builder" >"$output_dir/dependency-builder.inspect.json"

podman run --rm --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$dependency_prepared:/prepared:rw" \
    "$dependency_builder" python3 /checkout/packaging/rpm/python-dependencies/prepare_sources.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --output-dir /prepared
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$dependency_prepared:/prepared:ro" \
    --volume "$dependency_srpms:/srpms:rw" "$dependency_builder" \
    /checkout/packaging/rpm/python-dependencies/build_rpms.sh \
        --source-dir /prepared --output-dir /srpms
"$repo_root/packaging/rpm/python-dependencies/rebuild_srpms.sh" \
    --base-image "$base_image" --checkout "$repo_root" --source-sha "$source_sha" \
    --srpm-dir "$dependency_srpms" --output-dir "$dependency_rpms"
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$dependency_rpms:/rpms:ro" \
    --volume "$companion_consumer_rpms:/selected:rw" \
    --volume "$output_dir:/evidence:rw" "$source_builder" \
    python3 /checkout/packaging/rpm/desktop/select_companion_rpms.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --rpm-dir /rpms --output-dir /selected \
        --report /evidence/companion-rpm-selection.json

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$prepared:/prepared:ro" \
    --volume "$srpms:/srpms:rw" "$source_builder" \
    /checkout/packaging/rpm/desktop/build_srpms.sh \
        --prepared-dir /prepared --output-dir /srpms
"$script_dir/rebuild_srpms.sh" \
    --base-image "$base_image" --checkout "$repo_root" --source-sha "$source_sha" \
    --srpm-dir "$srpms" --companion-rpm-dir "$companion_consumer_rpms" \
    --output-dir "$rpms"

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" --volume "$test_plugin_rpms:/output:rw" \
    "$source_builder" /checkout/packaging/rpm/desktop/build_test_plugin.sh \
        --source-dir /checkout/packaging/rpm/desktop/test-plugin \
        --license /checkout/LICENSE --output-dir /output
find "$companion_consumer_rpms" -maxdepth 1 -type f -name '*.rpm' \
    -exec cp -- {} "$previous_repo/" \; -exec cp -- {} "$final_repo/" \;
find "$rpms/previous" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' \
    ! -name '*-debuginfo-*' -exec cp -- {} "$previous_repo/" \;
find "$rpms/final" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' \
    ! -name '*-debuginfo-*' -exec cp -- {} "$final_repo/" \;
find "$test_plugin_rpms" -maxdepth 1 -type f -name '*.rpm' \
    -exec cp -- {} "$previous_repo/" \; -exec cp -- {} "$final_repo/" \;
for repository in "$previous_repo" "$final_repo"; do
    podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
        --volume "$repository:/repo:rw" "$source_builder" createrepo_c /repo
done

podman run --rm --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro" \
    --volume "$companion_consumer_rpms:/companions:ro" \
    --volume "$rpms/previous:/previous:ro" --volume "$rpms/final:/final:ro" \
    --volume "$previous_repo:/previous-repo:ro" --volume "$final_repo:/final-repo:ro" \
    --volume "$prepared:/prepared:ro" --volume "$install_evidence:/evidence:rw" \
    "$base_image" /checkout/packaging/rpm/desktop/install_and_verify.sh \
        --companion-dir /companions --previous-dir /previous --final-dir /final \
        --previous-repo /previous-repo --final-repo /final-repo \
        --prepared-dir /prepared --evidence-dir /evidence --checkout /checkout \
        --core-version "$core_version"

find "$output_dir" -type f ! -name ALL-SHA256SUMS -print0 | sort -z | xargs -0 sha256sum \
    | sed "s#${output_dir}/##" >"$output_dir/ALL-SHA256SUMS"
