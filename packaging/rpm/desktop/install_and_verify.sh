#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --companion-dir DIR --previous-dir DIR --final-dir DIR --previous-repo DIR --final-repo DIR --prepared-dir DIR --evidence-dir DIR --checkout DIR\n' "$0" >&2
}
companion_dir=""
previous_dir=""
final_dir=""
previous_repo=""
final_repo=""
prepared_dir=""
evidence_dir=""
checkout=""
while (($#)); do
    case "$1" in
        --companion-dir) companion_dir=$2; shift 2 ;;
        --previous-dir) previous_dir=$2; shift 2 ;;
        --final-dir) final_dir=$2; shift 2 ;;
        --previous-repo) previous_repo=$2; shift 2 ;;
        --final-repo) final_repo=$2; shift 2 ;;
        --prepared-dir) prepared_dir=$2; shift 2 ;;
        --evidence-dir) evidence_dir=$2; shift 2 ;;
        --checkout) checkout=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
for directory in "$companion_dir" "$previous_dir" "$final_dir" "$previous_repo" \
    "$final_repo" "$prepared_dir" "$evidence_dir" "$checkout"; do
    [[ -d "$directory" ]] || { usage; exit 2; }
done

packaging_dir="$checkout/packaging/rpm/desktop"
identity="$prepared_dir/prepared-inputs.json"
core_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["pep440_version"])' "$identity")
previous_core=$(find "$previous_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
previous_desktop=$(find "$previous_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
final_core=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
final_mcp=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs+mcp-*.noarch.rpm' -print -quit)
final_desktop=$(find "$final_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
for package in "$previous_core" "$previous_desktop" "$final_core" "$final_mcp" "$final_desktop"; do
    [[ -f "$package" ]] || { printf 'missing lifecycle package\n' >&2; exit 1; }
done

cat >/etc/yum.repos.d/tongs-previous.repo <<EOF
[tongs-previous]
name=Tongs previous lifecycle candidates
baseurl=file://$previous_repo
enabled=0
gpgcheck=0
EOF
cat >/etc/yum.repos.d/tongs-final.repo <<EOF
[tongs-final]
name=Tongs final lifecycle candidates
baseurl=file://$final_repo
enabled=0
gpgcheck=0
EOF
dnf repolist --all >"$evidence_dir/install-repositories.txt"

install -d /root/.local/share/applications /root/.config/tongs
printf 'per-user menu sentinel\n' >/root/.local/share/applications/tongs.desktop
printf 'user configuration sentinel\n' >/root/.config/tongs/config.toml
printf 'unrelated sentinel\n' >/tmp/tongs-rpm-unrelated
sentinels=(
    /root/.local/share/applications/tongs.desktop
    /root/.config/tongs/config.toml
    /tmp/tongs-rpm-unrelated
)
tracked_packages=(python3-tongs tongs-desktop tongs-desktop-test-plugin)
snapshot() {
    local name=$1
    local arguments=()
    local package
    for package in "${tracked_packages[@]}"; do
        arguments+=(--package "$package")
    done
    local sentinel
    for sentinel in "${sentinels[@]}"; do
        arguments+=(--sentinel "$sentinel")
    done
    /usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" snapshot \
        "${arguments[@]}" --output "$evidence_dir/$name.json"
}

dnf install --assumeyes --setopt=install_weak_deps=False \
    appstream desktop-file-utils libcap xorg-x11-server-Xvfb xorg-x11-xauth util-linux \
    2>&1 | tee "$evidence_dir/dnf-bootstrap.log"
dnf install --assumeyes --setopt=install_weak_deps=False --enablerepo=tongs-final \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-clean-install.log"
snapshot clean-final

if rpm -q 'python3-tongs+mcp' >"$evidence_dir/minimal-mcp-package.txt" 2>&1; then
    printf 'minimal transaction unexpectedly installed python3-tongs+mcp\n' >&2
    exit 1
fi
if rpm -q 'python3-mcp+cli' >"$evidence_dir/minimal-mcp-provider.txt" 2>&1; then
    printf 'minimal transaction unexpectedly installed python3-mcp+cli\n' >&2
    exit 1
fi
if dnf repoquery --installed --whatprovides 'python3dist(mcp[cli])' \
    >"$evidence_dir/minimal-mcp-capability.txt" 2>&1 && \
    [[ -s "$evidence_dir/minimal-mcp-capability.txt" ]]; then
    printf 'minimal transaction unexpectedly contains the MCP CLI capability\n' >&2
    exit 1
fi
if command -v tongs-mcp >/dev/null; then
    printf 'minimal core unexpectedly owns tongs-mcp\n' >&2
    exit 1
fi
/usr/bin/python3 -E -P - <<'PY' >"$evidence_dir/minimal-plugin.txt"
from tongs.mcp.plugin import MCPPlugin

assert MCPPlugin().get_commands() == []
print("MCP command hidden without optional dependency")
PY

/usr/bin/python3 -E -P "$checkout/packaging/rpm/python-dependencies/verify_install.py" \
    --manifest "$checkout/packaging/rpm/python-dependencies/manifest.json" \
    --fixture-dir "$checkout/tests/desktop/installer/fixtures" \
    --output "$evidence_dir/python-closure.json"
/usr/bin/python3 -E -P "$packaging_dir/verify_install.py" \
    --install-manifest "$prepared_dir/SOURCES/desktop-install.json" \
    --libexec-dir /usr/libexec/tongs-desktop \
    --expected-version "$core_version" \
    --expected-launcher "$prepared_dir/SOURCES/tongs-desktop" \
    --expected-desktop "$prepared_dir/SOURCES/tongs.desktop" \
    --output "$evidence_dir/installed-payload.json"
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" metadata \
    --package python3-tongs --package tongs-desktop \
    --package tongs-desktop-test-plugin \
    --output "$evidence_dir/package-file-metadata.json"
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" elf \
    --package tongs-desktop --output "$evidence_dir/elf-provider-map.json"
timeout --signal=TERM 20s /usr/bin/python3 -E -P "$packaging_dir/verify_sidecar_plugin.py" \
    --expected-version "$core_version" --output "$evidence_dir/sidecar-plugin.ndjson"
desktop-file-validate /usr/share/applications/tongs.desktop
appstreamcli validate --no-net /usr/share/metainfo/io.github.andre_motta.tongs.metainfo.xml \
    >"$evidence_dir/appstream-validation.txt"

set +e
/usr/bin/tongs-desktop unexpected >"$evidence_dir/launcher-argument.stdout" \
    2>"$evidence_dir/launcher-argument.stderr"
argument_status=$?
set -e
[[ $argument_status -eq 64 ]] || { printf 'launcher argument guard failed\n' >&2; exit 1; }
useradd --create-home --shell /bin/bash tongs-rpm-test
install -d -m 0700 -o tongs-rpm-test -g tongs-rpm-test /tmp/tongs-rpm-runtime
set +e
runuser -u tongs-rpm-test -- env XDG_RUNTIME_DIR=/tmp/tongs-rpm-runtime \
    timeout --signal=TERM 12s xvfb-run -a sh -x /usr/bin/tongs-desktop \
    >"$evidence_dir/hosted-launch.stdout" 2>"$evidence_dir/hosted-launch.stderr"
launch_status=$?
set -e
printf '%s\n' "$launch_status" >"$evidence_dir/hosted-launch.exit-status"
[[ $launch_status -eq 124 ]] || {
    printf 'desktop launch did not remain live for the bounded X11 window: %s\n' \
        "$launch_status" >&2
    exit 1
}
grep -F -- 'exec /usr/libexec/tongs-desktop/tongs-desktop --ozone-platform=x11' \
    "$evidence_dir/hosted-launch.stderr"
printf 'This hosted Xvfb smoke proves launcher/runtime liveness only; it makes no hardware GPU claim.\n' \
    >"$evidence_dir/hosted-launch-scope.txt"

snapshot before-mcp
dnf install --assumeyes --setopt=install_weak_deps=False --enablerepo=tongs-final \
    'python3-tongs+mcp' 2>&1 | tee "$evidence_dir/dnf-mcp-install.log"
[[ $(rpm -qf /usr/bin/tongs-mcp --queryformat '%{NAME}') == 'python3-tongs+mcp' ]]
command -v tongs-mcp >"$evidence_dir/tongs-mcp-path.txt"
timeout --signal=TERM 20s /usr/bin/python3 -E -P "$packaging_dir/verify_mcp_command.py" \
    --output "$evidence_dir/mcp-command.json"
/usr/bin/python3 -E -P - <<'PY' >"$evidence_dir/mcp-plugin.txt"
import mcp.server.fastmcp
from tongs.mcp.plugin import MCPPlugin

commands = MCPPlugin().get_commands()
assert len(commands) == 1 and commands[0][0] == "Start MCP Server"
print("MCP command available with optional dependency")
PY
dnf remove --assumeyes 'python3-tongs+mcp' \
    2>&1 | tee "$evidence_dir/dnf-mcp-remove.log"
! command -v tongs-mcp
snapshot after-mcp-removal
cmp "$evidence_dir/before-mcp.json" "$evidence_dir/after-mcp-removal.json"

snapshot before-reinstall
dnf reinstall --assumeyes --setopt=install_weak_deps=False --enablerepo=tongs-final \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-reinstall.log"
snapshot after-reinstall
cmp "$evidence_dir/before-reinstall.json" "$evidence_dir/after-reinstall.json"

for package in python3-tongs tongs-desktop tongs-desktop-test-plugin; do
    rpm -q "$package" --queryformat '[%{FILENAMES}|%{FILEMODES:perms}\n]' \
        | awk -F '|' '$2 !~ /^d/ {print $1}' >>"$evidence_dir/owned-files.txt"
done
dnf remove --assumeyes tongs-desktop-test-plugin tongs-desktop python3-tongs \
    2>&1 | tee "$evidence_dir/dnf-final-cycle-remove.log"
snapshot after-final-cycle-remove

dnf install --assumeyes --setopt=install_weak_deps=False --enablerepo=tongs-previous \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-previous-install.log"
[[ $(rpm -q tongs-desktop --queryformat '%{VERSION}') == 0.4.9 ]]
snapshot installed-previous

corrupt_desktop=/tmp/corrupt-tongs-desktop.rpm
cp -- "$final_desktop" "$corrupt_desktop"
/usr/bin/python3 - "$corrupt_desktop" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = bytearray(path.read_bytes())
payload[-1] ^= 0xFF
path.write_bytes(payload)
PY
set +e
rpm -K "$corrupt_desktop" >"$evidence_dir/corrupt-rpm-check.log" 2>&1
corrupt_check_status=$?
dnf upgrade --assumeyes --disablerepo='*' "$final_core" "$corrupt_desktop" \
    >"$evidence_dir/dnf-corrupt-upgrade.log" 2>&1
corrupt_upgrade_status=$?
set -e
[[ $corrupt_check_status -ne 0 && $corrupt_upgrade_status -ne 0 ]] || {
    printf 'corrupted higher candidate was not rejected\n' >&2
    exit 1
}
grep -Eiq 'NOT OK|BAD|digest|payload|checksum|does not verify|signature' \
    "$evidence_dir/corrupt-rpm-check.log" "$evidence_dir/dnf-corrupt-upgrade.log"
snapshot after-corrupt-failure
cmp "$evidence_dir/installed-previous.json" "$evidence_dir/after-corrupt-failure.json"

set +e
dnf upgrade --assumeyes --disablerepo='*' "$final_desktop" \
    >"$evidence_dir/dnf-failed-upgrade.log" 2>&1
failed_upgrade_status=$?
set -e
[[ $failed_upgrade_status -ne 0 ]] || {
    printf 'dependency-negative upgrade unexpectedly passed\n' >&2
    exit 1
}
grep -Eiq 'nothing provides|conflicting requests|cannot install|problem with installed package' \
    "$evidence_dir/dnf-failed-upgrade.log"
snapshot after-dependency-failure
cmp "$evidence_dir/installed-previous.json" "$evidence_dir/after-dependency-failure.json"

dnf upgrade --assumeyes --setopt=install_weak_deps=False --enablerepo=tongs-final \
    python3-tongs tongs-desktop \
    2>&1 | tee "$evidence_dir/dnf-upgrade.log"
[[ $(rpm -q tongs-desktop --queryformat '%{VERSION}') == 0.5.0 ]]
snapshot upgraded-final
/usr/bin/python3 -E -P "$packaging_dir/verify_install.py" \
    --install-manifest "$prepared_dir/SOURCES/desktop-install.json" \
    --libexec-dir /usr/libexec/tongs-desktop --expected-version "$core_version" \
    --expected-launcher "$prepared_dir/SOURCES/tongs-desktop" \
    --expected-desktop "$prepared_dir/SOURCES/tongs.desktop" \
    --output "$evidence_dir/upgraded-installed-payload.json"

rpm -qa --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    | sort >"$evidence_dir/rpm-installed.txt"
dnf repoquery --installed --queryformat '%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{from_repo}\n' \
    | sort >"$evidence_dir/installed-packages.txt"
rpm -q --requires python3-tongs >"$evidence_dir/core-requires.txt"
rpm -qp --requires "$final_mcp" >"$evidence_dir/mcp-requires.txt"
rpm -q --requires tongs-desktop >"$evidence_dir/desktop-requires.txt"
rpm -q --provides python3-tongs >"$evidence_dir/core-provides.txt"
rpm -qp --provides "$final_mcp" >"$evidence_dir/mcp-provides.txt"
rpm -q --provides tongs-desktop >"$evidence_dir/desktop-provides.txt"
grep -Eq '^python\(abi\) = 3\.[0-9]+$' "$evidence_dir/core-requires.txt"
grep -F 'python3dist(mcp[cli])' "$evidence_dir/mcp-requires.txt"
grep -F 'xorg-x11-server-Xwayland' "$evidence_dir/desktop-requires.txt"
core_evr=$(rpm -q python3-tongs --queryformat '%{VERSION}-%{RELEASE}')
grep -Fx "python3-tongs = $core_evr" "$evidence_dir/desktop-requires.txt"
grep -Fx "python3-tongs = $core_evr" "$evidence_dir/mcp-requires.txt"
for package in "$final_core" "$final_mcp" "$final_desktop"; do
    rpm -qp --scripts "$package" >>"$evidence_dir/package-scriptlets.txt"
done
[[ ! -s "$evidence_dir/package-scriptlets.txt" ]]

dnf remove --assumeyes tongs-desktop-test-plugin tongs-desktop python3-tongs \
    2>&1 | tee "$evidence_dir/dnf-uninstall.log"
while IFS= read -r owned; do
    [[ -z "$owned" ]] && continue
    [[ ! -e "$owned" && ! -L "$owned" ]] || {
        printf 'owned path remains after uninstall: %s\n' "$owned" >&2
        exit 1
    }
done <"$evidence_dir/owned-files.txt"
snapshot after-uninstall
