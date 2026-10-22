"""Where the model will be used (UI step 1), and what follows from it.

The scenario fixes the task card (holding period), the universe, and the
capital tier; the capital tier fixes how many names are held and the
liquidity floor, so the trained model is evaluated on stocks the user could
actually buy. Options shown in the UI but not implemented are listed with
status "planned" and cannot be selected.
"""

from __future__ import annotations

# Each holding may be at most this share of the stock's 20-day mean daily
# traded value. Conservative for an open-auction / first-hour execution.
PARTICIPATION = 0.05

# key, label, capital used (upper bound of the tier, yuan), names held
CAPITAL_TIERS = [
    ("lt30w", "0–30 万", 300_000, 10),
    ("30w_100w", "30–100 万", 1_000_000, 20),
    ("100w_600w", "100–600 万", 6_000_000, 30),
    ("600w_1000w", "600 万–1000 万", 10_000_000, 40),
    ("1000w_1e", "1000 万–1 亿", 100_000_000, 50),
    ("1e_5e", "1 亿–5 亿", 500_000_000, 100),
    ("5e_plus", "5 亿及以上", 1_000_000_000, 100),
]
TIERS = {k: {"label": lbl, "capital": cap, "n_hold": n} for k, lbl, cap, n in CAPITAL_TIERS}
DEFAULT_TIER = "100w_600w"

HORIZONS = {1: "日频：持有 1 天", 5: "周频：持有 5 天", 20: "月频：持有 20 天"}
DEFAULT_HORIZON = 5

UNIVERSES = {
    "all_ex_st": "全 A 股（剔除 ST）",
    "all": "全 A 股（含 ST）",
    "st": "只做 ST 股",
    "csi300": "沪深 300 成分股",
    "csi500": "中证 500 成分股",
    "csi1000": "中证 1000 成分股",
    "list": "自选股票名单",
}
INDEX_UNIVERSES = {"csi300": "000300.SH", "csi500": "000905.SH", "csi1000": "000852.SH"}

# what the user picks as the model; only "ready" ones can be trained
MODELS = {
    "lightgbm": {"label": "LightGBM（梯度提升树）", "status": "ready",
                 "note": "表格数据上的业内基准。速度快、稳定，只用 CPU，任何电脑都能跑。"},
    "ridge": {"label": "Ridge（线性回归）", "status": "ready",
              "note": "最简单、最好解释，几秒钟训练完。适合当底线：复杂模型连它都赢不了就不值得用。"},
    "xgboost": {"label": "XGBoost / CatBoost", "status": "planned",
                "note": "和 LightGBM 同一类方法，结果通常接近。"},
    "kronos": {"label": "Kronos（K 线预训练大模型）", "status": "planned",
               "note": "需要 NVIDIA 显卡。实测零样本排序能力弱于 LightGBM（IC 0.034 对 0.114），"
                       "擅长的是预测未来多根 K 线的价格路径。尚未接入一键训练。"},
}
DEFAULT_MODEL = "lightgbm"

PLANNED = {
    "market": [("us", "美股")],
    "horizon": [("intraday", "分钟级 / 日内")],
    "direction": [("long_short", "多空（A 股融券受限）")],
}


def derive(tier: str) -> dict:
    """Holdings and liquidity floor implied by a capital tier."""
    t = TIERS[tier]
    per_name = t["capital"] / t["n_hold"]
    return {"label": t["label"], "capital": t["capital"], "n_hold": t["n_hold"],
            "per_name": per_name, "min_adv_yuan": per_name / PARTICIPATION}


def task_card(horizon: int, market: str = "cn_a") -> dict:
    """Rank stocks by the open-to-open return over `horizon` trading days."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {sorted(HORIZONS)}")
    return {
        "name": f"cn_rank_{horizon}d",
        "market": market,
        "template": "daily",
        "target": {"type": "return_rank", "horizon_days": horizon},
        "timing": {"signal_available": {"day": 0, "time": "15:00"},
                   "decision": {"day": 0, "time": "15:30"},
                   "execution": {"day": 1, "time": "09:30", "price": "open"}},
        "label": {"start": {"day": 1, "time": "09:30", "price": "open"},
                  "end": {"day": 1 + horizon, "time": "09:30", "price": "open"}},
        "output": "score",
        "metrics": ["rank_ic"],
        "acceptance": {
            "confirmatory": {"metric": "rank_ic_mean"},
            "go": "rank_ic_mean > 0 with NW t > 2 on the final test years, and above every baseline",
            "no_go": "NW t < 2 or not above the best simple baseline"},
    }


def fmt_yuan(x: float) -> str:
    if x >= 1e8:
        return f"{x / 1e8:g} 亿"
    if x >= 1e4:
        return f"{x / 1e4:g} 万"
    return f"{x:,.0f} 元"
