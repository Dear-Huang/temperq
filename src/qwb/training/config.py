"""Strict, versioned configuration for causal prediction runs."""

from __future__ import annotations

import copy
from pathlib import Path

from .. import experiments, taskcard
from ..issues import ERROR, Report
from ..portfolio.contracts import number, timestamp

VERSION = "e2e_prediction_v1"
TOP = {"spec_version", "name", "task", "data", "observations", "model", "folds",
       "train", "evaluation_asof", "output"}
OBSERVATIONS = {"lookback": 60, "min_history": 20}
FOLDS = {"frequency": "M", "validation_days": 20, "min_train_days": 60,
         "train_days": None, "embargo": "0s"}
TRAIN = {"seeds": [0, 1, 2], "max_samples": 50000, "max_seconds": 300.0, "cpu_threads": 1}
GRU = {"name": "sequence_gru_v1", "hidden_size": 32, "epochs": 20,
       "batch_size": 256, "learning_rate": .001, "patience": 5, "freeze_encoder": False}
RIDGE = {"name": "ridge_factor_v1", "alpha": 10.0}


def _mapping(value, name, allowed):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"unknown {name} fields: {sorted(unknown)}")
    return value


def validate_task(card):
    if not isinstance(card, dict):
        raise ValueError("task must be a mapping")
    rep = taskcard.validate(card)
    if not rep.ok:
        raise ValueError(str(rep))
    if not (card["market"] == "cn_a" and card["template"] == "daily"
            and card["target"] == {"type": "return", "horizon_days": 0}
            and card["output"] == "score"
            and card["timing"] == {"signal_available": {"day": 0, "time": "15:00"},
                                  "decision": {"day": 0, "time": "15:30"},
                                  "execution": {"day": 1, "time": "09:30", "price": "open"}}
            and card["label"] == {"start": {"day": 1, "time": "09:30", "price": "open"},
                                  "end": {"day": 1, "time": "15:00", "price": "close"}}):
        raise ValueError("e2e_prediction_v1 requires cn_a next-session open-to-close return, score output")


def resolve(raw, base_dir):
    rep = Report()
    try:
        raw = _mapping(raw, "config", TOP)
        if raw.get("spec_version") != VERSION:
            raise ValueError(f"spec_version must be {VERSION}")
        if not isinstance(raw.get("name"), str) or not raw["name"].strip():
            raise ValueError("name must be a nonempty string")
        base = Path(base_dir).resolve()
        card = raw.get("task")
        if isinstance(card, str):
            card = taskcard.load(base / card)
        else:
            card = copy.deepcopy(card)
        validate_task(card)
        data = _mapping(raw.get("data"), "data", {"source", "dir"})
        if data.get("source") != "pit_bundle" or not isinstance(data.get("dir"), str) or not data["dir"]:
            raise ValueError("data requires source: pit_bundle and dir")
        data = {"source": "pit_bundle", "dir": str((base / data["dir"]).resolve())}
        obs = dict(OBSERVATIONS, **_mapping(raw.get("observations", {}), "observations", OBSERVATIONS))
        if (type(obs["lookback"]) is not int or obs["lookback"] < 2
                or type(obs["min_history"]) is not int or not 1 <= obs["min_history"] <= obs["lookback"]):
            raise ValueError("observations need lookback >=2 and min_history in 1..lookback")
        fold = dict(FOLDS, **_mapping(raw.get("folds"), "folds", {*FOLDS, "first_test", "last_test"}))
        import pandas as pd

        for key in ("first_test", "last_test"):
            if not isinstance(fold.get(key), str):
                raise ValueError(f"folds.{key} must be a local ISO date")
            stamp = pd.Timestamp(fold[key])
            if pd.isna(stamp) or stamp.tzinfo is not None or fold[key] != str(stamp.date()):
                raise ValueError(f"folds.{key} must be YYYY-MM-DD")
        if fold["first_test"] > fold["last_test"] or fold["frequency"] not in {"M", "W-FRI"}:
            raise ValueError("invalid fold date order or frequency")
        for key in ("validation_days", "min_train_days"):
            if type(fold[key]) is not int or fold[key] < 1:
                raise ValueError(f"folds.{key} must be a positive integer")
        if fold["train_days"] is not None and (type(fold["train_days"]) is not int
                or fold["train_days"] < fold["min_train_days"]):
            raise ValueError("folds.train_days must be null or >= min_train_days")
        gap = pd.Timedelta(fold["embargo"])
        if pd.isna(gap) or gap < pd.Timedelta(0):
            raise ValueError("folds.embargo must be a nonnegative duration")
        fold["embargo"] = str(gap)
        train = dict(TRAIN, **_mapping(raw.get("train", {}), "train", TRAIN))
        seeds = train["seeds"]
        if (not isinstance(seeds, list) or not seeds or any(type(s) is not int or s < 0 for s in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ValueError("train.seeds must be distinct nonnegative integers")
        for key in ("max_samples", "cpu_threads"):
            if type(train[key]) is not int or train[key] < 1:
                raise ValueError(f"train.{key} must be a positive integer")
        number(train["max_seconds"], "training time budget", lower=.001)
        requested = raw.get("model", {"name": "sequence_gru_v1"})
        if not isinstance(requested, dict):
            raise ValueError("model must be a mapping")
        defaults = {"sequence_gru_v1": GRU, "ridge_factor_v1": RIDGE}.get(requested.get("name"))
        if defaults is None:
            raise ValueError("model.name must be sequence_gru_v1 or ridge_factor_v1")
        model = dict(defaults, **_mapping(requested, "model", defaults))
        if model["name"] == "ridge_factor_v1":
            number(model["alpha"], "ridge alpha", lower=1e-12)
        else:
            for key in ("hidden_size", "epochs", "batch_size", "patience"):
                if type(model[key]) is not int or model[key] < 1:
                    raise ValueError(f"model.{key} must be a positive integer")
            number(model["learning_rate"], "learning rate", lower=1e-8, upper=1)
            if type(model["freeze_encoder"]) is not bool:
                raise ValueError("model.freeze_encoder must be boolean")
        evaluation = str(timestamp(raw.get("evaluation_asof")))
        if timestamp(evaluation) < pd.Timestamp(fold["last_test"]).tz_localize("Asia/Shanghai"):
            raise ValueError("evaluation_asof cannot precede the test period")
        resolved = {"engine": VERSION, "name": raw["name"], "card": card, "data": data,
                    "model": model["name"], "model_config": model, "observations": obs,
                    "folds": fold, "train": train, "evaluation_asof": evaluation,
                    "output": str(raw.get("output") or Path("runs") / raw["name"]),
                    "deviations": ["versioned causal prediction run; no legacy five-day Bench"]}
        resolved["config_hash"] = experiments.object_digest({k: v for k, v in resolved.items()
                                                            if k not in {"name", "output", "deviations"}})
        return resolved, rep
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        rep.add(ERROR, "E2E_CONFIG", str(exc))
        return None, rep
