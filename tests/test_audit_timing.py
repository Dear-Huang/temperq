"""Fill-time sensitivity: whether the result depends on the minute the orders filled."""
from qwb import audit_timing


def bundle(gain_per_share=0., net=1000.):
    fills = [{"date": "2024-01-02", "time": "09:31:00", "code": "510300.SH", "side": "buy", "quantity": 1000, "price": 4.0},
             {"date": "2024-01-05", "time": "09:31:00", "code": "510300.SH", "side": "sell", "quantity": 1000, "price": 4.2}]
    minutes = []
    for f in fills:
        for t in audit_timing.TIMES:
            # buys get cheaper and sells dearer by gain_per_share at every later minute
            shift = 0 if t == "09:31" else (-gain_per_share if f["side"] == "buy" else gain_per_share)
            minutes.append({"date": f["date"], "time": t, "code": f["code"], "close": f["price"] + shift,
                            "open": f["price"] + .01 if t == "09:31" else None, "volume": 10000. if t == "09:31" else None})
    accounting = {"status": "verified", "daily": [{"net_pnl": net}]}
    return {"fills": fills, "minute_prices": minutes}, accounting


def test_small_swing_is_stable_and_large_swing_is_sensitive():
    data, acc = bundle(gain_per_share=.01, net=1000.)        # 2 fills x 1000 shares x 0.01 = 20 yuan per minute
    out = audit_timing.analyze(data, acc)
    assert out["status"] == "stable" and abs(out["high"] - 20.) < 1e-6
    data, acc = bundle(gain_per_share=.2, net=1000.)         # 400 yuan: 40% of net
    out = audit_timing.analyze(data, acc)
    assert out["status"] == "sensitive" and "依赖成交时点" in out["verdict"]
    assert out["open_share"] == 1.0 and out["max_volume_share"] == .1


def test_every_later_minute_worse_means_the_open_flatters():
    data, acc = bundle(gain_per_share=-.2, net=1000.)
    out = audit_timing.analyze(data, acc)
    assert out["status"] == "sensitive" and out.get("open_flatters")


def test_backtest_slippage_is_not_counted_as_timing():
    # The fill price includes slippage; the baseline is the fill minute's close, so no minute looks better by it.
    data, acc = bundle(gain_per_share=0., net=1000.)
    for f in data["fills"]:
        f["price"] += .05 if f["side"] == "buy" else -.05
    out = audit_timing.analyze(data, acc)
    assert all(abs(r["gain"]) < 1e-6 for r in out["rows"] if r["field"] == "close")


def test_missing_inputs_say_what_is_missing():
    data, acc = bundle()
    assert audit_timing.analyze({"fills": data["fills"]}, acc)["status"] == "needs_minutes"
    untimed = [{**f, "time": ""} for f in data["fills"]]
    assert audit_timing.analyze({"fills": untimed, "minute_prices": data["minute_prices"]}, acc)["status"] == "no_fill_times"
    assert audit_timing.analyze({"fills": []}, acc) is None


def test_minute_file_imports_and_report_shows_the_table(tmp_path, capsys):
    import json, shutil
    from pathlib import Path
    from qwb.cli import main
    src = Path(__file__).parent / "fixtures" / "formats" / "samples" / "generic"
    target = tmp_path / "m"
    shutil.copytree(src, target)
    trades = (target / "trades.csv").read_text(encoding="utf-8").splitlines()
    rows = ["date,time,code,close,open,volume"]
    for line in trades[1:]:
        d, t, code, side, q, price = line.split(",")[:6]
        for minute in sorted({t[:5], *audit_timing.TIMES}):          # the fill's own minute is required too
            rows.append(f"{d},{minute},{code},{float(price) + (0 if minute == t[:5] else .01)},,")
    (target / "minutes.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    assert main(["audit", "run", str(target), "--out", str(out_dir), "--json"]) == 0
    capsys.readouterr()
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["fill_timing"]["status"] in ("stable", "sensitive")
    assert "成交时点敏感性" in (out_dir / "report.html").read_text(encoding="utf-8")
    done = {r["check"] for r in summary["scope"]["done"]}
    assert "成交时点敏感性" in done
