"""Label maturity and fold manifests (SPEC §6).

A sample decided on trading day t has a label that ends at
label.end (trading-day offset + clock time). It may enter a fit only if
that label had matured by the fit's cutoff.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd
import numpy as np


def label_end_times(dates: pd.Series, calendar: pd.DatetimeIndex, card: dict,
                    tz: str) -> pd.Series:
    """UTC time at which each sample's label is fully observed.

    Samples whose label end falls beyond the calendar get NaT (immature).
    """
    cal = pd.DatetimeIndex(calendar).normalize().unique().sort_values()
    end = card["label"]["end"]
    offset = int(end["day"])
    hh, mm = str(end.get("time", "00:00")).split(":")
    pos = cal.get_indexer(pd.to_datetime(dates).dt.normalize())
    if (pos < 0).any():
        raise ValueError("some sample dates are not trading days in the calendar")
    tgt = pos + offset
    out = pd.Series(pd.NaT, index=dates.index, dtype="datetime64[ns, UTC]")
    ok = tgt < len(cal)
    if ok.any():
        local = cal[tgt[ok]] + pd.Timedelta(hours=int(hh), minutes=int(mm))
        out[ok] = local.tz_localize(tz).tz_convert("UTC")
    return out


def mature_mask(label_end: pd.Series, fit_cutoff) -> pd.Series:
    cutoff = pd.Timestamp(fit_cutoff)
    if cutoff.tzinfo is None:
        raise ValueError("fit_cutoff must be timezone-aware")
    return label_end.notna() & (label_end <= cutoff.tz_convert("UTC"))


@dataclass
class FoldManifest:
    """What a fold was fitted on; written to the experiment record."""
    fold: int
    fit_cutoff: str
    train_start: str
    train_end: str
    n_train: int
    n_dropped_immature: int
    max_label_end: str
    data_snapshot_id: str
    model_version: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_fold(fold: int, dates: pd.Series, label_end: pd.Series, train_window: tuple,
               fit_cutoff, data_snapshot_id: str, model_version: str):
    """Training mask for one fold plus its manifest.

    Every learned transform (scaler, imputer, feature selector, tokenizer)
    must be fitted on exactly this mask.
    """
    d = pd.to_datetime(dates).dt.normalize()
    lo, hi = (pd.Timestamp(x) for x in train_window)
    in_window = (d >= lo) & (d <= hi)
    mature = mature_mask(label_end, fit_cutoff)
    mask = in_window & mature
    used = label_end[mask]
    man = FoldManifest(
        fold=fold,
        fit_cutoff=str(pd.Timestamp(fit_cutoff)),
        train_start=str(d[mask].min().date()) if mask.any() else "",
        train_end=str(d[mask].max().date()) if mask.any() else "",
        n_train=int(mask.sum()),
        n_dropped_immature=int((in_window & ~mature).sum()),
        max_label_end=str(used.max()) if mask.any() else "",
        data_snapshot_id=data_snapshot_id,
        model_version=model_version,
    )
    return mask, man


def purged_tail_split(dates: pd.DatetimeIndex, label_end: pd.Series, eligible: np.ndarray,
                      *, validation_days: int, min_train_days: int, decision_time: str,
                      tz: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """Select inside an outer training mask; no outer-test rows or labels enter.

    Inner training labels must be known by the first validation decision. The
    complete outer matured mask remains available for the subsequent refit.
    """
    if eligible.shape[0] != len(dates) or eligible.ndim != 2 or len(label_end) != len(dates):
        raise ValueError("inner split is not aligned to the training calendar")
    candidates = np.flatnonzero(eligible.sum(axis=1) >= 20)
    if len(candidates) <= validation_days:
        raise ValueError("insufficient historical dates for purged validation (>=20 names/day)")
    validation = candidates[-validation_days:]
    start = int(validation[0])
    hh, mm = map(int, decision_time.split(":"))
    cutoff = (dates[start] + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    mature = mature_mask(label_end, cutoff).to_numpy()
    before = np.arange(len(dates)) < start
    train = eligible & (before & mature)[:, None]
    valid = eligible & np.isin(np.arange(len(dates)), validation)[:, None]
    if (train.sum(axis=1) > 0).sum() < min_train_days:
        raise ValueError("insufficient historical training dates after purging inner validation")
    used = np.flatnonzero(train.any(axis=1))
    return train, valid, {
        "method": "purged_tail_rank_ic_v1", "fit_cutoff": str(cutoff),
        "train_start": str(dates[used[0]].date()), "train_end": str(dates[used[-1]].date()),
        "max_label_end": str(label_end.iloc[used].max()),
        "validation_start": str(dates[start].date()),
        "validation_end": str(dates[validation[-1]].date()), "validation_days": len(validation),
        "n_train": int(train.sum()), "n_validation": int(valid.sum()),
        "purged_rows": int((eligible & (before & ~mature)[:, None]).sum()),
        "uses_outer_test_labels": False, "refit_on_all_matured": True,
    }


@dataclass(frozen=True)
class TimeFold:
    """Explicit aware decision intervals; legacy annual manifests stay unchanged."""
    fold: int
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str

    def __post_init__(self):
        if type(self.fold) is not int or self.fold < 0:
            raise ValueError("fold id must be a nonnegative integer")
        points = [pd.Timestamp(getattr(self, key)) for key in (
            "train_start", "train_end", "validation_start", "validation_end", "test_start", "test_end")]
        if any(pd.isna(x) or x.tzinfo is None for x in points):
            raise ValueError("fold boundaries must be finite and timezone-aware")
        if not points[0] <= points[1] < points[2] <= points[3] < points[4] <= points[5]:
            raise ValueError("fold windows must be ordered and nonoverlapping")

    def to_dict(self):
        return asdict(self)


def rolling_time_folds(calendar, *, first_test, frequency="M", validation_days=20,
                       min_train_days=60, train_days=None, decision_time="15:30", tz="Asia/Shanghai"):
    """Monthly/weekly test blocks, trailing validation and expanding/sliding training."""
    cal = pd.DatetimeIndex(calendar)
    if (cal.empty or cal.tz is not None or cal.hasnans or cal.has_duplicates
            or not cal.is_monotonic_increasing or not cal.equals(cal.normalize())):
        raise ValueError("calendar must contain sorted unique local trading dates")
    if frequency not in {"M", "W-FRI"}:
        raise ValueError("supported frequencies are M and W-FRI")
    if (type(validation_days) is not int or validation_days < 1
            or type(min_train_days) is not int or min_train_days < 1
            or (train_days is not None and (type(train_days) is not int or train_days < min_train_days))):
        raise ValueError("invalid training/validation window lengths")
    hh, mm = map(int, decision_time.split(":"))
    if not 0 <= hh < 24 or not 0 <= mm < 60:
        raise ValueError("invalid decision clock")
    stamps = (cal + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    start = pd.Timestamp(first_test)
    if start.tzinfo is not None:
        start = start.tz_convert(tz).tz_localize(None)
    first = cal.searchsorted(start.normalize())
    if first == len(cal):
        raise ValueError("no test sessions in calendar")
    groups = cal.to_period(frequency)
    result = []
    for period in groups[first:].unique():
        test = np.flatnonzero((groups == period) & (np.arange(len(cal)) >= first))
        stop_train = int(test[0]) - validation_days
        start_train = max(0, stop_train - train_days) if train_days is not None else 0
        if stop_train - start_train < min_train_days:
            raise ValueError("insufficient training history before test/validation")
        result.append(TimeFold(len(result), str(stamps[start_train]), str(stamps[stop_train - 1]),
                               str(stamps[stop_train]), str(stamps[test[0] - 1]),
                               str(stamps[test[0]]), str(stamps[test[-1]])))
    return result


def time_fold_masks(decisions, label_end, fold: TimeFold, *, label_available=None, embargo="0s"):
    """Purge by actual label availability at validation/test decisions, not row count."""
    def aware(values, name, *, allow_nat=False):
        raw = pd.Series(values)
        for value in raw:
            if pd.isna(value) and allow_nat:
                continue
            t = pd.Timestamp(value)
            if pd.isna(t) or t.tzinfo is None:
                raise ValueError(f"{name} requires timezone-aware timestamps")
        return pd.to_datetime(raw, utc=True).reset_index(drop=True)

    d = aware(decisions, "decisions")
    ends = aware(label_end, "label end", allow_nat=True)
    available = ends if label_available is None else aware(label_available, "label availability", allow_nat=True)
    if len(d) != len(ends) or len(d) != len(available):
        raise ValueError("sample times must be aligned")
    if (ends.notna() & (ends <= d)).any():
        raise ValueError("forecast labels must end after their decisions")
    if (available.notna() & ends.notna() & (available < ends)).any():
        raise ValueError("label cannot be available before it ends")
    gap = pd.Timedelta(embargo)
    if pd.isna(gap) or gap < pd.Timedelta(0):
        raise ValueError("embargo duration must be nonnegative")
    tr_start, tr_end, va_start, va_end, te_start, te_end = [pd.Timestamp(getattr(fold, key)) for key in (
        "train_start", "train_end", "validation_start", "validation_end", "test_start", "test_end")]
    maturity = ends.notna() & available.notna()
    train_window = d.between(tr_start, tr_end)
    valid_window = d.between(va_start, va_end)
    train = train_window & maturity & (available <= va_start - gap)
    valid = valid_window & maturity & (available <= te_start - gap)
    test = d.between(te_start, te_end)
    if not train.any() or not valid.any() or not test.any():
        raise ValueError("empty fold after label maturity purging")
    return train.to_numpy(), valid.to_numpy(), test.to_numpy(), {
        **fold.to_dict(), "method": "explicit_time_maturity_v1", "embargo": str(gap),
        "fit_cutoff": str(va_start - gap), "selection_cutoff": str(te_start - gap),
        "n_train": int(train.sum()), "n_validation": int(valid.sum()), "n_test": int(test.sum()),
        "n_purged_train": int((train_window & ~train).sum()),
        "n_purged_validation": int((valid_window & ~valid).sum()),
        "max_train_label_available": str(available[train].max()),
        "max_validation_label_available": str(available[valid].max()),
        "uses_outer_test_labels_for_selection": False}
