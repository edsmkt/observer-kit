from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent


class BuildPy(_build_py):
    """Build package data; product runtime lives under observer_kit/, not skill playbooks."""

    def run(self):
        super().run()


setup(
    cmdclass={"build_py": BuildPy},
    package_data={
        "observer_kit": [
            "assets/*.js",
            "EXPLAIN.md",
        ],
    },
    include_package_data=True,
)
