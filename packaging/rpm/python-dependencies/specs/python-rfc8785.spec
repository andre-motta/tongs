Name:           python-rfc8785
Version:        0.1.4
Release:        1%{?dist}
Summary:        Python implementation of RFC 8785
License:        Apache-2.0
URL:            https://github.com/trailofbits/rfc8785.py
Source0:        rfc8785-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3dist(flit-core) >= 3.5

%description
Pure Python JSON Canonicalization Scheme implementation.

%package -n python3-rfc8785
Summary:        %{summary}

%description -n python3-rfc8785
Pure Python JSON Canonicalization Scheme implementation.

%prep
%autosetup -n rfc8785-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l rfc8785

%files -n python3-rfc8785 -f %{pyproject_files}

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 0.1.4-1
- Initial source-built companion RPM
