"""Governance time boundaries and exact-account economic invariants."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qwb import governance as g, experiments, paper, strategy, ui, worker
from qwb.jobs import Cancelled


@pytest.fixture
def market():
    rng = np.random.default_rng(44)
    dates = pd.bdate_range("2023-01-02", periods=400)
    codes = ["000001.SZ", "600000.SH", "688001.SH"]
    noise = rng.normal(0, .015, (400, 3))
    close = pd.DataFrame(12*np.exp(np.cumsum(noise, axis=0)), index=dates, columns=codes)
    panel = {"close": close, "open": close.shift(1).fillna(12)*(1+noise*.15),
             "adj_factor": close*0+1, "up_limit": close.shift(1).fillna(12)*1.2,
             "down_limit": close.shift(1).fillna(12)*.8}
    signals = pd.DataFrame(np.tile([3.,2.,1.], (400,1)), index=dates, columns=codes)
    signals.iloc[::10] = [2., 3., 1.]
    return panel, signals


def test_band_retains_known_holdings_and_ties_have_stable_code_order():
    scores = pd.Series({"600000.SH": 1., "000001.SZ": 1., "688001.SH": .1})
    cfg = g.GovernanceConfig(n_hold=1, hold_until_rank=2)
    chosen, reasons = g.choose(scores, {"600000.SH"}, cfg)
    assert chosen == ["600000.SH"] and reasons[0]["reason"] == "HOLD_IN_BAND"
    assert g.choose(scores, set(), cfg)[0] == ["000001.SZ"]
    chosen, reasons = g.choose(scores, {"688001.SH"}, cfg)
    assert chosen == ["000001.SZ"]
    assert reasons[-1]["reason"] == "SELL_OUT_OF_BAND"


@pytest.mark.parametrize("kwargs", [{"n_hold": 0}, {"hold_until_rank": 1}, {"cash_floor": -1},
    {"max_weight": float("nan")}, {"target_volatility": 0}, {"n_hold": True}])
def test_invalid_mandates_rejected(kwargs):
    with pytest.raises(ValueError):
        g.GovernanceConfig(**kwargs)


def test_future_labels_and_future_prices_cannot_change_earlier_risk_fit(market):
    panel, signals = market
    day = signals.index[300]
    samples = g.risk_samples(g.RiskData(panel), signals, 2)
    f, ewma = g.RiskData(panel).features(day, list(signals.columns[:2]))
    head = g.RiskHead(samples)
    predicted, reason = head.predict(day, 300, f, ewma)
    assert reason == "ML_RISK"
    assert pd.Timestamp(head.model["max_label_end"]) < day
    changed = samples.copy()
    changed.loc[changed.label_end >= day, "variance"] = 1e12
    other = g.RiskHead(changed)
    assert other.predict(day, 300, f, ewma)[0] == predicted
    altered = {key: value.copy() for key, value in panel.items()}
    for field in ("open", "close"):
        altered[field].loc[altered[field].index > day] *= 50
    data2 = g.RiskData(altered)
    assert np.array_equal(f, data2.features(day, list(signals.columns[:2]))[0])
    new = g.RiskHead(g.risk_samples(data2, signals, 2))
    assert new.predict(day, 300, f, ewma)[0] == predicted


def test_label_uses_next_open_to_sixth_open_and_maturity(market):
    panel, signals = market
    risk, day = g.RiskData(panel), signals.index[100]
    label, end = risk.label(day, ["000001.SZ"])
    opens = panel["open"]["000001.SZ"].iloc[101:107].to_numpy()
    assert label == pytest.approx(252*np.mean((opens[1:]/opens[:-1]-1)**2))
    assert end == signals.index[106]


def test_four_arms_reuse_cash_lot_fees_and_default_equivalence(market):
    panel, signals = market
    cfg = g.GovernanceConfig(n_hold=2, hold_until_rank=2, max_weight=1., cash_floor=.1)
    arms, _ = g.compare(panel, signals, cfg, start=signals.index[300], end=signals.index[325])
    assert arms["rules_baseline"] == arms["fixed"]
    for name, arm in arms.items():
        assert all(row["cash"] >= 0 for row in arm["path"])
        assert arm["metrics"]["total_cost"] > 0
        assert sum(f["total_cost"] for f in arm["fills"]) == pytest.approx(arm["metrics"]["total_cost"])
        for pos in arm["positions"]:
            state = pos["state"]
            eq = state["cash"] + sum(p["quantity"]*p["mark"] for p in state["positions"])
            actual = next(row["equity"] for row in arm["path"] if row["date"] == pos["date"])
            assert eq == pytest.approx(actual)
        assert all(sum(d["weights"].values()) <= .9+1e-12 for d in arm["decisions"])
    assert arms["ml"]["risk_models"] and arms["ml"]["metrics"]["ml_warmup_decisions"] == 0
    for a,b in zip(arms["ewma"]["decisions"], arms["ml"]["decisions"]):
        assert a["evaluation_basket"] == b["evaluation_basket"]
        assert a["evaluation_target_variance"] == b["evaluation_target_variance"]


def test_cap_leaves_cash_and_blocked_orders_remain_visible(market):
    panel, signals = market
    cfg = g.GovernanceConfig(n_hold=2, hold_until_rank=3, max_weight=.1)
    start, end = signals.index[300], signals.index[307]
    panel["up_limit"].loc[signals.index[301], :] = panel["open"].loc[signals.index[301], :]
    arms, _ = g.compare(panel, signals, cfg, start=start, end=end)
    assert all(d["cash_target"] == pytest.approx(.8) for d in arms["fixed"]["decisions"])
    assert any(f["reason"] == "LIMIT_UP" and f["quantity"] == 0 for f in arms["fixed"]["fills"])


def test_risk_scores_use_reference_basket_even_when_band_retains_other_names(market):
    panel, signals = market
    signals.loc[signals.index[305]] = [2., 1., 3.]
    cfg = g.GovernanceConfig(n_hold=2, hold_until_rank=3, max_weight=.4)
    samples = g.risk_samples(g.RiskData(panel), signals, cfg.n_hold)
    arm = g.replay(panel, signals, cfg, mode="ewma", start=signals.index[300],
                   end=signals.index[312], initial_cash=100000, snapshot_id="fixture", samples=samples)
    decision = arm["decisions"][1]
    assert "600000.SH" in decision["weights"] and "600000.SH" not in decision["evaluation_basket"]
    expected, _ = g.RiskData(panel).label(signals.index[305], decision["evaluation_basket"])
    assert decision["evaluation_target_variance"] == expected


def test_unmodelled_actions_fail_and_do_not_change_paper_identity(market):
    panel, signals = market
    before = paper.implementation_hash()
    panel["adj_factor"].iloc[302:, :] = 1.01
    with pytest.raises(ValueError, match="公司行动"):
        g.compare(panel, signals, g.GovernanceConfig(n_hold=2), start=signals.index[300], end=signals.index[310])
    assert paper.implementation_hash() == before


def test_scores_require_unique_explicit_decision_timing(market):
    panel, signals = market
    row = {"code": "000001.SZ", "decision_time": "2023-01-02T15:30:00+08:00", "prediction": 1.}
    good = pd.DataFrame([row])
    assert g.signal_matrix(good, panel).iloc[0,0] == 1
    with pytest.raises(ValueError, match="多份分数"):
        g.signal_matrix(pd.concat([good,good]), panel)
    for time in ("2023-01-02T15:30:00", "2023-01-02T16:00:00+08:00"):
        with pytest.raises(ValueError):
            g.signal_matrix(pd.DataFrame([{**row,"decision_time":time}]), panel)


def snapshot(tmp_path, panel, signals):
    data = tmp_path / "data/feed"
    (data / "bars").mkdir(parents=True)
    rows = pd.concat([frame.stack().rename(field) for field,frame in panel.items()],axis=1).reset_index()
    rows.columns = ["date","code",*panel]
    for field in ("high", "low"):
        rows[field] = rows.close
    rows["amount"] = 1e7
    rows.to_parquet(data/"bars/part.parquet",index=False)
    experiments.write(data/"snapshot.json", {"data_snapshot_id":"fixture", "source":"fixture", "start":str(signals.index[0].date()),"end":str(signals.index[-1].date())})
    pd.DataFrame({"date":signals.index.strftime("%Y-%m-%d"),"is_open":1}).to_csv(data/"calendar.csv",index=False)
    source = tmp_path/"runs/source"
    source.mkdir(parents=True)
    (source / "final_model").mkdir()
    experiments.write(source/"final_model/meta.json", {})
    frame = signals.stack().rename("prediction").reset_index()
    frame.columns = ["decision_time","code","prediction"]
    frame["decision_time"] = (frame.decision_time+pd.Timedelta(hours=15,minutes=30)).dt.tz_localize("Asia/Shanghai").astype(str)
    frame.to_parquet(source/"predictions.parquet",index=False)
    return data, source


def test_worker_ui_result_and_csrf_share_same_experiment(tmp_path, market, monkeypatch):
    panel, signals = market
    data, source = snapshot(tmp_path, panel, signals)
    monkeypatch.setattr(ui, "datasets", lambda wd: {"feed":{"path":data}})
    app = ui.create_app(tmp_path, runner=lambda fn: fn())
    client = TestClient(app)
    assert client.get("/governance").status_code == 200
    form = {"source":"source", "dataset":"feed", "start":str(signals.index[300].date()),
            "end":str(signals.index[315].date()), "cash":"100000", "n_hold":"2", "hold_until_rank":"3",
            "max_weight":"40", "exposure_cap":"90", "cash_floor":"10", "target_volatility":"15", "slippage_bp":"2"}
    assert client.post("/governance",data=form).status_code == 403
    response = client.post("/governance",data={**form,"token":app.state.token})
    assert response.status_code == 200 and "查看治理对照" in response.text
    paths = list((tmp_path/"runs/governance").glob("*/result.json"))
    assert len(paths) == 1
    result = experiments.read(paths[0])
    assert result["status"] == "SUCCEEDED" and result["config"]["max_weight"] == .4
    report = client.get(f"/governance/{paths[0].parent.name}")
    assert report.status_code == 200 and "<polyline" in report.text
    assert "逐日决策" in report.text and "平均仓位" in report.text
    assert client.get(f"/governance/{paths[0].parent.name}/decisions").status_code == 200
    job = app.state.store.list()[0]
    with pytest.raises(ValueError, match="only failed"):
        app.state.store.retry(job["job_id"])
    # Simulate worker failure after output was written, before job completion.
    app.state.store._set(job["job_id"], "failed", "worker exited after writing result")
    app.state.store.retry(job["job_id"])
    worker.run_job(tmp_path,job["job_id"])
    assert len(list((tmp_path/"runs/governance").glob("*/result.json"))) == 2


def test_ml_warmup_is_explicit_not_claimed_as_ml(market):
    panel, signals = market
    risk = g.RiskData(panel)
    day = signals.index[100]
    samples = g.risk_samples(risk, signals, 2)
    features, ewma = risk.features(day, list(signals.columns[:2]))
    head = g.RiskHead(samples)
    assert head.predict(day, 100, features, ewma) == (ewma, "ML_WARMUP_EWMA")
    assert head.checkpoints == []


def test_matched_control_uses_realized_exposure_and_discloses_hindsight(market):
    panel, signals = market
    cfg = g.GovernanceConfig(n_hold=2, hold_until_rank=3, max_weight=.6, target_volatility=.07)
    # Larger cash reduces lot-size quantization; the small-account failure
    # case below deliberately checks that matching need not be attainable.
    arms, _ = g.compare(panel, signals, cfg, start=signals.index[300], end=signals.index[325],
                        initial_cash=1_000_000)
    control, reference = arms["ml_matched_fixed"], arms["ml"]
    diagnostic = control["diagnostic"]
    assert diagnostic["scope"] == "post_hoc_explanation_not_deployable"
    assert diagnostic["status"] == "MATCHED_WITHIN_TOLERANCE"
    actual = np.mean([row["exposure"] for row in control["path"][1:]])
    assert actual == pytest.approx(reference["metrics"]["mean_exposure"], abs=g.EXPOSURE_MATCH_TOLERANCE)
    assert diagnostic["signed_match_error"] == pytest.approx(actual-reference["metrics"]["mean_exposure"])
    targets = [d["unconstrained_exposure"] for d in control["decisions"]]
    assert len(set(targets)) == 1 and control["risk_models"] == []
    assert targets[0] == diagnostic["constant_target_exposure"]
    assert all(d["risk_reason"] == "FIXED_EXPOSURE" for d in control["decisions"])
    assert all(row["cash"] >= 0 for row in control["path"])


def test_unattainable_matching_reports_error_instead_of_forcing_weights(market, monkeypatch):
    panel, signals = market
    # The account is too small to buy even the minimum lot: no nonzero
    # exposure is attainable. A constructed reference tests the failure state.
    cfg = g.GovernanceConfig(n_hold=2, hold_until_rank=3, max_weight=.6)
    samples = g.risk_samples(g.RiskData(panel), signals, cfg.n_hold)
    kwargs = dict(start=signals.index[300],end=signals.index[310],initial_cash=1.,snapshot_id="fixture",samples=samples)
    fixed = g.replay(panel, signals, cfg, mode="fixed", **kwargs)
    reference = {"metrics":{"mean_exposure":.4}}
    monkeypatch.setattr(g,"EXPOSURE_MATCH_STEPS",3)
    arm = g.exposure_matched_control(panel, signals, cfg, reference, fixed, **kwargs)
    assert arm["diagnostic"]["status"] == "NOT_MATCHED"
    assert arm["diagnostic"]["signed_match_error"] == pytest.approx(-.4)
    assert arm["metrics"]["mean_exposure"] == 0
    assert "diagnostic" not in fixed


def test_failure_retained_and_existing_output_never_overwritten(tmp_path, market):
    panel, signals = market
    data, source = snapshot(tmp_path,panel,signals)
    panel["adj_factor"].iloc[302:, :] = 1.1
    # Alter the stored bars to require an unsupported action on an active holding.
    bars = pd.read_parquet(data/"bars/part.parquet")
    bars.loc[bars.date >= signals.index[302],"adj_factor"] = 1.1
    bars.to_parquet(data/"bars/part.parquet",index=False)
    output = tmp_path/"runs/governance/failure"
    with pytest.raises(ValueError,match="公司行动"):
        g.run(source/"predictions.parquet",data,output,g.GovernanceConfig(n_hold=2),
              start=str(signals.index[300].date()),end=str(signals.index[310].date()))
    assert experiments.read(output/"result.json")["status"] == "FAILED"
    with pytest.raises(FileExistsError):
        g.run(source/"predictions.parquet",data,output,g.GovernanceConfig(),start="2024-01-01",end="2024-01-10")


def test_cancellation_and_calendar_gaps_are_recorded(tmp_path, market):
    panel, signals = market
    data, source = snapshot(tmp_path,panel,signals)
    def cancel():
        raise Cancelled("user cancelled")
    output = tmp_path/"runs/governance/cancelled"
    with pytest.raises(Cancelled):
        g.run(source/"predictions.parquet",data,output,g.GovernanceConfig(n_hold=2),
              start=str(signals.index[300].date()),end=str(signals.index[310].date()),check=cancel)
    assert experiments.read(output/"result.json")["status"] == "CANCELLED"
    bars = pd.read_parquet(data/"bars/part.parquet")
    bars.loc[bars.date.ne(signals.index[305])].to_parquet(data/"bars/part.parquet",index=False)
    output = tmp_path/"runs/governance/gap"
    with pytest.raises(ValueError,match="日历不连续"):
        g.run(source/"predictions.parquet",data,output,g.GovernanceConfig(n_hold=2),
              start=str(signals.index[300].date()),end=str(signals.index[310].date()))
    assert experiments.read(output/"result.json")["status"] == "FAILED"
