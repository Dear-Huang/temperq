"""Cross-sectional evaluation (SPEC §6, §9).

Daily rank IC, its mean tested with Newey-West (lag fixed in advance, equal
to the label overlap), the minimum detectable effect at that standard error,
and seed / shuffle reference points.
"""

from __future__ import annotations

import warnings

import numpy as np

MIN_NAMES = 20                      # dates with fewer valid pairs are skipped
Z_MDE = 1.96 + 0.84                 # 5% two-sided, 80% power


def _rank_rows(a: np.ndarray) -> np.ndarray:
    """Row-wise average ranks, NaN kept."""
    out = np.full(a.shape, np.nan)
    for t in range(a.shape[0]):
        v = ~np.isnan(a[t])
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
    valid = ~np.isnan(pred) & ~np.isnan(y)
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
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 2 * (lag + 1):
        return {"n": n, "mean": float(np.mean(x)) if n else np.nan, "se": np.nan,
                "t": np.nan, "mde": np.nan, "lag": lag}
    u = x - x.mean()
    s = u @ u / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * (u[k:] @ u[:-k]) / n
    se = float(np.sqrt(max(s, 0.0) / n))
    # a constant series (e.g. no trades at all) has no sampling error to test against
    t = float(x.mean() / se) if se > 0 else np.nan
    return {"n": n, "mean": float(x.mean()), "se": se, "t": t, "mde": Z_MDE * se, "lag": lag}


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
