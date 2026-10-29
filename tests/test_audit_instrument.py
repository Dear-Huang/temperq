"""Recorder, supplement and instrument-check: getting premise and timing materials without changing the strategy."""
import importlib.util
from pathlib import Path

from qwb import audit_instrument

STATIC = Path(__file__).resolve().parents[1] / "src" / "qwb" / "static"
RECORDER = (STATIC / "temperq_recorder.py").read_text(encoding="utf-8")
STRATEGY = '''def initialize(context):
    g.n = 1

def trade(context):
    scores = {"510300.XSHG": 1.0, "518880.XSHG": 0.5}
    target = sorted(scores, key=scores.get, reverse=True)[:g.n]
    order_target_value(target[0], 1000)
'''


def instrument(source):
    anchor = "    target = sorted(scores, key=scores.get, reverse=True)[:g.n]\n"
    return RECORDER + "\n" + source.replace(anchor, anchor + "    temperq_record(context, scores, target)\n")


def test_recording_only_copy_is_accepted():
    out = audit_instrument.compare(STRATEGY, instrument(STRATEGY))
    assert out["same_logic"] and out["added_calls"] == 1 and not out["differences"]


def test_any_other_change_is_rejected_and_named():
    changed = instrument(STRATEGY).replace("g.n = 1", "g.n = 2")
    out = audit_instrument.compare(STRATEGY, changed)
    assert not out["same_logic"] and any("g.n = 2" in d["code"] for d in out["differences"])
    sneaky = instrument(STRATEGY).replace("    temperq_record(context, scores, target)\n",
                                          "    temperq_record(context, scores, target)\n    target = target[::-1]\n")
    assert not audit_instrument.compare(STRATEGY, sneaky)["same_logic"]


def test_recorder_writes_scores_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scope = {}
    exec(RECORDER, scope)
    from datetime import datetime
    from types import SimpleNamespace
    ctx = SimpleNamespace(current_dt=datetime(2024, 1, 2, 9, 31))
    scope["temperq_record"](ctx, {"510300.XSHG": 1.0, "518880.XSHG": float("nan")}, ["510300.XSHG"])
    scope["temperq_record"](ctx, {"510300.XSHG": 0.9}, [])
    scope["temperq_record"](object(), {"x": 1}, [])          # bad context: logged, not raised
    lines = (tmp_path / "temperq_scores.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "date,code,score,selected,available_at"
    assert lines[1].startswith("2024-01-02,510300.XSHG,1,true") and len(lines) == 3   # NaN skipped


def test_supplement_asks_only_for_what_the_audit_needs():
    spec = importlib.util.spec_from_file_location("supplement", STATIC / "temperq_supplement.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                         # no get_price here, so main() does not run
    prices, minutes = module.needed(
        [{"date": "2024-01-02", "code": "510300.SH"}, {"date": "2024-01-02", "code": "510300.XSHG"}],
        [{"date": "2024-01-03", "code": "518880.SH"}])
    assert prices == [("2024-01-02", "510300.XSHG")] and minutes == [("2024-01-03", "518880.XSHG")]


def test_recorded_scores_import_with_selected():
    from qwb import strategy_imports as imp
    files = [imp.SourceFile("temperq_scores.csv",
                            "date,code,score,selected,available_at\n2024-01-02,510300.XSHG,1.0,true,2024-01-02 09:31:00\n"
                            "2024-01-02,518880.XSHG,0.5,false,2024-01-02 09:31:00\n".encode()),
             imp.SourceFile("code.py", STRATEGY.encode())]
    bundle = imp.normalize(files)
    assert [s["selected"] for s in bundle["scores"]] == [True, False]


def test_budget_lot_is_the_largest_affordable_round_lot():
    scope = {}
    exec((STATIC / "temperq_budget_lot.py").read_text(encoding="utf-8"), scope)
    f = scope["temperq_max_lot"]
    cost = lambda q, p=4.531, s=.001: 0 if q == 0 else q * (p + s) + max(.0003 * q * (p + s), 5)
    q = f(100000, 4.531, slippage=.001)
    assert q == 22000 and cost(q) <= 100000 < cost(q + 100)       # matches the v14b certificate for this row
    assert f(3, 4.0) == 0 and f(410, 4.0, minimum=5) == 100 and f(404, 4.0, minimum=5) == 0
