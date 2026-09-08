#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --companion-dir DIR --previous-dir DIR --final-dir DIR --previous-repo DIR --final-repo DIR --prepared-dir DIR --evidence-dir DIR --checkout DIR --core-version VERSION\n' "$0" >&2
}
companion_dir=""
previous_dir=""
final_dir=""
previous_repo=""
final_repo=""
prepared_dir=""
evidence_dir=""
checkout=""
core_version=""
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
        --core-version) core_version=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
for directory in "$companion_dir" "$previous_dir" "$final_dir" "$previous_repo" \
    "$final_repo" "$prepared_dir" "$evidence_dir" "$checkout"; do
    [[ -d "$directory" ]] || { usage; exit 2; }
done
[[ $core_version =~ ^[0-9A-Za-z][0-9A-Za-z.+-]*$ ]] || { usage; exit 2; }

packaging_dir="$checkout/packaging/rpm/desktop"
previous_core=$(find "$previous_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
previous_desktop=$(find "$previous_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
final_core=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs-[0-9]*.noarch.rpm' -print -quit)
final_mcp=$(find "$final_dir" -maxdepth 1 -type f -name 'python3-tongs+mcp-*.noarch.rpm' -print -quit)
final_desktop=$(find "$final_dir" -maxdepth 1 -type f -name 'tongs-desktop-*.x86_64.rpm' -print -quit)
previous_test_plugin=$(find "$previous_repo" -maxdepth 1 -type f -name 'tongs-desktop-test-plugin-*.noarch.rpm' -print -quit)
final_test_plugin=$(find "$final_repo" -maxdepth 1 -type f -name 'tongs-desktop-test-plugin-*.noarch.rpm' -print -quit)
for package in "$previous_core" "$previous_desktop" "$previous_test_plugin" \
    "$final_core" "$final_mcp" "$final_desktop" "$final_test_plugin"; do
    [[ -f "$package" ]] || { printf 'missing lifecycle package\n' >&2; exit 1; }
done
companion_contract="$companion_dir/expected-packages.tsv"
[[ -f "$companion_contract" ]] || {
    printf 'missing manifest-bound companion package contract\n' >&2
    exit 1
}
mapfile -t companion_rpms < <(
    find "$companion_dir" -maxdepth 1 -type f -name '*.rpm' -print | sort
)
[[ ${#companion_rpms[@]} -eq 7 ]] || {
    printf 'expected seven selected companion RPMs, found %s\n' \
        "${#companion_rpms[@]}" >&2
    exit 1
}
sort "$companion_contract" >"$evidence_dir/expected-companion-packages.txt"
rpm -qp --queryformat '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' \
    "${companion_rpms[@]}" | sort >"$evidence_dir/selected-companion-packages.txt"
expected_companion_sha=$(sha256sum "$evidence_dir/expected-companion-packages.txt" \
    | cut -d ' ' -f 1)
selected_companion_sha=$(sha256sum "$evidence_dir/selected-companion-packages.txt" \
    | cut -d ' ' -f 1)
[[ "$selected_companion_sha" == "$expected_companion_sha" ]] || {
    printf 'selected companion RPM identities do not match their contract\n' >&2
    exit 1
}
mapfile -t core_python_abis < <(
    rpm -qp --requires "$final_core" \
        | sed -nE 's/^python\(abi\) = (3\.[0-9]+)$/\1/p'
)
[[ ${#core_python_abis[@]} -eq 1 ]] || {
    printf 'expected one exact Python ABI in final core RPM, found %s\n' \
        "${#core_python_abis[@]}" >&2
    exit 1
}
printf '%s\n' "${core_python_abis[0]}" >"$evidence_dir/core-python-abi.txt"

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

user_site="/root/.local/lib/python${core_python_abis[0]}/site-packages"
install -d /root/.local/share/applications /root/.config/tongs \
    /root/.local/share/tongs/desktop/versions/user-archive-sentinel "$user_site"
printf 'per-user menu sentinel\n' >/root/.local/share/applications/tongs.desktop
printf 'user configuration sentinel\n' >/root/.config/tongs/config.toml
printf 'per-user archive sentinel\n' \
    >/root/.local/share/tongs/desktop/versions/user-archive-sentinel/archive.txt
printf 'per-user plugin sentinel\n' >"$user_site/tongs_user_plugin_sentinel.py"
printf 'unrelated sentinel\n' >/tmp/tongs-rpm-unrelated
sentinels=(
    /root/.local/share/applications/tongs.desktop
    /root/.config/tongs/config.toml
    /root/.local/share/tongs/desktop/versions/user-archive-sentinel/archive.txt
    "$user_site/tongs_user_plugin_sentinel.py"
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
sentinel_snapshot() {
    local name=$1
    local sentinel
    {
        for sentinel in "${sentinels[@]}"; do
            [[ -f "$sentinel" && ! -L "$sentinel" ]] || {
                printf 'sentinel is not a regular non-link file: %s\n' "$sentinel" >&2
                return 1
            }
            stat --printf='%n|%F|%a|%u|%g|%s|' "$sentinel"
            sha256sum "$sentinel" | cut -d ' ' -f 1
        done
    } >"$evidence_dir/$name-sentinels.txt"
}
assert_sentinels() {
    local name=$1
    sentinel_snapshot "$name"
    cmp "$evidence_dir/preinstall-sentinels.txt" \
        "$evidence_dir/$name-sentinels.txt"
}
assert_final_state() {
    local name=$1
    local include_mcp=$2
    local arguments=(
        --expected-rpm "$final_core"
        --expected-rpm "$final_desktop"
        --expected-rpm "$final_test_plugin"
    )
    if [[ "$include_mcp" == yes ]]; then
        arguments+=(--expected-rpm "$final_mcp")
    else
        arguments+=(--absent 'python3-tongs+mcp')
    fi
    /usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" assert-installed \
        "${arguments[@]}" --output "$evidence_dir/$name-installed-state.json"
}
assert_previous_state() {
    local name=$1
    /usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" assert-installed \
        --expected-rpm "$previous_core" --expected-rpm "$previous_desktop" \
        --expected-rpm "$previous_test_plugin" --absent 'python3-tongs+mcp' \
        --output "$evidence_dir/$name-installed-state.json"
}
installed_closure() {
    dnf repoquery --installed \
        --queryformat '%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{from_repo}\n' \
        | sort >"$1"
}
dnf_transaction_options=(
    --assumeyes
    --setopt=install_weak_deps=False
    --setopt=tsflags=
)

sentinel_snapshot preinstall
{
    printf '[base /etc/dnf/dnf.conf]\n'
    if [[ -f /etc/dnf/dnf.conf ]]; then
        cat /etc/dnf/dnf.conf
    else
        printf '(missing)\n'
    fi
    printf '[lifecycle transaction options]\n'
    printf '%s\n' "${dnf_transaction_options[@]}"
} >"$evidence_dir/dnf-lifecycle-policy.txt"
grep -Fx -- '--setopt=install_weak_deps=False' \
    "$evidence_dir/dnf-lifecycle-policy.txt"
grep -Fx -- '--setopt=tsflags=' "$evidence_dir/dnf-lifecycle-policy.txt"
dnf repolist --all >"$evidence_dir/install-repositories.txt"
dnf repoquery --repo=tongs-final --available \
    --queryformat '%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{repoid}\n' \
    | sort >"$evidence_dir/final-repository-packages.txt"
[[ $(grep -c '^[^|]*|' "$evidence_dir/final-repository-packages.txt") -eq 11 ]] || {
    printf 'final consumer repository does not contain exactly eleven packages\n' >&2
    exit 1
}
! grep -Eq '^[^|]+-(debuginfo|debugsource)\|' \
    "$evidence_dir/final-repository-packages.txt"
while IFS= read -r identity; do
    [[ $(grep -Fxc "$identity|tongs-final" \
        "$evidence_dir/final-repository-packages.txt") -eq 1 ]] || {
        printf 'final repository lacks exact companion identity: %s\n' "$identity" >&2
        exit 1
    }
done <"$evidence_dir/expected-companion-packages.txt"
for package in python3-tongs 'python3-tongs+mcp' tongs-desktop \
    tongs-desktop-test-plugin; do
    [[ $(cut -d '|' -f 1 "$evidence_dir/final-repository-packages.txt" \
        | grep -Fxc "$package") -eq 1 ]]
done

dnf install "${dnf_transaction_options[@]}" \
    appstream desktop-file-utils diffutils libcap xorg-x11-server-Xvfb \
    xorg-x11-xauth util-linux \
    2>&1 | tee "$evidence_dir/dnf-bootstrap.log"
assert_sentinels after-bootstrap
dnf install "${dnf_transaction_options[@]}" --enablerepo=tongs-final \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-clean-install.log"
assert_sentinels clean-install
assert_final_state clean-final no
snapshot clean-final
installed_closure "$evidence_dir/clean-install-closure.txt"
while IFS= read -r identity; do
    [[ $(grep -Fxc "$identity|tongs-final" \
        "$evidence_dir/clean-install-closure.txt") -eq 1 ]] || {
        printf 'installed companion lacks exact tongs-final provenance: %s\n' \
            "$identity" >&2
        exit 1
    }
done <"$evidence_dir/expected-companion-packages.txt"

if rpm -q 'python3-tongs+mcp' >"$evidence_dir/minimal-mcp-package.txt" 2>&1; then
    printf 'minimal transaction unexpectedly installed python3-tongs+mcp\n' >&2
    exit 1
fi
if rpm -q 'python3-mcp+cli' >"$evidence_dir/minimal-mcp-provider.txt" 2>&1; then
    printf 'minimal transaction unexpectedly installed python3-mcp+cli\n' >&2
    exit 1
fi
if rpm -q --whatprovides 'python3dist(mcp[cli])' \
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
    --install-manifest "$prepared_dir/SOURCES/desktop-install.json" \
    --libexec-dir /usr/libexec/tongs-desktop --checkout-license "$checkout/LICENSE" \
    --test-plugin-module "$packaging_dir/test-plugin/module.mjs" \
    --output "$evidence_dir/package-file-metadata.json"
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" elf \
    --package tongs-desktop --output "$evidence_dir/elf-provider-map.json"
timeout --signal=TERM 20s /usr/bin/python3 -E -P "$packaging_dir/verify_sidecar_plugin.py" \
    --expected-version "$core_version" \
    --expected-module "$packaging_dir/test-plugin/module.mjs" \
    --output "$evidence_dir/sidecar-plugin.ndjson"
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
run_desktop_smoke() {
    local name=$1
    local status
    set +e
    runuser -u tongs-rpm-test -- env XDG_RUNTIME_DIR=/tmp/tongs-rpm-runtime \
        timeout --signal=TERM 12s xvfb-run -a sh -x /usr/bin/tongs-desktop \
        >"$evidence_dir/$name.stdout" 2>"$evidence_dir/$name.stderr"
    status=$?
    set -e
    printf '%s\n' "$status" >"$evidence_dir/$name.exit-status"
    [[ $status -eq 124 ]] || {
        printf 'desktop launch did not remain live for the bounded X11 window: %s\n' \
            "$status" >&2
        exit 1
    }
    grep -F -- 'exec /usr/libexec/tongs-desktop/tongs-desktop --ozone-platform=x11' \
        "$evidence_dir/$name.stderr"
    ! grep -Eiq 'Traceback|ModuleNotFoundError|sidecar failed|(^|[^[:alnum:]_])FATAL([:[:space:]]|$)|ERR_FILE_NOT_FOUND' \
        "$evidence_dir/$name.stdout" "$evidence_dir/$name.stderr"
}
run_desktop_smoke hosted-launch
printf 'This hosted Xvfb smoke proves launcher/runtime liveness only; it makes no hardware GPU claim.\n' \
    >"$evidence_dir/hosted-launch-scope.txt"

snapshot before-mcp
installed_closure "$evidence_dir/mcp-closure-before.txt"
dnf install "${dnf_transaction_options[@]}" --enablerepo=tongs-final \
    'python3-tongs+mcp' 2>&1 | tee "$evidence_dir/dnf-mcp-install.log"
assert_sentinels mcp-install
assert_final_state with-mcp yes
installed_closure "$evidence_dir/mcp-closure-after.txt"
/usr/bin/python3 -E -P "$packaging_dir/verify_mcp_provider.py" \
    --audit "$evidence_dir/fedora-provider-audit.json" \
    --before "$evidence_dir/mcp-closure-before.txt" \
    --after "$evidence_dir/mcp-closure-after.txt" \
    --output "$evidence_dir/mcp-provider-install.json"
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
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" metadata \
    --package python3-tongs --package 'python3-tongs+mcp' \
    --package tongs-desktop --package tongs-desktop-test-plugin \
    --install-manifest "$prepared_dir/SOURCES/desktop-install.json" \
    --libexec-dir /usr/libexec/tongs-desktop --checkout-license "$checkout/LICENSE" \
    --test-plugin-module "$packaging_dir/test-plugin/module.mjs" \
    --output "$evidence_dir/package-file-metadata-with-mcp.json"
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" inventory \
    --package python3-tongs --package 'python3-tongs+mcp' \
    --package tongs-desktop --package tongs-desktop-test-plugin \
    --output "$evidence_dir/owned-path-inventory.json"
dnf remove "${dnf_transaction_options[@]}" 'python3-tongs+mcp' \
    2>&1 | tee "$evidence_dir/dnf-mcp-remove.log"
assert_sentinels mcp-remove
assert_final_state after-mcp-removal no
! command -v tongs-mcp
/usr/bin/tongs --help >"$evidence_dir/post-mcp-tongs-help.txt"
grep -F 'Terminal code review inbox for GitHub and GitLab' \
    "$evidence_dir/post-mcp-tongs-help.txt"
timeout --signal=TERM 20s /usr/bin/python3 -E -P "$packaging_dir/verify_sidecar_plugin.py" \
    --expected-version "$core_version" \
    --expected-module "$packaging_dir/test-plugin/module.mjs" \
    --output "$evidence_dir/post-mcp-sidecar-plugin.ndjson"
run_desktop_smoke post-mcp-hosted-launch
snapshot after-mcp-removal
cmp "$evidence_dir/before-mcp.json" "$evidence_dir/after-mcp-removal.json"

snapshot before-reinstall
dnf reinstall "${dnf_transaction_options[@]}" --enablerepo=tongs-final \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-reinstall.log"
assert_sentinels reinstall
assert_final_state after-reinstall no
snapshot after-reinstall
cmp "$evidence_dir/before-reinstall.json" "$evidence_dir/after-reinstall.json"

dnf remove "${dnf_transaction_options[@]}" \
    tongs-desktop-test-plugin tongs-desktop python3-tongs \
    2>&1 | tee "$evidence_dir/dnf-final-cycle-remove.log"
assert_sentinels final-cycle-remove
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" assert-installed \
    --absent python3-tongs --absent 'python3-tongs+mcp' --absent tongs-desktop \
    --absent tongs-desktop-test-plugin \
    --output "$evidence_dir/final-cycle-remove-installed-state.json"
snapshot after-final-cycle-remove

dnf install "${dnf_transaction_options[@]}" --enablerepo=tongs-previous \
    python3-tongs tongs-desktop tongs-desktop-test-plugin \
    2>&1 | tee "$evidence_dir/dnf-previous-install.log"
[[ $(rpm -q tongs-desktop --queryformat '%{VERSION}') == 0.4.9 ]]
assert_sentinels previous-install
assert_previous_state installed-previous
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
dnf upgrade "${dnf_transaction_options[@]}" --disablerepo='*' \
    "$final_core" "$corrupt_desktop" \
    >"$evidence_dir/dnf-corrupt-upgrade.log" 2>&1
corrupt_upgrade_status=$?
set -e
[[ $corrupt_check_status -ne 0 && $corrupt_upgrade_status -ne 0 ]] || {
    printf 'corrupted higher candidate was not rejected\n' >&2
    exit 1
}
grep -Eiq 'NOT OK|BAD|digest|payload|checksum|does not verify|signature' \
    "$evidence_dir/corrupt-rpm-check.log"
grep -Eiq 'digest|payload|checksum|does not verify|signature|corrupt' \
    "$evidence_dir/dnf-corrupt-upgrade.log"
printf 'rpm -K: package integrity rejection\ndnf: package integrity rejection\n' \
    >"$evidence_dir/corrupt-negative-classification.txt"
assert_sentinels corrupt-failure
assert_previous_state after-corrupt-failure
snapshot after-corrupt-failure
cmp "$evidence_dir/installed-previous.json" "$evidence_dir/after-corrupt-failure.json"

set +e
dnf upgrade "${dnf_transaction_options[@]}" --disablerepo='*' "$final_desktop" \
    >"$evidence_dir/dnf-failed-upgrade.log" 2>&1
failed_upgrade_status=$?
set -e
[[ $failed_upgrade_status -ne 0 ]] || {
    printf 'dependency-negative upgrade unexpectedly passed\n' >&2
    exit 1
}
grep -Eiq 'nothing provides|conflicting requests|cannot install|problem with installed package' \
    "$evidence_dir/dnf-failed-upgrade.log"
rpm -K "$final_desktop" >"$evidence_dir/exact-dependency-rpm-check.log" 2>&1
grep -Eiq 'digests signatures OK|digests OK|signature' \
    "$evidence_dir/exact-dependency-rpm-check.log"
printf 'rpm -K: candidate integrity accepted\ndnf: exact core dependency rejection\n' \
    >"$evidence_dir/exact-dependency-negative-classification.txt"
assert_sentinels dependency-failure
assert_previous_state after-dependency-failure
snapshot after-dependency-failure
cmp "$evidence_dir/installed-previous.json" "$evidence_dir/after-dependency-failure.json"

dnf upgrade "${dnf_transaction_options[@]}" --enablerepo=tongs-final \
    python3-tongs tongs-desktop \
    2>&1 | tee "$evidence_dir/dnf-upgrade.log"
[[ $(rpm -q tongs-desktop --queryformat '%{VERSION}') == 0.5.0 ]]
assert_sentinels successful-upgrade
assert_final_state upgraded-final no
snapshot upgraded-final
cmp "$evidence_dir/clean-final.json" "$evidence_dir/upgraded-final.json"
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
    # RPM's --filetriggers output includes package and transaction file triggers.
    for query in scripts triggers filetriggers; do
        result="$evidence_dir/$(basename "$package").$query.txt"
        rpm -qp --"$query" "$package" >"$result"
        [[ ! -s "$result" ]] || {
            printf 'built RPM unexpectedly contains %s: %s\n' "$query" "$package" >&2
            exit 1
        }
    done
done

dnf remove "${dnf_transaction_options[@]}" \
    tongs-desktop-test-plugin tongs-desktop python3-tongs \
    2>&1 | tee "$evidence_dir/dnf-uninstall.log"
assert_sentinels final-uninstall
/usr/bin/python3 -E -P "$packaging_dir/verify_rpm_state.py" assert-absent \
    --inventory "$evidence_dir/owned-path-inventory.json" \
    --package python3-tongs --package 'python3-tongs+mcp' \
    --package tongs-desktop --package tongs-desktop-test-plugin \
    --output "$evidence_dir/final-uninstall-absence.json"
snapshot after-uninstall
