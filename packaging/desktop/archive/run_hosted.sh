#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 4 || "$1" != "--source-root" || "$3" != "--output-dir" ]]; then
  echo "usage: run_hosted.sh --source-root SOURCE --output-dir OUTPUT" >&2
  exit 64
fi

source_root=$(realpath "$2")
output_dir=$4
if [[ -e "$output_dir" ]]; then
  echo "output directory already exists: $output_dir" >&2
  exit 1
fi

expected_head=${TONGS_HEAD_SHA:?TONGS_HEAD_SHA must identify the checked-out candidate}
actual_head=$(git -C "$source_root" rev-parse HEAD)
if [[ "$actual_head" != "$expected_head" ]]; then
  echo "checked-out source does not match TONGS_HEAD_SHA" >&2
  exit 1
fi
if ! git -C "$source_root" diff --quiet || \
   ! git -C "$source_root" diff --cached --quiet; then
  echo "source checkout has tracked modifications" >&2
  exit 1
fi

source_date_epoch=$(git -C "$source_root" show -s --format=%ct "$actual_head")
release_version=0.5.0
electron_name=electron-v44.2.0-linux-x64.zip
electron_sha256=574f7d8cd2a82d77812849729a282b86639b050de120d58b138a126d16b48692
electron_url=https://github.com/electron/electron/releases/download/v44.2.0/$electron_name
image_tag=tongs-desktop-archive-builder:${actual_head}
work_root=$(mktemp -d "${RUNNER_TEMP:?RUNNER_TEMP is required}/tongs-desktop-archive.XXXXXX")
trap 'rm -rf "$work_root"' EXIT

mkdir -p "$output_dir"
source_archive=$work_root/source.tar
electron_archive=$work_root/$electron_name
git -C "$source_root" archive --format=tar --output="$source_archive" "$actual_head"
curl --fail --location --retry 3 --retry-all-errors \
  --output "$electron_archive" "$electron_url"
printf '%s  %s\n' "$electron_sha256" "$electron_archive" | sha256sum --check --strict

podman build \
  --file "$source_root/packaging/desktop/archive/Containerfile.build" \
  --tag "$image_tag" \
  "$source_root/packaging/desktop/archive"

mkdir -p "$work_root/source-a" "$work_root/source-b" "$output_dir/evidence"
tar --extract --file "$source_archive" --directory "$work_root/source-a"
tar --extract --file "$source_archive" --directory "$work_root/source-b"

podman image inspect "$image_tag" > "$output_dir/evidence/builder-image.json"
podman run --rm "$image_tag" /bin/bash -c \
  'python3.12 --version; python3.12 -c "import zlib; print(zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION)"; node --version; npm --version' \
  > "$output_dir/evidence/toolchain.txt"
for record in rpm-nevra.txt rpm-sha256-check.txt rpm-signatures.txt; do
  podman run --rm "$image_tag" \
    cat "/usr/share/tongs-archive-builder/$record" \
    > "$output_dir/evidence/$record"
done

for build in a b; do
  podman run --rm \
    --volume "$work_root:/work:rw,Z" \
    "$image_tag" \
    /bin/bash "/work/source-$build/packaging/desktop/archive/build_in_container.sh" \
    "/work/source-$build" "/work/output-$build" "/work/$electron_name" \
    "$actual_head" "$source_date_epoch" "$release_version"
  (
    cd "$work_root/output-$build"
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
  ) > "$output_dir/evidence/build-$build.sha256"
done

diff --recursive --brief "$work_root/output-a" "$work_root/output-b"
cmp "$output_dir/evidence/build-a.sha256" "$output_dir/evidence/build-b.sha256"
cp -a "$work_root/output-a" "$output_dir/archive"
cp "$source_archive" "$output_dir/evidence/source.tar"
cp "$electron_archive" "$output_dir/evidence/$electron_name"
cat > "$output_dir/evidence/inputs.env" <<EOF
TONGS_HEAD_SHA=$actual_head
SOURCE_DATE_EPOCH=$source_date_epoch
RELEASE_VERSION=$release_version
CORE_MINIMUM=0.4.2-dev.183
CORE_MAXIMUM_EXCLUSIVE=0.5.0
ELECTRON_ARCHIVE_SHA256=$electron_sha256
EOF

echo "Two clean source roots produced byte-identical desktop archive outputs."
