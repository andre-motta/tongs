#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --base-image IMAGE --checkout DIR --source-sha SHA --srpm-dir DIR --companion-rpm-dir DIR --output-dir DIR\n' "$0" >&2
}
base_image=""
checkout=""
source_sha=""
srpm_dir=""
companion_rpm_dir=""
output_dir=""
while (($#)); do
    case "$1" in
        --base-image) base_image=$2; shift 2 ;;
        --checkout) checkout=$2; shift 2 ;;
        --source-sha) source_sha=$2; shift 2 ;;
        --srpm-dir) srpm_dir=$2; shift 2 ;;
        --companion-rpm-dir) companion_rpm_dir=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$base_image" && -d "$checkout" && -n "$source_sha" && -d "$srpm_dir" && -d "$companion_rpm_dir" && -d "$output_dir" ]] || {
    usage
    exit 2
}
script=/checkout/packaging/rpm/desktop/rebuild_one_srpm.sh
for source_name in python-tongs tongs-desktop; do
    source_rpm=$(find "$srpm_dir" -maxdepth 1 -type f -name "${source_name}-*.src.rpm" -print -quit)
    [[ -n "$source_rpm" ]] || { printf 'missing %s source RPM\n' "$source_name" >&2; exit 1; }
    for variant in previous final; do
        context=$(mktemp -d "${RUNNER_TEMP:-/tmp}/tongs-${source_name}-${variant}.XXXXXX")
        cp -- "$source_rpm" "$context/source.src.rpm"
        mkdir "$context/companions"
        find "$companion_rpm_dir" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' \
            ! -name '*-debuginfo-*' -exec cp -- {} "$context/companions/" \;
        cat >"$context/Containerfile" <<EOF
FROM $base_image
COPY companions /companions
COPY source.src.rpm /source.src.rpm
RUN dnf install --assumeyes --setopt=install_weak_deps=False createrepo_c dnf5-plugins rpm-build \
    && createrepo_c /companions \
    && printf '[tongs-companions]\nname=Tongs source-built companions\nbaseurl=file:///companions\nenabled=1\ngpgcheck=0\n' >/etc/yum.repos.d/tongs-companions.repo \
    && dnf builddep --assumeyes --setopt=install_weak_deps=False /source.src.rpm \
    && dnf clean all
LABEL org.opencontainers.image.revision="$source_sha"
LABEL tools.tongs.build-source="declared-srpm-buildrequires"
EOF
        image="localhost/tongs-${source_name}-${variant}:${source_sha}"
        podman build --pull=never --tag "$image" --file "$context/Containerfile" "$context" \
            2>&1 | tee "$output_dir/buildenv-${source_name}-${variant}.log"
        mkdir -p -- "$output_dir/$variant"
        dist=.fc44
        [[ "$variant" == previous ]] && dist='.fc44~previous'
        rebuild_arguments=()
        if [[ "$source_name" == tongs-desktop && "$variant" == previous ]]; then
            rebuild_arguments=(--desktop-version 0.4.9)
        fi
        podman run --rm --network=none --cap-drop=all \
            --security-opt=no-new-privileges \
            --volume "$checkout:/checkout:ro" \
            --volume "$output_dir/$variant:/output:rw" \
            "$image" "$script" --source-rpm /source.src.rpm --output-dir /output \
                --dist "$dist" "${rebuild_arguments[@]}"
        podman image inspect "$image" >"$output_dir/buildenv-${source_name}-${variant}.inspect.json"
        rm -rf -- "$context"
    done
done

for variant in previous final; do
    rpm -qp --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}|%{SOURCERPM}\n' \
        "$output_dir/$variant"/*.rpm | sort >"$output_dir/$variant/package-metadata.txt"
    sha256sum "$output_dir/$variant"/*.rpm | sed "s#${output_dir}/${variant}/##" \
        >"$output_dir/$variant/SHA256SUMS"
done
