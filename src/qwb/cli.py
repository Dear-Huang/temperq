"""Command line entry: `qwb validate ...`, `qwb build ...`, `qwb scan ...`."""

from __future__ import annotations

import argparse
import re
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
    from . import experiments, worker
    from .jobs import JobSpec, Store

    store = Store(Path.cwd() / "runs" / "jobs")
    jid = store.submit(JobSpec("train", {"action": "train", "resolved": rc,
                                        "ui": {"name": rc["name"], "what": "train"}}))
    print(f"job {jid}", flush=True)
    if worker.run_job(Path.cwd(), jid, echo=True) != "succeeded":
        print(f"run failed: {store.status(jid).get('error')}", file=sys.stderr)
        return 1
    out = Path((store.root / jid / "artifacts" / "output.txt").read_text(encoding="utf-8"))
    s = experiments.read(out / "metrics.json")
    if s.get("engine") == "e2e_utility_v1":
        print(f"{s['evidence_status']}: utility folds {s['folds']}, seeds {s['seeds']}; "
              f"proxy comparison eligible {s['comparison_eligible']}; economic efficacy not established; report {out / 'report.md'}")
        if "outer_evaluation_status" in s:
            print(f"outer evaluation {s['outer_evaluation_status']}; exact failures {s['outer_exact_failures']}; "
                  f"continuous failures {s['outer_continuous_failures']}")
        return 0
    if s.get("engine") == "e2e_prediction_v1":
        stats = s["prediction_metrics"]
        print(f"{s['evidence_status']}: {stats['n_predictions']} historical predictions, "
              f"{stats['n_evaluable']} evaluable; economic efficacy not evaluated; report {out / 'report.md'}")
        return 0
    f = s["ic"][s.get("model_key", "lgbm_ensemble")]["final_test"]
    print(f"final test IC {f['mean']:+.4f} (t={f['t']:.2f})  "
          f"{'Go' if s['go'] else 'No-Go'}  report {out / 'report.md'}")
    return 0


