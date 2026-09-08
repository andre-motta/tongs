#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --companion-dir DIR --previous-dir DIR --final-dir DIR --prepared-dir DIR --evidence-dir DIR --checkout DIR\n' "$0" >&2
}
companion_dir=""
previous_dir=""
final_dir=""
prepared_dir=""
evidence_dir=""
checkout=""
while (($#)); do
    case "$1" in
        --companion-dir) companion_dir=$2; shift 2 ;;
        --previous-dir) previous_dir=$2; shift 2 ;;
        --final-dir) final_dir=$2; shift 2 ;;
        --prepared-dir) prepared_dir=$2; shift 2 ;;
        --evidence-dir) evidence_dir=$2; shift 2 ;;
        --checkout) checkout=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -d "$companion_dir" && -d "$previous_dir" && -d "$final_dir" && -d "$prepared_dir" && -d "$evidence_dir" && -d "$checkout" ]] || {
    usage
    exit 2
}
packaging_dir="$checkout/packaging/rpm/desktop"
identity="$prepared_dir/prepared-inputs.json"
core_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["pep440_version"])' "$identity")

mapfile -t companion_rpms < <(find "$companion_dir" -maxdepth 1 -type f -name '*.rpm' \
    ! -name '*.src.rpm' ! -name '*-debuginfo-*' -print | sort)
