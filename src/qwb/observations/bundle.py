"""Versioned point-in-time files; labels resolve revisions at each fit cutoff."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import experiments
from ..portfolio.contracts import identity, timestamp
from .windows import CHANNELS, WindowBuilder

FILES = ("manifest.json", "calendar.json", "bars.parquet", "universe.parquet")
TZ = "Asia/Shanghai"


def fingerprint(root):
    root = Path(root)
    files = {name: experiments.digest(root / name) for name in FILES}
    return {"files": files, "content_hash": experiments.object_digest(files)}


def session_time(day, clock):
    return (pd.Timestamp(day) + pd.Timedelta(clock + ":00")).tz_localize(TZ)


class PITBundle:
    def __init__(self, root, *, lookback=60):
        self.root = Path(root)
        before = fingerprint(self.root)
        manifest = experiments.read(self.root / "manifest.json")
        expected = {"schema_version", "data_snapshot_id", "data_kind", "license", "universe_policy"}
        if set(manifest) != expected or manifest["schema_version"] != "pit_numeric_v1":
            raise ValueError("unsupported PIT bundle manifest")
        identity(manifest["data_snapshot_id"], "data snapshot")
        identity(manifest["license"], "data license declaration")
        if manifest["data_kind"] not in {"synthetic", "market_development"}:
            raise ValueError("data_kind must be synthetic or market_development")
        if manifest["universe_policy"] != "explicit_daily_declarations":
            raise ValueError("declare explicit_daily_declarations universe policy")
        cal = experiments.read(self.root / "calendar.json")
        if set(cal) != {"sessions"} or not isinstance(cal["sessions"], list):
            raise ValueError("calendar.json requires an explicit sessions list")
        self.calendar = pd.DatetimeIndex(cal["sessions"])
        self.bars = pd.read_parquet(self.root / "bars.parquet")
        self.windows = WindowBuilder(self.bars, self.calendar, source_hash=before["content_hash"], lookback=lookback)
        self.bars = self.windows.frame
        # This recipe consumes complete daily bars, never pre-close partial OHLCV.
        close_times = self.bars["date"].dt.tz_localize(TZ) + pd.Timedelta(hours=15)
        if (self.bars["available_time"] < close_times).any():
            raise ValueError("complete daily bar availability cannot precede session close")
        self.universe = pd.read_parquet(self.root / "universe.parquet")
        required = {"code", "date", "available_time", "eligible", "revision"}
        if set(self.universe) != required:
            raise ValueError(f"universe requires exactly {sorted(required)}")
        u = self.universe.copy()
        if (u.empty or not u["code"].map(lambda x: isinstance(x, str) and bool(x.strip())).all()
                or not u["eligible"].map(lambda x: isinstance(x, (bool, np.bool_))).all()
                or not u["revision"].map(lambda x: isinstance(x, (int, np.integer))
                                         and not isinstance(x, bool) and x >= 0).all()):
            raise ValueError("universe ids, eligibility and revisions must be explicit")
        u["date"] = pd.to_datetime(u["date"])
        if u["date"].dt.tz is not None or not u["date"].isin(self.calendar).all():
            raise ValueError("universe dates must belong to the local trading calendar")
        u["available_time"] = pd.to_datetime([timestamp(x) for x in u["available_time"]], utc=True)
        keys = ["date", "code", "available_time", "revision"]
        if u.duplicated(keys).any():
            raise ValueError("ambiguous universe declarations")
        self.universe = u.sort_values(keys)
        self.manifest, self.identity = manifest, before
        self.assert_unchanged()

    def assert_unchanged(self):
        if fingerprint(self.root) != self.identity:
            raise ValueError("PIT bundle content changed during the run")

    def universe_at(self, decision):
        stamp = timestamp(decision)
        day = stamp.tz_convert(TZ).tz_localize(None).normalize()
        rows = self.universe[(self.universe["date"] == day) & (self.universe["available_time"] <= stamp)]
        rows = rows.drop_duplicates("code", keep="last").sort_values("code")
        if rows.empty:
            raise ValueError(f"no decision-visible universe declaration at {stamp}")
        return tuple(rows["code"]), set(rows.loc[rows["eligible"], "code"])

    def samples(self, *, min_history, last_test, max_samples, log=lambda *_: None):
        values, masks, rows, coverage = [], [], [], []
        count = 0
        for day in self.calendar[self.calendar <= pd.Timestamp(last_test)]:
            decision = session_time(day, "15:30")
            assets, eligible = self.universe_at(decision)
            obs = self.windows.at(decision, assets, eligible=eligible)
            keep = obs.asset_mask[0] & (obs.lengths[0] >= min_history)
            # A prediction also requires a price at the end of the decision window.
            keep &= obs.valid[0, :, -1, :4].all(axis=1)
            n = int(keep.sum())
            count += n
            if count > max_samples:
                raise ValueError("predeclared observation sample budget exceeded")
            if n:
                values.append(obs.values[0, keep])
                masks.append(obs.valid[0, keep])
                for code in np.asarray(assets)[keep]:
                    rows.append({"code": str(code), "decision_time": str(decision)})
            coverage.append({"decision_time": str(decision), "declared": len(assets),
                             "eligible": len(eligible), "predicted_candidates": n,
                             "ineligible": len(assets) - len(eligible),
                             "excluded_missing_or_short_history": len(eligible) - n})
            log(f"observations {day.date()}: {n}/{len(eligible)} eligible samples")
        if not rows:
            raise ValueError("no usable causal observations")
        return np.concatenate(values), np.concatenate(masks), pd.DataFrame(rows), pd.DataFrame(coverage)

    def labels_at(self, samples, cutoff):
        """Use only next-session bar revisions visible by this model/evaluation cutoff."""
        cutoff = timestamp(cutoff)
        visible = self.bars[self.bars["available_time"] <= cutoff].drop_duplicates(["code", "date"], keep="last")
        lookup = visible.set_index(["code", "date"])
        result = []
        for row in samples.itertuples(index=False):
            day = timestamp(row.decision_time).tz_convert(TZ).tz_localize(None).normalize()
            index = self.calendar.get_loc(day) + 1
            if index == len(self.calendar):
                result.append({"label": np.nan, "label_available_time": None,
                               "label_start": None, "label_end": None})
                continue
            execution_day = self.calendar[index]
            start, end = session_time(execution_day, "09:30"), session_time(execution_day, "15:00")
            label, available = np.nan, None
            key = (row.code, execution_day)
            if key in lookup.index:
                bar = lookup.loc[key]
                if np.isfinite(bar["open"]) and np.isfinite(bar["close"]) and min(bar["open"], bar["close"]) > 0:
                    label = float(bar["close"] / bar["open"] - 1)
                    available = str(max(end, bar["available_time"]))
            result.append({"label": label, "label_available_time": available,
                           "label_start": str(start), "label_end": str(end)})
        return pd.DataFrame(result)
