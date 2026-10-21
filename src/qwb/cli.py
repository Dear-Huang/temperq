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

    r = sub.add_parser("run", help="run a whole training/evaluation from one YAML config")
    r.add_argument("config")
    r.add_argument("--check", action="store_true", help="validate and show the resolved config only")

    args = ap.parse_args(argv)
    if args.cmd == "run":
        return _run(args.config, args.check)
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
