"""Build the in-repo format regression fixtures (AUDIT_EVIDENCE_SPEC R8).

One small synthetic account is written in every supported platform format, using the real
export headers kept in tests/fixtures/formats/headers. Every format must import, close its
books, and give the expected fill count. Nothing here is market data.

    python scripts/make_format_fixtures.py

Rewrites tests/fixtures/formats/samples/ and manifest.json. Run it only when a format changes
on purpose; tests/test_format_regression.py fails if a sample changes without the manifest.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/formats"
SAMPLES = ROOT / "samples"
DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11"]
NAMES = {"600000.SH": "合成股票甲", "000001.SZ": "合成股票乙"}
CLOSE = {"600000.SH": [10.0, 10.1, 10.2, 10.1, 10.3, 10.1, 10.2, 10.4],
         "000001.SZ": [12.0, 12.2, 12.1, 12.3, 12.4, 12.3, 12.5, 12.5]}
# (day index, code, side, quantity, price)
TRADES = [(0, "600000.SH", "buy", 1000, 10.0), (0, "000001.SZ", "buy", 1000, 12.0),
          (4, "600000.SH", "sell", 1000, 10.3), (5, "600000.SH", "buy", 500, 10.1),
          (7, "000001.SZ", "sell", 1000, 12.5)]
DIVIDEND = (2, "600000.SH", 200.)        # 0.2 yuan per share, paid on 2024-01-04 with no trade that day
OPENING_CASH = 100000.
CODE = "def initialize(context):\n    set_order_cost(OrderCost(open_commission=0.0003, close_commission=0.0003,"\
       " close_tax=0.0005, min_commission=5), type='stock')\n    g.stock_num = 2\n"


def fee(side, notional):
    return round(max(5., notional * 3e-4) + (notional * 5e-4 if side == "sell" else 0.), 2)


def ledger():
    cash, held, rows, fills = OPENING_CASH, {}, [], []
    for i, day in enumerate(DAYS):
        if DIVIDEND[0] == i:
            cash += DIVIDEND[2]
        for j, code, side, qty, price in TRADES:
            if j != i:
                continue
            notional = round(qty * price, 2)
            f = fee(side, notional)
            cash += -notional - f if side == "buy" else notional - f
            held[code] = held.get(code, 0) + (qty if side == "buy" else -qty)
            fills.append({"date": day, "code": code, "side": side, "quantity": qty, "price": price, "notional": notional, "fee": f})
        held = {c: q for c, q in held.items() if q}
        holdings = [{"code": c, "quantity": q, "price": CLOSE[c][i], "value": round(q * CLOSE[c][i], 2)} for c, q in sorted(held.items())]
        mv = round(sum(h["value"] for h in holdings), 2)
        cash = round(cash, 2)
        rows.append({"date": day, "cash": cash, "mv": mv, "equity": round(cash + mv, 2), "holdings": holdings})
    return fills, rows


def csv_text(header, rows):
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows([header, *rows])
    return out.getvalue()


def header(name):
    return next(csv.reader([(ROOT / "headers" / name).read_text(encoding="utf-8").strip()]))


jq = lambda c: c.replace(".SH", ".XSHG").replace(".SZ", ".XSHE")


def bigquant(fills, days):
    trades, last = [], None
    for f in sorted(fills, key=lambda f: f["date"], reverse=True):     # newest first, merged date cells
        trades.append([f["date"] if f["date"] != last else "", "09:30:00", f["code"], NAMES[f["code"]],
                       "买入" if f["side"] == "buy" else "卖出", f["quantity"], f["price"], f["notional"], "--", f["fee"]])
        last = f["date"]
    pos = []
    for s in reversed(days):
        for k, h in enumerate(s["holdings"]):
            pos.append([s["date"] if k == 0 else "", h["code"], NAMES[h["code"]], h["quantity"], h["price"], h["price"],
                        h["value"], f"{h['value'] / s['equity']:.2%}", 0])
        if not s["holdings"]:
            pos.append([s["date"], "--", "--", "--", "--", "--", "--", "--", "--"])
        pos.append(["", "", "", "", f"（总资产￥{s['equity']}", f"剩余金额 ￥{s['cash']}", f"总市值 ￥{s['mv']}", "总占比", "总收益）"])
    i, code, cash = DIVIDEND
    q = next(h["quantity"] for h in days[i - 1]["holdings"] if h["code"] == code)
    log = [[f"{DAYS[i]} 15:00:00", "INFO", f"split[{DAYS[i]}][{code},old={q},now={q},cash={cash}]"]]
    return {"交易.csv": ("utf-8-sig", csv_text(header("bigquant/交易.csv"), trades)),
            "持仓.csv": ("utf-8-sig", csv_text(header("bigquant/持仓.csv"), pos)),
            "日志.csv": ("utf-8-sig", csv_text(header("bigquant/日志.csv"), log))}


def joinquant(fills, days):
    trades = []
    for f in fills:
        q = f["quantity"] if f["side"] == "buy" else -f["quantity"]
        trades.append([f["date"], "14:40:00", "股票", f"{NAMES[f['code']]}({jq(f['code'])})", "买" if q > 0 else "卖", "市价单",
                       f"{q}股", f["price"], f["notional"] if q > 0 else -f["notional"], f"{q}股", "-", 0, f["fee"], "全部成交",
                       f"{f['date']} 14:40:00"])
    pos = []
    for s in days:
        for h in s["holdings"]:
            pos.append([s["date"], "股票", f"{NAMES[h['code']]}({jq(h['code'])})", "多", f"{h['quantity']}股", f"{h['quantity']}股",
                        h["price"], h["value"], 0, h["price"], "-", 0, 0, "0股", "0%", s["equity"], f"{h['value'] / s['equity']:.2%}"])
        pos.append([s["date"], "", "Cash", "", "", "", "", s["cash"], "", "", "", "", "", "", "", "0"])
    returns = [[f"{s['date']} 16:00:00", 0, round((s["equity"] / OPENING_CASH - 1) * 100, 4), 0, 0, 0, 0, 0] for s in days]
    log = "".join(f"{s['date']} 09:30:00 - INFO  - 合成日志 {s['date']}\n" for s in days)
    return {"transaction.csv": ("gbk", csv_text(header("joinquant/transaction.csv"), trades)),
            "position.csv": ("gbk", csv_text(header("joinquant/position.csv"), pos)),
            "result.csv": ("gbk", csv_text(header("joinquant/result.csv"), returns)),
            "log.txt": ("gbk", log)}


def ricequant_web(fills, days):
    trades = [[f"{f['date']} 15:00:00", jq(f["code"]), NAMES[f["code"]], "买" if f["side"] == "buy" else "卖",
               "开" if f["side"] == "buy" else "平", f["quantity"], f["price"], f["fee"]] for f in fills]
    pos = [[f"{s['date']} 15:30:00", jq(h["code"]), NAMES[h["code"]], h["price"], "--", h["quantity"], h["price"], h["value"], 0, "--", 0]
           for s in days for h in s["holdings"]]
    acct = [[f"{s['date']} 15:30:00", "股票", s["cash"], s["equity"], s["mv"], "--", 0] for s in days]
    return {"交易详情.csv": ("utf-8-sig", csv_text(header("ricequant_web/交易详情.csv"), trades)),
            "持仓详情.csv": ("utf-8-sig", csv_text(header("ricequant_web/持仓详情.csv"), pos)),
            "账户详情.csv": ("utf-8-sig", csv_text(header("ricequant_web/账户详情.csv"), acct))}


def ricequant_local(fills, days):
    trades, orders = [], []
    for n, f in enumerate(fills):
        stamp = f"{f['date']} 09:31:00"
        tax = round(f["notional"] * 5e-4, 2) if f["side"] == "sell" else 0.
        trades.append([stamp, stamp, jq(f["code"]), NAMES[f["code"]], "买" if f["side"] == "buy" else "卖",
                       "开" if f["side"] == "buy" else "平", 1000 + n, tax, round(f["fee"] - tax, 2), f["quantity"], f["price"], 1000 + n, f["fee"]])
        side = "SIDE.BUY" if f["side"] == "buy" else "SIDE.SELL"
        orders.append([stamp, "EVENT.ORDER_CREATION_PASS", 1000 + n, jq(f["code"]), side, f["quantity"], 0, "ORDER_STATUS.ACTIVE", ""])
        orders.append([stamp, "EVENT.TRADE", 1000 + n, jq(f["code"]), side, f["quantity"], f["quantity"], "ORDER_STATUS.FILLED", ""])
    orders.append([f"{DAYS[6]} 09:31:00", "EVENT.ORDER_UNSOLICITED_UPDATE", 2000, "000001.XSHE", "SIDE.BUY", 100, 0, "ORDER_STATUS.REJECTED",
                   "订单创建失败: 可用资金不足。当前资金: 999.50，000001.XSHE 下单所需资金: 1001.20"])
    pos = [[s["date"], jq(h["code"]), NAMES[h["code"]], h["quantity"], h["price"], h["price"], h["value"], 0, round(h["value"] / s["equity"], 6)]
           for s in days for h in s["holdings"]]
    # RQAlpha books the dividend as receivable on the ex-date and pays it the next day.
    i, _, amount = DIVIDEND
    acct = []
    for k, s in enumerate(days):
        receivable = amount if k == i else 0.
        available = s["cash"] - receivable
        day_fee = sum(f["fee"] for f in fills if f["date"] == s["date"])
        acct.append([s["date"], "股票/场内基金", round(available, 2), round(day_fee, 2), s["mv"], s["equity"], receivable])
    ranks = []
    for k, s in enumerate(days):
        order = sorted(CLOSE, key=lambda c: -CLOSE[c][k])
        ranks += [[f"{s['date']} 09:31:00", "|".join(jq(c) for c in CLOSE), r + 1, jq(c), "True" if r == 0 else "False"]
                  for r, c in enumerate(order)]
    return {"交易详情.csv": ("utf-8-sig", csv_text(header("ricequant_local/交易详情.csv"), trades)),
            "持仓详情.csv": ("utf-8-sig", csv_text(header("ricequant_local/持仓详情.csv"), pos)),
            "账户详情.csv": ("utf-8-sig", csv_text(header("ricequant_local/账户详情.csv"), acct)),
            "委托详情.csv": ("utf-8-sig", csv_text(header("ricequant_local/委托详情.csv"), orders)),
            "每日股票排名.csv": ("utf-8-sig", csv_text(header("ricequant_local/每日股票排名.csv"), ranks))}


def generic(fills, days):
    trades = [[f["date"], "09:30:00", f["code"], f["side"], f["quantity"], f["price"], f["fee"]] for f in fills]
    pos = [[s["date"], h["code"], h["quantity"], h["price"]] for s in days for h in s["holdings"]]
    acct = [[s["date"], s["equity"], s["cash"]] for s in days]
    return {"trades.csv": ("utf-8", csv_text(["date", "time", "code", "side", "quantity", "price", "fee"], trades)),
            "holdings.csv": ("utf-8", csv_text(["date", "code", "quantity", "price"], pos)),
            "account.csv": ("utf-8", csv_text(["date", "equity", "cash"], acct))}


# What each format must produce. Written by hand, not computed, so the test checks the code.
# The one dividend day is split across the two names held the day before, so it infers two rows.
EXPECTED = {
    "bigquant": {"adapter": "bigquant_v1", "fills": 5, "accounting": "verified"},
    "joinquant": {"adapter": "joinquant_v1", "fills": 5, "accounting": "verified", "inferred_dividends": 2},
    "ricequant_web": {"adapter": "ricequant_v1", "fills": 5, "accounting": "verified", "inferred_dividends": 2},
    "ricequant_local": {"adapter": "ricequant_v1", "fills": 5, "accounting": "verified", "receivable_dividend_days": 1,
                        "orders": 6, "rank_rows": 16},
    "generic": {"adapter": "generic_v1", "fills": 5, "accounting": "verified", "inferred_dividends": 2},
}


def main():
    fills, days = ledger()
    manifest = {"note": "合成账户，非行情数据；由 scripts/make_format_fixtures.py 生成", "formats": {}}
    for name, build in (("bigquant", bigquant), ("joinquant", joinquant), ("ricequant_web", ricequant_web),
                        ("ricequant_local", ricequant_local), ("generic", generic)):
        folder = SAMPLES / name
        folder.mkdir(parents=True, exist_ok=True)
        files = {**build(fills, days), "代码.txt": ("utf-8", CODE)}
        hashes = {}
        for filename, (encoding, text) in files.items():
            data = text.encode(encoding)
            (folder / filename).write_bytes(data)
            hashes[filename] = hashlib.sha256(data).hexdigest()
        manifest["formats"][name] = {"files": hashes, "expected": EXPECTED[name]}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote", ", ".join(manifest["formats"]))


if __name__ == "__main__":
    main()
