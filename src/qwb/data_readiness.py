"""Explain data sufficiency before training; these checks do not certify alpha."""
from __future__ import annotations

import html

import pandas as pd


def auto_years(snap):
    y0, y1 = int(str(snap["start"])[:4]), int(str(snap["end"])[:4])
    final = [y1 - 1, y1] if y1 - y0 + 1 >= 5 else [y1]
    return max(y0 + 1, final[0] - 3), final


def csv_profile(bars, columns):
    """Count observed price rows, before task, age, liquidity and ST filters."""
    valid = bars["open"].gt(0) & bars["close"].gt(0) & bars["amount"].gt(0) & bars["adj_factor"].gt(0)
    counts = bars.loc[valid].groupby("date")["code"].nunique()
    dates = pd.Index(sorted(bars["date"].unique()))
    counts = counts.reindex(dates, fill_value=0)
    return {"schema": "csv_data_readiness_v1", "provided_columns": sorted(columns),
            "dates_by_year": {str(year): int(sum(dates.str.startswith(str(year))))
                              for year in sorted({str(day)[:4] for day in dates})},
            "dates_with_20_price_rows": int(counts.ge(20).sum()),
            "median_price_names": float(counts.median())}


def assess(snap, *, horizon=5, synthetic=False):
    if synthetic:
        return {"label": "流程演示", "blocked": False, "messages": [
            "合成数据无需准备历史；只熟悉操作，不能评价真实收益或启用真实前向账户。"]}
    messages, blocked = [], False
    start, end = pd.Timestamp(snap["start"]), pd.Timestamp(snap["end"])
    days, codes = snap.get("trading_days"), snap.get("codes")
    profile = snap.get("readiness") or {}
    first, final = auto_years(snap)
    messages.append(f"当前范围 {start:%Y-%m-%d} 至 {end:%Y-%m-%d}；"
                    f"{days if days is not None else '未知'} 个交易日，{codes if codes is not None else '未知'} 只股票。")
    messages.append(f"当前自动划分：{first} 年起滚动评价，最终检验使用 {'、'.join(map(str, final))} 年；每折只用之前的数据训练。")
    if codes is not None and int(codes) < 20:
        blocked = True
        messages.append("不足 20 只股票，无法满足独立截面基准每天至少 20 只有效股票的要求；请补充完整股票池。")
    elif codes is not None and int(codes) < 30:
        messages.append("少于建议的 30 只股票；上市时长、ST 和流动性筛选后可能不足 20 只。优先补充同一历史股票池。")
    if end - start < pd.Timedelta(days=365.25 * 3) or (days is not None and int(days) < 720):
        messages.append("历史偏短：建议至少约 3 整年、720 个真实交易日；仅跨 3 个年份不等于有 3 年历史。推荐 5–8 年以覆盖不同市场阶段。")
    elif end - start < pd.Timedelta(days=365.25 * 5):
        messages.append("已有约 3 年历史，可先研究；推荐扩展至 5–8 年，并保留最近 1–2 年作检验。")
    counts = profile.get("dates_by_year") or {}
    if counts:
        test_days = sum(counts.get(str(year), 0) for year in final)
        messages.append(f"最终检验年份有 {test_days} 个原始交易日；未扣除标签未成熟、停牌及股票池筛选。")
        if test_days <= int(horizon) + 1:
            blocked = True
            messages.append("最终检验期太短，无法形成足够的成熟收益标签；请补充连续行情后再训练。")
        elif test_days < 160:
            messages.append("检验日期偏少：默认基准的最长 40 日块稳健性检查至少需要 160 个有效检验日；建议保留 1–2 年，160 日不保证信号显著。")
        if profile.get("dates_with_20_price_rows", 0) == 0:
            blocked = True
            messages.append("没有任何一天同时具备 20 只有效价格股票；多只股票分散在不同日期仍无法做截面检验。")
    provided = profile.get("provided_columns")
    if provided is not None:
        if "adj_factor" not in provided:
            messages.append("缺少 adj_factor：除权可能被误当成收益；请使用原始未复权 OHLC 加同源复权因子。每日更新必须提供该列。")
        if "is_st" not in provided:
            messages.append("缺少历史 is_st，不能可靠剔除 ST；请提供逐日标记，不能用今天的名单回填历史。")
    if horizon != 5:
        messages.append("当前每日纸面交易只支持 5 日、次日开盘到第 6 日开盘；此持有期可训练研究，暂不能进入每日闭环。")
    messages.append("这是元数据初检，不代表数据时点、退市覆盖或模型有效性已通过；更长数据和更多股票也不保证通过。")
    return {"label": "先补数据" if blocked else "训练前数据提示", "blocked": blocked, "messages": messages}


def render(value):
    return ('<div class="notice" role="status"><h3>' + html.escape(value["label"]) +
            '</h3><ul>' + ''.join('<li>' + html.escape(message) + '</li>' for message in value["messages"]) + '</ul></div>')


REQUIREMENTS = """<details class="data-section"><summary>需要准备什么数据？建议至少 3 年、720 个交易日（点开看详细要求）</summary>
<p><b>做 A 股日频排序，建议至少约 3 整年、720 个交易日，30 只以上同时覆盖的股票；推荐 5–8 年及较完整的历史股票池。</b>
可以从数百只股票开始。独立基准要求每个有效检验日至少 20 只合格股票，筛选后数量会减少；股票行数不能替代时间长度。</p>
<p>按时间先后训练和检验，建议保留最近 1–2 年。当前 CSV 导入硬要求是至少跨 3 个年份，这只是格式入口，不是有效性门槛。
5 日标签需要再往后 6 个交易日的开盘价才能计算；最后几天不能立即算出检验收益。默认基准最长块检查至少要 160 个有效检验日，也不保证显著。</p>
<div class="tw"><table><tr><th>数据</th><th>准备要求</th></tr>
<tr><td>基础日线（必需）</td><td>每行一只股票一天：code、date、open、high、low、close、amount。成交额单位为元，OHLC 使用原始未复权价。</td></tr>
<tr><td>复权与股票池（强烈建议）</td><td>同源 adj_factor、逐日 is_st；研究历史股票池要包含当时存在、后来退市的股票，不能只选今天仍上市的赢家。指数池还需历史成分。</td></tr>
<tr><td>纸面交易（后续必需）</td><td>持续更新完整股票池、adj_factor、涨跌停价格与停牌状态，以及覆盖下个交易日的 calendar.csv（date,is_open）；当前只支持 5 日任务。</td></tr>
<tr><td>公告、财务、因子（可选）</td><td>基础模型不要求；添加时必须带真实可用时点，不能提前使用未来公告或修订值。</td></tr></table></div>
<p class="mut">数据达标表示具备研究起点，不保证模型能赚钱。免费日线路径见使用手册；上传前可下载模板。</p></details>"""
