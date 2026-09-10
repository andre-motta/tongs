from __future__ import annotations

from setuptools import setup
from wheel.bdist_wheel import bdist_wheel


class LinuxX64Wheel(bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        return ("py3", "none", "linux_x86_64")


setup(
    cmdclass={"bdist_wheel": LinuxX64Wheel},
    include_package_data=True,
    package_data={"tongs_electron_prototype": ["runtime/**/*"]},
)
