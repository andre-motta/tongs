Name:           tongs-desktop-test-plugin
Version:        1.0.0
Release:        1%{?dist}
Summary:        Test-only installed plugin for Tongs desktop RPM validation
License:        MIT
Source0:        tongs_rpm_test_plugin.py
Source1:        module.mjs
Source2:        LICENSE

BuildArch:      noarch
BuildRequires:  python3-devel >= 3.12
Requires:       python3-tongs

%description
This package exists only inside the disposable issue 52 lifecycle proof. It
validates discovery, packaged resource loading, and invocation through the
installed Tongs desktop sidecar.

%prep

%build

%install
install -Dm 0644 %{SOURCE0} %{buildroot}%{python3_sitelib}/tongs_rpm_test_plugin.py
install -Dm 0644 %{SOURCE1} \
    %{buildroot}%{python3_sitelib}/tongs_rpm_test_plugin_assets/assets/module.mjs
install -Dm 0644 /dev/null \
    %{buildroot}%{python3_sitelib}/tongs_rpm_test_plugin_assets/__init__.py
dist_info=%{buildroot}%{python3_sitelib}/tongs_desktop_test_plugin-%{version}.dist-info
install -d "$dist_info"
cat >"$dist_info/METADATA" <<EOF
Metadata-Version: 2.1
Name: tongs-desktop-test-plugin
Version: %{version}
EOF
cat >"$dist_info/entry_points.txt" <<EOF
[tongs.desktop_plugins]
rpm-test = tongs_rpm_test_plugin:RPMTestPlugin
EOF
install -Dm 0644 %{SOURCE2} \
    %{buildroot}%{_licensedir}/tongs-desktop-test-plugin/LICENSE

%files
%pycached %{python3_sitelib}/tongs_rpm_test_plugin.py
%dir %{python3_sitelib}/tongs_rpm_test_plugin_assets
%pycached %{python3_sitelib}/tongs_rpm_test_plugin_assets/__init__.py
%dir %{python3_sitelib}/tongs_rpm_test_plugin_assets/assets
%{python3_sitelib}/tongs_rpm_test_plugin_assets/assets/module.mjs
%{python3_sitelib}/tongs_desktop_test_plugin-%{version}.dist-info
%license %{_licensedir}/tongs-desktop-test-plugin/LICENSE
