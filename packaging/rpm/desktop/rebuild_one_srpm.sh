#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --source-rpm FILE --output-dir DIR [--dist DIST]\n' "$0" >&2
}
source_rpm=""
output_dir=""
dist=.fc44
while (($#)); do
    case "$1" in
        --source-rpm) source_rpm=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        --dist) dist=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -f "$source_rpm" && -d "$output_dir" ]] || { usage; exit 2; }
topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-desktop-rebuild.XXXXXX")
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,cache}
export XDG_CACHE_HOME="$topdir/cache"
rpmbuild --rebuild --define "_topdir $topdir" --define "dist $dist" "$source_rpm"
find "$topdir/RPMS" -type f -name '*.rpm' -exec cp -- {} "$output_dir/" \;
