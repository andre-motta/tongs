#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --prepared-dir DIR --output-dir DIR\n' "$0" >&2
}

prepared_dir=""
output_dir=""
while (($#)); do
    case "$1" in
        --prepared-dir) prepared_dir=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -d "$prepared_dir/SOURCES" && -d "$prepared_dir/SPECS" && -n "$output_dir" ]] || {
    usage
    exit 2
}
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-desktop-srpm.XXXXXX")
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
cp -- "$prepared_dir"/SOURCES/* "$topdir/SOURCES/"
cp -- "$prepared_dir"/SPECS/*.spec "$topdir/SPECS/"

for spec in python-tongs.spec tongs-desktop.spec; do
    rpmbuild -bs --define "_topdir $topdir" "$topdir/SPECS/$spec" \
        2>&1 | tee "$output_dir/srpm-${spec%.spec}.log"
done
cp -- "$topdir"/SRPMS/*.src.rpm "$output_dir/"
rpm -qp --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}|%{SOURCERPM}\n' \
    "$output_dir"/*.src.rpm | sort >"$output_dir/srpm-metadata.txt"
sha256sum "$output_dir"/*.src.rpm | sed "s#${output_dir}/##" >"$output_dir/SHA256SUMS"
