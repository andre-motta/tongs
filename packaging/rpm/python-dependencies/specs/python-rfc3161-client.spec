Name:           python-rfc3161-client
Version:        1.0.8
Release:        1%{?dist}
Summary:        Python RFC 3161 timestamp protocol client
License:        Apache-2.0
URL:            https://github.com/trailofbits/rfc3161-client
Source0:        rfc3161_client-%{version}.tar.gz
Source1:        rfc3161-client-%{version}-cargo-vendor.tar.gz
BuildRequires:  cargo
BuildRequires:  gcc
BuildRequires:  maturin >= 1.7
BuildRequires:  openssl-devel
BuildRequires:  python3-devel

%description
Python API and Rust implementation for RFC 3161 timestamp requests and responses.

%package -n python3-rfc3161-client
Summary:        %{summary}

%description -n python3-rfc3161-client
Python API and Rust implementation for RFC 3161 timestamp requests and responses.

%prep
%autosetup -n rfc3161_client-%{version}
tar -xzf %{SOURCE1}
sed -i 's/openssl = { version = "0\.10\.80", features = \["vendored"\] }/openssl = "0.10.80"/' rust/Cargo.toml

%build
export CARGO_NET_OFFLINE=true
export OPENSSL_NO_VENDOR=1
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l rfc3161_client

%files -n python3-rfc3161-client -f %{pyproject_files}

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 1.0.8-1
- Initial source-built companion RPM with Fedora system OpenSSL
