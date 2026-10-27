"""Refine insufficient turnover-only matches; preserve the initial diagnostic."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import time

import diagnose_training_comparison as diag
from run_training_comparison import digest, load_data, write
import numpy as np
import pandas as pd
import torch


def estimates_for(paths):
    rows = []
    for (arm, stress), part in paths.groupby(["arm", "stress_bp"]):
        values = part.groupby(["year", "date"]).net_return.mean().to_numpy()
        eq = np.cumprod(1+values)
        rows.append({"arm":arm, "stress_bp":int(stress),
            "utility_ann":float(252*(np.log1p(values).mean()-5*np.var(values))),
            "cagr":float(eq[-1]**(252/len(values))-1), "volatility_ann":float(values.std()*np.sqrt(252)),
            "mean_turnover":float(part.one_way_turnover.mean()),
            "mean_exposure":float((part.holdings_value/part.equity).mean())})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise ValueError("refinement already completed")
    initial = json.loads((args.diagnostic / "result.json").read_text())
    started, cpu = time.perf_counter(), time.process_time()
    write(args.output / "registration.json", {"source_diagnostic_sha256":digest(args.diagnostic / "result.json"),
        "registered_at":pd.Timestamp.now(tz="UTC"), "script_sha256":digest(Path(__file__)),
        "diagnostic_script_sha256":digest(Path(diag.__file__)), "new_fits":0,
        "rule":"if gap > .002, 17 evenly spaced alphas between adjacent tested neighbors; at most two rounds",
        "selection":"minimum absolute turnover gap, tie larger alpha; returns never select",
        "scope":"posthoc conditional diagnostic, not deployable selection or independent confirmation"})
    shutil.copy2(Path(__file__), args.output / "refinement-source.py")
    shutil.copy2(Path(diag.__file__), args.output / "diagnostic-source.py")
    torch.set_num_threads(2)
    d = load_data()
    if d["hashes"] != json.loads((args.predictions / "data.json").read_text())["hashes"]:
        raise ValueError("source data changed")
    paths = pd.read_parquet(args.diagnostic / "daily-paths.parquet")
    candidate_rows = pd.read_csv(args.diagnostic / "turnover-candidates.csv").to_dict("records")
    matches = []
    for original in initial["matches"]:
        match = dict(original)
        case_name = match["case"]
        if match["absolute_gap"] <= .002:
            matches.append(match)
            continue
        case = args.predictions / case_name
        archive = np.load(case / "predictions.npz", allow_pickle=False)
        test = d["dates"].get_indexer(pd.DatetimeIndex(archive["dates"]))
        pred = archive["B0"][...,0]
        rows = [v for v in candidate_rows if v["case"] == case_name]
        best = min(rows, key=lambda v:(v["absolute_gap"],-v["alpha"]))
        for round_id in range(2):
            alphas = sorted({v["alpha"] for v in rows})
            center = alphas.index(best["alpha"])
            lo = alphas[max(0, center-1)]; hi = alphas[min(len(alphas)-1,center+1)]
            for alpha in np.linspace(lo,hi,17):
                alpha = float(alpha)
                if any(abs(v-alpha)<1e-12 for v in alphas):
                    continue
                targets = diag.smooth_targets(pred,d["u"],d["spread"],test,alpha)
                path,_,_ = diag.replay(d,targets,test,0)
                turnover = float(path.one_way_turnover.iloc[1:].mean())
                row = {"case":case_name,"alpha":alpha,"actual_turnover":turnover,
                    "target_turnover":match["target_turnover"],"absolute_gap":abs(turnover-match["target_turnover"]),
                    "refinement_round":round_id+1}
                rows.append(row); candidate_rows.append(row)
            best = min(rows,key=lambda v:(v["absolute_gap"],-v["alpha"]))
            if best["absolute_gap"] <= .002:
                break
        targets = diag.smooth_targets(pred,d["u"],d["spread"],test,best["alpha"])
        out = args.output / case_name; out.mkdir()
        write(out / "B0-matched-targets.json", {str(d["dates"][t].date()):dict(zip(d["codes"][w>0],w[w>0].tolist())) for t,w in targets.items()})
        paths = paths[~((paths.arm == "B0-matched") & (paths.year == int(case_name[1:5])) & (paths.seed == int(case_name[-1])))]
        for stress in (0,10):
            path,fills,_ = diag.replay(d,targets,test,stress)
            path = path.assign(arm="B0-matched",year=int(case_name[1:5]),seed=int(case_name[-1]),stress_bp=stress)
            path.to_parquet(out / f"B0-matched-slip{stress}-path.parquet",index=False)
            fills.to_parquet(out / f"B0-matched-slip{stress}-fills.parquet",index=False)
            paths = pd.concat([paths,path.iloc[1:]],ignore_index=True)
        match.update(alpha=best["alpha"],absolute_gap=best["absolute_gap"],actual_turnover=best["actual_turnover"],
                     matched_within_tolerance=bool(best["absolute_gap"]<=.002),coarse_gap=original["absolute_gap"])
        matches.append(match)
        diag.r.log(f"REFINED {case_name} alpha={best['alpha']:.6f} gap={best['absolute_gap']:.6f}")
    paths.to_parquet(args.output / "daily-paths.parquet",index=False)
    pd.DataFrame(matches).to_csv(args.output / "turnover-matches.csv",index=False)
    pd.DataFrame(candidate_rows).to_csv(args.output / "turnover-candidates.csv",index=False)
    result = {**initial,"source_diagnostic_sha256":digest(args.diagnostic / "result.json"),
        "estimates":estimates_for(paths),"paired_inference":diag.paired(paths),"matches":matches,
        "all_matches_within_tolerance":all(v["matched_within_tolerance"] for v in matches),
        "refinement_cpu_seconds":time.process_time()-cpu,"refinement_wall_seconds":time.perf_counter()-started,
        "refinement_script_sha256":digest(Path(__file__))}
    write(args.output / "result.json",result)
    pd.DataFrame(result["estimates"]).to_csv(args.output / "comparison.csv",index=False)
    print(json.dumps({"all_matched":result["all_matches_within_tolerance"],"estimates":result["estimates"],
                      "comparisons":result["paired_inference"]["0"]},indent=2))


if __name__ == "__main__":
    main()
