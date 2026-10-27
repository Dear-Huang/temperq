"""The JoinQuant export script must produce files the generic importer accepts and audits."""
import importlib.util
from pathlib import Path

from qwb import strategy_audit as audit, strategy_imports as imports

SCRIPT = Path(__file__).resolve().parents[1] / "src/qwb/static/joinquant_export.py"


def load_script():
    spec = importlib.util.spec_from_file_location("joinquant_export", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)   # get_backtest is absent, so main() must not run
    return module


ORDERS = [
    {"order_id": 1, "time": "2024-01-02 09:30:00", "security": "600000.XSHG", "action": "open", "side": "long",
     "amount": 1000, "filled": 1000, "price": 10.0, "commission": 5.0, "status": "held"},
    {"order_id": 2, "time": "2024-01-02 09:30:00", "security": "000001.XSHE", "action": "open", "side": "long",
     "amount": 500, "filled": 500, "price": 20.0, "commission": 5.0, "status": "held"},
    {"order_id": 3, "time": "2024-01-03 09:30:00", "security": "600000.XSHG", "action": "close", "side": "long",
     "amount": 1000, "filled": 1000, "price": 11.0, "commission": 10.5, "status": "held"},
    {"order_id": 4, "time": "2024-01-03 09:30:00", "security": "000002.XSHE", "action": "open", "side": "long",
     "amount": 100, "filled": 0, "price": 30.0, "commission": 0, "status": "canceled"},
]
POSITIONS = [
    {"time": "2024-01-02", "security": "600000.XSHG", "amount": 1000, "price": 10.0},
    {"time": "2024-01-02", "security": "000001.XSHE", "amount": 500, "price": 20.0},
    {"time": "2024-01-03", "security": "000001.XSHE", "amount": 500, "price": 21.0},
]
BALANCES = [
    {"time": "2024-01-02", "total_value": 99990.0, "cash": 79990.0},
    {"time": "2024-01-03", "total_value": 101479.5, "cash": 90979.5},
]


def test_script_does_not_run_outside_joinquant():
    module = load_script()
    assert module.BACKTEST_ID.startswith("请替换")


def test_convert_keeps_fills_skips_cancelled_and_normalises_dates():
    trades, holdings, account, notes = load_script().convert(ORDERS, POSITIONS, BALANCES)
    assert [t["side"] for t in trades] == ["buy", "buy", "sell"]
    assert trades[0]["date"] == "2024-01-02" and trades[0]["time"] == "09:30:00"
    assert len(holdings) == 3 and len(account) == 2
    assert any("跳过 1 条" in n for n in notes)


def test_exported_files_import_as_generic_and_audit(tmp_path):
    module = load_script()
    trades, holdings, account, _ = module.convert(ORDERS, POSITIONS, BALANCES)
    module.write(tmp_path / "temperq_trades.csv", trades, ["date", "time", "code", "side", "quantity", "price", "fee", "order_id"])
    module.write(tmp_path / "temperq_holdings.csv", holdings, ["date", "code", "quantity", "price"])
    module.write(tmp_path / "temperq_account.csv", account, ["date", "equity", "cash"])
    files = [imports.SourceFile(p.name, p.read_bytes()) for p in sorted(tmp_path.glob("*.csv"))]
    files.append(imports.SourceFile("strategy.py", b"def initialize(context):\n    g.stock_count = 2\n"))
    bundle = imports.normalize(files, adapter="auto", name="聚宽导出测试")
    assert bundle["adapter"] == "generic_v1"
    assert {f["code"] for f in bundle["fills"]} == {"600000.SH", "000001.SZ"}
    assert len(bundle["snapshots"]) == 2 and not bundle["quality"]["fills_inferred"]
    result = audit.analyze(bundle)
    assert result["modules"]["fee_analysis"]["priced_fill_count"] == 3


def test_missing_balances_are_reported_not_faked():
    _, _, account, notes = load_script().convert(ORDERS, POSITIONS, [])
    assert account == [] and any("近似" in n for n in notes)


def test_short_or_unknown_direction_is_skipped():
    orders = [{**ORDERS[0], "side": "short"}, {**ORDERS[1], "action": "weird"}]
    trades, _, _, notes = load_script().convert(orders, [], [])
    assert trades == [] and any("跳过 2 条" in n for n in notes)
