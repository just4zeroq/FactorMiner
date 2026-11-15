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

from core.evaluation.annualisation import DEFAULT_ANNUALISATION


# --------------------------------------------------------------------------- #
# IC time-series helpers
# --------------------------------------------------------------------------- #
def _spearman_row(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rho for one cross-section (ranks via argsort).

    NaN positions are excluded pairwise: a missing factor value or a missing
    return must not pollute the ranks of the *other* asset. The naive
    ``argsort`` implementation ranks NaN as the largest value, which silently
    inflates IC on sparse panels (crypto listing gaps, etc.).
    """
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    a = a[ok]
    b = b[ok]
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
    """One-sided empirical p-value of ``value`` against a null distribution.

    Returns the fraction of null samples that are at least as extreme as
    ``value``. A non-finite ``value`` (e.g. a NaN Newey-West t-statistic when a
    factor panel collapses) cannot reject the null, so it returns ``1.0`` rather
    than a spurious low p-value. This is what stops a ``hac_t=NaN`` candidate
    from **falsely passing** the empirical-null gate (see
    ``docs/reviews/system_code_review.md``, finding F5/C1).
    """
    if not np.isfinite(value):
        return 1.0
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


def multiple_testing_bar(
    n_obs: int,
    n_trials: int,
    annualisation: float = DEFAULT_ANNUALISATION,
    sigma_sr: Optional[float] = None,
) -> Dict[str, float]:
    """The Sharpe a strategy must beat given how many ideas were already tried.

    DSR compares a candidate against ``E[max SR]`` for ``n_trials`` null
    strategies. That number is not a property of the factor — it rises with the
    search budget. Surfacing it turns "why did nothing pass?" into an actionable
    answer: at 787 trials and 610 observations the bar is 2.35 on the old
    252-day convention, or ~2.83 on the 7x24 (365-period) convention that
    crypto daily bars actually use. A strategy with a true annual Sharpe of 1.5
    cannot pass either way.

    ``annualisation`` is reporting-only: the DSR verdict itself compares
    per-period Sharpes and is unaffected by this parameter.
    """
    if n_obs < 2:
        return {
            "n_trials": int(n_trials),
            "n_obs": int(n_obs),
            "bar_per_period_sharpe": float("nan"),
            "bar_annualised_sharpe": float("nan"),
        }
    sigma = (
        float(sigma_sr)
        if sigma_sr is not None and np.isfinite(sigma_sr) and float(sigma_sr) > 0
        else 1.0 / math.sqrt(max(n_obs - 1, 1))
    )
    bar = expected_max_sr(max(1, int(n_trials)), sigma=sigma)
    return {
        "n_trials": int(n_trials),
        "n_obs": int(n_obs),
        "bar_per_period_sharpe": bar,
        "bar_annualised_sharpe": bar * math.sqrt(annualisation),
    }


def deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    n_trials: int = 1,
    sr_benchmark: Optional[float] = None,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    sigma_sr: Optional[float] = None,
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
        # Bailey & Lopez de Prado's sigma is the CROSS-SECTIONAL dispersion of
        # Sharpe ratios ACROSS TRIALS, not the sampling error of one SR
        # (1/sqrt(T-1)). Callers that have the trial population must pass
        # ``sigma_sr``; the legacy sampling-error proxy is kept only as an
        # explicitly marked fallback so old single-factor runs still work.
        sr_sigma = (
            float(sigma_sr)
            if sigma_sr is not None and np.isfinite(sigma_sr) and float(sigma_sr) > 0
            else 1.0 / math.sqrt(max(n_obs - 1, 1))
        )
        sr_benchmark = expected_max_sr(n_trials, sigma=sr_sigma)

    # E-01 confirmed bug: clamping the quadratic INSIDE the square root to
    # 1e-9 made denom ~3.16e-5, so z -> +/-inf and DSR -> 1.0 on exactly the
    # pathological (fat-tailed / implausible) returns that should be rejected.
    # A non-positive variance term is not computable evidence; fail closed.
    var_term = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    if not np.isfinite(var_term) or var_term <= 1e-9:
        return 0.0
    denom = math.sqrt(var_term)
    z = (sharpe - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --------------------------------------------------------------------------- #
# Promotion gate
# --------------------------------------------------------------------------- #
DEFAULT_GATE_CFG = {
    "hac_t_min": 2.0,       # |NW t-stat| threshold (≈95% two-sided)
    "fdr_max": 0.05,        # adjusted p-value ceiling
    "dsr_min": 0.90,        # P(true strategy SR > benchmark) after deflation
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
    # Phase P0.5 — the cost gate is now denominated in *returns*, not IC.
    # ``cost_adj_return`` is the per-period net return of the quantile
    # long-short book after turnover costs (see core/evaluation/portfolio_eval).
    # It must clear this floor, and - optionally - the Newey-West t-stat of
    # that net return series must clear ``ls_t_min`` (0.0 = only the sign and
    # the level are enforced; raise to ~1.5 for a stricter production run).
    "cost_adj_return_min": 0.0,
    "ls_t_min": 0.0,
    # P2: empirical null gate. Until now target_permutation_tstats /
    # random_formula_null / empirical_p existed as library functions with unit
    # tests but were never called by any gate. Set ``empirical_p_max`` (e.g.
    # 0.05) together with ``evaluation.gates.null_n_perm`` to activate: a
    # candidate must beat the |t| distribution obtained by permuting its own
    # targets. None = disabled (backward compatible).
    "empirical_p_max": None,
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
    family_p_values: Optional[Sequence[float]] = None,
    null_tstats: Optional[Sequence[Sequence[float]]] = None,
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

    ``family_p_values`` is the raw p-value of **every hypothesis tested so far**
    (this run plus all previous runs). BH-FDR is then applied to that whole
    family and each candidate reads off its own adjusted value. Correcting only
    the short list — typically 2–6 factors out of a thousand evaluated — barely
    corrects anything, which is why this argument exists.

    ``null_tstats`` carries, per candidate, the empirical |t| samples obtained
    by target permutation (see :func:`target_permutation_tstats`). Supplying it
    computes ``empirical_p`` and enables the ``empirical_p_max`` gate; omitting
    it leaves the gate silent. This is the P2 wiring that turns the null
    functions from untested-in-production helpers into an actual defence.

    Returns one dict per candidate: ``{"p_adj", "dsr", "gate", "status",
    "empirical_p"}``.
    """
    pvals = [float(m.get("p_value", 1.0)) for m in metrics_list]
    # E-01: estimate the cross-trial Sharpe dispersion from the population
    # actually being gated. Bailey-LdP's E[max SR] needs sigma ACROSS trials;
    # a single-factor call (the old re-audit path) has no population, so it
    # falls back to the legacy sampling-error proxy inside deflated_sharpe_ratio.
    sr_population: List[float] = []
    for m in metrics_list:
        try:
            v = float(m.get("sr", np.nan))
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            sr_population.append(v)
    sigma_sr = (
        float(np.std(sr_population, ddof=1))
        if len(sr_population) >= 3 and float(np.std(sr_population, ddof=1)) > 0
        else None
    )
    if family_p_values:
        family = np.sort(np.asarray(list(family_p_values), dtype=float))
        family = family[np.isfinite(family)]
        if family.size:
            adj_sorted = bh_fdr(family)
            padj = []
            for p in pvals:
                idx = int(np.searchsorted(family, p, side="left"))
                idx = min(idx, family.size - 1)
                padj.append(float(adj_sorted[idx]))
        else:
            padj = list(bh_fdr(pvals))
    elif n_trials is not None and int(n_trials) > len(pvals):
        # A4 safety net: callers that gate one factor at a time while declaring
        # a large historical trial count must not collapse the BH family to 1.
        # Pad with p=1.0 for the unobserved historical trials (conservative:
        # it can only make the adjusted p-values larger).
        finite_pvals = [float(p) for p in pvals if np.isfinite(p)]
        family = np.sort(np.asarray(
            finite_pvals + [1.0] * (int(n_trials) - len(finite_pvals)), dtype=float
        ))
        adj_sorted = bh_fdr(family)
        padj = []
        for p in pvals:
            idx = min(int(np.searchsorted(family, p, side="left")), family.size - 1)
            padj.append(float(adj_sorted[idx]))
    else:
        padj = list(bh_fdr(pvals))
    out: List[Dict[str, Any]] = []
    for i, (m, p) in enumerate(zip(metrics_list, padj)):
        emp_p: Optional[float] = None
        if null_tstats is not None and i < len(null_tstats):
            samples = null_tstats[i]
            if samples is not None and len(samples):
                emp_p = empirical_p(abs(float(m.get("hac_t", 0.0))), samples)
        dsr = float(m.get("dsr", 0.0))
        if n_trials is not None and int(n_trials) > 1:
            # Deflate the strategy Sharpe when available; fall back to the IC
            # information ratio for callers that only have that (older runs).
            sharpe = m.get("sr")
            obs = m.get("sr_n_obs")
            if sharpe is None or obs is None:
                sharpe = m.get("ir")
                obs = m.get("n_obs")
            if sharpe is not None and obs is not None and int(obs) >= 2:
                dsr = deflated_sharpe_ratio(
                    sharpe=float(sharpe),
                    n_obs=int(obs),
                    n_trials=int(n_trials),
                    # P1-M9: propagate the net-return skew/kurtosis computed by
                    # portfolio_eval so the gate's deflation matches the
                    # evaluator's (normal defaults are optimistic for fat tails).
                    skewness=float(m.get("net_skew", 0.0) or 0.0),
                    kurtosis=float(m.get("net_kurtosis", 3.0) or 3.0),
                    sigma_sr=sigma_sr,
                )
        gate_input = {
            "hac_t": float(m.get("hac_t", 0.0)),
            "p_adj": float(p),
            "dsr": float(dsr),
            "oos_consistent": bool(m.get("oos_consistent", False)),
            "cost_adj_return": float(m.get("cost_adj_return", 0.0)),
            "ls_t": float(m.get("ls_t", 0.0)),
            "RankIC_oos": float(m.get("RankIC_oos", 0.0)),
            "oos_enabled": bool(m.get("oos_enabled", False)),
            "empirical_p": emp_p,
        }
        # A4: re-audit callers may need per-factor gate config (e.g. factors
        # without an OOS snapshot must not be failed by an OOS-only gate).
        # Allow a per-candidate override while still batching the BH family.
        local_cfg = {**(cfg or {}), **(m.get("_gate_cfg") or {})}
        gate = promotion_gates(gate_input, local_cfg)
        status = "INSPECTED" if gate["pass"] else "DISCOVERED_UNVERIFIED"
        out.append({
            "p_adj": float(p),
            "dsr": float(dsr),
            "gate": gate,
            "status": status,
            "empirical_p": emp_p,
        })
    return out


def promotion_gates(
    metrics: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Decide whether a factor may be promoted to INSPECTED.

    Gates (mirrors the defences found in crypto-factor-lab and
    LLM-Alpha-Mining-Framework):
      * hac_t_significant  : |NW t| of in-sample IC above threshold
      * fdr_pass          : BH-adjusted p <= fdr_max
      * dsr_pass          : deflated Sharpe probability >= dsr_min. The Sharpe
                            being deflated is the *traded book's* net Sharpe
                            (``sr``), not the scale-free IC information ratio,
                            so the test is sensitive to how much the strategy
                            actually earns per unit of risk.
      * oos_consistent    : OOS sign agrees with in-sample
      * cost_adj_positive : net long-short return after turnover costs > 0
      * ls_significant    : NW t of that net return >= ls_t_min (default off)
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
            float(metrics.get("cost_adj_return", 0.0))
            > float(cfg["cost_adj_return_min"])
            if cfg["require_cost_adj_positive"]
            else True
        ),
        # The net long-short return must also be positive in a statistical
        # sense, not just on average. Default 0.0 keeps this silent (the level
        # gate above already enforces the sign); set ls_t_min ~1.5 for a
        # stricter mining run.
        "ls_significant": (
            float(metrics.get("ls_t", 0.0)) >= float(cfg["ls_t_min"])
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
        # P2: empirical "luck" baseline. Silent when the null was not computed
        # (no panels available / feature disabled), so existing runs keep their
        # verdicts. When active, the observed |t| must sit in the tail of the
        # target-permutation distribution for this very factor shape.
        "empirical_null_pass": (
            float(metrics.get("empirical_p", 1.0)) <= float(cfg["empirical_p_max"])
            if (cfg.get("empirical_p_max") is not None
                and metrics.get("empirical_p") is not None)
            else True
        ),
    }
    g["pass"] = all(g.values())
    return g
