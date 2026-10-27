"""Serve an isolated synthetic timing workspace for browser QA (dev deps only)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'tests'))

import pandas as pd
import pytest
import uvicorn

from qwb import experiments as ex, strategy_workspace as ws, ui
from test_strategy_workspace import setup


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--port', type=int, default=8806)
    args = parser.parse_args()
    workdir = args.output.resolve()
    if workdir.exists():
        raise SystemExit('Use a new output directory; previous attempts are retained.')
    workdir.mkdir(parents=True)
    patch = pytest.MonkeyPatch()
    _, run, feed, _ = setup.__wrapped__(workdir, patch)
    ex.write(feed/'snapshot.json', {'source': 'synthetic_timing_qa', 'data_snapshot_id': 'fixture'})
    patch.setattr(ui, 'datasets', lambda p: {'daily': {'path': feed}})
    sid, vid = ws.create(workdir, run, feed, name='调仓时机 · 合成检验', cash=100000,
                         preset='direct', overrides={'n_hold': 1, 'hold_until_rank': 1,
                         'max_weight': 1., 'exposure_cap': .5}, cost_model='commission_only')
    frame = pd.read_parquet(run/'predictions.parquet')
    frame['available_at'] = frame.decision_time
    frame['data_asof'] = (pd.to_datetime(frame.decision_time, utc=True)-pd.Timedelta(minutes=30)).astype(str)
    days, codes = sorted(frame.decision_time.unique()), list(frame.code.unique())
    frame['prediction'] = [(.02 if code == (codes[0] if at < days[2] else codes[1]) else 0.)
                           for at, code in zip(frame.decision_time, frame.code)]
    frame.to_csv(workdir/'signals.csv', index=False)
    url = f'http://127.0.0.1:{args.port}/strategies/{sid}/{vid}/timing'
    ex.write(workdir/'qa.json', {'url': url, 'synthetic_only': True, 'signals': str(workdir/'signals.csv')})
    print(url, flush=True)
    uvicorn.run(ui.create_app(workdir, port=args.port, runner=lambda fn: fn()),
                host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