def _bench(args) -> int:
    from pathlib import Path

    from .bench.contracts import create_protocol, object_digest
    from .bench.runner import compare_models, evaluate_model, verify_result

    try:
        if args.bench_cmd in {"allocate", "allocation-verify"}:
            from .portfolio import prediction

            if args.bench_cmd == "allocation-verify":
                result = prediction.verify(args.result, data_dir=args.data_dir, inputs=args.inputs)
                print(f"{result['status']} {result['scope']} {result['evidence_status']}")
                return 0
            result = prediction.evaluate(args.run, args.protocol, args.data_dir, args.config, args.output)
            print(f"{result['status']} {result.get('evidence_status', '')}; "
                  f"explicit failures: {result.get('failed_decisions', 'n/a')}; {result.get('error', '')}")
            return 0 if result["status"] == "COMPLETED" else 1
        if args.bench_cmd in {"targets", "target-verify", "target-fixture"}:
            from .bench import targets

            if args.bench_cmd == "target-fixture":
                print(targets.create_fixture(args.output, lifecycle=args.lifecycle))
                return 0
            if args.bench_cmd == "target-verify":
                result = targets.verify(args.result, inputs=args.inputs)
                print(f"{result['status']} {result['scope']} {result['evidence_status']}")
                return 0
            result = targets.evaluate(args.protocol, args.submission, args.output)
            print(f"{result['status']} {result.get('evidence_status', '')} {result.get('error', '')}")
            return 0 if result["status"] == "COMPLETED" else 1
        if args.bench_cmd == "init":
            p = create_protocol(args.anchor_run, args.output, cwd=Path.cwd(),
                                data_dir=args.data_dir, block_days=args.block_days,
                                resamples=args.resamples, min_days=args.min_days,
                                min_signal_score=args.min_signal_score,
                                min_anchor_gain_score=args.min_anchor_gain_score)
            print(f"development protocol {object_digest(p)}  {args.output}")
            return 0
        if args.bench_cmd == "traps":
            from .bench import traps
            s = traps.run()
            for r in s["pairs"]:
                print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['pair']:<40} caught={r['caught']} false_alarm={r['false_alarm']}")
            print(f"trap set {s['n']} pairs  catch rate {s['catch_rate']:.0%}  false alarm rate {s['false_alarm_rate']:.0%}")
            return 0 if s["all_pass"] else 1
        if args.bench_cmd == "verify":
            s = verify_result(args.result, inputs=args.inputs, integrity_only=args.integrity_only)
            print(f"local integrity {s['status']}  checked {s['checked_files']} files  "
                  f"inputs checked {s['inputs_checked']}  daily mean recomputed {s['daily_mean_recomputed']}  "
                  f"account ledger recomputed {s['account_ledger_recomputed']}  scope {s['scope']}")
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
        s = evaluate_model(args.source, args.protocol, args.output, data_dir=args.data_dir, workdir=Path.cwd(),
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


def _runs(args) -> int:
    import json
    from pathlib import Path

    from . import evidence_export, experiments

    try:
        if args.runs_cmd == "list":
            rows = experiments.list_runs(Path.cwd())
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['run_id']}  {row['state']:<10}  {row['name']}  {row['model']}  {row['created_at']}")
            return 0
        if args.runs_cmd == "import":
            row = experiments.import_legacy(Path(args.source), Path.cwd())
            print(f"imported {row['run_id']}  {row['archive_dir']}  origin {row['origin']}")
            return 0
        path = Path(args.run)
        if experiments.ID_RE.fullmatch(args.run):
            path = experiments.run_dir(Path.cwd(), args.run)
        if args.runs_cmd == "verify":
            row = experiments.verify(path)
            print(f"local artifact integrity PASS  run_id {row['run_id']}  {len(row['artifacts'])} files")
            return 0
        payload = evidence_export.build(path, Path(args.evaluation) if args.evaluation else None)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(payload)
        print(f"evidence-only bundle {output}  {len(payload)} bytes; author-submitted local research")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"runs: {exc}", file=sys.stderr)
        return 1


