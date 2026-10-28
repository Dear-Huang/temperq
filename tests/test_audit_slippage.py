"""Slippage already in backtest prices must not be counted again as spread."""
import pytest

from qwb import audit_slippage as slip


def declared(code):
    return slip.declared({"code": {"source": code}})


def test_joinquant_models_convert_to_cost_per_side():
    fixed = declared("def initialize(context):\n    set_slippage(FixedSlippage(0.02))\n")
    assert slip.per_side_bp(fixed, "600000.SH", 10.) == pytest.approx(10.)         # 0.01 yuan per side on 10 yuan
    ratio = declared("set_slippage(PriceRelatedSlippage(0.002))\n")
    assert slip.per_side_bp(ratio, "600000.SH", 10.) == pytest.approx(10.)         # 0.1% per side
    steps = declared("set_slippage(StepRelatedSlippage(2))\n")
    assert slip.per_side_bp(steps, "600000.SH", 10.) == pytest.approx(10.)         # one tick per side
    assert slip.per_side_bp(steps, "510300.SH", 1.) == pytest.approx(10.)          # fund tick is 0.001


def test_scope_and_last_call_win():
    code = ("set_slippage(FixedSlippage(0.02), type='stock')\n"
            "set_slippage(FixedSlippage(0.002), type='fund')\n"
            "set_slippage(FixedSlippage(0.04), type='stock')\n")
    s = declared(code)
    assert slip.per_side_bp(s, "600000.SH", 10.) == pytest.approx(20.)
    assert slip.per_side_bp(s, "510300.SH", 1.) == pytest.approx(10.)


def test_rqalpha_config_dict_is_read():
    code = "__config__ = {'mod': {'sys_simulation': {'slippage': 0.00123, 'slippage_model': 'PriceRatioSlippage'}}}\n"
    s = declared(code)
    assert slip.per_side_bp(s, "513520.SH", 1.9) == pytest.approx(12.3)
    spread = declared("__config__ = {'mod': {'sys_simulation': {'slippage': 0.002, 'slippage_model': 'x.FixedSpreadSlippage'}}}\n")
    assert slip.per_side_bp(spread, "510300.SH", 4.) == pytest.approx(2.5)


def test_none_zero_and_unknown_are_stated_plainly():
    assert slip.declared({"code": {"source": "x = 1\n"}}) is None
    assert slip.per_side_bp(None, "600000.SH", 10.) == 0.
    assert "设为 0" in slip.describe(declared("set_slippage(FixedSlippage(0))\n"))
    unknown = declared("set_slippage(MySlippage(0.1))\n")
    assert slip.per_side_bp(unknown, "600000.SH", 10.) is None and "无法换算" in slip.describe(unknown)
