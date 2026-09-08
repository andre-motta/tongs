#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --base-image IMAGE --checkout DIR --source-sha SHA --srpm-dir DIR --output-dir DIR\n' "$0" >&2
}

base_image=""
checkout=""
source_sha=""
srpm_dir=""
output_dir=""
while (($#)); do
    case "$1" in
        --base-image) base_image=$2; shift 2 ;;
        --checkout) checkout=$2; shift 2 ;;
        --source-sha) source_sha=$2; shift 2 ;;
        --srpm-dir) srpm_dir=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$base_image" && -d "$checkout" && -n "$source_sha" && -d "$srpm_dir" && -d "$output_dir" ]] || {
    usage
    exit 2
}

while IFS= read -r distribution; do
    mapfile -t matches < <(find "$srpm_dir" -maxdepth 1 -type f \
        -name "python-${distribution}-*.src.rpm" -print)
    [[ ${#matches[@]} -eq 1 ]] || {
        printf 'expected one SRPM for %s, found %s\n' "$distribution" "${#matches[@]}" >&2
        exit 1
    }
    context=$(mktemp -d "${RUNNER_TEMP:-/tmp}/tongs-builddep-${distribution}.XXXXXX")
    cp -- "${matches[0]}" "$context/source.src.rpm"
    cat >"$context/Containerfile" <<EOF
FROM $base_image
COPY source.src.rpm /source.src.rpm
RUN dnf install --assumeyes --setopt=install_weak_deps=False dnf5-plugins rpm-build \
    && dnf builddep --assumeyes --setopt=install_weak_deps=False /source.src.rpm \
    && dnf clean all
LABEL org.opencontainers.image.revision="$source_sha"
LABEL tools.tongs.build-source="declared-srpm-buildrequires"
EOF
    image="localhost/tongs-python-rpm-${distribution}:${source_sha}"
    podman build --pull=never --tag "$image" --file "$context/Containerfile" "$context" \
        2>&1 | tee "$output_dir/buildenv-${distribution}.log"
    podman image inspect "$image" >"$output_dir/buildenv-${distribution}.inspect.json"
    podman run --rm --network=none --cap-drop=all \
        --security-opt=no-new-privileges \
        --volume "$checkout:/checkout:ro" \
        --volume "$output_dir:/output:rw" \
        "$image" \
        /checkout/packaging/rpm/python-dependencies/rebuild_one_srpm.sh \
            --distribution "$distribution" \
            --source-rpm /source.src.rpm \
            --output-dir /output
    podman run --rm --network=none --cap-drop=all \
        --security-opt=no-new-privileges "$image" \
        rpm -qa --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
        | sort >"$output_dir/buildenv-${distribution}.packages.txt"
    rm -rf -- "$context"
done <"$srpm_dir/build-order.txt"

sha256sum "$output_dir"/*.rpm | sed "s#${output_dir}/##" >"$output_dir/SHA256SUMS"
