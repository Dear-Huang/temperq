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
OPTIONAL_FIELDS = ["down_limit"]   # used by the net-of-cost check when present
INDEX_CODES = {"csi300": "000300.SH", "csi500": "000905.SH", "csi1000": "000852.SH"}
MODELS = ("lightgbm", "ridge")
FEATURES = ["r1", "r5", "r20", "r60", "vol20", "amt20", "amt_ratio", "gap", "range",
            "to_limit"]

DEFAULTS = {
    "universe": "all",            # all | st | all_ex_st | csi300 | csi500 | csi1000 | list
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
    "ridge_alpha": 1.0,
    "shuffle_null_draws": 20,
    # net-of-cost check (qwb.netcost): top share held, commission per side in bp
    "portfolio": {"top": 0.1, "commission_bp": 3.0},
}


def _short(v) -> str:
    if isinstance(v, list) and len(v) > 5:
        return f"[{len(v)} 项]"
    return repr(v)


def deviations(cfg: dict, defaults: dict = DEFAULTS, prefix: str = "") -> list[str]:
    out = []
    # settings with no default (capital floor, holdings, model choice) are always listed
    for k, v in cfg.items():
        if k not in defaults and k != "scenario" and v not in (None, [], {}):
            out.append(f"{prefix}{k} = {_short(v)} (no default)")
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
    import pyarrow.parquet as pq
    have = set(pq.read_schema(files[0]).names)
    cols = ["code", "date"] + FIELDS + [f for f in OPTIONAL_FIELDS if f in have]
    bars = pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    if start is not None:
        bars = bars[bars["date"] >= pd.Timestamp(start)]
    if end is not None:
        bars = bars[bars["date"] <= pd.Timestamp(end)]
    return panel_from_bars(bars)


def panel_from_bars(bars: pd.DataFrame) -> dict:
    fields = FIELDS + [f for f in OPTIONAL_FIELDS if f in bars.columns]
    wide = {f: bars.pivot(index="date", columns="code", values=f).sort_index() for f in fields}
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
        # user-added sources (qwb.dataset): already aligned to the day they are usable
        **{k: v for k, v in P.items() if k.startswith("x_")},
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


def members_wide(members: pd.DataFrame, dates: pd.DatetimeIndex, codes: pd.Index,
                 index_code: str) -> pd.DataFrame:
    """Boolean date x code from constituent snapshots.

    A snapshot dated d lists the members from d until the next snapshot. A
    trading day uses the latest snapshot dated on or before it, so a later
    rebalance is never visible earlier. Days before the first snapshot are empty.
    """
    m = members[members["index"] == index_code]
    snaps = pd.to_datetime(m["date"]).drop_duplicates().sort_values()
    out = np.zeros((len(dates), len(codes)), dtype=bool)
    if snaps.empty:
        return pd.DataFrame(out, index=dates, columns=codes)
    pos = {c: i for i, c in enumerate(codes)}
    which = np.searchsorted(snaps.to_numpy(), dates.to_numpy(), side="right") - 1
    by_snap = {d: [pos[c] for c in g["code"] if c in pos]
               for d, g in m.assign(date=pd.to_datetime(m["date"])).groupby("date")}
    for k, d in enumerate(snaps):
        rows = np.flatnonzero(which == k)
        if len(rows) and by_snap.get(d):
            out[np.ix_(rows, by_snap[d])] = True
    return pd.DataFrame(out, index=dates, columns=codes)


def universe(P: dict, cfg: dict, st: pd.DataFrame | None = None,
             members: pd.DataFrame | None = None) -> pd.DataFrame:
    """Stocks eligible on each day. Every filter uses data known at that day only."""
    traded = P["close"].notna()
    ok = traded & (traded.cumsum() >= cfg["min_age_days"])
    amt = P["amount"].where(ok)
    ok &= amt.ge(amt.quantile(cfg["min_amount_quantile"], axis=1), axis=0)
    if cfg.get("min_adv_yuan"):
        # capital tier: a holding must stay a small share of the stock's recent trading
        adv = P["amount"].rolling(20, min_periods=10).mean()
        ok &= adv.ge(float(cfg["min_adv_yuan"])).fillna(False)
    u = cfg["universe"]
    if u in ("st", "all_ex_st"):
        if st is None:
            raise ValueError(f"universe={u} needs ST status data")
        ok &= st if u == "st" else ~st
    elif u in INDEX_CODES:
        if members is None:
            raise ValueError(f"universe={u} needs index constituent data (index_members.parquet)")
        ok &= members
    elif u == "list":
        names = set(cfg.get("universe_list") or [])
        if not names:
            raise ValueError("universe=list needs universe_list")
        ok &= pd.DataFrame(np.isin(P["close"].columns, list(names))[None, :].repeat(len(ok), 0),
                           index=ok.index, columns=ok.columns)
    elif u != "all":
        raise ValueError(f"unknown universe {u!r}")
    return ok


# ------------------------------------------------------------------ training

def feature_names(F: dict) -> list[str]:
    """Built-in features first, then user-added ones, in a fixed order."""
    return FEATURES + sorted(k for k in F if k.startswith("x_"))


def _stack(F: dict, mask: np.ndarray) -> np.ndarray:
    return np.column_stack([F[k].values[mask] for k in feature_names(F)]).astype("float32")


def _label_target(y: pd.DataFrame, how: str) -> pd.DataFrame:
    return y.rank(axis=1, pct=True) - 0.5 if how == "rank" else y


