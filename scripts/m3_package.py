"""M3: turn an M2 run into a model package, build the publish dir, check R1/R2.

Usage:  python -X utf8 scripts/m3_package.py runs/m2/cn_rank_5d_smoke
Writes <run>/package/ (staging) and <run>/publish/ (whitelist build output).
Publishing to HF/GitHub is a separate, explicit step (not done here).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401
import yaml  # noqa: E402

from qwb import card, consistency, daily, env, package  # noqa: E402


def main() -> int:
    run = Path(sys.argv[1]).resolve()
    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    stage, pub = run / "package", run / "publish"
    for d in (stage, pub):
        if d.exists():
            shutil.rmtree(d)
    (stage / "models").mkdir(parents=True)
    (stage / "consistency").mkdir()
    (stage / "report").mkdir()

    models = sorted((run / "models").glob("*.txt"))
    for p in models:
        shutil.copy2(p, stage / "models" / p.name)
    task = ROOT / "examples" / "cn_rank_5d.yaml"
    shutil.copy2(task, stage / "task.yaml")
    recipe = {"source": "tushare", "market": "cn_a", "freq": "daily", "start": "2018-01-02",
              "end": "2025-12-31", "universe": {"type": "all"},
              "fields": ["open", "high", "low", "close", "amount", "adj_factor", "up_limit"],
              "extras": ["stock_st", "suspend_d"], "revision_policy": "latest"}
    (stage / "recipe.yaml").write_text(yaml.safe_dump(recipe, allow_unicode=True),
                                       encoding="utf-8")
    shutil.copy2(run / "folds.json", stage / "folds.json")
    shutil.copy2(run / "report.md", stage / "report" / "report.md")
    (stage / "environment.json").write_text(json.dumps(env.metadata(), indent=2, default=str),
                                            encoding="utf-8")
    sample = consistency.make_sample(models, daily.FEATURES)
    (stage / "consistency" / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
    (stage / "notes.md").write_text(
        "LightGBM baseline, yearly walk-forward refits, per-date rank features.\n"
        "Inputs: the 10 features in qwb.daily.FEATURES, each a per-date percentile rank.\n",
        encoding="utf-8")

    files = (["task.yaml", "recipe.yaml", "folds.json", "environment.json",
              "report/report.md", "consistency/sample.json", "notes.md"]
             + [f"models/{p.name}" for p in models])
    manifest = {
        "name": m["task"] + "_lgbm", "version": "0.1.0",
        "base_model": {"name": "lightgbm", "version": m["env"]["packages"]["lightgbm"]},
        "task_card": "task.yaml", "data_recipe": "recipe.yaml",
        "train_config": {"config_hash": "sha256:" + m["acceptance_hash"],
                         "seeds": [int(k.removeprefix("lgbm_seed")) for k in m["ic"]
                                   if k.startswith("lgbm_seed")],
                         "folds": "folds.json"},
        "environment": {"lockfile": "environment.json", "hardware": "cpu"},
        "report": "report/report.md",
        "licenses": {"model": "Apache-2.0", "code": "Apache-2.0",
                     "data": "tushare-compatible vendor: not redistributable; recipe only"},
        "custom_code": False, "includes_data": False,
        "consistency_sample": "consistency/sample.json",
        "deviations_from_default": m["deviations"],
        "data_snapshot_id": m["data_snapshot_id"],
        "files": files,
    }
    (stage / package.MANIFEST).write_text(yaml.safe_dump(manifest, allow_unicode=True,
                                                         sort_keys=False), encoding="utf-8")

    rep = package.build(stage, pub)
    print(rep)
    if not rep.ok:
        return 1
    r = consistency.check(pub)
    print("R1", r["R1"], "R2", r["R2"])
    for k, v in r["models"].items():
        print(" ", k, v)
    (run / "reproducibility.json").write_text(json.dumps(r, indent=2), encoding="utf-8")

    # model card generated from the manifest, after R1/R2 so it states the checked result
    task_card = yaml.safe_load((stage / "task.yaml").read_text(encoding="utf-8"))
    readme = card.render(manifest, task_card, m, r)
    parsed = card.parse(readme)
    if not parsed["ok"] or parsed["flags"]:
        print("model card failed its own parse/content check:", parsed)
        return 1
    (pub / "README.md").write_text(readme, encoding="utf-8")
    manifest["files"].append("README.md")
    (pub / package.MANIFEST).write_text(yaml.safe_dump(manifest, allow_unicode=True,
                                                       sort_keys=False), encoding="utf-8")
    rescan = package.scan_dir(pub)
    print("rescan with README:", rescan)
    return 0 if r["R1"] and r["R2"] and rescan.ok else 1


if __name__ == "__main__":
    sys.exit(main())
