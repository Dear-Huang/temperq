"""Strict configuration for chronological portfolio-utility runs."""

from __future__ import annotations

import copy
from pathlib import Path

from .. import experiments, taskcard
from ..issues import ERROR, Report
from ..bench import targets
from ..portfolio.contracts import number, timestamp
from ..portfolio.optimization import AllocationConfig
from ..portfolio.recipes import CostRecipe, RiskRecipe
from ..portfolio.comparison import BASE_LIMITS as LIMITS, validate_acceptance as acceptance
from .config import _mapping, validate_task, OBSERVATIONS

VERSION = "e2e_utility_v1"
TOP = {"spec_version", "name", "task", "data", "observations", "initialization", "model", "folds",
       "portfolio", "continuous", "utility", "train", "acceptance", "evaluation_asof", "output", "validation_audit",
       "outer_evaluation"}
TRAIN = {"seeds": [0, 1, 2], "epochs": 10, "patience": 3, "learning_rate": .001,
         "cpu_threads": 1, "max_seconds": 300., "max_episode_steps": 256, "detach_every": None}


def resolve(raw, base_dir):
    rep = Report()
    try:
        raw = _mapping(raw, "config", TOP)
        if raw.get("spec_version") != VERSION:
            raise ValueError(f"spec_version must be {VERSION}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a nonempty string")
        base = Path(base_dir).resolve()
        card = taskcard.load(base / raw["task"]) if isinstance(raw.get("task"), str) else copy.deepcopy(raw.get("task"))
        if not isinstance(card, dict) or card.get("name") != "cn_alloc_daily_v1" or card.get("output") != "target_weight":
            raise ValueError("utility training requires cn_alloc_daily_v1 target_weight task")
        probe = copy.deepcopy(card)
        probe["output"] = "score"
        validate_task(probe)
        data = _mapping(raw.get("data"), "data", {"source", "dir"})
        if data.get("source") != "pit_bundle" or not isinstance(data.get("dir"), str) or not data["dir"]:
            raise ValueError("data requires source pit_bundle and dir")
        obs = dict(OBSERVATIONS, **_mapping(raw.get("observations", {}), "observations", OBSERVATIONS))
        if (type(obs["lookback"]) is not int or obs["lookback"] < 2 or type(obs["min_history"]) is not int
                or not 1 <= obs["min_history"] <= obs["lookback"]):
            raise ValueError("invalid observation lookback or minimum history")
        init = copy.deepcopy(_mapping(raw.get("initialization"), "initialization", {"run", "fold"}))
        if not isinstance(init.get("run"), str) or not init["run"] or type(init.get("fold")) is not int or init["fold"] < 0:
            raise ValueError("initialization requires a prediction run and nonnegative fold")
        init["run"] = str((base / init["run"]).resolve())
        model = {"name": "decision_gru_v1", "freeze_encoder": False,
                 **_mapping(raw.get("model", {}), "model", {"name", "freeze_encoder"})}
        if model["name"] != "decision_gru_v1" or type(model["freeze_encoder"]) is not bool:
            raise ValueError("utility model must be decision_gru_v1 with boolean freeze_encoder")
        portfolio = copy.deepcopy(_mapping(raw.get("portfolio"), "portfolio", {"risk", "cost", "optimizer", "production"}))
        from dataclasses import asdict

        for key, cls in (("risk", RiskRecipe), ("cost", CostRecipe), ("optimizer", AllocationConfig)):
            value = _mapping(portfolio.get(key), f"portfolio.{key}", cls.__dataclass_fields__)
            portfolio[key] = asdict(cls(**value))
        production = _mapping(portfolio.get("production"), "portfolio.production", {"policy", "buffer", "dust_threshold"})
        if set(production) != {"policy", "buffer", "dust_threshold"}:
            raise ValueError("production requires explicit policy, buffer and dust_threshold (null for v1)")
        if production["policy"] not in {"torch_box_budget_turnover_v1", "torch_box_budget_turnover_dust_v1"}:
            raise ValueError("unsupported utility production policy")
        tolerance = portfolio["optimizer"]["tolerance"]
        number(production["buffer"], "production buffer", lower=1e-15, upper=min(tolerance, 1e-9))
        if production["policy"] == "torch_box_budget_turnover_dust_v1":
            number(production["dust_threshold"], "dust threshold", lower=1e-15, upper=min(tolerance, 1e-8))
        elif production["dust_threshold"] is not None:
            raise ValueError("dust threshold requires the explicit dust production policy")
        continuous = copy.deepcopy(_mapping(raw.get("continuous"), "continuous",
            {"integer_lots", "settlement", "market_restrictions", "minimum_commission", "fee_model"}))
        if set(continuous) != {"integer_lots", "settlement", "market_restrictions", "minimum_commission", "fee_model"}:
            raise ValueError("declare all continuous execution settings")
        if (any(type(continuous[key]) is not bool for key in continuous if key != "fee_model")
                or continuous["integer_lots"] or continuous["fee_model"] not in {"none", "declared_execution", "causal_linear"}
                or (continuous["fee_model"] == "causal_linear" and continuous["minimum_commission"])):
            raise ValueError("utility training needs fractional execution and a valid explicit fee configuration")
        utility = copy.deepcopy(_mapping(raw.get("utility"), "utility", {"variance_penalty"}))
        number(utility["variance_penalty"], "variance penalty")
        train = {**TRAIN, **_mapping(raw.get("train", {}), "train", TRAIN)}
        if (not isinstance(train["seeds"], list) or not train["seeds"] or len(set(train["seeds"])) != len(train["seeds"])
                or any(type(seed) is not int or seed < 0 for seed in train["seeds"])):
            raise ValueError("utility seeds must be distinct nonnegative integers")
        for key in ("epochs", "patience", "cpu_threads", "max_episode_steps"):
            if type(train[key]) is not int or train[key] < 1:
                raise ValueError(f"train.{key} must be a positive integer")
        if train["detach_every"] is not None and (type(train["detach_every"]) is not int or train["detach_every"] < 1):
            raise ValueError("detach_every must be null or a positive integer")
        number(train["learning_rate"], "utility learning rate", lower=1e-8, upper=1)
        number(train["max_seconds"], "utility time budget", lower=.001)
        limits = acceptance(raw.get("acceptance"))
        audit = None
        if "validation_audit" in raw:
            audit = _mapping(raw["validation_audit"], "validation_audit", {"schema_version"})
            if audit != {"schema_version": "utility_validation_audit_v1"}:
                raise ValueError("validation_audit requires schema_version utility_validation_audit_v1")
        evaluation = str(timestamp(raw.get("evaluation_asof")))
        folds, seen, last_test = [], set(), None
        if not isinstance(raw.get("folds"), list) or not raw["folds"]:
            raise ValueError("utility folds must be a nonempty explicit list")
        for value in raw["folds"]:
            value = _mapping(value, "fold", {"id", "train", "validation", "test", "fit_cutoff", "selection_cutoff"})
            if type(value.get("id")) is not int or value["id"] < 0 or value["id"] in seen:
                raise ValueError("utility fold IDs must be unique nonnegative integers")
            seen.add(value["id"])
            protocols, loaded = {}, {}
            for phase in ("train", "validation", "test"):
                if not isinstance(value.get(phase), str) or not value[phase]:
                    raise ValueError(f"fold.{phase} must be a protocol path")
                path = (base / value[phase]).resolve()
                protocol = targets.read(path)
                protocol_id = targets.validate_protocol(protocol)
                if len(protocol["sessions"]) > train["max_episode_steps"]:
                    raise ValueError("utility protocol exceeds max_episode_steps")
                if protocol["constraints"] != {key: portfolio["optimizer"][key] for key in ("max_weight", "min_cash")}:
                    raise ValueError("utility protocol and allocator constraints differ")
                protocols[phase] = {"path": str(path), "file_hash": experiments.digest(path), "protocol_id": protocol_id}
                loaded[phase] = protocol
            fit_cutoff, selection_cutoff = timestamp(value["fit_cutoff"]), timestamp(value["selection_cutoff"])
            first = {phase: timestamp(p["sessions"][0]["decision_time"]) for phase, p in loaded.items()}
            end = {phase: timestamp(p["sessions"][-1]["close_at"]) for phase, p in loaded.items()}
            if not (first["train"] < end["train"] <= fit_cutoff <= first["validation"] < end["validation"]
                    <= selection_cutoff <= first["test"] < end["test"] <= timestamp(evaluation)):
                raise ValueError("utility fold feedback/selection/test intervals overlap or exceed evaluation_asof")
            if last_test is not None and first["test"] <= last_test:
                raise ValueError("utility outer test decisions must be ordered and disjoint")
            last_test = timestamp(loaded["test"]["sessions"][-1]["decision_time"])
            folds.append({"id": value["id"], "protocols": protocols,
                          "fit_cutoff": str(fit_cutoff), "selection_cutoff": str(selection_cutoff)})
        if raw.get("output") is not None and (not isinstance(raw["output"], str) or not raw["output"].strip()):
            raise ValueError("utility output must be a nonempty path string")
        resolved = {"engine": VERSION, "name": name, "card": card,
            "data": {"source": "pit_bundle", "dir": str((base / data["dir"]).resolve())},
            "observations": obs, "initialization": init, "model": model["name"], "model_config": model,
            "folds": folds, "portfolio": portfolio, "continuous": continuous, "utility": utility,
            "train": train, "acceptance": limits, "evaluation_asof": evaluation,
            "output": str(raw.get("output") or Path("runs") / name),
            "deviations": ["utility research run; exact Bench independent; proxy threshold breaches remain diagnostic"]}
        if audit is not None:
            resolved["validation_audit"] = copy.deepcopy(audit)
        if "outer_evaluation" in raw:
            from ..portfolio.evaluation_failures import configuration

            resolved["outer_evaluation"] = configuration(raw["outer_evaluation"])
        resolved["config_hash"] = experiments.object_digest({k: v for k, v in resolved.items()
                                                            if k not in {"name", "output", "deviations"}})
        return resolved, rep
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        rep.add(ERROR, "UTILITY_CONFIG", str(exc))
        return None, rep
