Name:           python-tuf
Version:        7.0.1
Release:        1%{?dist}
Summary:        The Update Framework reference implementation
License:        Apache-2.0 OR MIT
URL:            https://github.com/theupdateframework/python-tuf
Source0:        tuf-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3dist(hatchling) >= 1.31

%description
Python reference implementation of The Update Framework.

%package -n python3-tuf
Summary:        %{summary}

%description -n python3-tuf
Python reference implementation of The Update Framework.

%prep
%autosetup -n tuf-%{version}
sed -i 's/hatchling==1\.32\.0/hatchling>=1.31,<2/' pyproject.toml

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l tuf

%files -n python3-tuf -f %{pyproject_files}

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 7.0.1-1
- Initial source-built companion RPM
