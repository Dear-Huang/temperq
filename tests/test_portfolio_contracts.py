"""State provenance, target isolation, and impossible intent rejection."""

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qwb.portfolio.contracts import (AccountState, PendingOrder, Position,
                                     SettlementLot, TargetWeights, TASK, validate_target)
from qwb.bench.contracts import align_predictions

DECISION = "2024-01-02T15:30:00+08:00"
OPEN = "2024-01-03T09:30:00+08:00"


def state():
    return AccountState(DECISION, "fixture", 10000.0, (Position("A", 100, 100, 10.0, DECISION),))


def target(s=None, **kwargs):
    s = s or state()
    execution_at = kwargs.pop("execution_not_before", OPEN)
    return TargetWeights("decision-1", s.observed_at, execution_at, TASK, "model-1", s.data_snapshot_id,
                         s.state_id, "known-assets", "protocol-1", **{
                             "weights": (("A", .4), ("B", .4)), "cash_weight": .2, **kwargs})


def validate(t, s=None, **kwargs):
    return validate_target(t, s or state(), assets=("A", "B"), universe_id="known-assets",
                           protocol_id="protocol-1", execution_at=OPEN, **kwargs)


def test_roundtrip_preserves_original_target_and_actual_state():
    s, t = state(), target()
    assert AccountState.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    assert TargetWeights.from_dict(json.loads(json.dumps(t.to_dict()))) == t
    assert validate(t) is t
    assert s.weights(("A", "B")).tolist() == [1000 / 11000, 0]
    with pytest.raises(ValueError, match="retain"):
        s.weights(("B",))


def test_tolerated_sum_error_is_not_renormalized():
    t = target(cash_weight=.2000001)
    assert validate(t).cash_weight == .2000001
    assert TargetWeights.from_dict(t.to_dict()).to_dict() == t.to_dict()


@pytest.mark.parametrize("weights,cash", [((("A", np.nan),), 0), ((("A", np.inf),), 0),
    ((("A", -.000000001), ("B", .8)), .2), ((("A", .5), ("A", .3)), .2),
    ((("A", .5),), .2), ((("A", True),), 0)])
def test_invalid_weights_never_get_silently_repaired(weights, cash):
    with pytest.raises(ValueError):
        target(weights=weights, cash_weight=cash)


@pytest.mark.parametrize("field,value", [("state_id", "old"), ("data_snapshot_id", "other"),
    ("task", "score-task"), ("universe_id", "other"), ("protocol_id", "other"),
    ("execution_not_before", "2024-01-04T09:30:00+08:00")])
def test_stale_identity_or_timing_is_rejected(field, value):
    with pytest.raises(ValueError):
        validate(replace(target(), **{field: value}))


def test_unknown_assets_retained_holdings_and_limits():
    with pytest.raises(ValueError, match="unknown"):
        validate(target(weights=(("UNKNOWN", .8),)))
    with pytest.raises(ValueError, match="constraint"):
        validate(target(), max_weight=.3)
    with pytest.raises(ValueError, match="constraint"):
        validate(target(), min_cash=.3)
    with pytest.raises(ValueError, match="sector"):
        validate(target(), sector_by_asset={"A": "finance", "B": "finance"}, sector_caps={"finance": .7})
    with pytest.raises(ValueError, match="state"):
        validate(target(), replace(state(), cash=10001))


def test_future_marks_unknown_settlement_and_live_orders_are_explicit():
    with pytest.raises(ValueError, match="timezone"):
        replace(state(), observed_at="2024-01-02 15:30")
    with pytest.raises(ValueError, match="future"):
        replace(state(), positions=(Position("A", 100, 100, 10, OPEN),))
    with pytest.raises(ValueError, match="reconcile"):
        Position("A", 100, 0, 10, DECISION)
    lot = SettlementLot(100, OPEN)
    s = replace(state(), positions=(Position("A", 100, 0, 10, DECISION, (lot,)),))
    assert s.positions[0].sellable_quantity == 0
    order = PendingOrder("order", "A", "BUY", 100, DECISION, OPEN)
    assert replace(s, pending_orders=(order,)).state_id != s.state_id
    with pytest.raises(ValueError, match="settled"):
        replace(s, observed_at=OPEN)


def test_score_interface_rejects_target_fields_even_with_fabricated_scores(tmp_path):
    f = pd.DataFrame({"code": ["A"], "decision_time": [DECISION], "task": ["x"],
                      "prediction": [0.4], "model_version": ["m"], "data_snapshot_id": ["s"],
                      "target_weight": [.4]})
    path = tmp_path / "disguised.parquet"
    f.to_parquet(path)
    with pytest.raises(ValueError, match="separate portfolio"):
        align_predictions(path, pd.DatetimeIndex(["2024-01-02"]), pd.Index(["A"]), {})


def test_unknown_serialized_fields_are_rejected():
    with pytest.raises(ValueError, match="fields"):
        AccountState.from_dict({**state().to_dict(), "future_price": 99})
    with pytest.raises(ValueError, match="fields"):
        TargetWeights.from_dict({**target().to_dict(), "prediction": 1})


def test_new_task_is_valid_but_not_silently_routed_through_legacy_training():
    from qwb import runconfig, taskcard

    examples = Path(__file__).resolve().parents[1] / "examples"
    card = taskcard.load(examples / "cn_alloc_daily_v1.yaml")
    assert taskcard.validate(card).ok
    config = runconfig.load(examples / "run_smoke.yaml")
    config["task"] = card
    resolved, report = runconfig.resolve(config, examples)
    assert resolved is None and "UNSUPPORTED_NATIVE_TASK" in report.codes()
