Name:           python-sigstore-rekor-types
Version:        0.0.18
Release:        1%{?dist}
Summary:        Python models for Rekor API types
License:        Apache-2.0
URL:            https://github.com/trailofbits/sigstore-rekor-types
Source0:        sigstore_rekor_types-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3dist(setuptools) >= 75

%description
Python data models for the Rekor transparency log API.

%package -n python3-sigstore-rekor-types
Summary:        %{summary}

%description -n python3-sigstore-rekor-types
Python data models for the Rekor transparency log API.

%prep
%autosetup -n sigstore_rekor_types-%{version}
sed -i 's/"pydantic\[email\] >=2,<3",/"pydantic >=2,<3", "email-validator >=2",/' pyproject.toml

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l rekor_types

%files -n python3-sigstore-rekor-types -f %{pyproject_files}

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 0.0.18-1
- Initial source-built companion RPM
