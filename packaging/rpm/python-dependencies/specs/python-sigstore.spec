Name:           python-sigstore
Version:        4.5.0
Release:        1%{?dist}
Summary:        Python client for Sigstore signing and verification
License:        Apache-2.0
URL:            https://github.com/sigstore/sigstore-python
Source0:        sigstore-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3dist(flit-core) >= 3.2

%description
Python implementation of Sigstore signing and verification APIs.

%package -n python3-sigstore
Summary:        %{summary}

%description -n python3-sigstore
Python implementation of Sigstore signing and verification APIs.

%prep
%autosetup -n sigstore-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l sigstore

%files -n python3-sigstore -f %{pyproject_files}
%{_bindir}/sigstore

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 4.5.0-1
- Initial source-built companion RPM
