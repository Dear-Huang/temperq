"""Command line entry: `qwb validate ...`, `qwb build ...`, `qwb scan ...`."""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import package, recipe, taskcard
from .formats import MARKET_TZ, SPECS, validate_frame


def _read_table(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, dtype={"code": "string", "record_id": "string"})


def _run(path: str, check_only: bool) -> int:
    from pathlib import Path

    from . import runconfig

    p = Path(path).resolve()
    rc, rep = runconfig.resolve(runconfig.load(p), p.parent)
    if rep.issues:
        print(rep)
    if rc is None:
        return 1
    print(f"run {rc['name']}  config {rc['config_hash']}  output {rc['output']}")
    print("deviations from default: " + ("; ".join(rc["deviations"]) or "none"))
    if check_only:
        return 0
    from . import pipeline

    s = pipeline.run(rc, Path.cwd(), log=lambda m: print(m, flush=True))
    f = s["ic"]["lgbm_ensemble"]["final_test"]
    print(f"final test IC {f['mean']:+.4f} (t={f['t']:.2f})  "
          f"{'Go' if s['go'] else 'No-Go'}  report {s['output']}\\report.md")
    return 0


def _bench(args) -> int:
    from pathlib import Path

    from .bench.contracts import create_protocol, object_digest
    from .bench.runner import compare_models, evaluate_model, verify_result

    try:
        if args.bench_cmd == "init":
            p = create_protocol(args.anchor_run, args.output, cwd=Path.cwd(),
                                data_dir=args.data_dir, block_days=args.block_days,
                                resamples=args.resamples, min_days=args.min_days,
                                min_signal_score=args.min_signal_score,
                                min_anchor_gain_score=args.min_anchor_gain_score)
            print(f"development protocol {object_digest(p)}  {args.output}")
            return 0
        if args.bench_cmd == "verify":
            s = verify_result(args.result, inputs=args.inputs)
            print(f"local integrity {s['status']}  checked {s['checked_files']} files  "
                  f"inputs checked {s['inputs_checked']}  daily mean recomputed {s['daily_mean_recomputed']}  "
                  f"account ledger recomputed {s['account_ledger_recomputed']}")
            return 0
        if args.bench_cmd == "compare":
            s = compare_models(args.old, args.new, args.output)
            print(f"signal score gain {s['signal_score_gain']:+.4f}  "
                  f"{s['conclusion']['status']}  report {Path(args.output) / 'report.md'}")
            economic = s["economic_comparison"]
            if economic["status"] == "COMPLETED":
                v = economic["results"]["extra_0bp_per_side"]
                print(f"research net annual gain {v['annual_net_gain_bp']:+.2f} bp  "
                      f"{v['qualification']}  improvement {v['conclusion']['status']}")
            return 0
        s = evaluate_model(args.source, args.protocol, args.output, data_dir=args.data_dir,
                           log=lambda m: print(m, flush=True))
        if s["status"] == "INVALID":
            print("INVALID: " + "; ".join(s["errors"]))
            return 1
        ev = s["evaluation"]
        rank = ev["reference_rank"]
        score = ev["models"]["candidate"]["signal_score"]
        score_text = "N/A" if score is None else f"{score:+.4f}"
        print(f"development signal score {score_text}  "
              f"rank {str(rank['position']) + '/5' if rank else 'N/A'}  "
              f"signal {ev['signal']['status']}  report {Path(args.output) / 'report.md'}")
        print(f"readiness {s['readiness']['level']}  live capital eligible False")
        economic = s["economic"]
        if economic["status"] == "COMPLETED":
            v = economic["results"]["extra_0bp_per_side"]["candidate"]
            print(f"research net annual excess {v['net_excess_ann_bp']:+.2f} bp  "
                  f"CI95 {v['net_excess_ann_ci95_bp']}  {v['qualification']}  "
                  "conditional research only; no impact/capacity certification")
        else:
            print(f"economic NOT_EVALUATED: {economic.get('reason')}")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qwb")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a data table, task card, recipe or manifest")
    v.add_argument("what", choices=sorted(SPECS) + ["task", "recipe", "manifest"])
    v.add_argument("path")
    v.add_argument("--market", choices=sorted(MARKET_TZ),
                   help="interpret naive timestamps in this market's timezone")

    b = sub.add_parser("build", help="copy manifest-listed files into an empty publish dir")
    b.add_argument("src")
    b.add_argument("dest")

    s = sub.add_parser("scan", help="pre-publish scan of a package directory")
    s.add_argument("path")

    sub.add_parser("doctor", help="report environment and the matching torch wheel")

    u = sub.add_parser("ui", help="open the local web UI (127.0.0.1 only)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")

    r = sub.add_parser("run", help="run a whole training/evaluation from one YAML config")
    r.add_argument("config")
    r.add_argument("--check", action="store_true", help="validate and show the resolved config only")

    bench = sub.add_parser("bench", help="development signal benchmark (no retraining)")
    commands = bench.add_subparsers(dest="bench_cmd", required=True)
    init = commands.add_parser("init", help="pin snapshot and LightGBM reference artifacts")
    init.add_argument("anchor_run")
    init.add_argument("--output", required=True, help="new development protocol YAML")
    init.add_argument("--data-dir")
    init.add_argument("--block-days", type=int, default=20)
    init.add_argument("--resamples", type=int, default=10000)
    init.add_argument("--min-days", type=int, default=60)
    init.add_argument("--min-signal-score", type=float, default=0.0,
                      help="predeclare minimum IC score; not a live-capital threshold")
    init.add_argument("--min-anchor-gain-score", type=float, default=0.0,
                      help="predeclare minimum IC score improvement over the anchor")
    model = commands.add_parser("model", help="score a run directory or a prediction parquet/CSV")
    model.add_argument("source")
    model.add_argument("--protocol", required=True)
    model.add_argument("--output", required=True, help="new result directory (never overwritten)")
    model.add_argument("--data-dir", help="relocate the identical hash-verified snapshot")
    compare = commands.add_parser("compare", help="paired comparison of two signal Bench results")
    compare.add_argument("old")
    compare.add_argument("new")
    compare.add_argument("--output", required=True)
    verify = commands.add_parser("verify", help="check local artifacts and recompute mean scores")
    verify.add_argument("result")
    verify.add_argument("--inputs", action="store_true", help="also rehash original data and submissions")

    args = ap.parse_args(argv)
    if args.cmd == "bench":
        return _bench(args)
    if args.cmd == "run":
        return _run(args.config, args.check)
    if args.cmd == "ui":
        from pathlib import Path

        from . import ui
        ui.serve(Path.cwd(), port=args.port, open_browser=not args.no_browser)
        return 0
    if args.cmd == "doctor":
        from . import env
        print("\n".join(env.doctor()))
        return 0
    if args.cmd == "scan":
        rep = package.scan_dir(args.path)
    elif args.cmd == "build":
        rep = package.build(args.src, args.dest)
    elif args.what == "task":
        rep = taskcard.validate(taskcard.load(args.path))
    elif args.what == "recipe":
        rep = recipe.validate(recipe.load(args.path))
    elif args.what == "manifest":
        rep = package.validate_manifest(package.load_manifest(args.path))
    else:
        tz = MARKET_TZ.get(args.market) if args.market else None
        rep = validate_frame(_read_table(args.path), args.what, tz)

    print(rep)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
