"""Reproducibility levels R1/R2 for packaged LightGBM models (SPEC §12).

The consistency sample is synthetic (uniform feature ranks), so it can be
shipped with any package regardless of the training data's license.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

TOL = 1e-6


def make_sample(model_files: list[Path], features: list[str], n: int = 256,
                seed: int = 12345) -> dict:
    import lightgbm as lgb

    X = np.random.default_rng(seed).uniform(0, 1, (n, len(features))).astype("float32")
    expected = {}
    for p in model_files:
        b = lgb.Booster(model_file=str(p))
        expected[p.name] = b.predict(X, num_threads=1).tolist()
    return {"features": features, "seed": seed, "X": X.tolist(), "expected": expected,
            "tolerance": TOL}


def check(package_dir: str | Path, sample_path: str = "consistency/sample.json") -> dict:
    """R1: every model loads; R2: predictions on the sample match within tolerance."""
    import lightgbm as lgb

    root = Path(package_dir)
    s = json.loads((root / sample_path).read_text(encoding="utf-8"))
    X = np.asarray(s["X"], dtype="float32")
    out = {"R1": True, "R2": True, "models": {}}
    for name, exp in s["expected"].items():
        try:
            b = lgb.Booster(model_file=str(root / "models" / name))
        except Exception as e:  # a model that does not load fails R1
            out["R1"] = out["R2"] = False
            out["models"][name] = f"load failed: {type(e).__name__}"
            continue
        if b.feature_name() != s["features"]:
            out["R2"] = False
            out["models"][name] = "feature names differ"
            continue
        diff = float(np.max(np.abs(b.predict(X, num_threads=1) - np.asarray(exp))))
        ok = diff <= s["tolerance"]
        out["R2"] &= ok
        out["models"][name] = f"max |diff| = {diff:.2e}"
    return out
