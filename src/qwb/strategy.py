"""Small CSV-account adapter for saved B0 models and the raw-share reference."""

from __future__ import annotations

import io
import json
import re
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from . import daily, daily_data, experiments, picks, pipeline, rules
from .portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights, TASK
from .portfolio.reference import ExecutionBar, Fees, execute_session

HOLDING_COLUMNS = {"code", "quantity", "sellable_quantity"}


def protocol():
    value = deepcopy(rules.load("daily_strategy_v1"))
    return value, experiments.object_digest(value)


def stock_code(value):
    code = str(value).strip().upper()
    if re.fullmatch(r"(SH|SZ)\.\d{6}", code):
        code = code[3:] + "." + code[:2]
    if re.fullmatch(r"\d{6}", code):
        code += ".SH" if code.startswith("6") else ".SZ"
    if not (re.fullmatch(r"60\d{4}\.SH|68[89]\d{3}\.SH|00\d{4}\.SZ|30\d{4}\.SZ", code)):
        raise ValueError(f"不支持的证券 {code}：仅支持沪深主板、创业板与科创板 A 股，北交所等暂不支持")
    return code


def read_holdings(source):
    """Quantities are the user's declared holdings/sellable quantities at execution."""
    if source is None or source == b"":
        return []
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    frame = pd.read_csv(source, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    if set(frame.columns) != HOLDING_COLUMNS:
        raise ValueError("持仓 CSV 只能有 code,quantity,sellable_quantity 三列；公司行动、特殊订单等不支持")
    result = []
    for row in frame.to_dict("records"):
        code = stock_code(row["code"])
        for field in ("quantity", "sellable_quantity"):
            if not re.fullmatch(r"\d+", str(row[field]).strip()):
                raise ValueError(f"{code} 的 {field} 必须是非负整数")
        quantity, sellable = int(row["quantity"]), int(row["sellable_quantity"])
        if quantity <= 0 or not 0 <= sellable <= quantity:
            raise ValueError(f"{code} 数量需大于零，且可卖数量不能超过持仓")
        result.append({"code": code, "quantity": quantity, "sellable_quantity": sellable})
    if len({r["code"] for r in result}) != len(result):
        raise ValueError("持仓代码重复，请先合并")
    return sorted(result, key=lambda row: row["code"])


def locked_recipe(meta):
    card = meta["card"]
    if meta["model"] not in {"lightgbm", "ridge"}:
        raise ValueError("每日策略首版只支持 LightGBM / Ridge")
    if (card.get("market") != "cn_a" or
            card["label"]["start"].get("price") != "open" or
            card["label"]["end"].get("price") != "open" or
            int(card["label"]["start"]["day"]) != 1 or
            int(card["label"]["end"]["day"]) != 6 or
            card["timing"]["decision"].get("time") != "15:30"):
        raise ValueError("首版冻结为收盘后 15:30 决策、次日开盘到第 6 日开盘的 5 日标签")
    base, base_hash = protocol()
    recipe = {"base_protocol": base, "base_protocol_hash": base_hash,
              "market_rules_hash": experiments.digest(Path(rules.__file__).parent / "cn_a.yaml"),
              "task": card, "training_configuration": meta["cfg"],
              "features": meta["features"], "feature_recipe": "daily.features_rank_on_input_panel_v1"}
    return recipe, experiments.object_digest(recipe)


def model_identity(model_dir):
    meta = experiments.read(Path(model_dir) / "meta.json")
    names = ["meta.json", *meta["files"]]
    if any(Path(name).name != name for name in names):
        raise ValueError("模型文件路径非法")
    return experiments.object_digest({name: experiments.digest(Path(model_dir) / name) for name in names})


def session_bar(code, *, open_price, close_price, upper, lower, suspended=False):
    code = stock_code(code)
    quantity = protocol()[0]["quantity_rules"][rules.cn_board(code)]
    return ExecutionBar(code, open_price, close_price, upper, lower, suspended=suspended,
                        buy_lot=quantity["buy_step"], sell_lot=quantity["sell_step"],
                        minimum_buy=quantity["minimum_buy"], minimum_sell=quantity["minimum_sell"])


def account(cash, holdings, marks, *, observed_at, snapshot_id, locked_until):
    if not np.isfinite(float(cash)) or float(cash) < 0:
        raise ValueError("现金必须是非负有限数字")
    positions = []
    for row in holdings:
        code, quantity, sellable = row["code"], row["quantity"], row["sellable_quantity"]
        stock_code(code)
        if code not in marks or not np.isfinite(marks[code]) or marks[code] <= 0:
            raise ValueError(f"持仓 {code} 缺少有效估值，不能忽略持仓")
        locked = (SettlementLot(quantity - sellable, str(locked_until)),) if quantity > sellable else ()
        positions.append(Position(code, quantity, sellable, float(marks[code]), str(observed_at), locked))
    return AccountState(str(observed_at), snapshot_id, float(cash), tuple(positions))


def target_for(state, weights, *, execution_at, recipe_hash):
    assets = tuple(sorted(set(weights) | {p.asset_id for p in state.positions}))
    universe_id = experiments.object_digest(assets)
    target = TargetWeights(experiments.object_digest([state.state_id, weights, recipe_hash]),
                           state.observed_at, str(execution_at), TASK, recipe_hash,
                           state.data_snapshot_id, state.state_id, universe_id, recipe_hash,
                           tuple(sorted(weights.items())), max(0.0, 1 - sum(weights.values())))
    return target, assets, universe_id


def weights_on(meta, panel, st, members, day, *, feature_frames=None):
    stamp = pd.Timestamp(day).normalize()
    if stamp < pd.Timestamp(meta["data_end"]):
        raise ValueError("不能用最终模型回填其训练数据截止前的历史决策")
    dates, codes = panel["close"].index, panel["close"].columns
    if stamp not in dates:
        raise ValueError(f"行情中没有 {stamp:%Y-%m-%d}")
    t = dates.get_loc(stamp)
    features = feature_frames if feature_frames is not None else daily.selected_features(panel, meta["cfg"])
    features = {name: features[name] for name in meta["features"] if name in features}
    if daily.feature_names(features) != meta["features"]:
        raise ValueError("新数据特征与训练不一致")
    eligible = daily.universe(panel, meta["cfg"], st, members).iloc[t].to_numpy()
    supported = np.array([bool(re.fullmatch(r"60\d{4}\.SH|68[89]\d{3}\.SH|00\d{4}\.SZ|30\d{4}\.SZ", c))
                          for c in codes])
    idx = np.flatnonzero(eligible & supported)
    if not len(idx):
        raise ValueError("没有受支持且符合模型股票池的证券")
    x = np.column_stack([features[name].iloc[t].to_numpy()[idx] for name in meta["features"]])
    scores = daily.predict_rows(Path(meta["dir"]), meta, x)
    if not np.isfinite(scores).all():
        raise ValueError("模型打分包含无效值")
    pf = meta["cfg"].get("portfolio", {})
    count = min(len(idx), int(pf.get("n_hold") or max(1, int(len(idx) * pf.get("top", 0.1)))))
    if count <= 0:
        raise ValueError("持仓只数必须大于零")
    chosen = idx[np.argsort(-scores, kind="stable")[:count]]
    return {str(codes[j]): 1.0 / count for j in chosen}


def preview(run_dir, cwd, cash, holdings, execution_day):
    meta = picks.load(Path(run_dir))
    recipe, recipe_hash = locked_recipe(meta)
    input_snapshot_id = meta["snapshot_id"]
    if meta["data"].get("source") != "synthetic":
        source = meta["data"].get("dir") or pipeline.find_local_config(Path(cwd)).get("data_dir")
        if not source:
            raise ValueError("模型未绑定行情目录")
        path = Path(source)
        path = path if path.is_absolute() else Path(cwd) / path
        if path.parent.name == "snapshots" and (path.parent.parent / "current.json").is_file():
            path = path.parent.parent
        if (path / "current.json").is_file():
            path = daily_data.current(path)
        meta["data"] = dict(meta["data"], dir=str(path.resolve()))
        if (path / "snapshot.json").is_file():
            input_snapshot_id = experiments.read(path / "snapshot.json")["data_snapshot_id"]
    panel, st, members, features = picks._loaded(meta, Path(cwd))
    day = panel["close"].index[-1]
    execution = pd.Timestamp(execution_day)
    if execution != execution.normalize() or execution.tzinfo is not None or execution <= day:
        raise ValueError("执行日需为数据截止日之后的交易日，格式 YYYY-MM-DD")
    observed = (day + pd.Timedelta(hours=15, minutes=30)).tz_localize("Asia/Shanghai")
    op = (execution + pd.Timedelta(hours=9, minutes=30)).tz_localize("Asia/Shanghai")
    close = op.normalize() + pd.Timedelta(hours=15)
    nxt = op + pd.Timedelta(days=1)
    weights = weights_on(meta, panel, st, members, day, feature_frames=features)
    marks = panel["close"].iloc[-1].to_dict()
    # This is a price estimate only; the resulting hypothetical state is discarded.
    state = account(cash, holdings, marks, observed_at=observed,
                    snapshot_id=input_snapshot_id, locked_until=nxt)
    target, assets, universe_id = target_for(state, weights, execution_at=op, recipe_hash=recipe_hash)
    bars = tuple(session_bar(code, open_price=float(marks[code]), close_price=float(marks[code]),
                             upper=float(marks[code]) * 100, lower=float(marks[code]) / 100) for code in assets)
    _, audit = execute_session(state, target, bars, open_at=op, close_at=close, next_open_at=nxt,
                               decision_assets=assets, universe_id=universe_id, protocol_id=recipe_hash,
                               fees=Fees(**recipe["base_protocol"]["fees"]))
    quantities = {p.asset_id: p for p in state.positions}
    fills = {f["asset_id"]: f for f in audit["fills"]}
    rows = []
    for code in assets:
        pos, fill = quantities.get(code), fills.get(code)
        current = pos.quantity if pos else 0
        delta = (fill["quantity"] * (1 if fill["side"] == "BUY" else -1)) if fill else 0
        rows.append({"code": code, "current_quantity": current,
                     "sellable_quantity": pos.sellable_quantity if pos else 0,
                     "target_weight": weights.get(code, 0.0), "reference_price": float(marks[code]),
                     "suggested_delta": delta, "estimated_quantity": current + delta,
                     "estimated_cost": fill["total_cost"] if fill else 0.0,
                     "reason": fill["reason"] if fill else "HOLD"})
    return {"scope": "reference_price_advice_not_orders_or_fills", "date": str(day.date()),
            "execution_day": str(execution.date()), "protocol_hash": recipe_hash,
            "base_protocol_hash": recipe["base_protocol_hash"], "recipe": recipe,
            "model_hash": model_identity(meta["dir"]), "input_snapshot_id": input_snapshot_id,
            "cash": state.cash, "equity": state.equity,
            "estimated_cost": audit["total_cost"], "rows": rows,
            "limitations": ["执行日由用户确认，参考价不是成交价", "CSV 可卖数量以本次执行时券商可卖数量为准",
                            "公司行动、北交所、特殊订单和券商下单不支持"]}
