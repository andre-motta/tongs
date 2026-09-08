Name:           python-rfc3161-client
Version:        1.0.8
Release:        1%{?dist}
Summary:        Python RFC 3161 timestamp protocol client
License:        Apache-2.0 AND BSD-3-Clause AND MIT AND Unicode-3.0 AND (Apache-2.0 WITH LLVM-exception)
URL:            https://github.com/trailofbits/rfc3161-client
Source0:        rfc3161_client-%{version}.tar.gz
Source1:        rfc3161-client-%{version}-cargo-vendor.tar.gz
Source2:        rfc3161-cargo-inventory.json
Source3:        rfc3161-client-%{version}-cargo-licenses.tar.gz
BuildRequires:  cargo
BuildRequires:  gcc
BuildRequires:  maturin >= 1.7
BuildRequires:  openssl-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-pip

%description
Python API and Rust implementation for RFC 3161 timestamp requests and responses.

%package -n python3-rfc3161-client
Summary:        %{summary}

%description -n python3-rfc3161-client
Python API and Rust implementation for RFC 3161 timestamp requests and responses.

%prep
%autosetup -n rfc3161_client-%{version}
tar -xzf %{SOURCE1}
tar -xzf %{SOURCE3}
sed -i 's/openssl = { version = "0\.10\.80", features = \["vendored"\] }/openssl = "0.10.80"/' rust/Cargo.toml

%build
export CARGO_NET_OFFLINE=true
export OPENSSL_NO_VENDOR=1
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l rfc3161_client
install -d %{buildroot}%{_licensedir}/python3-rfc3161-client/cargo
cp -a cargo-licenses/. %{buildroot}%{_licensedir}/python3-rfc3161-client/cargo/
install -m 0644 %{SOURCE2} \
    %{buildroot}%{_licensedir}/python3-rfc3161-client/cargo-inventory.json

%files -n python3-rfc3161-client -f %{pyproject_files}
%license %{_licensedir}/python3-rfc3161-client

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 1.0.8-1
- Initial source-built companion RPM with Fedora system OpenSSL
