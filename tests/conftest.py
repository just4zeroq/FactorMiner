"""Test-suite isolation for global experiment state.

The portable hypothesis-budget snapshot is intentionally project-wide in
production (one committable file). Under pytest it must not leak between tests
or pick up a developer's real budget, otherwise ``n_trials`` assertions become
machine-dependent. ``FACTORMINER_BUDGET_PATH`` is honoured by
``core.experiment.budget``.

The same applies to the two auditable ledgers that live in ``user_workspace/``
and are otherwise shared across every test run (and with the developer's real
working tree): the holdout ledger (``FACTORMINER_HOLDOUT_LEDGER_PATH``) and the
universe ledger (``FACTORMINER_UNIVERSE_LEDGER_PATH``). Redirecting all three
to ``tmp_path`` stops tests from (a) mutating the real ledgers and (b) leaking
cumulative state into each other — the pollution risk flagged in
``docs/reviews/system_code_review.md`` (finding T3).
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_global_ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FACTORMINER_BUDGET_PATH", str(tmp_path / ".hypothesis_budget.json")
    )
    monkeypatch.setenv(
        "FACTORMINER_HOLDOUT_LEDGER_PATH", str(tmp_path / ".holdout_ledger.json")
    )
    monkeypatch.setenv(
        "FACTORMINER_UNIVERSE_LEDGER_PATH", str(tmp_path / ".universe_ledger.json")
    )
