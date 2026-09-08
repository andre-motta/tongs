Name:           python-securesystemslib
Version:        1.5.1
Release:        1%{?dist}
Summary:        Cryptographic support library for secure update systems
License:        MIT AND CC0-1.0
URL:            https://github.com/secure-systems-lab/securesystemslib
Source0:        securesystemslib-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3dist(hatchling) >= 1.31

%description
Cryptographic and general-purpose routines used by TUF and Sigstore.

%package -n python3-securesystemslib
Summary:        %{summary}

%description -n python3-securesystemslib
Cryptographic and general-purpose routines used by TUF and Sigstore.

%prep
%autosetup -n securesystemslib-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l securesystemslib
sed -i '\#securesystemslib/_vendor/ed25519/LICENSE$#d' %{pyproject_files}

%files -n python3-securesystemslib -f %{pyproject_files}
%license %{python3_sitelib}/securesystemslib/_vendor/ed25519/LICENSE

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 1.5.1-1
- Initial source-built companion RPM
