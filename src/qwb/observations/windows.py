"""Build raw numeric windows from information actually available at a decision."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from ..experiments import object_digest
from ..portfolio.contracts import identity, timestamp

CHANNELS = ("open", "high", "low", "close", "volume", "amount")
RECIPE = "causal_ohlcva_window_v1"


@dataclass(frozen=True)
class ObservationBatch:
    values: np.ndarray
    valid: np.ndarray
    available_ns: np.ndarray
    asset_mask: np.ndarray
    lengths: np.ndarray
    asset_ids: tuple[str, ...]
    decision_time: str
    calendar: tuple[str, ...]
    manifest: dict


class WindowBuilder:
    """No full-dataset fitted statistics, forward filling, or future universe inference."""

    def __init__(self, bars: pd.DataFrame, calendar: pd.DatetimeIndex, *, source_hash: str,
                 lookback=60, timezone="Asia/Shanghai"):
        required = {"code", "date", "available_time", *CHANNELS}
        if not required <= set(bars):
            raise ValueError(f"observations require {sorted(required)}")
        if type(lookback) is not int or lookback < 2:
            raise ValueError("lookback must be an integer >=2")
        identity(source_hash, "source_hash")
        if not source_hash.startswith("sha256:") or len(source_hash) != 71:
            raise ValueError("full source content SHA-256 is required")
        cal = pd.DatetimeIndex(calendar)
        if (cal.tz is not None or cal.has_duplicates or not cal.is_monotonic_increasing
                or not cal.equals(cal.normalize()) or cal.empty or cal.hasnans):
            raise ValueError("calendar must contain sorted unique local trading dates")
        frame = bars.copy()
        if frame["code"].isna().any() or not frame["code"].map(lambda x: isinstance(x, str) and bool(x.strip())).all():
            raise ValueError("observation asset ids must be explicit strings")
        frame["date"] = pd.to_datetime(frame["date"])
        if frame["date"].dt.tz is not None or not frame["date"].isin(cal).all():
            raise ValueError("bar dates must be local dates in the declared calendar")
        frame["available_time"] = pd.to_datetime([timestamp(x) for x in frame["available_time"]], utc=True)
        earliest = frame["date"].dt.tz_localize(timezone).dt.tz_convert("UTC")
        if (frame["available_time"] < earliest).any():
            raise ValueError("bar availability cannot precede its session")
        if "revision" not in frame:
            frame["revision"] = 0
        if not frame["revision"].map(lambda x: isinstance(x, (int, np.integer)) and not isinstance(x, bool) and x >= 0).all():
            raise ValueError("revision must be a nonnegative integer")
        keys = ["code", "date", "available_time", "revision"]
        if frame.duplicated(keys).any():
            raise ValueError("ambiguous duplicate observations")
        for name in CHANNELS:
            frame[name] = pd.to_numeric(frame[name], errors="raise")
        self.frame, self.calendar = frame.sort_values(keys), cal
        self.lookback, self.timezone = lookback, timezone
        content = pd.util.hash_pandas_object(self.frame[[*keys, *CHANNELS]], index=False).to_numpy().tobytes()
        self.manifest = {"recipe": RECIPE, "lookback": lookback, "channels": CHANNELS,
                         "source_hash": source_hash, "timezone": timezone,
                         "loaded_content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
                         "calendar_hash": object_digest([str(day.date()) for day in cal]),
                         "price_transform": "log_price_over_last_available_window_close",
                         "activity_transform": "window_centered_log1p",
                         "missing": "zero_with_per_channel_mask_no_forward_fill",
                         "uses_fitted_dataset_statistics": False}
        self.manifest["recipe_hash"] = object_digest(self.manifest)

    def at(self, decision_time, assets, *, eligible=None):
        stamp = timestamp(decision_time)
        local_day = stamp.tz_convert(self.timezone).tz_localize(None).normalize()
        if local_day not in self.calendar:
            raise ValueError("decision must lie on a declared trading session")
        assets = tuple(assets)
        if not assets or len(set(assets)) != len(assets):
            raise ValueError("assets must be explicit, nonempty and unique")
        for code in assets:
            identity(code, "asset_id")
        eligible = set(assets) if eligible is None else set(eligible)
        if eligible - set(assets):
            raise ValueError("eligible assets must belong to the declared asset order")
        end = self.calendar.get_loc(local_day) + 1
        dates = self.calendar[max(0, end - self.lookback):end]
        pad = self.lookback - len(dates)
        visible = self.frame[(self.frame["available_time"] <= stamp)
                             & self.frame["date"].isin(dates) & self.frame["code"].isin(assets)]
        visible = visible.drop_duplicates(["code", "date"], keep="last")
        shape = (1, len(assets), self.lookback, len(CHANNELS))
        values, valid = np.zeros(shape, np.float32), np.zeros(shape, bool)
        available = np.full((1, len(assets), self.lookback), np.iinfo(np.int64).min, np.int64)
        lengths = np.zeros((1, len(assets)), np.int64)
        for i, code in enumerate(assets):
            rows = visible[visible["code"] == code].set_index("date").reindex(dates)
            raw = rows[list(CHANNELS)].to_numpy(dtype=float)
            mask = np.isfinite(raw) & (raw >= 0)
            mask[:, :4] &= raw[:, :4] > 0
            closes = np.flatnonzero(mask[:, 3])
            transformed = np.zeros_like(raw)
            if len(closes):
                ref = raw[closes[-1], 3]
                transformed[:, :4] = np.log(np.where(mask[:, :4], raw[:, :4] / ref, 1.0))
            else:
                mask[:, :4] = False
            for j in (4, 5):
                active = mask[:, j]
                if active.any():
                    activity = np.log1p(raw[active, j])
                    transformed[active, j] = activity - activity.mean()
            values[0, i, pad:] = transformed
            valid[0, i, pad:] = mask
            available[0, i, pad:] = rows["available_time"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
            lengths[0, i] = mask[:, :4].all(axis=1).sum()
        if not np.isfinite(values).all():
            raise ValueError("nonfinite transformed observation")
        for array in (values, valid, available, lengths):
            array.setflags(write=False)
        asset_mask = np.array([[code in eligible for code in assets]], bool)
        asset_mask.setflags(write=False)
        return ObservationBatch(values, valid, available, asset_mask, lengths, assets,
                                str(stamp), tuple(str(d.date()) for d in dates), dict(self.manifest))
