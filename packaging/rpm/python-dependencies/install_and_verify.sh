#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --rpm-dir DIR --evidence-dir DIR --checkout DIR\n' "$0" >&2
}

rpm_dir=""
evidence_dir=""
checkout=""
while (($#)); do
    case "$1" in
        --rpm-dir)
            rpm_dir=$2
            shift 2
            ;;
        --evidence-dir)
            evidence_dir=$2
            shift 2
            ;;
        --checkout)
            checkout=$2
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done
[[ -d "$rpm_dir" && -d "$evidence_dir" && -d "$checkout" ]] || { usage; exit 2; }
packaging_dir="$checkout/packaging/rpm/python-dependencies"
manifest="$packaging_dir/manifest.json"

mapfile -t requirements < <(
    python3 - "$manifest" <<'PY'
from __future__ import annotations

import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for item in manifest["system_requirements"]:
    print(item["requirement"])
PY
)
mapfile -t binary_rpms < <(find "$rpm_dir" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' -print | sort)
[[ ${#binary_rpms[@]} -eq 7 ]] || {
    printf 'expected seven binary companion RPMs, found %s\n' "${#binary_rpms[@]}" >&2
    exit 1
}

dnf install --assumeyes --setopt=install_weak_deps=False \
    "${requirements[@]}" "${binary_rpms[@]}" 2>&1 | tee "$evidence_dir/dnf-install.log"
dnf repoquery --installed --queryformat '%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{from_repo}' \
    | sort >"$evidence_dir/installed-packages.txt"
rpm -qa --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    | sort >"$evidence_dir/rpm-installed.txt"

python3 "$packaging_dir/verify_install.py" \
    --manifest "$manifest" \
    --fixture-dir "$checkout/tests/desktop/installer/fixtures" \
    --output "$evidence_dir/import-and-verifier-smoke.json"

extension=$(
    python3 - <<'PY'
import rfc3161_client._rust

print(rfc3161_client._rust.__file__)
PY
)
ldd "$extension" | tee "$evidence_dir/rfc3161-extension.ldd.txt"
if grep -Fq '/vendor/' "$evidence_dir/rfc3161-extension.ldd.txt"; then
    printf 'rfc3161 extension links a prepared vendor path\n' >&2
    exit 1
fi
libcrypto=$(awk '/libcrypto\.so/{print $3; exit}' "$evidence_dir/rfc3161-extension.ldd.txt")
[[ -n "$libcrypto" && -f "$libcrypto" ]] || {
    printf 'rfc3161 extension did not resolve Fedora libcrypto\n' >&2
    exit 1
}
rpm -qf "$libcrypto" >"$evidence_dir/rfc3161-libcrypto-owner.txt"
grep -Eq '^openssl-libs-' "$evidence_dir/rfc3161-libcrypto-owner.txt"
