"""Statistical significance & multiple-testing correction — Phase 0.3/0.4/0.5.

Complements :mod:`core.evaluation.experiment_trace` with the *analytical* side of
the statistical gate:

* Newey-West HAC t-stat of the IC time series (autocorrelation-robust — crypto
  factor ICs are heavily autocorrelated, so a plain t-stat is wildly optimistic).
* Benjamini-Hochberg FDR and Holm step-down corrections over the trial set.
* Target-permutation and random-formula nulls -> empirical "luck" baselines.
* Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014), with the multiple-
  testing penalty folded into the benchmark SR.
* A single ``promotion_gates`` that the director can call to decide whether a
  factor is allowed to become INSPECTED.

Every function is dependency-light (numpy only) and unit-tested.

Grounded in the two rigorous prototypes from the survey:
* ``LLM-Alpha-Mining-Framework`` — NW HAC t + BH-FDR + Romano-Wolf + random
  syntax-pool max|t| null.
* ``crypto-factor-lab`` — cost-adjusted positivity + OOS consistency as part of
  the promotion condition.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np


# --------------------------------------------------------------------------- #
# IC time-series helpers
# --------------------------------------------------------------------------- #
def _spearman_row(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rho for one cross-section (ranks via argsort)."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    if len(a) < 3:
        return 0.0
    ar = a.argsort().argsort().astype(float)
    br = b.argsort().argsort().astype(float)
    da = ar - ar.mean()
    db = br - br.mean()
    denom = math.sqrt(float((da ** 2).sum() * (db ** 2).sum()))
    return float((da * db).sum() / denom) if denom > 0 else 0.0


def ic_series(factor_panel: np.ndarray, returns_panel: np.ndarray) -> np.ndarray:
    """Per-period cross-sectional Spearman IC.

    Both inputs are (T, N) panels aligned on the same (time, asset) axes.
    Returns a length-T vector of per-timestamp ICs.
    """
    factor_panel = np.asarray(factor_panel, float)
    returns_panel = np.asarray(returns_panel, float)
    if factor_panel.shape != returns_panel.shape:
        raise ValueError("factor_panel and returns_panel must share shape")
    T = factor_panel.shape[0]
    out = np.empty(T, dtype=float)
    for t in range(T):
        out[t] = _spearman_row(factor_panel[t], returns_panel[t])
    return out


# --------------------------------------------------------------------------- #
# Newey-West HAC t-stat
# --------------------------------------------------------------------------- #
def _acov(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, float) - np.asarray(x, float).mean()
    n = len(x)
    if lag >= n:
        return 0.0
    return float(np.sum(x[lag:] * x[: n - lag]) / n)


def newey_west_tstat(series: Sequence[float], max_lags: Optional[int] = None) -> float:
    """HAC t-stat of the mean of ``series`` (Newey & West, 1987/1994).

    Used to test whether the mean per-period IC is significantly non-zero without
    being fooled by the strong autocorrelation typical of crypto factors.
    """
    x = np.asarray(series, float)
    x = x[np.isfinite(x)]
    T = len(x)
    if T < 4:
        return 0.0
    mean = float(x.mean())
    if max_lags is None:
        # Newey-West (1994) heuristic bandwidth.
        max_lags = int(math.floor(4 * (T / 100.0) ** (2.0 / 9.0)))
    max_lags = max(1, min(max_lags, T - 1))
    s = _acov(x, 0)
    for lag in range(1, max_lags + 1):
        w = 1.0 - lag / (max_lags + 1.0)
        s += 2.0 * w * _acov(x, lag)
    var_mean = s / T
    if var_mean <= 0:
        return 0.0
    return float(mean / math.sqrt(var_mean))


def tstat_ic(factor_panel: np.ndarray, returns_panel: np.ndarray,
             max_lags: Optional[int] = None) -> float:
    """Newey-West HAC t-stat of the IC time series for a factor panel."""
    return newey_west_tstat(ic_series(factor_panel, returns_panel), max_lags=max_lags)


# --------------------------------------------------------------------------- #
# Multiple-testing corrections
# --------------------------------------------------------------------------- #
def bh_fdr(pvals: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (q-values), monotone-enforced."""
    p = np.clip(np.asarray(pvals, float), 0.0, 1.0)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    adj = ranked * n / np.arange(1, n + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out = np.empty(n, dtype=float)
    out[order] = np.clip(adj, 0.0, 1.0)
    return out


def holm(pvals: Sequence[float]) -> np.ndarray:
    """Holm step-down adjusted p-values (FWER control)."""
    p = np.clip(np.asarray(pvals, float), 0.0, 1.0)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    adj = ranked * (n - np.arange(n))
    adj = np.maximum.accumulate(adj)
    out = np.empty(n, dtype=float)
    out[order] = np.clip(adj, 0.0, 1.0)
    return out


# --------------------------------------------------------------------------- #
# Null distributions
# --------------------------------------------------------------------------- #
def target_permutation_tstats(
    factor_panel: np.ndarray,
    returns_panel: np.ndarray,
    n_perm: int = 100,
    seed: int = 0,
) -> np.ndarray:
    """Empirical |t| distribution under *target permutation* for one factor.

    Shuffling the return time-series breaks the factor->return link, so the
    resulting |t| are what pure luck produces for this factor shape. A factor
    whose real |t| sits above the bulk of this distribution is not explained by
    chance.
    """
    rng = np.random.default_rng(seed)
    T = returns_panel.shape[0]
    out = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        perm = rng.permutation(T)
        out[i] = abs(newey_west_tstat(ic_series(factor_panel, returns_panel[perm])))
    return out


def random_formula_null(
    n_formulas: int,
    sampler: Callable[[], Any],
    evaluator: Callable[[Any], np.ndarray],
    returns_panel: np.ndarray,
    n_perm_per: int = 0,
    seed: int = 0,
) -> np.ndarray:
    """Max |t| over ``n_formulas`` *random valid* formulas — the "search luck" baseline.

    ``sampler`` returns a factor expression; ``evaluator(expr)`` returns its
    (T, N) value panel. This reproduces the random-syntax-pool max|t| null from
    ``LLM-Alpha-Mining-Framework``: what is the best |t| you can expect to hit by
    randomly generating ``n_formulas`` expressions while *searching*.

    If ``n_perm_per > 0``, each formula's |t| is further maxed over that many
    target permutations (more conservative).
    """
    rng = np.random.default_rng(seed)
    T = returns_panel.shape[0]
    out = np.empty(n_formulas, dtype=float)
    for i in range(n_formulas):
        expr = sampler()
        panel = np.asarray(evaluator(expr), float)
        if panel.shape != returns_panel.shape:
            out[i] = 0.0
            continue
        t = abs(newey_west_tstat(ic_series(panel, returns_panel)))
        if n_perm_per and n_perm_per > 0:
            best = t
            for _ in range(n_perm_per):
                perm = rng.permutation(T)
                best = max(best, abs(newey_west_tstat(ic_series(panel, returns_panel[perm]))))
            out[i] = best
        else:
            out[i] = t
    return out


def empirical_p(value: float, null_samples: Sequence[float]) -> float:
    """One-sided empirical p-value of ``value`` against a null distribution."""
    null = np.asarray(null_samples, float)
    if null.size == 0:
        return 1.0
    return float(np.mean(null >= value))


# --------------------------------------------------------------------------- #
# Deflated Sharpe Ratio
# --------------------------------------------------------------------------- #
def expected_max_sr(n_trials: int, sr_0: float = 0.0, sigma: float = 1.0) -> float:
    """Expected max SR among ``n_trials`` independent random strategies.

    Used as the benchmark SR in the DSR so that multiple testing is penalised.
    For ``n_trials`` iid unit-normals the expected maximum is ~``sqrt(2 ln n)``
    (extreme-value approximation); ``sr_0`` shifts the centre and ``sigma`` the
    spread. With a single trial there is no search penalty.
    """
    if n_trials <= 1:
        return float(sr_0)
    return float(sr_0 + sigma * math.sqrt(2.0 * math.log(n_trials)))


def deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    n_trials: int = 1,
    sr_benchmark: Optional[float] = None,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Probability that the true SR exceeds the (multiple-testing-aware) benchmark.

    Bailey & Lopez de Prado (2014). ``sr_benchmark`` defaults to
    ``expected_max_sr(n_trials)`` **scaled to the sampling dispersion of a
    per-period Sharpe estimate** (``1/sqrt(n_obs-1)``). Without that scaling the
    benchmark would live on a different unit than ``sharpe`` (e.g. a daily IC IR
    of 0.2 vs a benchmark of ~3.8 for 1200 trials), which would reject every
    factor for purely dimensional reasons.
    """
    if n_obs < 2:
        return 0.0
    if sr_benchmark is None:
        sr_sigma = 1.0 / math.sqrt(max(n_obs - 1, 1))
        sr_benchmark = expected_max_sr(n_trials, sigma=sr_sigma)
    denom = math.sqrt(
        max(1e-9, 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2)
    )
    if denom <= 0:
        return 0.0
    z = (sharpe - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --------------------------------------------------------------------------- #
# Promotion gate
# --------------------------------------------------------------------------- #
DEFAULT_GATE_CFG = {
    "hac_t_min": 2.0,       # |NW t-stat| threshold (≈95% two-sided)
    "fdr_max": 0.05,        # adjusted p-value ceiling
    "dsr_min": 0.90,        # P(true SR > benchmark)
    "require_oos_consistent": True,
    "require_cost_adj_positive": True,
    # Phase 1.5: a factor may only be INSPECTED if it KEEPS real predictive
    # power out-of-sample. Before this gate, a factor that looked great in-sample
    # (oriented) but collapsed OOS could still slip through, because the other
    # hard gates (cost_adj_positive / dsr) are evaluated on the in-sample split
    # and oos_consistent only checks the sign, not the magnitude. This is the
    # exact root cause that let 6 OOS-losing factors be promoted before.
    # The gate is SILENT unless a test_period was actually held out
    # (oos_enabled), so it is backward-compatible with in-sample-only runs.
    "require_oos_strength": True,
    "oos_rankic_min": 0.02,  # min |RankIC_oos| to retain promotion eligibility
}


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (error-function based, no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def pvalue_from_t(t: float) -> float:
    """Two-sided p-value of a (HAC) t-statistic under the normal null.

    The Newey-West t-stat is asymptotically standard normal, so this is the
    per-factor raw p-value. The *family-wise* correction (BH-FDR / Holm) is
    applied later across the whole trial set in :func:`apply_promotion_gates`.
    """
    return float(2.0 * (1.0 - _norm_cdf(abs(float(t)))))


# --------------------------------------------------------------------------- #
# Batch verdict — the gate the director calls
# --------------------------------------------------------------------------- #
def apply_promotion_gates(
    metrics_list: Sequence[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    n_trials: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Run the full promotion gate across a *batch* of candidate metrics.

    This is the Phase 1 bridge between the per-factor primitives and the
    director's promotion decision:

    1. BH-FDR corrects every candidate's raw ``p_value`` for the number of
       factors tested together (multiple-testing control — the missing piece
       that let 6 OOS-losing factors slip through before).
    2. Each factor's ``hac_t`` / ``p_adj`` / ``dsr`` / ``oos_consistent`` /
       ``cost_adj_return`` are fed to :func:`promotion_gates`.
    3. ``lifecycle_status`` is set to ``INSPECTED`` only when every gate is
       green; otherwise ``DISCOVERED_UNVERIFIED`` (still stored, just not
       promoted).

    ``n_trials`` is the *cumulative* number of candidates tried so far
    (ExperimentTrace + current run). When supplied, DSR is recomputed with that
    denominator so the deflation bar reflects the real search effort; when
    omitted the caller's precomputed ``dsr`` is used unchanged.

    Returns one dict per candidate: ``{"p_adj", "dsr", "gate", "status"}``.
    """
    pvals = [float(m.get("p_value", 1.0)) for m in metrics_list]
    padj = bh_fdr(pvals)
    out: List[Dict[str, Any]] = []
    for m, p in zip(metrics_list, padj):
        dsr = float(m.get("dsr", 0.0))
        if n_trials is not None and int(n_trials) > 1:
            ir = m.get("ir")
            n_obs = m.get("n_obs")
            if ir is not None and n_obs is not None and int(n_obs) >= 2:
                dsr = deflated_sharpe_ratio(
                    sharpe=float(ir),
                    n_obs=int(n_obs),
                    n_trials=int(n_trials),
                )
        gate_input = {
            "hac_t": float(m.get("hac_t", 0.0)),
            "p_adj": float(p),
            "dsr": float(dsr),
            "oos_consistent": bool(m.get("oos_consistent", False)),
            "cost_adj_return": float(m.get("cost_adj_return", 0.0)),
            "RankIC_oos": float(m.get("RankIC_oos", 0.0)),
            "oos_enabled": bool(m.get("oos_enabled", False)),
        }
        gate = promotion_gates(gate_input, cfg)
        status = "INSPECTED" if gate["pass"] else "DISCOVERED_UNVERIFIED"
        out.append({"p_adj": float(p), "dsr": float(dsr), "gate": gate, "status": status})
    return out


def promotion_gates(
    metrics: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Decide whether a factor may be promoted to INSPECTED.

    Gates (mirrors the defences found in crypto-factor-lab and
    LLM-Alpha-Mining-Framework):
      * hac_t_significant  : |NW t| of in-sample IC above threshold
      * fdr_pass          : BH-adjusted p <= fdr_max
      * dsr_pass          : deflated Sharpe probability >= dsr_min
      * oos_consistent    : OOS sign agrees with in-sample
      * cost_adj_positive : return after costs still positive
    """
    cfg = {**DEFAULT_GATE_CFG, **(cfg or {})}
    oos_eval = bool(metrics.get("oos_enabled", False))
    g = {
        "hac_t_significant": abs(float(metrics.get("hac_t", 0.0))) > cfg["hac_t_min"],
        "fdr_pass": float(metrics.get("p_adj", 1.0)) <= cfg["fdr_max"],
        "dsr_pass": float(metrics.get("dsr", 0.0)) >= cfg["dsr_min"],
        "oos_consistent": bool(metrics.get("oos_consistent", False))
        if cfg["require_oos_consistent"]
        else True,
        "cost_adj_positive": (
            float(metrics.get("cost_adj_return", 0.0)) > 0
            if cfg["require_cost_adj_positive"]
            else True
        ),
        # Silent unless a test_period was actually held out; otherwise a factor
        # mined without OOS evaluation would be wrongly blocked. When active it
        # demands the factor retain >= oos_rankic_min |RankIC| out-of-sample.
        "oos_strength": (
            abs(float(metrics.get("RankIC_oos", 0.0))) >= cfg["oos_rankic_min"]
            if (cfg["require_oos_strength"] and oos_eval)
            else True
        ),
    }
    g["pass"] = all(g.values())
    return g
