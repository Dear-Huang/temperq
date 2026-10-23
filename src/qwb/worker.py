"""Serializable local jobs shared by CLI and detached UI workers."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import experiments
from .jobs import Store


ACTIONS = {"train", "protocol", "bench", "compare", "free_data", "governance", "strategy_evaluate", "strategy_tick"}


def execute(ctx, config: dict, workdir: Path, *, echo=False) -> None:
    action = config["action"]
    ctx.check_cancel()
    def log(message):
        ctx.log(str(message))
        if echo:
            print(message, flush=True)
        ctx.check_cancel()
    if action in {"strategy_evaluate", "strategy_tick"}:
        from . import strategy_workspace as workspace
        if action == "strategy_evaluate":
            out=workspace.evaluate(workdir,config["sid"],config["vid"],start=config["start"],
                end=config["end"],check=ctx.check_cancel,log=log)
        else:
            workspace.refresh_and_tick(workdir,config["sid"],config["vid"],check=ctx.check_cancel,log=log,serialized=True)
            out=workspace.location(workdir,config["sid"])/"versions"/config["vid"]
    elif action == "governance":
        from . import governance
        out = Path(config["output"])
        if out.exists():
            # A retry is a new recorded attempt; preserve the failed artifact.
            out = out.with_name(uuid.uuid4().hex)
        governance.run(config["predictions"], config["data_dir"], out,
            governance.GovernanceConfig(**config["governance_config"]), start=config["start"],
            end=config["end"], initial_cash=config["initial_cash"], check=ctx.check_cancel, log=log)
    elif action == "free_data":
        from .free_data import fetch
        name = config["name"]
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise ValueError("invalid dataset name")
        result = fetch(workdir / "data" / name, start=config["start"], end=config["end"],
                       log=log, progress=ctx.progress, check_cancel=ctx.check_cancel)
        out = Path(result["snapshot"])
    elif action == "train":
        from .pipeline import run
        for note in config.get("notes", []):
            log(note)
        summary = run(dict(config["resolved"], _job_id=ctx.job_id), workdir, log=log)
        out = Path(summary["output"])
    elif action == "protocol":
        from .bench.contracts import create_protocol
        out = workdir / "bench" / "protocols" / config["filename"]
        if Path(config["filename"]).name != config["filename"]:
            raise ValueError("invalid protocol filename")
        create_protocol(config["anchor"], out, cwd=workdir, **config.get("statistics", {}))
    elif action == "bench":
        from .bench.runner import evaluate_model
        if config.get("create_protocol"):
            from .bench.contracts import create_protocol
            protocol_path = Path(config["protocol"])
            if not protocol_path.exists():
                create_protocol(config["source"], protocol_path, cwd=workdir)
        evaluation_id = uuid.uuid4().hex
        out = workdir / "runs" / "bench" / evaluation_id
        summary = evaluate_model(config["source"], config["protocol"], out,
                                 data_dir=config.get("data_dir"), workdir=workdir,
                                 evaluation_id=evaluation_id, log=log)
        (ctx.artifacts / "output.txt").write_text(str(out), encoding="utf-8")
        if summary["status"] == "INVALID":
            raise ValueError("; ".join(summary["errors"]))
    elif action == "compare":
        from .bench.runner import compare_models
        out = workdir / "runs" / "comparisons" / uuid.uuid4().hex
        compare_models(config["old"], config["new"], out)
    else:
        raise ValueError(f"unsupported worker action: {action}")
    (ctx.artifacts / "output.txt").write_text(str(out), encoding="utf-8")


def run_job(workdir: Path, job_id: str, *, wait=True, echo=False) -> str:
    workdir = Path(workdir).resolve()
    store = Store(workdir / "runs" / "jobs")
    if store.spec(job_id)["config"].get("action") not in ACTIONS:
        raise ValueError("this job is not supported by the persistent worker")
    while True:
        if store.status(job_id)["state"] != "queued":
            return store.status(job_id)["state"]
        try:
            with experiments.output_lock(store.root / "worker"):
                return store.run(job_id, lambda ctx, cfg: execute(ctx, cfg, workdir, echo=echo))
        except ValueError as exc:
            if "another run is using output" not in str(exc) or not wait:
                raise
            time.sleep(0.5)


def launch(workdir: Path, job_id: str) -> int:
    root = Path(workdir) / "runs" / "jobs" / job_id
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    with (root / "worker.log").open("ab") as output:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "qwb.worker",
                                    "--workdir", str(workdir), "--job", job_id],
                                   cwd=workdir, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   creationflags=flags, start_new_session=os.name != "nt")
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    state = run_job(args.workdir, args.job)
    return 0 if state == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
