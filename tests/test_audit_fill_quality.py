"""Were the fill prices obtainable? Position in the day's range and the worst price soon after."""
import random

from qwb import audit_fill_quality


def bundle(lucky, n=60, seed=1, after=0.):
    """Fills on n days. lucky=True buys at the day's low and sells at its high; False places them at random."""
    rng = random.Random(seed)
    fills, ranges, minutes = [], [], []
    for i in range(n):
        day = f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}"
        low, high = 10., 11.
        side = "buy" if i % 2 == 0 else "sell"
        pos = (0. if side == "buy" else 1.) if lucky else rng.random()
        price = low + pos * (high - low)
        fills.append({"date": day, "time": "09:31:00", "code": "A.SH", "side": side, "quantity": 100, "price": price})
        ranges.append({"date": day, "code": "A.SH", "high": high, "low": low})
        for k in range(11):
            m = f"09:{31 + k:02d}"
            minutes.append({"date": day, "time": m, "code": "A.SH", "close": price,
                            "high": price + after, "low": price - after})
    return {"fills": fills, "day_ranges": ranges, "minute_prices": minutes}


def test_buying_lows_and_selling_highs_is_flagged():
    out = audit_fill_quality.analyze(bundle(lucky=True), {"status": "verified", "daily": [{"net_pnl": 100.}]})
    assert out["range"]["status"] == "suspicious" and out["status"] == "suspicious"
    assert out["range"]["best_share"] == 1. and "疑似" in out["range"]["verdict"]


def test_random_fills_are_normal():
    out = audit_fill_quality.analyze(bundle(lucky=False), {"status": "verified", "daily": [{"net_pnl": 100.}]})
    assert out["range"]["status"] == "normal" and "接近随机" in out["range"]["verdict"]


def test_worst_price_floor_can_erase_the_profit():
    data = bundle(lucky=False, after=.05)                    # 60 fills x 100 shares x 0.05 = 300 yuan worse
    out = audit_fill_quality.analyze(data, {"status": "verified", "daily": [{"net_pnl": 200.}]})
    assert abs(out["floor"]["change"] + 300.) < 1e-6 and out["floor"]["net_after"] < 0
    assert out["status"] == "suspicious" and "压力情景" in out["floor"]["verdict"]
    kept = audit_fill_quality.analyze(data, {"status": "verified", "daily": [{"net_pnl": 2000.}]})
    assert kept["floor"]["net_after"] > 0 and "仍为" in kept["floor"]["verdict"]


def test_lunch_break_is_skipped_and_missing_inputs_are_named():
    assert audit_fill_quality._minutes_after("11:28", 4) == ["11:28", "11:29", "11:30", "13:01", "13:02"]
    assert audit_fill_quality._minutes_after("14:58", 10, own=False) == ["14:59", "15:00"]
    out = audit_fill_quality.analyze({"fills": bundle(False)["fills"]}, {"status": "verified", "daily": []})
    assert out["status"] == "needs_prices" and out["missing"]


def test_supplement_writes_the_fill_window():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "qwb" / "static" / "temperq_supplement.py"
    spec = importlib.util.spec_from_file_location("supplement", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clocks = module.fill_clocks([{"date": "2024-01-02", "time": "09:31:00", "code": "510300.SH"}])
    assert clocks[("2024-01-02", "510300.XSHG")] >= {"09:31", "09:41"}
