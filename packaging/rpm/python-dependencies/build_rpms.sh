#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --source-dir DIR --output-dir DIR\n' "$0" >&2
}

source_dir=""
output_dir=""
while (($#)); do
    case "$1" in
        --source-dir)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            source_dir=$2
            shift 2
            ;;
        --output-dir)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            output_dir=$2
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[[ -d "$source_dir" && -n "$output_dir" ]] || { usage; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=$(cd -- "$source_dir" && pwd)
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-rpmbuild.XXXXXX")
cleanup() {
    rm -rf -- "$topdir"
}
trap cleanup EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
export HOME="$topdir/home"
export XDG_CACHE_HOME="$topdir/cache"
export CARGO_HOME="$topdir/cargo-home"
mkdir -p -- "$HOME" "$XDG_CACHE_HOME" "$CARGO_HOME"

python3 "$script_dir/validate_manifest.py" "$script_dir/manifest.json"
cp -- "$source_dir"/*.tar.gz "$topdir/SOURCES/"
cp -- "$script_dir"/specs/*.spec "$topdir/SPECS/"

python3 - "$script_dir/manifest.json" <<'PY' >"$output_dir/build-order.txt"
from __future__ import annotations

import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for name in manifest["build_order"]:
    print(name)
PY

while IFS= read -r distribution; do
    spec="$topdir/SPECS/python-${distribution}.spec"
    log="$output_dir/build-${distribution}.log"
    [[ -f "$spec" ]] || { printf 'missing spec: %s\n' "$spec" >&2; exit 1; }
    rpmbuild -ba --define "_topdir $topdir" "$spec" 2>&1 | tee "$log"
done <"$output_dir/build-order.txt"

cp -- "$topdir"/RPMS/*/*.rpm "$output_dir/"
cp -- "$topdir"/SRPMS/*.rpm "$output_dir/"
sha256sum "$output_dir"/*.rpm | sed "s#${output_dir}/##" >"$output_dir/SHA256SUMS"

for rpm_path in "$output_dir"/*.rpm; do
    rpm_name=$(basename -- "$rpm_path")
    rpm -qp --queryformat '%{NEVRA}\nLicense: %{LICENSE}\n' "$rpm_path" \
        >"$output_dir/${rpm_name}.metadata.txt"
    rpm -qp --provides "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.provides.txt"
    rpm -qp --requires "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.requires.txt"
done