def ridge_fit(X: np.ndarray, t: np.ndarray, alpha: float) -> np.ndarray:
    """Ridge on centred rank features; missing values sit at the cross-section median.

    Returns [intercept, coef...]. Features are per-date ranks in [0, 1], so no
    scaling is fitted and nothing is learned from the test fold.
    """
    Xc = np.nan_to_num(X.astype("float64") - 0.5, nan=0.0)
    A = Xc.T @ Xc + alpha * len(Xc) * 1e-4 * np.eye(Xc.shape[1])
    coef = np.linalg.solve(A, Xc.T @ (t - t.mean()))
    return np.concatenate([[t.mean()], coef])


def ridge_predict(coef: np.ndarray, X: np.ndarray) -> np.ndarray:
    Xc = np.nan_to_num(X.astype("float64") - 0.5, nan=0.0)
    return coef[0] + Xc @ coef[1:]


def run(P: dict, card: dict, cfg: dict, snapshot_id: str, tz: str, st=None,
        model_dir: Path | None = None, log=print, members=None) -> dict:
    """Walk-forward yearly refits. Returns predictions, metrics inputs, manifests."""
    model = cfg.get("model", "lightgbm")
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    if model == "lightgbm":
        import lightgbm as lgb

    dates = P["close"].index
    F = features(P)
    y = labels(P, card)
    U = universe(P, cfg, st, members)
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
        if len(t) == 0 or len(Xte) == 0:
            raise ValueError(f"fold {i} ({yr}) has no training or test rows; the universe or "
                             "the capital tier leaves too few stocks")
        if model == "ridge":
            # deterministic: one fit per fold, shared by every seed slot
            coef = ridge_fit(X, t, cfg["ridge_alpha"])
            p_te = ridge_predict(coef, Xte)
            for s in cfg["seeds"]:
                pred[s][te] = p_te
            if model_dir is not None:
                p = model_dir / f"ridge_fold{i}.json"
                p.write_text(json.dumps({"features": feature_names(F), "coef": coef.tolist(),
                                         "alpha": cfg["ridge_alpha"]}), encoding="utf-8")
                models.append(p.name)
            continue
        for s in cfg["seeds"]:
            params = dict(cfg["lgbm"], seed=s, bagging_seed=s, feature_fraction_seed=s)
            booster = lgb.train(params, lgb.Dataset(X, t, feature_name=feature_names(F),
                                                    free_raw_data=True),
                                num_boost_round=cfg["num_boost_round"])
            pred[s][te] = booster.predict(Xte, num_threads=cfg["lgbm"]["num_threads"])
            if model_dir is not None:
                p = model_dir / f"lgbm_fold{i}_seed{s}.txt"
                booster.save_model(str(p))
                models.append(p.name)
    test_mask = np.isin(dates, dates[dates.year >= cfg["first_test_year"]])
    return {"dates": dates, "codes": P["close"].columns, "F": F,
            "feature_names": feature_names(F), "y": y.where(U).values,
            "U": U.values, "pred": pred, "manifests": manifests, "models": models,
            "test_rows": test_mask}


def fit_final(P: dict, card: dict, cfg: dict, snapshot_id: str, tz: str, out_dir: Path,
              F: dict, U: np.ndarray, log=print) -> dict:
    """The model to use going forward: one fit on every sample whose label has matured
    by the last day in the data (same rules as each walk-forward fold)."""
    model = cfg.get("model", "lightgbm")
    dates = P["close"].index
    y = labels(P, card)
    Ud = pd.DataFrame(U, index=dates, columns=P["close"].columns)
    target = _label_target(y.where(Ud), cfg["label_transform"])
    lend = foldlib.label_end_times(pd.Series(dates), dates, card, tz)
    lend.index = dates
    hh, mm = map(int, card["timing"]["decision"]["time"].split(":"))
    cutoff = (dates[-1] + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    dmask, man = foldlib.build_fold(-1, pd.Series(dates, index=dates), lend,
                                    (dates[0], dates[-1]), cutoff, snapshot_id, f"{model}-final")
    tm = U & dmask.values[:, None] & target.notna().values
    X, t = _stack(F, tm), target.values[tm]
    names = feature_names(F)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    if model == "ridge":
        coef = ridge_fit(X, t, cfg["ridge_alpha"])
        (out_dir / "ridge.json").write_text(json.dumps({"features": names, "coef": coef.tolist()}),
                                            encoding="utf-8")
        files.append("ridge.json")
    else:
        import lightgbm as lgb
        for s in cfg["seeds"]:
            params = dict(cfg["lgbm"], seed=s, bagging_seed=s, feature_fraction_seed=s)
            b = lgb.train(params, lgb.Dataset(X, t, feature_name=names, free_raw_data=True),
                          num_boost_round=cfg["num_boost_round"])
            b.save_model(str(out_dir / f"lgbm_seed{s}.txt"))
            files.append(f"lgbm_seed{s}.txt")
    m = man.to_dict()
    log(f"final model: {len(t)} training rows up to {m['train_end']}")
    return {"model": model, "files": files, "features": names, "train_end": m["train_end"],
            "n_train": int(len(t)), "max_label_end": m["max_label_end"]}


def predict_rows(model_dir: Path, meta: dict, X: np.ndarray) -> np.ndarray:
    """Scores from a saved final model; mean over seeds for LightGBM."""
    if meta["model"] == "ridge":
        coef = np.asarray(json.loads((model_dir / "ridge.json").read_text("utf-8"))["coef"])
        return ridge_predict(coef, X)
    import lightgbm as lgb
    preds = [lgb.Booster(model_file=str(model_dir / f)).predict(X, num_threads=4)
             for f in meta["files"]]
    return np.mean(preds, axis=0)


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
