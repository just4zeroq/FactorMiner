"""Packaging metadata.

``requirements.txt`` is the single source of truth for dependencies. Notably
``ml4t-backtest`` is NOT published on PyPI: it is installed from a pinned git
commit. An old plain version specifier here made ``pip install -e .`` fail on a
fresh machine, and the short list omitted pandas/numpy/fastapi/ccxt entirely.
"""

from pathlib import Path

from setuptools import find_packages, setup


def read_requirements():
    path = Path(__file__).with_name("requirements.txt")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


setup(
    name='factorminer',
    version='4.0.0',
    packages=find_packages(),
    install_requires=read_requirements(),
    entry_points={
        'console_scripts': [
            'factorminer=core.cli:main',
        ],
    },
)
