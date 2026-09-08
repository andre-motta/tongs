#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --prepared-dir DIR --expected-version VERSION --output FILE\n' "$0" >&2
}
prepared_dir=""
expected_version=""
output=""
while (($#)); do
    case "$1" in
        --prepared-dir) prepared_dir=$2; shift 2 ;;
        --expected-version) expected_version=$2; shift 2 ;;
        --output) output=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -d "$prepared_dir/SOURCES" && -f "$prepared_dir/SPECS/python-tongs.spec" \
    && -n "$expected_version" && -n "$output" ]] || { usage; exit 2; }

topdir=$(mktemp -d "${TMPDIR:-/tmp}/tongs-core-version-preflight.XXXXXX")
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
cp -- "$prepared_dir"/SOURCES/* "$topdir/SOURCES/"
cp -- "$prepared_dir/SPECS/python-tongs.spec" "$topdir/SPECS/"
rpmbuild -bc --nodeps --define "_topdir $topdir" "$topdir/SPECS/python-tongs.spec" \
    2>&1 | tee "$output"
wheel=$(find "$topdir/BUILD" -type f -path '*/pyproject-wheeldir/*.whl' -print -quit)
[[ -f "$wheel" ]] || { printf 'version preflight did not create a wheel\n' >&2; exit 1; }
python3 - "$wheel" "$expected_version" <<'PY'
from __future__ import annotations

import email
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
expected = sys.argv[2]
with zipfile.ZipFile(wheel) as archive:
    metadata_name = next(
        name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
    )
    metadata = email.message_from_bytes(archive.read(metadata_name))
actual = metadata["Version"]
if actual != expected:
    raise RuntimeError(f"source-built wheel version {actual!r} != {expected!r}")
print(f"source-built wheel version: {actual}")
PY
