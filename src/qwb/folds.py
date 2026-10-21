"""Label maturity and fold manifests (SPEC §6).

A sample decided on trading day t has a label that ends at
label.end (trading-day offset + clock time). It may enter a fit only if
that label had matured by the fit's cutoff.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


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