[[ ${#companion_rpms[@]} -eq 7 ]] || {
    printf 'expected seven companion RPMs, found %s\n' "${#companion_rpms[@]}" >&2
    exit 1
}
previous_core=$(find "$previous_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
previous_desktop=$(find "$previous_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
final_core=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
final_mcp=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs+mcp-*.noarch.rpm' -print -quit)
final_desktop=$(find "$final_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
for package in "$previous_core" "$previous_desktop" "$final_core" "$final_mcp" "$final_desktop"; do
    [[ -f "$package" ]] || { printf 'missing lifecycle package\n' >&2; exit 1; }
done

install -d /root/.local/share/applications /root/.config/tongs
printf 'per-user menu sentinel\n' >/root/.local/share/applications/tongs.desktop
printf 'user configuration sentinel\n' >/root/.config/tongs/config.toml
printf 'unrelated sentinel\n' >/tmp/tongs-rpm-unrelated

dnf install --assumeyes --setopt=install_weak_deps=False \
    xorg-x11-server-Xvfb util-linux "${companion_rpms[@]}" \
    2>&1 | tee "$evidence_dir/dnf-bootstrap.log"
dnf install --assumeyes --setopt=install_weak_deps=False "$previous_core" "$previous_desktop" \
    2>&1 | tee "$evidence_dir/dnf-clean-install.log"

if command -v tongs-mcp >/dev/null; then
    printf 'minimal core unexpectedly owns tongs-mcp\n' >&2
    exit 1
fi
python3 -E -P - <<'PY' >"$evidence_dir/minimal-plugin.txt"
from tongs.mcp.plugin import MCPPlugin

assert MCPPlugin().get_commands() == []
print("MCP command hidden without optional dependency")
PY
set +e
/usr/bin/tongs-desktop unexpected >"$evidence_dir/launcher-argument.stdout" \
    2>"$evidence_dir/launcher-argument.stderr"
argument_status=$?
set -e
[[ $argument_status -eq 64 ]] || { printf 'launcher argument guard failed\n' >&2; exit 1; }

useradd --create-home --shell /bin/bash tongs-rpm-test
set +e
timeout --signal=TERM 12s runuser -u tongs-rpm-test -- xvfb-run -a /usr/bin/tongs-desktop \
    >"$evidence_dir/hosted-launch.stdout" 2>"$evidence_dir/hosted-launch.stderr"
launch_status=$?
set -e
printf '%s\n' "$launch_status" >"$evidence_dir/hosted-launch.exit-status"

rpm -q python3-tongs tongs-desktop --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    >"$evidence_dir/installed-previous-nevra.txt"
previous_desktop_evr=$(rpm -q tongs-desktop --queryformat '%{EPOCHNUM}:%{VERSION}-%{RELEASE}')
set +e
dnf upgrade --assumeyes --disablerepo='*' "$final_desktop" \
    >"$evidence_dir/dnf-failed-upgrade.log" 2>&1
failed_upgrade_status=$?
set -e
[[ $failed_upgrade_status -ne 0 ]] || { printf 'dependency-negative upgrade unexpectedly passed\n' >&2; exit 1; }
grep -Eq 'nothing provides|conflicting requests|cannot install' "$evidence_dir/dnf-failed-upgrade.log"
[[ $(rpm -q tongs-desktop --queryformat '%{EPOCHNUM}:%{VERSION}-%{RELEASE}') == "$previous_desktop_evr" ]]
test -x /usr/bin/tongs-desktop

dnf upgrade --assumeyes --setopt=install_weak_deps=False "$final_core" "$final_desktop" \
    2>&1 | tee "$evidence_dir/dnf-upgrade.log"
dnf reinstall --assumeyes --setopt=install_weak_deps=False "$final_core" "$final_desktop" \
    2>&1 | tee "$evidence_dir/dnf-reinstall.log"

python3 "$packaging_dir/verify_install.py" \
    --install-manifest "$prepared_dir/SOURCES/desktop-install.json" \
    --libexec-dir /usr/libexec/tongs-desktop \
    --expected-version "$core_version" \
    --expected-launcher "$prepared_dir/SOURCES/tongs-desktop" \
    --expected-desktop "$prepared_dir/SOURCES/tongs.desktop" \
    --output "$evidence_dir/installed-payload.json"

dnf install --assumeyes --setopt=install_weak_deps=False "$final_mcp" \
    2>&1 | tee "$evidence_dir/dnf-mcp-install.log"
command -v tongs-mcp >"$evidence_dir/tongs-mcp-path.txt"
python3 -E -P - <<'PY' >"$evidence_dir/mcp-plugin.txt"
import mcp.server.fastmcp
from tongs.mcp.plugin import MCPPlugin

commands = MCPPlugin().get_commands()
assert len(commands) == 1 and commands[0][0] == "Start MCP Server"
print("MCP command available with optional dependency")
PY

rpm -qa --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    | sort >"$evidence_dir/rpm-installed.txt"
dnf repoquery --installed --queryformat '%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{from_repo}\n' \
    | sort >"$evidence_dir/installed-packages.txt"
rpm -q --requires python3-tongs >"$evidence_dir/core-requires.txt"
rpm -q --requires 'python3-tongs+mcp' >"$evidence_dir/mcp-requires.txt"
rpm -q --requires tongs-desktop >"$evidence_dir/desktop-requires.txt"
grep -Eq '^python\(abi\) = 3\.[0-9]+$' "$evidence_dir/core-requires.txt"
grep -F 'python3dist(mcp[cli])' "$evidence_dir/mcp-requires.txt"
grep -F 'xorg-x11-server-Xwayland' "$evidence_dir/desktop-requires.txt"
for package in "$final_core" "$final_mcp" "$final_desktop"; do
    rpm -qp --scripts "$package" >>"$evidence_dir/package-scriptlets.txt"
    rpm -qpl "$package" >>"$evidence_dir/owned-files.txt"
done
[[ ! -s "$evidence_dir/package-scriptlets.txt" ]]
rpm -q tongs-desktop --queryformat '[%{FILENAMES}|%{FILEMODES:perms}|%{FILEUSERNAME}|%{FILEGROUPNAME}|%{FILEFLAGS:fflags}\n]' \
    >"$evidence_dir/desktop-file-metadata.txt"
grep -Eq '/usr/libexec/tongs-desktop/chrome-sandbox\|-rwxr-xr-x\|root\|root\|' \
    "$evidence_dir/desktop-file-metadata.txt"

dnf remove --assumeyes 'python3-tongs+mcp' tongs-desktop python3-tongs \
    2>&1 | tee "$evidence_dir/dnf-uninstall.log"
while IFS= read -r owned; do
    [[ -z "$owned" || "$owned" == */ ]] && continue
    [[ ! -e "$owned" && ! -L "$owned" ]] || {
        printf 'owned path remains after uninstall: %s\n' "$owned" >&2
        exit 1
    }
done <"$evidence_dir/owned-files.txt"
grep -Fx 'per-user menu sentinel' /root/.local/share/applications/tongs.desktop
grep -Fx 'user configuration sentinel' /root/.config/tongs/config.toml
grep -Fx 'unrelated sentinel' /tmp/tongs-rpm-unrelated
