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

mapfile -t source_rpms < <(find "$srpm_dir" -maxdepth 1 -type f \
    -name '*.src.rpm' -print | sort)
[[ ${#source_rpms[@]} -gt 0 ]] || { printf 'no source RPMs found\n' >&2; exit 1; }
container_srpms=()
for source_rpm in "${source_rpms[@]}"; do
    container_srpms+=("/srpms/$(basename -- "$source_rpm")")
done
podman run --rm --network=none --cap-drop=all \
    --security-opt=no-new-privileges \
    --volume "$srpm_dir:/srpms:ro" \
    --entrypoint rpm \
    "$base_image" \
    -qp --queryformat '%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    "${container_srpms[@]}" >"$output_dir/srpm-package-metadata.txt"

python3 "$checkout/packaging/rpm/python-dependencies/resolve_srpms.py" \
    --build-order "$srpm_dir/build-order.txt" \
    --metadata "$output_dir/srpm-package-metadata.txt" \
    --srpm-dir "$srpm_dir" >"$output_dir/resolved-srpms.txt"

while IFS='|' read -r distribution source_rpm; do
    context=$(mktemp -d "${RUNNER_TEMP:-/tmp}/tongs-builddep-${distribution}.XXXXXX")
    cp -- "$source_rpm" "$context/source.src.rpm"
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
done <"$output_dir/resolved-srpms.txt"

sha256sum "$output_dir"/*.rpm | sed "s#${output_dir}/##" >"$output_dir/SHA256SUMS"
