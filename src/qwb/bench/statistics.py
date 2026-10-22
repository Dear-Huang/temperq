"""Paired circular moving-block bootstrap over the original trading-date grid."""

from __future__ import annotations

import numpy as np


def block_summary(values, *, block_days: int, resamples: int, seed: int,
                  min_days: int, family_size: int = 1) -> list[dict]:
    x = np.asarray(values, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or not len(x) or np.isinf(x).any():
        raise ValueError("bootstrap requires a nonempty date x series array without infinity")
    if block_days < 1 or resamples < 100 or min_days < 2 or family_size < 1:
        raise ValueError("invalid bootstrap settings")
    n = np.isfinite(x).sum(axis=0)
    means = np.nansum(x, axis=0) / np.maximum(n, 1)
    enough = n >= max(min_days, 4 * block_days)
    draws = np.full((resamples, x.shape[1]), np.nan)
    if enough.any():
        rng = np.random.default_rng(seed)
        blocks = (len(x) + block_days - 1) // block_days
        # Share the same sampled dates across models and paired differences.
        for start in range(0, resamples, 128):
            size = min(128, resamples - start)
            origins = rng.integers(0, len(x), size=(size, blocks))
            indices = ((origins[:, :, None] + np.arange(block_days)) % len(x))
            sample = x[indices.reshape(size, -1)[:, :len(x)]]
            count = np.isfinite(sample).sum(axis=1)
            avg = np.nansum(sample, axis=1) / np.maximum(count, 1)
            draws[start:start + size] = np.where(count > 0, avg, np.nan)
    out = []
    for j in range(x.shape[1]):
        finite = draws[:, j][np.isfinite(draws[:, j])]
        ok = bool(enough[j] and len(finite) == resamples)
        item = {
            "n": int(n[j]), "mean": float(means[j]) if n[j] else None,
            "ci95": np.quantile(finite, [0.025, 0.975]).tolist() if ok else None,
            "ci_reason": None if ok else "insufficient_dates_or_valid_blocks",
        }
        if family_size > 1:
            tail = 0.025 / family_size
            item["simultaneous_ci95"] = np.quantile(finite, [tail, 1 - tail]).tolist() if ok else None
            item["family_size"] = family_size
            item["adjustment"] = "bonferroni_percentile_block_bootstrap"
        out.append(item)
    return out


def conclusion(summary: dict, threshold: float = 0.0) -> dict:
    ci = summary["ci95"]
    if ci is None:
        return {"status": "INCONCLUSIVE", "reason": summary["ci_reason"]}
    status = "PASS" if ci[0] > threshold else "FAIL" if ci[1] <= threshold else "INCONCLUSIVE"
    return {"status": status, "reason": "paired_time_block_interval", "threshold": threshold}
