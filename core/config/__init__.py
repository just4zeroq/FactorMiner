"""Shared configuration defaults and single-source-of-truth constants.

Keeping the canonical values here (instead of re-hardcoding them in every
module that needs them) is what prevents the cost-split drift flagged in
``docs/reviews/system_code_review.md`` (findings A2 / F2): previously the
round-trip cost ``0.0018`` lived in five different files, so changing it in
one place silently broke the identity ``gate_cost_rate == 2 * (commission +
slippage)`` everywhere else.
"""

from core.config.defaults import *  # noqa: F401,F403
