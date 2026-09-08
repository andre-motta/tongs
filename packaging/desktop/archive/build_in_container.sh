#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 6 ]]; then
  echo "usage: build_in_container.sh SOURCE OUTPUT ELECTRON_ZIP COMMIT EPOCH RELEASE" >&2
  exit 64
fi

source_root=$1
output_dir=$2
electron_archive=$3
source_commit=$4
source_date_epoch=$5
release_version=$6

export HOME=/tmp/tongs-archive-home
export LC_ALL=C.UTF-8
export SOURCE_DATE_EPOCH="$source_date_epoch"
export TZ=UTC

mkdir -p "$HOME"
npm --prefix "$source_root/desktop" ci \
  --ignore-scripts \
  --no-audit \
  --no-fund

PYTHONPATH="$source_root/src" python3.12 \
  "$source_root/scripts/build_desktop_archive.py" \
  --source-root "$source_root" \
  --electron-archive "$electron_archive" \
  --output-dir "$output_dir" \
  --release-version "$release_version" \
  --core-minimum 0.4.2-dev.183 \
  --core-maximum-exclusive 0.5.0 \
  --source-commit "$source_commit" \
  --source-date-epoch "$source_date_epoch"
