"""Cross-sectional evaluation (SPEC §6, §9).

Daily rank IC, its mean tested with Newey-West (lag fixed in advance, equal
to the label overlap), the minimum detectable effect at that standard error,
and seed / shuffle reference points.
"""

from __future__ import annotations

import warnings
from statistics import NormalDist

import numpy as np

MIN_NAMES = 20                      # dates with fewer valid pairs are skipped
Z_MDE = 1.96 + 0.84                 # 5% two-sided, 80% power


def _rank_rows(a: np.ndarray) -> np.ndarray:
    """Row-wise average ranks, NaN kept."""
    out = np.full(a.shape, np.nan)
    for t in range(a.shape[0]):
        v = np.isfinite(a[t])
        if v.any():
            x = a[t, v]
            order = x.argsort(kind="mergesort")
            r = np.empty(len(x))
            r[order] = np.arange(1, len(x) + 1)
            # average ties
            _, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
            sums = np.bincount(inv, weights=r)
            out[t, v] = (sums / cnt)[inv]
    return out


def rank_ic(pred: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Spearman correlation per row (date) between two T x N arrays."""
    valid = np.isfinite(pred) & np.isfinite(y)
    p = np.where(valid, pred, np.nan)
    q = np.where(valid, y, np.nan)
    rp, rq = _rank_rows(p), _rank_rows(q)
    with warnings.catch_warnings():   # all-NaN dates are expected (warm-up, label tail)
        warnings.simplefilter("ignore", RuntimeWarning)
        rp -= np.nanmean(rp, axis=1, keepdims=True)
        rq -= np.nanmean(rq, axis=1, keepdims=True)
    num = np.nansum(rp * rq, axis=1)
    den = np.sqrt(np.nansum(rp ** 2, axis=1) * np.nansum(rq ** 2, axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = num / den
    ic[valid.sum(axis=1) < MIN_NAMES] = np.nan
    return ic


def newey_west(x: np.ndarray, lag: int) -> dict:
    x = np.asarray(x, dtype=float)
    observed = np.isfinite(x)
    n = int(observed.sum())
    mean = float(x[observed].mean()) if n else np.nan
    if n < 2 * (lag + 1):
        return {"n": n, "mean": mean, "se": np.nan,
                "t": np.nan, "mde": np.nan, "lag": lag}
    # Zero scores at missing dates preserve trading-calendar lag distances.
    u = np.where(observed, x - mean, 0.0)
    s = u @ u / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * (u[k:] @ u[:-k]) / n
    se = float(np.sqrt(max(s, 0.0) / n))
    # a constant series (e.g. no trades at all) has no sampling error to test against
    t = float(mean / se) if se > 0 else np.nan
    return {"n": n, "mean": mean, "se": se, "t": t, "mde": Z_MDE * se, "lag": lag}


def paired_signal_screen(candidate: np.ndarray, references: dict, lag: int) -> dict:
    """Paired historical IC differences; a fixed family, not all-search correction."""
    family = len(references)
    if not family:
        raise ValueError("at least one fixed reference is required")
    z = NormalDist().inv_cdf(1 - 0.05 / (2 * family))
    arrays = [np.asarray(candidate), *(np.asarray(value) for value in references.values())]
    active = np.logical_or.reduce([np.isfinite(value) for value in arrays])
    common = np.logical_and.reduce([np.isfinite(value) for value in arrays])
    complete = bool(np.array_equal(active, common))
    compared = {}
    for name, reference in references.items():
        stat = newey_west(np.asarray(candidate) - np.asarray(reference), lag)
        ci = ([stat["mean"] - z * stat["se"], stat["mean"] + z * stat["se"]]
              if np.isfinite(stat["se"]) else None)
        status = ("INSUFFICIENT" if ci is None else "IMPROVED" if ci[0] > 0
                  else "DEGRADED" if ci[1] < 0 else "NO_CHANGE" if ci == [0, 0]
                  else "INCONCLUSIVE")
        compared[name] = {**stat, "ci95_family": ci, "status": status}
    own = newey_west(candidate, lag)
    positive = np.isfinite(own["se"]) and own["mean"] - 1.96 * own["se"] > 0
    passed = bool(complete and positive and all(v["status"] == "IMPROVED" for v in compared.values()))
    return {"status": "PASS" if passed else "NOT_ESTABLISHED", "candidate": own,
            "comparisons": compared, "family_size": family, "family_alpha": 0.05,
            "daily_ic_coverage_complete": complete, "missing_common_dates": int((active & ~common).sum()),
            "method": "paired_newey_west_normal_bonferroni",
            "scope": "historical_signal_only_not_all_search_or_live_approval"}


def top_overlap(a: np.ndarray, b: np.ndarray, q: float = 0.1) -> float:
    """Mean share of names in both top-q sets, per date."""
    vals = []
    for t in range(a.shape[0]):
        v = ~np.isnan(a[t]) & ~np.isnan(b[t])
        n = int(v.sum())
        k = int(n * q)
        if n < MIN_NAMES or k == 0:
            continue
        ia = set(np.flatnonzero(v)[np.argsort(-a[t, v])[:k]])
        ib = set(np.flatnonzero(v)[np.argsort(-b[t, v])[:k]])
        vals.append(len(ia & ib) / k)
    return float(np.mean(vals)) if vals else np.nan


def shuffle_null(pred: np.ndarray, y: np.ndarray, n: int = 20, seed: int = 0) -> np.ndarray:
    """Mean rank IC of `pred` shuffled within each date, n times."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        s = np.full(pred.shape, np.nan)
        for t in range(pred.shape[0]):
            v = np.flatnonzero(~np.isnan(pred[t]))
            s[t, v] = rng.permutation(pred[t, v])
        out.append(np.nanmean(rank_ic(s, y)))
    return np.array(out)
