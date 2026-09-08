#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --source-rpm FILE --output-dir DIR [--dist DIST] [--desktop-version VERSION]\n' "$0" >&2
}
source_rpm=""
output_dir=""
dist=.fc44
desktop_version=""
while (($#)); do
    case "$1" in
        --source-rpm) source_rpm=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        --dist) dist=$2; shift 2 ;;
        --desktop-version) desktop_version=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -f "$source_rpm" && -d "$output_dir" ]] || { usage; exit 2; }
topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-desktop-rebuild.XXXXXX")
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,cache}
export XDG_CACHE_HOME="$topdir/cache"
defines=(--define "_topdir $topdir" --define "dist $dist")
if [[ -n "$desktop_version" ]]; then
    defines+=(--define "tongs_desktop_version $desktop_version")
fi
rpmbuild --rebuild "${defines[@]}" "$source_rpm"
find "$topdir/RPMS" -type f -name '*.rpm' -exec cp -- {} "$output_dir/" \;
