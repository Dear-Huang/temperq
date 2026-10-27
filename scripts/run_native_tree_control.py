"""Post-primary robustness check: preserve the original ten tree features."""
from __future__ import annotations

import argparse
from pathlib import Path
import time

import run_training_comparison as r


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, choices=(2021, 2022, 2023), required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    registration = args.output / f"registration-{args.year}.json"
    if registration.exists():
        raise ValueError("control attempt already exists")
    r.write(registration, {"purpose": "check whether feature expansion weakened the tree benchmark",
            "status": "supplementary_not_original_preregistered_primary",
            "primary_point_estimates_already_seen": True, "primary_recipe_unchanged": True,
            "tree_input": "original_ten_features_plus_same_six_market_summaries",
            "selection": "same_two_parameter_candidates_500_rounds_60_day_validation",
            "script_sha256": r.digest(Path(__file__)),
            "training_runner_sha256": r.digest(Path(r.__file__)),
            "registered_at": r.pd.Timestamp.now(tz="UTC")})
    r.torch.set_num_threads(2)
    d = r.load_data()
    d["x"] = r.np.concatenate([d["x"][..., :10], d["x"][..., 32:]], axis=-1)
    train, val, full, test = r.split_dates(d["dates"], args.year)
    for seed in (0, 1, 2):
        out = args.output / f"y{args.year}-s{seed}"
        out.mkdir(exist_ok=False)
        started, cpu = time.perf_counter(), time.process_time()
        r.log(f"NATIVE10 START {args.year} seed={seed}")
        pred, vp = r.train_trees(d, train, val, full, test, seed, out)
        # Standard B0 equal-weight portfolio. Do not introduce the new allocator.
        r.evaluate(d, pred, None, test, "B0", seed, args.year, out)
        r.np.savez_compressed(out / "predictions.npz", dates=d["dates"][test].to_numpy(),
            codes=d["codes"].to_numpy(dtype=str), B0=pred)
        r.write(out / "completed.json", {"cpu_seconds": time.process_time()-cpu,
            "wall_seconds": time.perf_counter()-started, "state": "succeeded"})
        r.log(f"NATIVE10 DONE {args.year} seed={seed}")


if __name__ == "__main__":
    main()
