Name:           python-sigstore-models
Version:        0.0.6
Release:        1%{?dist}
Summary:        Pydantic data models for Sigstore
License:        MIT
URL:            https://github.com/astral-sh/sigstore-models
Source0:        sigstore_models-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3dist(hatchling) >= 1.31

%description
Typed Pydantic models used by the Sigstore Python verifier.

%package -n python3-sigstore-models
Summary:        %{summary}

%description -n python3-sigstore-models
Typed Pydantic models used by the Sigstore Python verifier.

%prep
%autosetup -n sigstore_models-%{version}
python3 - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text()
text = text.replace(
    'requires = ["uv_build>=0.9.0,<0.10"]\nbuild-backend = "uv_build"',
    'requires = ["hatchling>=1.31,<2"]\nbuild-backend = "hatchling.build"',
)
text += '\n[tool.hatch.build.targets.wheel]\npackages = ["src/sigstore_models"]\n'
path.write_text(text)
PY

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l sigstore_models

%files -n python3-sigstore-models -f %{pyproject_files}

%changelog
* Tue Sep 08 2026 Tongs Desktop Packaging <noreply@openai.com> - 0.0.6-1
- Initial source-built companion RPM
