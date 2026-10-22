"""Use a trained model: load it, score the latest day, and turn the scores into a
target holding list and the trades to get there.

Output per stock: rank, score, action (买入 / 继续持有 / 卖出), weight, reference
price (that day's close), an estimated share count in board lots, and notes on
whether the order can be filled at the next open.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from . import daily, scenario


def load(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    p = run_dir / "final_model" / "meta.json"
    if not p.is_file():
        raise FileNotFoundError("这次训练没有保存最终模型（旧版本训练的结果），请用新版重新训练一次")
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["dir"] = run_dir / "final_model"
    meta["metrics"] = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8")) \
        if (run_dir / "metrics.json").is_file() else {}
    return meta


def _lot(code: str) -> int:
    return 200 if code.startswith(("688", "689")) else 100


# Loading 8 years of full-market data and its features takes about a minute; the
# model page, the CSV downloads and a date change all need the same arrays. Keep the
# last few datasets in memory, keyed by what is on disk now, so new data is picked up.
_CACHE: dict = {}
_CACHE_MAX = 2
_LOCK = threading.Lock()


def _data_key(data: dict, cfg: dict) -> tuple:
    if data.get("source") == "synthetic":
        return ("synthetic",)
    d = Path(data["dir"])
    snap = d / "snapshot.json"
    stamp = snap.read_text(encoding="utf-8") if snap.is_file() else ""
    # the ST / index masks loaded with the panel depend on the universe kind
    return (str(d.resolve()), stamp, cfg.get("universe", "all"))


def _loaded(meta: dict, cwd: Path):
    from .pipeline import load_data

    key = _data_key(meta["data"], meta["cfg"])
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        P, snap, st, members = load_data({"data": meta["data"], "cfg": meta["cfg"]}, cwd)
        val = (P, st, members, daily.features(P))
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = val
        return val


_WARMING: set = set()


def is_ready(run_dir: Path) -> bool:
    """True if this model's data is already in memory (the page will answer at once)."""
    meta = load(run_dir)
    return _data_key(meta["data"], meta["cfg"]) in _CACHE


def warm(run_dir: Path, cwd: Path) -> None:
    """Load the model's data in the background so the page does not hang."""
    meta = load(run_dir)
    key = _data_key(meta["data"], meta["cfg"])
    with _LOCK:
        if key in _CACHE or key in _WARMING:
            return
        _WARMING.add(key)

    def go():
        try:
            _loaded(meta, cwd)
        finally:
            _WARMING.discard(key)
    threading.Thread(target=go, daemon=True).start()


def picks(run_dir: Path, cwd: Path, date: str | None = None,
          capital: float | None = None) -> dict:
    meta = load(run_dir)
    P, st, members, F = _loaded(meta, cwd)
    cfg, card = meta["cfg"], meta["card"]
    dates, codes = P["close"].index, P["close"].columns
    if date:
        d = pd.Timestamp(date)
        if d not in dates:
            raise ValueError(f"{date} 不是数据里的交易日（数据到 {dates[-1]:%Y-%m-%d}）")
        t = dates.get_loc(d)
    else:
        t = len(dates) - 1
    names = daily.feature_names(F)
    if names != meta["features"]:
        raise ValueError("数据的特征和模型训练时不一致（可能之后增删了数据来源），请重新训练")
    U = daily.universe(P, cfg, st, members).to_numpy()
    h = int(card["label"]["end"]["day"]) - int(card["label"]["start"]["day"])

    def score_day(i):
        idx = np.flatnonzero(U[i])
        if len(idx) == 0:
            return idx, np.array([])
        X = np.column_stack([F[k].to_numpy()[i, idx] for k in names]).astype("float32")
        return idx, daily.predict_rows(meta["dir"], meta, X)

    idx, sc = score_day(t)
    if len(idx) == 0:
        raise ValueError(f"{dates[t]:%Y-%m-%d} 股票池里没有可选股票")
    pf = cfg.get("portfolio", {})
    n_hold = pf.get("n_hold") or max(1, int(len(idx) * pf.get("top", 0.1)))
    order = np.argsort(-sc)
    target = set(idx[order[:n_hold]])
    prev = set()
    if t - h >= 0:
        pidx, psc = score_day(t - h)
        if len(pidx):
            prev = set(pidx[np.argsort(-psc)[:n_hold]])

    if not capital:                     # default: the capital tier chosen at training time
        capital = (cfg.get("min_adv_yuan") or 0) * scenario.PARTICIPATION * n_hold
    per_name = capital / n_hold if capital else None
    close, up = P["close"].to_numpy()[t], P["up_limit"].to_numpy()[t]
    dn = P["down_limit"].to_numpy()[t] if "down_limit" in P else np.full(len(codes), np.nan)
    rank_of = {j: r + 1 for r, j in enumerate(idx[order])}
    score_of = dict(zip(idx, sc))
    rows = []
    for j in sorted(target | prev, key=lambda j: rank_of.get(j, 10 ** 9)):
        code = codes[j]
        act = "继续持有" if j in target and j in prev else "买入" if j in target else "卖出"
        note = []
        if np.isnan(close[j]):
            note.append("今日停牌")
        elif act != "卖出" and close[j] >= up[j] - 1e-6:
            note.append("今日涨停收盘，明日开盘可能买不进")
        elif act == "卖出" and not np.isnan(dn[j]) and close[j] <= dn[j] + 1e-6:
            note.append("今日跌停收盘，明日开盘可能卖不出")
        shares = None
        if per_name and act != "卖出" and close[j] > 0:
            lot = _lot(code)
            shares = int(math.floor(per_name / close[j] / lot) * lot)
            if shares == 0:
                note.append(f"按每只约 {scenario.fmt_yuan(per_name)} 买不到 1 手")
        rows.append({"code": code, "rank": rank_of.get(j),
                     "score": float(score_of[j]) if j in score_of else None, "action": act,
            "weight": round(1 / n_hold, 4) if act != "卖出" else 0.0,
            "ref_close": None if np.isnan(close[j]) else float(close[j]),
            "est_shares": shares, "note": "；".join(note)})
    table = pd.DataFrame(rows)
    return {"date": f"{dates[t]:%Y-%m-%d}", "data_end": f"{dates[-1]:%Y-%m-%d}",
            "trade_on": "下一个交易日开盘", "hold_days": h, "n_hold": n_hold,
            "universe_size": int(len(idx)), "per_name": per_name, "capital": capital,
            "table": table,
            "full_rank": pd.DataFrame({"code": codes[idx[order]], "rank": np.arange(1, len(idx) + 1),
                                       "score": sc[order]}),
            "meta": meta, "stale_days": int((pd.Timestamp.now().normalize() - dates[-1]).days)}
