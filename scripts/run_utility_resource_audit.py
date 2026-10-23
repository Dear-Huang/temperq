"""Sequential, process-isolated resource measurements of actual GRU utility fits."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import itertools
import math
import os
from pathlib import Path
import platform
import signal
import statistics
import subprocess
import sys
import time

from qwb import experiments


SCHEMA = "utility_resource_audit_v1"
THREAD_ENV = {key: "1" for key in
              ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")}


def configuration(*, assets=(24, 64, 128), steps=(3, 12), detach=(None, 2), repeats=2,
                  epochs=1, max_seconds=120., wall_seconds=180., pretrain_max_samples=50000):
    values = {"assets": list(assets), "steps": list(steps), "detach": list(detach),
              "repeats": repeats, "epochs": epochs, "max_seconds": max_seconds,
              "wall_seconds": wall_seconds, "pretrain_max_samples": pretrain_max_samples}
    for key, minimum in (("assets", 2), ("steps", 1)):
        if (not values[key] or len(set(values[key])) != len(values[key])
                or any(type(value) is not int or value < minimum for value in values[key])):
            raise ValueError(f"{key} must contain unique integers >= {minimum}")
    if (not values["detach"] or len(set(values["detach"])) != len(values["detach"])
            or any(value is not None and (type(value) is not int or value < 1) for value in values["detach"])):
        raise ValueError("detach must contain unique positive integers or null")
    for key in ("repeats", "epochs", "pretrain_max_samples"):
        if type(values[key]) is not int or values[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("max_seconds", "wall_seconds"):
        if type(values[key]) not in (int, float) or not math.isfinite(values[key]) or values[key] <= 0:
            raise ValueError(f"{key} must be positive and finite")
    if wall_seconds <= max_seconds:
        raise ValueError("wall_seconds must exceed the fit time budget")
    return values


def cases(config):
    return [{"id": f"a{a:04d}-t{n:04d}-k{'full' if k is None else k}-r{r:02d}",
             "assets": a, "steps": n, "detach_every": k, "repeat": r}
            for a, n, k, r in itertools.product(config["assets"], config["steps"], config["detach"],
                                               range(1, config["repeats"] + 1))]


def check_implementation(task):
    if (task["implementation_hash"] != experiments.code_identity()
            or task["script_hash"] != experiments.digest(Path(__file__))):
        raise ValueError("resource audit implementation or script changed")


def prepare_source(task, root):
    from qwb import pipeline, runconfig
    from qwb.observations.fixture import create

    config_path = create(root / "bundle", days=task["days"], assets=task["assets"], seed=17)
    raw = runconfig.load(config_path)
    raw["observations"] = {"lookback": 60, "min_history": 20}
    raw["model"].update(epochs=1, hidden_size=8)
    raw["folds"]["last_test"] = raw["folds"]["first_test"]
    raw["train"].update(max_samples=task["config"]["pretrain_max_samples"], max_seconds=120.)
    resolved, report = runconfig.resolve(raw, config_path.parent)
    if resolved is None:
        raise ValueError(str(report))
    output = pipeline.run(resolved, root, log=lambda *_: None)
    record = experiments.verify(Path(output["output"]))
    return {"source_run": output["output"], "source_run_id": record["run_id"],
            "source_artifact_hash": record["artifact_hash"], "bundle": str(config_path.parent),
            "data_content_hash": record["data_content_hash"]}


def fit_inputs(task, bundle):
    from qwb.observations.bundle import session_time
    from qwb.portfolio.recipes import CostRecipe, RiskRecipe
    from qwb.portfolio.reference import Fees
    from qwb.training.utility_fit import DecisionTrainingConfig
    from run_allocation_demo import synthetic_protocol

    config = task["config"]
    validation_start = 80 + max(config["steps"])
    protocols = {phase: synthetic_protocol(bundle,
        [str(session_time(day, "15:30")) for day in bundle.calendar[start:start + length]],
        max_weight=.4, min_cash=.05, initial_holdings=True)
        for phase, start, length in (("train", 80, task["steps"]), ("validation", validation_start, 3))}
    context = {"fit_cutoff": protocols["validation"]["sessions"][0]["decision_time"],
               "selection_cutoff": str(session_time(bundle.calendar[validation_start + 4], "15:30"))}
    policy = {"risk": asdict(RiskRecipe(20, 10, .2)),
              "cost": asdict(CostRecipe(10, 10, "2020-01-01T00:00:00+08:00")),
              "optimizer": {"max_weight": .4, "min_cash": .05, "stability_penalty": .02},
              "fees": Fees(**protocols["train"]["fees"]), "min_history": 20,
              "production_policy": "torch_box_budget_turnover_dust_v1", "dust_threshold": 1e-10}
    training = DecisionTrainingConfig(epochs=config["epochs"], patience=config["epochs"],
        max_seconds=config["max_seconds"], max_episode_steps=max(3, *config["steps"]),
        detach_every=task["detach_every"], cpu_threads=1, seed=0)
    return protocols, context, policy, training


def run_fit(task, root):
    from qwb.observations.bundle import PITBundle
    from qwb.portfolio.continuous import ContinuousConfig
    from qwb.training.decision import DecisionGRU
    from qwb.training.utility_fit import fit, process_peak_memory
    import torch

    source = task["source"]
    bundle = PITBundle(source["bundle"], lookback=60)
    if (bundle.identity["content_hash"] != source["data_content_hash"]
            or bundle.universe["code"].nunique() != task["assets"]):
        raise ValueError("resource source bundle changed")
    model = DecisionGRU.from_run(source["source_run"], fold=0, seed=0, bundle=bundle)
    if model.source["artifact_hash"] != source["source_artifact_hash"]:
        raise ValueError("resource pretraining archive changed")
    protocols, context, policy, training = fit_inputs(task, bundle)
    for name, protocol in protocols.items():
        experiments.write(root / f"{name}.json", protocol)
    experiments.write(root / "fit-inputs.json", {"context": context, "training": asdict(training),
                                                "policy": {**policy, "fees": asdict(policy["fees"])}})
    baseline_memory = process_peak_memory()
    history = []

    def log(entry):
        if isinstance(entry, dict):
            history.append(entry)
            experiments.write(root / "history.json", {"history": history})

    record = fit(model, bundle, protocols["train"], protocols["validation"], policy_config=policy,
                 context=context, config=training, continuous=ContinuousConfig(), log=log)
    model.save(root / "checkpoint")
    restored = DecisionGRU.load(root / "checkpoint")
    if any(not torch.equal(a, b) for a, b in zip(model.net.parameters(), restored.net.parameters(), strict=True)):
        raise ValueError("resource checkpoint reload differs")
    return {"fit": record, "baseline_process_memory": baseline_memory,
            "checkpoint_reload_equal": True, "source": source}


def child(task_path):
    from qwb.training.utility_fit import process_peak_memory
    import torch

    task_path = Path(task_path).resolve()
    task, root = experiments.read(task_path), task_path.parent
    started = time.monotonic()
    result = {"state": "failed", "task_hash": experiments.digest(task_path),
              "pid": os.getpid(), "parent_pid": os.getppid()}
    try:
        check_implementation(task)
        torch.set_num_threads(1)
        payload = prepare_source(task, root) if task["kind"] == "prepare" else run_fit(task, root)
        check_implementation(task)
        result.update(state="succeeded", **payload)
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}")
        if hasattr(exc, "qwb_training_failure"):
            result["failure_context"] = exc.qwb_training_failure
    finally:
        result.update(elapsed_inside_child_seconds=time.monotonic() - started, memory=process_peak_memory(),
                      torch_num_threads=torch.get_num_threads(), thread_environment={key: os.getenv(key) for key in THREAD_ENV})
        experiments.write(root / "child-result.json", result)
    return 0 if result["state"] == "succeeded" else 1


def terminate_tree(process):
    # Windows venv launchers can have a separate interpreter below them.
    if sys.platform == "win32":
        killed = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=15, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        if killed.returncode and process.poll() is None:
            process.kill()
            process.wait(timeout=15)
            raise RuntimeError("cannot confirm child process-tree termination")
        method = "windows_taskkill_tree"
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        method = "posix_dedicated_process_group"
    process.wait(timeout=15)
    return {"method": method, "launcher_exit_code": process.returncode}


def execute_process(command, log, *, wall_seconds):
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, **THREAD_ENV}, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        start_new_session=sys.platform != "win32")
    result = {"state": "exited", "launcher_pid": process.pid}
    try:
        result["exit_code"] = process.wait(timeout=wall_seconds)
    except subprocess.TimeoutExpired:
        result.update(state="timed_out", termination=terminate_tree(process), exit_code=None,
                      error="child wall-clock budget exceeded; process tree killed and reaped")
    except BaseException:
        if process.poll() is None:
            terminate_tree(process)
        raise
    result["wall_elapsed_seconds"] = time.monotonic() - started
    return result


def run_child(task_path, *, wall_seconds):
    task_path = Path(task_path).resolve()
    with (task_path.parent / "process.log").open("wb") as log:
        result = execute_process([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                                  "--child-task", str(task_path)], log, wall_seconds=wall_seconds)
        if result["state"] != "timed_out":
            result["state"] = "failed"
            path = task_path.parent / "child-result.json"
            if path.exists():
                result["child"] = experiments.read(path)
                if result["exit_code"] == 0 and result["child"]["state"] == "succeeded":
                    result["state"] = "succeeded"
                else:
                    result["error"] = result["child"].get("error", "child did not succeed")
            else:
                result["error"] = "child exited without a measurement result; peak memory unavailable"
    return result


def summarize(plan, attempts, root):
    import torch
    from qwb.training.decision import DecisionGRU

    expected = {row["id"]: row for row in plan["cases"]}
    if len(expected) != len(plan["cases"]) or set(attempts) != set(expected):
        raise ValueError("resource measurement coverage differs from frozen plan")
    grouped = {}
    for name, spec in expected.items():
        row = attempts[name]
        if row["state"] not in {"succeeded", "failed", "timed_out", "not_run", "interrupted"}:
            raise ValueError("resource attempt is not terminal")
        key = f"a{spec['assets']}-t{spec['steps']}-k{spec['detach_every']}"
        group = grouped.setdefault(key, {"assets": spec["assets"], "steps": spec["steps"],
            "detach_every": spec["detach_every"], "attempts": [], "state_counts": {}, "successes": []})
        group["attempts"].append(name)
        group["state_counts"][row["state"]] = group["state_counts"].get(row["state"], 0) + 1
        if row["state"] == "succeeded":
            measured = row["child"]
            if (row["exit_code"] != 0 or measured["state"] != "succeeded"
                    or measured["task_hash"] != experiments.digest(root / "cases" / name / "task.json")):
                raise ValueError("resource successful child identity differs")
            group["successes"].append(name)
    for group in grouped.values():
        completed = group.pop("successes")
        group["success_fraction_of_all_declared"] = len(completed) / len(group["attempts"])
        group["successful_measurements"] = len(completed)
        group["conditional_success_ranges"] = {}
        for field in ("wall_elapsed_seconds", "fit_elapsed_seconds", "peak_rss_bytes"):
            values = []
            for name in completed:
                row = attempts[name]
                value = (row["wall_elapsed_seconds"] if field == "wall_elapsed_seconds" else
                         row["child"]["fit"]["elapsed_seconds"] if field == "fit_elapsed_seconds" else
                         row["child"]["memory"]["peak_rss_bytes"])
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid positive finite resource measurement")
                values.append(value)
            group["conditional_success_ranges"][field] = ({"min": min(values), "median": statistics.median(values),
                                                            "max": max(values)} if values else None)
        group["repeated_parameters_equal"] = None
        if len(completed) >= 2:
            first = DecisionGRU.load(root / "cases" / completed[0] / "checkpoint").net.state_dict()
            group["repeated_parameters_equal"] = all(all(torch.equal(value, other[key]) for key, value in first.items())
                for other in (DecisionGRU.load(root / "cases" / name / "checkpoint").net.state_dict()
                              for name in completed[1:]))
    return {"schema_version": SCHEMA, "groups": grouped,
            "status": "COMPLETED" if all(row["state"] == "succeeded" for row in attempts.values()) else "DIAGNOSTIC_WITH_FAILURES",
            "total_declared": len(attempts), "success_count": sum(row["state"] == "succeeded" for row in attempts.values()),
            "evidence_status": "SYNTHETIC_ONLY", "economic_efficacy": "NOT_ESTABLISHED",
            "scope": "fresh sequential child processes; process high-water RSS includes imports and model loading; no GPU or memory-bound BPTT claim",
            "aggregation": "ranges conditional on successful measurements; all failures/not_run remain in denominator; not a capacity certification"}


def compute(output, config):
    from qwb.portfolio.prediction import environment

    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = configuration(**config)
    identity = {"implementation_hash": experiments.code_identity(), "script_hash": experiments.digest(Path(__file__))}
    plan = {"schema_version": SCHEMA, "created_at": experiments.now(), **identity, "config": config,
            "cases": cases(config), "environment": environment(), "machine": {"cpu_count": os.cpu_count(),
            "processor": platform.processor()}, "controller_pid": os.getpid(), "thread_environment": THREAD_ENV,
            "pretraining_budget": {"epochs": 1, "fit_seconds": 120., "wall_seconds": max(180., config["wall_seconds"])},
            "pretraining": "one verified one-epoch prediction run per asset pool; fold 0 seed 0; no hand-set heads",
            "limits": "CPU synthetic measurements only; no concurrent audit children; no hardware-exclusive claim; whole-audit rerun only"}
    experiments.write(root / "intent.json", plan)
    sources = {str(a): {"state": "queued"} for a in config["assets"]}
    attempts = {row["id"]: {"state": "queued"} for row in plan["cases"]}
    try:
        for a in config["assets"]:
            directory = root / "sources" / f"a{a:04d}"
            directory.mkdir(parents=True)
            task = {**identity, "kind": "prepare", "assets": a, "days": max(110, 90 + max(config["steps"])), "config": config}
            experiments.write(directory / "task.json", task)
            sources[str(a)] = {"state": "running"}
            experiments.write(root / "preparation.json", sources)
            sources[str(a)] = run_child(directory / "task.json", wall_seconds=max(180., config["wall_seconds"]))
            experiments.write(root / "preparation.json", sources)
            print(f"pretraining assets={a}: {sources[str(a)]['state']}", flush=True)
        plan["sources"] = sources
        experiments.write(root / "plan.json", plan)
        for spec in plan["cases"]:
            name, source = spec["id"], sources[str(spec["assets"])]
            if source["state"] != "succeeded":
                attempts[name] = {"state": "not_run", "error": "declared source pretraining did not succeed"}
            else:
                directory = root / "cases" / name
                directory.mkdir(parents=True)
                task = {**identity, **spec, "kind": "fit", "config": config,
                        "source": {key: source["child"][key] for key in
                                   ("source_run", "source_run_id", "source_artifact_hash", "bundle", "data_content_hash")}}
                experiments.write(directory / "task.json", task)
                attempts[name] = {"state": "running"}
                experiments.write(root / "attempts.json", attempts)
                attempts[name] = run_child(directory / "task.json", wall_seconds=config["wall_seconds"])
            experiments.write(root / "attempts.json", attempts)
            print(f"{name}: {attempts[name]['state']}", flush=True)
        check_implementation(plan)
        result = summarize(plan, attempts, root)
        experiments.write(root / "result.json", result)
        lines = ["# Isolated utility-fit resource audit", "", result["scope"], "", result["aggregation"], "",
                 "| Assets | Train steps | Detach | Success/declared | Median fit seconds | Median process RSS bytes |",
                 "| ---: | ---: | --- | ---: | ---: | ---: |"]
        for group in result["groups"].values():
            ranges = group["conditional_success_ranges"]
            duration, memory = ranges["fit_elapsed_seconds"], ranges["peak_rss_bytes"]
            lines.append(f"| {group['assets']} | {group['steps']} | {group['detach_every']} | "
                f"{group['successful_measurements']}/{len(group['attempts'])} | "
                f"{duration['median'] if duration else None} | {memory['median'] if memory else None} |")
        lines += ["", "No market efficacy, supported capacity limit, GPU support or memory bounded by truncation is established.",
                  "Child startup/wall time, fit time, import baseline and process high-water RSS are distinct measurements."]
        (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return result
    except BaseException as exc:
        for row in [*sources.values(), *attempts.values()]:
            if row["state"] in {"queued", "running"}:
                row.update(state="not_run" if row["state"] == "queued" else "interrupted", error=str(exc))
        experiments.write(root / "result.json", {"status": "INVALID_OR_INTERRUPTED", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        experiments.write(root / "preparation.json", sources)
        experiments.write(root / "attempts.json", attempts)
        experiments.write(root / "checksums.json", experiments.inventory(root))


def verify(directory):
    from qwb.observations.bundle import PITBundle
    from qwb.training.decision import DecisionGRU

    root = Path(directory).resolve()
    inventory = experiments.inventory(root)
    inventory.pop("checksums.json", None)
    if inventory != experiments.read(root / "checksums.json"):
        raise ValueError("resource audit inventory changed")
    plan = experiments.read(root / "plan.json")
    check_implementation(plan)
    if plan["schema_version"] != SCHEMA or plan["cases"] != cases(configuration(**plan["config"])):
        raise ValueError("resource plan case grid differs")
    intent = experiments.read(root / "intent.json")
    if intent != {key: value for key, value in plan.items() if key != "sources"}:
        raise ValueError("resource frozen intent differs")
    preparation = experiments.read(root / "preparation.json")
    if preparation != plan["sources"]:
        raise ValueError("resource pretraining state differs")
    if set(preparation) != {str(a) for a in plan["config"]["assets"]}:
        raise ValueError("resource pretraining pool coverage differs")
    for count, row in preparation.items():
        directory = root / "sources" / f"a{int(count):04d}"
        expected_task = {**{key: plan[key] for key in ("implementation_hash", "script_hash")},
            "kind": "prepare", "assets": int(count), "days": max(110, 90 + max(plan["config"]["steps"])),
            "config": plan["config"]}
        if experiments.read(directory / "task.json") != expected_task:
            raise ValueError("resource pretraining task differs")
        if row["state"] == "succeeded":
            source = row["child"]
            record = experiments.verify(Path(source["source_run"]))
            bundle = PITBundle(source["bundle"])
            if (record["artifact_hash"] != source["source_artifact_hash"] or record["state"] != "succeeded"
                    or record["code_hash"] != plan["implementation_hash"] or record["model"] != "sequence_gru_v1"
                    or record["run_id"] != source["source_run_id"]
                    or record["data_content_hash"] != source["data_content_hash"]
                    or source != experiments.read(directory / "child-result.json")
                    or source["task_hash"] != experiments.digest(directory / "task.json")
                    or bundle.identity["content_hash"] != source["data_content_hash"]
                    or bundle.universe["code"].nunique() != int(count)):
                raise ValueError("resource pretraining provenance differs")
    attempts = experiments.read(root / "attempts.json")
    if set(attempts) != {row["id"] for row in plan["cases"]}:
        raise ValueError("resource measurement coverage differs from frozen plan")
    for spec in plan["cases"]:
        row, path = attempts[spec["id"]], root / "cases" / spec["id"]
        if row["state"] == "not_run":
            if preparation[str(spec["assets"])]["state"] == "succeeded":
                raise ValueError("resource omitted a runnable declared case")
        elif row["state"] in {"succeeded", "failed"}:
            measured = row.get("child")
            if measured is not None and measured != experiments.read(path / "child-result.json"):
                raise ValueError("resource child measurement differs")
            succeeded = row["exit_code"] == 0 and measured is not None and measured["state"] == "succeeded"
            if succeeded != (row["state"] == "succeeded"):
                raise ValueError("resource child exit and trial state differ")
        if row["state"] == "succeeded":
            task = experiments.read(path / "task.json")
            source = preparation[str(spec["assets"])]["child"]
            model = DecisionGRU.load(path / "checkpoint")
            expected_source = {key: source[key] for key in
                ("source_run", "source_run_id", "source_artifact_hash", "bundle", "data_content_hash")}
            expected_task = {**{key: plan[key] for key in ("implementation_hash", "script_hash")}, **spec,
                             "kind": "fit", "config": plan["config"], "source": expected_source}
            protocols, context, policy, training = fit_inputs(task, PITBundle(source["bundle"], lookback=60))
            if (any(task[key] != value for key, value in spec.items())
                    or task != expected_task
                    or row["child"] != experiments.read(path / "child-result.json")
                    or model.fit_record != row["child"]["fit"]
                    or model.source["artifact_hash"] != source["source_artifact_hash"]
                    or model.fit_record["data_content_hash"] != source["data_content_hash"]
                    or model.fit_record["implementation_hash"] != plan["implementation_hash"]
                    or model.fit_record["config"] != asdict(training) or model.fit_record["context"] != context
                    or len(model.fit_record["history"]) != plan["config"]["epochs"]):
                raise ValueError("resource fit/checkpoint provenance differs")
            measured = row["child"]
            if (measured["pid"] == plan["controller_pid"] or measured["torch_num_threads"] != 1
                    or measured["thread_environment"] != THREAD_ENV
                    or not row["wall_elapsed_seconds"] >= measured["elapsed_inside_child_seconds"] >= model.fit_record["elapsed_seconds"]
                    or not measured["memory"]["peak_rss_bytes"] >= model.fit_record["memory"]["peak_rss_bytes"]
                           >= measured["baseline_process_memory"]["peak_rss_bytes"]):
                raise ValueError("resource process isolation or measurement units differ")
            inputs = {"context": context, "training": asdict(training), "policy": {**policy, "fees": asdict(policy["fees"])}}
            if (experiments.read(path / "fit-inputs.json") != inputs
                    or any(experiments.object_digest(experiments.read(path / f"{name}.json")) != experiments.object_digest(value)
                           for name, value in protocols.items())
                    or model.fit_record["protocol_ids"] != [experiments.object_digest(protocols[name])
                                                           for name in ("train", "validation")]):
                raise ValueError("resource frozen episode inputs differ")
    if summarize(plan, attempts, root) != experiments.read(root / "result.json"):
        raise ValueError("resource summary arithmetic differs")
    return {"status": "PASS", "scope": "inventory, pretraining/checkpoint provenance and summary arithmetic; not rerun or timing attestation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--verify", type=Path)
    action.add_argument("--child-task", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--assets", type=int, nargs="+", default=[24, 64, 128])
    parser.add_argument("--steps", type=int, nargs="+", default=[3, 12])
    parser.add_argument("--detach", nargs="+", default=["full", "2"])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=120.)
    parser.add_argument("--wall-seconds", type=float, default=180.)
    parser.add_argument("--pretrain-max-samples", type=int, default=50000)
    args = parser.parse_args()
    if args.child_task is not None:
        return child(args.child_task)
    if args.verify is not None:
        print(verify(args.verify))
        return 0
    config = configuration(assets=args.assets, steps=args.steps,
        detach=[None if value == "full" else int(value) for value in args.detach], repeats=args.repeats,
        epochs=args.epochs, max_seconds=args.max_seconds, wall_seconds=args.wall_seconds,
        pretrain_max_samples=args.pretrain_max_samples)
    result = compute(args.output, config)
    print(f"{result['status']}: {result['success_count']}/{result['total_declared']}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
