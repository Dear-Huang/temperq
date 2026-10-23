from qwb.paper_ui import equity_chart, statistics


def test_unstarted_account_has_no_performance():
    account = {"config": {"cash": 100000, "holdings": []}, "sessions": []}
    stats = statistics(account)
    assert stats["equity"] == 100000
    assert stats["change"] is None and stats["drawdown"] is None
    assert stats["forward_days"] == 0
    assert "polyline" not in equity_chart([])
    account["config"]["holdings"] = [{"code": "000001.SZ", "quantity": 100}]
    assert statistics(account)["equity"] is None


def test_account_performance_separates_replay_and_forward():
    records = [{"day": day, "equity": equity, "state": {"cash": 5000},
                "audit": {"total_cost": 5}, "scope": scope}
               for day, equity, scope in [("2026-10-20", 100000, "historical_replay"),
                                         ("2026-10-21", 110000, "late_reconstruction"),
                                         ("2026-10-22", 99000, "forward_paper")]]
    stats = statistics({"config": {}, "sessions": records})
    assert round(stats["change"], 6) == -.01
    assert round(stats["drawdown"], 6) == -.1
    assert stats["fees"] == 15 and stats["forward_days"] == 1
    assert "polyline" not in equity_chart(records[:1])
    chart = equity_chart(records)
    assert "polyline" in chart and "2026-10-20" in chart and "2026-10-22" in chart
