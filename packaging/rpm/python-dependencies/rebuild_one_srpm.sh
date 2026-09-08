#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --distribution NAME --source-rpm FILE --output-dir DIR\n' "$0" >&2
}

distribution=""
source_rpm=""
output_dir=""
while (($#)); do
    case "$1" in
        --distribution) distribution=$2; shift 2 ;;
        --source-rpm) source_rpm=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$distribution" && -f "$source_rpm" && -d "$output_dir" ]] || {
    usage
    exit 2
}

topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-srpm-rebuild.XXXXXX")
cleanup() {
    rm -rf -- "$topdir"
}
trap cleanup EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,cache,cargo-home}
export XDG_CACHE_HOME="$topdir/cache"
export CARGO_HOME="$topdir/cargo-home"

rpmbuild --rebuild --define "_topdir $topdir" "$source_rpm" \
    2>&1 | tee "$output_dir/rebuild-${distribution}.log"

mapfile -t built < <(find "$topdir/RPMS" -type f -name '*.rpm' -print | sort)
[[ ${#built[@]} -gt 0 ]] || { printf 'SRPM rebuild produced no RPMs\n' >&2; exit 1; }
for rpm_path in "${built[@]}"; do
    rpm_name=$(basename -- "$rpm_path")
    cp -- "$rpm_path" "$output_dir/$rpm_name"
    rpm -qp --queryformat '%{NEVRA}\nLicense: %{LICENSE}\n' "$rpm_path" \
        >"$output_dir/${rpm_name}.metadata.txt"
    rpm -qp --provides "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.provides.txt"
    rpm -qp --requires "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.requires.txt"
    rpm -qp --queryformat '[%{FILENAMES}|%{FILEFLAGS:fflags}\n]' "$rpm_path" \
        >"$output_dir/${rpm_name}.file-flags.txt"
done
