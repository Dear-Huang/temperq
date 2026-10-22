"""Run config: one YAML drives a whole run (task, data, model, training).

    name: cn_rank_5d_lgbm
    task: cn_rank_5d.yaml            # path (relative to this file) or inline mapping
    data: {source: snapshot}         # snapshot | synthetic; dir defaults to config.local.yaml
    model: {name: lightgbm, num_boost_round: 300, params: {num_leaves: 63}}
    train: {universe: all_ex_st, seeds: [0, 1, 2]}
    output: runs/cn_rank_5d_lgbm     # relative to the working directory

Anything not written takes the default in qwb.daily.DEFAULTS; anything changed
shows up in the report's deviation list. Unknown keys are errors, so a typo
cannot silently fall back to a default.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

from . import daily, taskcard
from .issues import ERROR, WARN, Report

TOP = {"name", "task", "data", "model", "train", "output"}
DATA = {"source", "dir"}
SOURCES = {"snapshot", "synthetic"}
MODELS = set(daily.MODELS)
MODEL = {"name", "params", "num_boost_round", "alpha"}
TRAIN = {"universe", "universe_list", "min_age_days", "min_amount_quantile", "min_adv_yuan",
         "label_transform", "first_test_year", "final_test_years", "seeds",
         "shuffle_null_draws", "portfolio", "scenario"}
PORTFOLIO = {"top", "n_hold", "commission_bp"}
UNIVERSES = {"all", "st", "all_ex_st", "list", *daily.INDEX_CODES}


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _unknown(rep: Report, where: str, got: dict, allowed: set) -> None:
    for k in sorted(set(got) - allowed):
        rep.add(ERROR, "UNKNOWN_KEY", f"{where}.{k} is not a known setting "
                f"(allowed: {', '.join(sorted(allowed))})")


def resolve(raw: dict, base_dir: str | Path = ".") -> tuple[dict | None, Report]:
    """Validate a run config and expand it. Returns (resolved, report)."""
    rep = Report()
    base_dir = Path(base_dir)
    if not isinstance(raw, dict):
        rep.add(ERROR, "BAD_CONFIG", "run config must be a mapping")
        return None, rep
    _unknown(rep, "config", raw, TOP)
    for k in ("name", "task"):
        if k not in raw:
            rep.add(ERROR, "MISSING_FIELD", f"run config needs `{k}`")
    data = raw.get("data") or {"source": "snapshot"}
    model = raw.get("model") or {"name": "lightgbm"}
    train = raw.get("train") or {}
    for where, got, allowed in (("data", data, DATA), ("model", model, MODEL),
                                ("train", train, TRAIN)):
        if not isinstance(got, dict):
            rep.add(ERROR, "BAD_CONFIG", f"{where} must be a mapping")
        else:
            _unknown(rep, where, got, allowed)
    if not rep.ok:
        return None, rep

    if data.get("source", "snapshot") not in SOURCES:
        rep.add(ERROR, "BAD_SOURCE", f"data.source must be one of {sorted(SOURCES)}")
    if model.get("name", "lightgbm") not in MODELS:
        rep.add(ERROR, "BAD_MODEL", f"model.name must be one of {sorted(MODELS)}")
    if train.get("universe", daily.DEFAULTS["universe"]) not in UNIVERSES:
        rep.add(ERROR, "BAD_UNIVERSE", f"train.universe must be one of {sorted(UNIVERSES)}")
    pf = train.get("portfolio") or {}
    if not isinstance(pf, dict):
        rep.add(ERROR, "BAD_CONFIG", "train.portfolio must be a mapping")
    else:
        _unknown(rep, "train.portfolio", pf, PORTFOLIO)
        top = pf.get("top", daily.DEFAULTS["portfolio"]["top"])
        if not (isinstance(top, (int, float)) and 0 < top <= 0.5):
            rep.add(ERROR, "BAD_PORTFOLIO", "train.portfolio.top must be in (0, 0.5]")
        n_hold = pf.get("n_hold")
        if n_hold is not None and not (isinstance(n_hold, int) and 1 <= n_hold <= 1000):
            rep.add(ERROR, "BAD_PORTFOLIO", "train.portfolio.n_hold must be an integer in 1..1000")
    adv = train.get("min_adv_yuan")
    if adv is not None and not (isinstance(adv, (int, float)) and adv >= 0):
        rep.add(ERROR, "BAD_ADV", "train.min_adv_yuan must be a non-negative number (yuan)")
    if train.get("universe") == "list":
        lst = train.get("universe_list")
        if not isinstance(lst, list) or not lst or not all(isinstance(c, str) for c in lst):
            rep.add(ERROR, "BAD_UNIVERSE", "universe: list needs train.universe_list (codes)")
    alpha = model.get("alpha")
    if alpha is not None and not (isinstance(alpha, (int, float)) and alpha > 0):
        rep.add(ERROR, "BAD_MODEL", "model.alpha must be a positive number")
    seeds = train.get("seeds", daily.DEFAULTS["seeds"])
    if not isinstance(seeds, list) or not seeds or not all(isinstance(s, int) for s in seeds):
        rep.add(ERROR, "BAD_SEEDS", "train.seeds must be a non-empty list of integers")
    params = model.get("params") or {}
    for k in sorted(set(params) - set(daily.DEFAULTS["lgbm"])):
        rep.add(WARN, "UNCHECKED_PARAM", f"model.params.{k} is passed to LightGBM unchecked; "
                "LightGBM ignores misspelled names without error")

    # task card: path relative to the run config, or inline
    task = raw["task"]
    if isinstance(task, str):
        p = (base_dir / task) if not Path(task).is_absolute() else Path(task)
        if not p.is_file():
            rep.add(ERROR, "NO_TASK", f"task card not found: {p}")
            return None, rep
        card = taskcard.load(p)
    elif isinstance(task, dict):
        card = copy.deepcopy(task)
    else:
        rep.add(ERROR, "BAD_TASK", "task must be a path or a mapping")
        return None, rep
    rep.extend(taskcard.validate(card))
    if card.get("template") not in (None, "daily"):
        rep.add(ERROR, "UNSUPPORTED_TEMPLATE", "only the daily template is implemented")
    if not rep.ok:
        return None, rep

    cfg = copy.deepcopy(daily.DEFAULTS)
    cfg.update({k: v for k, v in train.items() if k != "portfolio"})
    cfg["portfolio"] = dict(cfg["portfolio"], **(train.get("portfolio") or {}))
    if "num_boost_round" in model:
        cfg["num_boost_round"] = model["num_boost_round"]
    cfg["lgbm"] = dict(cfg["lgbm"], **params)
    # only non-default models are recorded, so existing LightGBM configs keep their hash
    if model.get("name", "lightgbm") != "lightgbm":
        cfg["model"] = model["name"]
        if params:
            rep.add(WARN, "IGNORED_PARAM", "model.params only applies to LightGBM; ignored")
    if "alpha" in model:
        cfg["ridge_alpha"] = float(model["alpha"])
    if not set(cfg["final_test_years"]) or min(cfg["final_test_years"]) < cfg["first_test_year"]:
        rep.add(ERROR, "BAD_YEARS", "final_test_years must be at or after first_test_year")
        return None, rep

    name = str(raw["name"])
    resolved = {
        "name": name, "card": card, "cfg": cfg,
        "data": {"source": data.get("source", "snapshot"), "dir": data.get("dir")},
        "model": model.get("name", "lightgbm"),
        "output": str(raw.get("output") or Path("runs") / name),
        "deviations": daily.deviations(cfg),
    }
    blob = json.dumps({k: resolved[k] for k in ("card", "cfg", "data", "model")},
                      sort_keys=True, ensure_ascii=False, default=str)
    resolved["config_hash"] = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return resolved, rep