def _jobs(args) -> int:
    import json
    from pathlib import Path

    from . import worker
    from .jobs import Store

    store = Store(Path.cwd() / "runs" / "jobs")
    try:
        if args.jobs_cmd == "list":
            print(json.dumps(store.list(), ensure_ascii=False, indent=2))
            return 0
        if not re.fullmatch(r"[0-9a-f]{12}", args.job):
            raise ValueError("invalid job id")
        if args.jobs_cmd == "status":
            print(json.dumps(store.status(args.job), ensure_ascii=False, indent=2))
        elif args.jobs_cmd == "cancel":
            store.cancel(args.job)
        elif args.jobs_cmd == "recover":
            store.recover(args.job)
        else:
            if store.spec(args.job)["config"].get("action") not in worker.ACTIONS:
                raise ValueError("only serialized training/evaluation jobs support this command")
            if args.jobs_cmd == "retry":
                store.recover(args.job)
                store.retry(args.job)
            state = worker.run_job(Path.cwd(), args.job, echo=True)
            print(f"job {args.job}  {state}")
            return 0 if state == "succeeded" else 1
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"jobs: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="temperq")
    sub = ap.add_subparsers(dest="cmd", required=True)
    from . import paper_cli
    paper_cli.add_parser(sub)

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

    e2e = sub.add_parser("e2e", help="portfolio research contract and numerical acceptance")
    ec = e2e.add_subparsers(dest="e2e_cmd", required=True)
    fixture = ec.add_parser("fixture", help="create an explicitly synthetic PIT bundle and prediction config")
    fixture.add_argument("--output", required=True, help="new directory")
    gate = ec.add_parser("check", help="run synthetic account/state and solver-gradient checks")
    gate.add_argument("--output", help="new evidence directory; never overwritten")
    utility_verify = ec.add_parser("utility-verify", help="recompute utility-run policies and independent accounts")
    utility_verify.add_argument("run")
    utility_verify.add_argument("--data-dir")
    utility_verify.add_argument("--inputs", action="store_true")

    runs = sub.add_parser("runs", help="list, verify, import or export fixed training versions")
    rc = runs.add_subparsers(dest="runs_cmd", required=True)
    listing = rc.add_parser("list")
    listing.add_argument("--json", action="store_true")
    importing = rc.add_parser("import", help="copy legacy training artifacts into history without rewriting the source")
    importing.add_argument("source")
    checking = rc.add_parser("verify")
    checking.add_argument("run", help="run id or training directory")
    exporting = rc.add_parser("export", help="export an evidence-only zip; no data, weights or local paths")
    exporting.add_argument("run", help="run id or training directory")
    exporting.add_argument("--evaluation", help="Bench result directory bound to this run")
    exporting.add_argument("--output", required=True)

    jobs = sub.add_parser("jobs", help="inspect, cancel or retry local persistent jobs")
    jc = jobs.add_subparsers(dest="jobs_cmd", required=True)
    jc.add_parser("list")
    for command in ("status", "cancel", "recover", "start", "retry"):
        item = jc.add_parser(command)
        item.add_argument("job")

    bench = sub.add_parser("bench", help="development signal benchmark (no retraining)")
    commands = bench.add_subparsers(dest="bench_cmd", required=True)
    allocate = commands.add_parser("allocate", help="allocate a frozen intraday prediction run through causal risk/cost recipes")
    allocate.add_argument("run")
    allocate.add_argument("--protocol", required=True)
    allocate.add_argument("--data-dir", required=True)
    allocate.add_argument("--config", required=True)
    allocate.add_argument("--output", required=True)
    allocation_verify = commands.add_parser("allocation-verify", help="recompute causal allocation and its independent account")
    allocation_verify.add_argument("result")
    allocation_verify.add_argument("--data-dir")
    allocation_verify.add_argument("--inputs", action="store_true")
    target_fixture = commands.add_parser("target-fixture", help="create a synthetic raw-share target submission")
    target_fixture.add_argument("--output", required=True)
    target_fixture.add_argument("--lifecycle", action="store_true", help="include versioned dividends, receivables and split accounting")
    target = commands.add_parser("targets", help="replay target weights in an independent raw-share account")
    target.add_argument("submission")
    target.add_argument("--protocol", required=True)
    target.add_argument("--output", required=True)
    target_verify = commands.add_parser("target-verify", help="recompute target account arithmetic")
    target_verify.add_argument("result")
    target_verify.add_argument("--inputs", action="store_true")
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
    commands.add_parser("traps", help="run the platform trap set (planted mistakes vs clean cases)")
    verify = commands.add_parser("verify", help="check local artifacts and recompute mean scores")
    verify.add_argument("result")
    verify.add_argument("--inputs", action="store_true", help="also rehash original data and submissions")
    verify.add_argument("--integrity-only", action="store_true",
                        help="check stored hashes/bindings only, without protocol acceptance or arithmetic")

    args = ap.parse_args(argv)
    if args.cmd == "paper":
        return paper_cli.run(args)
    if args.cmd == "e2e":
        import json
        from .portfolio.acceptance import run as check_e2e

        try:
            if args.e2e_cmd == "fixture":
                from .observations.fixture import create

                print(create(args.output))
                return 0
            if args.e2e_cmd == "utility-verify":
                from .training.utility_runner import verify

                result = verify(args.run, data_dir=args.data_dir, inputs=args.inputs)
            else:
                result = check_e2e(args.output)
        except (ValueError, OSError) as exc:
            print(f"e2e: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if args.cmd == "runs":
        return _runs(args)
    if args.cmd == "jobs":
        return _jobs(args)
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
