"""Profit-and-loss decomposition built only from audited modules.

Answers four questions in order: where the money went (gross, fees, spread
scenario), which stocks carried the result, what kind of trades made or lost
money (holding period, price band), and what the worst drawdown was made of.
Every split is checked to close back to the account's net P&L.
"""
from __future__ import annotations

TOLERANCE = 1.0  # yuan


def _y(v):
    """Whole yuan without a stray minus on rounding to zero."""
    return f"¥{(0. if abs(v) < .5 else v):,.0f}"


def _band_rows(bands, key="closed"):
    rows = []
    for b in bands or []:
        part = b.get(key, b)
        if part.get("matches"):
            rows.append({"label": b["label"], "matches": part["matches"], "gross": part["gross_pnl"],
                         "fees": part["fees"], "net": part["net_pnl"], "after_tick": part["net_after_tick_scenario"]})
    return rows


def build(result):
    modules = result.get("modules", {})
    attr, risk = modules.get("attribution"), modules.get("risk_analysis") or {}
    if not attr or result.get("mode", "full") != "full":
        return None
    tick = (modules.get("tick_analysis") or {}).get("accounted_intervals") or {}
    holding = modules.get("holding_analysis") or {}
    stocks = sorted(attr["stocks"], key=lambda s: s["net_pnl"], reverse=True)
    net, gross, fees = attr["net_pnl"], attr["gross_pnl"], attr["fees"]
    winners = [s for s in stocks if s["net_pnl"] > 0]
    losers = [s for s in stocks if s["net_pnl"] < 0]

    # 1. waterfall: gross → fees → net → spread scenario
    spread = tick.get("tick_scenario_cost")
    waterfall = [{"label": "费用前盈亏", "value": gross},
                 {"label": "已扣佣金与印花税", "value": -fees},
                 {"label": "回测净盈亏", "value": net, "total": True}]
    if spread is not None:
        waterfall += [{"label": "买卖价差（最小价位情景）", "value": -spread},
                      {"label": "扣价差情景后", "value": net - spread, "total": True}]

    # 2. concentration: how much of the net result the top names carry
    top = stocks[:5]
    from .securities import asset_class
    noun = "股票" if all(asset_class(s["code"]) == "stock" for s in stocks) else "标的"
    concentration = {"noun": noun,
        "stocks": len(stocks), "winners": len(winners), "losers": len(losers),
        "winning_total": sum(s["net_pnl"] for s in winners), "losing_total": sum(s["net_pnl"] for s in losers),
        "top": top, "bottom": list(reversed(stocks[-5:])) if len(stocks) > 5 else [],
        "top5_net": sum(s["net_pnl"] for s in top),
        "rest_net": net - sum(s["net_pnl"] for s in top),
    }

    # 3. by trade type, closed lots only (FIFO); open lots are listed separately
    by_holding = _band_rows(holding.get("closed_age_bands"), key=None) if holding.get("status") == "verified" else []
    by_price = _band_rows(holding.get("entry_price_bands")) if holding.get("status") == "verified" else []
    closed = holding.get("closed_summary") or {}
    open_lots = holding.get("open_summary") or {}

    # 4. worst drawdown
    dd = risk.get("max_drawdown") or {}
    drawdown = None
    if dd.get("loss"):
        names = sorted(dd.get("stocks") or [], key=lambda s: s["net_pnl"])
        drawdown = {"ratio": dd["ratio"], "peak": dd["peak_date"], "trough": dd["trough_date"],
                    "loss": dd["loss"], "fees": dd.get("fees"), "recovered": dd.get("recovery_date"),
                    "worst": names[:5], "gap": dd.get("contribution_gap")}

    checks = {
        "stocks_close": abs(sum(s["net_pnl"] for s in stocks) - net) <= TOLERANCE,
        "fees_close": abs(gross - fees - net) <= TOLERANCE,
        "drawdown_close": drawdown is None or abs(drawdown["gap"] or 0) <= TOLERANCE,
    }
    return {"verified": attr["status"] == "verified" and all(checks.values()), "checks": checks,
            "net": net, "gross": gross, "fees": fees, "spread": spread, "waterfall": waterfall,
            "concentration": concentration, "by_holding": by_holding, "by_price": by_price,
            "closed": closed, "open": open_lots, "drawdown": drawdown,
            "summary": summary(net, gross, fees, spread, concentration, by_holding, drawdown)}


def summary(net, gross, fees, spread, conc, by_holding, drawdown):
    """Three plain sentences for the top of the page."""
    lines = []
    fee_share = fees / gross if gross > 0 else None
    line = f"费用前赚 {_y(gross)}，佣金和印花税扣掉 {_y(fees)}"
    line += f"（占 {fee_share:.0%}）" if fee_share is not None else ""
    line += f"，回测净盈亏 {_y(net)}。"
    if spread is not None:
        line += f"再按最小价位估算买卖价差约 {_y(spread)}，扣除后为 {_y(net - spread)}。"
    lines.append(line)
    noun = conc.get("noun", "股票")
    line = f"{conc['stocks']} 只{noun}里 {conc['winners']} 只赚钱、{conc['losers']} 只亏钱"
    if conc["stocks"] > 5:
        line += f"；盈利前 5 只合计 {_y(conc['top5_net'])}，其余合计 {_y(conc['rest_net'])}"
    elif conc["top"]:
        best = conc["top"][0]
        line += f"；贡献最大的是 {best['code']}，净赚 {_y(best['net_pnl'])}"
    lines.append(line + "。")
    if by_holding:
        worst = min(by_holding, key=lambda r: r["after_tick"])
        lines.append(f"按持有期看，亏得最多的是「{worst['label']}」的 {worst['matches']} 笔交易，扣价差情景后 {_y(worst['after_tick'])}。")
    elif drawdown:
        lines.append(f"最大回撤 {drawdown['ratio']:.1%}，从 {drawdown['peak']} 到 {drawdown['trough']}。")
    return lines
