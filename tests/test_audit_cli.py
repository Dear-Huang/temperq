"""`qwb audit`: the skill's entry point gives the same audit as the web page, from files on disk."""
import json
import shutil
from pathlib import Path

import pytest

from qwb.cli import main

SAMPLES = Path(__file__).parent / "fixtures" / "formats" / "samples"


def run(argv, capsys):
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("name", sorted(p.name for p in SAMPLES.iterdir() if p.is_dir()))
def test_check_reads_every_platform_sample_and_closes_the_books(name, capsys):
    code, out = run(["audit", "check", str(SAMPLES / name), "--json"], capsys)
    assert out["status"] == "imported", out
    assert out["books"] == "verified" and code == 0
    assert out["counts"]["fills"] > 0 and out["files"]


def test_run_writes_report_summary_evidence_and_scope(tmp_path, capsys):
    out_dir = tmp_path / "out"
    code, out = run(["audit", "run", str(SAMPLES / "generic"), "--out", str(out_dir), "--name", "样例", "--json"], capsys)
    assert code == 0 and out["status"] == "done" and out["books"] == "verified"
    for name in ("report.html", "summary.json", "evidence.json", "stocks.csv", "daily.csv"):
        assert (out_dir / name).exists(), name
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema"] == "qwb_audit_summary_v1" and summary["strategy"] == "样例"
    assert summary["conclusions"] and summary["stages"]
    scope = summary["scope"]
    assert scope["done"] and scope["absent"]          # what ran, and what the engine cannot do yet
    report = (out_dir / "report.html").read_text(encoding="utf-8")
    assert "七、审计范围与限制" in report and "已审" in report and "本工具尚不具备" in report
    assert summary["result_digest"] in report


def test_run_never_overwrites_an_earlier_audit(tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert run(["audit", "run", str(SAMPLES / "generic"), "--out", str(out_dir), "--json"], capsys)[0] == 0
    code, out = run(["audit", "run", str(SAMPLES / "generic"), "--out", str(out_dir), "--json"], capsys)
    assert code == 2 and "不覆盖" in out["error"]


def test_check_fails_when_converted_books_do_not_close(tmp_path, capsys):
    # A conversion that drops a fill is what the skill must catch before auditing.
    target = tmp_path / "broken"
    shutil.copytree(SAMPLES / "generic", target)
    trades = (target / "trades.csv").read_text(encoding="utf-8").splitlines()
    (target / "trades.csv").write_text("\n".join(trades[:-1]) + "\n", encoding="utf-8")
    code, out = run(["audit", "check", str(target), "--json"], capsys)
    assert code == 1 and out["books"] != "verified" and out["book_failures"]


def test_conversion_scripts_in_a_subfolder_are_not_read_as_code(tmp_path, capsys):
    target = tmp_path / "with_script"
    shutil.copytree(SAMPLES / "generic", target)
    (target / "_convert").mkdir()
    (target / "_convert" / "convert.py").write_text("print('conversion')\n", encoding="utf-8")
    code, out = run(["audit", "check", str(target), "--json"], capsys)
    assert code == 0 and [f["read_as"] for f in out["files"]].count("code") == 1


def test_two_files_of_one_kind_are_named_in_the_error(tmp_path, capsys):
    target = tmp_path / "two_codes"
    shutil.copytree(SAMPLES / "generic", target)
    (target / "convert.py").write_text("print('conversion')\n", encoding="utf-8")
    code, out = run(["audit", "check", str(target), "--json"], capsys)
    assert code == 2 and "convert.py" in out["error"] and "同一种材料" in out["error"]


def test_generic_account_with_receivable_dividend_closes_on_the_ex_date(tmp_path, capsys):
    target = tmp_path / "dividend"
    target.mkdir()
    (target / "trades.csv").write_text(
        "date,time,code,side,quantity,price,fee\n2024-01-02,09:31:00,510300.SH,buy,1000,4.00,5.00\n", encoding="utf-8")
    (target / "holdings.csv").write_text(
        "date,code,quantity,price\n2024-01-02,510300.SH,1000,4.00\n2024-01-03,510300.SH,1000,3.90\n", encoding="utf-8")
    (target / "account.csv").write_text(
        "date,equity,cash,receivable_dividend\n2024-01-02,9995.00,5995.00,0\n2024-01-03,9995.00,5995.00,100.00\n",
        encoding="utf-8")
    (target / "code.txt").write_text("def initialize(context):\n    pass\n", encoding="utf-8")
    code, out = run(["audit", "check", str(target), "--json"], capsys)
    assert out["books"] == "verified", out.get("first_exceptions")


def test_compare_two_audited_versions(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        assert run(["audit", "run", str(SAMPLES / "generic"), "--out", str(d), "--json"], capsys)[0] == 0
    code, out = run(["audit", "compare", str(a), str(b), "--json"], capsys)
    assert code == 0 and out["comparable"] and out["same_trades"] and out["evidence"] == "E4"
    c = tmp_path / "c"
    assert run(["audit", "run", str(SAMPLES / "generic"), "--out", str(c), "--commission-bp", "1", "--json"], capsys)[0] == 0
    code, out = run(["audit", "compare", str(a), str(c), "--json"], capsys)
    assert code == 2 and "费用情景不同" in out["error"]


def test_interaction_of_two_changes(tmp_path, capsys):
    outs = []
    for i in range(4):
        d = tmp_path / f"r{i}"
        assert run(["audit", "run", str(SAMPLES / "generic"), "--out", str(d), "--json"], capsys)[0] == 0
        outs.append(str(d))
    code, out = run(["audit", "interaction", *outs, "--labels", "保留带,留现金", "--json"], capsys)
    assert code == 0 and out["comparable"] and out["evidence"] == "E4"
    assert out["realistic"]["interaction"] == 0 and "大致可以相加" in out["verdict"]


def test_interaction_flags_a_margin_that_changes_sign():
    from qwb import audit_factorial
    def run_of(daily):
        return {"mode": "full", "settings": {"commission_bp": 3, "minimum_commission": 5}, "modules": {
            "accounting": {"baseline_equity": 100., "daily": [{"date": "2024-01-02", "net_pnl": daily}]}}}
    out = audit_factorial.analyze(run_of(0.), run_of(-10.), run_of(1.), run_of(-14.), labels=("A", "B"))
    gains = {(m["change"], m["given"]): m["gain"] for m in out["reported"]["margins"]}
    assert gains[("B", "不加A")] == 1. and gains[("B", "已加A")] == -4.
    assert out["reported"]["flips"] == ["B"] and "不能直接叠加" in out["verdict"]
