"""Task card (SPEC §5): what is predicted, when it is decided and executed.

Time points are written relative to the signal day t:
    {day: 0, time: "15:00"}            -> t, 15:00
    {day: 1, time: "09:30", price: open} -> t+1 open
The single correctness rule enforced here is the ordering
    signal_available <= decision < execution <= label.start < label.end
Which execution price to use (open, VWAP, close...) is the user's choice.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .issues import ERROR, WARN, Report

TARGET_TYPES = {"return_rank", "return", "volatility", "volume",
                "event_class", "delist_risk", "custom"}
TEMPLATES = {"low_freq", "daily", "intraday"}
OUTPUTS = {"score", "probability", "class", "target_weight", "point_forecast"}


def _minutes(tp: dict) -> int:
    """Convert {day, time} to minutes since t 00:00 for ordering."""
    hh, mm = str(tp.get("time", "00:00")).split(":")
    return int(tp["day"]) * 1440 + int(hh) * 60 + int(mm)


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate(card: dict) -> Report:
    rep = Report()
    for key in ("name", "market", "template", "target", "timing", "label", "output", "metrics"):
        if key not in card:
            rep.add(ERROR, "MISSING_FIELD", f"task card needs `{key}`")
    if not rep.ok:
        return rep

    if card["template"] not in TEMPLATES:
        rep.add(ERROR, "BAD_TEMPLATE", f"template must be one of {sorted(TEMPLATES)}")
    if card["target"].get("type") not in TARGET_TYPES:
        rep.add(ERROR, "BAD_TARGET", f"target.type must be one of {sorted(TARGET_TYPES)}")
    if card["output"] not in OUTPUTS:
        rep.add(ERROR, "BAD_OUTPUT", f"output must be one of {sorted(OUTPUTS)}")
    if not card["metrics"]:
        rep.add(ERROR, "NO_METRICS", "at least one acceptance metric is required")

    points = {}
    timing, label = card["timing"], card["label"]
    for name, tp in [("signal_available", timing.get("signal_available")),
                     ("decision", timing.get("decision")),
                     ("execution", timing.get("execution")),
                     ("label.start", label.get("start")),
                     ("label.end", label.get("end"))]:
        if not isinstance(tp, dict) or "day" not in tp:
            rep.add(ERROR, "BAD_TIMEPOINT", f"{name} must be a mapping with `day` (and `time`)")
            continue
        try:
            points[name] = _minutes(tp)
        except (ValueError, KeyError):
            rep.add(ERROR, "BAD_TIMEPOINT", f"{name}.time must look like HH:MM")
    if len(points) < 5:
        return rep

    chain = [("signal_available", "decision", "SIGNAL_AFTER_DECISION", False),
             ("decision", "execution", "EXECUTION_NOT_AFTER_DECISION", True),
             ("execution", "label.start", "LABEL_BEFORE_EXECUTION", False),
             ("label.start", "label.end", "EMPTY_LABEL_WINDOW", True)]
    for a, b, code, strict in chain:
        bad = points[a] >= points[b] if strict else points[a] > points[b]
        if bad:
            rep.add(ERROR, code, f"{a} must be {'before' if strict else 'no later than'} {b}")

    h = card["target"].get("horizon_days")
    if h is not None and card["template"] == "daily":
        span = int(label["end"]["day"]) - int(label["start"]["day"])
        if span != int(h):
            rep.add(WARN, "HORIZON_MISMATCH",
                    f"target.horizon_days={h} but label spans {span} trading days")
    return rep


def derive_purge_embargo(card: dict) -> dict:
    """Purge/embargo lengths from the label window (SPEC §6), in trading days.

    A training sample at t carries information up to label.end; any sample
    whose label window reaches past the train/test cut must be purged, so the
    gap equals the number of days from the decision day to label.end.
    """
    decision_day = int(card["timing"]["decision"]["day"])
    span = int(card["label"]["end"]["day"]) - decision_day
    return {"purge_days": span, "embargo_days": span}
