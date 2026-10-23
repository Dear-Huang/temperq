"""Causal factor Ridge reference; training-only scaling and JSON checkpoints."""

from __future__ import annotations

from pathlib import Path
import time

import numpy as np
from .. import daily, experiments


def factor_features(values, valid):
    """Returns, volatility and activity from the same observed window, plus masks."""
    x, m = np.asarray(values), np.asarray(valid)
    if x.ndim != 3 or x.shape != m.shape or x.shape[-1] != 6 or m.dtype != bool or not np.isfinite(x).all():
        raise ValueError("factor reference requires finite OHLCVA windows and masks")
    features, observed = [], []
    for lag in (1, 5, 20):
        ok = (m[:, -1, 3] & m[:, -1 - lag, 3]) if x.shape[1] > lag else np.zeros(len(x), bool)
        value = (x[:, -1, 3] - x[:, -1 - lag, 3]) if x.shape[1] > lag else np.zeros(len(x))
        features.append(np.where(ok, value, 0))
        observed.append(ok)
    logret = np.diff(x[:, -min(21, x.shape[1]):, 3], axis=1)
    okret = m[:, -min(21, x.shape[1]) + 1:, 3] & m[:, -min(21, x.shape[1]):-1, 3]
    n = okret.sum(axis=1)
    mean = np.sum(np.where(okret, logret, 0), axis=1) / np.maximum(n, 1)
    vol = np.sqrt(np.sum(np.where(okret, (logret - mean[:, None]) ** 2, 0), axis=1) / np.maximum(n, 1))
    features.append(np.where(n >= 2, vol, 0))
    observed.append(n >= 2)
    for channel in (4, 5):
        features.append(np.where(m[:, -1, channel], x[:, -1, channel], 0))
        observed.append(m[:, -1, channel])
    return np.column_stack([*features, *observed]).astype(np.float64)


class FactorRidge:
    def __init__(self, *, alpha=10.0, max_seconds=300):
        self.alpha, self.max_seconds = alpha, max_seconds

    def fit(self, x, mask, y, vx, vm, vy, *, context, log=lambda *_: None):
        started = time.monotonic()
        features = factor_features(x, mask)
        self.mean, self.scale = features.mean(axis=0), features.std(axis=0)
        self.scale[self.scale < 1e-12] = 1
        # Reuse the native baseline's sample-scaled regularization convention.
        fitted = daily.ridge_fit((features - self.mean) / self.scale + .5, y, self.alpha)
        self.coef, self.intercept = fitted[1:], float(fitted[0])
        validation_mse = float(np.mean((self.predict(vx, vm) - vy) ** 2))
        if not np.isfinite(validation_mse):
            raise ValueError("nonfinite Ridge validation loss")
        self.fit_record = {"adapter": "ridge_factor_v1", "feature_recipe": "window_r1_r5_r20_vol20_activity_masks_v1",
                           "n_train": len(y), "n_validation": len(vy), "context": context,
                           "validation_mse": validation_mse, "refit_on_validation": False,
                           "l2_penalty": self.alpha * len(y) * 1e-4,
                           "selection": "fixed_predeclared_alpha", "elapsed_seconds": time.monotonic() - started}
        if self.fit_record["elapsed_seconds"] > self.max_seconds:
            raise ValueError("predeclared Ridge training time budget exceeded")
        log(f"Ridge validation_mse={validation_mse:.8g}")
        return self.fit_record

    def predict(self, x, mask):
        result = ((factor_features(x, mask) - self.mean) / self.scale) @ self.coef + self.intercept
        if not np.isfinite(result).all():
            raise ValueError("nonfinite Ridge predictions")
        return result

    def save(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=False)
        experiments.write(root / "model.json", {"schema_version": "factor_ridge_v1", "alpha": self.alpha,
                          "mean": self.mean.tolist(), "scale": self.scale.tolist(),
                          "coef": self.coef.tolist(), "intercept": self.intercept, "fit": self.fit_record})

    @classmethod
    def load(cls, directory):
        meta = experiments.read(Path(directory) / "model.json")
        if meta["schema_version"] != "factor_ridge_v1":
            raise ValueError("unsupported Ridge checkpoint")
        model = cls(alpha=meta["alpha"])
        for name in ("mean", "scale", "coef"):
            setattr(model, name, np.asarray(meta[name], float))
        model.intercept, model.fit_record = meta["intercept"], meta["fit"]
        return model
