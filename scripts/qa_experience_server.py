"""Serve a new isolated workspace with synthetic data and no pre-trained models."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from qwb import daily_data, pipeline, ui
import numpy as np
import pandas as pd
import uvicorn


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--port', type=int, default=8784)
    parser.add_argument('--resume', action='store_true', help='Resume an existing isolated synthetic QA workspace')
    args = parser.parse_args()
    workdir = args.output.resolve()
    def serve():
        def run(fn):
            threading.Thread(target=fn, daemon=True).start()
        print(f'QA workspace: {workdir}; synthetic data only', flush=True)
        uvicorn.run(ui.create_app(workdir, port=args.port, runner=run), host='127.0.0.1', port=args.port, log_level='warning')
    if args.resume:
        from qwb import experiments as ex
        snap=ex.read(daily_data.current(workdir/'data/qa_synthetic')/'snapshot.json')
        if snap.get('source')!='synthetic_experience_only':raise SystemExit('Resume requires the synthetic QA fixture.')
        serve()
        return
    if workdir.exists():
        raise SystemExit('Choose a new output directory; previous attempts are retained.')
    (workdir / 'examples').mkdir(parents=True)
    for name in ('cn_rank_5d.yaml', 'st_task.yaml', 'csv_template.csv'):
        shutil.copyfile(ROOT / 'examples' / name, workdir / 'examples' / name)
    dates = pd.bdate_range('2019-01-01', '2023-01-20')
    codes = [f'{i:06d}.SZ' for i in range(1, 41)]
    bars = pipeline.synth_bars(len(dates), len(codes))
    bars['date'] = np.repeat(dates, len(codes))
    bars['code'] = np.tile(codes, len(dates))
    bars['amount'] *= 100
    bars['down_limit'] = bars['close'] * .8
    bars['up_limit'] = bars['close'] * 1.2
    bars['is_st'] = 0
    calendar = pd.DataFrame({'date': pd.bdate_range('2019-01-01', '2027-01-01').strftime('%Y-%m-%d'), 'is_open': 1})
    daily_data.update(workdir / 'data/qa_synthetic', bars.to_csv(index=False).encode(),
        calendar.to_csv(index=False).encode(), source='synthetic_experience_only')
    # Same worker.execute code, isolated threaded job runner; no network/downloads.
    serve()


if __name__ == '__main__':
    main()
