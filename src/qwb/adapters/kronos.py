"""Kronos zero-shot adapter (SPEC §3 model menu).

Declares: input = one OHLCV(+amount) series per stock, univariate per series,
no covariates, no text; output = point forecast of the next `pred_len` bars.
The ranking score is the forecast return over the task's label window.

Kronos is not a pip package: code is loaded from a pinned clone under
third_party/Kronos and weights from local snapshots (safetensors only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

INPUTS = {"series": "ohlcv", "covariates": False, "text": False}
OUTPUT = "point_forecast"


class KronosScorer:
    def __init__(self, root: str | Path, model_dir: str, tokenizer_dir: str,
                 device: str = "cuda:0", max_context: int = 512):
        root = Path(root)
        code = root / "third_party" / "Kronos"
        if str(code) not in sys.path:
            sys.path.insert(0, str(code))
        from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

        tok = KronosTokenizer.from_pretrained(str(root / tokenizer_dir))
        mdl = Kronos.from_pretrained(str(root / model_dir))
        self.pred = KronosPredictor(mdl, tok, device=device, max_context=max_context)

    def score_day(self, P: dict, t: int, codes: list[str], lookback: int, card: dict,
                  future_days: pd.DatetimeIndex, batch: int = 256, seed: int = 0) -> pd.Series:
        """Forecast return from label.start to label.end for `codes` decided on day t."""
        import torch

        s, e = card["label"]["start"], card["label"]["end"]
        pred_len = int(e["day"])
        if len(future_days) < pred_len:
            raise ValueError("calendar does not cover the forecast horizon")
        sl = slice(t - lookback + 1, t + 1)
        dates = P["close"].index
        xs = pd.Series(dates[sl])
        ys = pd.Series(future_days[:pred_len])
        af = P["adj_factor"].iloc[sl]

        def frame(c):
            px = {k: (P[k].iloc[sl][c] * af[c]).to_numpy() for k in ("open", "high", "low", "close")}
            amt = P["amount"].iloc[sl][c].to_numpy()
            return pd.DataFrame(px | {"volume": amt / P["close"].iloc[sl][c].to_numpy(),
                                      "amount": amt})

        out = {}
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            torch.manual_seed(seed + t * 7919 + i)      # sampling is stochastic; fixed per call
            preds = self.pred.predict_batch([frame(c) for c in chunk], [xs] * len(chunk),
                                            [ys] * len(chunk), pred_len=pred_len, T=1.0,
                                            top_p=0.9, sample_count=1, verbose=False)
            for c, df in zip(chunk, preds):
                # forecast rows 0..pred_len-1 are days t+1..t+pred_len
                a = df[s.get("price", "open")].iloc[int(s["day"]) - 1]
                b = df[e.get("price", "open")].iloc[int(e["day"]) - 1]
                out[c] = b / a - 1 if a > 0 else np.nan
        return pd.Series(out, dtype="float64")
