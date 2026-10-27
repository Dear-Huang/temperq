from qwb import corporate_actions as ca, strategy_audit as audit


def bundle(days, positions, fills=(), snapshots=None):
    snaps = snapshots or [{"date": d, "cash": c, "equity": e, "market_value": e - c, "ref": None} for d, c, e in days]
    return {"schema_version": audit.SCHEMA if hasattr(audit, "SCHEMA") else "strategy_import_v1",
            "opening": {"kind": "first_close", "cash": None}, "snapshots": snaps,
            "positions": [{**p, "ref": None} for p in positions], "fills": list(fills), "actions": []}


def test_cash_dividend_is_recovered_from_unexplained_cash():
    b = bundle([("2024-01-02", 1000., 2000.), ("2024-01-03", 1050., 2050.)],
               [{"date": "2024-01-02", "code": "600000.SH", "quantity": 100, "price": 10., "market_value": 1000.},
                {"date": "2024-01-03", "code": "600000.SH", "quantity": 100, "price": 10., "market_value": 1000.}])
    acts = ca.infer(b)
    assert len(acts) == 1 and acts[0]["kind"] == "cash_dividend" and abs(acts[0]["cash"] - 50.) < 1e-9
    assert acts[0]["inferred"]


def test_bonus_shares_need_a_clean_ratio_and_a_falling_price():
    pos = [{"date": "2024-01-02", "code": "002199.SZ", "quantity": 3000, "price": 17.93, "market_value": 53790.},
           {"date": "2024-01-03", "code": "002199.SZ", "quantity": 3900, "price": 13.35, "market_value": 52065.}]
    b = bundle([("2024-01-02", 0., 53790.), ("2024-01-03", 0., 52065.)], pos)
    acts = ca.infer(b)
    assert [(a["kind"], a["quantity_change"]) for a in acts] == [("bonus_shares", 900)]


def test_unexplained_share_jump_is_not_guessed():
    # 3000 → 3170 is not a whole 0.5-per-10 ratio and the price did not fall in proportion.
    pos = [{"date": "2024-01-02", "code": "000001.SZ", "quantity": 3000, "price": 10., "market_value": 30000.},
           {"date": "2024-01-03", "code": "000001.SZ", "quantity": 3170, "price": 10.1, "market_value": 32017.}]
    b = bundle([("2024-01-02", 0., 30000.), ("2024-01-03", 0., 32017.)], pos)
    assert ca.infer(b) == []


def test_sold_out_on_ex_date_counts_the_extra_shares():
    pos = [{"date": "2024-01-02", "code": "002224.SZ", "quantity": 4100, "price": 16.17, "market_value": 66297.}]
    fills = [{"date": "2024-01-03", "code": "002224.SZ", "side": "sell", "quantity": 4920, "price": 13.38,
              "notional": 4920 * 13.38, "fee": 0.}]
    b = bundle([("2024-01-02", 0., 66297.), ("2024-01-03", 65829.6, 65829.6)], pos, fills)
    acts = ca.infer(b)
    assert [(a["kind"], a["quantity_change"]) for a in acts] == [("bonus_shares", 820)]


def test_apply_labels_and_keeps_original():
    b = bundle([("2024-01-02", 1000., 2000.), ("2024-01-03", 1050., 2050.)],
               [{"date": d, "code": "600000.SH", "quantity": 100, "price": 10., "market_value": 1000.} for d in ("2024-01-02", "2024-01-03")])
    out = ca.apply(b, ca.infer(b))
    assert b["actions"] == [] and out["quality"]["inferred_actions"]["cash_dividend"] == 1
