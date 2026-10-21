"""Daily template (SPEC §3, §6, §10.1): panel, features, labels, folds, LightGBM.

Everything is computed on wide date x code arrays, so per-stock operations
(shift, rolling) cannot cross stocks, and the cross-sectional rank of a
feature on day t uses only day-t data.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import folds as foldlib

FIELDS = ["open", "high", "low", "close", "amount", "adj_factor", "up_limit"]
FEATURES = ["r1", "r5", "r20", "r60", "vol20", "amt20", "amt_ratio", "gap", "range",
            "to_limit"]

DEFAULTS = {
    "universe": "all",            # all | st | all_ex_st
    "min_age_days": 60,
    "min_amount_quantile": 0.05,
    "label_transform": "rank",    # per-date rank of the label
    "first_test_year": 2021,
    "final_test_years": [2024, 2025],
    "seeds": [0, 1, 2],
    "lgbm": {"objective": "regression", "learning_rate": 0.05, "num_leaves": 63,
             "min_data_in_leaf": 1000, "feature_fraction": 0.8, "bagging_fraction": 0.8,
             "bagging_freq": 1, "lambda_l2": 1.0, "num_threads": 8, "deterministic": True,
             "force_row_wise": True, "verbose": -1},
    "num_boost_round": 300,
    "shuffle_null_draws": 20,
}


def deviations(cfg: dict, defaults: dict = DEFAULTS, prefix: str = "") -> list[str]:
    out = []
    for k, dv in defaults.items():
        if k not in cfg:
            continue
        v = cfg[k]
        if isinstance(dv, dict) and isinstance(v, dict):
            out += deviations(v, dv, f"{prefix}{k}.")
        elif v != dv:
            out.append(f"{prefix}{k} = {v!r} (default {dv!r})")
    return out


# ------------------------------------------------------------------ data

def load_panel(data_dir: str | Path, start=None, end=None) -> dict:
    data_dir = Path(data_dir)
    files = sorted((data_dir / "bars").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no bars under {data_dir / 'bars'}")
    bars = pd.concat([pd.read_parquet(f, columns=["code", "date"] + FIELDS) for f in files],
                     ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    if start is not None:
        bars = bars[bars["date"] >= pd.Timestamp(start)]
    if end is not None:
        bars = bars[bars["date"] <= pd.Timestamp(end)]
    return panel_from_bars(bars)


def panel_from_bars(bars: pd.DataFrame) -> dict:
    wide = {f: bars.pivot(index="date", columns="code", values=f).sort_index() for f in FIELDS}
    idx, cols = wide["close"].index, wide["close"].columns
    return {f: w.reindex(index=idx, columns=cols).astype("float64") for f, w in wide.items()}


def st_wide(status: pd.DataFrame, dates: pd.DatetimeIndex, codes: pd.Index) -> pd.DataFrame:
    """Boolean date x code: ST on that day (intervals are [start, end))."""
    out = pd.DataFrame(False, index=dates, columns=codes)
    st = status[status["field"] == "st"]
    for r in st.itertuples():
        if r.code not in out.columns:
            continue
        s = pd.Timestamp(r.start)
        e = pd.Timestamp(r.end) if isinstance(r.end, str) and r.end else None
        sel = (dates >= s) & ((dates < e) if e is not None else True)
        out.loc[sel, r.code] = True
    return out


# ------------------------------------------------------------- features / labels

def raw_features(P: dict) -> dict:
    ac = P["close"] * P["adj_factor"]
    ao = P["open"] * P["adj_factor"]
    r1 = ac / ac.shift(1) - 1
    amt_m = P["amount"].rolling(20, min_periods=15).mean()
    return {
        "r1": r1, "r5": ac / ac.shift(5) - 1, "r20": ac / ac.shift(20) - 1,
        "r60": ac / ac.shift(60) - 1,
        "vol20": r1.rolling(20, min_periods=15).std(),
        "amt20": np.log(amt_m.where(amt_m > 0)),
        "amt_ratio": P["amount"] / amt_m,
        "gap": ao / ac.shift(1) - 1,
        "range": (P["high"] - P["low"]) / P["close"],
        "to_limit": P["close"] / P["up_limit"],
    }


def features(P: dict) -> dict:
    """Per-date percentile ranks of the raw features (no fitted parameters)."""
    return {k: v.rank(axis=1, pct=True).astype("float32") for k, v in raw_features(P).items()}


def labels(P: dict, card: dict) -> pd.DataFrame:
    """Adjusted return from label.start to label.end (trading-day offsets)."""
    def px(tp):
        return P[tp.get("price", "open")] * P["adj_factor"]
    s, e = card["label"]["start"], card["label"]["end"]
    return px(e).shift(-int(e["day"])) / px(s).shift(-int(s["day"])) - 1


def universe(P: dict, cfg: dict, st: pd.DataFrame | None = None) -> pd.DataFrame:
    traded = P["close"].notna()
    ok = traded & (traded.cumsum() >= cfg["min_age_days"])
    amt = P["amount"].where(ok)
    ok &= amt.ge(amt.quantile(cfg["min_amount_quantile"], axis=1), axis=0)
    if cfg["universe"] != "all":
        if st is None:
            raise ValueError(f"universe={cfg['universe']} needs ST status data")
        ok &= st if cfg["universe"] == "st" else ~st
    return ok


# ------------------------------------------------------------------ training

def _stack(F: dict, mask: np.ndarray) -> np.ndarray:
    return np.column_stack([F[k].values[mask] for k in FEATURES]).astype("float32")


def _label_target(y: pd.DataFrame, how: str) -> pd.DataFrame:
    return y.rank(axis=1, pct=True) - 0.5 if how == "rank" else y


def run(P: dict, card: dict, cfg: dict, snapshot_id: str, tz: str, st=None,
        model_dir: Path | None = None, log=print) -> dict:
    """Walk-forward yearly refits. Returns predictions, metrics inputs, manifests."""
    import lightgbm as lgb

    dates = P["close"].index
    F = features(P)
    y = labels(P, card)
    U = universe(P, cfg, st)
    target = _label_target(y.where(U), cfg["label_transform"])
    lend = foldlib.label_end_times(pd.Series(dates), dates, card, tz)
    lend.index = dates
    dec = card["timing"]["decision"]
    hh, mm = map(int, dec["time"].split(":"))

    years = sorted({d.year for d in dates if d.year >= cfg["first_test_year"]})
    pred = {s: np.full(U.shape, np.nan, dtype="float64") for s in cfg["seeds"]}
    manifests, models = [], []
    for i, yr in enumerate(years):
        train_days = dates[dates.year < yr]
        test_days = dates[dates.year == yr]
        cutoff = (train_days[-1] + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
        dmask, man = foldlib.build_fold(i, pd.Series(dates, index=dates), lend,
                                        (dates[0], train_days[-1]), cutoff, snapshot_id,
                                        f"lgbm-fold{i}")
        tm = U.values & dmask.values[:, None] & target.notna().values
        X, t = _stack(F, tm), target.values[tm]
        man.n_train = int(tm.sum())          # sample rows, not dates
        manifests.append(man.to_dict())
        te = U.values & np.isin(dates, test_days)[:, None]
        Xte = _stack(F, te)
        log(f"fold {i} test {yr}: train rows {len(t)}, test rows {int(te.sum())}")
        for s in cfg["seeds"]:
            params = dict(cfg["lgbm"], seed=s, bagging_seed=s, feature_fraction_seed=s)
            booster = lgb.train(params, lgb.Dataset(X, t, feature_name=FEATURES,
                                                    free_raw_data=True),
                                num_boost_round=cfg["num_boost_round"])
            pred[s][te] = booster.predict(Xte, num_threads=cfg["lgbm"]["num_threads"])
            if model_dir is not None:
                p = model_dir / f"lgbm_fold{i}_seed{s}.txt"
                booster.save_model(str(p))
                models.append(p.name)
    test_mask = np.isin(dates, dates[dates.year >= cfg["first_test_year"]])
    return {"dates": dates, "codes": P["close"].columns, "F": F, "y": y.where(U).values,
            "U": U.values, "pred": pred, "manifests": manifests, "models": models,
            "test_rows": test_mask}


def ensemble(pred: dict) -> np.ndarray:
    """Mean over seeds; cells no seed predicted (outside test years / universe) stay NaN."""
    stack = np.stack(list(pred.values()))
    n = (~np.isnan(stack)).sum(axis=0)
    with np.errstate(invalid="ignore"):
        return np.where(n > 0, np.nansum(stack, axis=0) / np.maximum(n, 1), np.nan)


def prediction_table(res: dict, card: dict, snapshot_id: str, tz: str) -> pd.DataFrame:
    ens = ensemble(res["pred"])
    m = ~np.isnan(ens)
    ti, ci = np.nonzero(m)
    dec = card["timing"]["decision"]
    hh, mm = map(int, dec["time"].split(":"))
    dts = (res["dates"][ti] + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    return pd.DataFrame({
        "code": res["codes"][ci], "decision_time": dts.astype(str),
        "task": card["name"], "prediction": ens[m].astype("float32"),
        "model_version": "lgbm-ensemble-" + "-".join(map(str, res["pred"].keys())),
        "data_snapshot_id": snapshot_id,
    })


def load_snapshot(data_dir: str | Path) -> dict:
    return json.loads((Path(data_dir) / "snapshot.json").read_text(encoding="utf-8"))
