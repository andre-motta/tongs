#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --source-dir DIR --license FILE --output-dir DIR\n' "$0" >&2
}
source_dir=""
license=""
output_dir=""
while (($#)); do
    case "$1" in
        --source-dir) source_dir=$2; shift 2 ;;
        --license) license=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -d "$source_dir" && -f "$license" && -d "$output_dir" ]] || {
    usage
    exit 2
}
topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-rpm-test-plugin.XXXXXX")
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
cp -- "$source_dir"/{module.mjs,tongs_rpm_test_plugin.py} "$topdir/SOURCES/"
cp -- "$license" "$topdir/SOURCES/LICENSE"
cp -- "$source_dir/tongs-desktop-test-plugin.spec" "$topdir/SPECS/"
rpmbuild -bb --define "_topdir $topdir" \
    "$topdir/SPECS/tongs-desktop-test-plugin.spec"
find "$topdir/RPMS" -type f -name '*.rpm' -exec cp -- {} "$output_dir/" \;
