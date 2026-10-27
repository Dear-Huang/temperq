"""Deterministic rule signals use the same historical and daily calculation."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import uuid

import pandas as pd

from . import daily, daily_data, experiments as ex, metrics, pipeline, rule_cards, scenario


def engine_hash():
    from . import experience_rules
    return ex.object_digest({Path(m.__file__).name: ex.digest(Path(m.__file__))
                             for m in (experience_rules, rule_cards)} | {"rule_signals.py": ex.digest(Path(__file__))})


def configuration(document, universe):
    cfg = deepcopy(daily.DEFAULTS)
    # Returns and return volatility require one price before the window.
    required = max(max(r["window"] + (r["operator"] in {"return", "return_above", "low_volatility"}),
                       r["value"] if r["operator"] == "mean_above_mean" else 0)
                   for r in document["rules"])
    cfg.update(universe=universe, min_amount_quantile=0., min_age_days=max(cfg["min_age_days"], required))
    return cfg


def validate_model(meta):
    if meta.get("model") != "rule_card" or meta.get("engine_hash") != engine_hash():
        raise ValueError("经验规则实现变化，请从规则卡建立新版本")
    doc = rule_cards.validate(meta["rule_card"])
    if meta["card"] != scenario.task_card(5): raise ValueError("经验策略时点契约无效")
    expected = configuration(doc, meta["cfg"]["universe"])
    if meta["cfg"]["universe"] not in {"all", "all_ex_st"} or meta["cfg"] != expected:
        raise ValueError("经验策略股票池契约无效")
    return doc


def calculate(panel, document, cfg, st=None, members=None):
    adjusted = dict(panel)
    factor = panel["adj_factor"].where(panel["adj_factor"] > 0)
    for field in rule_cards.PRICE_FIELDS:
        adjusted[field] = panel[field] * factor
    result = rule_cards.scorecard(document).evaluate(adjusted)
    universe = daily.universe(panel, cfg, st, members)
    scores = result["scores"].where(universe & result["eligible"])
    return scores, result, universe


def create(workdir, feed, document, *, parent=None):
    doc = rule_cards.validate(document)
    snapshot = daily_data.current(feed)
    info = ex.read(snapshot / "snapshot.json")
    cfg = configuration(doc, "all_ex_st" if info.get("has_st") else "all")
    panel, snapshot_id, st, members = pipeline.load_data(
        {"data": {"source": "snapshot", "dir": str(snapshot)}, "cfg": cfg}, Path(workdir))
    scores, detail, universe = calculate(panel, doc, cfg, st, members)
    ready = universe.any(axis=1) & ~(universe & ~detail["valid"]).any(axis=1)
    scores = scores.where(ready, axis=0)
    rows = scores.rename_axis(index="date", columns="code").stack().dropna().rename("prediction").reset_index()
    if not ready.any(): raise ValueError("规则缺少足够的有效历史，请补齐窗口和所需字段；原草稿已保留")
    rows["decision_time"] = (pd.to_datetime(rows.pop("date")).dt.tz_localize("Asia/Shanghai")
                             + pd.Timedelta(hours=15, minutes=30)).astype(str)
    card = scenario.task_card(5)
    y = daily.labels(panel, card)
    ic = metrics.newey_west(metrics.rank_ic(scores.to_numpy(), y.to_numpy()), 5)
    summary = {"model_key": "rule_card", "ic": {"rule_card": {"final_test": ic}},
        "scope": "historical_development", "evidence": "INSUFFICIENT", "rule_card": rule_cards.report(doc),
        "coverage": {"valid_cells": int((detail["valid"] & universe).to_numpy().sum()),
                     "minimum_history_sessions": cfg["min_age_days"],
                     "universe_cells": int(universe.to_numpy().sum()), "signal_cells": len(rows),
                     "invalid_days": int((universe.any(axis=1) & ~ready).sum())}, "parent": parent,
        "limitations": ["模板和修改后查看的历史属于开发分析，不是独立最终留出。", "排序能力不等于扣成本收益。"]}
    if parent:
        if not isinstance(parent, str) or not parent.startswith("rule_") or not ex.ID_RE.fullmatch(parent[5:]):
            raise ValueError("参考规则版本无效")
        parent_path = Path(workdir) / "runs" / parent
        previous = ex.read(parent_path / "final_model/meta.json")
        base_doc = validate_model(previous)
        if previous["cfg"]["universe"] != cfg["universe"]:
            raise ValueError("参考策略股票池不同，需要建立新的比较系列")
        base, base_detail, base_universe = calculate(panel, base_doc, previous["cfg"], st, members)
        base_ready = base_universe.any(axis=1) & ~(base_universe & ~base_detail["valid"]).any(axis=1)
        base = base.where(base_ready, axis=0)
        delta = metrics.rank_ic(scores.to_numpy(), y.to_numpy()) - metrics.rank_ic(base.to_numpy(), y.to_numpy())
        summary["paired_previous"] = {"rank_ic_difference": metrics.newey_west(delta, 5),
            "candidate_cells": int(scores.notna().to_numpy().sum()), "reference_cells": int(base.notna().to_numpy().sum()),
            "scope": "paired_dates; eligibility_changes_are_part_of_strategy"}
    source = Path(workdir) / "runs" / ("rule_" + uuid.uuid4().hex)
    source.mkdir(parents=True)
    try:
        (source / "final_model").mkdir()
        (source / "final_model/rule_card.json").write_text(rule_cards.dumps(doc), encoding="utf-8")
        ex.write(source / "final_model/signal_dates.json", {"dates": [str(d.date()) for d in scores.index[ready]]})
        ex.write(source / "final_model/meta.json", {"model": "rule_card", "name": doc["name"],
            "files": ["rule_card.json", "signal_dates.json"], "features": [], "card": card, "cfg": cfg,
            "data_end": str(panel["close"].index[-1].date()), "rule_card": doc,
            "engine_hash": engine_hash(), "data_snapshot_id": snapshot_id})
        rows.to_parquet(source / "predictions.parquet", index=False)
        ex.write(source / "metrics.json", summary)
        return source
    except BaseException:
        shutil.rmtree(source, ignore_errors=True)
        raise


def live_scores(meta, panel, st, members, days):
    doc = validate_model(meta)
    scores, detail, universe = calculate(panel, doc, meta["cfg"], st, members)
    result = {}
    for day in days:
        unknown = universe.loc[day] & ~detail["valid"].loc[day]
        if unknown.any():
            raise ValueError(f"{day:%Y-%m-%d} 规则输入缺失或窗口不足：{int(unknown.sum())} 只；补齐数据后重试")
        values = scores.loc[day].dropna()
        result[str(day.date())] = {str(code): float(value) for code, value in values.items()}
    return result
