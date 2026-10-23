"""Hash-addressed development protocols and strict prediction alignment."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .. import daily, dataset, experiments, pipeline, taskcard
from . import VERSION
from .account import DEFAULT_ACCOUNT

REFERENCES = ("reversal_5d", "momentum_20d", "low_vol_20d", "lgbm_anchor_v1")
UNIVERSE_KEYS = ("universe", "min_age_days", "min_amount_quantile")


def digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def object_digest(value) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def read_yaml(path: str | Path) -> dict:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a YAML mapping: {path}")
    return value


def read_json(path: str | Path):
    def reject(value):
        raise ValueError(f"non-standard JSON number: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def validate_task(card: dict) -> None:
    rep = taskcard.validate(card)
    if not rep.ok:
        raise ValueError(str(rep))
    horizon = card["target"].get("horizon_days")
    expected = (card["market"] == "cn_a" and card["template"] == "daily"
                and card["target"]["type"] == "return_rank" and card["output"] == "score"
                and horizon in (5, 20)
                and card["timing"]["decision"] == {"day": 0, "time": "15:30"}
                and card["timing"]["execution"] == {"day": 1, "time": "09:30", "price": "open"}
                and card["label"]["start"] == {"day": 1, "time": "09:30", "price": "open"}
                and card["label"]["end"] == {"day": horizon + 1, "time": "09:30", "price": "open"})
    if not expected:
        raise ValueError("supports cn_a daily rank 5d/20d, 15:30 decision, next-open labels only")


def data_files(root: Path) -> list[Path]:
    files = [root / "snapshot.json", *sorted((root / "bars").glob("*.parquet"))]
    if len(files) < 2:
        raise ValueError("snapshot requires snapshot.json and bars/*.parquet")
    if (root / "st_status.parquet").is_file():
        files.append(root / "st_status.parquet")
    return files


def inventory(root: Path, *, legacy=False) -> dict:
    files = ({p.relative_to(root).as_posix(): p for p in data_files(root)} if legacy
             else dataset.input_files(root))
    return {name: digest(path) for name, path in files.items()}


def resolve_data_dir(raw: dict, cwd: Path, override=None) -> Path:
    name = override or raw["data"].get("dir") or pipeline.find_local_config(cwd).get("data_dir")
    if not name:
        raise ValueError("set --data-dir or data_dir in config.local.yaml")
    p = Path(name)
    return (p if p.is_absolute() else cwd / p).resolve()


def create_protocol(anchor_run: str | Path, output: str | Path, *, cwd: Path,
                    data_dir=None, block_days=20, resamples=10000, min_days=60,
                    min_signal_score=0.0, min_anchor_gain_score=0.0) -> dict:
    anchor, output = experiments.frozen_source(Path(anchor_run)), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"protocol already exists: {output}")
    raw = read_yaml(anchor / "config.resolved.yaml")
    if raw.get("model", "lightgbm") != "lightgbm":
        raise ValueError("the frozen reference must be a LightGBM training run")
    card = read_yaml(anchor / "task.yaml")
    validate_task(card)
    if raw["data"]["source"] != "snapshot":
        raise ValueError("init requires a local snapshot; synthetic fixtures are not market evidence")
    root = resolve_data_dir(raw, cwd, data_dir)
    years = sorted(set(raw["cfg"]["final_test_years"]))
    hashes = inventory(root)
    if (anchor / "data_manifest.json").is_file():
        declared = read_json(anchor / "data_manifest.json")
        if declared != {"files": hashes, "content_hash": object_digest(hashes)}:
            raise ValueError("anchor training data content differs from the requested protocol snapshot")
    evidence = {name: digest(anchor / name) for name in
                ("predictions.parquet", "task.yaml", "config.resolved.yaml", "folds.json")}
    if (anchor / "metrics.json").is_file():
        evidence["metrics.json"] = digest(anchor / "metrics.json")
    if (anchor / "data_manifest.json").is_file():
        evidence["data_manifest.json"] = digest(anchor / "data_manifest.json")
    value = {
        "schema_version": 5, "scorer_version": VERSION, "stage": "development",
        "task": card, "timezone": "Asia/Shanghai",
        "data": {"dir": str(root), "snapshot_id": daily.load_snapshot(root)["data_snapshot_id"],
                 "files": hashes, "content_hash": object_digest(hashes)},
        "evaluation_years": years,
        "universe": {**{k: raw["cfg"][k] for k in UNIVERSE_KEYS},
                     "min_adv_yuan": raw["cfg"].get("min_adv_yuan") or 0,
                     **({"universe_list": sorted(set(raw["cfg"].get("universe_list") or []))}
                        if raw["cfg"]["universe"] == "list" else {})},
        "statistics": {"block_days": block_days, "resamples": resamples,
                       "seed": 0, "min_days": min_days},
        "coverage": {"min_daily": 1.0, "min_total": 1.0, "min_names": 20},
        "signal_threshold": min_signal_score / 100, "references": list(REFERENCES),
        "anchor": {"run_dir": str(anchor), "files": evidence},
        "sample_status": "historical_oos_declared", "evidence_source": "local_declared",
        "account": deepcopy(DEFAULT_ACCOUNT),
        "assessment": {"min_anchor_gain_ic": min_anchor_gain_score / 100,
                       "comparison_family_size": len(REFERENCES),
                       "sensitivity_blocks": sorted({max(1, block_days // 2), block_days,
                                                     min(252, block_days * 2)}),
                       "top_share": 0.1, "cost_hurdles_bp": [10, 30, 60],
                       "liquidity_window": 20, "regime_window": 20},
    }
    validate_protocol(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return value


def validate_protocol(p: dict) -> None:
    keys = {"schema_version", "scorer_version", "stage", "task", "timezone", "data",
            "evaluation_years", "universe", "statistics", "coverage", "signal_threshold",
            "references", "anchor", "sample_status", "evidence_source"}
    keys.add("assessment")
    keys.add("account")
    if set(p) - {"lifecycle"} != keys or p["schema_version"] not in (3, 4, 5) or p["scorer_version"] != VERSION:
        raise ValueError("unsupported protocol schema/fields/scorer version")
    if "lifecycle" in p:
        spec = p["lifecycle"]
        if (not isinstance(spec, dict) or set(spec) != {"path", "sha256"}
                or not str(spec["sha256"]).startswith("sha256:")):
            raise ValueError("lifecycle block needs exactly path and sha256")
    if p["account"] != DEFAULT_ACCOUNT:
        raise ValueError("unsupported account assumptions; v0.3 uses one fixed research environment")
    if (p["stage"] != "development" or p["timezone"] != "Asia/Shanghai"
            or p["sample_status"] != "historical_oos_declared"
            or p["evidence_source"] != "local_declared"):
        raise ValueError("only local development historical protocols are supported")
    validate_task(p["task"])
    if p["references"] != list(REFERENCES):
        raise ValueError("the complete fixed reference set is required")
    if p["coverage"] != {"min_daily": 1.0, "min_total": 1.0, "min_names": 20}:
        raise ValueError("ranking requires full eligible-sample coverage and >=20 names")
    years = p["evaluation_years"]
    if not isinstance(years, list) or not years or any(type(y) is not int for y in years):
        raise ValueError("evaluation_years must be a nonempty integer list")
    if years != list(range(min(years), max(years) + 1)):
        raise ValueError("evaluation_years must be sorted and consecutive to preserve date blocks")
    u = p["universe"]
    universe_keys = set(UNIVERSE_KEYS) | ({"min_adv_yuan"} if p["schema_version"] >= 4 else set())
    allowed = {"all", "st", "all_ex_st"}
    if p["schema_version"] >= 5:
        allowed |= {"list", *daily.INDEX_CODES}
        if u.get("universe") == "list":
            universe_keys.add("universe_list")
            names = u.get("universe_list")
            if (not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names)
                    or names != sorted(set(names))):
                raise ValueError("list universe requires sorted unique security codes")
    if (set(u) != universe_keys or u["universe"] not in allowed
            or type(u["min_age_days"]) is not int or u["min_age_days"] < 1
            or not 0 <= u["min_amount_quantile"] < 1):
        raise ValueError("invalid universe settings")
    if (p["schema_version"] >= 4 and (type(u["min_adv_yuan"]) not in (int, float)
            or not np.isfinite(u["min_adv_yuan"]) or u["min_adv_yuan"] < 0)):
        raise ValueError("invalid min_adv_yuan")
    s = p["statistics"]
    if (set(s) != {"block_days", "resamples", "seed", "min_days"}
            or any(type(v) is not int for v in s.values())
            or not 1 <= s["block_days"] <= 252 or not 100 <= s["resamples"] <= 100000
            or s["min_days"] < 2 or s["seed"] < 0):
        raise ValueError("invalid bootstrap settings")
    if not isinstance(p["signal_threshold"], (float, int)) or not np.isfinite(p["signal_threshold"]):
        raise ValueError("invalid signal_threshold")
    a = p["assessment"]
    expected = {"min_anchor_gain_ic", "comparison_family_size", "sensitivity_blocks", "top_share",
                "cost_hurdles_bp", "liquidity_window", "regime_window"}
    if not isinstance(a, dict) or set(a) != expected:
        raise ValueError("invalid assessment settings")
    if (not isinstance(a["min_anchor_gain_ic"], (float, int))
            or not np.isfinite(a["min_anchor_gain_ic"]) or a["min_anchor_gain_ic"] < 0
            or a["comparison_family_size"] != len(REFERENCES)
            or a["top_share"] != 0.1 or a["cost_hurdles_bp"] != [10, 30, 60]
            or a["liquidity_window"] != 20 or a["regime_window"] != 20):
        raise ValueError("invalid assessment thresholds or fixed diagnostic definitions")
    blocks = a["sensitivity_blocks"]
    if (not isinstance(blocks, list) or not blocks
            or any(type(v) is not int or not 1 <= v <= 252 for v in blocks)
            or blocks != sorted(set(blocks)) or s["block_days"] not in blocks):
        raise ValueError("sensitivity blocks must be sorted, unique and include the main block")
    for section, fields in (("data", {"dir", "snapshot_id", "files", "content_hash"}),
                            ("anchor", {"run_dir", "files"})):
        if set(p[section]) != fields or not isinstance(p[section]["files"], dict):
            raise ValueError(f"invalid {section} inventory")
        for name, h in p[section]["files"].items():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or ":" in name:
                raise ValueError("inventory paths must stay within their input directory")
            if not isinstance(h, str) or len(h) != 71 or not h.startswith("sha256:"):
                raise ValueError("full SHA-256 required")
    if p["data"]["content_hash"] != object_digest(p["data"]["files"]):
        raise ValueError("data manifest content hash mismatch")
    anchor_files = set(p["anchor"]["files"])
    required = {"predictions.parquet", "task.yaml", "config.resolved.yaml", "folds.json"}
    optional = {"metrics.json"} | ({"data_manifest.json"} if p["schema_version"] >= 5 else set())
    if not required <= anchor_files or anchor_files - required - optional:
        raise ValueError("anchor artifacts are incomplete")


def verify_inputs(p: dict, data_dir=None) -> tuple[Path, Path]:
    root = Path(data_dir or p["data"]["dir"]).resolve()
    if inventory(root, legacy=p["schema_version"] < 5) != p["data"]["files"]:
        raise ValueError("snapshot content changed: create a new protocol, do not reuse its scores")
    if daily.load_snapshot(root)["data_snapshot_id"] != p["data"]["snapshot_id"]:
        raise ValueError("snapshot ID mismatch")
    anchor = Path(p["anchor"]["run_dir"])
    for name, expected in p["anchor"]["files"].items():
        if digest(anchor / name) != expected:
            raise ValueError(f"frozen reference changed: {name}")
    return root, anchor


def align_predictions(path: Path, dates: pd.DatetimeIndex, codes: pd.Index,
                      p: dict) -> tuple[np.ndarray, dict]:
    frame = (pd.read_parquet(path) if path.suffix == ".parquet" else
             pd.read_csv(path, dtype={"code": "string"}))
    if {"target_weight", "cash_weight", "receivable_weight", "state_id"} & set(frame):
        raise ValueError("target-weight/state submissions require the separate portfolio interface")
    required = {"code", "decision_time", "task", "prediction", "model_version", "data_snapshot_id"}
    if not required <= set(frame) or frame.empty:
        raise ValueError("prediction table is empty or missing required columns")
    for field in required - {"prediction"}:
        if frame[field].isna().any():
            raise ValueError(f"prediction field has missing values: {field}")
    if set(frame["task"]) != {p["task"]["name"]}:
        raise ValueError("prediction task mismatch")
    if set(frame["data_snapshot_id"]) != {p["data"]["snapshot_id"]}:
        raise ValueError("prediction snapshot mismatch")
    if frame["model_version"].nunique() != 1:
        raise ValueError("one model_version per submission is required")
    stamps = frame["decision_time"].astype(str)
    if not stamps.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True).all():
        raise ValueError("decision_time must contain an explicit timezone")
    ts = pd.to_datetime(stamps, utc=True, errors="raise").dt.tz_convert(p["timezone"])
    day = ts.dt.tz_localize(None).dt.normalize()
    delta = ts.dt.tz_localize(None) - day
    if not (delta == pd.Timedelta(hours=15, minutes=30)).all():
        raise ValueError("prediction decision time differs from the protocol's 15:30")
    ti = dates.get_indexer(day)
    ci = codes.get_indexer(frame["code"])
    if (ti < 0).any() or (ci < 0).any():
        raise ValueError("prediction has unknown trading dates or security codes")
    keys = pd.MultiIndex.from_arrays([ti, ci])
    if keys.has_duplicates:
        raise ValueError("duplicate code/decision_time predictions")
    values = pd.to_numeric(frame["prediction"], errors="raise").to_numpy(dtype=float)
    if np.isinf(values).any():
        raise ValueError("infinite predictions are not allowed")
    out = np.full((len(dates), len(codes)), np.nan)
    out[ti, ci] = values
    return out, {"rows": len(frame), "model_version": str(frame["model_version"].iloc[0])}
