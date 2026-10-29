"""Strategy premise: identified from code or the user, checked against candidate returns."""
import random

from qwb import audit_premise


CODE = '''"""Docstring mentions momentum but computes nothing."""
LOOKBACK = 20

def rebalance(context, bar_dict):
    scores = {}
    for code in POOL:
        closes = history_bars(code, LOOKBACK + 1, "1d", "close")
        scores[code] = closes[-1] / closes[0] - 1
    ranked = sorted(scores, key=scores.get, reverse=True)
'''


def bundle(edge, days=60, names=12, seed=1):
    """Scores that predict the next period's return by `edge` (0 = noise, negative = reversed)."""
    rng = random.Random(seed)
    codes = [f"60000{i:02d}.SH" for i in range(names)]
    price = {c: 10. for c in codes}
    scores, prices = [], []
    for d in range(days):
        day = f"20{20 + d // 30}-{1 + d // 30 % 12:02d}-{1 + d % 28:02d}"
        score = {c: rng.gauss(0, 1) for c in codes}
        top = sorted(codes, key=score.get, reverse=True)[:3]
        scores += [{"date": day, "code": c, "score": score[c], "selected": c in top} for c in codes]
        prices += [{"date": day, "code": c, "close": price[c]} for c in codes]
        for c in codes:
            price[c] *= 1 + .002 + edge * score[c] + rng.gauss(0, .02)
    return {"scores": scores, "prices": prices, "code": {"source": CODE}}


def test_factor_and_direction_come_from_code_not_docstring():
    found = audit_premise.from_code({"code": {"source": CODE}})
    assert found["direction"] == "higher" and found["lookback"] == 20
    assert found["factors"][0]["key"] == "momentum"
    assert found["factors"][0]["line"] == 8          # the computation, not the docstring on line 1


def test_embedded_original_strategy_is_read():
    wrapper = "SOURCE = " + repr("def MOM(etf):\n    y = 1\n    df.sort_values(by='score', ascending=False)\n") + "\n"
    found = audit_premise.from_code({"code": {"source": wrapper}})
    assert found["embedded"] and found["factors"][0]["key"] == "momentum" and found["direction"] == "higher"


def test_true_premise_holds_with_size_and_periods():
    result = audit_premise.analyze(bundle(.01))
    assert result["status"] == "held"
    assert result["source"] == "从代码和策略选股行为识别"
    assert result["premise"].startswith("过去20日动量越高")
    assert result["spread_mean"] > 0 and result["spread_t"] > 2
    assert {s["label"] for s in result["by_year"]} == {"2020年", "2021年"}
    assert [s["label"] for s in result["by_market"]] == ["候选池整体上涨的周期", "候选池整体下跌的周期"]
    assert result["held_years"]


def test_reversed_and_noise_are_told_apart():
    assert audit_premise.analyze(bundle(-.01))["status"] == "reversed"
    assert audit_premise.analyze(bundle(0., seed=2))["status"] == "not_supported"


def test_user_premise_overrides_and_code_alone_cannot_be_checked():
    result = audit_premise.analyze(bundle(.01), {"text": "低估值会修复", "direction": "lower"})
    assert result["source"] == "用户填写" and result["premise"] == "低估值会修复"
    assert result["status"] == "reversed"            # the user's direction is tested as stated
    only_code = audit_premise.analyze({"scores": [], "prices": [], "code": {"source": CODE}})
    assert only_code["status"] == "needs_scores"


def test_partition_segments_are_reported():
    data = bundle(.01)
    days = sorted({s["date"] for s in data["scores"]})
    part = {"status": "planned", "development_start": days[0], "development_end": days[39],
            "validation_start": days[40], "validation_end": days[-1]}
    result = audit_premise.analyze(data, partition=part)
    assert result["segments"]["development"]["periods"] == 40
    assert result["segments"]["validation"]["label"] == "验证段"


def test_attribution_page_answers_the_three_questions():
    from qwb import strategy_audit_ui as ui
    html = ui.premise_section({"modules": {"premise": audit_premise.analyze(bundle(.01))}})
    for words in ("对不对", "对多少", "哪段时间对", "按年份", "按市场涨跌", "折合每年"):
        assert words in html
    noise = ui.premise_section({"modules": {"premise": audit_premise.analyze(bundle(0., seed=2))}})
    assert "不宜折成年化" in noise
