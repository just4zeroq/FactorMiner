from setuptools import setup, find_packages

setup(
    name='factorminer',
    version='4.0.0',
    packages=find_packages(),
    install_requires=[
        # Backtesting engine (core/backtest adapter -> ml4t.backtest).
        # Installed locally as an editable build from the ml4t/backtest repo.
        "ml4t-backtest>=0.1.0",
        "polars>=1.36.1",
    ],
    entry_points={
        'console_scripts': [
            'factorminer=core.cli:main',
        ],
    },
)
