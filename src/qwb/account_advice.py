"""Preview actual holdings against a paper account's frozen model and bound feed."""
from __future__ import annotations

import pandas as pd

from . import daily, experiments, paper, strategy
from .portfolio.reference import Fees, execute_session


def preview(workdir, name, cash, holdings, execution_day):
    value = paper.inspect(workdir, name)
    config = value["config"]
    root = paper.location(workdir, name)
    if (strategy.model_identity(root / "final_model") != config["model_hash"] or
            paper.implementation_hash() != config["implementation_hash"]):
        raise ValueError("冻结模型或执行实现已改变，请停止使用此账户")
    meta, panel, snapshot_id, st, members, calendar = paper.load_inputs(config, root, workdir)
    recipe, recipe_hash = strategy.locked_recipe(meta)
    if recipe_hash != config["protocol_hash"]:
        raise ValueError("冻结协议发生变化")
    day = panel["close"].index[-1]
    execution = pd.Timestamp(execution_day)
    if execution.tzinfo is not None or execution != execution.normalize() or execution <= day or execution not in calendar:
        raise ValueError("计划执行日必须是数据截止日之后、日历内的交易日")
    op = (execution + pd.Timedelta(hours=9, minutes=30)).tz_localize("Asia/Shanghai")
    nxt = op + pd.Timedelta(days=1)
    weights = strategy.weights_on(meta, panel, st, members, day, feature_frames=daily.features(panel))
    marks = panel["close"].iloc[-1].to_dict()
    state = strategy.account(cash, holdings, marks,
                             observed_at=(day + pd.Timedelta(hours=15, minutes=30)).tz_localize("Asia/Shanghai"),
                             snapshot_id=snapshot_id, locked_until=nxt)
    target, assets, universe_id = strategy.target_for(state, weights, execution_at=op, recipe_hash=recipe_hash)
    bars = tuple(strategy.session_bar(code, open_price=float(marks[code]), close_price=float(marks[code]),
                                     upper=float(marks[code])*100, lower=float(marks[code])/100) for code in assets)
    _, audit = execute_session(state, target, bars, open_at=op,
                               close_at=op.normalize()+pd.Timedelta(hours=15), next_open_at=nxt,
                               decision_assets=assets, universe_id=universe_id, protocol_id=recipe_hash,
                               fees=Fees(**recipe["base_protocol"]["fees"]))
    quantities = {p.asset_id: p for p in state.positions}
    fills = {f["asset_id"]: f for f in audit["fills"]}
    rows = []
    for code in assets:
        pos, fill = quantities.get(code), fills.get(code)
        current = pos.quantity if pos else 0
        delta = fill["quantity"] * (1 if fill["side"] == "BUY" else -1) if fill else 0
        rows.append({"code": code, "current_quantity": current,
                     "sellable_quantity": pos.sellable_quantity if pos else 0,
                     "target_weight": weights.get(code, 0), "reference_price": float(marks[code]),
                     "suggested_delta": delta, "estimated_quantity": current+delta,
                     "estimated_cost": fill["total_cost"] if fill else 0,
                     "reason": fill["reason"] if fill else "HOLD"})
    return {"scope": "reference_price_advice_not_orders_or_fills", "date": str(day.date()),
            "execution_day": str(execution.date()), "cash": state.cash, "equity": state.equity,
            "estimated_cost": audit["total_cost"], "rows": rows, "recipe": recipe,
            "protocol_hash": recipe_hash, "model_hash": config["model_hash"],
            "input_snapshot_id": snapshot_id, "paper_account": name,
            "holdings": holdings, "run_source": str(root)}
